"""
Base de données — dédup rapide + historique des annonces.

Deux responsabilités dans un seul module (elles partagent la même connexion
SQLite et le même cycle de vie) :

  1. Anti-doublons : cache mémoire O(1) (dict), aucune I/O sur le chemin
     critique. La persistance SQLite se fait en arrière-plan (fire-and-forget)
     pour survivre à un redémarrage sans jamais ralentir le scan.
  2. Historique des annonces (table `listings`) : titre/prix/url/image/etc,
     avec le statut d'envoi Telegram/Discord — sert à l'affichage GUI,
     à l'export et au retry des notifications échouées.
"""
import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "vinted_bot.sqlite3"
MAX_MEM_SIZE = 200_000    # évite que le cache dédup grossisse indéfiniment
TTL_SECONDS = 7 * 86400   # 7 jours

# ── Cache mémoire dédup ───────────────────────────────────────────────────────
# Clé : str(id) d'annonce, Valeur : timestamp d'insertion
_cache: dict[str, float] = {}

_loop: Optional[asyncio.AbstractEventLoop] = None
_db_conn = None  # aiosqlite connection, initialisée dans init_db()
_listing_count = 0


async def init_db(db_path: Optional[Path] = None) -> None:
    """À appeler une fois au démarrage depuis la coroutine principale."""
    global _db_conn, _loop, _listing_count
    import aiosqlite

    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _db_conn = await aiosqlite.connect(str(path))
    _loop = asyncio.get_running_loop()

    await _db_conn.execute("PRAGMA journal_mode=WAL")
    await _db_conn.execute("PRAGMA synchronous=NORMAL")

    await _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_ads (
            id TEXT PRIMARY KEY,
            seen_at INTEGER NOT NULL
        )
    """)
    await _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_ads(seen_at)")

    await _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            keyword TEXT,
            market TEXT,
            title TEXT,
            price TEXT,
            price_num REAL,
            url TEXT,
            image TEXT,
            size TEXT,
            seller TEXT,
            detected_at INTEGER NOT NULL,
            telegram_status TEXT DEFAULT 'disabled',
            telegram_attempts INTEGER DEFAULT 0,
            discord_status TEXT DEFAULT 'disabled'
        )
    """)
    await _db_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_detected_at ON listings(detected_at DESC)"
    )
    await _db_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_keyword ON listings(keyword)"
    )
    await _db_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_status ON listings(telegram_status)"
    )
    await _db_conn.commit()

    # Charge les IDs récents en mémoire (< TTL) pour la dédup instantanée
    cutoff = int(time.time()) - TTL_SECONDS
    async with _db_conn.execute(
        "SELECT id, seen_at FROM seen_ads WHERE seen_at > ?", (cutoff,)
    ) as cur:
        rows = await cur.fetchall()
    for row_id, row_ts in rows:
        _cache[row_id] = row_ts

    async with _db_conn.execute("SELECT COUNT(*) FROM listings") as cur:
        row = await cur.fetchone()
        _listing_count = row[0] if row else 0

    logger.info(f"[db] {len(_cache)} IDs dédup chargés, {_listing_count} annonces en historique")


# ── Anti-doublons (API synchrone sur le cache mémoire) ───────────────────────

async def _async_mark_seen(ad_id: str, ts: float) -> None:
    """Persiste un ID vu en SQLite, en arrière-plan (hors chemin critique)."""
    try:
        await _db_conn.execute(
            "INSERT OR IGNORE INTO seen_ads (id, seen_at) VALUES (?, ?)",
            (ad_id, int(ts)),
        )
        await _db_conn.commit()
    except Exception as e:
        logger.debug(f"[db] _async_mark_seen error: {e}")


def is_seen(ad_id: str) -> bool:
    """O(1) — pure mémoire, aucune I/O."""
    return ad_id in _cache


def filter_new(ads: list[dict]) -> list[dict]:
    """
    Retourne les annonces non encore vues et les marque immédiatement.
    Zéro I/O sur le chemin critique (SQLite en arrière-plan).
    """
    now = time.time()
    new = []

    for ad in ads:
        aid = str(ad.get("id", ""))
        if aid and aid not in _cache:
            _cache[aid] = now
            new.append(ad)
            _spawn(_async_mark_seen(aid, now))

    if len(_cache) > MAX_MEM_SIZE:
        _evict_oldest()

    return new


def mark_all_seen(ads: list[dict]) -> None:
    """Warmup — marque tout en mémoire + SQLite en arrière-plan."""
    now = time.time()
    for ad in ads:
        aid = str(ad.get("id", ""))
        if aid:
            _cache[aid] = now
            _spawn(_async_mark_seen(aid, now))


def reset_all() -> None:
    """Efface mémoire + SQLite (dédup ET historique)."""
    global _cache, _listing_count
    _cache = {}
    _listing_count = 0

    async def _do_reset():
        await _db_conn.execute("DELETE FROM seen_ads")
        await _db_conn.execute("DELETE FROM listings")
        await _db_conn.commit()

    _spawn(_do_reset())
    logger.info("[db] Historique effacé")


def get_stats() -> dict:
    return {
        "total_mem": len(_cache),
        "total_listings": _listing_count,
    }


