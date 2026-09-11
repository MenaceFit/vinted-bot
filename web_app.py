"""
Vinted Monitor — serveur web FastAPI.

Remplace app.py (Tkinter) par une interface web temps réel (WebSocket).

Lancement :
    pip install -r requirements.txt
    python web_app.py          # → http://localhost:8080
"""
import asyncio
import json
import logging
import os
import time
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

import config as app_config
from core.performance import PerformanceMonitor
from core.scanner import Scanner
from database import database as db
from services import discord as discord_service
from services import telegram as telegram_service
from services.autobuy import run_autobuy, close_all as autobuy_close
from utils.logger import setup_logging

setup_logging(logging.INFO)
logger = logging.getLogger(__name__)

# ── Config globale ────────────────────────────────────────────────────────────
config_data: dict = app_config.load_config()
perf = PerformanceMonitor()
WEB_DIR = Path(__file__).parent / "web"


# ── WebSocket broadcast ───────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)
        logger.info(f"[WS] Client connecté ({len(self._connections)} total)")

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        msg = json.dumps(data, ensure_ascii=False)
        for ws in self._connections[:]:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def broadcast_sync(self, data: dict):
        """Thread-safe : appelle broadcast depuis le thread asyncio."""
        if _main_loop and not _main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.broadcast(data), _main_loop)


manager = ConnectionManager()
_main_loop: Optional[asyncio.AbstractEventLoop] = None

# ── Autobuy callbacks ─────────────────────────────────────────────────────────

async def _on_new_ad(ad: dict):
    manager.broadcast_sync({"type": "new_ad", "ad": ad})

    cfg = config_data
    autobuy_cfg = cfg.get("autobuy", {})
    if not autobuy_cfg.get("enabled", False):
        return

    token = app_config.get_secret("VINTED_TOKEN")
    if not token:
        return

    max_price = autobuy_cfg.get("max_price")
    result = await run_autobuy(
        ad=ad,
        token=token,
        max_price=max_price,
        markets_info=cfg.get("markets", {}),
    )
    manager.broadcast_sync({"type": "autobuy_result", "result": result})
    status_icon = "✅" if result.get("success") else "❌"
    logger.info(
        f"[Autobuy] {status_icon} {result.get('title', '')[:40]} — "
        f"{result.get('status')} ({result.get('elapsed_ms')}ms)"
    )


def _on_log(msg: str):
    manager.broadcast_sync({"type": "log", "msg": msg, "ts": datetime.now().strftime("%H:%M:%S")})


def _on_keyword_status(keyword: str, status: dict):
    manager.broadcast_sync({"type": "keyword_status", "keyword": keyword, "status": status})


def _on_notification_update(ad_id: str, channel: str, ok: bool):
    manager.broadcast_sync({"type": "notification_update", "ad_id": ad_id, "channel": channel, "ok": ok})


# ── Scanner ───────────────────────────────────────────────────────────────────

scanner = Scanner(
    config_provider=lambda: config_data,
    on_log=_on_log,
    on_new_ad=lambda ad: (
        asyncio.run_coroutine_threadsafe(_on_new_ad(ad), _main_loop)
        if _main_loop and not _main_loop.is_closed() else None
    ),
    on_keyword_status=_on_keyword_status,
    on_notification_update=_on_notification_update,
    perf=perf,
)


# ── FastAPI lifecycle ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_running_loop()

    await db.init_db()
    await telegram_service.start_worker()
    await discord_service.start_worker()
    asyncio.create_task(db.periodic_purge(6.0), name="db-purge")
    asyncio.create_task(scanner.telegram_retry_loop(120.0), name="telegram-retry")
    asyncio.create_task(_perf_broadcast_loop(), name="perf-broadcast")

    logger.info("🛍️ Vinted Monitor Web — prêt sur http://localhost:8080")
    yield

    if scanner.running:
        scanner.stop()
    await telegram_service.close()
    await discord_service.close()
    await autobuy_close()
    from services.vinted import close_all
    await close_all()
    await db.close()


