"""
Autobuy — achat automatique ultra-rapide sur Vinted.

Flow fast (défaut) :
  POST /api/v2/items/{id}/buy → résultat en ~150ms
  409 → article parti · 401 → token expiré · 422 → réessai sans shipping

Flow conservateur (fast=False) :
  1. GET /api/v2/items/{id}  → vérification can_buy + prix
  2. POST /api/v2/items/{id}/buy (avec retry 422 shipping)

Sécurité :
  - Token jamais loggué en entier (masqué à 4 chars)
  - Aucune donnée bancaire stockée ou transmise
  - Token uniquement en config locale / .env
"""
import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

TIMEOUT_BUY   = aiohttp.ClientTimeout(total=12, connect=3, sock_read=8)
TIMEOUT_CHECK = aiohttp.ClientTimeout(total=15, connect=4, sock_read=10)

# ── Sessions ──────────────────────────────────────────────────────────────────
# FIX: une seule session partagée "fast" et "buy" (l'ancien code créait
# une session par item_id → fuite de file descriptors + connexions)
_buy_sessions: dict[str, aiohttp.ClientSession] = {}

# Semaphore (init lazy) — limite le nombre d'achats simultanés
_BUY_CONCURRENCY = 4
_buy_sem: Optional[asyncio.Semaphore] = None

# Dédup intra-process : empêche d'acheter le même item deux fois si les
# tâches mot-clé se chevauchent ou si le même item arrive sur plusieurs marchés
_in_flight: set[str] = set()

# Stats globales (depuis le démarrage)
_stats: dict[str, int] = {
    "attempts": 0, "success": 0, "already_sold": 0,
    "token_invalid": 0, "timeout": 0, "price_exceeded": 0,
    "duplicate_skipped": 0, "validation_error": 0, "other_error": 0,
}

_RETRY_DELAY = 0.05  # secondes entre deux tentatives (timeout réseau)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mask(token: str) -> str:
    if not token or len(token) <= 4:
        return "****"
    return "****" + token[-4:]


def _get_sem() -> asyncio.Semaphore:
    global _buy_sem
    if _buy_sem is None:
        _buy_sem = asyncio.Semaphore(_BUY_CONCURRENCY)
    return _buy_sem


def _get_buy_session(market_key: str, timeout: Optional[aiohttp.ClientTimeout] = None) -> aiohttp.ClientSession:
    if market_key not in _buy_sessions or _buy_sessions[market_key].closed:
        connector = aiohttp.TCPConnector(limit=10, force_close=False, enable_cleanup_closed=True)
        _buy_sessions[market_key] = aiohttp.ClientSession(
            connector=connector, timeout=timeout or TIMEOUT_BUY
        )
    return _buy_sessions[market_key]


def _auth_headers(base_url: str, lang: str, token: str, csrf: Optional[str] = None) -> dict:
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": lang,
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": base_url,
        "Referer": f"{base_url}/catalog",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if csrf:
        h["X-CSRF-Token"] = csrf
    return h


def _parse_order_id(data: dict) -> str:
    """Extrait l'order_id quel que soit le format de réponse API."""
    return str(
        data.get("order", {}).get("id")
        or data.get("transaction", {}).get("id")
        or data.get("id")
        or ""
    )


async def _json_safe(resp: aiohttp.ClientResponse) -> dict:
    try:
        return await resp.json(content_type=None)
    except Exception:
        return {}


async def _post_buy(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    body: dict,
) -> tuple[int, dict]:
    """POST helper — retourne (status, response_data). Peut lever TimeoutError."""
    async with session.post(url, headers=headers, json=body) as r:
        return r.status, await _json_safe(r)


def _extract_shipping_option(item_data: dict) -> Optional[str]:
    """Récupère l'id de la première option d'expédition disponible."""
    opts = item_data.get("service_fee") or item_data.get("shipping_options") or []
    if isinstance(opts, list) and opts:
        first = opts[0]
        if isinstance(first, dict):
            return first.get("id")
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Statistiques des tentatives d'achat depuis le démarrage."""
    return dict(_stats)


async def prewarm(market_keys: Optional[list[str]] = None) -> None:
    """Pré-initialise les sessions HTTP (élimine la pénalité de cold-start)."""
    for key in (market_keys or ["fast", "buy", "check"]):
        _get_buy_session(key)
    logger.debug("[Autobuy] Sessions pré-chauffées")


