"""Palette et polices partagées par toutes les vues."""

BG        = "#0f1117"
SIDEBAR   = "#141722"
PANEL     = "#1a1d27"
PANEL2    = "#22263a"
ACCENT    = "#09B1BA"
ACCENT2   = "#f5a623"
TEXT      = "#e8eaf6"
TEXT2     = "#8892b0"
GREEN     = "#2ecc71"
RED       = "#e74c3c"
YELLOW    = "#f5c518"
CARD_BG   = "#1e2235"
CARD_HL   = "#252a40"
BORDER    = "#2a2f45"

FONT_TITLE = ("Segoe UI", 16, "bold")
FONT_H2    = ("Segoe UI", 13, "bold")
FONT_BODY  = ("Segoe UI", 9)
FONT_BOLD  = ("Segoe UI", 9, "bold")
FONT_SMALL = ("Segoe UI", 8)
FONT_MONO  = ("Consolas", 8)
FONT_STAT  = ("Segoe UI", 22, "bold")

STATUS_COLORS = {
    "starting": TEXT2,
    "scanning": GREEN,
    "error":    YELLOW,
    "stopped":  RED,
}
STATUS_LABELS = {
    "starting": "🟡 DÉMARRAGE",
    "scanning": "🟢 SCANNING",
    "error":    "🟡 ERROR / RETRY",
    "stopped":  "🔴 ARRÊTÉ",
}


def button(parent, text, command, bg=ACCENT, fg="white", **kw):
    import tkinter as tk
    defaults = dict(font=FONT_BOLD, relief="flat", cursor="hand2", padx=10, pady=6)
    defaults.update(kw)
    return tk.Button(parent, text=text, command=command, bg=bg, fg=fg, **defaults)
