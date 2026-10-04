"""Local storage for favourites, settings, and thumbnail cache."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import shutil
import time
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path

APP_NAME = "LocalAvatarFavourites"
LEGACY_APP_NAME = "VRChatAvatarFavouritor"

DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
FAVS_FILE = DATA_DIR / "favourites.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "avatar_log.json"
CHANGES_FILE = DATA_DIR / "avatar_changes.json"
THUMBS_DIR = DATA_DIR / "thumbs"

_MIGRATED = False

DEFAULT_SETTINGS = {
    "osc_send_ip": "127.0.0.1",
    "osc_send_port": 9000,
    "osc_receive_port": 9001,
    "auth_token": "",
    "auth_username": "",
    # When false, closing the window hides to the notification area instead.
    "exit_on_close": True,
}


def ensure_dirs() -> None:
    _migrate_legacy()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)


def _migrate_legacy() -> None:
    """One-time copy of data from the tool's previous app name, if any.

    Skipped when DATA_DIR has been redirected elsewhere, which is how the
    self-test points storage at a temporary directory.
    """
    global _MIGRATED
    if _MIGRATED:
        return
    _MIGRATED = True

    default_dir = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    if default_dir != DATA_DIR:
        return

    legacy = default_dir.parent / LEGACY_APP_NAME
    if legacy == DATA_DIR or not legacy.exists():
        return
    if DATA_DIR.exists() and any(DATA_DIR.iterdir()):
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for item in legacy.iterdir():
            dest = DATA_DIR / item.name
            if dest.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
    except OSError:
        pass


def normalize_id(value) -> str:
    return str(value or "").strip().lower()


def merge_favourites(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int]:
    """Merge imported favourites into existing ones, skipping IDs already present.

    Returns the merged list and the number of newly added entries.
    """
    merged: dict[str, dict] = {}
    for entry in existing:
        if isinstance(entry, dict) and entry.get("id"):
            merged[normalize_id(entry["id"])] = dict(entry)
    added = 0
    for entry in incoming or []:
        if not isinstance(entry, dict):
            continue
        avatar_id = normalize_id(entry.get("id"))
        if not avatar_id or avatar_id in merged:
            continue
        new = new_entry(avatar_id)
        # "thumb" is deliberately not copied: it is a local cache filename, so a
        # shared export must never dictate a path on the importing machine.
        # thumb_url is kept and re-resolved on the next metadata refresh.
        for key in ("name", "notes", "author", "release_status", "thumb_url"):
            value = entry.get(key)
            if value:
                new[key] = value
        if isinstance(entry.get("tags"), list):
            new["tags"] = [str(t) for t in entry["tags"]]
        if isinstance(entry.get("platforms"), list):
            new["platforms"] = [str(p) for p in entry["platforms"]]
        new["favorite"] = bool(entry.get("favorite"))
        merged[avatar_id] = new
        added += 1
    return list(merged.values()), added


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def now_iso() -> str:
    return _utcnow_iso()


# ------------------------------------------------------------------ secrets
# DPAPI keeps the VRChat auth token encrypted at rest and scoped to this Windows
# user account, so a copy of settings.json is not enough to impersonate anyone.
CRYPTPROTECT_UI_FORBIDDEN = 0x01
AUTH_TOKEN_ENC_KEY = "auth_token_enc"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def dpapi_available() -> bool:
    # ctypes imports everywhere; only Windows has the DLL-backed windll loader.
    return hasattr(ctypes, "windll")


def _to_blob(data: bytes):
    """Build a DATA_BLOB pointing at a copy of ``data``.

    The returned buffer must be kept alive by the caller for as long as the
    blob is in use, otherwise it can be collected out from under the DLL call.
    """
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _from_blob(blob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect_secret(plaintext: str) -> str:
    """Encrypt with DPAPI. Returns base64, or "" if unavailable."""
    if not plaintext or not dpapi_available():
        return ""
    try:
        crypt32 = ctypes.windll.crypt32
        in_blob, _keepalive = _to_blob(plaintext.encode("utf-8"))
        out = _DataBlob()
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        )
        if not ok:
            return ""
        return base64.b64encode(_from_blob(out)).decode("ascii")
    except Exception:
        return ""


def unprotect_secret(encoded: str) -> str:
    """Decrypt a DPAPI blob. Returns "" if it cannot be read."""
    if not encoded or not dpapi_available():
        return ""
    try:
        raw = base64.b64decode(encoded, validate=True)
        crypt32 = ctypes.windll.crypt32
        in_blob, _keepalive = _to_blob(raw)
        out = _DataBlob()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        )
        if not ok:
            return ""
        return _from_blob(out).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def load_log() -> list[dict]:
    ensure_dirs()
    if not LOG_FILE.exists():
        return []
    try:
        data = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        entries = data.get("entries", []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and e.get("id")]
    except (json.JSONDecodeError, OSError):
        return []


def save_log(entries: list[dict]) -> None:
    ensure_dirs()
    data = {"version": 1, "entries": entries}
    tmp = LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LOG_FILE)


def load_changes() -> list[dict]:
    ensure_dirs()
    if not CHANGES_FILE.exists():
        return []
    try:
        data = json.loads(CHANGES_FILE.read_text(encoding="utf-8"))
        entries = data.get("entries", []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and e.get("player")]
    except (json.JSONDecodeError, OSError):
        return []


def save_changes(entries: list[dict]) -> None:
    ensure_dirs()
    data = {"version": 1, "entries": entries}
    tmp = CHANGES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CHANGES_FILE)


def load_favourites() -> list[dict]:
    ensure_dirs()
    if not FAVS_FILE.exists():
        return []
    try:
        data = json.loads(FAVS_FILE.read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and e.get("id")]
    except (json.JSONDecodeError, OSError):
        return []


def save_favourites(entries: list[dict]) -> None:
    ensure_dirs()
    data = {"version": 1, "entries": entries}
    tmp = FAVS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FAVS_FILE)


def new_entry(avatar_id: str, name: str = "") -> dict:
    return {
        "id": avatar_id,
        "name": name or "Unnamed avatar",
        "notes": "",
        "tags": [],
        "added": _utcnow_iso(),
        "thumb": None,
        "thumb_url": None,
        "author": "",
        "platforms": [],
        "release_status": "",
        "favorite": False,
    }


def sanitize_settings(raw) -> dict:
    """Coerce a loaded settings mapping into a valid, fully-populated dict.

    settings.json is user-writable and hand-editable, so every field is treated
    as untrusted. Previously a non-numeric ``osc_send_port`` reached
    ``int()`` in the Backend constructor and aborted start-up with a traceback
    and no window.
    """
    settings = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return settings
    for key, value in raw.items():
        if key in settings or key == AUTH_TOKEN_ENC_KEY:
            settings[key] = value

    for key, default in (("osc_send_port", 9000), ("osc_receive_port", 9001)):
        raw_port = settings.get(key, default)
        try:
            port = int(raw_port)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            port = default
        if not (0 < port < 65536):
            port = default
        settings[key] = port

    host = settings.get("osc_send_ip")
    settings["osc_send_ip"] = str(host) if isinstance(host, str) and host.strip() else "127.0.0.1"

    for key in ("auth_token", "auth_username"):
        value = settings.get(key)
        settings[key] = value if isinstance(value, str) else ""

    settings["exit_on_close"] = bool(settings.get("exit_on_close", True))

    return settings


def load_settings() -> dict:
    ensure_dirs()
    raw = None
    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = None
    settings = sanitize_settings(raw)

    # Prefer the encrypted token; fall back to a plaintext one written by an
    # older version, which save_settings will upgrade on the next write.
    encrypted = settings.pop(AUTH_TOKEN_ENC_KEY, "")
    if isinstance(encrypted, str) and encrypted:
        decrypted = unprotect_secret(encrypted)
        if decrypted:
            settings["auth_token"] = decrypted
    return settings


def save_settings(settings: dict) -> None:
    ensure_dirs()
    merged = sanitize_settings(settings)
    token = merged.get("auth_token") or ""
    # Never write the token in the clear: encrypt it and drop the plain field.
    merged.pop("auth_token", None)
    encrypted = protect_secret(token) if token else ""
    if encrypted:
        merged[AUTH_TOKEN_ENC_KEY] = encrypted
    elif token:
        # DPAPI failed. Refuse to persist an unencrypted credential rather than
        # silently downgrading; the session simply will not survive a restart.
        merged["auth_token_error"] = "token could not be encrypted; not saved"
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def sanitize_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return value or "avatar"


def resolve_thumb(name) -> Path | None:
    """Resolve a stored thumbnail name to a path inside the thumbnail cache.

    Returns None for anything that is not a plain filename inside THUMBS_DIR.
    Thumb names come from favourites.json and from imported files, so a crafted
    ``thumb`` value such as ``C:\\Windows\\win.ini`` would otherwise make the app
    read an arbitrary local file and hand it to the UI as base64.

    Anything containing a separator is rejected outright rather than reduced to
    its final component: silently rewriting ``../../x.png`` to ``thumbs/x.png``
    would still turn the cache into a probe for files that happen to be there.
    """
    if not name or not isinstance(name, str):
        return None
    if "/" in name or "\\" in name or ":" in name or name in (".", ".."):
        return None
    try:
        root = THUMBS_DIR.resolve()
        candidate = (root / name).resolve()
    except (OSError, ValueError):
        return None
    return candidate if candidate.parent == root else None


def thumb_file_path(avatar_id: str, ext: str = "png") -> Path:
    return THUMBS_DIR / f"{sanitize_filename(avatar_id)}.{ext}"


def delete_thumb(name) -> bool:
    """Remove one cached thumbnail. Returns True when a file went away."""
    path = resolve_thumb(name)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def prune_thumbs(keep: set[str], max_files: int = 2000,
                 grace_seconds: float = 60.0) -> tuple[int, int]:
    """Drop thumbnails no longer referenced by a favourite.

    Deletes orphaned files outright, then trims the cache by oldest-first so it
    cannot grow without bound. Files written within ``grace_seconds`` are left
    alone: an avatar deleted moments ago may still be sitting in the undo buffer,
    and restoring it must find its image intact.

    Returns (orphans_removed, trimmed).
    """
    removed = 0
    trimmed = 0
    now = time.time()
    try:
        files = [p for p in THUMBS_DIR.iterdir() if p.is_file()]
    except OSError:
        return 0, 0

    survivors: list[Path] = []
    for path in files:
        if path.name in keep:
            survivors.append(path)
            continue
        try:
            if now - path.stat().st_mtime < grace_seconds:
                survivors.append(path)
                continue
        except OSError:
            survivors.append(path)
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            survivors.append(path)

    if len(survivors) > max_files:
        try:
            survivors.sort(key=lambda p: p.stat().st_mtime)
        except OSError:
            pass
        for path in survivors[: len(survivors) - max_files]:
            try:
                path.unlink()
                trimmed += 1
            except OSError:
                pass
    return removed, trimmed


def clear_thumbs() -> int:
    """Delete every cached thumbnail. Returns how many were removed."""
    removed = 0
    try:
        files = [p for p in THUMBS_DIR.iterdir() if p.is_file()]
    except OSError:
        return 0
    for path in files:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
