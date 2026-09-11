"""
Autobuy — achat automatique ultra-rapide sur Vinted.

Flow standard (fast=True, défaut) :
  POST /api/v2/items/{id}/buy directement → résultat en ~150ms
  409 Already sold → item parti, pas de retentative

Flow conservateur (fast=False) :
  1. check_available()  → GET /api/v2/items/{id}  (verif can_buy + prix)
  2. attempt_buy()      → POST /api/v2/items/{id}/buy

Sécurité :
  - Token jamais loggué en entier (masqué à 4 chars)
  - Aucune donnée bancaire stockée ou transmise
  - Token stocké uniquement dans config locale / .env
"""
import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

TIMEOUT_BUY = aiohttp.ClientTimeout(connect=3, sock_read=8)
TIMEOUT_CHECK = aiohttp.ClientTimeout(connect=4, sock_read=10)
_buy_sessions: dict[str, aiohttp.ClientSession] = {}


def _mask(token: str) -> str:
    if not token or len(token) <= 4:
        return "****"
    return "****" + token[-4:]


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


def _get_buy_session(market_key: str, timeout: Optional[aiohttp.ClientTimeout] = None) -> aiohttp.ClientSession:
    if market_key not in _buy_sessions or _buy_sessions[market_key].closed:
        connector = aiohttp.TCPConnector(limit=10, force_close=False, enable_cleanup_closed=True)
        _buy_sessions[market_key] = aiohttp.ClientSession(
            connector=connector, timeout=timeout or TIMEOUT_BUY
        )
    return _buy_sessions[market_key]


async def prewarm(market_keys: Optional[list[str]] = None) -> None:
    """Pré-initialise les sessions HTTP pour éliminer la pénalité de cold-start."""
    for key in (market_keys or ["fr", "buy", "check"]):
        _get_buy_session(key)
    logger.debug("[Autobuy] Sessions pré-chauffées")


async def check_available(base_url: str, item_id: str, token: str, lang: str = "fr-FR,fr;q=0.9") -> tuple[bool, dict]:
    """
    Vérifie qu'un item est encore disponible à l'achat.
    Retourne (can_buy, item_data).
    """
    session = _get_buy_session("check")
    headers = _auth_headers(base_url, lang, token)
    try:
        async with session.get(f"{base_url}/api/v2/items/{item_id}", headers=headers) as r:
            if r.status == 401:
                logger.warning(f"[Autobuy] Token invalide ({_mask(token)}) → 401")
                return False, {"error": "token_invalid"}
            if r.status != 200:
                return False, {"error": f"http_{r.status}"}
            data = await r.json(content_type=None)
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
    Tente d'acheter un article via l'API Vinted.
    Retourne {"success": bool, "status": str, "error": str, "order_id": str}.
    """
    session = _get_buy_session("buy")
    headers = _auth_headers(base_url, lang, token)

    # Récupère l'option d'expédition la moins chère
    shipping_options = item_data.get("service_fee", []) or []
    if not shipping_options:
        # Fallback : tenter sans option d'expédition
        shipping_option_id = None
    else:
        # Prend la première option disponible
        shipping_option_id = shipping_options[0].get("id") if isinstance(shipping_options[0], dict) else None

    body: dict = {"item_id": int(item_id)}
    if shipping_option_id:
        body["shipping_option_id"] = shipping_option_id

    # Essai 1 : endpoint buy_it_now
    try:
        async with session.post(
            f"{base_url}/api/v2/items/{item_id}/buy",
            headers=headers,
            json=body,
        ) as r:
            resp_data = {}
            try:
                resp_data = await r.json(content_type=None)
            except Exception:
                pass

            if r.status in (200, 201):
                order_id = (
                    resp_data.get("order", {}).get("id")
                    or resp_data.get("id", "")
                )
                logger.info(f"[Autobuy] ✅ Achat réussi — item {item_id}, order {order_id}")
                return {"success": True, "status": "purchased", "order_id": str(order_id), "error": ""}

            if r.status == 401:
                return {"success": False, "status": "token_invalid", "order_id": "", "error": "Token expiré ou invalide"}

            if r.status == 409:
                return {"success": False, "status": "already_sold", "order_id": "", "error": "Article déjà vendu"}

            if r.status == 422:
                err_msg = resp_data.get("error", {}).get("message", "") or resp_data.get("message", str(r.status))
                return {"success": False, "status": "validation_error", "order_id": "", "error": err_msg}

            # Essai 2 : endpoint transactions (ancien format)
            body2 = {
                "item_id": int(item_id),
                "buyer_id": item_data.get("user", {}).get("id"),
            }
            async with session.post(
                f"{base_url}/api/v2/transactions",
                headers=headers,
                json=body2,
            ) as r2:
                resp2 = {}
                try:
                    resp2 = await r2.json(content_type=None)
                except Exception:
                    pass
                if r2.status in (200, 201):
                    order_id = resp2.get("transaction", {}).get("id", "")
                    logger.info(f"[Autobuy] ✅ Transaction créée — item {item_id}, tx {order_id}")
                    return {"success": True, "status": "purchased", "order_id": str(order_id), "error": ""}

                err = resp2.get("error", {}).get("message", "") or str(r2.status)
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
    Achat optimiste ultra-rapide — saute le GET check_available.

    Économise 200-500ms par rapport au flow standard car on va directement
    au POST buy. Le 409 (already sold) nous indique que l'article est parti.
    Retourne le même format que attempt_buy().
    """
    session = _get_buy_session(f"fast_{item_id[:3]}")
    headers = _auth_headers(base_url, lang, token)
    body: dict = {"item_id": int(item_id)}

    try:
        async with session.post(
            f"{base_url}/api/v2/items/{item_id}/buy",
            headers=headers,
            json=body,
        ) as r:
            resp_data = {}
            try:
                resp_data = await r.json(content_type=None)
            except Exception:
                pass

            if r.status in (200, 201):
                order_id = (
                    resp_data.get("order", {}).get("id")
                    or resp_data.get("id", "")
                )
                logger.info(f"[Autobuy] ✅ Fast buy réussi — item {item_id}, order {order_id}")
                return {"success": True, "status": "purchased", "order_id": str(order_id), "error": ""}

            if r.status == 401:
                return {"success": False, "status": "token_invalid", "order_id": "", "error": "Token expiré ou invalide"}

            if r.status == 409:
                return {"success": False, "status": "already_sold", "order_id": "", "error": "Article déjà vendu"}

            if r.status == 422:
                err_msg = resp_data.get("error", {}).get("message", "") or resp_data.get("message", str(r.status))
                return {"success": False, "status": "validation_error", "order_id": "", "error": err_msg}

            # Fallback : essai endpoint transactions
            body2 = {"item_id": int(item_id)}
            async with session.post(
                f"{base_url}/api/v2/transactions",
                headers=headers,
                json=body2,
            ) as r2:
                resp2 = {}
                try:
                    resp2 = await r2.json(content_type=None)
                except Exception:
                    pass
                if r2.status in (200, 201):
                    order_id = resp2.get("transaction", {}).get("id", "")
                    return {"success": True, "status": "purchased", "order_id": str(order_id), "error": ""}
                err = resp2.get("error", {}).get("message", "") or str(r2.status)
                return {"success": False, "status": "api_error", "order_id": "", "error": err}

    except asyncio.TimeoutError:
        return {"success": False, "status": "timeout", "order_id": "", "error": "Timeout lors de l'achat"}
    except Exception as e:
        logger.error(f"[Autobuy] fast_buy exception: {e}")
        return {"success": False, "status": "exception", "order_id": "", "error": str(e)}


