"""
Service autobuy : vérification disponibilité, tentative d'achat, filtres prix.
Aucun appel réseau réel — session monkeypatchée.

Couverture :
- check_available       (5 tests)
- attempt_buy           (7 tests incl. 422 shipping retry)
- fast_buy              (8 tests incl. timeout retry, tx fallback, order_id formats)
- run_autobuy           (9 tests incl. dedup _in_flight, stats)
- helpers               (6 tests : _parse_order_id, _mask, _extract_shipping_option)
"""
import asyncio
import pytest
from unittest.mock import patch

from services import autobuy as autobuy_mod
from services.autobuy import (
    check_available, attempt_buy, run_autobuy, fast_buy,
    get_stats, _parse_order_id, _mask, _extract_shipping_option,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data = data
        self.headers: dict = {}

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class ErrorCtx:
    """Context manager async qui raise l'exception donnée dans __aenter__."""
    def __init__(self, exc: Exception):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *_):
        pass


class FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def _pop(self, method: str, url: str):
        self.calls.append((method, url))
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            return ErrorCtx(r)
        return r

    def get(self, url, **kw):
        return self._pop("GET", url)

    def post(self, url, **kw):
        return self._pop("POST", url)

    @property
    def closed(self) -> bool:
        return False


def _patch_session(session: FakeSession):
    """Remplace _get_buy_session pour tous les appels."""
    return patch.object(autobuy_mod, "_get_buy_session", return_value=session)


@pytest.fixture(autouse=True)
def reset_autobuy_state():
    """Remet à zéro les états globaux entre chaque test."""
    autobuy_mod._stats.update({
        "attempts": 0, "success": 0, "already_sold": 0,
        "token_invalid": 0, "timeout": 0, "price_exceeded": 0,
        "duplicate_skipped": 0, "validation_error": 0, "other_error": 0,
    })
    autobuy_mod._in_flight.clear()
    autobuy_mod._buy_sem = None
    yield
    autobuy_mod._in_flight.clear()
    autobuy_mod._buy_sem = None


# ── Helper unit tests — synchrones ────────────────────────────────────────────

def test_parse_order_id_from_order():
    assert _parse_order_id({"order": {"id": "ORD-100"}}) == "ORD-100"


def test_parse_order_id_from_transaction():
    assert _parse_order_id({"transaction": {"id": "TX-777"}}) == "TX-777"


def test_parse_order_id_from_root_id():
    assert _parse_order_id({"id": "ROOT-42"}) == "ROOT-42"


def test_parse_order_id_empty():
    assert _parse_order_id({}) == ""


def test_parse_order_id_order_takes_priority():
    # 'order' doit primer sur 'transaction' et 'id'
    assert _parse_order_id({"order": {"id": "A"}, "transaction": {"id": "B"}, "id": "C"}) == "A"


def test_mask_long_token():
    assert _mask("abcdefgh") == "****efgh"


def test_mask_short_token():
    assert _mask("abc") == "****"


def test_mask_empty():
    assert _mask("") == "****"


def test_extract_shipping_option_from_shipping_options():
    data = {"shipping_options": [{"id": "SHIP-1"}, {"id": "SHIP-2"}]}
    assert _extract_shipping_option(data) == "SHIP-1"


def test_extract_shipping_option_none():
    assert _extract_shipping_option({}) is None


def test_extract_shipping_option_empty_list():
    assert _extract_shipping_option({"shipping_options": []}) is None


# ── check_available ───────────────────────────────────────────────────────────

async def test_check_available_can_buy_true():
    session = FakeSession(FakeResponse(200, {"item": {"can_buy": True, "title": "Nike", "price": "50"}}))
    with _patch_session(session):
        can_buy, item = await check_available("https://www.vinted.fr", "12345", "tok_test")
    assert can_buy is True
    assert item["title"] == "Nike"
    assert session.calls == [("GET", "https://www.vinted.fr/api/v2/items/12345")]


async def test_check_available_can_buy_false():
    session = FakeSession(FakeResponse(200, {"item": {"can_buy": False}}))
    with _patch_session(session):
        can_buy, item = await check_available("https://www.vinted.fr", "99", "tok")
    assert can_buy is False


async def test_check_available_401_token_invalid():
    session = FakeSession(FakeResponse(401, {}))
    with _patch_session(session):
        can_buy, item = await check_available("https://www.vinted.fr", "1", "bad_token")
    assert can_buy is False
    assert item["error"] == "token_invalid"


async def test_check_available_404_returns_false():
    session = FakeSession(FakeResponse(404, {}))
    with _patch_session(session):
        can_buy, item = await check_available("https://www.vinted.fr", "0", "tok")
    assert can_buy is False


