"""Minimal VRChat API client used for optional login and avatar metadata.

Only the pieces we need: login (with TOTP 2FA), fetch one avatar, and
download its thumbnail image. All requests go through a cookie jar so the
auth token is handled automatically.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from http.cookiejar import Cookie, CookieJar
from urllib.request import HTTPCookieProcessor, build_opener

try:
    from version import __version__
except Exception:  # pragma: no cover - defensive
    __version__ = "0.0.0"

API_BASE = "https://api.vrchat.cloud/api/1"
USER_AGENT = f"LocalAvatarFavourites/{__version__} (local favourites tool)"

# Avatar thumbnails are ~200x300. This is generous headroom, not a target.
MAX_IMAGE_BYTES = 4 * 1024 * 1024


class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


class TwoFactorRequired(ApiError):
    def __init__(self, methods: list[str] | None = None) -> None:
        self.methods = [str(m) for m in (methods or []) if isinstance(m, str)]
        super().__init__("Two-factor authentication required.")


def _auth_cookie(token: str) -> Cookie:
    return Cookie(
        version=0,
        name="auth",
        value=token,
        port=None,
        port_specified=False,
        domain="api.vrchat.cloud",
        domain_specified=True,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        # Only ever sent over HTTPS.
        secure=True,
        expires=None,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


class VRCApi:
    def __init__(self, token: str = "") -> None:
        self._jar = CookieJar()
        if token:
            self._jar.set_cookie(_auth_cookie(token))
        self._opener = build_opener(HTTPCookieProcessor(self._jar))

    # ------------------------------------------------------------------ tokens
    @property
    def token(self) -> str:
        for cookie in self._jar:
            if cookie.name == "auth":
                return cookie.value or ""
        return ""

    def is_logged_in(self) -> bool:
        return bool(self.token)

    # ------------------------------------------------------------------ low-level
    def _request(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        basic: tuple[str, str] | None = None,
        timeout: int = 20,
    ):
        url = API_BASE + path
        body = None
        headers = {"User-Agent": USER_AGENT}
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if basic is not None:
            raw = f"{basic[0]}:{basic[1]}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise ApiError(f"Network error: {exc.reason}") from exc

    @staticmethod
    def _json(raw: bytes) -> dict:
        try:
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _raise_api_error(raw: bytes, status: int) -> None:
        payload = VRCApi._json(raw)
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message")
        elif isinstance(err, str):
            message = err
        else:
            message = None
        message = message or payload.get("message")
        if status == 401:
            raise AuthError(message or "Invalid credentials.")
        if status == 403:
            raise AuthError(message or "Access denied.")
        if status == 404:
            raise ApiError(message or "Not found.")
        if status == 429:
            raise ApiError(message or "Rate limited (429). Wait a moment and retry.")
        raise ApiError(message or f"VRChat API error ({status}).")

    # ------------------------------------------------------------------ auth
    TWO_FACTOR_ENDPOINTS = {
        "totp": "/auth/twofactorauth/totp/verify",
        "emailotp": "/auth/twofactorauth/emailotp/verify",
        "otp": "/auth/twofactorauth/otp/verify",
    }

    def login(self, username: str, password: str) -> dict:
        """Log in with username/password. Returns user dict. Raises
        TwoFactorRequired (with the available methods) if 2FA is needed."""
        status, raw = self._request("GET", "/auth/user", basic=(username, password))
        payload = self._json(raw)
        if status == 200:
            required = payload.get("requiresTwoFactorAuth")
            if required:
                methods = required if isinstance(required, list) else [required]
                raise TwoFactorRequired([str(m) for m in methods])
            return payload
        self._raise_api_error(raw, status)
        return {}  # pragma: no cover

    def verify_2fa(self, code: str, method: str = "totp") -> dict:
        """Verify a 2FA code. ``method`` is one of totp / emailotp / otp."""
        path = self.TWO_FACTOR_ENDPOINTS.get(
            (method or "totp").lower(), self.TWO_FACTOR_ENDPOINTS["totp"])
        status, raw = self._request("POST", path, data={"code": code})
        payload = self._json(raw)
        if status == 200:
            return payload
        self._raise_api_error(raw, status)
        return {}  # pragma: no cover

    def verify_totp(self, code: str) -> dict:
        """Backwards-compatible TOTP verification."""
        return self.verify_2fa(code, "totp")

    def verify(self) -> dict:
        """Check whether the stored token is still valid."""
        status, raw = self._request("GET", "/auth/user")
        if status == 200:
            payload = self._json(raw)
            if not payload.get("requiresTwoFactorAuth"):
                return payload
        self._raise_api_error(raw, status)
        return {}  # pragma: no cover

    # ------------------------------------------------------------------ avatars
    def get_avatar(self, avatar_id: str) -> dict | None:
        """Fetch one avatar.

        Raises AuthError on 401 and returns None only for a genuine 404. The two
        used to collapse into None, so an expired token looked identical to a
        private avatar and the caller marked everything private.
        """
        status, raw = self._request("GET", f"/avatars/{avatar_id}")
        if status == 200:
            return self._json(raw)
        if status == 404:
            return None
        self._raise_api_error(raw, status)
        return None  # pragma: no cover

    def download_image(self, url: str, dest_path: str, max_bytes: int = MAX_IMAGE_BYTES) -> bool:
        """Download an image to disk (auth cookie sent automatically).

        Capped at ``max_bytes``: without a ceiling a hostile or broken URL can fill
        the disk, since nothing here bounds the response length.
        """
        if not str(url or "").lower().startswith(("http://", "https://")):
            return False
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        written = 0
        try:
            with self._opener.open(req, timeout=30) as resp:
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    return False
                with open(dest_path, "wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            fh.close()
                            try:
                                os.remove(dest_path)
                            except OSError:
                                pass
                            return False
                        fh.write(chunk)
            return True
        except (urllib.error.URLError, OSError):
            return False
