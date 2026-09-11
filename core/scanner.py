"""
Scanner — moteur asyncio pur, une Task dédiée par mot-clé.

Corrections apportées vs l'ancien core/scheduler.py (voir audit) :

  - BUG CRITIQUE : `start()` créait DEUX boucles de scan par mot-clé — une via
    `asyncio.run_coroutine_threadsafe(self._keyword_loop(kt), loop)` jamais
    trackée, une seconde via une Task séparée. La première n'était jamais
    annulée par `stop()` et s'accumulait à chaque cycle démarrer/arrêter
    (requêtes Vinted doublées puis multipliées). Fixé : la création des Tasks
    se fait une seule fois, entièrement à l'intérieur du thread de la loop.
  - BUG : `run_once()` ne notifiait jamais tant que le scan auto n'avait pas
    tourné une fois (condition de warmup toujours vraie sur un dict vide).
    Fixé : état de warmup dédié au scan manuel.

Pipeline de notification (voir spec) : Vinted → Scanner → Anti-doublon →
Database → Telegram/Discord → Interface. Chaque étape est indépendante :
l'annonce est enregistrée en base même si Telegram/Discord échoue ou est
désactivé.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

import config as app_config
from core.performance import PerformanceMonitor
from database import database as db
from services import discord as discord_service
from services import telegram as telegram_service
from services.vinted import PER_PAGE_DEFAULT, PER_PAGE_WARMUP, extract_params_from_url, scrape_markets

logger = logging.getLogger(__name__)

# Plancher de sécurité absolu. En dessous de 1s on risque un 429 quasi-certain
# même avec une seule requête. La valeur recommandée reste 5-8s en prod avec
# plusieurs mots-clés/marchés ; 1s est réservé aux configs à mot-clé unique
# et marché unique sur une connexion stable.
MIN_INTERVAL_SECONDS = 1.0

OnLog = Callable[[str], None]
OnNewAd = Callable[[dict], None]
OnKeywordStatus = Callable[[str, dict], None]


def _parse_kw_list(raw: list) -> list[dict]:
    """Normalise keywords_filter : accepte strings et dicts.

    Entrée :  ["nike", {"text": "jordan", "interval": 3, "max_price": 100}]
    Sortie :  [{"text": "nike", "interval": None, "max_price": None, ...}, ...]
    """
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"text": item.strip(), "interval": None, "max_price": None,
                        "autobuy_enabled": None, "autobuy_max_price": None,
                        "brand_ids": None, "size_ids": None})
        elif isinstance(item, dict):
            out.append({
                "text": str(item.get("text", "")).strip(),
                "interval": item.get("interval"),
                "max_price": item.get("max_price"),
                "autobuy_enabled": item.get("autobuy_enabled"),
                "autobuy_max_price": item.get("autobuy_max_price"),
                "brand_ids": item.get("brand_ids"),
                "size_ids": item.get("size_ids"),
            })
    return [k for k in out if k["text"] or True]  # keep empty-string global


@dataclass
class KeywordTask:
    """État d'une tâche de surveillance par mot-clé."""
    keyword: str
    active_keys: list[str]
    params: dict
    interval: float
    kw_cfg: dict = field(default_factory=dict)
    warmup_done: bool = False
    scan_count: int = 0
    new_total: int = 0
    last_scan_ts: float = 0.0
    status: str = "starting"       # starting | scanning | error | stopped
    last_error: str = ""
    task: Optional[asyncio.Task] = field(default=None, repr=False)


