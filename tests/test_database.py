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
