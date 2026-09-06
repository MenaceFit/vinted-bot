"""Vue Telegram — configuration, activation, test de connexion."""
import tkinter as tk

import config as app_config
from gui import theme
from services import telegram as telegram_service


class TelegramView(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=theme.BG)
        self.app = app
        self._build()

    def _build(self) -> None:
        tk.Label(self, text="📱 TELEGRAM", font=theme.FONT_H2, bg=theme.BG, fg=theme.TEXT).pack(anchor="w", pady=(0, 12))

        panel = tk.Frame(self, bg=theme.PANEL, padx=20, pady=20)
        panel.pack(fill="x")

        tk.Label(panel, text="Bot Token", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w")
        tk.Label(panel, text="Obtenu via @BotFather sur Telegram", font=theme.FONT_SMALL,
                 bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w")
        token_row = tk.Frame(panel, bg=theme.PANEL)
        token_row.pack(fill="x", pady=(4, 14))
        self._token_entry = tk.Entry(token_row, show="•", bg=theme.PANEL2, fg=theme.TEXT,
                                      insertbackground=theme.TEXT, relief="flat")
        self._token_entry.insert(0, app_config.get_secret("TELEGRAM_BOT_TOKEN"))
        self._token_entry.pack(side="left", fill="x", expand=True, ipady=5)
        theme.button(token_row, "👁", lambda: self._toggle_visibility(self._token_entry),
                     bg=theme.PANEL2, fg=theme.TEXT2, padx=8).pack(side="left", padx=(6, 0))

        tk.Label(panel, text="Chat ID", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w")
        tk.Label(panel, text="Obtenu via @userinfobot, ou l'API getUpdates après un /start au bot", font=theme.FONT_SMALL,
                 bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w")
        chat_row = tk.Frame(panel, bg=theme.PANEL)
        chat_row.pack(fill="x", pady=(4, 16))
        self._chat_entry = tk.Entry(chat_row, show="•", bg=theme.PANEL2, fg=theme.TEXT,
                                     insertbackground=theme.TEXT, relief="flat")
        self._chat_entry.insert(0, app_config.get_secret("TELEGRAM_CHAT_ID"))
        self._chat_entry.pack(side="left", fill="x", expand=True, ipady=5)
        theme.button(chat_row, "👁", lambda: self._toggle_visibility(self._chat_entry),
                     bg=theme.PANEL2, fg=theme.TEXT2, padx=8).pack(side="left", padx=(6, 0))

        tg_cfg = self.app.config_data.setdefault("telegram", {})
        self._send_new_var = tk.BooleanVar(value=tg_cfg.get("send_new", True))
        self._send_images_var = tk.BooleanVar(value=tg_cfg.get("send_images", True))
        self._sound_var = tk.BooleanVar(value=tg_cfg.get("sound_enabled", False))
        for text, var in [
            ("Envoyer les nouvelles annonces", self._send_new_var),
            ("Envoyer les images", self._send_images_var),
            ("Son des notifications", self._sound_var),
        ]:
            tk.Checkbutton(
                panel, text=text, variable=var, bg=theme.PANEL, fg=theme.TEXT,
                selectcolor=theme.PANEL2, activebackground=theme.PANEL, activeforeground=theme.TEXT,
                font=theme.FONT_BODY,
            ).pack(anchor="w", pady=2)

        btn_row = tk.Frame(panel, bg=theme.PANEL)
        btn_row.pack(fill="x", pady=(18, 0))
        theme.button(btn_row, "🟢 Activer Telegram", self._enable, bg=theme.GREEN).pack(fill="x", pady=(0, 4))
        theme.button(btn_row, "🔴 Désactiver Telegram", self._disable, bg=theme.PANEL2, fg=theme.RED).pack(fill="x", pady=(0, 4))
        theme.button(btn_row, "🧪 Tester la connexion", self._test, bg=theme.PANEL2, fg=theme.ACCENT).pack(fill="x", pady=(0, 4))
        theme.button(btn_row, "💾 Sauvegarder", self._save, bg=theme.ACCENT).pack(fill="x")

        self._result_lbl = tk.Label(panel, text="", font=theme.FONT_BODY, bg=theme.PANEL, fg=theme.TEXT2,
                                     wraplength=420, justify="left")
        self._result_lbl.pack(anchor="w", pady=(14, 0))

        self._status_lbl = tk.Label(self, text="", font=theme.FONT_BOLD, bg=theme.BG, fg=theme.TEXT2)
        self._status_lbl.pack(anchor="w", pady=(10, 0))
        self._refresh_enabled_label()

    def on_show(self) -> None:
        pass

    # ── Actions ───────────────────────────────────────────────────────────────

    def _toggle_visibility(self, entry: tk.Entry) -> None:
        entry.config(show="" if entry.cget("show") else "•")

    def _save(self, silent: bool = False) -> None:
        token = self._token_entry.get().strip()
        chat_id = self._chat_entry.get().strip()
        if token:
            app_config.set_secret("TELEGRAM_BOT_TOKEN", token)
        if chat_id:
            app_config.set_secret("TELEGRAM_CHAT_ID", chat_id)

        tg_cfg = self.app.config_data.setdefault("telegram", {})
        tg_cfg["send_new"] = self._send_new_var.get()
        tg_cfg["send_images"] = self._send_images_var.get()
        tg_cfg["sound_enabled"] = self._sound_var.get()
        self.app.save_config()

        if not silent:
            self._set_result("💾 Paramètres Telegram sauvegardés.", theme.TEXT2)
            self.app._on_log("💾 Paramètres Telegram sauvegardés.")

    def _enable(self) -> None:
        if not self._token_entry.get().strip() or not self._chat_entry.get().strip():
            self._set_result("⚠️ Renseigne le Bot Token et le Chat ID avant d'activer.", theme.YELLOW)
            return
        self._save(silent=True)
        self.app.config_data["telegram"]["enabled"] = True
        self.app.save_config()
        self._refresh_enabled_label()
        self.app._on_log("📱 Telegram activé")

    def _disable(self) -> None:
        self.app.config_data.setdefault("telegram", {})["enabled"] = False
        self.app.save_config()
        self._refresh_enabled_label()
        self.app._on_log("📱 Telegram désactivé")

    def _test(self) -> None:
        token = self._token_entry.get().strip()
        chat_id = self._chat_entry.get().strip()
        if not token or not chat_id:
            self._set_result("⚠️ Renseigne le Bot Token et le Chat ID.", theme.YELLOW)
            return
        self._set_result("⏳ Test en cours...", theme.TEXT2)
        self.app.run_async(telegram_service.test_connection(token, chat_id), self._on_test_result)

    def _on_test_result(self, result) -> None:
        if isinstance(result, Exception):
            self._set_result(f"🔴 Échec de connexion — {result}", theme.RED)
            return
        ok, message = result
        prefix = "🟢 " if ok else "🔴 "
        self._set_result(prefix + message, theme.GREEN if ok else theme.RED)

    def _refresh_enabled_label(self) -> None:
        enabled = self.app.config_data.get("telegram", {}).get("enabled", False)
        self._status_lbl.config(
            text="🟢 Telegram opérationnel (activé)" if enabled else "🔴 Telegram désactivé",
            fg=theme.GREEN if enabled else theme.RED,
        )

    def _set_result(self, text: str, color: str) -> None:
        self._result_lbl.config(text=text, fg=color)
