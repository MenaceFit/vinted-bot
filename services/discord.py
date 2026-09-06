"""
Webhook Discord — async aiohttp + retry rate-limit + queue dédiée.

  - Totalement async (aiohttp) — aucun thread bloquant
  - Une seule ClientSession réutilisée (keep-alive, connection pooling)
  - Queue asyncio pour sérialiser les envois et respecter les rate-limits Discord
  - Retry automatique sur 429 avec Retry-After
"""
import asyncio
import logging
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

VINTED_COLOR  = 0x09B1BA
MAX_RETRY_429 = 4
_session: Optional[aiohttp.ClientSession] = None
_queue: asyncio.Queue = asyncio.Queue()
_worker_task: Optional[asyncio.Task] = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
        timeout   = aiohttp.ClientTimeout(total=10, connect=5)
        _session  = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _session


async def start_worker():
    """Lance le worker de queue en arrière-plan (appeler une fois au démarrage)."""
    global _worker_task
    _worker_task = asyncio.create_task(_queue_worker(), name="discord-worker")


async def _queue_worker():
    while True:
        item = await _queue.get()
        if item is None:
            break
        webhook_url, ad, on_result = item
        try:
            ok = await _send_now(webhook_url, ad)
            if on_result:
                on_result(ok)
        except Exception as e:
            logger.error(f"[discord] Worker error: {e}")
            if on_result:
                on_result(False)
        finally:
            _queue.task_done()


async def send_ad_nowait(
    webhook_url: str,
    ad: dict,
    on_result: Optional[Callable[[bool], None]] = None,
) -> None:
    """Enqueue une notification Discord — fire and forget, ne bloque jamais le scan."""
    if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
        if on_result:
            on_result(False)
        return
    await _queue.put((webhook_url, ad, on_result))


async def _send_now(webhook_url: str, ad: dict) -> bool:
    payload = _build_payload(ad)
    session = _get_session()

    for attempt in range(MAX_RETRY_429):
        try:
            async with session.post(webhook_url, json=payload) as resp:
                if resp.status in (200, 204):
                    logger.debug(f"[discord] ✅ {ad.get('title','?')[:50]}")
                    return True

                if resp.status == 429:
                    body = await resp.json(content_type=None)
                    retry_after = float(body.get("retry_after", 1.0))
                    logger.warning(f"[discord] Rate-limit → attente {retry_after:.1f}s")
                    await asyncio.sleep(retry_after + 0.05)
                    continue

                text = await resp.text()
                logger.warning(f"[discord] HTTP {resp.status}: {text[:200]}")
                return False

        except asyncio.TimeoutError:
            logger.warning(f"[discord] Timeout tentative {attempt+1}")
        except aiohttp.ClientError as e:
            logger.warning(f"[discord] ClientError tentative {attempt+1}: {e}")

    logger.error(f"[discord] Abandon après {MAX_RETRY_429} tentatives")
    return False


def _build_payload(ad: dict) -> dict:
    market_label = ad.get("market_label", "")
    footer_text  = "🛍️ Vinted Monitor"
    if market_label:
        footer_text += f" • {market_label}"
    keyword = ad.get("keyword", "")
    if keyword:
        footer_text += f" • {keyword}"

    seller     = ad.get("seller", "")
    seller_url = ad.get("seller_url", "")
    seller_val = f"[{seller}]({seller_url})" if seller and seller_url else seller or "N/A"

    fields = [
        {"name": "💰 Prix",    "value": ad.get("price", "N/A"),    "inline": True},
        {"name": "📏 Taille",  "value": ad.get("size") or "N/A",   "inline": True},
        {"name": "✨ État",    "value": ad.get("status") or "N/A", "inline": True},
        {"name": "👤 Vendeur", "value": seller_val,                 "inline": True},
    ]
    if ad.get("date"):
        fields.append({"name": "📅 Publié", "value": ad["date"], "inline": True})
    if ad.get("url"):
        fields.append({"name": "🔗 Annonce", "value": ad["url"], "inline": False})

    embed: dict = {
        "title":  (ad.get("title") or "Annonce Vinted")[:256],
        "url":    ad.get("url", ""),
        "color":  VINTED_COLOR,
        "fields": fields,
        "footer": {"text": footer_text},
    }
    image_url = ad.get("image", "")
    if image_url and image_url.startswith("http"):
        embed["image"] = {"url": image_url}

    return {"embeds": [embed], "username": "🛍️ Vinted Monitor"}


async def close():
    """Ferme proprement le worker et la session."""
    if _worker_task:
        await _queue.put(None)
        await _queue.join()
        _worker_task.cancel()
    if _session and not _session.closed:
        await _session.close()