async def test_check_available_timeout():
    session = FakeSession(asyncio.TimeoutError())
    with _patch_session(session):
        can_buy, item = await check_available("https://www.vinted.fr", "1", "tok")
    assert can_buy is False
    assert item["error"] == "timeout"


# ── attempt_buy ───────────────────────────────────────────────────────────────

async def test_attempt_buy_success_201():
    session = FakeSession(FakeResponse(201, {"order": {"id": "ORD-42"}}))
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "12345", "tok", {})
    assert result["success"] is True
    assert result["status"] == "purchased"
    assert result["order_id"] == "ORD-42"


async def test_attempt_buy_success_200():
    session = FakeSession(FakeResponse(200, {"id": "ORD-77"}))
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "12345", "tok", {})
    assert result["success"] is True
    assert result["order_id"] == "ORD-77"


async def test_attempt_buy_409_already_sold():
    session = FakeSession(FakeResponse(409, {}))
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "1", "tok", {})
    assert result["success"] is False
    assert result["status"] == "already_sold"


async def test_attempt_buy_401_token_invalid():
    session = FakeSession(FakeResponse(401, {}))
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "1", "bad", {})
    assert result["success"] is False
    assert result["status"] == "token_invalid"


async def test_attempt_buy_422_validation_error():
    """422 sans option shipping → pas de retry, validation_error."""
    session = FakeSession(FakeResponse(422, {"error": {"message": "champs manquants"}}))
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "1", "tok", {})
    assert result["success"] is False
    assert result["status"] == "validation_error"
    assert "champs manquants" in result["error"]
    # Un seul appel (pas de retry sans shipping_option)
    assert len(session.calls) == 1


async def test_attempt_buy_422_with_shipping_retries_without():
    """422 + shipping_option_id → 2ème POST sans shipping → succès."""
    item_data = {"shipping_options": [{"id": "SHIP-99"}]}
    first = FakeResponse(422, {"error": {"message": "shipping invalide"}})
    second = FakeResponse(201, {"order": {"id": "ORD-RETRY"}})
    session = FakeSession(first, second)
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "5", "tok", item_data)
    assert result["success"] is True
    assert result["order_id"] == "ORD-RETRY"
    # Deux POSTs sur /buy
    assert len(session.calls) == 2
    assert all(url.endswith("/buy") for _, url in session.calls)


async def test_attempt_buy_422_with_shipping_still_fails():
    """422 + shipping_option_id → retry → encore 422 → validation_error."""
    item_data = {"shipping_options": [{"id": "SHIP-1"}]}
    first = FakeResponse(422, {"error": {"message": "err1"}})
    second = FakeResponse(422, {"error": {"message": "err2"}})
    session = FakeSession(first, second)
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "5", "tok", item_data)
    assert result["success"] is False
    assert result["status"] == "validation_error"


async def test_attempt_buy_fallback_to_transactions():
    """Si /buy renvoie un code non géré, on essaie /transactions."""
    buy_resp = FakeResponse(500, {})
    tx_resp = FakeResponse(201, {"transaction": {"id": "TX-99"}})
    session = FakeSession(buy_resp, tx_resp)
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "1", "tok", {})
    assert result["success"] is True
    assert result["order_id"] == "TX-99"
    assert ("POST", "https://www.vinted.fr/api/v2/transactions") in session.calls


async def test_attempt_buy_timeout():
    session = FakeSession(asyncio.TimeoutError())
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "1", "tok", {})
    assert result["success"] is False
    assert result["status"] == "timeout"


# ── fast_buy ─────────────────────────────────────────────────────────────────

async def test_fast_buy_success_201():
    """fast_buy va directement au POST buy → 201 = achat réussi."""
    session = FakeSession(FakeResponse(201, {"order": {"id": "FAST-1"}}))
    with _patch_session(session):
        result = await fast_buy("https://www.vinted.fr", "123", "tok")
    assert result["success"] is True
    assert result["status"] == "purchased"
    assert result["order_id"] == "FAST-1"
    assert session.calls == [("POST", "https://www.vinted.fr/api/v2/items/123/buy")]


async def test_fast_buy_409_already_sold():
    """fast_buy : 409 → already_sold (article parti entre-temps)."""
    session = FakeSession(FakeResponse(409, {}))
    with _patch_session(session):
        result = await fast_buy("https://www.vinted.fr", "99", "tok")
    assert result["success"] is False
    assert result["status"] == "already_sold"


