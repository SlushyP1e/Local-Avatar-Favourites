"""Python backend bridge exposed to the web UI through pywebview.

Owns OSC, the VRChat API client, and local storage. Every public method here
is callable from JavaScript as ``window.pywebview.api.<method>(...)`` and must
return JSON-serialisable data. Private helpers (leading underscore) are not
exposed to the UI.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

import webview

import storage
from api import ApiError, AuthError, TwoFactorRequired, VRCApi
from osc import OSCBridge
from version import __version__
from versions import is_newer
from vrcache import VRCacheWatcher
from vrclog import VRCLogWatcher

RELEASES_API = "https://api.github.com/repos/SlushyP1e/Local-Avatar-Favourites/releases/latest"
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

MAX_LOG_ENTRIES = 800
MAX_CHANGE_ENTRIES = 1000
DISCOVERY_FEED_MAX = 300

# How long to wait for VRChat to broadcast the avatar back before assuming the
# change was refused. Large avatars can take a while to download.
WEAR_CONFIRM_TIMEOUT = 6.0

# Where a logged avatar id came from. Surfaced in the UI so it is obvious which
# source is actually producing discoveries.
SOURCE_OSC = "osc"
SOURCE_LOG = "log"
SOURCE_CACHE = "cache-db"


def _set_clipboard(text: str) -> bool:
    """Copy text to the Windows clipboard without pulling in a GUI toolkit."""
    if not text:
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        cf_unicode = 13
        gmem_moveable = 0x0002

        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

        payload = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(gmem_moveable, len(payload))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, payload, len(payload))
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(cf_unicode, handle)
        finally:
            user32.CloseClipboard()
        return True
    except Exception:
        return False


class Backend:
    """Bridge exposed to the web UI.

    ``start_services=False`` builds the object without binding the OSC socket,
    reading VRChat's real files or starting the poll thread, so tests can drive
    the logic without touching the user's install.
    """

    def __init__(self, start_services: bool = True,
                 cache: VRCacheWatcher | None = None) -> None:
        self._lock = threading.RLock()
        self._window = None
        self._pending_api: VRCApi | None = None
        self._pending_2fa_methods: list[str] = []
        self._pending_2fa_method: str = ""
        self._running: set[str] = set()
        self.status = ""
        self._revs = {"entries": 0, "logs": 0, "changes": 0}
        # Set when the stored token stops working, so the UI can ask for a fresh
        # login instead of silently reporting every avatar as private.
        self.session_expired = False
        # (avatar_id, monotonic timestamp) of an unconfirmed wear request.
        self._pending_wear: tuple[str, float] | None = None

        self.entries: list[dict] = storage.load_favourites()
        self._index: dict[str, dict] = {}
        self._reindex()
        self.log: list[dict] = storage.load_log()
        self.changes: list[dict] = storage.load_changes()
        self.settings: dict = storage.load_settings()
        self.api = VRCApi(self.settings.get("auth_token", ""))
        self.osc = OSCBridge(
            send_ip=self.settings.get("osc_send_ip", "127.0.0.1"),
            send_port=int(self.settings.get("osc_send_port", 9000)),
            receive_port=int(self.settings.get("osc_receive_port", 9001)),
        )
        self.osc.add_avatar_change_listener(self._on_avatar_change)
        if start_services:
            self.osc.start()

        self._watcher = VRCLogWatcher()
        self._cache = cache if cache is not None else VRCacheWatcher()
        if start_services:
            try:
                # Record the existing backlog as already-seen so starting the app
                # does not emit tens of thousands of "new" discoveries at once.
                self._cache.bootstrap()
            except Exception:
                pass
        self._stopped = False
        if start_services:
            threading.Thread(target=self._prune_loop, daemon=True).start()
            threading.Thread(target=self._log_loop, daemon=True).start()

    def _prune_loop(self) -> None:
        # Give the UI a moment to come up before touching the thumbnail cache.
        if self._stopped:
            return
        time.sleep(5.0)
        self.prune_orphans_on_start()

    def stop(self) -> None:
        self._stopped = True
        self.osc.stop()

    def _log_loop(self) -> None:
        while not self._stopped:
            try:
                events = self._watcher.poll()
                if events:
                    self._ingest_log_events(events)
            except Exception:
                pass
            try:
                # The local cache is the richest source of real ids; the text log
                # is a weak fallback that mostly sees our own avatar.
                for avatar_id in self._cache.poll():
                    self._record_log(avatar_id, source=SOURCE_CACHE)
            except Exception:
                pass
            try:
                self._expire_pending_wear()
            except Exception:
                pass
            time.sleep(1.0)

    def _ingest_log_events(self, events: list[dict]) -> None:
        with self._lock:
            changed_log = False
            changed_changes = False
            for event in events:
                if event["type"] == "avatar-id":
                    if self._record_log(event["id"], event.get("time", ""),
                                        save=False, source=SOURCE_LOG):
                        changed_log = True
                elif event["type"] == "avatar-change" and self._record_change(
                    event.get("player", ""),
                    event.get("avatar", ""),
                    event.get("time", "")):
                    changed_changes = True
            if changed_log:
                storage.save_log(self.log)
            if changed_changes:
                storage.save_changes(self.changes)

    def attach_window(self, window) -> None:
        self._window = window

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _norm_id(avatar_id) -> str:
        return storage.normalize_id(avatar_id)

    def _touch(self, section: str) -> None:
        self._revs[section] = self._revs.get(section, 0) + 1

    def _reindex(self) -> None:
        """Rebuild the id -> entry map after the list is replaced wholesale."""
        self._index = {}
        for entry in self.entries:
            avatar_id = self._norm_id(entry.get("id"))
            if avatar_id:
                self._index[avatar_id] = entry

    def _index_add(self, entry: dict) -> None:
        avatar_id = self._norm_id(entry.get("id"))
        if avatar_id:
            self._index[avatar_id] = entry

    def _entry(self, avatar_id: str) -> dict | None:
        # Was a linear scan, which made save_all_logs O(n^2) over the list.
        return self._index.get(self._norm_id(avatar_id))

    def _set_status(self, text: str) -> None:
        self.status = text

    def _on_avatar_change(self, avatar_id: str) -> None:
        with self._lock:
            pending = self._pending_wear
            # VRChat echoing the id back is the only confirmation of a wear.
            if pending and pending[0] == avatar_id:
                self._pending_wear = None
        self._record_log(avatar_id, source=SOURCE_OSC)
        entry = self._entry(avatar_id)
        name = f"Now wearing {entry['name']}." if entry and entry.get("name") \
            else f"Now wearing {avatar_id}."
        self._set_status(name)

    def _record_log(self, avatar_id: str, when: str = "", save: bool = True,
                     source: str = SOURCE_OSC) -> bool:
        """Record that an avatar id was seen.

        ``source`` records where the id came from. De-duplication uses a
        minute-resolution bucket rather than the exact timestamp: the same
        avatar change arrives twice in normal operation, once over OSC and once
        via the log file, with different timestamps, so the old exact comparison
        counted every change twice.
        """
        avatar_id = (avatar_id or "").strip().lower()
        if not avatar_id:
            return False
        stamp = when or storage.now_iso()
        bucket = stamp[:16]
        with self._lock:
            entry = next((e for e in self.log if e.get("id") == avatar_id), None)
            if entry:
                if entry.get("seen_bucket") == bucket:
                    return False
                entry["seen_bucket"] = bucket
                entry["last_seen"] = stamp
                entry["count"] = int(entry.get("count", 1)) + 1
                if entry.get("source") != source:
                    entry["source"] = source
            else:
                self.log.append({
                    "id": avatar_id,
                    "name": "",
                    "first_seen": stamp,
                    "last_seen": stamp,
                    "seen_bucket": bucket,
                    "count": 1,
                    "private": False,
                    "source": source,
                })
            if len(self.log) > MAX_LOG_ENTRIES:
                self.log.sort(key=lambda e: e.get("last_seen", ""))
                self.log = self.log[-MAX_LOG_ENTRIES:]
            if save:
                storage.save_log(self.log)
            self._touch("logs")
        return True

    def _record_change(self, player: str, avatar: str, when: str = "") -> bool:
        player = (player or "").strip()
        avatar = (avatar or "").strip()
        if not player or not avatar:
            return False
        stamp = when or storage.now_iso()
        with self._lock:
            entry = next((e for e in self.changes
                          if e.get("player") == player and e.get("avatar") == avatar), None)
            if entry:
                if entry.get("last_seen") == stamp:
                    return False
                entry["last_seen"] = stamp
                entry["count"] = int(entry.get("count", 1)) + 1
            else:
                self.changes.append({
                    "player": player,
                    "avatar": avatar,
                    "first_seen": stamp,
                    "last_seen": stamp,
                    "count": 1,
                })
            if len(self.changes) > MAX_CHANGE_ENTRIES:
                self.changes.sort(key=lambda e: e.get("last_seen", ""))
                self.changes = self.changes[-MAX_CHANGE_ENTRIES:]
            self._touch("changes")
        return True

    def _log_sorted(self) -> list[dict]:
        return sorted((dict(e) for e in self.log),
                      key=lambda e: e.get("last_seen", ""), reverse=True)

    def _changes_sorted(self) -> list[dict]:
        return sorted((dict(e) for e in self.changes),
                      key=lambda e: e.get("last_seen", ""), reverse=True)

    def _snapshot_entries(self) -> list[dict]:
        with self._lock:
            return [dict(entry) for entry in self.entries]

    # ------------------------------------------------------------------ state
    def get_state(self, revs: dict | None = None) -> dict:
        revs = revs if isinstance(revs, dict) else None
        with self._lock:
            def changed(section: str) -> bool:
                return revs is None or revs.get(section) != self._revs.get(section)

            state = {
                "version": __version__,
                "current_avatar_id": self.osc.current_avatar_id,
                "status": self.status,
                "logged_in": self.api.is_logged_in(),
                "session_expired": self.session_expired,
                "username": self.settings.get("auth_username", ""),
                "pending_2fa": self._pending_api is not None,
                "revs": dict(self._revs),
                "discovery": self.discovery_state(),
                "osc": {
                    "listening": self.osc.listening,
                    "error": self.osc.error,
                    "seen_traffic": self.osc.seen_traffic(),
                },
            }
            state["entries"] = self._snapshot_entries() if changed("entries") else None
            state["logs"] = self._log_sorted() if changed("logs") else None
            state["changes"] = self._changes_sorted() if changed("changes") else None
            return state

    def get_settings(self) -> dict:
        return {
            "version": __version__,
            "osc_send_port": self.settings.get("osc_send_port", 9000),
            "osc_receive_port": self.settings.get("osc_receive_port", 9001),
            "username": self.settings.get("auth_username", ""),
            "logged_in": self.api.is_logged_in(),
            "session_expired": self.session_expired,
            "pending_2fa": self._pending_api is not None,
            "twofa_methods": list(self._pending_2fa_methods),
            "twofa_method": self._pending_2fa_method,
            "discovery": self.discovery_state(),
        }

    def discovery_state(self) -> dict:
        """Which local source is producing avatar ids, and how much it has seen.

        Reported to the UI so a working source looks working and a degraded one
        is visible, rather than both appearing as "nothing new".
        """
        status = dict(self._cache.status)
        # The text log remains the lowest-priority source.
        status.setdefault(SOURCE_LOG, "ok")
        return {
            "sources": status,
            "backlog": self._cache.backlog_size(),
            "db_path": str(self._cache.db_path),
        }

    def get_backlog(self, limit: int = 60, offset: int = 0) -> dict:
        """A page of previously-seen ids, newest first."""
        ids = self._cache.backlog(limit=limit, offset=offset)
        return {"ok": True, "ids": ids, "total": self._cache.backlog_size()}

    def get_thumbnail(self, avatar_id: str) -> str:
        with self._lock:
            entry = self._entry(avatar_id)
            thumb = entry.get("thumb") if entry else None
        if not thumb:
            return ""
        # The stored name is untrusted: it can arrive from an imported file.
        path = storage.resolve_thumb(thumb)
        if path is None or not path.exists():
            return ""
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        mime = IMAGE_MIME.get(path.suffix.lower(), "image/png")
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    # ------------------------------------------------------------------ actions
    def add_current(self) -> dict:
        avatar_id = self._norm_id(self.osc.current_avatar_id)
        if not avatar_id:
            return {
                "ok": False,
                "title": "No avatar detected",
                "message": "VRChat hasn't told us what you're wearing yet.\n\n"
                           "Make sure VRChat is running with OSC enabled "
                           "(Action Menu > OSC > Enabled) and you're inside a world, "
                           "then switch avatars once.",
            }
        with self._lock:
            if self._entry(avatar_id):
                return {"ok": False, "title": "Already favourited",
                        "message": "That avatar is already in your list."}
            entry = storage.new_entry(avatar_id)
            self.entries.append(entry)
            self._index_add(entry)
            storage.save_favourites(self.entries)
            self._touch("entries")
        if self.api.is_logged_in():
            self._set_status("Added. Fetching metadata...")
            self._start_metadata(avatar_id)
        else:
            self._set_status("Added. Log in in Settings to auto-fetch names and thumbnails.")
        return {"ok": True, "id": avatar_id}

    def add_by_id(self, avatar_id: str, force: bool = False) -> dict:
        avatar_id = self._norm_id(avatar_id)
        if not avatar_id:
            return {"ok": False, "title": "Nothing entered", "message": "Enter an avatar ID."}
        if not avatar_id.lower().startswith("avtr_") and not force:
            return {
                "ok": False,
                "confirm": True,
                "title": "Unusual ID",
                "message": "That doesn't look like a normal avatar ID (expected 'avtr_...').\n"
                           "Add it anyway?",
            }
        with self._lock:
            if self._entry(avatar_id):
                return {"ok": False, "title": "Already favourited",
                        "message": "That avatar is already in your list."}
            entry = storage.new_entry(avatar_id)
            self.entries.append(entry)
            self._index_add(entry)
            storage.save_favourites(self.entries)
            self._touch("entries")
        if self.api.is_logged_in():
            self._set_status("Added. Fetching metadata...")
            self._start_metadata(avatar_id)
        else:
            self._set_status("Added. Log in in Settings to auto-fetch names and thumbnails.")
        return {"ok": True, "id": avatar_id}

    def wear(self, avatar_id: str) -> dict:
        avatar_id = self._norm_id(avatar_id)
        if self.osc.error:
            return {"ok": False, "title": "OSC error", "message": self.osc.error}
        if not self.osc.listening:
            return {"ok": False, "title": "OSC not running",
                    "message": "The OSC connection is not active."}
        # VRChat does not acknowledge /avatar/change. It broadcasts the new id
        # back once the avatar actually loads, which is the only confirmation we
        # get, so remember what we asked for and check it later.
        with self._lock:
            self._pending_wear = (avatar_id, time.monotonic())
        if self.osc.change_avatar(avatar_id):
            self._set_status("Requested avatar change over OSC.")
            return {"ok": True, "pending": avatar_id}
        with self._lock:
            self._pending_wear = None
        return {"ok": False, "title": "Wear failed",
                "message": "Could not send the avatar change over OSC."}

    def _expire_pending_wear(self) -> None:
        """Report a wear that VRChat never confirmed.

        Most avatars load fine. VRChat refuses ones the account cannot use --
        private avatars, or paid avatars it does not own -- and it does that
        silently, so without this the click just appears to do nothing.
        """
        with self._lock:
            pending = self._pending_wear
            if pending is None:
                return
            avatar_id, requested_at = pending
            if time.monotonic() - requested_at < WEAR_CONFIRM_TIMEOUT:
                return
            self._pending_wear = None
            if self.osc.current_avatar_id == avatar_id:
                return
        entry = self._entry(avatar_id)
        name = f'"{entry["name"]}"' if entry and entry.get("name") else avatar_id
        self._set_status(
            f"VRChat did not switch to {name}. It may be private, or a paid "
            "avatar your account does not own."
        )

    def delete(self, avatar_id: str) -> dict:
        avatar_id = self._norm_id(avatar_id)
        with self._lock:
            entry = self._entry(avatar_id)
            before = len(self.entries)
            self.entries = [e for e in self.entries if self._norm_id(e.get("id")) != avatar_id]
            self._reindex()
            if len(self.entries) == before:
                return {"ok": False}
            storage.save_favourites(self.entries)
            self._touch("entries")
        # Reclaim the cached image too, rather than leaving it to accumulate.
        if entry and entry.get("thumb"):
            storage.delete_thumb(entry["thumb"])
        self._set_status("Removed from favourites.")
        return {"ok": True}

    def copy_id(self, avatar_id: str) -> dict:
        return {"ok": _set_clipboard(avatar_id)}

    def copy_text(self, text: str) -> dict:
        return {"ok": _set_clipboard(text or "")}

    def save_details(self, avatar_id: str, name: str, notes: str, tags) -> dict:
        avatar_id = self._norm_id(avatar_id)
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        else:
            tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        with self._lock:
            entry = self._entry(avatar_id)
            if not entry:
                return {"ok": False}
            entry["name"] = (name or "").strip() or "Unnamed avatar"
            entry["notes"] = (notes or "").rstrip("\n")
            entry["tags"] = tags
            storage.save_favourites(self.entries)
            self._touch("entries")
        return {"ok": True}

    def toggle_favorite(self, avatar_id: str) -> dict:
        avatar_id = self._norm_id(avatar_id)
        with self._lock:
            entry = self._entry(avatar_id)
            if not entry:
                return {"ok": False}
            entry["favorite"] = not bool(entry.get("favorite"))
            storage.save_favourites(self.entries)
            self._touch("entries")
            return {"ok": True, "favorite": entry["favorite"]}

    # ---------------------------------------------------------------- discovery
    def import_vrchat_favourites(self, limit: int = 100) -> dict:
        """Pull your VRChat favourites into the local list.

        A convenience for bootstrapping: these are avatars you already have, so
        they come with working names, authors, platforms and thumbnails. Nothing
        about them is special -- any avatar id can be added with Add by ID.
        """
        if not self.api.is_logged_in():
            return {"ok": False, "title": "Not logged in",
                    "message": "Log in via Settings first."}
        self._set_status("Reading your VRChat favourites...")
        try:
            remote = self.api.list_favorite_avatars(limit=max(1, min(int(limit), 100)))
        except AuthError as exc:
            self._handle_session_expired(exc)
            return {"ok": False, "title": "Session expired",
                    "message": "Log in again in Settings, then retry."}
        except ApiError as exc:
            return {"ok": False, "title": "Could not read favourites", "message": str(exc)}

        added = 0
        unresolved: list[str] = []
        with self._lock:
            for avatar in remote:
                avatar_id = self._norm_id(avatar.get("id"))
                if not avatar_id or self._entry(avatar_id):
                    continue
                entry = storage.new_entry(avatar_id, avatar.get("name") or "")
                entry["author"] = avatar.get("authorName") or ""
                entry["release_status"] = (avatar.get("releaseStatus") or "").lower()
                entry["platforms"] = self._platforms(avatar)
                entry["thumb_url"] = avatar.get("thumbnailImageUrl") or ""
                self.entries.append(entry)
                self._index_add(entry)
                added += 1
                unresolved.append(avatar_id)
            if added:
                storage.save_favourites(self.entries)
                self._touch("entries")
        self._set_status(f"Imported {added} avatar(s) from VRChat.")
        if unresolved:
            threading.Thread(target=self._bulk_metadata, args=(unresolved,),
                             daemon=True).start()
        return {"ok": True, "added": added}

    # ------------------------------------------------------------------ logs
    def save_from_log(self, avatar_id: str) -> dict:
        avatar_id = self._norm_id(avatar_id)
        with self._lock:
            if self._entry(avatar_id):
                return {"ok": True, "id": avatar_id, "already": True}
            log_entry = next((e for e in self.log if e.get("id") == avatar_id), None)
            if log_entry and log_entry.get("private"):
                return {
                    "ok": False,
                    "title": "Private avatar",
                    "message": "This avatar is private or unavailable, so it can't be cloned.",
                }
            entry = storage.new_entry(avatar_id)
            if log_entry and log_entry.get("name"):
                entry["name"] = log_entry["name"]
            self.entries.append(entry)
            self._index_add(entry)
            storage.save_favourites(self.entries)
            self._touch("entries")
        if self.api.is_logged_in():
            self._set_status("Saved. Fetching metadata...")
            self._start_metadata(avatar_id)
        return {"ok": True, "id": avatar_id}

    def delete_log(self, avatar_id: str) -> dict:
        avatar_id = self._norm_id(avatar_id)
        with self._lock:
            self.log = [e for e in self.log if e.get("id") != avatar_id]
            storage.save_log(self.log)
            self._touch("logs")
        return {"ok": True}

    def clear_logs(self) -> dict:
        with self._lock:
            self.log = []
            storage.save_log(self.log)
            self._touch("logs")
        return {"ok": True}

    def save_all_logs(self) -> dict:
        """Save every logged avatar (that isn't private or already saved)."""
        added_ids: list[str] = []
        with self._lock:
            for log_entry in list(self.log):
                avatar_id = log_entry.get("id")
                if not avatar_id or log_entry.get("private") or self._entry(avatar_id):
                    continue
                entry = storage.new_entry(avatar_id)
                if log_entry.get("name"):
                    entry["name"] = log_entry["name"]
                self.entries.append(entry)
                self._index_add(entry)
                added_ids.append(avatar_id)
            if added_ids:
                storage.save_favourites(self.entries)
                self._touch("entries")
        if added_ids and self.api.is_logged_in():
            self._set_status(f"Saved {len(added_ids)} avatar(s) - fetching metadata...")
            threading.Thread(target=self._bulk_metadata, args=(added_ids,), daemon=True).start()
        elif added_ids:
            self._set_status(f"Saved {len(added_ids)} avatar(s). Log in to fetch metadata.")
        return {"ok": True, "added": len(added_ids)}

    def _handle_session_expired(self, exc: Exception) -> None:
        """Record that the stored token is no longer usable and prompt a re-login."""
        with self._lock:
            if self.session_expired:
                return
            self.session_expired = True
        self._set_status(
            "VRChat session expired - log in again in Settings to fetch names and thumbnails."
        )

    def _bulk_metadata(self, ids: list[str]) -> None:
        for avatar_id in ids:
            if self._stopped or self.session_expired:
                return
            with self._lock:
                if avatar_id in self._running:
                    continue
                self._running.add(avatar_id)
            try:
                self._metadata_worker(avatar_id)
            except Exception:
                pass
            # Be a good API citizen: VRChat terminates accounts for abuse, and
            # the README's own advice is to keep request rates low.
            time.sleep(0.6)

    def clear_changes(self) -> dict:
        with self._lock:
            self.changes = []
            storage.save_changes(self.changes)
            self._touch("changes")
        return {"ok": True}

    def save_change(self, player: str, avatar: str, avatar_id: str = "") -> dict:
        """Save a logged player-avatar change. If an ID is supplied it becomes a
        favourite (cloneable); otherwise the avatar name is stored with a
        manual lookup note."""
        avatar_id = (avatar_id or "").strip()
        if avatar_id:
            return self.save_from_log(avatar_id)
        return {
            "ok": False,
            "title": "No avatar ID",
            "message": f"VRChat's log only recorded the avatar name \"{avatar}\" worn by "
                       f"{player}, not its ID, so it can't be cloned automatically. "
                       "Add it manually with Add by ID once you have the ID.",
        }

    def refresh_metadata(self, avatar_id: str) -> dict:
        avatar_id = self._norm_id(avatar_id)
        if not self.api.is_logged_in():
            return {"ok": False, "title": "Not logged in",
                    "message": "Log in first to look up names and thumbnails."}
        self._set_status("Fetching metadata...")
        self._start_metadata(avatar_id)
        return {"ok": True}

    def refresh_all_metadata(self) -> dict:
        if not self.api.is_logged_in():
            return {"ok": False, "title": "Not logged in",
                    "message": "Log in first to look up names and thumbnails."}
        with self._lock:
            ids = [e["id"] for e in self.entries]
        if not ids:
            return {"ok": True}
        self._set_status(f"Fetching metadata for {len(ids)} avatar(s)...")
        threading.Thread(target=self._bulk_metadata, args=(ids,), daemon=True).start()
        return {"ok": True}

    def open_data_folder(self) -> dict:
        storage.ensure_dirs()
        try:
            os.startfile(str(storage.DATA_DIR))
            return {"ok": True}
        except OSError:
            return {"ok": False, "message": str(storage.DATA_DIR)}

    def prune_thumbnails(self) -> dict:
        """Drop cached thumbnails that no longer belong to a favourite."""
        storage.ensure_dirs()
        with self._lock:
            keep = {e["thumb"] for e in self.entries if isinstance(e.get("thumb"), str)}
        orphans, trimmed = storage.prune_thumbs(keep)
        total = orphans + trimmed
        message = (
            f"Removed {total} unused thumbnail{'s' if total != 1 else ''}."
            if total else "Nothing to clean up."
        )
        self._set_status(message)
        return {"ok": True, "removed": total, "orphans": orphans, "trimmed": trimmed}

    def prune_orphans_on_start(self) -> None:
        """Best-effort cleanup at start-up. Never fatal."""
        try:
            storage.ensure_dirs()
            with self._lock:
                keep = {e["thumb"] for e in self.entries if isinstance(e.get("thumb"), str)}
            storage.prune_thumbs(keep)
        except Exception:
            pass

    # ------------------------------------------------------------------ files
    def _dialog(self, kind, **kwargs) -> str | None:
        if self._window is None:
            return None
        try:
            result = self._window.create_file_dialog(kind, **kwargs)
        except Exception:
            return None
        if not result:
            return None
        return str(result[0])

    def export_favourites(self) -> dict:
        storage.ensure_dirs()
        with self._lock:
            entries = [dict(e) for e in self.entries]
        data = json.dumps({"version": 1, "entries": entries}, indent=2, ensure_ascii=False)
        path = self._dialog(webview.SAVE_DIALOG, save_filename="favourites.json")
        if not path:
            return {"ok": False, "cancelled": True}
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            Path(path).write_text(data, encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "title": "Export failed", "message": str(exc)}
        return {"ok": True, "path": path, "count": len(entries)}

    def import_favourites(self) -> dict:
        path = self._dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            raw = Path(path).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "title": "Import failed", "message": str(exc)}
        incoming = data.get("entries", []) if isinstance(data, dict) else data
        if not isinstance(incoming, list):
            return {"ok": False, "title": "Import failed",
                    "message": "That file isn't a favourites export."}
        with self._lock:
            merged, added = storage.merge_favourites(self.entries, incoming)
            self.entries = merged
            self._reindex()
            storage.save_favourites(self.entries)
            self._touch("entries")
        self._set_status(f"Imported {added} avatar(s).")
        return {"ok": True, "added": added}

    # ------------------------------------------------------------------ updates
    def check_updates(self) -> dict:
        req = urllib.request.Request(
            RELEASES_API,
            headers={"User-Agent": f"LocalAvatarFavourites/{__version__}",
                     "Accept": "application/vnd.github+json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            return {"ok": False, "current": __version__}
        tag = str(payload.get("tag_name") or "").lstrip("v")
        url = payload.get("html_url") or ""
        if tag and is_newer(tag, __version__):
            return {"ok": True, "update": True, "latest": tag,
                    "url": url, "current": __version__}
        return {"ok": True, "update": False, "latest": tag, "current": __version__}

    def open_url(self, url: str) -> dict:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False}
        try:
            import webbrowser

            webbrowser.open(url)
            return {"ok": True}
        except Exception:
            return {"ok": False}

    # ------------------------------------------------------------------ auth
    @staticmethod
    def _pick_method(methods: list[str] | None) -> str:
        lowered = [str(m).lower() for m in (methods or [])]
        for preferred in ("totp", "emailotp", "otp"):
            if preferred in lowered:
                return preferred
        return lowered[0] if lowered else "totp"

    def login(self, username: str, password: str, code: str = "", method: str = "") -> dict:
        username = (username or "").strip()
        code = (code or "").strip()
        method = (method or "").strip().lower()
        api = self._pending_api

        if api is not None and code:
            # Finish a 2FA login using the selected method.
            try:
                api.verify_2fa(code, method or self._pending_2fa_method or "totp")
            except AuthError as exc:
                self._pending_api = None
                self._pending_2fa_methods = []
                return {"status": "error", "message": str(exc)}
            except ApiError as exc:
                self._pending_api = None
                self._pending_2fa_methods = []
                return {"status": "error", "message": str(exc)}
            except Exception as exc:
                self._pending_api = None
                self._pending_2fa_methods = []
                return {"status": "error", "message": f"Login failed: {exc}"}
        else:
            if not username or not password:
                return {"status": "error", "message": "Enter your username and password."}

            # Try the password as entered, then again with surrounding
            # whitespace stripped (pasting often adds a trailing space/newline).
            candidates = [password]
            stripped = password.strip()
            if stripped and stripped != password:
                candidates.append(stripped)

            last_error: Exception | None = None
            api = None
            for candidate in candidates:
                api = VRCApi()
                try:
                    api.login(username, candidate)
                except TwoFactorRequired as exc:
                    self._pending_api = api
                    self._pending_2fa_methods = exc.methods or ["totp"]
                    self._pending_2fa_method = self._pick_method(exc.methods)
                    return {
                        "status": "2fa",
                        "methods": self._pending_2fa_methods,
                        "method": self._pending_2fa_method,
                        "message": "2FA required - enter your code.",
                    }
                except AuthError as exc:
                    last_error = exc
                    continue
                except ApiError as exc:
                    last_error = exc
                    break
                except Exception as exc:
                    last_error = exc
                    break
                else:
                    last_error = None
                    break

            if last_error is not None:
                self._pending_api = None
                self._pending_2fa_methods = []
                return {"status": "error", "message": str(last_error)}

        if api is None:
            # Unreachable in practice: both branches above either populate `api`
            # or return early. Checked so a future change cannot silently
            # promote a None client.
            self._pending_api = None
            self._pending_2fa_methods = []
            return {"status": "error", "message": "Login failed."}

        self._pending_api = None
        self._pending_2fa_methods = []
        self._pending_2fa_method = ""
        self.api = api
        self.session_expired = False
        self.settings["auth_token"] = api.token
        if username:
            self.settings["auth_username"] = username
        storage.save_settings(self.settings)
        return {"status": "ok", "message": "Logged in."}

    def logout(self) -> dict:
        self._pending_api = None
        self._pending_2fa_methods = []
        self._pending_2fa_method = ""
        self.api = VRCApi("")
        self.session_expired = False
        self.settings["auth_token"] = ""
        storage.save_settings(self.settings)
        return {"ok": True}

    # ------------------------------------------------------------------ settings
    def save_settings(self, send_port, recv_port) -> dict:
        try:
            send_port = int(send_port)
            recv_port = int(recv_port)
        except (TypeError, ValueError):
            return {"ok": False, "message": "Ports must be numbers."}
        if not (0 < send_port < 65536) or not (0 < recv_port < 65536):
            return {"ok": False, "message": "Ports must be between 1 and 65535."}
        if send_port == recv_port:
            return {"ok": False,
                    "message": "The send and receive ports must be different."}

        # Bind before persisting. The old order wrote the settings first and only
        # then discovered the port was unavailable, so a single conflict left
        # the app unable to start OSC on every subsequent launch with no way to
        # recover from the UI.
        previous = self.osc
        candidate = OSCBridge(
            send_ip=self.settings.get("osc_send_ip", "127.0.0.1"),
            send_port=send_port,
            receive_port=recv_port,
        )
        candidate.add_avatar_change_listener(self._on_avatar_change)
        candidate.start()
        if candidate.error:
            candidate.stop()
            return {"ok": False, "message": candidate.error}

        previous.stop()
        self.osc = candidate
        self.settings["osc_send_port"] = send_port
        self.settings["osc_receive_port"] = recv_port
        storage.save_settings(self.settings)
        self._set_status("OSC settings applied.")
        return {"ok": True}

    # ------------------------------------------------------------------ metadata
    @staticmethod
    def _platforms(avatar: dict) -> list[str]:
        mapping = {
            "standalonewindows": "PC",
            "android": "Quest",
            "ios": "iOS",
        }
        found: list[str] = []
        for package in avatar.get("unityPackages") or []:
            if not isinstance(package, dict):
                continue
            label = mapping.get(str(package.get("platform", "")).lower())
            if label and label not in found:
                found.append(label)
        return found

    def _start_metadata(self, avatar_id: str) -> None:
        with self._lock:
            if avatar_id in self._running:
                return
            self._running.add(avatar_id)
        threading.Thread(target=self._metadata_worker, args=(avatar_id,), daemon=True).start()

    def _metadata_worker(self, avatar_id: str) -> None:
        api = self.api
        try:
            try:
                avatar = api.get_avatar(avatar_id)
            except AuthError as exc:
                # An expired or revoked token must never be recorded as "this
                # avatar is private": that silently greyed out the whole log and
                # made Save all skip everything.
                self._handle_session_expired(exc)
                return
            except ApiError as exc:
                self._set_status(f"Metadata failed for {avatar_id}: {exc}")
                return
            if not avatar:
                with self._lock:
                    log_entry = next((e for e in self.log if e.get("id") == avatar_id), None)
                    if log_entry and not log_entry.get("private"):
                        log_entry["private"] = True
                        storage.save_log(self.log)
                self._set_status(f"No public metadata for {avatar_id} (private avatar? skipped).")
                return
            name = avatar.get("name") or ""
            thumb_url = avatar.get("thumbnailImageUrl") or avatar.get("imageUrl") or ""
            author = avatar.get("authorName") or ""
            release_status = (avatar.get("releaseStatus") or "").lower()
            platforms = self._platforms(avatar)
            with self._lock:
                entry = self._entry(avatar_id)
                if entry:
                    changed = False
                    if name and entry.get("name") in ("", "Unnamed avatar"):
                        entry["name"] = name
                        changed = True
                    if entry.get("author") != author:
                        entry["author"] = author
                        changed = True
                    if entry.get("release_status") != release_status:
                        entry["release_status"] = release_status
                        changed = True
                    # Always replace platforms (even with an empty list) so a
                    # stale/incorrect list from an earlier lookup is cleared.
                    if entry.get("platforms") != platforms:
                        entry["platforms"] = platforms
                        changed = True
                    if thumb_url and entry.get("thumb_url") != thumb_url:
                        entry["thumb_url"] = thumb_url
                        changed = True
                    if changed:
                        storage.save_favourites(self.entries)
                        self._touch("entries")
                log_entry = next((e for e in self.log if e.get("id") == avatar_id), None)
                if log_entry and name and log_entry.get("name") != name:
                    log_entry["name"] = name
                    log_entry["private"] = False
                    storage.save_log(self.log)
                    self._touch("logs")
            if thumb_url:
                self._thumb_worker(avatar_id, thumb_url)
        finally:
            with self._lock:
                self._running.discard(avatar_id)

    def _thumb_worker(self, avatar_id: str, url: str) -> None:
        api = self.api
        ext = "png"
        tail = url.split("?")[0].rsplit(".", 1)
        if len(tail) == 2:
            candidate = tail[1].lower()
            if candidate in ("jpg", "jpeg", "png", "webp"):
                ext = "jpg" if candidate in ("jpg", "jpeg") else candidate
        dest = storage.thumb_file_path(avatar_id, ext)
        if api.download_image(url, str(dest)):
            with self._lock:
                entry = self._entry(avatar_id)
                if entry:
                    entry["thumb"] = os.path.basename(str(dest))
                    storage.save_favourites(self.entries)
                    self._touch("entries")
            self._set_status("Thumbnail updated.")
        else:
            self._set_status("Could not download thumbnail for this avatar.")
