"""
Vue Annonces — tableau des annonces détectées (Treeview), miniatures incluses.

Les miniatures sont récupérées de façon async (elles ne bloquent jamais le
scan ni l'UI) puis appliquées sur le thread Tkinter via `app.run_async(...)`.
Un échec d'image n'empêche jamais l'affichage de la ligne (fallback emoji).
"""
import io
import logging
import time
import webbrowser
from datetime import datetime
from typing import Optional

import tkinter as tk
from tkinter import ttk

from gui import theme

logger = logging.getLogger(__name__)

MAX_ROWS = 500
THUMB_SIZE = (36, 36)

STATUS_DISPLAY = {
    "sent": "✅ Envoyé",
    "failed": "❌ Échec",
    "pending": "⏳ En attente",
    "queued": "📤 En file",
    "disabled": "➖",
}

_thumb_session = None


async def _fetch_thumbnail_bytes(url: str) -> Optional[bytes]:
    import aiohttp

    global _thumb_session
    try:
        if _thumb_session is None or _thumb_session.closed:
            _thumb_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        async with _thumb_session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        logger.debug(f"[listings] thumbnail fetch failed: {e}")
    return None


class ListingsView(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=theme.BG)
        self.app = app
        self._thumb_cache: dict[str, object] = {}
        self._row_data: dict[str, dict] = {}
        self._build()

    def _build(self) -> None:
        top = tk.Frame(self, bg=theme.BG)
        top.pack(fill="x", pady=(0, 8))
        tk.Label(top, text="📦 ANNONCES", font=theme.FONT_H2, bg=theme.BG, fg=theme.TEXT).pack(side="left")
        tk.Label(top, text="Double-clic pour ouvrir · les plus récentes en haut", font=theme.FONT_SMALL,
                 bg=theme.BG, fg=theme.TEXT2).pack(side="left", padx=12)
        theme.button(top, "🗑️ Vider l'affichage", self.clear_display, bg=theme.PANEL2, fg=theme.TEXT2).pack(side="right")

        style = ttk.Style(self)
        style.configure("Listings.Treeview", background=theme.PANEL, fieldbackground=theme.PANEL,
                        foreground=theme.TEXT, rowheight=42, borderwidth=0)
        style.configure("Listings.Treeview.Heading", background=theme.PANEL2, foreground=theme.TEXT2,
                        relief="flat", font=theme.FONT_BOLD)
        style.map("Listings.Treeview", background=[("selected", theme.CARD_HL)])

        columns = ("title", "price", "keyword", "detected", "telegram", "discord")
        self._tree = ttk.Treeview(self, columns=columns, show="tree headings", style="Listings.Treeview", selectmode="browse")
        self._tree.heading("#0", text="🖼️")
        self._tree.column("#0", width=50, stretch=False, anchor="center")

        headers = {"title": "Nom", "price": "Prix", "keyword": "Mot-clé", "detected": "Détecté",
                   "telegram": "Telegram", "discord": "Discord"}
        widths = {"title": 340, "price": 90, "keyword": 110, "detected": 130, "telegram": 100, "discord": 100}
        for col in columns:
            self._tree.heading(col, text=headers[col])
            self._tree.column(col, width=widths[col], anchor="w")

        vsb = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-3>", self._on_right_click)

        self._menu = tk.Menu(self, tearoff=0, bg=theme.PANEL2, fg=theme.TEXT)
        self._menu.add_command(label="🔗 Ouvrir l'annonce", command=self._open_selected)
        self._menu.add_command(label="📋 Copier le lien", command=self._copy_selected)

    def on_show(self) -> None:
        pass

    # ── Peuplement ────────────────────────────────────────────────────────────

    def hydrate(self, rows: list[dict]) -> None:
        """rows = résultat de db.get_recent_listings(), déjà triées newest-first."""
        for row in reversed(rows):
            norm = {
                "id": row["id"], "title": row.get("title", ""), "price": row.get("price", ""),
                "keyword": row.get("keyword", ""), "url": row.get("url", ""), "image": row.get("image", ""),
                "detected_at": row.get("detected_at", 0),
                "telegram_status": row.get("telegram_status", "disabled"),
                "discord_status": row.get("discord_status", "disabled"),
            }
            self._insert_row(norm)
        self._enforce_max_rows()

    def add_ad(self, ad: dict) -> None:
        tg_cfg = self.app.config_data.get("telegram", {})
        dc_cfg = self.app.config_data.get("discord", {})
        norm = {
            "id": ad.get("id"), "title": ad.get("title", ""), "price": ad.get("price", ""),
            "keyword": ad.get("keyword", ""), "url": ad.get("url", ""), "image": ad.get("image", ""),
            "detected_at": int(time.time()),
            "telegram_status": "pending" if tg_cfg.get("enabled") else "disabled",
            "discord_status": "pending" if dc_cfg.get("enabled") else "disabled",
        }
        self._insert_row(norm)
        self._enforce_max_rows()

    def _insert_row(self, row: dict) -> None:
        rid = str(row["id"])
        if not rid or self._tree.exists(rid):
            return
        detected_str = ""
        if row.get("detected_at"):
            try:
                detected_str = datetime.fromtimestamp(row["detected_at"]).strftime("%d/%m %H:%M:%S")
            except Exception:
                pass
        values = (
            (row.get("title") or "")[:90],
            row.get("price", ""),
            row.get("keyword") or "—",
            detected_str,
            STATUS_DISPLAY.get(row.get("telegram_status", "disabled"), "—"),
            STATUS_DISPLAY.get(row.get("discord_status", "disabled"), "—"),
        )
        self._tree.insert("", 0, iid=rid, text="🖼️", values=values)
        self._row_data[rid] = row

        if row.get("image", "").startswith("http"):
            self.app.run_async(_fetch_thumbnail_bytes(row["image"]), lambda result, rid=rid: self._apply_thumbnail(rid, result))

    def _apply_thumbnail(self, rid: str, result) -> None:
        if not result or isinstance(result, Exception) or not self._tree.exists(rid):
            return
        try:
            from PIL import Image, ImageTk
            img = Image.open(io.BytesIO(result))
            img.thumbnail(THUMB_SIZE)
            photo = ImageTk.PhotoImage(img)
            self._thumb_cache[rid] = photo  # garder la référence (sinon Tkinter la garbage-collect)
            self._tree.item(rid, text="", image=photo)
        except Exception as e:
            logger.debug(f"[listings] thumbnail render failed: {e}")

    def _enforce_max_rows(self) -> None:
        children = self._tree.get_children("")
        for rid in children[MAX_ROWS:]:
            self._tree.delete(rid)
            self._row_data.pop(rid, None)
            self._thumb_cache.pop(rid, None)

    # ── Statut notifications (mis à jour en direct) ──────────────────────────

    def update_status(self, ad_id: str, channel: str, ok: bool) -> None:
        rid = str(ad_id)
        if not self._tree.exists(rid):
            return
        col = "telegram" if channel == "telegram" else "discord"
        self._tree.set(rid, col, STATUS_DISPLAY["sent"] if ok else STATUS_DISPLAY["failed"])

    # ── Actions ───────────────────────────────────────────────────────────────

    def clear_display(self) -> None:
        for rid in self._tree.get_children(""):
            self._tree.delete(rid)
        self._row_data.clear()
        self._thumb_cache.clear()

    def _on_double_click(self, event) -> None:
        rid = self._tree.identify_row(event.y)
        self._open_row(rid)

    def _on_right_click(self, event) -> None:
        rid = self._tree.identify_row(event.y)
        if rid:
            self._tree.selection_set(rid)
            self._menu.tk_popup(event.x_root, event.y_root)

    def _selected_row(self) -> Optional[dict]:
        sel = self._tree.selection()
        if not sel:
            return None
        return self._row_data.get(sel[0])

    def _open_selected(self) -> None:
        row = self._selected_row()
        if row and row.get("url"):
            webbrowser.open(row["url"])

    def _open_row(self, rid: str) -> None:
        row = self._row_data.get(rid)
        if row and row.get("url"):
            webbrowser.open(row["url"])

    def _copy_selected(self) -> None:
        row = self._selected_row()
        if row and row.get("url"):
            self.clipboard_clear()
            self.clipboard_append(row["url"])
