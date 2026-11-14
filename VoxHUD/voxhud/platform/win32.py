"""Small, fail-soft Windows integrations: DPI, rounded corners, hotkeys, tray."""

from __future__ import annotations

import ctypes
import logging
import socket
import socketserver
import threading
from typing import Callable

log = logging.getLogger(__name__)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_ulong),
        ("pt", POINT),
        ("lPrivate", ctypes.c_ulong),
    ]


def _parse_hotkey(value: str) -> tuple[int, int] | None:
    parts = [p.strip().lower() for p in (value or "").split("+") if p.strip()]
    if not parts:
        return None
    modifiers = 0
    key = parts[-1]
    for part in parts[:-1]:
        if part in ("ctrl", "control"):
            modifiers |= 0x0002
        elif part in ("alt", "menu"):
            modifiers |= 0x0001
        elif part in ("shift",):
            modifiers |= 0x0004
        elif part in ("win", "windows", "super"):
            modifiers |= 0x0008
        else:
            return None
    named = {
        "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
        "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
        "insert": 0x2D, "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pagedown": 0x22,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "printscreen": 0x2C, "pause": 0x13,
    }
    if key in named:
        vk = named[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        return None
    if modifiers == 0 and not key.startswith("f"):
        return None
    return modifiers, vk


def set_dpi_awareness() -> None:
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _root_hwnd(root) -> int:
    hwnd = int(root.winfo_id())
    try:
        ga_root = 2
        parent = ctypes.windll.user32.GetAncestor(hwnd, ga_root)
        return int(parent) or hwnd
    except Exception:
        return hwnd


def apply_rounded_corners(root) -> None:
    try:
        hwnd = _root_hwnd(root)
        # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
        value = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass


def apply_dark_frame(root) -> None:
    try:
        hwnd = _root_hwnd(root)
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass


class HotkeyManager:
    """Native ``RegisterHotKey`` on a message-only window.

    Unlike low-level keyboard hooks, this keeps working when the Tk window is
    withdrawn and does not depend on a third-party hook package.
    """

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    MOD_NOREPEAT = 0x4000

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._errors: list[str] = []
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._wndproc = None

    def register(self, registrations: list[tuple[str, Callable[[], None]]]) -> None:
        self.clear()
        self._errors = []
        if not registrations:
            return
        self._thread = threading.Thread(
            target=self._run, args=(registrations,), daemon=True, name="voxhud-hotkeys"
        )
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def _run(self, registrations: list[tuple[str, Callable[[], None]]]) -> None:
        try:
            self._native_run(registrations)
        except Exception as exc:
            self._errors.append(f"热键线程异常：{exc}")
            log.exception("热键线程异常")
            self._ready.set()

    def _native_run(self, registrations: list[tuple[str, Callable[[], None]]]) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        user32.RegisterHotKey.restype = ctypes.c_bool
        user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.UnregisterHotKey.restype = ctypes.c_bool
        user32.RegisterClassW.argtypes = [ctypes.c_void_p]
        user32.RegisterClassW.restype = ctypes.c_ushort
        user32.CreateWindowExW.argtypes = [
            ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.PostMessageW.restype = ctypes.c_bool
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_ssize_t,
        )

        def wndproc(hwnd, msg, wparam, lparam):
            log.debug("hotkey wndproc msg=0x%x wparam=%s", msg, wparam)
            if msg == 0x0312:  # WM_HOTKEY
                callback = self._callbacks.get(int(wparam))
                if callback:
                    try:
                        callback()
                    except Exception:
                        log.exception("热键回调失败")
                return 0
            if msg in (0x0010, 0x0002):  # WM_CLOSE / WM_DESTROY
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
            ]

        instance = kernel32.GetModuleHandleW(None)
        class_name = "VoxHudHotkeyWindow"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = instance
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0, class_name, "VoxHUD", 0, 0, 0, 0, 0,
            ctypes.c_void_p(-3), None, instance, None,  # HWND_MESSAGE
        )
        if not hwnd:
            self._errors.append("创建热键窗口失败")
            self._ready.set()
            return
        self._hwnd = int(hwnd)

        for index, (hotkey, callback) in enumerate(registrations, start=1):
            parsed = _parse_hotkey(hotkey)
            if parsed is None:
                self._errors.append(f"无法解析热键：{hotkey}")
                continue
            modifiers, vk = parsed
            ok = user32.RegisterHotKey(
                hwnd, index, modifiers | self.MOD_NOREPEAT, vk
            )
            if ok:
                self._callbacks[index] = callback
                log.info("注册热键：%s", hotkey)
            else:
                self._errors.append(f"热键被占用或无效：{hotkey}")
                log.warning("注册热键失败：%s", hotkey)

        self._ready.set()
        log.info("热键消息循环启动 hwnd=%s", hwnd)
        stop_heartbeat = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(3.0):
                log.debug("hotkey heartbeat")

        threading.Thread(target=heartbeat, daemon=True, name="voxhud-hotkey-hb").start()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        stop_heartbeat.set()
        log.info("热键消息循环结束")
        for index in list(self._callbacks):
            try:
                user32.UnregisterHotKey(hwnd, index)
            except Exception:
                pass
        self._callbacks.clear()

    def clear(self) -> None:
        hwnd = self._hwnd
        self._hwnd = None
        if hwnd:
            try:
                ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._ready.clear()
        self._callbacks.clear()

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def error(self) -> str:
        return "；".join(self._errors)


