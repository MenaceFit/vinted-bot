"""
Scanner : plusieurs mots-clés surveillés indépendamment, arrêt propre,
redémarrage, dispatch Telegram/Discord.

Régression clé : l'ancien core/scheduler.py démarrait DEUX boucles de scan
par mot-clé (une jamais trackée par stop()), qui s'accumulaient à chaque
cycle démarrer/arrêter. `test_stop_cancels_all_tasks_no_leak` et
`test_no_duplicate_scan_per_keyword` vérifient que ça ne se reproduit pas.
"""
import asyncio

import pytest

import database.database as db
from core.scanner import Scanner, _parse_kw_list


class StubScraper:
    """Remplace services.vinted.scrape_markets : un item nouveau et
    déterministe à chaque appel, aucun accès réseau."""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, active_keys, params, keywords=None, keyword_key="", per_page=10):
        self.calls.append(keyword_key)
        n = sum(1 for k in self.calls if k == keyword_key)
        ad = {
            "id": f"{keyword_key or 'global'}_{n}", "title": f"Item {keyword_key} #{n}",
            "price": "10 €", "url": "https://vinted.fr/items/x", "image": "",
            "keyword": keyword_key, "market_key": "fr", "created_ts": n,
        }
        return [ad], {"api_ms": 1.0, "parse_ms": 0.5, "retries": 0, "error": False}


@pytest.fixture
async def clean_db(tmp_path):
    db._cache.clear()
    await db.init_db(tmp_path / "scanner_test.sqlite3")
    yield
    await db.close()
    db._cache.clear()


def _base_cfg(keywords, interval):
    return {
        "keywords_filter": keywords,
        "markets": {"fr": {"enabled": True}},
        "interval_seconds": interval,
        "warmup_first_run": True,
        "custom_url": "",
        "catalog_ids": [], "brand_ids": [], "min_price": None, "max_price": None,
        "telegram": {"enabled": False}, "discord": {"enabled": False},
    }


def _make_scanner(scrape_fn, keywords, interval=100):
    cfg = _base_cfg(keywords, interval)
    logs: list[str] = []
    new_ads: list[dict] = []
    statuses: dict[str, list[dict]] = {}
    scanner = Scanner(
        config_provider=lambda: cfg,
        on_log=logs.append,
        on_new_ad=new_ads.append,
        on_keyword_status=lambda kw, status: statuses.setdefault(kw, []).append(status),
        scrape_fn=scrape_fn,
    )
    return scanner, cfg, logs, new_ads, statuses


def _running_scan_tasks():
    return [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done() and (t.get_name() or "").startswith("scan-")
    ]


async def test_multiple_keywords_scanned_independently(clean_db):
    stub = StubScraper()
    scanner, cfg, logs, new_ads, statuses = _make_scanner(stub, ["nike", "jordan", "supreme"])

    scanner.start(asyncio.get_running_loop())
    await asyncio.sleep(0.2)

    assert set(scanner._tasks.keys()) == {"nike", "jordan", "supreme"}
    # Un seul appel par mot-clé pour ce premier cycle (warmup) — pas deux.
    assert sorted(stub.calls) == sorted(["nike", "jordan", "supreme"])

    scanner.stop()
    await asyncio.sleep(0.1)


async def test_no_duplicate_scan_per_keyword(clean_db):
    """Régression directe du bug de double-boucle : un seul mot-clé, un seul
    cycle de warmup attendu -> exactement un appel au scraper, pas deux."""
    stub = StubScraper()
    scanner, *_ = _make_scanner(stub, ["nike"])

    scanner.start(asyncio.get_running_loop())
    await asyncio.sleep(0.2)
    scanner.stop()
    await asyncio.sleep(0.1)

    assert stub.calls == ["nike"]


async def test_stop_cancels_all_tasks_no_leak(clean_db):
    stub = StubScraper()
    scanner, *_ = _make_scanner(stub, ["nike"])

    scanner.start(asyncio.get_running_loop())
    await asyncio.sleep(0.2)
    assert len(_running_scan_tasks()) == 1  # exactement une tâche, pas deux

    scanner.stop()
    await asyncio.sleep(0.1)

    assert _running_scan_tasks() == []


