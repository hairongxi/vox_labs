"""Reusable Tk widgets: animated orb, flat buttons, scrollable settings."""

from __future__ import annotations

import math
import time
import tkinter as tk
from typing import Callable

from . import theme


class FlatButton(tk.Button):
    def __init__(
        self,
        master,
        text: str,
        command: Callable[[], None] | None = None,
        *,
        bg: str = theme.SURFACE_2,
        fg: str = theme.TEXT,
        hover: str | None = None,
        font=None,
        padx: int = 12,
        pady: int = 7,
        **kwargs,
    ) -> None:
        self._bg = bg
        self._hover = hover or theme.SURFACE_3
        super().__init__(
            master,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=self._hover,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            font=font,
            padx=padx,
            pady=pady,
            **kwargs,
        )
        self.bind("<Enter>", lambda _e: self.configure(bg=self._hover))
        self.bind("<Leave>", lambda _e: self.configure(bg=self._bg))

    def set_bg(self, bg: str, hover: str | None = None) -> None:
        self._bg = bg
        self._hover = hover or theme.lighten(bg, 0.08)
        self.configure(bg=bg, activebackground=self._hover)


class IconButton(FlatButton):
    def __init__(self, master, text: str, command=None, *, color=theme.MUTED, **kwargs):
        super().__init__(
            master,
            text,
            command,
            bg=theme.BG,
            fg=color,
            hover=theme.SURFACE_2,
            padx=8,
            pady=4,
            font=("Segoe UI", 12),
            **kwargs,
        )


class ScrollableFrame(tk.Frame):
    def __init__(self, master, bg: str = theme.BG, **kwargs) -> None:
        super().__init__(master, bg=bg, **kwargs)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_inner(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class OrbCanvas(tk.Canvas):
    """A small, dependency-free animated state orb.

    The drawing is intentionally simple (rings + radial bars) so it stays cheap
    at 30 fps while still reacting to both microphone and TTS levels.
    """

    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, bg=theme.BG, highlightthickness=0, bd=0, **kwargs)
        self._state = "idle"
        self._color = theme.MUTED
        self._animation = "breathe"
        self._mic = 0.0
        self._out = 0.0
        self._phase = 0.0
        self._last = time.monotonic()
        self._configured_width = 360
        self.after(33, self._tick)

    def set_state(self, state: str, style: dict) -> None:
        self._state = state
        self._color = style.get("color", theme.MUTED)
        self._animation = style.get("animation", "breathe")

    def set_levels(self, mic: float, out: float) -> None:
        # Normalize and smooth; speech RMS is usually 0.01–0.15.
        self._mic = max(0.0, min(1.0, mic * 7.0))
        self._out = max(0.0, min(1.0, out * 7.0))

    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self._last)
        self._last = now
        self._phase += dt
        try:
            self._draw()
        except tk.TclError:
            return
        self.after(33, self._tick)

    def _draw(self) -> None:
        self.delete("all")
        w = max(320, self.winfo_width())
        h = max(180, self.winfo_height())
        cx, cy = w / 2, h / 2 + 4
        color = self._color
        level = max(self._mic, self._out)
        phase = self._phase
        pulse = 0.5 + 0.5 * math.sin(phase * 1.6)

        if self._state == "idle":
            level *= 0.25
        elif self._state == "thinking":
            level = 0.25 + 0.12 * pulse
        elif self._state == "interrupted":
            level = 0.55 + 0.25 * pulse
        elif self._state == "error":
            level = 0.2

        # Soft halo.
        for i in range(4, 0, -1):
            r = 52 + i * 15 + level * 12
            self.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                outline=theme.blend(theme.BG, color, 0.06 + 0.035 * i),
                width=1,
            )

        # Rotating thinking arc.
        if self._animation == "orbit" or self._state == "thinking":
            start = (phase * 120) % 360
            for offset, extent in ((0, 110), (180, 55)):
                self.create_arc(
                    cx - 86, cy - 86, cx + 86, cy + 86,
                    start=start + offset, extent=extent,
                    style="arc", outline=color, width=3,
                )

        # Radial waveform bars.
        if self._animation in ("wave", "flash") or self._state in ("listening", "speaking", "interrupted"):
            bars = 56
            base_r = 58
            for i in range(bars):
                angle = (i / bars) * math.tau - math.pi / 2
                shimmer = 0.5 + 0.5 * math.sin(phase * 5.0 + i * 0.65)
                amp = 3 + level * 30 * (0.35 + 0.65 * shimmer)
                if self._state == "interrupted":
                    amp *= 0.6
                r0 = base_r
                r1 = base_r + amp
                x0, y0 = cx + math.cos(angle) * r0, cy + math.sin(angle) * r0
                x1, y1 = cx + math.cos(angle) * r1, cy + math.sin(angle) * r1
                self.create_line(
                    x0, y0, x1, y1,
                    fill=theme.blend(theme.BG, color, 0.35 + 0.65 * (amp / 36)),
                    width=2,
                    capstyle="round",
                )

        # Core.
        core_r = 38 + level * 8
        self.create_oval(
            cx - core_r, cy - core_r, cx + core_r, cy + core_r,
            fill=theme.blend(theme.SURFACE_2, color, 0.12 + 0.16 * pulse),
            outline=theme.blend(theme.BG, color, 0.55),
            width=2,
        )
        inner_r = 10 + level * 12
        self.create_oval(
            cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r,
            fill=theme.blend(color, theme.WHITE, 0.12),
            outline="",
        )

        if self._state == "error":
            self.create_line(cx - 12, cy - 12, cx + 12, cy + 12, fill=theme.WHITE, width=3)
            self.create_line(cx - 12, cy + 12, cx + 12, cy - 12, fill=theme.WHITE, width=3)
        elif self._state == "idle" and level < 0.03:
            self.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=theme.WHITE, outline="")
