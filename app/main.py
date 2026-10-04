"""Local Avatar Favourites - desktop shell.

The interface is an HTML/CSS/JS frontend rendered in a native webview
(pywebview). All logic lives in ``backend.Backend`` and is reached from the
frontend through ``window.pywebview.api``.
"""

from __future__ import annotations

import os
import sys

# The app directory is a script directory rather than a package: sibling modules
# are imported flat (import storage, from api import ...). Put this directory on
# sys.path before those imports so `python app\main.py`, `python -m app.main` and
# the PyInstaller entry point all work.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview

from backend import Backend
from tray import TrayIcon
from tray import available as tray_available
from version import __version__

APP_TITLE = "Local Avatar Favourites"
TRAY_IDLE = 0.0


def _web_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "web")


def _icon_path() -> str | None:
    """Locate icon.ico next to the bundled data, for the tray icon."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        candidate = os.path.join(base, "assets", "icon.ico")
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidate = os.path.join(root, "assets", "icon.ico")
    return candidate if os.path.exists(candidate) else None


class _Shell:
    """Glue between the webview window and the tray icon."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.window = None
        self.tray: TrayIcon | None = None
        self.quitting = False

    def attach(self, window) -> None:
        self.window = window
        self.backend.attach_window(window)

    def start_tray(self) -> bool:
        if not tray_available():
            return False
        self.tray = TrayIcon(APP_TITLE, icon_path=_icon_path())
        self.tray.on_command = self._on_tray  # type: ignore[assignment]
        try:
            return self.tray.start()
        except Exception:
            self.tray = None
            return False

    def _on_tray(self, command: int) -> None:
        if command == TrayIcon.OPEN:
            self.show()
        elif command == TrayIcon.WEAR_LAST:
            self._wear_last()
        elif command == TrayIcon.QUIT:
            self.quit()

    def _wear_last(self) -> None:
        try:
            self.backend.wear_last()
        except Exception:
            pass

    def show(self) -> None:
        if self.window is None:
            return
        try:
            self.window.restore()
        except Exception:
            pass
        try:
            self.window.show()
        except Exception:
            pass

    def quit(self) -> None:
        self.quitting = True
        if self.tray is not None:
            self.tray.stop()
        try:
            if self.window is not None:
                self.window.destroy()
        except Exception:
            pass

    def on_closing(self) -> bool:
        """Hide to the tray unless the user asked to exit.

        Polarity matters and is easy to get backwards. pywebview's
        ``Event.set()`` treats a handler returning **False** as "cancel", and
        the WinForms backend does ``args.Cancel = True`` when that happens. So
        returning True here would let the window close -- which is exactly what
        it did before this was corrected.
        """
        if self.quitting or self.tray is None or not self.tray._added:
            return True  # nothing to fall back to: let it close
        if self.backend.settings.get("exit_on_close", True):
            return True  # user chose to exit

        try:
            if self.window is not None:
                self.window.hide()
        except Exception:
            return True
        try:
            self.tray.notify(
                APP_TITLE,
                "Still running in the notification area. Right-click the icon "
                "for Open, Wear last avatar and Quit. To make closing always "
                "exit, turn off 'hide to tray' in Settings.",
            )
        except Exception:
            pass
        return False  # cancel the close


def main() -> int:
    # CI runs the packaged executable with --selftest. A bundle that is missing
    # an import fails here instead of on the user's first launch, which matters
    # because the spec deliberately excludes a long list of modules.
    if "--selftest" in sys.argv:
        backend = Backend(start_services=False)
        # One poll so the reported source status is real rather than the
        # unpolled initial state.
        backend._cache.poll()
        icon = _icon_path()
        frozen = " - frozen bundle OK" if getattr(sys, "frozen", False) else ""
        print(f"Local Avatar Favourites {__version__}{frozen}")
        print(f"  entries loaded : {len(backend.entries)}")
        print(f"  discovery      : {backend.discovery_state()['sources']}")
        print(f"  tray available : {tray_available()}")
        # A blank tray icon means this file was missing from the bundle.
        print(f"  tray icon      : {icon or 'MISSING'}")
        if icon and not os.path.exists(icon):
            return 1
        return 0

    backend = Backend()
    shell = _Shell(backend)

    index = os.path.join(_web_dir(), "index.html")
    window = webview.create_window(
        APP_TITLE,
        index,
        js_api=backend,
        width=1120,
        height=720,
        min_size=(760, 560),
        background_color="#0b0c10",
        on_top=False,
    )
    shell.attach(window)

    def on_loaded() -> None:
        # Start the tray only once the window exists, so Open has something to
        # restore, and the icon never outlives a failed launch.
        shell.start_tray()
        backend.set_tray(shell.tray)

    window.events.loaded += on_loaded  # type: ignore[union-attr]
    window.events.closing += shell.on_closing  # type: ignore[union-attr]

    try:
        webview.start()
    finally:
        backend.stop()
        if shell.tray is not None:
            shell.tray.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