async def run_autobuy(
    ad: dict,
    token: str,
    max_price: Optional[float],
    markets_info: dict,
    fast: bool = True,
) -> dict:
    """
    Point d'entrée principal. fast=True (défaut) : achat direct sans pré-vérification.
    fast=False : flow conservateur check_available → attempt_buy.
    Retourne le résultat complet à logger/broadcaster.
    """
    t0 = time.monotonic()
    item_id = ad.get("raw_id", "")
    market_key = ad.get("market_key", "fr")
    price_num = ad.get("price_num")
    title = ad.get("title", "")[:60]
    url = ad.get("url", "")

    from services.vinted import MARKETS
    market_info = MARKETS.get(market_key, MARKETS["fr"])
    base_url = market_info["base_url"]
    lang = market_info["lang"]

    base_result = {
        "item_id": item_id,
        "title": title,
        "url": url,
        "price": ad.get("price", ""),
        "market": market_key,
        "elapsed_ms": 0,
    }

    if not token:
        return {**base_result, "success": False, "status": "no_token", "error": "Token Vinted non configuré"}

    if max_price is not None and price_num is not None:
        if price_num > max_price:
            return {**base_result, "success": False, "status": "price_exceeded",
                    "error": f"{price_num}€ > max {max_price}€"}

    if fast:
        result = await fast_buy(base_url, item_id, token, lang)
        elapsed = int((time.monotonic() - t0) * 1000)
        return {**base_result, **result, "elapsed_ms": elapsed}

    # Flow conservateur
    can_buy, item_data = await check_available(base_url, item_id, token, lang)
    if not can_buy:
        err = item_data.get("error", "indisponible")
        elapsed = int((time.monotonic() - t0) * 1000)
        return {**base_result, "success": False, "status": "unavailable", "error": err, "elapsed_ms": elapsed}

    result = await attempt_buy(base_url, item_id, token, item_data, lang)
    elapsed = int((time.monotonic() - t0) * 1000)
    return {**base_result, **result, "elapsed_ms": elapsed}


async def close_all() -> None:
    for s in _buy_sessions.values():
        if not s.closed:
            await s.close()
    _buy_sessions.clear()
