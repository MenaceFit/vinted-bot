"""Vue Dashboard — statistiques temps réel."""
from datetime import datetime

import tkinter as tk

import config as app_config
from gui import theme
from services import telegram as telegram_service


class DashboardView(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=theme.BG)
        self.app = app
        self._tiles: dict[str, tk.Label] = {}
        self._perf_vars: dict[str, tk.StringVar] = {}
        self._reliability_vars: dict[str, tk.StringVar] = {}
        self._build()

    def _build(self) -> None:
        tk.Label(self, text="📊 DASHBOARD", font=theme.FONT_H2, bg=theme.BG, fg=theme.TEXT).pack(anchor="w", pady=(0, 12))

        grid = tk.Frame(self, bg=theme.BG)
        grid.pack(fill="x")
        for i in range(3):
            grid.grid_columnconfigure(i, weight=1, uniform="tile")

        specs = [
            ("total_ads", "ANNONCES DÉTECTÉES", "0"),
            ("ads_per_hour", "ANNONCES / HEURE", "0"),
            ("scanners", "SCANNERS ACTIFS", "0"),
            ("avg_scan", "TEMPS MOYEN DE SCAN", "—"),
            ("avg_latency", "LATENCE MOYENNE", "—"),
            ("telegram", "TELEGRAM", "⚪ INACTIF"),
        ]
        for idx, (key, label, default) in enumerate(specs):
            r, c = divmod(idx, 3)
            card = tk.Frame(grid, bg=theme.CARD_BG, padx=14, pady=14)
            card.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
            tk.Label(card, text=label, font=theme.FONT_SMALL, bg=theme.CARD_BG, fg=theme.TEXT2).pack(anchor="w")
            val = tk.Label(card, text=default, font=theme.FONT_STAT, bg=theme.CARD_BG, fg=theme.TEXT)
            val.pack(anchor="w", pady=(4, 0))
            self._tiles[key] = val

        perf = tk.Frame(self, bg=theme.PANEL)
        perf.pack(fill="x", pady=(16, 8))
        tk.Label(perf, text="⚡ PERFORMANCE", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w", padx=12, pady=(10, 4))
        for key, label in [("api_ms", "API"), ("parse_ms", "Parsing"), ("process_ms", "Processing"), ("total_ms", "TOTAL")]:
            row = tk.Frame(perf, bg=theme.PANEL)
            row.pack(fill="x", padx=12, pady=2)
            tk.Label(row, text=label, font=theme.FONT_BODY, bg=theme.PANEL, fg=theme.TEXT2, width=14, anchor="w").pack(side="left")
            var = tk.StringVar(value="—")
            tk.Label(row, textvariable=var, font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.ACCENT).pack(side="left")
            self._perf_vars[key] = var
        tk.Frame(perf, bg=theme.PANEL, height=6).pack()

        rel = tk.Frame(self, bg=theme.PANEL)
        rel.pack(fill="x")
        tk.Label(rel, text="🛡️ FIABILITÉ", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w", padx=12, pady=(10, 4))
        rel_row = tk.Frame(rel, bg=theme.PANEL)
        rel_row.pack(fill="x", padx=12, pady=(0, 10))
        for key, label in [
            ("requests", "Requêtes"), ("errors", "Erreurs"), ("retries", "Retries"),
            ("latency_min_ms", "Latence min"), ("latency_max_ms", "Latence max"),
        ]:
            cell = tk.Frame(rel_row, bg=theme.PANEL)
            cell.pack(side="left", expand=True, fill="x")
            tk.Label(cell, text=label, font=theme.FONT_SMALL, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w")
            var = tk.StringVar(value="—")
            tk.Label(cell, textvariable=var, font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT).pack(anchor="w")
            self._reliability_vars[key] = var

    def on_show(self) -> None:
        pass

    def refresh(self, summary: dict, ad_count: int, start_time, scanner) -> None:
        self._tiles["total_ads"].config(text=str(ad_count))

        if start_time:
            elapsed_hours = (datetime.now() - start_time).total_seconds() / 3600
            aph = round(scanner.total_new_ads / elapsed_hours, 1) if elapsed_hours > 0 else 0
        else:
            aph = 0
        self._tiles["ads_per_hour"].config(text=str(aph))

        has_scans = summary["scans"] > 0
        self._tiles["avg_scan"].config(text=f"{summary['total_ms']/1000:.1f} s" if has_scans else "—")
        self._tiles["avg_latency"].config(text=f"{summary['latency_avg_ms']/1000:.1f} s" if has_scans else "—")

        tg_cfg = self.app.config_data.get("telegram", {})
        tg_configured = bool(tg_cfg.get("enabled") and app_config.get_secret("TELEGRAM_BOT_TOKEN"))
        tg_status = telegram_service.is_connected()
        if not tg_configured:
            text, color = "⚪ INACTIF", theme.TEXT2
        elif tg_status is None:
            text, color = "⚪ EN ATTENTE", theme.TEXT2
        elif tg_status:
            text, color = "🟢 CONNECTÉ", theme.GREEN
        else:
            text, color = "🔴 ERREUR", theme.RED
        self._tiles["telegram"].config(text=text, fg=color)

        stats = scanner.get_stats()
        active = sum(1 for s in stats["keywords"].values() if s["status"] == "scanning")
        self._tiles["scanners"].config(text=str(active))

        for key in ("api_ms", "parse_ms", "process_ms", "total_ms"):
            self._perf_vars[key].set(f"{summary[key]/1000:.2f} s" if has_scans else "—")

        self._reliability_vars["requests"].set(str(summary["requests"]))
        self._reliability_vars["errors"].set(str(summary["errors"]))
        self._reliability_vars["retries"].set(str(summary["retries"]))
        self._reliability_vars["latency_min_ms"].set(f"{summary['latency_min_ms']/1000:.2f} s" if has_scans else "—")
        self._reliability_vars["latency_max_ms"].set(f"{summary['latency_max_ms']/1000:.2f} s" if has_scans else "—")