app = FastAPI(title="Vinted Monitor", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Stats périodiques ─────────────────────────────────────────────────────────

async def _perf_broadcast_loop():
    while True:
        await asyncio.sleep(2)
        try:
            summary = perf.summary()
            stats = db.get_stats()
            scanner_stats = scanner.get_stats()
            await manager.broadcast({
                "type": "stats",
                "perf": summary,
                "db": stats,
                "scanner": scanner_stats,
            })
        except Exception as e:
            logger.debug(f"perf broadcast error: {e}")


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Envoie l'état initial au client qui vient de se connecter
        recent = await db.get_recent_listings(100)
        await ws.send_text(json.dumps({"type": "init", "ads": recent, "config": _safe_config()}))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                await _handle_ws_msg(msg, ws)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        logger.debug(f"[WS] Erreur: {e}")
        manager.disconnect(ws)


async def _handle_ws_msg(msg: dict, ws: WebSocket):
    cmd = msg.get("cmd")

    if cmd == "scanner_start":
        if not scanner.running:
            app_config.save_config(config_data)
            scanner.start(_main_loop)
            await ws.send_text(json.dumps({"type": "scanner_state", "running": True}))

    elif cmd == "scanner_stop":
        if scanner.running:
            scanner.stop()
            await ws.send_text(json.dumps({"type": "scanner_state", "running": False}))

    elif cmd == "scanner_once":
        scanner.run_once()

    elif cmd == "get_state":
        await ws.send_text(json.dumps({
            "type": "state",
            "running": scanner.running,
            "config": _safe_config(),
            "stats": scanner.get_stats(),
        }))


def _safe_config() -> dict:
    """Config sans secrets."""
    import copy
    cfg = copy.deepcopy(config_data)
    cfg.pop("_secrets", None)
    has_token = bool(app_config.get_secret("VINTED_TOKEN"))
    has_tg = bool(app_config.get_secret("TELEGRAM_BOT_TOKEN"))
    has_dc = bool(app_config.get_secret("DISCORD_WEBHOOK_URL"))
    cfg["_has_token"] = has_token
    cfg["_has_telegram"] = has_tg
    cfg["_has_discord"] = has_dc
    return cfg


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    return JSONResponse(_safe_config())


@app.post("/api/config")
async def update_config(body: dict):
    global config_data
    # Sépare les secrets des champs normaux
    tg_token = body.pop("telegram_bot_token", None)
    tg_chat = body.pop("telegram_chat_id", None)
    dc_webhook = body.pop("discord_webhook_url", None)

    if tg_token:
        app_config.set_secret("TELEGRAM_BOT_TOKEN", tg_token)
    if tg_chat:
        app_config.set_secret("TELEGRAM_CHAT_ID", tg_chat)
    if dc_webhook:
        app_config.set_secret("DISCORD_WEBHOOK_URL", dc_webhook)

    app_config._deep_merge(config_data, body)
    app_config.save_config(config_data)

    if scanner.running:
        scanner.sync_keywords()

    await manager.broadcast({"type": "config_updated", "config": _safe_config()})
    return JSONResponse({"ok": True})


@app.post("/api/token")
async def set_token(body: dict):
    token = body.get("token", "").strip()
    if not token:
        raise HTTPException(400, "Token vide")
    app_config.set_secret("VINTED_TOKEN", token)
    return JSONResponse({"ok": True, "masked": "****" + token[-4:] if len(token) > 4 else "****"})


@app.get("/api/stats")
async def get_stats():
    return JSONResponse({
        "scanner": scanner.get_stats(),
        "perf": perf.summary(),
        "db": db.get_stats(),
        "running": scanner.running,
    })


@app.get("/api/listings")
async def get_listings(limit: int = 200):
    rows = await db.get_recent_listings(limit)
    return JSONResponse({"listings": rows})


@app.post("/api/scanner/start")
async def start_scanner():
    if not scanner.running:
        app_config.save_config(config_data)
        scanner.start(_main_loop)
    await manager.broadcast({"type": "scanner_state", "running": True})
    return JSONResponse({"running": True})


@app.post("/api/scanner/stop")
async def stop_scanner():
    if scanner.running:
        scanner.stop()
    await manager.broadcast({"type": "scanner_state", "running": False})
    return JSONResponse({"running": False})


@app.post("/api/scanner/once")
async def scan_once():
    scanner.run_once()
    return JSONResponse({"ok": True})


@app.post("/api/autobuy/test")
async def test_autobuy(body: dict):
    token = app_config.get_secret("VINTED_TOKEN") or body.get("token", "")
    item_url = body.get("url", "")
    if not token or not item_url:
        raise HTTPException(400, "Token et URL requis")

    import re
    m = re.search(r"/items/(\d+)", item_url)
    if not m:
        raise HTTPException(400, "URL Vinted invalide")

    item_id = m.group(1)
    market_key = "fr"
    if "vinted.pl" in item_url:
        market_key = "pl"
    elif "vinted.co.uk" in item_url:
        market_key = "uk"

    from services.vinted import MARKETS
    market = MARKETS[market_key]

    from services.autobuy import check_available
    can_buy, item_data = await check_available(market["base_url"], item_id, token, market["lang"])
    return JSONResponse({
        "can_buy": can_buy,
        "item_id": item_id,
        "title": item_data.get("title", ""),
        "price": item_data.get("price", ""),
        "status": item_data.get("status", ""),
        "error": item_data.get("error", ""),
    })


# ── Frontend SPA ──────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    index = WEB_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text("utf-8"))
    return HTMLResponse("<h1>web/index.html manquant</h1>", status_code=500)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "web_app:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="warning",
    )
