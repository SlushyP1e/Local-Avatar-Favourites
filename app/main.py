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
from version import __version__

APP_TITLE = "Local Avatar Favourites"


def _web_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "web")


def main() -> int:
    # CI runs the packaged executable with --selftest. A bundle that is missing
    # an import fails here instead of on the user's first launch, which matters
    # because the spec deliberately excludes a long list of modules.
    if "--selftest" in sys.argv:
        backend = Backend(start_services=False)
        # One poll so the reported source status is real rather than the
        # unpolled initial state.
        backend._cache.poll()
        frozen = " - frozen bundle OK" if getattr(sys, "frozen", False) else ""
        print(f"Local Avatar Favourites {__version__}{frozen}")
        print(f"  entries loaded : {len(backend.entries)}")
        print(f"  discovery      : {backend.discovery_state()['sources']}")
        return 0

    backend = Backend()
    index = os.path.join(_web_dir(), "index.html")
    window = webview.create_window(
        APP_TITLE,
        index,
        js_api=backend,
        width=1120,
        height=720,
        min_size=(760, 560),
        background_color="#0b0c10",
    )
    backend.attach_window(window)
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