async def check_available(
    base_url: str,
    item_id: str,
    token: str,
    lang: str = "fr-FR,fr;q=0.9",
) -> tuple[bool, dict]:
    """Vérifie qu'un item est encore disponible à l'achat. Retourne (can_buy, item_data)."""
    session = _get_buy_session("check")
    headers = _auth_headers(base_url, lang, token)
    try:
        async with session.get(f"{base_url}/api/v2/items/{item_id}", headers=headers) as r:
            if r.status == 401:
                logger.warning(f"[Autobuy] Token invalide ({_mask(token)}) → 401")
                return False, {"error": "token_invalid"}
            if r.status != 200:
                return False, {"error": f"http_{r.status}"}
            data = await _json_safe(r)
            item = data.get("item", {})
            can_buy = bool(item.get("can_buy", False))
            return can_buy, item
    except asyncio.TimeoutError:
        return False, {"error": "timeout"}
    except Exception as e:
        logger.warning(f"[Autobuy] check_available error: {e}")
        return False, {"error": str(e)}


async def attempt_buy(
    base_url: str,
    item_id: str,
    token: str,
    item_data: dict,
    lang: str = "fr-FR,fr;q=0.9",
) -> dict:
    """
    Flow conservateur : POST /buy avec l'option d'expédition si disponible.
    Si 422 avec shipping option → retry sans shipping (cas fréquent Vinted).
    Si code non géré → fallback POST /transactions.
    """
    session = _get_buy_session("buy")
    headers = _auth_headers(base_url, lang, token)
    shipping_option_id = _extract_shipping_option(item_data)

    body: dict = {"item_id": int(item_id)}
    if shipping_option_id:
        body["shipping_option_id"] = shipping_option_id

    buy_url = f"{base_url}/api/v2/items/{item_id}/buy"

    try:
        status, data = await _post_buy(session, buy_url, headers, body)

        if status in (200, 201):
            order_id = _parse_order_id(data)
            logger.info(f"[Autobuy] ✅ Achat réussi — item {item_id}, order {order_id}")
            return {"success": True, "status": "purchased", "order_id": order_id, "error": ""}

        if status == 401:
            return {"success": False, "status": "token_invalid", "order_id": "", "error": "Token expiré ou invalide"}

        if status == 409:
            return {"success": False, "status": "already_sold", "order_id": "", "error": "Article déjà vendu"}

        if status == 422:
            err_msg = data.get("error", {}).get("message") or data.get("message") or str(status)
            if shipping_option_id:
                # 422 souvent causé par une option d'expédition invalide → retry sans
                body2: dict = {"item_id": int(item_id)}
                status2, data2 = await _post_buy(session, buy_url, headers, body2)
                if status2 in (200, 201):
                    order_id = _parse_order_id(data2)
                    logger.info(f"[Autobuy] ✅ Achat réussi (sans shipping) — item {item_id}")
                    return {"success": True, "status": "purchased", "order_id": order_id, "error": ""}
                err_msg = data2.get("error", {}).get("message") or data2.get("message") or str(status2)
            return {"success": False, "status": "validation_error", "order_id": "", "error": err_msg}

        # Fallback : endpoint transactions
        buyer_id = item_data.get("user", {}).get("id") if item_data else None
        body_tx: dict = {"item_id": int(item_id)}
        if buyer_id:
            body_tx["buyer_id"] = buyer_id
        status_tx, data_tx = await _post_buy(session, f"{base_url}/api/v2/transactions", headers, body_tx)
        if status_tx in (200, 201):
            order_id = _parse_order_id(data_tx)
            logger.info(f"[Autobuy] ✅ Transaction créée — item {item_id}, tx {order_id}")
            return {"success": True, "status": "purchased", "order_id": order_id, "error": ""}

        err = data_tx.get("error", {}).get("message") or str(status_tx)
        return {"success": False, "status": "api_error", "order_id": "", "error": err}

    except asyncio.TimeoutError:
        return {"success": False, "status": "timeout", "order_id": "", "error": "Timeout lors de l'achat"}
    except Exception as e:
        logger.error(f"[Autobuy] attempt_buy exception: {e}")
        return {"success": False, "status": "exception", "order_id": "", "error": str(e)}


