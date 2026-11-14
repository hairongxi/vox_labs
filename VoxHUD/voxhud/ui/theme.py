"""Dark HUD palette and small colour helpers (Tk has no alpha)."""

from __future__ import annotations

import tkinter.font as tkfont

BG = "#0B0C10"
SURFACE = "#14161D"
SURFACE_2 = "#1C1F29"
SURFACE_3 = "#242836"
BORDER = "#2A2E3D"
TEXT = "#F2F3F7"
TEXT_SOFT = "#C9CCD8"
MUTED = "#8B90A3"
MUTED_2 = "#62677A"
ACCENT = "#7C5CFF"
ACCENT_SOFT = "#A18CFF"
GREEN = "#4FD1C5"
AMBER = "#F6AD55"
RED = "#FC8181"
RED_DARK = "#F56565"
WHITE = "#FFFFFF"


def pick_font(root) -> str:
    families = set(tkfont.families(root))
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Arial"):
        if name in families:
            return name
    return "TkDefaultFont"


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except Exception:
        return 124, 92, 255


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = (max(0, min(255, int(v))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def blend(base: str, color: str, alpha: float) -> str:
    """Composite ``color`` over ``base`` at ``alpha`` (0..1)."""
    alpha = max(0.0, min(1.0, alpha))
    br, bg, bb = hex_to_rgb(base)
    cr, cg, cb = hex_to_rgb(color)
    return rgb_to_hex((
        br + (cr - br) * alpha,
        bg + (cg - bg) * alpha,
        bb + (cb - bb) * alpha,
    ))


def lighten(color: str, amount: float = 0.15) -> str:
    return blend(color, "#FFFFFF", amount)


def darken(color: str, amount: float = 0.15) -> str:
    return blend(color, "#000000", amount)
