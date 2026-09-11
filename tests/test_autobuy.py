"""
Service autobuy : vérification disponibilité, tentative d'achat, filtres prix.
Aucun appel réseau réel — session monkeypatchée.
"""
import asyncio
import pytest
from unittest.mock import patch

from services import autobuy as autobuy_mod
from services.autobuy import check_available, attempt_buy, run_autobuy


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
    session = FakeSession(FakeResponse(422, {"error": {"message": "champs manquants"}}))
    with _patch_session(session):
        result = await attempt_buy("https://www.vinted.fr", "1", "tok", {})
    assert result["success"] is False
    assert result["status"] == "validation_error"
    assert "champs manquants" in result["error"]


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
    session = FakeSession(FakeResponse(200, {"item": {"can_buy": False}}))
    ad = {"raw_id": "5", "market_key": "fr", "price_num": 30.0, "title": "Objet", "price": "30€", "url": ""}
    with _patch_session(session):
        result = await run_autobuy(ad=ad, token="tok", max_price=None, markets_info={})
    assert result["success"] is False
    assert result["status"] == "unavailable"


async def test_run_autobuy_full_success():
    check_resp = FakeResponse(200, {"item": {"can_buy": True, "title": "Nike Tech", "price": "80"}})
    buy_resp = FakeResponse(201, {"order": {"id": "ORD-1"}})
    session = FakeSession(check_resp, buy_resp)
    ad = {"raw_id": "42", "market_key": "fr", "price_num": 80.0, "title": "Nike Tech", "price": "80€", "url": "https://vinted.fr/items/42"}
    with _patch_session(session):
        result = await run_autobuy(ad=ad, token="valid_token", max_price=100.0, markets_info={})
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
        result = await run_autobuy(ad=ad, token="tok", max_price=50.0, markets_info={})
    assert result["success"] is True