async def test_fast_buy_401_token_invalid():
    session = FakeSession(FakeResponse(401, {}))
    with _patch_session(session):
        result = await fast_buy("https://www.vinted.fr", "1", "bad")
    assert result["success"] is False
    assert result["status"] == "token_invalid"


async def test_fast_buy_timeout_then_success():
    """Premier appel timeout → retry → succès."""
    session = FakeSession(
        asyncio.TimeoutError(),
        FakeResponse(201, {"order": {"id": "RETRY-OK"}}),
    )
    with _patch_session(session), \
         patch.object(autobuy_mod, "_RETRY_DELAY", 0):
        result = await fast_buy("https://www.vinted.fr", "77", "tok")
    assert result["success"] is True
    assert result["order_id"] == "RETRY-OK"
    assert len(session.calls) == 2


async def test_fast_buy_timeout_twice_returns_timeout():
    """Deux timeouts consécutifs → status == 'timeout'."""
    session = FakeSession(asyncio.TimeoutError(), asyncio.TimeoutError())
    with _patch_session(session), \
         patch.object(autobuy_mod, "_RETRY_DELAY", 0):
        result = await fast_buy("https://www.vinted.fr", "77", "tok")
    assert result["success"] is False
    assert result["status"] == "timeout"


async def test_fast_buy_order_id_from_transaction():
    """Réponse avec 'transaction.id' — _parse_order_id l'extrait correctement."""
    session = FakeSession(FakeResponse(200, {"transaction": {"id": "TX-777"}}))
    with _patch_session(session):
        result = await fast_buy("https://www.vinted.fr", "55", "tok")
    assert result["success"] is True
    assert result["order_id"] == "TX-777"


async def test_fast_buy_order_id_from_root_id():
    """Réponse avec 'id' à la racine — _parse_order_id l'extrait correctement."""
    session = FakeSession(FakeResponse(200, {"id": "ROOT-42"}))
    with _patch_session(session):
        result = await fast_buy("https://www.vinted.fr", "56", "tok")
    assert result["success"] is True
    assert result["order_id"] == "ROOT-42"


async def test_fast_buy_fallback_transactions():
    """fast_buy code non géré (503) → fallback /transactions → succès."""
    buy_resp = FakeResponse(503, {})
    tx_resp = FakeResponse(201, {"order": {"id": "TX-FAST-FB"}})
    session = FakeSession(buy_resp, tx_resp)
    with _patch_session(session):
        result = await fast_buy("https://www.vinted.fr", "10", "tok")
    assert result["success"] is True
    assert result["order_id"] == "TX-FAST-FB"
    assert ("POST", "https://www.vinted.fr/api/v2/transactions") in session.calls


# ── run_autobuy ───────────────────────────────────────────────────────────────

async def test_run_autobuy_no_token():
    ad = {"raw_id": "1", "market_key": "fr", "price_num": 50.0, "title": "Nike"}
    result = await run_autobuy(ad=ad, token="", max_price=None, markets_info={})
    assert result["success"] is False
    assert result["status"] == "no_token"


async def test_run_autobuy_price_exceeded():
    ad = {"raw_id": "1", "market_key": "fr", "price_num": 200.0, "title": "Supreme", "price": "200€"}
    result = await run_autobuy(ad=ad, token="tok", max_price=100.0, markets_info={})
    assert result["success"] is False
    assert result["status"] == "price_exceeded"
    assert "200.0" in result["error"]


async def test_run_autobuy_item_unavailable():
    """Flow conservateur (fast=False) : item not can_buy → unavailable."""
    session = FakeSession(FakeResponse(200, {"item": {"can_buy": False}}))
    ad = {"raw_id": "5", "market_key": "fr", "price_num": 30.0, "title": "Objet", "price": "30€", "url": ""}
    with _patch_session(session):
        result = await run_autobuy(ad=ad, token="tok", max_price=None, markets_info={}, fast=False)
    assert result["success"] is False
    assert result["status"] == "unavailable"


async def test_run_autobuy_full_success():
    """Flow conservateur (fast=False) : check → buy → success."""
    check_resp = FakeResponse(200, {"item": {"can_buy": True, "title": "Nike Tech", "price": "80"}})
    buy_resp = FakeResponse(201, {"order": {"id": "ORD-1"}})
    session = FakeSession(check_resp, buy_resp)
    ad = {"raw_id": "42", "market_key": "fr", "price_num": 80.0, "title": "Nike Tech", "price": "80€", "url": "https://vinted.fr/items/42"}
    with _patch_session(session):
        result = await run_autobuy(ad=ad, token="valid_token", max_price=100.0, markets_info={}, fast=False)
    assert result["success"] is True
    assert result["order_id"] == "ORD-1"
    assert result["elapsed_ms"] >= 0


