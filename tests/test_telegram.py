"""
Service Telegram : envoi réussi, repli image→texte, échec explicite,
erreur réseau + retry, test de connexion.

Aucun appel réseau réel : `_get_session()` est monkeypatché avec une fausse
session qui rejoue une séquence de réponses/erreurs prédéfinies.
"""
import aiohttp
import pytest

import services.telegram as telegram


class FakeResponse:
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data = data

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Rejoue une séquence de réponses (ou lève une exception) à chaque post()."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url, json=None):
        self.calls.append((url, json))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_session(monkeypatch):
    def _install(responses):
        fake = FakeSession(responses)
        monkeypatch.setattr(telegram, "_get_session", lambda: fake)
        return fake
    return _install


AD = {
    "title": "Nike Tech Fleece Noir", "price": "45 €",
    "url": "https://www.vinted.fr/items/1", "image": "https://images.vinted.net/1.jpg",
}


async def test_send_ad_with_photo_success(fake_session):
    fake = fake_session([FakeResponse(200, {"ok": True, "result": {}})])

    ok, error = await telegram.send_ad("TOKEN", "123", AD, send_image=True)

    assert ok is True
    assert error == ""
    assert len(fake.calls) == 1
    assert fake.calls[0][0].endswith("/sendPhoto")
    assert "Nike Tech Fleece Noir" in fake.calls[0][1]["caption"]
    assert "45 €" in fake.calls[0][1]["caption"]


async def test_send_ad_photo_failure_falls_back_to_text(fake_session):
    fake = fake_session([
        FakeResponse(400, {"ok": False, "description": "Bad Request: wrong file identifier/HTTP URL specified"}),
        FakeResponse(200, {"ok": True, "result": {}}),
    ])

    ok, error = await telegram.send_ad("TOKEN", "123", AD, send_image=True)

    assert ok is True
    assert error == ""
    assert [c[0].rsplit("/", 1)[-1] for c in fake.calls] == ["sendPhoto", "sendMessage"]


async def test_send_ad_total_failure_returns_false(fake_session):
    fake_session([FakeResponse(400, {"ok": False, "description": "chat not found"})])

    ok, error = await telegram.send_ad("TOKEN", "BADCHAT", AD, send_image=False)

    assert ok is False
    assert error == "chat not found"
    assert telegram.is_connected() is False


async def test_network_error_then_retry_succeeds(fake_session):
    fake = fake_session([
        aiohttp.ClientConnectionError("connexion refusée"),
        FakeResponse(200, {"ok": True, "result": {}}),
    ])

    ok, data = await telegram._call("TOKEN", "sendMessage", {"chat_id": "1", "text": "hi"})

    assert ok is True
    assert len(fake.calls) == 2


async def test_rate_limit_429_is_retried(fake_session):
    fake = fake_session([
        FakeResponse(429, {"ok": False, "parameters": {"retry_after": 0}}),
        FakeResponse(200, {"ok": True, "result": {}}),
    ])

    ok, data = await telegram._call("TOKEN", "sendMessage", {"chat_id": "1", "text": "hi"})

    assert ok is True
    assert len(fake.calls) == 2


async def test_connection_test_bad_token(fake_session):
    fake_session([FakeResponse(401, {"ok": False, "description": "Unauthorized"})])

    ok, message = await telegram.test_connection("BADTOKEN", "123")

    assert ok is False
    assert "Unauthorized" in message


async def test_connection_test_bad_chat_id(fake_session):
    fake_session([
        FakeResponse(200, {"ok": True, "result": {"username": "vinted_bot"}}),
        FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"}),
    ])

    ok, message = await telegram.test_connection("TOKEN", "BADCHAT")

    assert ok is False
    assert "chat not found" in message


async def test_connection_test_success(fake_session):
    fake_session([
        FakeResponse(200, {"ok": True, "result": {"username": "vinted_bot"}}),
        FakeResponse(200, {"ok": True, "result": {}}),
    ])

    ok, message = await telegram.test_connection("TOKEN", "123")

    assert ok is True
    assert "vinted_bot" in message


async def test_missing_credentials_never_calls_api(fake_session):
    fake = fake_session([])
    results = []

    await telegram.send_ad_nowait("", "", AD, on_result=lambda ok, error: results.append((ok, error)))

    assert len(results) == 1
    assert results[0][0] is False
    assert results[0][1]  # un message d'erreur explicite, non vide
    assert fake.calls == []
