"""Windows notification-area (tray) icon, without a GUI toolkit dependency.

A VRChat companion has to get out of the way: the user wants it resident while
in a world, not occupying a taskbar slot. This adds a tray icon with Open /
Wear last / Quit, and lets the window close to the tray instead of quitting.

Everything goes through Shell_NotifyIcon on a message-only window, so the
executable stays a single self-contained file with no extra packages.

If any of this fails -- an unexpected Windows version, a stripped shell --
``available()`` returns False and the app runs without a tray icon rather than
failing to start.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

# Shell_NotifyIcon messages
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

# NOTIFYICONDATA uFlags
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

NOTIFYICONDATA_VERSION_4 = 4

# Window messages
WM_APP = 0x8000
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP_TRAY = WM_APP + 1

# Menu flags
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800

# LoadImage
IMAGE_ICON = 1
LR_LOADBYORDER = 0x00000010

# IDI_APPLICATION, as a resource id for LoadIconW. Passed through MAKEINTRESOURCE
# semantics (a pointer whose value is the id), not as a string: LoadIconW would
# otherwise look for a resource *named* "32512" and return nothing.
IDI_APPLICATION = 32512

# NOTIFYICONDATA.dwInfoFlags
NIIF_INFO = 0x00000001

# HWND_MESSAGE: a message-only window, never shown and never in the taskbar.
HWND_MESSAGE = -3

TRAY_CALLBACK = WM_APP_TRAY


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM)),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NOTIFYICONDATA(ctypes.Structure):
    """NOTIFYICONDATAW. The tail fields require uVersion 4, which we set."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


def available() -> bool:
    """True when this process can create a tray icon."""
    return os.name == "nt" and hasattr(ctypes, "windll")


