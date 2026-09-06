"""
Service Telegram — async aiohttp, queue dédiée, retry, fallback image→texte.

Miroir du pattern services/discord.py (déjà éprouvé) :
  - Une seule ClientSession réutilisée (keep-alive)
  - Queue asyncio : les envois ne bloquent jamais le scanner
  - Throttling ~1 msg/s/chat (limite Bot API Telegram)
  - Retry sur 429 (respecte `retry_after`) et sur erreurs réseau transitoires
  - Une erreur d'image ne doit jamais empêcher l'envoi du texte (fallback automatique)

Sécurité : le bot_token ne doit JAMAIS apparaître dans un message de log.
"""
import asyncio
import logging
import time
from typing import Callable, Optional

import aiohttp

from utils.formatting import TEST_MESSAGE, telegram_caption

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
MIN_INTERVAL = 1.05  # secondes entre deux envois au même chat
MAX_RETRY = 3

_session: Optional[aiohttp.ClientSession] = None
_queue: "asyncio.Queue" = asyncio.Queue()
_worker_task: Optional[asyncio.Task] = None
_last_send_ts = 0.0
_last_ok: Optional[bool] = None  # None = jamais essayé, sinon résultat du dernier appel API


def is_connected() -> Optional[bool]:
    """Reflète le résultat du dernier appel API réel (pas juste 'activé dans la config')."""
    return _last_ok


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=5, limit_per_host=5)
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        _session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _session


async def start_worker() -> None:
    global _worker_task
    _worker_task = asyncio.create_task(_queue_worker(), name="telegram-worker")


async def _queue_worker() -> None:
    global _last_send_ts
    while True:
        item = await _queue.get()
        if item is None:
            break
        bot_token, chat_id, ad, send_image, on_result = item
        try:
            wait = MIN_INTERVAL - (time.monotonic() - _last_send_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            ok, error = await send_ad(bot_token, chat_id, ad, send_image)
            _last_send_ts = time.monotonic()
            if on_result:
                on_result(ok, error)
        except Exception as e:
            logger.error(f"[telegram] Worker error: {e}")
            if on_result:
                on_result(False, str(e))
        finally:
            _queue.task_done()


async def send_ad_nowait(
    bot_token: str,
    chat_id: str,
    ad: dict,
    send_image: bool = True,
    on_result: Optional[Callable[[bool, str], None]] = None,
) -> None:
    """Enqueue une notification Telegram. Retourne immédiatement, ne bloque jamais le scan.
    `on_result(ok, error)` : `error` est la description Telegram de l'échec, vide si `ok`."""
    if not bot_token or not chat_id:
        if on_result:
            on_result(False, "Bot Token ou Chat ID manquant")
        return
    await _queue.put((bot_token, chat_id, ad, send_image, on_result))


# ── Envoi effectif ────────────────────────────────────────────────────────────

async def send_ad(bot_token: str, chat_id: str, ad: dict, send_image: bool = True) -> tuple[bool, str]:
    """Envoie une annonce (avec photo si possible). Retourne (ok, raison_si_échec)."""
    caption = telegram_caption(ad)
    image_url = ad.get("image", "") if send_image else ""

    if image_url and image_url.startswith("http"):
        ok, data = await _call(
            bot_token, "sendPhoto",
            {"chat_id": chat_id, "photo": image_url, "caption": caption, "parse_mode": "HTML"},
        )
        if ok:
            return True, ""
        logger.warning("[telegram] Envoi photo échoué → repli texte seul")

    ok, data = await _call(
        bot_token, "sendMessage",
        {"chat_id": chat_id, "text": caption, "parse_mode": "HTML"},
    )
    return ok, ("" if ok else data.get("description", "erreur inconnue"))


async def test_connection(bot_token: str, chat_id: str) -> tuple[bool, str]:
    """Valide le token puis envoie le message de test. Message d'erreur explicite."""
    if not bot_token or not chat_id:
        return False, "Bot Token et Chat ID sont requis."

    ok, data = await _call(bot_token, "getMe", {})
    if not ok:
        desc = data.get("description", "token invalide ou réseau indisponible")
        return False, f"Échec de connexion — {desc}"

    username = (data.get("result") or {}).get("username", "?")

    ok, data = await _call(bot_token, "sendMessage", {"chat_id": chat_id, "text": TEST_MESSAGE})
    if not ok:
        desc = data.get("description", "chat_id invalide")
        return False, f"Bot @{username} valide, mais échec d'envoi — {desc} (as-tu démarré une conversation avec le bot ?)"

    return True, f"Telegram opérationnel (bot @{username})"


async def _call(bot_token: str, method: str, payload: dict) -> tuple[bool, dict]:
    """Appelle l'API Telegram avec retry sur 429/erreurs réseau. Ne logue jamais le token."""
    global _last_ok
    url = f"{API_ROOT}/bot{bot_token}/{method}"
    session = _get_session()

    for attempt in range(MAX_RETRY):
        try:
            async with session.post(url, json=payload) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"description": f"réponse invalide (HTTP {resp.status})"}

                if resp.status == 200 and data.get("ok"):
                    _last_ok = True
                    return True, data

                if resp.status == 429:
                    retry_after = float((data.get("parameters") or {}).get("retry_after", 2))
                    logger.warning(f"[telegram] {method}: rate-limit → attente {retry_after:.1f}s")
                    await asyncio.sleep(retry_after + 0.05)
                    continue

                logger.warning(f"[telegram] {method}: HTTP {resp.status} — {data.get('description', '?')}")
                _last_ok = False
                return False, data

        except asyncio.TimeoutError:
            logger.warning(f"[telegram] {method}: timeout tentative {attempt + 1}/{MAX_RETRY}")
            await asyncio.sleep(1.0 * (attempt + 1))
        except aiohttp.ClientError as e:
            logger.warning(f"[telegram] {method}: erreur réseau tentative {attempt + 1}: {e}")
            await asyncio.sleep(1.0 * (attempt + 1))

    _last_ok = False
    return False, {"description": "échec réseau après plusieurs tentatives"}


async def close() -> None:
    if _worker_task:
        await _queue.put(None)
        await _queue.join()
        _worker_task.cancel()
    if _session and not _session.closed:
        await _session.close()