async def test_restart_scanner_after_stop(clean_db):
    stub = StubScraper()
    scanner, *_ = _make_scanner(stub, ["nike"])
    loop = asyncio.get_running_loop()

    scanner.start(loop)
    await asyncio.sleep(0.15)
    scanner.stop()
    await asyncio.sleep(0.05)
    assert not scanner.running
    assert _running_scan_tasks() == []

    scanner.start(loop)
    await asyncio.sleep(0.15)
    assert scanner.running
    assert "nike" in scanner._tasks
    assert len(_running_scan_tasks()) == 1

    scanner.stop()
    await asyncio.sleep(0.1)
    assert _running_scan_tasks() == []


async def test_new_ad_detected_and_dispatched_after_warmup(clean_db, monkeypatch):
    import core.scanner as scanner_mod

    # Le plancher normal (5s) protège Vinted mais rendrait ce test lent ;
    # on l'abaisse ici pour observer plusieurs cycles rapidement.
    monkeypatch.setattr(scanner_mod, "MIN_INTERVAL_SECONDS", 0.02)

    stub = StubScraper()
    scanner, *_rest, new_ads, _statuses = _make_scanner(stub, ["nike"], interval=0.03)

    scanner.start(asyncio.get_running_loop())
    await asyncio.sleep(0.25)  # laisse tourner : cycle 1 = warmup, cycle 2+ = réel
    scanner.stop()
    await asyncio.sleep(0.05)

    assert len(new_ads) >= 1
    assert all(ad["keyword"] == "nike" for ad in new_ads)


async def test_manual_scan_now_notifies_without_auto_start(clean_db):
    """Régression : l'ancien run_once() ne notifiait jamais tant que le scan
    auto n'avait pas tourné une fois (condition de warmup sur un dict vide)."""
    stub = StubScraper()
    scanner, cfg, logs, new_ads, _statuses = _make_scanner(stub, ["nike"])
    scanner._loop = asyncio.get_running_loop()

    await scanner._run_once_async()  # 1er appel = warmup, pas de notif
    assert new_ads == []

    await scanner._run_once_async()  # 2e appel = doit détecter la nouvelle annonce
    assert len(new_ads) == 1
    assert new_ads[0]["id"] == "nike_2"


async def test_keyword_status_reported(clean_db):
    stub = StubScraper()
    scanner, *_rest, statuses = _make_scanner(stub, ["nike"], interval=0.03)

    scanner.start(asyncio.get_running_loop())
    await asyncio.sleep(0.15)
    scanner.stop()
    await asyncio.sleep(0.05)

    assert "nike" in statuses
    assert any(s["status"] == "scanning" for s in statuses["nike"])


