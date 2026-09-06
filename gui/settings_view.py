"""Vue Settings — filtres, URL personnalisée, Discord (legacy), reset."""
import tkinter as tk
from tkinter import messagebox

import config as app_config
from database import database as db
from gui import theme


class SettingsView(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=theme.BG)
        self.app = app
        self._build()

    def _build(self) -> None:
        tk.Label(self, text="⚙️ SETTINGS", font=theme.FONT_H2, bg=theme.BG, fg=theme.TEXT).pack(anchor="w", pady=(0, 12))

        cfg = self.app.config_data

        filters = tk.Frame(self, bg=theme.PANEL, padx=16, pady=16)
        filters.pack(fill="x", pady=(0, 12))
        tk.Label(filters, text="Filtres de recherche", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w")

        price_row = tk.Frame(filters, bg=theme.PANEL)
        price_row.pack(fill="x", pady=(8, 10))
        tk.Label(price_row, text="Prix min", bg=theme.PANEL, fg=theme.TEXT2, font=theme.FONT_BODY).pack(side="left")
        self._min_price = tk.Entry(price_row, width=8, bg=theme.PANEL2, fg=theme.TEXT, insertbackground=theme.TEXT, relief="flat")
        v = cfg.get("min_price")
        self._min_price.insert(0, str(v) if v is not None else "")
        self._min_price.pack(side="left", padx=(6, 16), ipady=4)
        tk.Label(price_row, text="Prix max", bg=theme.PANEL, fg=theme.TEXT2, font=theme.FONT_BODY).pack(side="left")
        self._max_price = tk.Entry(price_row, width=8, bg=theme.PANEL2, fg=theme.TEXT, insertbackground=theme.TEXT, relief="flat")
        v2 = cfg.get("max_price")
        self._max_price.insert(0, str(v2) if v2 is not None else "")
        self._max_price.pack(side="left", padx=6, ipady=4)

        tk.Label(filters, text="URL Vinted personnalisée (remplace catalogue/marque/prix ci-dessus)",
                 bg=theme.PANEL, fg=theme.TEXT2, font=theme.FONT_SMALL, wraplength=500, justify="left").pack(anchor="w")
        self._custom_url = tk.Text(filters, height=2, bg=theme.PANEL2, fg=theme.TEXT, insertbackground=theme.TEXT,
                                    relief="flat", font=theme.FONT_SMALL, wrap="word")
        self._custom_url.insert("1.0", cfg.get("custom_url", ""))
        self._custom_url.pack(fill="x", pady=(4, 10))

        self._warmup_var = tk.BooleanVar(value=cfg.get("warmup_first_run", True))
        tk.Checkbutton(
            filters, text="Warmup au premier scan (ne pas notifier les annonces déjà en ligne)",
            variable=self._warmup_var, bg=theme.PANEL, fg=theme.TEXT, selectcolor=theme.PANEL2,
            activebackground=theme.PANEL, activeforeground=theme.TEXT, font=theme.FONT_BODY,
        ).pack(anchor="w")

        theme.button(filters, "💾 Sauvegarder", self._save, bg=theme.ACCENT).pack(fill="x", pady=(14, 0))

        discord = tk.Frame(self, bg=theme.PANEL, padx=16, pady=16)
        discord.pack(fill="x", pady=(0, 12))
        tk.Label(discord, text="Discord (optionnel)", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w")
        tk.Label(discord, text="Canal de notification historique, conservé en plus de Telegram.",
                 font=theme.FONT_SMALL, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w")

        webhook_row = tk.Frame(discord, bg=theme.PANEL)
        webhook_row.pack(fill="x", pady=(8, 8))
        tk.Label(webhook_row, text="Webhook URL", bg=theme.PANEL, fg=theme.TEXT2, font=theme.FONT_BODY, width=12, anchor="w").pack(side="left")
        self._webhook_entry = tk.Entry(webhook_row, show="•", bg=theme.PANEL2, fg=theme.TEXT,
                                        insertbackground=theme.TEXT, relief="flat")
        self._webhook_entry.insert(0, app_config.get_secret("DISCORD_WEBHOOK_URL"))
        self._webhook_entry.pack(side="left", fill="x", expand=True, ipady=4)
        theme.button(webhook_row, "👁", lambda: self._toggle_visibility(self._webhook_entry),
                     bg=theme.PANEL2, fg=theme.TEXT2, padx=8).pack(side="left", padx=(6, 0))

        self._discord_enabled_var = tk.BooleanVar(value=cfg.get("discord", {}).get("enabled", False))
        tk.Checkbutton(
            discord, text="Activer Discord", variable=self._discord_enabled_var, bg=theme.PANEL, fg=theme.TEXT,
            selectcolor=theme.PANEL2, activebackground=theme.PANEL, activeforeground=theme.TEXT, font=theme.FONT_BODY,
        ).pack(anchor="w")

        theme.button(discord, "💾 Sauvegarder", self._save_discord, bg=theme.ACCENT).pack(fill="x", pady=(12, 0))

        danger = tk.Frame(self, bg=theme.PANEL, padx=16, pady=16)
        danger.pack(fill="x")
        tk.Label(danger, text="Zone sensible", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.RED).pack(anchor="w")
        theme.button(
            danger, "🗑️ Réinitialiser tout l'historique (dédup + annonces)", self._reset_history,
            bg=theme.PANEL2, fg=theme.RED,
        ).pack(fill="x", pady=(8, 0))

    def on_show(self) -> None:
        pass

    def _toggle_visibility(self, entry: tk.Entry) -> None:
        entry.config(show="" if entry.cget("show") else "•")

    def _save(self) -> None:
        cfg = self.app.config_data

        def _parse_price(entry: tk.Entry):
            v = entry.get().strip()
            try:
                return float(v) if v else None
            except ValueError:
                return None

        cfg["min_price"] = _parse_price(self._min_price)
        cfg["max_price"] = _parse_price(self._max_price)
        cfg["custom_url"] = self._custom_url.get("1.0", "end").strip()
        cfg["warmup_first_run"] = self._warmup_var.get()
        self.app.save_config()
        self.app._on_log("💾 Filtres sauvegardés.")

    def _save_discord(self) -> None:
        webhook = self._webhook_entry.get().strip()
        if webhook:
            app_config.set_secret("DISCORD_WEBHOOK_URL", webhook)
        self.app.config_data.setdefault("discord", {})["enabled"] = self._discord_enabled_var.get()
        self.app.save_config()
        self.app._on_log("💾 Paramètres Discord sauvegardés.")

    def _reset_history(self) -> None:
        if messagebox.askyesno("Réinitialiser", "Effacer tout l'historique (annonces + anti-doublons) ?\nCette action est irréversible."):
            db.reset_all()
            self.app.listings_view.clear_display()
            self.app._on_log("🗑️ Historique effacé.")