def _configure_user32() -> None:
    """Pin the signatures of the user32 entry points we call.

    Without this, ctypes guesses. DefWindowProcW's fourth parameter is an LPARAM,
    which is 64-bit on x64; guessing c_int makes every message that reaches the
    default handler raise "int too long to convert" from inside the callback,
    where the exception is swallowed and only printed.
    """
    user32 = ctypes.windll.user32
    lrESULT = ctypes.POINTER(ctypes.c_longlong)
    wndproc_args: list = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]

    user32.DefWindowProcW.argtypes = wndproc_args
    user32.DefWindowProcW.restype = ctypes.c_longlong

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND

    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = wintypes.ATOM

    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.LoadImageW.restype = wintypes.HANDLE

    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadIconW.restype = wintypes.HANDLE

    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, wintypes.LPCVOID,
    ]
    user32.TrackPopupMenu.restype = wintypes.BOOL

    user32.PostMessageW.argtypes = wndproc_args
    user32.PostMessageW.restype = wintypes.BOOL

    user32.SendMessageW.argtypes = [*wndproc_args, ctypes.c_void_p]
    user32.SendMessageW.restype = lrESULT

    user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL

    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL

    shell32 = ctypes.windll.shell32
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class TrayIcon:
    """A message-only window owning a notification-area icon.

    ``on_command`` is invoked with :attr:`OPEN`, :attr:`WEAR_LAST` or
    :attr:`QUIT`. The window must live on the thread that pumps its messages, so
    construction starts a dedicated thread with its own loop.
    """

    OPEN = 1
    WEAR_LAST = 2
    QUIT = 3

    def __init__(self, title: str, tip: str = "Local Avatar Favourites",
                 icon_path: str | None = None) -> None:
        self.title = title
        self.tip = tip
        self.icon_path = icon_path
        self.on_command = None
        self._hwnd = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._added = False
        self._taskbar_created = 0
        # Keep the ctypes callbacks and structures alive: if they are garbage
        # collected while the window exists, the message loop crashes.
        self._wndproc = None
        self._wc: _WNDCLASS | None = None
        self._menu = None
        self._nid: _NOTIFYICONDATA | None = None

    # ------------------------------------------------------------------ setup
    def start(self) -> bool:
        if not available():
            return False
        _configure_user32()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self._added

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        # Explorer sends this broadcast after a restart; the icon has to be
        # re-added when we see it.
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"LocalAvatarFavouritesTray_{os.getpid()}"
        # Assigned a ctypes callback here; the attribute is declared untyped
        # because the callback type is only available at runtime.
        self._wndproc = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM,
        )(self._wnd_proc)  # type: ignore[assignment]
        self._wc = _WNDCLASS()
        self._wc.lpfnWndProc = self._wndproc
        self._wc.hInstance = hinstance
        self._wc.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(self._wc)) \
                and ctypes.get_last_error() != 1410:
            # 1410 == ERROR_CLASS_ALREADY_EXISTS, fine after a restart.
            self._ready.set()
            return

        hwnd = user32.CreateWindowExW(
            0, class_name, self.title, 0, 0, 0, 0, 0,
            ctypes.c_void_p(HWND_MESSAGE), None, hinstance, None,
        )
        if not hwnd:
            self._ready.set()
            return
        self._hwnd = hwnd

        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        nid.uCallbackMessage = TRAY_CALLBACK
        nid.szTip = self.tip
        hicon = self._load_icon()
        if hicon:
            nid.hIcon = hicon
        self._nid = nid

        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            version = _NOTIFYICONDATA()
            version.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
            version.uID = 1
            version.uVersion = NOTIFYICONDATA_VERSION_4
            shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(version))
            self._added = True

        self._menu = user32.CreatePopupMenu()
        user32.AppendMenuW(self._menu, MF_STRING, self.OPEN, "Open")
        user32.AppendMenuW(self._menu, MF_STRING, self.WEAR_LAST, "Wear last avatar")
        user32.AppendMenuW(self._menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(self._menu, MF_STRING, self.QUIT, "Quit")

        self._ready.set()

        msg = wintypes.MSG()
        while self._added:
            if not user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _load_icon(self):
        """Load the tray icon, falling back rather than showing a blank slot."""
        path = self.icon_path
        if path and os.path.exists(path):
            try:
                hicon = ctypes.windll.user32.LoadImageW(
                    None, str(path), IMAGE_ICON, 0, 0, LR_LOADBYORDER,
                )
                if hicon:
                    return hicon
            except Exception:
                pass
        # No usable file: borrow the system application icon so the tray entry
        # is still identifiable rather than an empty slot.
        try:
            return ctypes.windll.user32.LoadIconW(
                None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR)
            )
        except Exception:
            return None

    # -------------------------------------------------------------- callbacks
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        user32 = ctypes.windll.user32
        if msg == TRAY_CALLBACK:
            event = int(lparam) & 0xFFFF
            if event == WM_RBUTTONUP:
                self._show_menu()
            elif event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._fire(self.OPEN)
            return 0
        if msg == WM_COMMAND:
            self._fire(int(wparam) & 0xFFFF)
            return 0
        if self._taskbar_created and msg == self._taskbar_created:
            # Explorer restarted and dropped our icon; put it back.
            if self._nid is not None and not self._added \
                    and ctypes.windll.shell32.Shell_NotifyIconW(
                        NIM_ADD, ctypes.byref(self._nid)):
                self._added = True
            return 0
        if msg == WM_CLOSE:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _fire(self, command: int) -> None:
        if self.on_command is None:
            return
        try:
            self.on_command(command)
        except Exception:
            pass

    def _show_menu(self) -> None:
        user32 = ctypes.windll.user32
        if not self._menu or not self._hwnd:
            return
        # Required so the menu dismisses when it loses focus.
        user32.SetForegroundWindow(self._hwnd)
        point = _POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.TrackPopupMenu(self._menu, 0, point.x, point.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)

    # ---------------------------------------------------------------- control
    def notify(self, title: str, message: str) -> None:
        """Show a balloon notification. Silently does nothing when unavailable."""
        if not self._added or self._nid is None:
            return
        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title
        nid.szInfo = message
        nid.dwInfoFlags = NIIF_INFO
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def current_tip(self) -> str:
        """The tip currently shown. Exposed so tests can read it back."""
        return self._nid.szTip if self._nid is not None else ""

    def set_tip(self, tip: str) -> None:
        self.tip = tip
        if not self._added or self._nid is None:
            return
        self._nid.szTip = tip
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:
            pass

    def stop(self) -> None:
        self._added = False
        if self._nid is not None:
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
        if self._hwnd:
            try:
                ctypes.windll.user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None


__all__ = ["TrayIcon", "available"]
