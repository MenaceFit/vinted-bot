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
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware

import config as app_config
from core.performance import PerformanceMonitor
from core.scanner import Scanner
from database import database as db
from services import discord as discord_service
from services import telegram as telegram_service
from services.autobuy import run_autobuy, prewarm as autobuy_prewarm, close_all as autobuy_close
from utils.logger import setup_logging

setup_logging(logging.INFO)
logger = logging.getLogger(__name__)

config_data: dict = app_config.load_config()
perf = PerformanceMonitor()
WEB_DIR = Path(__file__).parent / "web"

# La loop FastAPI, initialisée dans lifespan(). Toutes les coroutines scanner
# tournent dans cette même loop, donc on utilise loop.create_task() plutôt
# que run_coroutine_threadsafe() (qui est l'API thread→loop, pas loop→loop).
_main_loop: Optional[asyncio.AbstractEventLoop] = None


# ── WebSocket broadcast ───────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)
        logger.info(f"[WS] +1 client ({len(self._connections)} connectés)")

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard if hasattr(self._connections, "discard") else None
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict) -> None:
        if not self._connections:
            return
        msg = json.dumps(data, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in self._connections[:]:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def schedule(self, data: dict) -> None:
        """Planifie un broadcast depuis un callback synchrone appelé dans _main_loop.

        Les callbacks du Scanner (on_log, on_keyword_status, …) sont des fonctions
        synchrones appelées depuis des coroutines asyncio. Elles s'exécutent dans
        le thread de _main_loop, donc loop.create_task() est l'API correcte ici —
        contrairement à run_coroutine_threadsafe() qui sert au cas thread→loop.
        """
        if _main_loop and not _main_loop.is_closed():
            _main_loop.create_task(self.broadcast(data))


manager = ConnectionManager()


# ── Callbacks du Scanner ──────────────────────────────────────────────────────
# Ces fonctions sont appelées par le Scanner depuis des coroutines asyncio
# tournant dans _main_loop. Elles restent synchrones car c'est le contrat
# de l'interface Scanner (on_log: Callable[[str], None]).

def _on_log(msg: str) -> None:
    manager.schedule({"type": "log", "msg": msg, "ts": datetime.now().strftime("%H:%M:%S")})


def _on_keyword_status(keyword: str, status: dict) -> None:
    manager.schedule({"type": "keyword_status", "keyword": keyword, "status": status})


def _on_notification_update(ad_id: str, channel: str, ok: bool) -> None:
    manager.schedule({"type": "notification_update", "ad_id": ad_id, "channel": channel, "ok": ok})


async def _on_new_ad(ad: dict) -> None:
    """Broadcast de la nouvelle annonce, puis tentative d'autobuy si activé."""
    await manager.broadcast({"type": "new_ad", "ad": ad})

    ab_cfg = config_data.get("autobuy", {})
    if not ab_cfg.get("enabled", False):
        return
    token = app_config.get_secret("VINTED_TOKEN")
    if not token:
        return

    # Respect des réglages per-keyword (autobuy_enabled, autobuy_max_price)
    kw_cfg = ad.get("_kw_cfg") or {}
    if kw_cfg.get("autobuy_enabled") is False:
        return
    kw_max_price = kw_cfg.get("autobuy_max_price")
    effective_max_price = kw_max_price if kw_max_price is not None else ab_cfg.get("max_price")

    result = await run_autobuy(
        ad=ad,
        token=token,
        max_price=effective_max_price,
        markets_info=config_data.get("markets", {}),
        fast=True,
    )
    await manager.broadcast({"type": "autobuy_result", "result": result})
    icon = "✅" if result.get("success") else "❌"
    logger.info(f"[Autobuy] {icon} {result.get('title','')[:40]} — {result.get('status')} ({result.get('elapsed_ms')}ms)")


# Le Scanner est construit au démarrage du module. on_new_ad est async, donc
# on la schedule via create_task depuis le callback synchrone.
scanner = Scanner(
    config_provider=lambda: config_data,
    on_log=_on_log,
    on_new_ad=lambda ad: _main_loop.create_task(_on_new_ad(ad)) if _main_loop else None,
    on_keyword_status=_on_keyword_status,
    on_notification_update=_on_notification_update,
    perf=perf,
)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_running_loop()

    await db.init_db()
    await telegram_service.start_worker()
    await discord_service.start_worker()
    asyncio.create_task(db.periodic_purge(6.0), name="db-purge")
    asyncio.create_task(scanner.telegram_retry_loop(120.0), name="telegram-retry")
    asyncio.create_task(_stats_broadcast_loop(), name="stats-broadcast")
    asyncio.create_task(autobuy_prewarm(), name="autobuy-prewarm")

    logger.info("🛍️ Vinted Monitor Web — http://localhost:8080")
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


# ── Stats broadcast périodique ────────────────────────────────────────────────

async def _stats_broadcast_loop() -> None:
    while True:
        await asyncio.sleep(2)
        try:
            await manager.broadcast({
                "type": "stats",
                "perf": perf.summary(),
                "db": db.get_stats(),
                "scanner": scanner.get_stats(),
            })
        except Exception as exc:
            logger.debug(f"stats broadcast: {exc}")


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        recent = await db.get_recent_listings(100)
        await ws.send_text(json.dumps({"type": "init", "ads": recent, "config": _safe_config()}))
        while True:
            raw = await ws.receive_text()
            try:
                await _handle_ws_msg(json.loads(raw), ws)
            except (json.JSONDecodeError, KeyError):
                pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug(f"[WS] {exc}")
    finally:
        manager.disconnect(ws)


async def _handle_ws_msg(msg: dict, ws: WebSocket) -> None:
    cmd = msg.get("cmd")
    if cmd == "scanner_start":
        if not scanner.running:
            app_config.save_config(config_data)
            scanner.start(_main_loop)
        await ws.send_text(json.dumps({"type": "scanner_state", "running": scanner.running}))
    elif cmd == "scanner_stop":
        if scanner.running:
            scanner.stop()
        await ws.send_text(json.dumps({"type": "scanner_state", "running": scanner.running}))
    elif cmd == "scanner_once":
        scanner.run_once()
    elif cmd == "get_state":
        await ws.send_text(json.dumps({
            "type": "state",
            "running": scanner.running,
            "config": _safe_config(),
            "stats": scanner.get_stats(),
        }))


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config() -> JSONResponse:
    return JSONResponse(_safe_config())


@app.post("/api/config")
async def update_config(body: dict) -> JSONResponse:
    # Strip computed/private flags — they must never be persisted to config.json
    for _k in ("_has_token", "_has_telegram", "_has_discord"):
        body.pop(_k, None)
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
async def set_token(body: dict) -> JSONResponse:
    token = body.get("token", "").strip()
    if not token:
        raise HTTPException(400, "Token vide")
    app_config.set_secret("VINTED_TOKEN", token)
    masked = ("****" + token[-4:]) if len(token) > 4 else "****"
    return JSONResponse({"ok": True, "masked": masked})


@app.get("/api/stats")
async def get_stats() -> JSONResponse:
    return JSONResponse({
        "scanner": scanner.get_stats(),
        "perf": perf.summary(),
        "db": db.get_stats(),
        "running": scanner.running,
    })


@app.get("/api/listings")
async def get_listings(limit: int = 200) -> JSONResponse:
    return JSONResponse({"listings": await db.get_recent_listings(limit)})


@app.post("/api/scanner/start")
async def start_scanner() -> JSONResponse:
    if not scanner.running:
        app_config.save_config(config_data)
        scanner.start(_main_loop)
    await manager.broadcast({"type": "scanner_state", "running": True})
    return JSONResponse({"running": True})


@app.post("/api/scanner/stop")
async def stop_scanner() -> JSONResponse:
    if scanner.running:
        scanner.stop()
    await manager.broadcast({"type": "scanner_state", "running": False})
    return JSONResponse({"running": False})


@app.post("/api/scanner/once")
async def scan_once() -> JSONResponse:
    scanner.run_once()
    return JSONResponse({"ok": True})


@app.post("/api/autobuy/check")
async def autobuy_check(body: dict) -> JSONResponse:
    """Vérifie la disponibilité d'un article (sans acheter)."""
    token = app_config.get_secret("VINTED_TOKEN") or body.get("token", "")
    item_url = body.get("url", "")
    if not item_url:
        raise HTTPException(400, "URL requise")

    import re
    m = re.search(r"/items/(\d+)", item_url)
    if not m:
        raise HTTPException(400, "URL Vinted invalide (doit contenir /items/{id})")

    item_id = m.group(1)
    from services.vinted import MARKETS
    market_key = "pl" if "vinted.pl" in item_url else "uk" if "vinted.co.uk" in item_url else "fr"
    market = MARKETS[market_key]

    from services.autobuy import check_available
    can_buy, item_data = await check_available(market["base_url"], item_id, token, market["lang"])
    return JSONResponse({
        "can_buy": can_buy,
        "item_id": item_id,
        "title": item_data.get("title", ""),
        "price": item_data.get("price", ""),
        "error": item_data.get("error", ""),
    })


@app.post("/api/autobuy/buy")
async def autobuy_buy(body: dict) -> JSONResponse:
    """Tente d'acheter un article (check + buy)."""
    token = app_config.get_secret("VINTED_TOKEN") or body.get("token", "")
    if not token:
        raise HTTPException(400, "Token Vinted requis")
    item_url = body.get("url", "")
    if not item_url:
        raise HTTPException(400, "URL requise")

    import re
    m = re.search(r"/items/(\d+)", item_url)
    if not m:
        raise HTTPException(400, "URL Vinted invalide")

    import re as _re
    market_key = "pl" if "vinted.pl" in item_url else "uk" if "vinted.co.uk" in item_url else "fr"
    ad = {"raw_id": m.group(1), "market_key": market_key, "price_num": None, "title": "", "price": "", "url": item_url}

    result = await run_autobuy(ad=ad, token=token, max_price=None, markets_info=config_data.get("markets", {}))
    await manager.broadcast({"type": "autobuy_result", "result": result})
    return JSONResponse(result)


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index() -> HTMLResponse:
    index = WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>web/index.html introuvable</h1>", status_code=500)
    return HTMLResponse(index.read_text("utf-8"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_config() -> dict:
    import copy
    cfg = copy.deepcopy(config_data)
    cfg["_has_token"] = bool(app_config.get_secret("VINTED_TOKEN"))
    cfg["_has_telegram"] = bool(app_config.get_secret("TELEGRAM_BOT_TOKEN"))
    cfg["_has_discord"] = bool(app_config.get_secret("DISCORD_WEBHOOK_URL"))
    return cfg


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("web_app:app", host="0.0.0.0", port=8080, reload=False, log_level="warning")