class Scanner:
    def __init__(
        self,
        config_provider: Callable[[], dict],
        on_log: OnLog,
        on_new_ad: OnNewAd,
        on_keyword_status: Optional[OnKeywordStatus] = None,
        on_notification_update: Optional[Callable[[str, str, bool], None]] = None,
        perf: Optional[PerformanceMonitor] = None,
        scrape_fn=scrape_markets,
    ):
        self.config_provider = config_provider
        self.on_log = on_log
        self.on_new_ad = on_new_ad
        self.on_keyword_status = on_keyword_status or (lambda kw, status: None)
        self.on_notification_update = on_notification_update or (lambda ad_id, channel, ok: None)
        self.perf = perf or PerformanceMonitor()
        self._scrape_fn = scrape_fn

        self._running = False
        self._tasks: dict[str, KeywordTask] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._manual_warmup_done = False

        self.total_scans = 0
        self.total_new_ads = 0
        self.start_time: Optional[float] = None

    # ── Helpers config ────────────────────────────────────────────────────────

    def _get_active_keys(self, cfg: dict) -> list[str]:
        markets_cfg = cfg.get("markets", {})
        keys = [k for k, v in markets_cfg.items() if v.get("enabled", False)]
        return keys or ["fr"]

    def _build_base_params(self, cfg: dict) -> dict:
        custom_url = cfg.get("custom_url", "").strip()
        if custom_url:
            try:
                p = urlparse(custom_url)
                if "vinted." in p.netloc:
                    return extract_params_from_url(custom_url)
            except Exception as e:
                logger.warning(f"URL custom invalide: {e}")

        params: dict = {"order": "newest_first"}
        catalog_ids = cfg.get("catalog_ids", [])
        if catalog_ids:
            params["catalog_ids[]"] = catalog_ids
        brand_ids = cfg.get("brand_ids", [])
        if brand_ids:
            params["brand_ids[]"] = brand_ids
        min_price = cfg.get("min_price")
        max_price = cfg.get("max_price")
        if min_price is not None:
            params["price_from"] = str(min_price)
        if max_price is not None:
            params["price_to"] = str(max_price)
        return params

    def _notification_state(self, cfg: dict) -> tuple[bool, bool]:
        """Retourne (telegram_enabled, discord_enabled) — activé ET credentials présents."""
        tg = cfg.get("telegram", {})
        telegram_enabled = bool(
            tg.get("enabled") and app_config.get_secret("TELEGRAM_BOT_TOKEN") and app_config.get_secret("TELEGRAM_CHAT_ID")
        )
        dc = cfg.get("discord", {})
        discord_enabled = bool(dc.get("enabled") and app_config.get_secret("DISCORD_WEBHOOK_URL"))
        return telegram_enabled, discord_enabled

    async def _dispatch_one(self, ad: dict, cfg: dict, telegram_enabled: bool, discord_enabled: bool) -> None:
        """Dispatch Database + Telegram + Discord pour une seule annonce."""
        tg_cfg = cfg.get("telegram", {})
        ad_id = ad.get("id", "")
        db.record_listing(
            ad,
            telegram_status="pending" if telegram_enabled else "disabled",
            discord_status="pending" if discord_enabled else "disabled",
        )
        self.on_new_ad(ad)

        coros = []
        if telegram_enabled:
            bot_token = app_config.get_secret("TELEGRAM_BOT_TOKEN")
            chat_id = app_config.get_secret("TELEGRAM_CHAT_ID")

            def _tg_done(ok: bool, error: str = "", aid: str = ad_id, title: str = ad.get("title", "")) -> None:
                db.update_telegram_status(aid, "sent" if ok else "failed", attempts=1)
                self.on_notification_update(aid, "telegram", ok)
                if not ok:
                    self.on_log(f"❌ Telegram — envoi échoué pour « {title[:40]} » : {error}")

            coros.append(telegram_service.send_ad_nowait(
                bot_token, chat_id, ad,
                send_image=tg_cfg.get("send_images", True),
                on_result=_tg_done,
            ))

        if discord_enabled:
            webhook_url = app_config.get_secret("DISCORD_WEBHOOK_URL")

            def _dc_done(ok: bool, aid: str = ad_id) -> None:
                db.update_discord_status(aid, "sent" if ok else "failed")
                self.on_notification_update(aid, "discord", ok)

            coros.append(discord_service.send_ad_nowait(webhook_url, ad, on_result=_dc_done))

        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

    async def _dispatch_new_ads(self, new_ads: list[dict], cfg: dict) -> None:
        """Anti-doublon déjà fait par l'appelant. Dispatch parallèle : Database → Telegram/Discord → Interface."""
        telegram_enabled, discord_enabled = self._notification_state(cfg)
        await asyncio.gather(
            *[self._dispatch_one(ad, cfg, telegram_enabled, discord_enabled) for ad in new_ads],
            return_exceptions=True,
        )

    def _market_breakdown(self, timing: dict) -> str:
        """' (FR: 10, UK: 0)' quand plusieurs marchés sont actifs — vide sinon,
        pour ne pas alourdir le log dans le cas courant à un seul marché.
        Sert à repérer immédiatement un marché qui ne renvoie jamais rien,
        masqué autrement par le total agrégé."""
        per_market = timing.get("per_market") or {}
        if len(per_market) < 2:
            return ""
        parts = ", ".join(f"{k.upper()}: {v}" for k, v in per_market.items())
        return f" ({parts})"

    def _emit_status(self, kt: KeywordTask) -> None:
        self.on_keyword_status(kt.keyword, {
            "status": kt.status,
            "scan_count": kt.scan_count,
            "new_total": kt.new_total,
            "last_error": kt.last_error,
            "warmup_done": kt.warmup_done,
        })

    # ── Boucle scan d'un mot-clé ──────────────────────────────────────────────

    async def _keyword_loop(self, kt: KeywordTask) -> None:
        label = f"[{kt.keyword or 'GLOBAL'}]"
        logger.info(f"Tâche démarrée {label}")
        consecutive_errors = 0

        while True:
            try:
                cfg = self.config_provider()
                kt.active_keys = self._get_active_keys(cfg)
                kt.params = self._build_base_params(cfg)

                # Intervalle : per-keyword > global, plancher absolu MIN_INTERVAL_SECONDS
                global_interval = max(MIN_INTERVAL_SECONDS, float(cfg.get("interval_seconds", 10)))
                kw_interval = kt.kw_cfg.get("interval")
                kt.interval = max(MIN_INTERVAL_SECONDS, float(kw_interval)) if kw_interval is not None else global_interval

                params = dict(kt.params)
                if kt.keyword:
                    params["search_text"] = kt.keyword

                # Filtres per-keyword (surcharge les filtres globaux)
                kw_max_price = kt.kw_cfg.get("max_price")
                if kw_max_price is not None:
                    params["price_to"] = str(kw_max_price)
                kw_brand_ids = kt.kw_cfg.get("brand_ids")
                if kw_brand_ids:
                    params["brand_ids[]"] = kw_brand_ids
                kw_size_ids = kt.kw_cfg.get("size_ids")
                if kw_size_ids:
                    params["size_ids[]"] = kw_size_ids

                ads, timing = await self._scrape_fn(
                    active_keys=kt.active_keys,
                    params=params,
                    keyword_key=kt.keyword,
                    per_page=PER_PAGE_WARMUP if not kt.warmup_done else PER_PAGE_DEFAULT,
                )

                kt.scan_count += 1
                kt.last_scan_ts = time.time()
                self.total_scans += 1

                if timing.get("error") and not ads:
                    consecutive_errors += 1
                    backoff = min(5.0 * consecutive_errors, 60.0)
                    kt.status, kt.last_error = "error", "requête Vinted échouée"
                    self.on_log(f"⚠️  {label} Requête Vinted échouée — retry {consecutive_errors} dans {backoff:.0f}s")
                    self._emit_status(kt)
                    self.perf.record_scan(
                        api_ms=timing["api_ms"], parse_ms=timing["parse_ms"], process_ms=0.0,
                        error=True, retries=timing.get("retries", 0),
                    )
                    await asyncio.sleep(backoff)
                    continue

                consecutive_errors = 0

                if not ads:
                    kt.status = "scanning"
                    self._emit_status(kt)
                    self.perf.record_scan(
                        api_ms=timing["api_ms"], parse_ms=timing["parse_ms"], process_ms=0.0,
                        retries=timing.get("retries", 0),
                    )
                    # 0 item brut (pas juste 0 nouvelle) : le vrai rate-limit (429) a déjà
                    # son propre backoff dans fetch_raw (Retry-After) — doubler l'attente
                    # ici en plus était redondant et ralentissait la reprise d'un marché
                    # qui recommence à répondre normalement, à l'encontre de la vitesse.
                    await asyncio.sleep(kt.interval)
                    continue

                t_process0 = time.monotonic()

                if not kt.warmup_done:
                    db.mark_all_seen(ads)
                    kt.warmup_done = True
                    process_ms = (time.monotonic() - t_process0) * 1000
                    self.on_log(
                        f"🔥 {label} Warmup — {len(ads)} annonces mémorisées"
                        f"{self._market_breakdown(timing)}. Surveillance active."
                    )
                    kt.status = "scanning"
                    self._emit_status(kt)
                    self.perf.record_scan(
                        api_ms=timing["api_ms"], parse_ms=timing["parse_ms"], process_ms=process_ms,
                        fetched=len(ads), retries=timing.get("retries", 0),
                    )
                    await asyncio.sleep(kt.interval)
                    continue

                new_ads = db.filter_new(ads)

                if len(new_ads) == len(ads) and len(ads) >= PER_PAGE_DEFAULT:
                    self.on_log(
                        f"⚠️  {label} {len(ads)}/{len(ads)} annonces reçues étaient nouvelles — "
                        f"l'intervalle est peut-être trop long, des annonces ont pu être manquées."
                    )

                self.on_log(
                    f"🔍 {label} Scan #{kt.scan_count} — {len(ads)} items"
                    f"{self._market_breakdown(timing)}, "
                    f"{len(new_ads)} nouvelle(s) ({timing['api_ms']:.0f}ms API)"
                )

                if new_ads:
                    new_ads.sort(key=lambda a: int(a.get("created_ts") or 0))
                    kt.new_total += len(new_ads)
                    self.total_new_ads += len(new_ads)
                    await self._dispatch_new_ads(new_ads, cfg)

                process_ms = (time.monotonic() - t_process0) * 1000
                self.perf.record_scan(
                    api_ms=timing["api_ms"], parse_ms=timing["parse_ms"], process_ms=process_ms,
                    fetched=len(ads), new_count=len(new_ads), retries=timing.get("retries", 0),
                )

                kt.status = "scanning"
                self._emit_status(kt)

                # Intervalle adaptatif : si nouvelles annonces trouvées, rescan
                # plus tôt pour ne pas rater la prochaine vague (moitié de
                # l'intervalle, plancher MIN_INTERVAL_SECONDS).
                if new_ads:
                    sleep_time = max(MIN_INTERVAL_SECONDS, kt.interval / 2)
                else:
                    sleep_time = kt.interval
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                logger.info(f"Tâche annulée {label}")
                kt.status = "stopped"
                self._emit_status(kt)
                break
            except Exception as e:
                consecutive_errors += 1
                backoff = min(5.0 * consecutive_errors, 60.0)
                kt.status, kt.last_error = "error", str(e)
                logger.error(f"{label} Erreur cycle (#{consecutive_errors}): {e}")
                self.on_log(f"❌ {label} Erreur: {e} — retry dans {backoff:.0f}s")
                self._emit_status(kt)
                self.perf.record_error()
                await asyncio.sleep(backoff)

    # ── Retry Telegram (annonces enregistrées mais jamais envoyées) ──────────

    async def telegram_retry_loop(self, interval_seconds: float = 120.0) -> None:
        """Si Telegram est indisponible, l'annonce reste enregistrée avec le
        statut 'failed'. Cette tâche retente l'envoi périodiquement quand
        Telegram est réactivé/reviens en ligne, sans jamais bloquer le scan."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                cfg = self.config_provider()
                telegram_enabled, _ = self._notification_state(cfg)
                if not telegram_enabled:
                    continue

                failed = await db.get_failed_telegram()
                if not failed:
                    continue

                bot_token = app_config.get_secret("TELEGRAM_BOT_TOKEN")
                chat_id = app_config.get_secret("TELEGRAM_CHAT_ID")
                send_images = cfg.get("telegram", {}).get("send_images", True)

                self.on_log(f"↻ Nouvelle tentative Telegram pour {len(failed)} annonce(s) en échec")
                for row in failed:
                    ad_id = row["id"]
                    attempts = int(row.get("telegram_attempts") or 0) + 1
                    ok, error = await telegram_service.send_ad(bot_token, chat_id, row, send_image=send_images)
                    db.update_telegram_status(ad_id, "sent" if ok else "failed", attempts=attempts)
                    self.on_notification_update(ad_id, "telegram", ok)
                    if not ok:
                        self.on_log(f"❌ Telegram — nouvelle tentative échouée pour « {row.get('title', '')[:40]} » : {error}")
            except Exception as e:
                logger.debug(f"[scanner] telegram_retry_loop error: {e}")

    # ── Planification thread-safe ─────────────────────────────────────────────

    def _schedule_coro(self, coro, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Planifie une coroutine dans la loop cible depuis n'importe quel contexte.

        - Depuis la loop elle-même (FastAPI, callbacks asyncio) → create_task
        - Depuis un thread étranger (ancienne GUI Tkinter, tests) → run_coroutine_threadsafe
        """
        target = loop or self._loop
        if not target or target.is_closed():
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
            if running is target:
                target.create_task(coro)
                return
        except RuntimeError:
            pass
        asyncio.run_coroutine_threadsafe(coro, target)

    # ── API publique ──────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def run_once(self) -> None:
        """Scan manuel unique. Fonctionne depuis la loop asyncio ou un thread étranger."""
        if self._loop and not self._loop.is_closed():
            self._schedule_coro(self._run_once_async())
        else:
            logger.warning("Loop asyncio non disponible pour run_once")

    async def _run_once_async(self) -> None:
        cfg = self.config_provider()
        active_keys = self._get_active_keys(cfg)
        params = self._build_base_params(cfg)
        kw_cfgs = _parse_kw_list(cfg.get("keywords_filter", []))
        kws_to_run = [k["text"] for k in kw_cfgs] if kw_cfgs else [""]

        t0 = time.monotonic()
        tasks = []
        for kw in kws_to_run:
            p = dict(params)
            if kw:
                p["search_text"] = kw
            tasks.append(self._scrape_fn(active_keys=active_keys, params=p, keyword_key=kw, per_page=PER_PAGE_DEFAULT))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_ads: list[dict] = []
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"Scan manuel erreur: {res}")
            else:
                ads, _timing = res
                all_ads.extend(ads)

        elapsed = (time.monotonic() - t0) * 1000
        self.on_log(f"🔎 Scan manuel — {len(all_ads)} annonces ({elapsed:.0f}ms)")

        # Warmup dédié au scan manuel : indépendant de _tasks (fix du bug où
        # "SCAN MAINTENANT" ne notifiait jamais tant que l'auto-scan n'avait
        # pas tourné une fois, car la condition portait sur un dict vide).
        already_warm = self._manual_warmup_done or any(kt.warmup_done for kt in self._tasks.values())
        if cfg.get("warmup_first_run", True) and not already_warm:
            db.mark_all_seen(all_ads)
            self._manual_warmup_done = True
            self.on_log(f"🔥 Warmup manuel: {len(all_ads)} annonces mémorisées")
            return

        self._manual_warmup_done = True
        new_ads = db.filter_new(all_ads)
        if not new_ads:
            self.on_log("✅ Aucune nouvelle annonce.")
            return

        new_ads.sort(key=lambda a: int(a.get("created_ts") or 0))
        await self._dispatch_new_ads(new_ads, cfg)
        self.on_log(f"🆕 {len(new_ads)} nouvelle(s) annonce(s) trouvée(s)")

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Démarre le scan automatique. `loop` : la loop asyncio du thread dédié."""
        if self._running:
            return
        self._running = True
        self._loop = loop
        self.start_time = time.time()

        cfg = self.config_provider()
        active_keys = self._get_active_keys(cfg)
        params = self._build_base_params(cfg)
        interval = max(MIN_INTERVAL_SECONDS, float(cfg.get("interval_seconds", 10)))
        kw_cfgs = _parse_kw_list(cfg.get("keywords_filter", []))
        kws_to_watch = kw_cfgs if kw_cfgs else [{"text": "", "interval": None, "max_price": None,
                                                   "autobuy_enabled": None, "autobuy_max_price": None,
                                                   "brand_ids": None, "size_ids": None}]

        # Toute la création de Task se fait à l'intérieur de la loop asyncio,
        # en une seule fois — c'est le fix du bug de double-scan (voir docstring).
        self._schedule_coro(self._start_tasks(kws_to_watch, active_keys, params, interval), loop)

        labels = ", ".join(active_keys)
        kw_texts = [k["text"] for k in kws_to_watch]
        kw_info = f" — mots-clés: {', '.join(kw_texts)}" if any(kw_texts) else ""
        self.on_log(f"▶️  Scan démarré ({labels}){kw_info} — interval {interval:.0f}s")

    async def _start_tasks(self, kws_to_watch: list[dict], active_keys: list[str], params: dict, interval: float) -> None:
        for kw_cfg in kws_to_watch:
            kw = kw_cfg["text"] if isinstance(kw_cfg, dict) else kw_cfg
            if kw in self._tasks:
                continue
            cfg_entry = kw_cfg if isinstance(kw_cfg, dict) else {}
            kt = KeywordTask(keyword=kw, active_keys=list(active_keys), params=dict(params),
                             interval=interval, kw_cfg=cfg_entry)
            kt.task = asyncio.create_task(self._keyword_loop(kt), name=f"scan-{kw or 'global'}")
            self._tasks[kw] = kt

    def sync_keywords(self) -> None:
        """Ajoute/retire des tâches mot-clé à chaud pendant que le scan tourne,
        pour ne pas avoir à tout arrêter/redémarrer juste pour ajouter un mot-clé."""
        if not self._running or not self._loop or self._loop.is_closed():
            return
        cfg = self.config_provider()
        kw_cfgs = _parse_kw_list(cfg.get("keywords_filter", []))
        desired_map = {k["text"]: k for k in kw_cfgs} if kw_cfgs else {"": {"text": "", "interval": None,
                                                                              "max_price": None, "autobuy_enabled": None,
                                                                              "autobuy_max_price": None,
                                                                              "brand_ids": None, "size_ids": None}}
        active_keys = self._get_active_keys(cfg)
        params = self._build_base_params(cfg)
        interval = max(MIN_INTERVAL_SECONDS, float(cfg.get("interval_seconds", 10)))
        self._schedule_coro(self._sync_tasks(desired_map, active_keys, params, interval))

    async def _sync_tasks(self, desired_map: dict[str, dict], active_keys: list[str], params: dict, interval: float) -> None:
        for kw in list(self._tasks.keys()):
            if kw not in desired_map:
                kt = self._tasks.pop(kw)
                if kt.task and not kt.task.done():
                    kt.task.cancel()
                self.on_log(f"⏹️  [{kw or 'GLOBAL'}] Surveillance arrêtée")
            else:
                # Met à jour la config per-keyword à chaud si elle a changé
                self._tasks[kw].kw_cfg = desired_map[kw]
        new_kws = [cfg for text, cfg in desired_map.items() if text not in self._tasks]
        if new_kws:
            await self._start_tasks(new_kws, active_keys, params, interval)
            new_texts = [k["text"] for k in new_kws]
            self.on_log(f"▶️  Nouveau(x) mot(s)-clé(s) surveillé(s): {', '.join(new_texts)}")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._loop and not self._loop.is_closed():
            for kt in self._tasks.values():
                if kt.task and not kt.task.done():
                    self._loop.call_soon_threadsafe(kt.task.cancel)

        self._tasks.clear()
        self.start_time = None
        self.on_log("⏹️  Scan arrêté.")

    def get_stats(self) -> dict:
        return {
            "total_scans": self.total_scans,
            "total_new_ads": self.total_new_ads,
            "running": self._running,
            "start_time": self.start_time,
            "keywords": {
                kw: {
                    "status": kt.status,
                    "scan_count": kt.scan_count,
                    "new_total": kt.new_total,
                    "warmup_done": kt.warmup_done,
                    "last_error": kt.last_error,
                }
                for kw, kt in self._tasks.items()
            },
        }
