"""Vue Scanner — mots-clés, marchés, intervalle, statut par mot-clé."""
import tkinter as tk

from gui import theme
from services import vinted as vinted_service


class ScannerView(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=theme.BG)
        self.app = app
        self._status_rows: dict[str, dict] = {}
        self._build()

    def _build(self) -> None:
        tk.Label(self, text="🔎 SCANNER", font=theme.FONT_H2, bg=theme.BG, fg=theme.TEXT).pack(anchor="w", pady=(0, 12))

        body = tk.Frame(self, bg=theme.BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=theme.PANEL, padx=14, pady=14, width=340)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        tk.Label(left, text="Mots-clés", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w")
        tk.Label(left, text="Chaque mot-clé = tâche de scan indépendante", font=theme.FONT_SMALL,
                 bg=theme.PANEL, fg=theme.TEXT2, wraplength=300, justify="left").pack(anchor="w")

        add_row = tk.Frame(left, bg=theme.PANEL)
        add_row.pack(fill="x", pady=(8, 10))
        self._new_kw_entry = tk.Entry(add_row, bg=theme.PANEL2, fg=theme.TEXT,
                                       insertbackground=theme.TEXT, relief="flat")
        self._new_kw_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self._new_kw_entry.bind("<Return>", lambda e: self._add_keyword())
        theme.button(add_row, "+ Ajouter", self._add_keyword, bg=theme.PANEL2, fg=theme.ACCENT).pack(side="left", padx=(6, 0))

        self._kw_list_frame = tk.Frame(left, bg=theme.PANEL)
        self._kw_list_frame.pack(fill="x", pady=(0, 10))

        tk.Label(left, text="Marchés actifs", font=theme.FONT_BOLD, bg=theme.PANEL, fg=theme.TEXT2).pack(anchor="w", pady=(6, 4))
        self._market_vars: dict[str, tk.BooleanVar] = {}
        for key, mdata in self.app.config_data["markets"].items():
            var = tk.BooleanVar(value=mdata.get("enabled", False))
            tk.Checkbutton(
                left, text=mdata.get("label", key), variable=var, bg=theme.PANEL, fg=theme.TEXT,
                selectcolor=theme.PANEL2, activebackground=theme.PANEL, activeforeground=theme.TEXT,
                font=theme.FONT_BODY,
            ).pack(anchor="w")
            self._market_vars[key] = var

        theme.button(
            left, "🔬 Tester les 3 marchés (FR/UK/PL)", self._test_markets,
            bg=theme.PANEL2, fg=theme.ACCENT,
        ).pack(fill="x", pady=(6, 0))

        interval_row = tk.Frame(left, bg=theme.PANEL)
        interval_row.pack(fill="x", pady=(14, 4))
        tk.Label(interval_row, text="Intervalle (s)", font=theme.FONT_BODY, bg=theme.PANEL, fg=theme.TEXT2).pack(side="left")
        self._interval_var = tk.StringVar(value=str(self.app.config_data.get("interval_seconds", 8)))
        tk.Spinbox(
            interval_row, from_=3, to=300, width=6, textvariable=self._interval_var,
            bg=theme.PANEL2, fg=theme.TEXT, insertbackground=theme.TEXT, relief="flat",
            buttonbackground=theme.PANEL2,
        ).pack(side="left", padx=6)
        tk.Label(interval_row, text="(min 3s — 5-8s conseillé)", font=theme.FONT_SMALL, bg=theme.PANEL, fg=theme.TEXT2).pack(side="left")

        theme.button(left, "💾 Sauvegarder", self._save_settings, bg=theme.PANEL2, fg=theme.ACCENT).pack(fill="x", pady=(14, 4))

        self._start_btn = theme.button(left, "🟢  DÉMARRER", self._toggle, bg=theme.GREEN)
        self._start_btn.pack(fill="x", pady=(4, 4))
        theme.button(left, "🔎 Scanner maintenant", self._scan_now, bg=theme.PANEL2, fg=theme.ACCENT).pack(fill="x")

        right = tk.Frame(body, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))
        tk.Label(right, text="Statut des mots-clés", font=theme.FONT_BOLD, bg=theme.BG, fg=theme.TEXT2).pack(anchor="w")
        self._status_frame = tk.Frame(right, bg=theme.PANEL)
        self._status_frame.pack(fill="both", expand=True, pady=(6, 0))

        self._refresh_keyword_list()

    # ── Mots-clés ─────────────────────────────────────────────────────────────

    def _refresh_keyword_list(self) -> None:
        for w in self._kw_list_frame.winfo_children():
            w.destroy()
        keywords = self.app.config_data.get("keywords_filter", [])
        if not keywords:
            tk.Label(self._kw_list_frame, text="Aucun mot-clé — scan global sur les marchés actifs",
                      font=theme.FONT_SMALL, bg=theme.PANEL, fg=theme.TEXT2, wraplength=300,
                      justify="left").pack(anchor="w", pady=4)
            return
        for kw in keywords:
            row = tk.Frame(self._kw_list_frame, bg=theme.PANEL2)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"🔤 {kw}", font=theme.FONT_BODY, bg=theme.PANEL2, fg=theme.TEXT, anchor="w").pack(
                side="left", fill="x", expand=True, padx=8, pady=4)
            tk.Button(
                row, text="✕", command=lambda k=kw: self._remove_keyword(k), bg=theme.PANEL2, fg=theme.RED,
                relief="flat", font=theme.FONT_SMALL, cursor="hand2", bd=0,
            ).pack(side="right", padx=6)

    def _add_keyword(self) -> None:
        kw = self._new_kw_entry.get().strip()
        if not kw:
            return
        keywords = self.app.config_data.setdefault("keywords_filter", [])
        if kw in keywords:
            self._new_kw_entry.delete(0, "end")
            return
        keywords.append(kw)
        self._new_kw_entry.delete(0, "end")
        self._refresh_keyword_list()
        self._save_settings()

    def _remove_keyword(self, kw: str) -> None:
        keywords = self.app.config_data.get("keywords_filter", [])
        if kw in keywords:
            keywords.remove(kw)
        self._remove_status_row(kw)
        self._refresh_keyword_list()
        self._save_settings()

    # ── Statut live par mot-clé ───────────────────────────────────────────────

    def update_keyword_status(self, keyword: str, status: dict) -> None:
        key = keyword or "(global)"
        if key not in self._status_rows:
            row = tk.Frame(self._status_frame, bg=theme.PANEL2)
            row.pack(fill="x", padx=8, pady=3)
            name_lbl = tk.Label(row, text=key, font=theme.FONT_BODY, bg=theme.PANEL2, fg=theme.TEXT, width=18, anchor="w")
            name_lbl.pack(side="left", padx=8, pady=6)
            status_lbl = tk.Label(row, text="", font=theme.FONT_BOLD, bg=theme.PANEL2, fg=theme.TEXT2, anchor="w")
            status_lbl.pack(side="left", padx=8)
            detail_lbl = tk.Label(row, text="", font=theme.FONT_SMALL, bg=theme.PANEL2, fg=theme.TEXT2, anchor="e")
            detail_lbl.pack(side="right", padx=8)
            self._status_rows[key] = {"row": row, "status": status_lbl, "detail": detail_lbl}

        widgets = self._status_rows[key]
        state = status.get("status", "starting")
        widgets["status"].config(text=theme.STATUS_LABELS.get(state, state), fg=theme.STATUS_COLORS.get(state, theme.TEXT2))
        detail = f"{status.get('scan_count', 0)} scans · {status.get('new_total', 0)} trouvées"
        if status.get("last_error"):
            detail += f" · {status['last_error'][:40]}"
        widgets["detail"].config(text=detail)

    def _remove_status_row(self, kw: str) -> None:
        key = kw or "(global)"
        widgets = self._status_rows.pop(key, None)
        if widgets:
            widgets["row"].destroy()

    # ── Actions ───────────────────────────────────────────────────────────────

    def _save_settings(self) -> None:
        cfg = self.app.config_data
        try:
            cfg["interval_seconds"] = max(3, int(self._interval_var.get()))
        except ValueError:
            pass
        for key, var in self._market_vars.items():
            cfg["markets"][key]["enabled"] = var.get()
        self.app.save_config()
        self.app.scanner.sync_keywords()
        self.app._on_log("💾 Configuration scanner sauvegardée.")

    def _toggle(self) -> None:
        self.app.toggle_scanner()

    def set_running_ui(self, running: bool) -> None:
        if running:
            self._start_btn.config(text="🔴  ARRÊTER", bg=theme.RED)
        else:
            self._start_btn.config(text="🟢  DÉMARRER", bg=theme.GREEN)
            for key in list(self._status_rows.keys()):
                widgets = self._status_rows.pop(key)
                widgets["row"].destroy()

    def _scan_now(self) -> None:
        self._save_settings()
        self.app.scanner.run_once()

    def _test_markets(self) -> None:
        """Teste FR/UK/PL en direct (init session + un vrai appel API chacun)
        et affiche le résultat dans les logs — pas besoin de terminal."""
        self.app._on_log("🔬 Test des 3 marchés en cours...")
        self.app.run_async(vinted_service.test_all_markets(), self._on_test_markets_result)

    def _on_test_markets_result(self, results) -> None:
        if isinstance(results, Exception):
            self.app._on_log(f"🔬 Test marchés — erreur inattendue : {results}")
            return
        for r in results:
            key = r["market"].upper()
            if r["ok"]:
                self.app._on_log(f"🔬 {key} ✅ opérationnel — {r['items']} annonce(s) reçue(s)")
            else:
                self.app._on_log(f"🔬 {key} ❌ échec — {r['reason']}")