async def test_run_autobuy_price_within_limit():
    """Prix = max_price exact doit passer (comparaison stricte >)."""
    check_resp = FakeResponse(200, {"item": {"can_buy": True}})
    buy_resp = FakeResponse(200, {"id": "ORD-2"})
    session = FakeSession(check_resp, buy_resp)
    ad = {"raw_id": "7", "market_key": "fr", "price_num": 50.0, "title": "X", "price": "50€", "url": ""}
    with _patch_session(session):
        result = await run_autobuy(ad=ad, token="tok", max_price=50.0, markets_info={}, fast=False)
    assert result["success"] is True


async def test_run_autobuy_fast_default_skips_check():
    """run_autobuy fast=True (défaut) ne fait qu'un seul appel POST buy."""
    session = FakeSession(FakeResponse(200, {"id": "ORD-FAST"}))
    ad = {"raw_id": "10", "market_key": "fr", "price_num": 20.0, "title": "Test", "price": "20€", "url": ""}
    with _patch_session(session):
        result = await run_autobuy(ad=ad, token="tok", max_price=None, markets_info={})
    assert result["success"] is True
    assert len(session.calls) == 1
    assert session.calls[0][0] == "POST"


async def test_run_autobuy_dedup_in_flight():
    """Si item_id est déjà dans _in_flight → duplicate_skipped immédiat."""
    autobuy_mod._in_flight.add("fr_dup")
    ad = {"raw_id": "fr_dup", "market_key": "fr", "price_num": 30.0, "title": "Dup", "price": "30€", "url": ""}
    result = await run_autobuy(ad=ad, token="tok", max_price=None, markets_info={})
    assert result["success"] is False
    assert result["status"] == "duplicate_skipped"
    assert autobuy_mod._stats["duplicate_skipped"] == 1


async def test_run_autobuy_dedup_in_flight_no_network_call():
    """duplicate_skipped doit court-circuiter sans appel réseau."""
    session = FakeSession()  # aucune réponse prévue
    autobuy_mod._in_flight.add("no_call")
    ad = {"raw_id": "no_call", "market_key": "fr", "price_num": 10.0, "title": "X", "price": "10€", "url": ""}
    with _patch_session(session):
        await run_autobuy(ad=ad, token="tok", max_price=None, markets_info={})
    assert session.calls == []


async def test_stats_success_incremented():
    """get_stats()['success'] est incrémenté après un achat réussi."""
    session = FakeSession(FakeResponse(201, {"order": {"id": "S1"}}))
    ad = {"raw_id": "1001", "market_key": "fr", "price_num": 20.0, "title": "T", "price": "20€", "url": ""}
    with _patch_session(session):
        result = await run_autobuy(ad=ad, token="tok", max_price=None, markets_info={})
    assert result["success"] is True
    s = get_stats()
    assert s["success"] == 1
    assert s["attempts"] == 1


async def test_stats_already_sold_incremented():
    """get_stats()['already_sold'] est incrémenté sur 409."""
    session = FakeSession(FakeResponse(409, {}))
    ad = {"raw_id": "1002", "market_key": "fr", "price_num": 20.0, "title": "T", "price": "20€", "url": ""}
    with _patch_session(session):
        result = await run_autobuy(ad=ad, token="tok", max_price=None, markets_info={})
    assert result["success"] is False
    assert get_stats()["already_sold"] == 1


async def test_stats_price_exceeded_incremented():
    """get_stats()['price_exceeded'] est incrémenté quand le prix dépasse le max."""
    ad = {"raw_id": "px1", "market_key": "fr", "price_num": 999.0, "title": "Luxe", "price": "999€"}
    await run_autobuy(ad=ad, token="tok", max_price=50.0, markets_info={})
    assert get_stats()["price_exceeded"] == 1
    assert get_stats()["attempts"] == 0  # pas encore compté comme tentative


async def test_stats_timeout_incremented():
    """get_stats()['timeout'] est incrémenté sur TimeoutError persistant."""
    session = FakeSession(asyncio.TimeoutError(), asyncio.TimeoutError())
    ad = {"raw_id": "1003", "market_key": "fr", "price_num": 10.0, "title": "T", "price": "10€", "url": ""}
    with _patch_session(session), \
         patch.object(autobuy_mod, "_RETRY_DELAY", 0):
        result = await run_autobuy(ad=ad, token="tok", max_price=None, markets_info={})
    assert result["success"] is False
    assert get_stats()["timeout"] == 1
