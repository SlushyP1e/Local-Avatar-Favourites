"""Local Avatar Favourites - desktop shell.

The interface is an HTML/CSS/JS frontend rendered in a native webview
(pywebview). All logic lives in ``backend.Backend`` and is reached from the
frontend through ``window.pywebview.api``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

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
from vrcdetails import (
    DEFAULT_AVATAR_IDS,
    DEFAULT_AVATAR_NAMES,
    is_default_avatar,
    is_default_avatar_name,
)

APP_TITLE = "Local Avatar Favourites"
TRAY_IDLE = 0.0


def _web_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "web")


# Markers that must be present in the bundled stylesheet. PyInstaller reuses
# build/ between runs, and a stale bundle looks exactly like a broken feature:
# the UI silently loses behaviour with no error anywhere.
CSS_MARKERS = ("data-motion", "view-in-next", "prefers-reduced-motion",
               ".group-picker", ".chip-count", ".more-row")

# Element ids app.js binds to at startup. A missing one throws in wire(), which
# would leave every later listener unwired with no useful error.
HTML_MARKERS = ("group-chips", "d-group", "bulk-group", "prompt-list")


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
    # Wear-request diagnostics are opt-in so normal GUI launches stay quiet.
    # When running from a terminal, set this to 1 to see timestamped source and
    # reason fields for OSC, client-log, and API-fallback decisions.
    if os.environ.get("LOCAL_AVATAR_FAVOURITES_DEBUG") == "1" and sys.stderr is not None:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    # CI, and anyone debugging a packaged build, runs the executable with
    # --selftest. A bundle missing an import or shipping a stale stylesheet fails
    # here rather than looking like a broken feature.
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

        # The thumbnail server is what keeps a large collection from costing
        # gigabytes of RAM. Nothing else in a bundle check touches it, so a build
        # that shipped without it would look perfect here and then quietly fall
        # back to base64 for the rest of the session.
        thumbs = backend._thumbs
        try:
            thumb_ok = thumbs.start() and bool(thumbs.base_url)
        except Exception:
            thumb_ok = False
        finally:
            thumbs.stop()
        print(f"  thumb server   : {'loopback OK' if thumb_ok else 'UNAVAILABLE'}")
        if not thumb_ok:
            return 1

        print(f"  tray available : {tray_available()}")
        print(f"  tray icon file : {icon or 'MISSING'}")
        # A stale bundle looks identical to a broken feature, so verify the
        # stylesheet that actually shipped.
        css_path = os.path.join(_web_dir(), "style.css")
        missing = []
        try:
            css = Path(css_path).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"  css            : UNREADABLE ({exc})")
            missing = list(CSS_MARKERS)
        else:
            missing = [m for m in CSS_MARKERS if m not in css]
            print(f"  css            : {len(css)} bytes, "
                  f"{'all markers present' if not missing else 'MISSING ' + ', '.join(missing)}")

        # Same reasoning for the markup. An index.html without these would leave
        # app.js wiring listeners to elements that are not there, and every group
        # control would fail silently.
        html_missing: list[str] = []
        try:
            html = Path(os.path.join(_web_dir(), "index.html")).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"  html           : UNREADABLE ({exc})")
            html_missing = list(HTML_MARKERS)
        else:
            html_missing = [m for m in HTML_MARKERS if m not in html]
            print(f"  html           : {len(html)} bytes, "
                  f"{'all markers present' if not html_missing else 'MISSING ' + ', '.join(html_missing)}")

        # The curated default-avatar list ships inside the frozen bytecode
        # archive, so it cannot be probed for like a data file. Import it and
        # assert the sentinels are actually filterable: a bundle that somehow
        # lost or truncated the module would silently stop filtering defaults
        # and look exactly like the feature being broken.
        filter_ok = (
            len(DEFAULT_AVATAR_IDS) > 200
            and is_default_avatar("avtr_c38a1615-5bf5-42b4-84eb-a8b6c37cbd11")
            and is_default_avatar_name("robot")
            and not is_default_avatar_name("Fallback")
        )
        print(f"  default filter : {len(DEFAULT_AVATAR_IDS)} ids, "
              f"{len(DEFAULT_AVATAR_NAMES)} names, "
              f"{'operational' if filter_ok else 'BROKEN'}")

        # Backend.__init__ already pruned, so this reports what survived rather
        # than re-pruning. Non-zero here would mean a default got through.
        remaining = sum(1 for e in backend.log if is_default_avatar(e.get("id")))
        remaining += sum(1 for c in backend.changes
                         if is_default_avatar_name(c.get("avatar")))
        print(f"  defaults in log: {remaining} (0 expected)")

        if icon and not os.path.exists(icon):
            print("  tray icon file : MISSING")
            return 1
        if missing:
            print("  ERROR: bundled stylesheet is stale -- rebuild with --clean")
            return 1
        if html_missing:
            print("  ERROR: bundled markup is stale -- rebuild with --clean")
            return 1
        if not filter_ok:
            print("  ERROR: curated default-avatar list missing or unusable in this bundle")
            return 1
        if remaining:
            print(f"  ERROR: {remaining} default avatars survived the filter")
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
