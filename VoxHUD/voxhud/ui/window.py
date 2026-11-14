"""Main HUD window and settings panel."""

from __future__ import annotations

import copy
import logging
import tkinter as tk
from dataclasses import dataclass
from tkinter import colorchooser, ttk
from typing import Callable

from .. import APP_TITLE
from ..config import (
    CN_ASR_URL,
    CN_LLM_URL,
    CN_TTS_URL,
    DEFAULT_STATE_STYLES,
    INTL_ASR_URL,
    INTL_LLM_URL,
    INTL_TTS_URL,
    AppConfig,
    STATE_KEYS,
)
from ..events import UiBus, UiEvent, UiEventKind
from . import theme
from .widgets import FlatButton, IconButton, OrbCanvas, ScrollableFrame

log = logging.getLogger(__name__)

WIDTH = 410
HEIGHT = 700


@dataclass
class WindowCallbacks:
    on_save: Callable[[AppConfig], None]
    on_toggle_mute: Callable[[], bool]
    on_interrupt: Callable[[], None]
    on_clear: Callable[[], None]
    on_submit_text: Callable[[str], None]
    on_hide: Callable[[], None]
    on_quit: Callable[[], None]
    on_list_devices: Callable[[], list[dict]]
    on_retry: Callable[[], None] = lambda: None