async def test_dispatch_sends_telegram_and_records_status(clean_db, monkeypatch):
    import core.scanner as scanner_mod

    sent_ids = []

    async def fake_send_ad_nowait(bot_token, chat_id, ad, send_image=True, on_result=None):
        sent_ids.append(ad["id"])
        if on_result:
            on_result(True)

    monkeypatch.setattr(scanner_mod.telegram_service, "send_ad_nowait", fake_send_ad_nowait)
    monkeypatch.setattr(
        scanner_mod.app_config, "get_secret",
        lambda name, default="": {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1"}.get(name, default),
    )

    updates = []
    scanner = Scanner(
        config_provider=lambda: {"telegram": {"enabled": True, "send_images": True}, "discord": {"enabled": False}},
        on_log=lambda m: None,
        on_new_ad=lambda ad: None,
        on_notification_update=lambda aid, ch, ok: updates.append((aid, ch, ok)),
        scrape_fn=StubScraper(),
    )
    ad = {"id": "fr_100", "title": "Nike", "price": "10€", "url": "https://x", "image": "", "keyword": "nike", "market_key": "fr"}

    await scanner._dispatch_new_ads([ad], scanner.config_provider())
    await asyncio.sleep(0.1)

    assert sent_ids == ["fr_100"]
    assert updates == [("fr_100", "telegram", True)]
    rows = await db.get_recent_listings(10)
    assert rows[0]["telegram_status"] == "sent"


async def test_dispatch_records_ad_even_when_telegram_fails(clean_db, monkeypatch):
    """Le pipeline Vinted -> dedup -> DB -> Telegram doit être résilient :
    une annonce reste enregistrée même si l'envoi Telegram échoue."""
    import core.scanner as scanner_mod

    async def failing_send(bot_token, chat_id, ad, send_image=True, on_result=None):
        if on_result:
            on_result(False)

    monkeypatch.setattr(scanner_mod.telegram_service, "send_ad_nowait", failing_send)
    monkeypatch.setattr(
        scanner_mod.app_config, "get_secret",
        lambda name, default="": {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1"}.get(name, default),
    )

    updates = []
    scanner = Scanner(
        config_provider=lambda: {"telegram": {"enabled": True, "send_images": True}, "discord": {"enabled": False}},
        on_log=lambda m: None,
        on_new_ad=lambda ad: None,
        on_notification_update=lambda aid, ch, ok: updates.append((aid, ch, ok)),
        scrape_fn=StubScraper(),
    )
    ad = {"id": "fr_101", "title": "Jordan", "price": "20€", "url": "https://x", "image": "", "keyword": "jordan", "market_key": "fr"}

    await scanner._dispatch_new_ads([ad], scanner.config_provider())
    await asyncio.sleep(0.1)

    assert updates == [("fr_101", "telegram", False)]
    rows = await db.get_recent_listings(10)
    assert len(rows) == 1  # l'annonce est bien en base malgré l'échec Telegram
    assert rows[0]["telegram_status"] == "failed"


async def test_sync_keywords_hot_adds_keyword(clean_db, monkeypatch):
    """sync_keywords() doit démarrer une nouvelle tâche sans arrêter le scanner."""
    import core.scanner as scanner_mod
    monkeypatch.setattr(scanner_mod, "MIN_INTERVAL_SECONDS", 0.02)

    stub = StubScraper()
    scanner, cfg, logs, new_ads, _ = _make_scanner(stub, ["nike"], interval=0.03)
    loop = asyncio.get_running_loop()

    scanner.start(loop)
    await asyncio.sleep(0.15)
    assert set(scanner._tasks.keys()) == {"nike"}

    # Ajoute "jordan" à chaud
    cfg["keywords_filter"] = ["nike", "jordan"]
    scanner.sync_keywords()
    await asyncio.sleep(0.15)

    assert "jordan" in scanner._tasks
    assert "nike" in scanner._tasks
    assert scanner.running

    scanner.stop()
    await asyncio.sleep(0.05)


async def test_sync_keywords_hot_removes_keyword(clean_db, monkeypatch):
    """sync_keywords() doit annuler la tâche d'un mot-clé retiré."""
    import core.scanner as scanner_mod
    monkeypatch.setattr(scanner_mod, "MIN_INTERVAL_SECONDS", 0.02)

    stub = StubScraper()
    scanner, cfg, logs, new_ads, _ = _make_scanner(stub, ["nike", "jordan"], interval=0.03)
    loop = asyncio.get_running_loop()

    scanner.start(loop)
    await asyncio.sleep(0.15)
    assert set(scanner._tasks.keys()) == {"nike", "jordan"}

    # Retire "jordan" à chaud
    cfg["keywords_filter"] = ["nike"]
    scanner.sync_keywords()
    await asyncio.sleep(0.15)

    assert "jordan" not in scanner._tasks
    assert "nike" in scanner._tasks

    scanner.stop()
    await asyncio.sleep(0.05)


async def test_saturation_warning_logged(clean_db, monkeypatch):
    """Quand toutes les annonces reçues sont nouvelles (saturation), un avertissement
    est loggué car des annonces ont peut-être été manquées."""
    import core.scanner as scanner_mod
    from services.vinted import PER_PAGE_DEFAULT

    monkeypatch.setattr(scanner_mod, "MIN_INTERVAL_SECONDS", 0.01)

    call_count = [0]

    async def saturated_scraper(active_keys, params, keywords=None, keyword_key="", per_page=10):
        """Retourne exactement per_page items différents à chaque appel → saturation."""
        call_count[0] += 1
        n = call_count[0]
        ads = [
            {"id": f"item_{n}_{i}", "title": f"Saturated {i}", "price": "10€", "url": "", "image": "",
             "keyword": keyword_key, "market_key": "fr", "created_ts": n * 100 + i}
            for i in range(per_page)
        ]
        return ads, {"api_ms": 1.0, "parse_ms": 0.5, "retries": 0, "error": False}

    scanner, cfg, logs, new_ads, _ = _make_scanner(saturated_scraper, ["test"], interval=0.02)
    cfg["warmup_first_run"] = False  # skip warmup to reach saturation check faster

    scanner.start(asyncio.get_running_loop())
    await asyncio.sleep(0.2)
    scanner.stop()
    await asyncio.sleep(0.05)

    warning_logs = [l for l in logs if "annonces reçues étaient nouvelles" in l]
    assert len(warning_logs) >= 1


# ── _parse_kw_list ───────────────────────────────────────────────────────────

def test_parse_kw_list_strings():
    """Strings simples → dicts avec text et champs None."""
    result = _parse_kw_list(["nike", "jordan"])
    assert len(result) == 2
    assert result[0]["text"] == "nike"
    assert result[0]["interval"] is None
    assert result[0]["max_price"] is None
    assert result[1]["text"] == "jordan"


def test_parse_kw_list_dicts():
    """Dicts complets préservés tel quel."""
    raw = [{"text": "jordan 1", "interval": 3, "max_price": 150, "autobuy_enabled": True}]
    result = _parse_kw_list(raw)
    assert result[0]["text"] == "jordan 1"
    assert result[0]["interval"] == 3
    assert result[0]["max_price"] == 150
    assert result[0]["autobuy_enabled"] is True


def test_parse_kw_list_mixed():
    """Mix string + dict → tous normalisés."""
    raw = ["nike", {"text": "supreme", "interval": 5}]
    result = _parse_kw_list(raw)
    assert result[0]["text"] == "nike"
    assert result[0]["interval"] is None
    assert result[1]["text"] == "supreme"
    assert result[1]["interval"] == 5


def test_parse_kw_list_empty():
    assert _parse_kw_list([]) == []


# ── per-keyword interval ─────────────────────────────────────────────────────

async def test_per_keyword_interval_overrides_global(clean_db, monkeypatch):
    """La tâche d'un mot-clé utilise son propre intervalle quand défini."""
    import core.scanner as scanner_mod
    monkeypatch.setattr(scanner_mod, "MIN_INTERVAL_SECONDS", 0.01)

    stub = StubScraper()
    cfg = {
        "keywords_filter": [{"text": "nike", "interval": 0.05}],
        "markets": {"fr": {"enabled": True}},
        "interval_seconds": 999,  # global très long — ne doit pas être utilisé
        "warmup_first_run": False,
        "custom_url": "",
        "catalog_ids": [], "brand_ids": [], "min_price": None, "max_price": None,
        "telegram": {"enabled": False}, "discord": {"enabled": False},
    }
    scanner = Scanner(
        config_provider=lambda: cfg,
        on_log=lambda m: None,
        on_new_ad=lambda ad: None,
        scrape_fn=stub,
    )
    scanner.start(asyncio.get_running_loop())
    await asyncio.sleep(0.25)
    scanner.stop()
    await asyncio.sleep(0.05)

    # Avec interval per-keyword=0.05s et warmup=False, on attend plusieurs cycles
    assert len(stub.calls) >= 3


def test_market_breakdown_hidden_for_single_market():
    """Un seul marché actif : le total déjà affiché suffit, pas besoin de détail."""
    scanner = Scanner(config_provider=lambda: {}, on_log=lambda m: None, on_new_ad=lambda ad: None)
    assert scanner._market_breakdown({"per_market": {"uk": 0}}) == ""
    assert scanner._market_breakdown({}) == ""


def test_market_breakdown_shown_for_multiple_markets():
    """Plusieurs marchés actifs : un marché resté à 0 doit être visible, pas noyé
    dans le total agrégé (cas exact du signalement 'ça ne scanne pas UK')."""
    scanner = Scanner(config_provider=lambda: {}, on_log=lambda m: None, on_new_ad=lambda ad: None)
    breakdown = scanner._market_breakdown({"per_market": {"fr": 10, "uk": 0}})
    assert "FR: 10" in breakdown
    assert "UK: 0" in breakdown
