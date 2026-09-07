"""
Scraper Vinted — async aiohttp, session persistante, détection ultra-rapide.

  - aiohttp async → aucun thread bloquant, aucun time.sleep dans le chemin de scan
  - ClientSession persistante par marché (keep-alive, HTTP pipelining)
  - per_page=10 par défaut (on ne cherche que les NOUVEAUX items, pas 30)
  - Filtrage mots-clés via search_text passé directement à l'API (côté serveur)
  - Retry exponentiel async, gestion 401/403/429, reinit de session automatique
  - Timeout aiohttp fin (connect=4s, read=9s)
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse

import aiohttp

logger = logging.getLogger(__name__)

MARKETS = {
    "fr": {
        "label":    "🇫🇷 France",
        "base_url": "https://www.vinted.fr",
        "lang":     "fr-FR,fr;q=0.9",
        "region":   "france",
    },
    "pl": {
        "label":    "🇵🇱 Pologne",
        "base_url": "https://www.vinted.pl",
        "lang":     "pl-PL,pl;q=0.9",
        "region":   "pologne",
    },
    "uk": {
        "label":    "🇬🇧 Angleterre",
        "base_url": "https://www.vinted.co.uk",
        "lang":     "en-GB,en;q=0.9",
        "region":   "angleterre",
    },
}

# ── Constantes ───────────────────────────────────────────────────────────────
PER_PAGE_DEFAULT = 10   # on ne veut que les derniers items (réduit bande passante)
PER_PAGE_WARMUP  = 30   # warmup : mémoriser plus d'articles
TIMEOUT_CONNECT  = 4     # secondes
TIMEOUT_READ     = 9     # secondes
MAX_RETRY        = 3
INIT_COOLDOWN    = 5.0   # secondes entre deux tentatives d'init

# Un MarketScraper est un singleton PAR MARCHÉ, partagé par toutes les tâches
# mot-clé qui surveillent ce marché. La limite de connexions doit donc pouvoir
# absorber N mots-clés scannés en même temps sans que les requêtes ne fassent
# la queue derrière un plafond bas (c'était le cas avec limit_per_host=3 en v3 :
# 6 mots-clés actifs => seules 3 requêtes en vol, les 3 autres attendaient).
CONNECTOR_LIMIT = 20

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _browser_headers(lang: str) -> dict:
    return {
        "User-Agent":         UA,
        "Accept-Language":    lang,
        "Accept-Encoding":    "gzip, deflate, br",
        "Connection":         "keep-alive",
        "Sec-Fetch-Dest":     "document",
        "Sec-Fetch-Mode":     "navigate",
        "Sec-Fetch-Site":     "none",
        "sec-ch-ua":          '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def _api_headers(base_url: str, lang: str, csrf: Optional[str] = None) -> dict:
    h = {
        "User-Agent":         UA,
        "Accept":             "application/json, text/plain, */*",
        "Accept-Language":    lang,
        "Accept-Encoding":    "gzip, deflate, br",
        "Connection":         "keep-alive",
        "Referer":            f"{base_url}/catalog",
        "Origin":             base_url,
        "Sec-Fetch-Dest":     "empty",
        "Sec-Fetch-Mode":     "cors",
        "Sec-Fetch-Site":     "same-origin",
        "sec-ch-ua":          '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    if csrf:
        h["X-CSRF-Token"] = csrf
    return h


def extract_params_from_url(vinted_url: str) -> dict:
    """Extrait les paramètres depuis une URL Vinted catalog."""
    parsed = urlparse(vinted_url)
    qs = parse_qs(parsed.query, keep_blank_values=False)
    params: dict = {}
    if "catalog[]" in qs:
        params["catalog_ids[]"] = qs["catalog[]"]
    if "brand_ids[]" in qs:
        params["brand_ids[]"] = qs["brand_ids[]"]
    if "search_text" in qs:
        params["search_text"] = qs["search_text"][0]
    if "price_from" in qs:
        params["price_from"] = qs["price_from"][0]
    if "price_to" in qs:
        params["price_to"] = qs["price_to"][0]
    if "size_ids[]" in qs:
        params["size_ids[]"] = qs["size_ids[]"]
    if "status_ids[]" in qs:
        params["status_ids[]"] = qs["status_ids[]"]
    params["order"] = "newest_first"
    return params


class ScrapeResult:
    """Retour d'un cycle de scan : annonces + timings réels pour le monitoring."""

    __slots__ = ("ads", "api_ms", "parse_ms", "retries", "error")

    def __init__(self, ads: list[dict], api_ms: float, parse_ms: float, retries: int = 0, error: bool = False):
        self.ads = ads
        self.api_ms = api_ms
        self.parse_ms = parse_ms
        self.retries = retries
        self.error = error


