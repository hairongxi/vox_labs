"""Application wiring: Tk main loop, asyncio worker loop, hotkeys, tray, lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any, Coroutine

from . import APP_NAME, __version__
from .audio.engine import AudioEngine
from .config import AppConfig, ConfigStore, data_dir
from .conversation import ConversationController
from .events import UiBus, UiEventKind
from .logging_setup import setup_logging
from .platform.win32 import (
    HotkeyManager,
    SingleInstance,
    TrayIcon,
    set_dpi_awareness,
)
from .ui.window import MainWindow, WindowCallbacks

log = logging.getLogger(__name__)


class AsyncRuntime:
    """Run asyncio on a dedicated thread; Tk owns the main thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="voxhud-asyncio", daemon=True)
        self._started = False

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        if not self._started:
            self._thread.start()
            self._started = True

    def submit(self, coro: Coroutine[Any, Any, Any]):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass
        self._thread.join(timeout=2.0)
        self._started = False


class VoxHudApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.store = ConfigStore()
        self.config = self.store.load()
        if args.demo:
            self.config.provider_mode = "demo"
        self.log_file = setup_logging(data_dir() / "logs", debug=args.debug)
        log.info("VoxHUD %s 启动，模式=%s，日志=%s", __version__, self.config.provider_mode, self.log_file)

        set_dpi_awareness()
        self.root = tk.Tk()
        self.bus = UiBus()
        self.runtime = AsyncRuntime()
        self.runtime.start()
        self.controller: ConversationController | None = None
        self.window: MainWindow | None = None
        self.hotkeys = HotkeyManager()
        self.tray: TrayIcon | None = None
        self.single: SingleInstance | None = None
        self._actions: queue.Queue[str] = queue.Queue()
        self._quitting = False
        self._shutdown_done = False

    # ------------------------------------------------------------------ startup
    def run(self) -> int:
        if not self.args.no_single_instance:
            self.single = SingleInstance()
            if not self.single.acquire():
                log.info("已有 VoxHUD 实例，发送显示请求后退出")
                return 0
            self.single.on_show = lambda: self._actions.put("show")
            self.single.on_quit = lambda: self._actions.put("quit")

        self.controller = ConversationController(self.config, self.bus, self.runtime.loop)
        self.runtime.submit(self.controller.start())

        callbacks = WindowCallbacks(
            on_save=self._save_config,
            on_toggle_mute=self._toggle_mute,
            on_interrupt=lambda: self._submit_action(self.controller.interrupt, "manual") if self.controller else None,
            on_clear=lambda: self._submit_action(self.controller.clear_conversation) if self.controller else None,
            on_submit_text=lambda text: self._submit_action(self.controller.submit_text, text) if self.controller else None,
            on_hide=self._on_hide,
            on_quit=self.quit,
            on_list_devices=AudioEngine.list_devices,
            on_retry=self._retry,
        )
        self.window = MainWindow(self.root, self.config, self.bus, callbacks)
        if self.config.ui.start_hidden:
            self.window.hide()
        if self.args.settings:
            self.window.show_settings()

        self._register_hotkeys()
        if not self.args.no_tray:
            self.tray = TrayIcon(
                on_show=lambda: self._actions.put("toggle"),
                on_settings=lambda: self._actions.put("settings"),
                on_clear=lambda: self._actions.put("clear"),
                on_mute=lambda: self._actions.put("mute"),
                on_quit=lambda: self._actions.put("quit"),
            )
            if not self.tray.start():
                # Without a tray icon, hiding on close would strand the app.
                self.config.ui.close_to_tray = False
                if self.window is not None:
                    self.window.config.ui.close_to_tray = False
                self.bus.emit(
                    UiEventKind.TOAST,
                    message="托盘不可用，关闭窗口将退出；热键仍可使用",
                    level="warn",
                )

        self.root.after(30, self._pump_actions)
        if self.args.smoke_test:
            self.root.after(1200, self._smoke_turn)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        log.info("界面就绪")
        try:
            self.root.mainloop()
        finally:
            self._shutdown()
        return 0

    def _smoke_turn(self) -> None:
        if self.controller is not None:
            log.info("smoke test: submit text turn")
            self._submit_action(self.controller.submit_text, "你好，请介绍一下你自己。")
            self.window.show_toast("Smoke test: 正在生成演示回答…")

    # ------------------------------------------------------------------ actions
    def _pump_actions(self) -> None:
        if self._quitting:
            return
        while True:
            try:
                action = self._actions.get_nowait()
            except queue.Empty:
                break
            log.debug("UI action: %s", action)
            try:
                if action == "toggle" and self.window:
                    self.window.toggle()
                elif action == "show" and self.window:
                    self.window.show()
                elif action == "settings" and self.window:
                    self.window.show_settings()
                elif action == "clear" and self.controller:
                    self.controller.clear_conversation()
                elif action == "mute":
                    muted = self._toggle_mute()
                    if self.window:
                        self.window._set_mute_ui(muted)
                elif action == "interrupt" and self.controller:
                    self._submit_action(self.controller.interrupt, "hotkey")
                elif action == "quit":
                    self.quit()
                    return
            except Exception:
                log.exception("UI action 失败：%s", action)
        self.root.after(40, self._pump_actions)

    def _submit_action(self, fn, *args) -> None:
        async def runner():
            fn(*args)

        self.runtime.submit(runner())

    def _toggle_mute(self) -> bool:
        if self.controller is None:
            return False
        desired = not self.controller.muted
        self._submit_action(self.controller.set_muted, desired)
        return desired

    def _retry(self) -> None:
        if self.controller is None:
            return
        async def runner():
            self.controller.manager.recover()
            if self.controller.providers is not None:
                await self.controller.providers.asr.start()
        self.runtime.submit(runner())

    def _on_hide(self) -> None:
        log.debug("窗口隐藏")

    # ------------------------------------------------------------------ hotkeys
    def _register_hotkeys(self) -> None:
        self.hotkeys.clear()
        if self.args.no_hotkey:
            return
        self.hotkeys.register([
            (self.config.ui.hotkey, lambda: self._hotkey_event("toggle")),
            (self.config.ui.interrupt_hotkey, lambda: self._hotkey_event("interrupt")),
        ])
        for error in self.hotkeys.errors:
            self.bus.emit(UiEventKind.TOAST, message=error, level="warn")

    def _hotkey_event(self, action: str) -> None:
        log.debug("hotkey event: %s", action)
        self._actions.put(action)

    # ------------------------------------------------------------------ config
    def _save_config(self, new_config: AppConfig) -> None:
        old = self.config
        self.config = new_config
        try:
            self.store.config = new_config
            self.store.save(new_config)
        except Exception as exc:
            log.exception("保存配置失败")
            self.bus.emit(UiEventKind.ERROR, message=f"保存配置失败：{exc}")
            return
        if self.window is not None:
            self.window.apply_config(new_config)
        if (
            old.ui.hotkey != new_config.ui.hotkey
            or old.ui.interrupt_hotkey != new_config.ui.interrupt_hotkey
        ):
            self._register_hotkeys()
        self._restart_controller(new_config)

    def _restart_controller(self, new_config: AppConfig) -> None:
        self.bus.emit(UiEventKind.CONNECTION, status="connecting", message="正在应用新配置…")

        async def restart():
            old = self.controller
            if old is not None:
                await old.shutdown()
            controller = ConversationController(new_config, self.bus, self.runtime.loop)
            self.controller = controller
            await controller.start()

        self.runtime.submit(restart())

    # ------------------------------------------------------------------ quit
    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        log.info("正在退出")
        self.hotkeys.clear()
        if self.tray is not None:
            self.tray.stop()
        if self.single is not None:
            self.single.close()
        if self.controller is not None:
            try:
                self.runtime.submit(self.controller.shutdown()).result(timeout=3.0)
            except Exception:
                pass
        self.runtime.stop()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    def _shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.hotkeys.clear()
        if self.tray is not None:
            self.tray.stop()
        if self.single is not None:
            self.single.close()
        self.runtime.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voxhud", description="VoxHUD 全双工语音助手")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--data-dir", type=str, default=None, help="数据目录")
    parser.add_argument("--demo", action="store_true", help="强制演示模式")
    parser.add_argument("--debug", action="store_true", help="调试日志")
    parser.add_argument("--no-hotkey", action="store_true", help="不注册全局热键")
    parser.add_argument("--no-tray", action="store_true", help="不创建托盘图标")
    parser.add_argument("--no-single-instance", action="store_true", help="跳过单实例检查")
    parser.add_argument("--smoke-test", action="store_true", help="启动后自动提交一轮文字测试")
    parser.add_argument("--settings", action="store_true", help="启动时打开设置页")
    parser.add_argument("--version", action="version", version=f"VoxHUD {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["VOXHUD_DATA_DIR"] = args.data_dir
    if args.config:
        os.environ["VOXHUD_CONFIG"] = args.config
    app = VoxHudApp(args)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
