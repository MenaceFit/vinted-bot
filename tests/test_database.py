"""
Base de données : détection nouvelle annonce, doublon, ajout en base,
persistance de la dédup après redémarrage.
"""
import asyncio

import pytest

import database.database as db


@pytest.fixture
async def fresh_db(tmp_path):
    db._cache.clear()
    db._listing_count = 0
    await db.init_db(tmp_path / "test.sqlite3")
    yield db
    await db.close()
    db._cache.clear()


async def test_new_ad_is_detected(fresh_db):
    ads = [{"id": "fr_1", "title": "Nike Tech Fleece"}]
    new = db.filter_new(ads)
    assert [a["id"] for a in new] == ["fr_1"]


async def test_duplicate_ad_not_returned_twice(fresh_db):
    ads = [{"id": "fr_1", "title": "Nike Tech Fleece"}]
    db.filter_new(ads)
    again = db.filter_new(ads)
    assert again == []


async def test_duplicate_across_multiple_keywords_matching_same_ad(fresh_db):
    """Une même annonce vue par deux mots-clés différents ne doit être
    renvoyée comme 'nouvelle' qu'une seule fois."""
    ad = {"id": "fr_42", "title": "Nike x Supreme"}
    first_batch = db.filter_new([dict(ad, keyword="nike")])
    second_batch = db.filter_new([dict(ad, keyword="supreme")])
    assert len(first_batch) == 1
    assert second_batch == []


async def test_record_listing_persists_fields(fresh_db):
    ad = {
        "id": "fr_2", "title": "Jordan 1", "price": "120 €", "url": "https://vinted.fr/items/2",
        "image": "https://img/2.jpg", "keyword": "jordan", "market_key": "fr", "size": "42", "seller": "bob",
    }
    db.record_listing(ad, telegram_status="pending", discord_status="disabled")
    await asyncio.sleep(0.05)

    rows = await db.get_recent_listings(10)
    assert len(rows) == 1
    assert rows[0]["title"] == "Jordan 1"
    assert rows[0]["price"] == "120 €"
    assert rows[0]["telegram_status"] == "pending"
    assert rows[0]["discord_status"] == "disabled"


async def test_update_telegram_status(fresh_db):
    ad = {"id": "fr_3", "title": "Stussy Hoodie", "price": "60 €", "url": "https://x", "image": "", "keyword": "stussy", "market_key": "fr"}
    db.record_listing(ad, telegram_status="pending", discord_status="disabled")
    await asyncio.sleep(0.05)

    db.update_telegram_status("fr_3", "failed", attempts=1)
    await asyncio.sleep(0.05)

    failed = await db.get_failed_telegram()
    assert len(failed) == 1
    assert failed[0]["id"] == "fr_3"
    assert failed[0]["telegram_attempts"] == 1

    db.update_telegram_status("fr_3", "sent", attempts=2)
    await asyncio.sleep(0.05)
    failed_after = await db.get_failed_telegram()
    assert failed_after == []


async def test_dedup_persists_across_restart(tmp_path):
    """Une annonce déjà vue reste connue même si l'appli/le scanner redémarre."""
    path = tmp_path / "restart.sqlite3"

    db._cache.clear()
    await db.init_db(path)
    db.filter_new([{"id": "fr_99", "title": "Carhartt Jacket"}])
    await asyncio.sleep(0.05)
    await db.close()

    db._cache.clear()
    await db.init_db(path)
    try:
        assert db.is_seen("fr_99")
        assert db.filter_new([{"id": "fr_99", "title": "Carhartt Jacket"}]) == []
    finally:
        await db.close()
        db._cache.clear()


async def test_reset_all_clears_dedup_and_listings(fresh_db):
    ad = {"id": "fr_5", "title": "Adidas Samba", "price": "80 €", "url": "https://x", "image": "", "keyword": "adidas", "market_key": "fr"}
    db.filter_new([ad])
    db.record_listing(ad)
    await asyncio.sleep(0.05)

    db.reset_all()
    await asyncio.sleep(0.05)

    assert not db.is_seen("fr_5")
    rows = await db.get_recent_listings(10)
    assert rows == []


async def test_mark_all_seen_batch(fresh_db):
    """mark_all_seen doit persister plusieurs IDs en une seule transaction."""
    ads = [{"id": f"warm_{i}"} for i in range(5)]
    db.mark_all_seen(ads)
    await asyncio.sleep(0.05)

    # Tous doivent être en cache mémoire
    for i in range(5):
        assert db.is_seen(f"warm_{i}")

    # Aucun ne doit ressortir comme nouveau
    new = db.filter_new(ads)
    assert new == []

    # Persistés en SQLite (doivent survivre au rechargement)
    counts_before = db.get_stats()["total_mem"]
    assert counts_before >= 5


async def test_mark_all_seen_empty_list(fresh_db):
    """mark_all_seen avec une liste vide ne doit pas planter."""
    db.mark_all_seen([])  # pas d'exception
    assert db.get_stats()["total_mem"] == 0


async def test_reset_all_resets_listing_count(fresh_db):
    """Bug: reset_all() ne remettait pas _listing_count à zéro → get_stats() restait faux."""
    ads = [
        {"id": f"fr_{i}", "title": f"Sneaker {i}", "price": "10€", "url": "https://x", "image": "", "keyword": "", "market_key": "fr"}
        for i in range(3)
    ]
    for ad in ads:
        db.record_listing(ad)
    await asyncio.sleep(0.05)

    assert db.get_stats()["total_listings"] == 3

    db.reset_all()
    await asyncio.sleep(0.05)

    assert db.get_stats()["total_listings"] == 0


async def test_filter_new_ignores_ads_without_id(fresh_db):
    """Annonces sans champ 'id' ou avec id vide sont silencieusement ignorées."""
    ads = [
        {"title": "Pas d'ID"},
        {"id": "", "title": "ID vide"},
        {"id": "fr_ok", "title": "Celui-ci est valide"},
    ]
    new = db.filter_new(ads)
    assert len(new) == 1
    assert new[0]["id"] == "fr_ok"
    # Les deux premiers n'ont pas pollué le cache
    assert not db.is_seen("")


async def test_listing_count_increments_per_insert(fresh_db):
    """_listing_count est incrémenté à chaque record_listing, même avant la commit SQLite."""
    assert db.get_stats()["total_listings"] == 0
    for i in range(5):
        db.record_listing({"id": f"fr_{i}", "title": f"Item {i}", "price": "5€", "url": "https://x", "image": "", "keyword": "", "market_key": "fr"})
    assert db.get_stats()["total_listings"] == 5


async def test_evict_oldest_preserves_newest(fresh_db, monkeypatch):
    """_evict_oldest supprime les 20% entrées les plus anciennes, pas les récentes."""
    import database.database as db_mod
    monkeypatch.setattr(db_mod, "MAX_MEM_SIZE", 5)

    now = 1_700_000_000.0
    # Peuple le cache à la main avec des timestamps croissants
    for i in range(5):
        db_mod._cache[f"old_{i}"] = now + i  # old_0 est le plus vieux

    # Simule l'ajout d'un 6ème item qui déclenche l'éviction
    db_mod._cache["new_6"] = now + 100
    db_mod._evict_oldest()

    # Après éviction de 20% de 6 items = 1 entrée : old_0 doit partir
    assert "old_0" not in db_mod._cache
    assert "new_6" in db_mod._cache
    # old_1..4 et new_6 restent (5 entrées)
    assert all(f"old_{i}" in db_mod._cache for i in range(1, 5))
