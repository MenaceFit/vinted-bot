"""
Fenêtre principale — sidebar + zone de contenu commutable + panneau de logs.

Architecture (voir README) :
    GUI (ce module, thread Tkinter)
      │  ── after(0, ...) ──────────────► mutations Tkinter thread-safe
      ▼
    Scanner Engine (thread asyncio dédié)
      ├── Database (SQLite)
      ├── Telegram / Discord (services async)
      └── Logging
"""
import asyncio
import logging
import sys
import threading
from datetime import datetime

import tkinter as tk

import config as app_config
from core.performance import PerformanceMonitor
from core.scanner import Scanner
from database import database as db
from gui import theme
from gui.dashboard_view import DashboardView
from gui.listings_view import ListingsView
from gui.scanner_view import ScannerView
from gui.settings_view import SettingsView
from gui.telegram_view import TelegramView
from services import discord as discord_service
from services import telegram as telegram_service

logger = logging.getLogger(__name__)

NAV_ITEMS = [
    ("dashboard", "🏠  Dashboard"),
    ("scanner",   "🔎  Scanner"),
    ("listings",  "📦  Annonces"),
    ("telegram",  "📱  Telegram"),
    ("settings",  "⚙️  Settings"),
]


def _run_async_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Tourne dans un thread secondaire : la loop asyncio complète."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🛍️ Vinted Monitor")
        self.geometry("1360x880")
        self.configure(bg=theme.BG)
        self.minsize(1080, 660)

        self.config_data = app_config.load_config()
        self.perf = PerformanceMonitor()
        self._start_time: datetime | None = None
        self._ad_count = 0

        # ── Thread asyncio dédié ────────────────────────────────────────────
        self._aloop = asyncio.new_event_loop()
        self._athread = threading.Thread(
            target=_run_async_loop, args=(self._aloop,), daemon=True, name="asyncio-loop",
        )
        self._athread.start()
        asyncio.run_coroutine_threadsafe(self._async_init(), self._aloop)

        # ── Scanner ─────────────────────────────────────────────────────────
        self.scanner = Scanner(
            config_provider=lambda: self.config_data,
            on_log=self._on_log,
            on_new_ad=self._on_new_ad,
            on_keyword_status=self._on_keyword_status,
            on_notification_update=self._on_notification_update,
            perf=self.perf,
        )

        self._build_ui()
        self.after(1500, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Init asynchrone (DB + workers) ──────────────────────────────────────

    async def _async_init(self) -> None:
        await db.init_db()
        await telegram_service.start_worker()
        await discord_service.start_worker()
        asyncio.create_task(db.periodic_purge(6.0), name="db-purge")
        asyncio.create_task(self.scanner.telegram_retry_loop(120.0), name="telegram-retry")

        recent = await db.get_recent_listings(300)
        self.after(0, self._hydrate_listings, recent)

    def _hydrate_listings(self, rows: list[dict]) -> None:
        self.listings_view.hydrate(rows)
        stats = db.get_stats()
        self._ad_count = stats.get("total_listings", 0)

    # ── Helper : exécuter une coroutine sur la loop asyncio depuis Tkinter ───

    def run_async(self, coro, callback=None):
        """Planifie `coro` sur la loop asyncio. `callback(result_or_exc)` est
        rappelé sur le thread Tkinter (via after) quand elle se termine."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._aloop)
        if callback:
            def _done(f):
                try:
                    result = f.result()
                except Exception as e:
                    result = e
                self.after(0, callback, result)
            fut.add_done_callback(_done)
        return fut

    def save_config(self) -> None:
        app_config.save_config(self.config_data)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = tk.Frame(self, bg=theme.BG)
        root.pack(fill="both", expand=True)

        sidebar = tk.Frame(root, bg=theme.SIDEBAR, width=200)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)

        right = tk.Frame(root, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True)

        content = tk.Frame(right, bg=theme.BG)
        content.pack(fill="both", expand=True, padx=16, pady=(14, 6))
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self.dashboard_view = DashboardView(content, self)
        self.scanner_view = ScannerView(content, self)
        self.listings_view = ListingsView(content, self)
        self.telegram_view = TelegramView(content, self)
        self.settings_view = SettingsView(content, self)

        self._views = {
            "dashboard": self.dashboard_view,
            "scanner": self.scanner_view,
            "listings": self.listings_view,
            "telegram": self.telegram_view,
            "settings": self.settings_view,
        }
        for view in self._views.values():
            view.grid(row=0, column=0, sticky="nsew")

        self._build_log_panel(right)
        self._show_view("dashboard")

    def _build_sidebar(self, sidebar: tk.Frame) -> None:
        tk.Label(
            sidebar, text="🛍️ VINTED\nMONITOR", font=theme.FONT_TITLE,
            bg=theme.SIDEBAR, fg=theme.ACCENT, justify="left", anchor="w",
        ).pack(fill="x", padx=16, pady=(20, 24))

        self._nav_buttons: dict[str, tk.Button] = {}
        for key, label in NAV_ITEMS:
            btn = tk.Button(
                sidebar, text=label, anchor="w", font=theme.FONT_BODY,
                bg=theme.SIDEBAR, fg=theme.TEXT2, activebackground=theme.PANEL2,
                activeforeground=theme.ACCENT, relief="flat", cursor="hand2",
                padx=16, pady=10, bd=0, command=lambda k=key: self._show_view(k),
            )
            btn.pack(fill="x")
            self._nav_buttons[key] = btn

        footer = tk.Frame(sidebar, bg=theme.SIDEBAR)
        footer.pack(side="bottom", fill="x", padx=16, pady=16)
        self._status_dot = tk.Label(
            footer, text="🔴 ARRÊTÉ", font=theme.FONT_BOLD, bg=theme.SIDEBAR, fg=theme.RED,
        )
        self._status_dot.pack(anchor="w")
        self._perf_lbl = tk.Label(
            footer, text="⚡ —", font=theme.FONT_SMALL, bg=theme.SIDEBAR, fg=theme.TEXT2,
        )
        self._perf_lbl.pack(anchor="w", pady=(4, 0))

    def _build_log_panel(self, parent: tk.Frame) -> None:
        panel = tk.Frame(parent, bg=theme.PANEL)
        panel.pack(fill="x", side="bottom", padx=16, pady=(0, 12))

        top = tk.Frame(panel, bg=theme.PANEL)
        top.pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(top, text="📋 Logs", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT2).pack(side="left")
        tk.Button(
            top, text="Vider", command=self._clear_logs, bg=theme.PANEL, fg=theme.TEXT2,
            relief="flat", font=theme.FONT_SMALL, cursor="hand2",
        ).pack(side="right")

        self._log_text = tk.Text(
            panel, height=6, bg=theme.PANEL, fg=theme.TEXT2, state="disabled",
            font=theme.FONT_MONO, relief="flat", wrap="word",
        )
        self._log_text.pack(fill="x", padx=8, pady=(2, 8))

    def _show_view(self, key: str) -> None:
        for k, btn in self._nav_buttons.items():
            active = k == key
            btn.config(
                bg=theme.PANEL2 if active else theme.SIDEBAR,
                fg=theme.ACCENT if active else theme.TEXT2,
            )
        self._views[key].tkraise()
        if hasattr(self._views[key], "on_show"):
            self._views[key].on_show()

    # ── Contrôle du scanner ───────────────────────────────────────────────────

    def toggle_scanner(self) -> None:
        if self.scanner.running:
            self.scanner.stop()
            self.set_running(False)
        else:
            self.save_config()
            self.scanner.start(self._aloop)
            self.set_running(True)

    def set_running(self, running: bool) -> None:
        self._start_time = datetime.now() if running else None
        if running:
            self._status_dot.config(text="🟢 ACTIF", fg=theme.GREEN)
        else:
            self._status_dot.config(text="🔴 ARRÊTÉ", fg=theme.RED)
        self.scanner_view.set_running_ui(running)

    # ── Callbacks thread-safe (appelés depuis le thread asyncio) ─────────────

    def _on_log(self, msg: str) -> None:
        self.after(0, self._append_log, msg)

    def _append_log(self, msg: str) -> None:
        self._log_text.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.insert("end", f"[{ts}] {msg}\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _clear_logs(self) -> None:
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _on_new_ad(self, ad: dict) -> None:
        self.after(0, self._handle_new_ad, ad)

    def _handle_new_ad(self, ad: dict) -> None:
        self._ad_count += 1
        self.listings_view.add_ad(ad)
        if self.config_data.get("telegram", {}).get("sound_enabled", False):
            self._play_sound()

    def _play_sound(self) -> None:
        try:
            if sys.platform == "win32":
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            else:
                print("\a", end="", flush=True)
        except Exception:
            pass

    def _on_keyword_status(self, keyword: str, status: dict) -> None:
        self.after(0, self.scanner_view.update_keyword_status, keyword, status)

    def _on_notification_update(self, ad_id: str, channel: str, ok: bool) -> None:
        self.after(0, self.listings_view.update_status, ad_id, channel, ok)

    # ── Stats périodiques ─────────────────────────────────────────────────────

    def _tick(self) -> None:
        summary = self.perf.summary()
        self._perf_lbl.config(text=f"⚡ {summary['total_ms']:.0f}ms/scan")
        self.dashboard_view.refresh(summary, self._ad_count, self._start_time, self.scanner)
        self.after(2000, self._tick)

    # ── Fermeture ─────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if self.scanner.running:
            self.scanner.stop()

        async def _shutdown():
            await telegram_service.close()
            await discord_service.close()
            from services.vinted import close_all
            await close_all()
            await db.close()

        fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._aloop)
        try:
            fut.result(timeout=5)
        except Exception as e:
            logger.debug(f"Shutdown: {e}")
        self._aloop.call_soon_threadsafe(self._aloop.stop)
        self.destroy()