class MainWindow:
    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        bus: UiBus,
        callbacks: WindowCallbacks,
    ) -> None:
        self.root = root
        self.config = config
        self.bus = bus
        self.callbacks = callbacks
        self.font_family = theme.pick_font(root)
        self.current_state = "idle"
        self._mic_level = 0.0
        self._out_level = 0.0
        self._visible = True
        self._settings_open = False
        self._assistant_block_open = False
        self._partial_text = ""
        self._toast_after: str | None = None
        self._drag_x = 0
        self._drag_y = 0
        self._settings_widgets: dict[str, object] = {}
        self._state_vars: dict[str, dict[str, tk.StringVar]] = {}
        self._state_color_buttons: dict[str, FlatButton] = {}
        self._device_rows: list[dict] = []
        self._last_transcript_len = 0

        self._configure_root()
        self._setup_style()
        self._build()
        self._placement()
        self.apply_config(config)
        self._intro()
        self.root.after(33, self._pump)
        self.root.after(120, self._round_corners)

    # ------------------------------------------------------------------ root
    def _configure_root(self) -> None:
        self.root.title(APP_TITLE)
        self.root.overrideredirect(True)
        self.root.configure(bg=theme.BG)
        self.root.attributes("-topmost", bool(self.config.ui.always_on_top))
        self.root.attributes("-alpha", float(self.config.ui.opacity))
        self.root.minsize(360, 520)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Escape>", lambda _e: self.close_settings() if self._settings_open else self.hide())
        self.root.bind("<Control-q>", lambda _e: self.callbacks.on_quit())
        self.root.bind("<Control-m>", lambda _e: self._toggle_mute_clicked())

    def _placement(self) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = self.config.ui.window_x
        y = self.config.ui.window_y
        if x is None or y is None:
            x = max(0, (sw - WIDTH) // 2)
            y = max(0, (sh - HEIGHT) // 2 - 30)
        x = max(0, min(sw - WIDTH, int(x)))
        y = max(0, min(sh - HEIGHT, int(y)))
        self._target_x = x
        self._target_y = y
        self.root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")
        # Frameless windows occasionally map at (0, 0) before the first
        # configure event on Windows; re-apply after the window is mapped.
        self.root.after(160, self._reapply_geometry)

    def _reapply_geometry(self) -> None:
        try:
            self.root.geometry(f"{WIDTH}x{HEIGHT}+{self._target_x}+{self._target_y}")
        except Exception:
            pass

    def _round_corners(self) -> None:
        try:
            from ..platform.win32 import apply_rounded_corners
            apply_rounded_corners(self.root)
        except Exception:
            pass

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Vox.TCombobox",
            fieldbackground=theme.SURFACE_2,
            background=theme.SURFACE_2,
            foreground=theme.TEXT,
            arrowcolor=theme.MUTED,
            bordercolor=theme.BORDER,
            lightcolor=theme.SURFACE_2,
            darkcolor=theme.SURFACE_2,
            padding=4,
        )
        style.map(
            "Vox.TCombobox",
            fieldbackground=[("readonly", theme.SURFACE_2)],
            foreground=[("readonly", theme.TEXT)],
            selectbackground=[("readonly", theme.SURFACE_2)],
            selectforeground=[("readonly", theme.TEXT)],
            bordercolor=[("focus", theme.ACCENT)],
        )
        self.root.option_add("*TCombobox*Listbox.background", theme.SURFACE_2)
        self.root.option_add("*TCombobox*Listbox.foreground", theme.TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", theme.ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", theme.WHITE)

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        self.container = tk.Frame(self.root, bg=theme.BG)
        self.container.pack(fill="both", expand=True)
        self._build_titlebar()

        self.content = tk.Frame(self.container, bg=theme.BG)
        self.content.pack(fill="both", expand=True)
        self._build_main(self.content)

        self.settings_host = tk.Frame(self.container, bg=theme.BG)
        self.toast = tk.Label(
            self.root,
            text="",
            bg=theme.SURFACE_3,
            fg=theme.TEXT,
            font=(self.font_family, 9),
            padx=12,
            pady=6,
        )

    def _build_titlebar(self) -> None:
        self.titlebar = tk.Frame(self.container, bg=theme.BG, height=44)
        self.titlebar.pack(fill="x", side="top")
        self.titlebar.pack_propagate(False)

        self.conn_dot = tk.Canvas(self.titlebar, width=10, height=10, bg=theme.BG, highlightthickness=0)
        self.conn_dot.pack(side="left", padx=(16, 6), pady=(16, 0))
        self._dot_item = self.conn_dot.create_oval(2, 2, 8, 8, fill=theme.MUTED, outline="")

        self.title_label = tk.Label(
            self.titlebar, text="VoxHUD", bg=theme.BG, fg=theme.TEXT,
            font=(self.font_family, 12, "bold"),
        )
        self.title_label.pack(side="left", pady=(12, 0))

        self.mode_badge = tk.Label(
            self.titlebar, text="DEMO", bg=theme.SURFACE_2, fg=theme.MUTED,
            font=(self.font_family, 7, "bold"), padx=6, pady=2,
        )
        self.mode_badge.pack(side="left", padx=(8, 0), pady=(14, 0))

        self.close_btn = IconButton(self.titlebar, "✕", self._on_close, color=theme.MUTED)
        self.close_btn.pack(side="right", padx=(0, 8), pady=(10, 0))
        self.min_btn = IconButton(self.titlebar, "—", self.hide, color=theme.MUTED)
        self.min_btn.pack(side="right", pady=(10, 0))
        self.gear_btn = IconButton(self.titlebar, "⚙", self.toggle_settings, color=theme.MUTED)
        self.gear_btn.pack(side="right", pady=(10, 0))
        self.pin_btn = IconButton(
            self.titlebar,
            "⌖",
            self._toggle_pin,
            color=theme.ACCENT if self.config.ui.always_on_top else theme.MUTED,
        )
        self.pin_btn.pack(side="right", pady=(10, 0))

        for widget in (self.titlebar, self.title_label, self.mode_badge, self.conn_dot):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)

    def _build_main(self, parent: tk.Frame) -> None:
        self.orb = OrbCanvas(parent, height=180)
        self.orb.pack(fill="x", padx=18, pady=(2, 0))

        self.status_label = tk.Label(
            parent, text="待命", bg=theme.BG, fg=theme.MUTED,
            font=(self.font_family, 17, "bold"),
        )
        self.status_label.pack(pady=(0, 2))

        self.hint_label = tk.Label(
            parent, text="", bg=theme.BG, fg=theme.MUTED,
            font=(self.font_family, 9), wraplength=340, justify="center",
        )
        self.hint_label.pack(pady=(0, 6))

        self.partial_label = tk.Label(
            parent, text="", bg=theme.BG, fg=theme.ACCENT_SOFT,
            font=(self.font_family, 10), wraplength=350, justify="center",
        )
        self.partial_label.pack(pady=(0, 2))

        self.transcript_card = tk.Frame(
            parent, bg=theme.SURFACE, highlightbackground=theme.BORDER,
            highlightthickness=1,
        )
        self.transcript_card.pack(fill="both", expand=True, padx=18, pady=(2, 8))

        header = tk.Frame(self.transcript_card, bg=theme.SURFACE)
        header.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(
            header, text="对话记录", bg=theme.SURFACE, fg=theme.MUTED,
            font=(self.font_family, 8, "bold"),
        ).pack(side="left")
        self.clear_btn = IconButton(header, "清空", self.callbacks.on_clear, color=theme.MUTED)
        self.clear_btn.configure(font=(self.font_family, 8), padx=4, pady=0)
        self.clear_btn.pack(side="right")

        self.transcript = tk.Text(
            self.transcript_card,
            height=9,
            bg=theme.SURFACE,
            fg=theme.TEXT_SOFT,
            bd=0,
            highlightthickness=0,
            wrap="word",
            font=(self.font_family, 9),
            padx=10,
            pady=6,
            insertbackground=theme.TEXT,
            spacing1=2,
            spacing3=2,
            cursor="arrow",
        )
        self.transcript.pack(fill="both", expand=True, padx=2, pady=(2, 6))
        self.transcript.tag_configure("user_label", foreground=theme.ACCENT_SOFT, font=(self.font_family, 8, "bold"))
        self.transcript.tag_configure("user_text", foreground=theme.TEXT, font=(self.font_family, 10))
        self.transcript.tag_configure("assistant_label", foreground=theme.GREEN, font=(self.font_family, 8, "bold"))
        self.transcript.tag_configure("assistant_text", foreground=theme.TEXT_SOFT, font=(self.font_family, 10))
        self.transcript.tag_configure("meta", foreground=theme.MUTED_2, font=(self.font_family, 8))
        self.transcript.bind("<Key>", lambda _e: "break")
        self.transcript.bind("<MouseWheel>", self._scroll_transcript)

        self.input_row = tk.Frame(parent, bg=theme.BG)
        self.input_row.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(
            self.input_row, text="文字测试", bg=theme.BG, fg=theme.MUTED_2,
            font=(self.font_family, 8),
        ).pack(side="left", padx=(0, 6))
        self.text_entry = tk.Entry(
            self.input_row,
            bg=theme.SURFACE_2,
            fg=theme.TEXT,
            insertbackground=theme.TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme.BORDER,
            highlightcolor=theme.ACCENT,
            font=(self.font_family, 9),
        )
        self.text_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.text_entry.bind("<Return>", self._submit_text)
        self.send_btn = IconButton(self.input_row, "发送", self._submit_text, color=theme.ACCENT_SOFT)
        self.send_btn.configure(font=(self.font_family, 8), padx=8, pady=3)
        self.send_btn.pack(side="left", padx=(6, 0))

        self.footer = tk.Frame(parent, bg=theme.BG)
        self.footer.pack(fill="x", padx=18, pady=(0, 12))
        self.mute_btn = FlatButton(
            self.footer, "🎙 麦克风", self._toggle_mute_clicked,
            bg=theme.SURFACE_2, fg=theme.TEXT_SOFT,
            font=(self.font_family, 9), padx=10, pady=6,
        )
        self.mute_btn.pack(side="left")
        self.interrupt_btn = FlatButton(
            self.footer, "⏹ 打断", self.callbacks.on_interrupt,
            bg=theme.SURFACE_2, fg=theme.TEXT_SOFT,
            font=(self.font_family, 9), padx=10, pady=6,
        )
        self.interrupt_btn.pack(side="left", padx=(8, 0))
        self.retry_btn = FlatButton(
            self.footer, "↻ 重试", self.callbacks.on_retry,
            bg=theme.SURFACE_2, fg=theme.TEXT_SOFT,
            font=(self.font_family, 9), padx=10, pady=6,
        )
        self.retry_btn.pack(side="left", padx=(8, 0))
        self.latency_label = tk.Label(
            self.footer, text="— ms", bg=theme.BG, fg=theme.MUTED_2,
            font=(self.font_family, 8),
        )
        self.latency_label.pack(side="right")

    # ------------------------------------------------------------------ events
    def _pump(self) -> None:
        for event in self.bus.drain(120):
            try:
                self._handle(event)
            except Exception:
                log.exception("处理 UI 事件失败：%s", event.kind)
        self.root.after(33, self._pump)

    def _handle(self, event: UiEvent) -> None:
        kind = event.kind
        data = event.data
        if kind == UiEventKind.STATE.value:
            self.set_state(data.get("state", "idle"), data.get("detail", ""))
        elif kind == UiEventKind.PARTIAL.value:
            self._partial_text = data.get("text", "")
            self._show_partial()
        elif kind == UiEventKind.FINAL.value:
            self._partial_text = ""
            text = (data.get("text", "") or "").strip()
            if len(text) > 90:
                text = "…" + text[-90:]
            self.partial_label.configure(text=f"你：{text}", fg=theme.ACCENT_SOFT)
            self._append_user(data.get("text", ""))
        elif kind == UiEventKind.ASSISTANT_CLEAR.value:
            self._assistant_block_open = False
        elif kind == UiEventKind.ASSISTANT_DELTA.value:
            self._append_assistant_delta(data.get("text", ""))
        elif kind == UiEventKind.ASSISTANT_DONE.value:
            if self._assistant_block_open:
                self._transcript_insert("\n\n", "assistant_text")
                self._assistant_block_open = False
        elif kind == UiEventKind.CLEAR.value:
            self._clear_transcript()
        elif kind == UiEventKind.LEVEL.value:
            self._mic_level = float(data.get("mic", 0.0))
            self._out_level = float(data.get("out", 0.0))
            self.orb.set_levels(self._mic_level, self._out_level)
        elif kind == UiEventKind.LATENCY.value:
            ms = data.get("eot_to_audio_ms")
            if ms is not None:
                self.latency_label.configure(text=f"首音 {int(ms)} ms")
        elif kind == UiEventKind.ERROR.value:
            message = data.get("message", "发生错误")
            self.show_toast(message, level="error", duration=4200)
        elif kind == UiEventKind.TOAST.value:
            self.show_toast(data.get("message", ""), level=data.get("level", "info"))
        elif kind == UiEventKind.CONNECTION.value:
            self._set_connection(data.get("status", ""), data.get("message", ""))
        elif kind == UiEventKind.MUTE.value:
            self._set_mute_ui(bool(data.get("muted")))
        elif kind == UiEventKind.CONFIG.value:
            mode = data.get("mode", "demo")
            self.mode_badge.configure(text="QWEN" if mode == "qwen" else "DEMO")
        elif kind == UiEventKind.DEBUG.value:
            if self._settings_open and "debug_text" in self._settings_widgets:
                text = self._settings_widgets["debug_text"]
                if isinstance(text, tk.Text):
                    text.configure(state="normal")
                    text.insert("end", f"{data.get('event')}: {data.get('data')}\n")
                    text.see("end")
                    text.configure(state="disabled")

    def set_state(self, state: str, detail: str = "") -> None:
        self.current_state = state
        style = self.config.state_style(state)
        self.status_label.configure(text=style.get("label", state), fg=style.get("color", theme.MUTED))
        self.hint_label.configure(text=style.get("hint", ""))
        self.orb.set_state(state, style)
        try:
            self.conn_dot.itemconfigure(self._dot_item, fill=style.get("color", theme.MUTED))
        except tk.TclError:
            pass
        if state == "idle":
            self.partial_label.configure(text="")
            self._partial_text = ""

    def _show_partial(self) -> None:
        if not self._partial_text:
            self.partial_label.configure(text="")
            return
        text = self._partial_text
        if len(text) > 90:
            text = "…" + text[-90:]
        self.partial_label.configure(text=f"你：{text}")

    def _append_user(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._transcript_insert("你  ", "user_label")
        self._transcript_insert(text + "\n\n", "user_text")

    def _append_assistant_delta(self, delta: str) -> None:
        if not delta:
            return
        if not self._assistant_block_open:
            self._transcript_insert("VoxHUD  ", "assistant_label")
            self._assistant_block_open = True
        self._transcript_insert(delta, "assistant_text")

    def _transcript_insert(self, text: str, tag: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, tag)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")
        self.transcript.after_idle(lambda: self.transcript.see("end"))
        self._trim_transcript()

    def _trim_transcript(self) -> None:
        try:
            lines = int(self.transcript.index("end-1c").split(".")[0])
        except Exception:
            return
        if lines > 220:
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", "80.0")
            self.transcript.configure(state="disabled")

    def _clear_transcript(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._assistant_block_open = False
        self._partial_text = ""
        self.partial_label.configure(text="")
        self.partial_label.configure(fg=theme.ACCENT_SOFT)
        self._intro()

    def _intro(self) -> None:
        self._transcript_insert(
            "直接说话即可开始。说完停顿一下，我会接话；我说话时也可以随时打断。\n",
            "meta",
        )
        if self.config.provider_mode == "demo":
            self._transcript_insert(
                "当前为演示模式：本地脚本回复 + 音调语音。配置 Qwen 后启用真实语音。\n\n",
                "meta",
            )

    def _scroll_transcript(self, event) -> None:
        self.transcript.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _set_connection(self, status: str, message: str) -> None:
        colors = {
            "connecting": theme.AMBER,
            "ready": theme.GREEN,
            "connected": theme.GREEN,
            "reconnecting": theme.AMBER,
            "error": theme.RED,
        }
        color = colors.get(status, theme.MUTED)
        try:
            self.conn_dot.itemconfigure(self._dot_item, fill=color)
        except tk.TclError:
            pass
        if status in ("connecting", "reconnecting") and message:
            self.show_toast(message, level="info")
        elif status == "error" and message:
            self.show_toast(message, level="error")

    def _set_mute_ui(self, muted: bool) -> None:
        if muted:
            self.mute_btn.configure(text="🔇 已静音", fg=theme.AMBER)
        else:
            self.mute_btn.configure(text="🎙 麦克风", fg=theme.TEXT_SOFT)

    def _toggle_mute_clicked(self) -> None:
        muted = self.callbacks.on_toggle_mute()
        self._set_mute_ui(muted)

    def _submit_text(self, _event=None) -> None:
        text = self.text_entry.get().strip()
        if not text:
            return
        self.text_entry.delete(0, "end")
        self.callbacks.on_submit_text(text)

    def show_toast(self, message: str, level: str = "info", duration: int = 2600) -> None:
        if not message:
            return
        color = {
            "error": theme.RED,
            "warn": theme.AMBER,
            "info": theme.TEXT,
        }.get(level, theme.TEXT)
        self.toast.configure(text=message, fg=color)
        self.toast.place(relx=0.5, rely=0.955, anchor="s")
        self.toast.lift()
        if self._toast_after is not None:
            try:
                self.root.after_cancel(self._toast_after)
            except Exception:
                pass
        self._toast_after = self.root.after(duration, self._hide_toast)

    def _hide_toast(self) -> None:
        self._toast_after = None
        try:
            self.toast.place_forget()
        except Exception:
            pass

    # ------------------------------------------------------------------ window
    def _start_drag(self, event) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag(self, event) -> None:
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _toggle_pin(self) -> None:
        self.config.ui.always_on_top = not self.config.ui.always_on_top
        self.root.attributes("-topmost", self.config.ui.always_on_top)
        self.pin_btn.configure(
            fg=theme.ACCENT if self.config.ui.always_on_top else theme.MUTED
        )
        self.show_toast("已置顶" if self.config.ui.always_on_top else "取消置顶")

    def _on_close(self) -> None:
        if self.config.ui.close_to_tray:
            self.hide()
        else:
            self.callbacks.on_quit()

    def hide(self) -> None:
        if self._settings_open:
            self.close_settings()
        self._visible = False
        self.root.withdraw()
        self.callbacks.on_hide()

    def show(self) -> None:
        self._visible = True
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", bool(self.config.ui.always_on_top))
        self.root.focus_force()
        self._round_corners()

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def show_settings(self) -> None:
        self.show()
        if not self._settings_open:
            self.open_settings()

    def destroy(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------ config
    def apply_config(self, config: AppConfig) -> None:
        self.config = config
        self.root.attributes("-alpha", float(config.ui.opacity))
        self.root.attributes("-topmost", bool(config.ui.always_on_top))
        self.pin_btn.configure(fg=theme.ACCENT if config.ui.always_on_top else theme.MUTED)
        self.mode_badge.configure(text="QWEN" if config.provider_mode == "qwen" else "DEMO")
        accent = config.ui.accent
        try:
            self.send_btn.set_bg(accent, theme.lighten(accent, 0.1))
            self.send_btn.configure(fg=theme.WHITE)
            self.partial_label.configure(fg=theme.lighten(accent, 0.2))
            self.transcript.tag_configure("user_label", foreground=theme.lighten(accent, 0.2))
        except Exception:
            pass
        self.set_state(self.current_state)
        if not config.ui.show_transcript:
            self.transcript_card.pack_forget()
        elif not self.transcript_card.winfo_manager():
            self.transcript_card.pack(
                fill="both", expand=True, padx=18, pady=(2, 8),
                before=self.input_row,
            )

    # ------------------------------------------------------------------ settings
    def toggle_settings(self) -> None:
        if self._settings_open:
            self.close_settings()
        else:
            self.open_settings()

    def open_settings(self) -> None:
        if self._settings_open:
            return
        self._settings_open = True
        self.content.pack_forget()
        self.title_label.configure(text="设置")
        self.gear_btn.configure(text="‹")
        self.gear_btn.configure(command=self.close_settings)
        self._build_settings()
        self.settings_host.pack(fill="both", expand=True)

    def close_settings(self) -> None:
        if not self._settings_open:
            return
        self._settings_open = False
        self.settings_host.pack_forget()
        try:
            self.settings_host.destroy()
        except Exception:
            pass
        self.settings_host = tk.Frame(self.container, bg=theme.BG)
        self.content.pack(fill="both", expand=True)
        self.title_label.configure(text="VoxHUD")
        self.gear_btn.configure(text="⚙", command=self.toggle_settings)
        self._settings_widgets.clear()
        self._state_vars.clear()

    def _build_settings(self) -> None:
        for child in self.settings_host.winfo_children():
            child.destroy()
        scroll = ScrollableFrame(self.settings_host, bg=theme.BG)
        scroll.pack(fill="both", expand=True)
        body = scroll.inner
        pad = {"padx": 18, "pady": 4}

        self._section(body, "运行模式")
        mode_row = tk.Frame(body, bg=theme.BG)
        mode_row.pack(fill="x", **pad)
        self._label(mode_row, "服务", width=7).pack(side="left")
        self._combo(
            mode_row,
            ["演示模式（离线）", "Qwen（DashScope）"],
            "Qwen（DashScope）" if self.config.provider_mode == "qwen" else "演示模式（离线）",
            key="provider_mode",
        ).pack(side="left", fill="x", expand=True)

        self._entry_row(body, "DashScope Key", self.config.dashscope.api_key, key="api_key", show="•")

        region_row = tk.Frame(body, bg=theme.BG)
        region_row.pack(fill="x", **pad)
        self._label(region_row, "区域", width=7).pack(side="left")
        self._combo(
            region_row,
            ["中国（北京）", "国际（新加坡）"],
            "国际（新加坡）" if self.config.dashscope.region == "intl" else "中国（北京）",
            key="region",
        ).pack(side="left", fill="x", expand=True)
        self._settings_widgets["region"].bind("<<ComboboxSelected>>", self._region_changed)

        self._section(body, "语音识别 ASR")
        self._entry_row(body, "WebSocket URL", self.config.dashscope.asr_url, key="asr_url")
        self._entry_row(body, "模型", self.config.dashscope.asr_model, key="asr_model")
        self._entry_row(body, "语言", self.config.dashscope.language, key="asr_language")
        self._entry_row(body, "静音断句 ms", str(self.config.dashscope.asr_silence_ms), key="asr_silence")

        self._section(body, "语音合成 TTS")
        self._entry_row(body, "WebSocket URL", self.config.dashscope.tts_url, key="tts_url")
        self._entry_row(body, "模型", self.config.dashscope.tts_model, key="tts_model")
        self._entry_row(body, "音色", self.config.dashscope.tts_voice, key="tts_voice")
        self._entry_row(body, "语言类型", self.config.dashscope.tts_language_type, key="tts_language")

        self._section(body, "大模型 LLM")
        self._entry_row(body, "Base URL", self.config.llm.base_url, key="llm_base")
        self._entry_row(body, "模型", self.config.llm.model, key="llm_model")
        self._entry_row(body, "API Key（留空复用）", self.config.llm.api_key, key="llm_key", show="•")
        self._text_row(body, "系统提示词", self.config.llm.system_prompt, key="system_prompt", height=4)

        self._section(body, "轮次管理 Turn-taking")
        self._entry_row(body, "EOT 静音 hold ms", str(self.config.turn.eot_hold_ms), key="eot_hold")
        self._entry_row(body, "句末 hold ms", str(self.config.turn.eot_terminal_hold_ms), key="eot_terminal")
        self._entry_row(body, "ASR 超时 ms", str(self.config.turn.eot_asr_timeout_ms), key="eot_timeout")
        self._check_row(body, "允许语音打断", self.config.turn.barge_in_enabled, key="barge_on")
        self._check_row(body, "思考时可打断", self.config.turn.allow_thinking_interrupt, key="barge_thinking")
        self._entry_row(body, "打断保护 ms", str(self.config.turn.barge_in_guard_ms), key="barge_guard")
        self._entry_row(body, "最短语音 ms", str(self.config.turn.barge_in_min_speech_ms), key="barge_min")
        self._entry_row(body, "灵敏度 0–1", str(self.config.turn.barge_in_sensitivity), key="barge_sensitivity")
        self._check_row(body, "ASR final 也可触发打断", self.config.turn.barge_in_on_asr, key="barge_asr")

        self._section(body, "音频")
        self._device_rows = self.callbacks.on_list_devices()
        input_values = ["默认输入"] + [
            f"{d['index']}: {d['name']}" for d in self._device_rows if d["inputs"] > 0
        ]
        output_values = ["默认输出"] + [
            f"{d['index']}: {d['name']}" for d in self._device_rows if d["outputs"] > 0
        ]
        self._device_combo(body, "输入设备", input_values, self.config.audio.input_device, key="input_device")
        self._device_combo(body, "输出设备", output_values, self.config.audio.output_device, key="output_device")
        self._check_row(body, "回声消除 AEC", self.config.audio.aec, key="aec")
        self._check_row(body, "降噪 NS", self.config.audio.noise_suppression, key="ns")
        self._check_row(body, "自动增益 AGC", self.config.audio.agc, key="agc")
        self._entry_row(body, "输出音量 0–2", str(self.config.audio.output_volume), key="volume")

        self._section(body, "界面与热键")
        self._entry_row(body, "呼出热键", self.config.ui.hotkey, key="hotkey")
        self._entry_row(body, "打断热键", self.config.ui.interrupt_hotkey, key="interrupt_hotkey")
        self._check_row(body, "窗口置顶", self.config.ui.always_on_top, key="topmost")
        self._check_row(body, "关闭时隐藏到托盘", self.config.ui.close_to_tray, key="tray")
        self._entry_row(body, "透明度 0.55–1", str(self.config.ui.opacity), key="opacity")

        accent_row = tk.Frame(body, bg=theme.BG)
        accent_row.pack(fill="x", **pad)
        self._label(accent_row, "强调色", width=10).pack(side="left")
        self.accent_btn = FlatButton(
            accent_row, self.config.ui.accent, self._choose_accent,
            bg=self.config.ui.accent, fg=theme.WHITE,
            font=(self.font_family, 8), padx=10, pady=4,
        )
        self.accent_btn.pack(side="left")

        self._section(body, "输出状态定制")
        tk.Label(
            body,
            text="每个状态的文案、提示和颜色都会即时生效；动画可选 breathe / wave / orbit / flash / flat。",
            bg=theme.BG, fg=theme.MUTED_2, font=(self.font_family, 8),
            wraplength=350, justify="left",
        ).pack(anchor="w", **pad)
        self._state_vars.clear()
        for state in STATE_KEYS:
            style = self.config.state_style(state)
            card = tk.Frame(body, bg=theme.SURFACE, highlightbackground=theme.BORDER, highlightthickness=1)
            card.pack(fill="x", padx=18, pady=4)
            head = tk.Frame(card, bg=theme.SURFACE)
            head.pack(fill="x", padx=8, pady=(6, 0))
            tk.Label(head, text=state, bg=theme.SURFACE, fg=theme.MUTED,
                     font=(self.font_family, 8, "bold")).pack(side="left")
            color_btn = FlatButton(
                head, style.get("color", theme.MUTED), None,
                bg=style.get("color", theme.MUTED), fg=theme.WHITE,
                font=(self.font_family, 7), padx=6, pady=1,
            )
            color_btn.configure(command=lambda b=color_btn, s=state: self._choose_state_color(b, s))
            color_btn.pack(side="right")
            self._state_color_buttons[state] = color_btn
            label_var = tk.StringVar(value=style.get("label", ""))
            hint_var = tk.StringVar(value=style.get("hint", ""))
            anim_var = tk.StringVar(value=style.get("animation", "breathe"))
            self._state_vars[state] = {
                "label": label_var, "hint": hint_var, "animation": anim_var,
                "color": tk.StringVar(value=style.get("color", theme.MUTED)),
            }
            row = tk.Frame(card, bg=theme.SURFACE)
            row.pack(fill="x", padx=8, pady=2)
            tk.Label(row, text="标题", bg=theme.SURFACE, fg=theme.MUTED_2,
                     font=(self.font_family, 8), width=5, anchor="w").pack(side="left")
            tk.Entry(row, textvariable=label_var, bg=theme.SURFACE_2, fg=theme.TEXT,
                     insertbackground=theme.TEXT, relief="flat", highlightthickness=1,
                     highlightbackground=theme.BORDER, font=(self.font_family, 8)).pack(
                side="left", fill="x", expand=True, ipady=2)
            row2 = tk.Frame(card, bg=theme.SURFACE)
            row2.pack(fill="x", padx=8, pady=(0, 2))
            tk.Label(row2, text="提示", bg=theme.SURFACE, fg=theme.MUTED_2,
                     font=(self.font_family, 8), width=5, anchor="w").pack(side="left")
            tk.Entry(row2, textvariable=hint_var, bg=theme.SURFACE_2, fg=theme.TEXT,
                     insertbackground=theme.TEXT, relief="flat", highlightthickness=1,
                     highlightbackground=theme.BORDER, font=(self.font_family, 8)).pack(
                side="left", fill="x", expand=True, ipady=2)
            row3 = tk.Frame(card, bg=theme.SURFACE)
            row3.pack(fill="x", padx=8, pady=(0, 6))
            tk.Label(row3, text="动画", bg=theme.SURFACE, fg=theme.MUTED_2,
                     font=(self.font_family, 8), width=5, anchor="w").pack(side="left")
            ttk.Combobox(
                row3, textvariable=anim_var, state="readonly",
                values=["breathe", "wave", "orbit", "flash", "flat"],
                style="Vox.TCombobox", font=(self.font_family, 8), width=12,
            ).pack(side="left")

        # Debug log section.
        self._section(body, "调试")
        debug_text = tk.Text(
            body, height=5, bg=theme.SURFACE, fg=theme.MUTED, bd=0,
            highlightthickness=1, highlightbackground=theme.BORDER,
            font=("Consolas", 8), padx=6, pady=4, wrap="word",
        )
        debug_text.pack(fill="x", padx=18, pady=(2, 4))
        debug_text.configure(state="disabled")
        self._settings_widgets["debug_text"] = debug_text

        note = tk.Label(
            body,
            text="提示：API Key 会以明文保存在本机配置文件中；请勿分享配置文件。\n保存后音频/服务会自动重连。",
            bg=theme.BG, fg=theme.MUTED_2, font=(self.font_family, 8),
            wraplength=350, justify="left",
        )
        note.pack(anchor="w", padx=18, pady=(6, 4))

        footer = tk.Frame(self.settings_host, bg=theme.BG)
        footer.pack(fill="x", side="bottom", padx=18, pady=(4, 14))
        FlatButton(
            footer, "保存并应用", self._save_settings,
            bg=theme.ACCENT, fg=theme.WHITE, hover=theme.lighten(theme.ACCENT, 0.1),
            font=(self.font_family, 10, "bold"), padx=14, pady=8,
        ).pack(side="right")
        FlatButton(
            footer, "取消", self.close_settings,
            bg=theme.SURFACE_2, fg=theme.TEXT_SOFT,
            font=(self.font_family, 9), padx=14, pady=8,
        ).pack(side="right", padx=(0, 8))
        FlatButton(
            footer, "恢复默认外观", self._reset_styles,
            bg=theme.SURFACE_2, fg=theme.MUTED,
            font=(self.font_family, 8), padx=10, pady=8,
        ).pack(side="left")

    # ------------------------------------------------------------- settings UI
    def _section(self, parent: tk.Frame, text: str) -> None:
        label = tk.Label(
            parent, text=text, bg=theme.BG, fg=theme.ACCENT_SOFT,
            font=(self.font_family, 9, "bold"), anchor="w",
        )
        label.pack(fill="x", padx=18, pady=(12, 4))

    def _label(self, parent, text: str, width: int = 10) -> tk.Label:
        return tk.Label(
            parent, text=text, bg=theme.BG, fg=theme.MUTED,
            font=(self.font_family, 8), width=width, anchor="w",
        )

    def _entry_row(
        self,
        parent: tk.Frame,
        label: str,
        value: str,
        *,
        key: str,
        show: str = "",
    ) -> tk.Entry:
        row = tk.Frame(parent, bg=theme.BG)
        row.pack(fill="x", padx=18, pady=3)
        self._label(row, label, width=16).pack(side="left")
        entry = tk.Entry(
            row, bg=theme.SURFACE_2, fg=theme.TEXT,
            insertbackground=theme.TEXT, relief="flat",
            highlightthickness=1, highlightbackground=theme.BORDER,
            highlightcolor=theme.ACCENT, font=(self.font_family, 8), show=show,
        )
        entry.insert(0, value)
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        self._settings_widgets[key] = entry
        return entry

    def _text_row(self, parent: tk.Frame, label: str, value: str, *, key: str, height: int = 3) -> tk.Text:
        row = tk.Frame(parent, bg=theme.BG)
        row.pack(fill="x", padx=18, pady=3)
        self._label(row, label, width=16).pack(side="left", anchor="n")
        text = tk.Text(
            row, height=height, bg=theme.SURFACE_2, fg=theme.TEXT,
            insertbackground=theme.TEXT, relief="flat",
            highlightthickness=1, highlightbackground=theme.BORDER,
            font=(self.font_family, 8), wrap="word", padx=5, pady=4,
        )
        text.insert("1.0", value)
        text.pack(side="left", fill="x", expand=True)
        self._settings_widgets[key] = text
        return text

    def _check_row(self, parent: tk.Frame, label: str, value: bool, *, key: str) -> None:
        row = tk.Frame(parent, bg=theme.BG)
        row.pack(fill="x", padx=18, pady=1)
        var = tk.BooleanVar(value=bool(value))
        check = tk.Checkbutton(
            row, text=label, variable=var, bg=theme.BG, fg=theme.TEXT_SOFT,
            activebackground=theme.BG, activeforeground=theme.TEXT,
            selectcolor=theme.SURFACE_2, highlightthickness=0, bd=0,
            font=(self.font_family, 8), anchor="w",
        )
        check.pack(side="left")
        self._settings_widgets[key] = var

    def _combo(self, parent, values: list[str], value: str, *, key: str) -> ttk.Combobox:
        combo = ttk.Combobox(
            parent, values=values, state="readonly", style="Vox.TCombobox",
            font=(self.font_family, 8),
        )
        combo.set(value)
        self._settings_widgets[key] = combo
        return combo

    def _device_combo(
        self, parent: tk.Frame, label: str, values: list[str],
        current: int | None, *, key: str,
    ) -> None:
        row = tk.Frame(parent, bg=theme.BG)
        row.pack(fill="x", padx=18, pady=3)
        self._label(row, label, width=16).pack(side="left")
        display = values[0]
        if current is not None:
            for value in values:
                if value.startswith(f"{current}:"):
                    display = value
                    break
        combo = ttk.Combobox(
            row, values=values, state="readonly", style="Vox.TCombobox",
            font=(self.font_family, 8),
        )
        combo.set(display)
        combo.pack(side="left", fill="x", expand=True)
        self._settings_widgets[key] = combo

    def _choose_accent(self) -> None:
        color = colorchooser.askcolor(color=self.config.ui.accent, title="选择强调色")[1]
        if color:
            self.accent_btn.configure(bg=color, fg=theme.WHITE, text=color)
            self._settings_widgets["accent"] = tk.StringVar(value=color)

    def _choose_state_color(self, button: FlatButton, state: str) -> None:
        current = button.cget("text")
        color = colorchooser.askcolor(color=current, title="选择状态颜色")[1]
        if color:
            button.configure(bg=color, text=color, fg=theme.WHITE)
            if state in self._state_vars:
                self._state_vars[state]["color"].set(color)

    def _reset_styles(self) -> None:
        for state in STATE_KEYS:
            style = DEFAULT_STATE_STYLES[state]
            if state in self._state_vars:
                self._state_vars[state]["label"].set(style["label"])
                self._state_vars[state]["hint"].set(style["hint"])
                self._state_vars[state]["color"].set(style["color"])
                self._state_vars[state]["animation"].set(style["animation"])
                button = self._state_color_buttons.get(state)
                if button is not None:
                    button.configure(bg=style["color"], text=style["color"])
        self.show_toast("外观已恢复默认，点击保存生效")

    def _region_changed(self, _event=None) -> None:
        region = self._settings_widgets.get("region")
        if not isinstance(region, ttk.Combobox):
            return
        intl = region.get().startswith("国际")
        mapping = {
            "asr_url": (INTL_ASR_URL if intl else CN_ASR_URL),
            "tts_url": (INTL_TTS_URL if intl else CN_TTS_URL),
            "llm_base": (INTL_LLM_URL if intl else CN_LLM_URL),
        }
        for key, url in mapping.items():
            widget = self._settings_widgets.get(key)
            if isinstance(widget, tk.Entry):
                widget.delete(0, "end")
                widget.insert(0, url)

    def _get_entry(self, key: str) -> str:
        widget = self._settings_widgets.get(key)
        if isinstance(widget, tk.Entry):
            return widget.get().strip()
        return ""

    def _save_settings(self) -> None:
        try:
            cfg = copy.deepcopy(self.config)
            mode = self._settings_widgets.get("provider_mode")
            if isinstance(mode, ttk.Combobox):
                cfg.provider_mode = "qwen" if mode.get().startswith("Qwen") else "demo"
            cfg.dashscope.api_key = self._get_entry("api_key")
            region = self._settings_widgets.get("region")
            if isinstance(region, ttk.Combobox):
                cfg.dashscope.region = "intl" if region.get().startswith("国际") else "cn"
            cfg.dashscope.asr_url = self._get_entry("asr_url")
            cfg.dashscope.asr_model = self._get_entry("asr_model")
            cfg.dashscope.language = self._get_entry("asr_language")
            cfg.dashscope.asr_silence_ms = int(self._get_entry("asr_silence") or 420)
            cfg.dashscope.tts_url = self._get_entry("tts_url")
            cfg.dashscope.tts_model = self._get_entry("tts_model")
            cfg.dashscope.tts_voice = self._get_entry("tts_voice")
            cfg.dashscope.tts_language_type = self._get_entry("tts_language")
            cfg.llm.base_url = self._get_entry("llm_base")
            cfg.llm.model = self._get_entry("llm_model")
            cfg.llm.api_key = self._get_entry("llm_key")
            system = self._settings_widgets.get("system_prompt")
            if isinstance(system, tk.Text):
                cfg.llm.system_prompt = system.get("1.0", "end").strip()
            cfg.turn.eot_hold_ms = int(self._get_entry("eot_hold") or 320)
            cfg.turn.eot_terminal_hold_ms = int(self._get_entry("eot_terminal") or 140)
            cfg.turn.eot_asr_timeout_ms = int(self._get_entry("eot_timeout") or 1100)
            cfg.turn.barge_in_enabled = bool(self._settings_widgets["barge_on"].get())
            cfg.turn.allow_thinking_interrupt = bool(self._settings_widgets["barge_thinking"].get())
            cfg.turn.barge_in_guard_ms = int(self._get_entry("barge_guard") or 320)
            cfg.turn.barge_in_min_speech_ms = int(self._get_entry("barge_min") or 140)
            cfg.turn.barge_in_sensitivity = float(self._get_entry("barge_sensitivity") or 0.55)
            cfg.turn.barge_in_on_asr = bool(self._settings_widgets["barge_asr"].get())
            cfg.audio.aec = bool(self._settings_widgets["aec"].get())
            cfg.audio.noise_suppression = bool(self._settings_widgets["ns"].get())
            cfg.audio.agc = bool(self._settings_widgets["agc"].get())
            cfg.audio.output_volume = float(self._get_entry("volume") or 0.85)
            cfg.audio.input_device = self._parse_device("input_device")
            cfg.audio.output_device = self._parse_device("output_device")
            cfg.ui.hotkey = self._get_entry("hotkey")
            cfg.ui.interrupt_hotkey = self._get_entry("interrupt_hotkey")
            cfg.ui.always_on_top = bool(self._settings_widgets["topmost"].get())
            cfg.ui.close_to_tray = bool(self._settings_widgets["tray"].get())
            cfg.ui.opacity = float(self._get_entry("opacity") or 0.97)
            accent = self._settings_widgets.get("accent")
            if isinstance(accent, tk.StringVar):
                cfg.ui.accent = accent.get()
            for state, vars_ in self._state_vars.items():
                cfg.ui.states.setdefault(state, {})
                cfg.ui.states[state]["label"] = vars_["label"].get()
                cfg.ui.states[state]["hint"] = vars_["hint"].get()
                cfg.ui.states[state]["color"] = vars_["color"].get()
                cfg.ui.states[state]["animation"] = vars_["animation"].get()
            cfg.ui.window_x = int(self.root.winfo_x())
            cfg.ui.window_y = int(self.root.winfo_y())
            warnings = cfg.validate()
            self.callbacks.on_save(cfg)
            self.close_settings()
            self.apply_config(cfg)
            if warnings:
                self.show_toast("；".join(warnings[:2]), level="warn", duration=3600)
            else:
                self.show_toast("设置已保存，正在重连…")
        except ValueError as exc:
            self.show_toast(f"数值格式错误：{exc}", level="error")
        except Exception as exc:
            log.exception("保存设置失败")
            self.show_toast(f"保存失败：{exc}", level="error")

    def _parse_device(self, key: str) -> int | None:
        widget = self._settings_widgets.get(key)
        if not isinstance(widget, ttk.Combobox):
            return None
        value = widget.get()
        if not value or value.startswith("默认"):
            return None
        try:
            return int(value.split(":", 1)[0])
        except ValueError:
            return None