class MarketScraper:
    """
    Scraper async dédié à un marché Vinted.
    Une seule ClientSession persistante par marché (connexion keep-alive),
    partagée par toutes les tâches mot-clé qui surveillent ce marché.
    """

    def __init__(self, market_key: str, market_info: dict):
        self.key      = market_key
        self.label    = market_info["label"]
        self.base_url = market_info["base_url"]
        self.lang     = market_info["lang"]
        self.region   = market_info["region"]
        self.api_url  = f"{self.base_url}/api/v2/catalog/items"

        self._session: Optional[aiohttp.ClientSession] = None
        self._csrf: Optional[str] = None
        self._initialized = False
        # -inf : garantit que la toute première tentative d'init n'est jamais
        # sautée par erreur si time.monotonic() démarre proche de 0 (dépend
        # de la plateforme) — avant, 0.0 pouvait faire échouer silencieusement
        # le tout premier scan d'un marché.
        self._last_init_attempt = float("-inf")
        self._consecutive_failures = 0

        self.stats = {
            "scans":         0,
            "total_fetched": 0,
            "total_new":     0,
            "errors":        0,
            "last_scan_ms":  0.0,
            "last_api_ms":   0.0,
        }

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=CONNECTOR_LIMIT,
                limit_per_host=CONNECTOR_LIMIT,
                force_close=False,      # keep-alive
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(
                connect=TIMEOUT_CONNECT,
                sock_read=TIMEOUT_READ,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers=_browser_headers(self.lang),
            )
        return self._session

    # ── Init session ─────────────────────────────────────────────────────────

    async def _init_session(self):
        """Visite la homepage puis /catalog pour obtenir les cookies + CSRF."""
        now = time.monotonic()
        cooldown = min(INIT_COOLDOWN * (1.5 ** self._consecutive_failures), 60.0)
        if now - self._last_init_attempt < cooldown:
            return
        self._last_init_attempt = now

        logger.info(f"[{self.key.upper()}] Init session...")
        session = self._get_session()
        try:
            async with session.get(self.base_url) as r:
                logger.debug(f"[{self.key.upper()}] GET / → {r.status}")
                if r.status not in (200, 301, 302):
                    raise aiohttp.ClientError(f"Homepage HTTP {r.status}")

            await asyncio.sleep(0.3)  # laisse les cookies s'établir (non bloquant)

            catalog_url = f"{self.base_url}/catalog?order=newest_first"
            async with session.get(
                catalog_url,
                headers={"Accept": "text/html,application/xhtml+xml,*/*"},
            ) as r:
                text = await r.text(errors="replace")
                logger.debug(f"[{self.key.upper()}] GET /catalog → {r.status}")

            self._csrf = None
            for name in ("XSRF-TOKEN", "csrf_token", "_csrf_token"):
                v = session.cookie_jar.filter_cookies(self.base_url).get(name)
                if v:
                    import urllib.parse
                    self._csrf = urllib.parse.unquote(v.value)
                    break
            if not self._csrf:
                for pat in [
                    r'"csrf_token"\s*:\s*"([^"]{20,})"',
                    r'csrf-token"\s+content="([^"]{20,})"',
                    r'"csrfToken"\s*:\s*"([^"]{20,})"',
                ]:
                    m = re.search(pat, text)
                    if m:
                        self._csrf = m.group(1)
                        break

            self._initialized = True
            self._consecutive_failures = 0
            logger.info(f"[{self.key.upper()}] ✅ Session OK (CSRF={'oui' if self._csrf else 'non'})")

        except Exception as e:
            self._consecutive_failures += 1
            self._initialized = False
            logger.error(f"[{self.key.upper()}] Init échouée #{self._consecutive_failures}: {e}")

    async def _reinit(self):
        self._initialized = False
        self._csrf = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        await self._init_session()

    # ── Fetch ─────────────────────────────────────────────────────────────────

    async def fetch_raw(self, params: dict, per_page: int = PER_PAGE_DEFAULT) -> tuple[list[dict], int]:
        """Appel API async avec retry exponentiel. Retourne (items, nb_retries)."""
        if not self._initialized:
            await self._init_session()
        if not self._initialized:
            # Session jamais établie pour ce marché (ou encore en cooldown après
            # un échec précédent) : on le compte comme une vraie erreur — sinon
            # ça remonte comme "0 items" indiscernable d'un cycle silencieusement
            # calme, et le marché a l'air juste "ne rien trouver" indéfiniment.
            logger.warning(f"[{self.key.upper()}] Session non établie — scan sauté")
            self.stats["errors"] += 1
            return [], 1

        merged = {
            **params,
            "order":    "newest_first",
            "per_page": str(per_page),
            "page":     "1",
            "time":     str(int(time.time())),
        }

        headers = _api_headers(self.base_url, self.lang, self._csrf)
        session = self._get_session()
        retries = 0

        for attempt in range(MAX_RETRY):
            t0 = time.monotonic()
            try:
                async with session.get(self.api_url, params=merged, headers=headers) as resp:
                    elapsed_ms = (time.monotonic() - t0) * 1000
                    self.stats["last_api_ms"] = elapsed_ms

                    if resp.status in (401, 403):
                        logger.warning(f"[{self.key.upper()}] Session expirée → reinit")
                        await self._reinit()
                        if self._csrf:
                            headers["X-CSRF-Token"] = self._csrf
                        retries += 1
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", "2"))
                        logger.warning(f"[{self.key.upper()}] Rate-limit → {retry_after}s")
                        retries += 1
                        await asyncio.sleep(retry_after)
                        continue

                    if resp.status != 200:
                        logger.warning(f"[{self.key.upper()}] HTTP {resp.status}")
                        return [], retries

                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        text = await resp.text()
                        if not text.strip().startswith("{"):
                            logger.warning(f"[{self.key.upper()}] Non-JSON: {text[:80]}")
                            await self._reinit()
                            retries += 1
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        data = json.loads(text)

                    items = data.get("items", [])
                    self._consecutive_failures = 0
                    return items, retries

            except asyncio.TimeoutError:
                logger.warning(f"[{self.key.upper()}] Timeout tentative {attempt+1}/{MAX_RETRY}")
                retries += 1
                await asyncio.sleep(1.0 * (attempt + 1))

            except aiohttp.ClientError as e:
                logger.warning(f"[{self.key.upper()}] ClientError tentative {attempt+1}: {e}")
                retries += 1
                await self._reinit()
                await asyncio.sleep(1.0 * (attempt + 1))

            except Exception as e:
                logger.warning(f"[{self.key.upper()}] Erreur tentative {attempt+1}: {e}")
                retries += 1
                await asyncio.sleep(1.0 * (attempt + 1))

        self._consecutive_failures += 1
        self.stats["errors"] += 1
        return [], retries

    # ── Scrape (fetch + parse + filtre) ───────────────────────────────────────

    async def scrape(
        self,
        params: dict,
        keywords: Optional[list[str]] = None,
        per_page: int = PER_PAGE_DEFAULT,
        keyword_key: str = "",
    ) -> ScrapeResult:
        """Scrape, parse et applique le filtre mots-clés. Renvoie un ScrapeResult
        avec les timings réels (api_ms / parse_ms) pour le monitoring."""
        self.stats["scans"] += 1

        t0 = time.monotonic()
        raw_items, retries = await self.fetch_raw(params, per_page=per_page)
        api_ms = (time.monotonic() - t0) * 1000

        self.stats["total_fetched"] += len(raw_items)

        t1 = time.monotonic()
        parsed = [self._parse_item(r, keyword_key) for r in raw_items]

        if keywords:
            parsed = [a for a in parsed if _kw_match(a.get("title", ""), keywords)]

        parsed.sort(key=lambda a: int(a.get("created_ts") or 0), reverse=True)
        parse_ms = (time.monotonic() - t1) * 1000

        self.stats["last_scan_ms"] = api_ms + parse_ms
        return ScrapeResult(ads=parsed, api_ms=api_ms, parse_ms=parse_ms, retries=retries, error=not raw_items and retries > 0)

    # ── Parser ────────────────────────────────────────────────────────────────

    def _parse_item(self, raw: dict, keyword: str = "") -> dict:
        item_id = str(raw.get("id", ""))

        url = raw.get("url", "")
        if url and not url.startswith("http"):
            url = self.base_url + url
        if not url and item_id:
            url = f"{self.base_url}/items/{item_id}"

        photos = raw.get("photos", [])
        image = ""
        if photos:
            p = photos[0]
            image = (
                p.get("url") or p.get("full_size_url") or
                p.get("large_url") or p.get("medium_url") or ""
            )
            if not image:
                thumbs = p.get("thumbnails", [])
                if thumbs:
                    image = thumbs[-1].get("url", "")

        price_obj = raw.get("price_numeric") or raw.get("total_item_price")
        currency  = raw.get("currency", "€")
        if isinstance(price_obj, dict):
            price = f"{price_obj.get('amount', '?')} {price_obj.get('currency_code', currency)}"
        elif price_obj is not None:
            price = f"{price_obj} {currency}"
        else:
            price = raw.get("price", "N/A")

        try:
            price_num = float(str(price_obj).replace(",", ".")) if price_obj else None
        except Exception:
            price_num = None

        created_at_ts = raw.get("created_at_ts")
        created_at    = raw.get("created_at", "")
        if created_at_ts:
            try:
                dt = datetime.fromtimestamp(int(created_at_ts), tz=timezone.utc) + timedelta(hours=2)
                created_at = dt.strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                pass

        user       = raw.get("user", {}) or {}
        seller     = user.get("login", "") or user.get("display_name", "")
        seller_url = (
            f"{self.base_url}/member/{user['id']}-{seller}"
            if user.get("id") else ""
        )

        size = raw.get("size_title", "") or (raw.get("size") or {}).get("title", "")

        return {
            "id":           f"{self.key}_{item_id}",
            "raw_id":       item_id,
            "platform":     "vinted",
            "market_key":   self.key,
            "market_label": self.label,
            "title":        raw.get("title", "Sans titre"),
            "url":          url,
            "price":        str(price),
            "price_num":    price_num,
            "image":        image,
            "date":         created_at,
            "created_ts":   int(created_at_ts) if created_at_ts else 0,
            "size":         size,
            "status":       raw.get("status", ""),
            "seller":       seller,
            "seller_url":   seller_url,
            "brand":        raw.get("brand_title", ""),
            "region":       self.region,
            "keyword":      keyword,
        }

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kw_match(title: str, keywords: list[str]) -> bool:
    """True si le titre contient au moins un mot-clé (case-insensitive)."""
    if not keywords:
        return True
    tl = title.lower()
    return any(kw.lower() in tl for kw in keywords)


