"""Local Avatar Favourites - desktop shell.

The interface is an HTML/CSS/JS frontend rendered in a native webview
(pywebview). All logic lives in ``backend.Backend`` and is reached from the
frontend through ``window.pywebview.api``.
"""

from __future__ import annotations

import os
import sys

import webview

from backend import Backend

APP_TITLE = "Local Avatar Favourites"


def _web_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "web")


def main() -> None:
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


if __name__ == "__main__":
    main()
