"""Local storage for favourites, settings, and thumbnail cache."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "LocalAvatarFavourites"

DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
FAVS_FILE = DATA_DIR / "favourites.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "avatar_log.json"
CHANGES_FILE = DATA_DIR / "avatar_changes.json"
THUMBS_DIR = DATA_DIR / "thumbs"

DEFAULT_SETTINGS = {
    "osc_send_ip": "127.0.0.1",
    "osc_send_port": 9000,
    "osc_receive_port": 9001,
    "auth_token": "",
    "auth_username": "",
    "auth_expires": 0,
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return _utcnow_iso()


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


def load_settings() -> dict:
    ensure_dirs()
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                settings.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def save_settings(settings: dict) -> None:
    ensure_dirs()
    merged = dict(DEFAULT_SETTINGS)
    merged.update(settings)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def sanitize_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return value or "avatar"


def thumb_file_path(avatar_id: str, ext: str = "png") -> Path:
    return THUMBS_DIR / f"{sanitize_filename(avatar_id)}.{ext}"


def load_text_or_default(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def human_timestamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso or ""


def unix_now() -> float:
    return time.time()