# ── Instances globales ────────────────────────────────────────────────────────
_scrapers: dict[str, MarketScraper] = {
    key: MarketScraper(key, info) for key, info in MARKETS.items()
}


async def scrape_markets(
    active_keys: list[str],
    params: dict,
    keywords: Optional[list[str]] = None,
    keyword_key: str = "",
    per_page: int = PER_PAGE_DEFAULT,
) -> tuple[list[dict], dict]:
    """
    Scrape tous les marchés actifs EN PARALLÈLE (asyncio.gather).
    Renvoie (annonces, timing) où timing = {"api_ms", "parse_ms", "retries", "error",
    "per_market": {clé_marché: nb_items}} — le détail par marché permet de repérer
    un marché qui ne renvoie jamais rien pendant qu'un autre fonctionne normalement
    (sinon noyé dans le total agrégé).
    """
    if not active_keys:
        return [], {"api_ms": 0.0, "parse_ms": 0.0, "retries": 0, "error": False, "per_market": {}}

    tasks = [
        _scrapers[key].scrape(params=params, keywords=keywords, per_page=per_page, keyword_key=keyword_key)
        for key in active_keys
        if key in _scrapers
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items: list[dict] = []
    api_ms = parse_ms = 0.0
    retries = 0
    error = False
    per_market: dict[str, int] = {}

    for key, result in zip(active_keys, results):
        if isinstance(result, Exception):
            logger.error(f"[{key.upper()}] Erreur gather: {result}")
            error = True
            per_market[key] = 0
            continue
        all_items.extend(result.ads)
        api_ms = max(api_ms, result.api_ms)
        parse_ms = max(parse_ms, result.parse_ms)
        retries += result.retries
        error = error or result.error
        per_market[key] = len(result.ads)

    return all_items, {
        "api_ms": api_ms, "parse_ms": parse_ms, "retries": retries, "error": error,
        "per_market": per_market,
    }


def get_scraper_stats() -> dict:
    return {key: scraper.stats.copy() for key, scraper in _scrapers.items()}


async def close_all() -> None:
    await asyncio.gather(*(s.close() for s in _scrapers.values()), return_exceptions=True)