async def fast_buy(
    base_url: str,
    item_id: str,
    token: str,
    lang: str = "fr-FR,fr;q=0.9",
) -> dict:
    """
    Achat optimiste ultra-rapide — pas de GET pré-vérification.

    - Session partagée "fast" (FIX: l'ancien code créait une session par item)
    - 1 retry automatique sur timeout réseau transitoire
    - Fallback /transactions si /buy renvoie un code non géré
    """
    session = _get_buy_session("fast")  # session partagée, pas par item
    headers = _auth_headers(base_url, lang, token)
    body: dict = {"item_id": int(item_id)}
    buy_url = f"{base_url}/api/v2/items/{item_id}/buy"

    for attempt in range(2):
        try:
            status, data = await _post_buy(session, buy_url, headers, body)

            if status in (200, 201):
                order_id = _parse_order_id(data)
                logger.info(f"[Autobuy] ✅ Fast buy réussi — item {item_id}, order {order_id}")
                return {"success": True, "status": "purchased", "order_id": order_id, "error": ""}

            if status == 401:
                return {"success": False, "status": "token_invalid", "order_id": "", "error": "Token expiré ou invalide"}

            if status == 409:
                return {"success": False, "status": "already_sold", "order_id": "", "error": "Article déjà vendu"}

            if status == 422:
                err_msg = data.get("error", {}).get("message") or data.get("message") or str(status)
                return {"success": False, "status": "validation_error", "order_id": "", "error": err_msg}

            # Code non géré → fallback /transactions
            status_tx, data_tx = await _post_buy(
                session, f"{base_url}/api/v2/transactions", headers, {"item_id": int(item_id)}
            )
            if status_tx in (200, 201):
                order_id = _parse_order_id(data_tx)
                logger.info(f"[Autobuy] ✅ Fast tx créée — item {item_id}, tx {order_id}")
                return {"success": True, "status": "purchased", "order_id": order_id, "error": ""}

            err = data_tx.get("error", {}).get("message") or str(status_tx)
            return {"success": False, "status": "api_error", "order_id": "", "error": err}

        except asyncio.TimeoutError:
            if attempt == 0:
                logger.warning(f"[Autobuy] fast_buy timeout item {item_id} — retry")
                await asyncio.sleep(_RETRY_DELAY)
                continue
            return {"success": False, "status": "timeout", "order_id": "", "error": "Timeout (après retry)"}
        except Exception as e:
            logger.error(f"[Autobuy] fast_buy exception: {e}")
            return {"success": False, "status": "exception", "order_id": "", "error": str(e)}

    return {"success": False, "status": "timeout", "order_id": "", "error": "Timeout (après retry)"}


async def run_autobuy(
    ad: dict,
    token: str,
    max_price: Optional[float],
    markets_info: dict,
    fast: bool = True,
) -> dict:
    """
    Point d'entrée principal de l'autobuy.

    Garanties :
    - Dédup : un même item (raw_id) ne peut pas être acheté deux fois simultanément
    - Semaphore : au plus _BUY_CONCURRENCY achats en même temps
    - Prix : rejeté si price_num > max_price (comparaison stricte, None ignoré)
    - Stats : chaque tentative et son résultat sont comptabilisés
    """
    t0 = time.monotonic()
    item_id  = ad.get("raw_id", "")
    market_key = ad.get("market_key", "fr")
    price_num  = ad.get("price_num")
    title  = ad.get("title", "")[:60]
    url    = ad.get("url", "")

    from services.vinted import MARKETS
    market_info = MARKETS.get(market_key, MARKETS["fr"])
    base_url = market_info["base_url"]
    lang     = market_info["lang"]

    base_result = {
        "item_id": item_id, "title": title, "url": url,
        "price": ad.get("price", ""), "market": market_key, "elapsed_ms": 0,
    }

    if not token:
        return {**base_result, "success": False, "status": "no_token",
                "error": "Token Vinted non configuré", "order_id": ""}

    if max_price is not None and price_num is not None and price_num > max_price:
        _stats["price_exceeded"] += 1
        return {**base_result, "success": False, "status": "price_exceeded",
                "error": f"{price_num}€ > max {max_price}€", "order_id": ""}

    # Dédup : si cet item est déjà en cours d'achat, abandonner silencieusement
    if item_id and item_id in _in_flight:
        _stats["duplicate_skipped"] += 1
        logger.debug(f"[Autobuy] Item {item_id} déjà en vol — ignoré")
        return {**base_result, "success": False, "status": "duplicate_skipped",
                "error": "Déjà en cours d'achat", "order_id": ""}

    _stats["attempts"] += 1
    if item_id:
        _in_flight.add(item_id)

    try:
        async with _get_sem():
            if fast:
                result = await fast_buy(base_url, item_id, token, lang)
            else:
                can_buy, item_data = await check_available(base_url, item_id, token, lang)
                if not can_buy:
                    err = item_data.get("error", "indisponible")
                    elapsed = int((time.monotonic() - t0) * 1000)
                    return {**base_result, "success": False, "status": "unavailable",
                            "error": err, "elapsed_ms": elapsed, "order_id": ""}
                result = await attempt_buy(base_url, item_id, token, item_data, lang)
    finally:
        _in_flight.discard(item_id)

    # Mise à jour des stats
    status = result.get("status", "")
    if result.get("success"):
        _stats["success"] += 1
    elif status == "already_sold":
        _stats["already_sold"] += 1
    elif status == "token_invalid":
        _stats["token_invalid"] += 1
    elif status == "timeout":
        _stats["timeout"] += 1
    elif status == "validation_error":
        _stats["validation_error"] += 1
    else:
        _stats["other_error"] += 1

    elapsed = int((time.monotonic() - t0) * 1000)
    return {**base_result, **result, "elapsed_ms": elapsed}


async def close_all() -> None:
    global _buy_sem
    _in_flight.clear()
    _buy_sem = None
    for s in _buy_sessions.values():
        if not s.closed:
            await s.close()
    _buy_sessions.clear()
