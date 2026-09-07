"""
Scraper Vinted : retry sur erreur réseau, abandon propre après le nombre
maximal de tentatives. Aucun appel réseau réel (session monkeypatchée).
"""
import aiohttp
import pytest

import services.vinted as vinted_module
from services.vinted import MARKETS, MAX_RETRY, MarketScraper


class FakeResponse:
    def __init__(self, status: int, json_data: dict):
        self.status = status
        self._json = json_data
        self.headers: dict = {}

    async def json(self, content_type=None):
        return self._json

    async def text(self, errors="strict"):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.closed = False

    def get(self, url, params=None, headers=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


async def _noop_reinit(self=None):
    return None


def _make_scraper(monkeypatch, responses):
    scraper = MarketScraper("fr", MARKETS["fr"])
    scraper._initialized = True  # saute l'init cookies/CSRF, hors-scope ici
    fake = FakeSession(responses)
    monkeypatch.setattr(scraper, "_get_session", lambda: fake)
    monkeypatch.setattr(scraper, "_reinit", _noop_reinit)
    return scraper, fake


async def test_fetch_raw_retries_then_succeeds(monkeypatch):
    scraper, fake = _make_scraper(monkeypatch, [
        aiohttp.ClientConnectionError("connexion refusée"),
        FakeResponse(200, {"items": [{"id": 1, "title": "Nike Tech Fleece"}]}),
    ])

    items, retries = await scraper.fetch_raw({"catalog_ids[]": "2050"}, per_page=10)

    assert len(items) == 1
    assert items[0]["title"] == "Nike Tech Fleece"
    assert retries == 1
    assert fake.calls == 2


async def test_fetch_raw_gives_up_after_max_retries(monkeypatch):
    scraper, fake = _make_scraper(monkeypatch, [aiohttp.ClientConnectionError("down")] * MAX_RETRY)

    items, retries = await scraper.fetch_raw({}, per_page=10)

    assert items == []
    assert retries == MAX_RETRY
    assert fake.calls == MAX_RETRY
    assert scraper.stats["errors"] == 1


async def test_fetch_raw_handles_rate_limit_then_succeeds(monkeypatch):
    scraper, fake = _make_scraper(monkeypatch, [
        FakeResponse(429, {}),
        FakeResponse(200, {"items": []}),
    ])
    fake.responses[0].headers = {"Retry-After": "0"}

    items, retries = await scraper.fetch_raw({}, per_page=10)

    assert items == []  # 0 items, mais requête réussie (pas d'erreur)
    assert fake.calls == 2


async def test_fetch_raw_session_never_initialized_is_a_visible_error(monkeypatch):
    """Régression : si la session n'est jamais établie pour un marché (init
    échouée ou encore en cooldown), fetch_raw renvoyait ([], 0) — indiscernable
    d'un cycle qui a réussi mais n'a rien trouvé. Un marché en échec devait
    apparaître comme une vraie erreur (comptée + retries > 0), pas comme un
    simple '0 résultat' silencieux."""
    scraper = MarketScraper("uk", MARKETS["uk"])
    scraper._initialized = False

    async def _init_never_succeeds(self=None):
        return None  # ne met jamais self._initialized à True

    monkeypatch.setattr(scraper, "_init_session", _init_never_succeeds)

    items, retries = await scraper.fetch_raw({}, per_page=10)

    assert items == []
    assert retries >= 1
    assert scraper.stats["errors"] == 1


async def test_scrape_reports_saturation_via_result_length(monkeypatch):
    """Si autant d'items sont reçus que demandé (per_page), l'appelant
    (Scanner) doit pouvoir détecter qu'on a peut-être manqué des annonces."""
    raw_items = [{"id": i, "title": f"Item {i}"} for i in range(10)]
    scraper, _fake = _make_scraper(monkeypatch, [FakeResponse(200, {"items": raw_items})])

    result = await scraper.scrape({"catalog_ids[]": "2050"}, per_page=10)

    assert len(result.ads) == 10
    assert result.error is False


# ── Test de connectivité par marché (bouton "🔬 Tester les 3 marchés") ──────

@pytest.fixture
def reset_scrapers():
    """Les MarketScraper testés ici sont les singletons partagés de
    services.vinted._scrapers — on restaure leur état pour ne pas polluer
    les autres tests qui les utilisent (ils sont réutilisés par toute l'app)."""
    saved = {k: (s._initialized, s._last_init_attempt) for k, s in vinted_module._scrapers.items()}
    yield
    for k, (initialized, last_attempt) in saved.items():
        vinted_module._scrapers[k]._initialized = initialized
        vinted_module._scrapers[k]._last_init_attempt = last_attempt


async def test_market_connectivity_reports_success(monkeypatch, reset_scrapers):
    scraper = vinted_module._scrapers["uk"]
    scraper._initialized = True
    fake = FakeResponse(200, {"items": [{"id": 1, "title": "Nike Tech Fleece", "price_numeric": "45.0"}]})
    monkeypatch.setattr(scraper, "_get_session", lambda: FakeSession([fake]))

    result = await vinted_module.test_market_connectivity("uk")

    assert result == {"market": "uk", "ok": True, "items": 1, "reason": ""}


async def test_market_connectivity_reports_session_never_established(monkeypatch, reset_scrapers):
    scraper = vinted_module._scrapers["uk"]
    scraper._initialized = False
    monkeypatch.setattr(scraper, "_init_session", _noop_reinit)

    result = await vinted_module.test_market_connectivity("uk")

    assert result["ok"] is False
    assert result["market"] == "uk"
    assert "session" in result["reason"]


async def test_market_connectivity_reports_missing_fields(monkeypatch, reset_scrapers):
    """Reproduit le signalement 'titre/prix vides' : items reçus mais sans
    les champs attendus doit être un échec explicite, pas un faux succès."""
    scraper = vinted_module._scrapers["pl"]
    scraper._initialized = True
    fake = FakeResponse(200, {"items": [{"id": 1}]})  # ni title ni price
    monkeypatch.setattr(scraper, "_get_session", lambda: FakeSession([fake]))

    result = await vinted_module.test_market_connectivity("pl")

    assert result["ok"] is False
    assert "title" in result["reason"] and "price" in result["reason"]


async def test_all_markets_tests_fr_uk_pl(monkeypatch, reset_scrapers):
    for key in vinted_module.MARKETS:
        scraper = vinted_module._scrapers[key]
        scraper._initialized = True
        fake = FakeResponse(200, {"items": [{"id": 1, "title": "X", "price_numeric": "1"}]})
        monkeypatch.setattr(scraper, "_get_session", lambda fake=fake: FakeSession([fake]))

    results = await vinted_module.test_all_markets()

    assert {r["market"] for r in results} == {"fr", "uk", "pl"}
    assert all(r["ok"] for r in results)