def _evict_oldest() -> None:
    """Supprime les 20% entrées les plus anciennes du cache mémoire (pas SQLite)."""
    cutoff_count = len(_cache) // 5
    sorted_ids = sorted(_cache, key=lambda k: _cache[k])
    for old_id in sorted_ids[:cutoff_count]:
        del _cache[old_id]
    logger.info(f"[db] Éviction mémoire: {cutoff_count} entrées")


async def periodic_purge(interval_hours: float = 6.0) -> None:
    """Coroutine à lancer en arrière-plan : purge SQLite + mémoire au-delà du TTL."""
    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            cutoff = int(time.time()) - TTL_SECONDS
            await _db_conn.execute("DELETE FROM seen_ads WHERE seen_at < ?", (cutoff,))
            await _db_conn.commit()
            evicted = [k for k, v in _cache.items() if v < cutoff]
            for k in evicted:
                del _cache[k]
            if evicted:
                logger.info(f"[db] Purge TTL: {len(evicted)} entrées mémoire")
        except Exception as e:
            logger.debug(f"[db] purge error: {e}")


# ── Historique des annonces ───────────────────────────────────────────────────

def record_listing(ad: dict, telegram_status: str = "disabled", discord_status: str = "disabled") -> None:
    """Enregistre une annonce en base, fire-and-forget (ne bloque jamais le scan)."""
    global _listing_count
    _listing_count += 1
    _spawn(_async_insert_listing(ad, telegram_status, discord_status))


async def _async_insert_listing(ad: dict, telegram_status: str, discord_status: str) -> None:
    try:
        await _db_conn.execute(
            """
            INSERT OR IGNORE INTO listings
                (id, keyword, market, title, price, price_num, url, image,
                 size, seller, detected_at, telegram_status, discord_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(ad.get("id", "")),
                ad.get("keyword", ""),
                ad.get("market_key", ""),
                ad.get("title", ""),
                str(ad.get("price", "")),
                ad.get("price_num"),
                ad.get("url", ""),
                ad.get("image", ""),
                ad.get("size", ""),
                ad.get("seller", ""),
                int(time.time()),
                telegram_status,
                discord_status,
            ),
        )
        await _db_conn.commit()
    except Exception as e:
        logger.debug(f"[db] record_listing error: {e}")


def update_telegram_status(ad_id: str, status: str, attempts: Optional[int] = None) -> None:
    _spawn(_async_update_telegram_status(ad_id, status, attempts))


async def _async_update_telegram_status(ad_id: str, status: str, attempts: Optional[int]) -> None:
    try:
        if attempts is None:
            await _db_conn.execute(
                "UPDATE listings SET telegram_status = ? WHERE id = ?", (status, ad_id)
            )
        else:
            await _db_conn.execute(
                "UPDATE listings SET telegram_status = ?, telegram_attempts = ? WHERE id = ?",
                (status, attempts, ad_id),
            )
        await _db_conn.commit()
    except Exception as e:
        logger.debug(f"[db] update_telegram_status error: {e}")


def update_discord_status(ad_id: str, status: str) -> None:
    _spawn(_async_update_discord_status(ad_id, status))


async def _async_update_discord_status(ad_id: str, status: str) -> None:
    try:
        await _db_conn.execute(
            "UPDATE listings SET discord_status = ? WHERE id = ?", (status, ad_id)
        )
        await _db_conn.commit()
    except Exception as e:
        logger.debug(f"[db] update_discord_status error: {e}")


async def get_recent_listings(limit: int = 300) -> list[dict]:
    """Annonces les plus récentes d'abord — pour hydrater la table GUI au démarrage."""
    cols = [
        "id", "keyword", "market", "title", "price", "price_num", "url", "image",
        "size", "seller", "detected_at", "telegram_status", "telegram_attempts", "discord_status",
    ]
    async with _db_conn.execute(
        f"SELECT {', '.join(cols)} FROM listings ORDER BY detected_at DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(zip(cols, row)) for row in rows]


async def get_failed_telegram(limit: int = 20, max_attempts: int = 5) -> list[dict]:
    """Annonces dont l'envoi Telegram a échoué et qui méritent un nouveau essai."""
    cols = ["id", "keyword", "market", "title", "price", "url", "image", "size", "seller", "telegram_attempts"]
    async with _db_conn.execute(
        f"""SELECT {', '.join(cols)} FROM listings
            WHERE telegram_status = 'failed' AND telegram_attempts < ?
            ORDER BY detected_at DESC LIMIT ?""",
        (max_attempts, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(zip(cols, row)) for row in rows]


async def close() -> None:
    if _db_conn is not None:
        await _db_conn.close()


# ── Internes ──────────────────────────────────────────────────────────────────

def _spawn(coro) -> None:
    """
    Planifie une coroutine en arrière-plan sans jamais bloquer l'appelant.
    Fonctionne aussi bien appelé depuis la loop asyncio (scanner) que depuis
    un thread étranger (ex: bouton "Reset" sur le thread Tkinter).
    """
    if not _loop or _loop.is_closed():
        coro.close()
        return

    if _is_foreign_thread():
        asyncio.run_coroutine_threadsafe(coro, _loop)
    else:
        asyncio.ensure_future(coro)


def _is_foreign_thread() -> bool:
    """True si le thread courant n'est pas celui qui fait tourner la loop asyncio."""
    try:
        asyncio.get_running_loop()
        return False
    except RuntimeError:
        return True
