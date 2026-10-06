"""Loopback HTTP server that streams cached thumbnails to the webview.

Why this exists
---------------
The webview cannot read the thumbnail cache directly, so the original design
shipped every image across the bridge as a base64 data URI. That is fine for a
couple of dozen avatars and ruinous for a few thousand: each base64 string is
about a third larger than the PNG, and it then lives in three places at once --
the Python string, the bridge's JSON payload, and the ``src`` attribute of the
``<img>`` that displays it. On top of that the webview keeps a decoded bitmap
for every image that has ever been painted, and a 512x512 avatar thumbnail
decodes to roughly a megabyte. A few thousand favourites therefore pinned
multiple gigabytes for as long as the elements existed.

Pointing ``<img src>`` at a real URL fixes all of it at once. The bytes never
enter JavaScript at all, the ``src`` attribute is a 60-character string, and
Chromium is free to decode on demand and discard the decoded frame for anything
scrolled out of view, using its own disk cache in between.

Security
--------
Bound to 127.0.0.1 only, so no firewall prompt and nothing off-machine. The path
must carry a per-run random token, which keeps every other process and every
other website in the user's browser from reading the cache by guessing a
port. Only names that :func:`storage.resolve_thumb` accepts are served, which
means plain filenames inside the thumbnail directory and nothing else.

Everything here is best-effort. If the server cannot start, the caller keeps
using the base64 bridge path and nothing is lost except the memory saving.
"""

from __future__ import annotations

import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import storage

# Read size when streaming. Small enough that a slow client cannot pin much of
# the file in the server's own buffers, large enough not to matter for a
# few-hundred-kilobyte PNG.
CHUNK_BYTES = 64 * 1024

# Thumbnails are content-addressed by filename: a new download writes a new
# name rather than overwriting the old one, so a cached copy can never be stale.
CACHE_CONTROL = "private, max-age=31536000, immutable"

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so the webview can keep the connection and revalidate cheaply.
    protocol_version = "HTTP/1.1"
    server_version = "LocalAvatarFavourites"
    sys_version = ""

    # Method names are fixed by BaseHTTPRequestHandler, so they cannot be
    # renamed to satisfy the naming rule.
    def do_GET(self) -> None:
        self._serve(body=True)

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def _serve(self, body: bool) -> None:
        server: _Server = self.server  # type: ignore[assignment]
        parts = urlsplit(self.path).path.split("/")
        # /<token>/<name> exactly: no trailing segments, no directory listings.
        if (len(parts) != 3 or not parts[1]
                or not secrets.compare_digest(parts[1], server.token)):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path = storage.resolve_thumb(unquote(parts[2]))
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            stat = path.stat()
            handle = path.open("rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        with handle:
            etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", CACHE_CONTROL)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type",
                             IMAGE_MIME.get(path.suffix.lower(), "image/png"))
            self.send_header("Content-Length", str(stat.st_size))
            self.send_header("Cache-Control", CACHE_CONTROL)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", self.date_time_string(int(stat.st_mtime)))
            self.end_headers()
            if not body:
                return
            remaining = stat.st_size
            while remaining > 0:
                chunk = handle.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, *args) -> None:
        """Silence the default stderr access log.

        Chromium revalidates every thumbnail it still displays, so a per-request
        log line is pure noise for a request nobody made by hand.
        """


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # The socket is closed in stop(). Refusing to reuse the address would make a
    # restart fail for no benefit, since the port is ephemeral anyway.
    allow_reuse_address = True
    token = ""


class ThumbServer:
    """A loopback-only file server for the thumbnail cache.

    ``base_url`` is "" until :meth:`start` succeeds and again after :meth:`stop`,
    which is how callers detect that the base64 fallback is required.
    """

    def __init__(self) -> None:
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None
        self._base = ""
        self._token = ""

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def token(self) -> str:
        return self._token

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        """Bind and serve. Returns True when ``base_url`` is usable."""
        if self._httpd is not None:
            return True
        try:
            httpd = _Server(("127.0.0.1", 0), _Handler)
        except OSError:
            return False
        token: str = secrets.token_urlsafe(18)
        self._token = token
        httpd.token = token
        # server_address is typed loosely, and this is always the loopback
        # address we just bound, so spell it out rather than trust it.
        port: int = httpd.server_address[1]
        host = "127.0.0.1"
        try:
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.5},
                name="thumb-server",
                daemon=True,
            )
            thread.start()
        except Exception:
            try:
                httpd.server_close()
            except OSError:
                pass
            return False
        self._httpd = httpd
        self._thread = thread
        self._base = f"http://{host}:{port}/{token}"
        return True

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        self._thread = None
        self._base = ""
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass


__all__ = ["CACHE_CONTROL", "CHUNK_BYTES", "IMAGE_MIME", "ThumbServer"]