class SingleInstance:
    """A tiny localhost handshake; second launch asks the first to show."""

    def __init__(self, port: int = 47321) -> None:
        self.port = port
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.on_show: Callable[[], None] = lambda: None
        self.on_quit: Callable[[], None] = lambda: None

    def acquire(self) -> bool:
        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    self.request.settimeout(1.0)
                    data = self.request.recv(64).decode("ascii", "ignore").strip().lower()
                    owner: SingleInstance = self.server.owner  # type: ignore[attr-defined]
                    if data == "show":
                        owner.on_show()
                        self.request.sendall(b"voxhud")
                    elif data == "quit":
                        owner.on_quit()
                        self.request.sendall(b"voxhud")
                    else:
                        self.request.sendall(b"voxhud")
                except Exception:
                    pass

        try:
            server = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), Handler)
            server.daemon_threads = True
            server.owner = self  # type: ignore[attr-defined]
            self._server = server
            self._thread = threading.Thread(target=server.serve_forever, daemon=True, name="single-instance")
            self._thread.start()
            return True
        except OSError:
            # Existing instance: ask it to reveal itself. Verify the handshake
            # so an unrelated process on the port does not block startup.
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.8) as sock:
                    sock.sendall(b"show")
                    reply = sock.recv(16)
                    if b"voxhud" in reply:
                        return False
            except Exception:
                pass
            return True

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None


class TrayIcon:
    def __init__(
        self,
        on_show: Callable[[], None],
        on_settings: Callable[[], None],
        on_clear: Callable[[], None],
        on_mute: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self.on_show = on_show
        self.on_settings = on_settings
        self.on_clear = on_clear
        self.on_mute = on_mute
        self.on_quit = on_quit
        self._icon = None
        self._thread: threading.Thread | None = None
        self._error = ""

    def start(self, wait_s: float = 0.4) -> bool:
        try:
            import pystray
            from PIL import Image, ImageDraw

            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((6, 6, 58, 58), fill=(124, 92, 255, 255))
            draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 230))
            menu = pystray.Menu(
                pystray.MenuItem("显示 / 隐藏", lambda: self.on_show(), default=True),
                pystray.MenuItem("设置", lambda: self.on_settings()),
                pystray.MenuItem("静音 / 取消静音", lambda: self.on_mute()),
                pystray.MenuItem("清空对话", lambda: self.on_clear()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出 VoxHUD", lambda: self.on_quit()),
            )
            self._icon = pystray.Icon("VoxHUD", image, "VoxHUD 语音助手", menu)

            def run() -> None:
                try:
                    self._icon.run()
                except Exception as exc:
                    self._error = str(exc)
                    log.warning("托盘图标运行失败：%s", exc)

            self._thread = threading.Thread(target=run, daemon=True, name="voxhud-tray")
            self._thread.start()
            import time as _time

            _time.sleep(wait_s)
            return not self._error
        except Exception as exc:
            self._error = str(exc)
            log.warning("托盘图标启动失败：%s", exc)
            return False

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
