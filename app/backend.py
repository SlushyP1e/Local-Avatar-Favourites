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
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import webview

import storage
from api import ApiError, AuthError, TwoFactorRequired, VRCApi
from jobs import JobRegistry, JobRunner
from osc import OSCBridge
from thumbsrv import IMAGE_MIME, ThumbServer
from version import __version__
from versions import is_newer
from vrcache import VRCacheWatcher
from vrcdetails import DEFAULT_AVATAR_IDS, is_default_avatar, is_default_avatar_name
from vrclog import VRCLogWatcher

RELEASES_API = "https://api.github.com/repos/SlushyP1e/Local-Avatar-Favourites/releases/latest"
LOGGER = logging.getLogger(__name__)

# Caps on the two log lists. The defaults live in storage as settings so the
# user can size them; these are only the fallbacks for when the setting is
# absent or invalid.
MAX_LOG_ENTRIES = storage.DEFAULT_MAX_AVATAR_LOG
MAX_CHANGE_ENTRIES = storage.DEFAULT_MAX_PLAYER_CHANGES
DISCOVERY_FEED_MAX = 300

# How long to wait for VRChat to broadcast the avatar back before assuming the
# change was refused. Large avatars can take a while to download.
WEAR_CONFIRM_TIMEOUT = 6.0
# How long after a request a refusal still counts as ours. VRChat writes the
# refusal to its log within a second or so of the request; a longer window than
# that is only there to absorb a slow poll.
WEAR_REFUSAL_WINDOW = 30.0

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
        # (avatar_id, monotonic timestamp) of the active wear request. An OSC
        # echo alone is not confirmation: VRChat may echo the requested id and
        # then reject it, so keep this active through the refusal/API-fallback
        # window.
        self._pending_wear: tuple[str, float] | None = None
        self._pending_wear_wall_time: datetime | None = None
        # Diagnostic copy only. Matching always uses _pending_wear and this is
        # cleared as soon as an OSC event, refusal, timeout, or API result is
        # processed; it must never make an unrelated log line look current.
        self._last_request: tuple[str, float] | None = None
        self._api_fallback_wear: tuple[str, float] | None = None
        self._jobs = JobRegistry()
        self._runner = JobRunner(self._jobs)
        # Serves cached thumbnails to the webview over loopback so image bytes
        # never cross the bridge as base64. Optional: if it cannot bind, the UI
        # falls back to the bridge path and behaves the same, just heavier.
        self._thumbs = ThumbServer()
        # Avatar ids the user never wants logged. Read from settings on demand by
        # :meth:`_ignored`; these two fields only cache the derived *name* set
        # used for player changes, which VRChat identifies by name rather than id.
        self._ignore_version = 0
        self._ignored_names_cache: set[str] | None = None
        self._ignored_names_stamp: tuple[int, int] | None = None

        self.entries: list[dict] = storage.load_favourites()
        self._index: dict[str, dict] = {}
        self._reindex()
        self.log: list[dict] = storage.load_log()
        self.changes: list[dict] = storage.load_changes()
        self.settings: dict = storage.load_settings()
        # Runs regardless of start_services: it is pure data hygiene over
        # already-loaded state, and doing it here rather than on the background
        # prune thread means the UI never renders a default avatar at all.
        self.prune_defaults()
        # Same reasoning: a cap lowered since the last launch must take effect
        # before the first poll, not whenever the list next overflows.
        self.trim_logs_to_limits()
        # A blocklist edited while the app was closed must apply to rows already
        # on disk, not only to sightings from now on.
        with self._lock:
            self._drop_ignored_rows_locked()
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
            self._thumbs.start()
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
        self._thumbs.stop()
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
                    self._confirm_wear_from_log(event["id"], event.get("time", ""))
                    if self._record_log(event["id"], event.get("time", ""),
                                        save=False, source=SOURCE_LOG):
                        changed_log = True
                elif event["type"] == "avatar-inaccessible":
                    self._note_inaccessible(event["id"], event.get("time", ""))
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

    def _log_limit(self) -> int:
        """Cap on the avatar log, from settings.

        Read through sanitize_settings' bounds on every call rather than cached,
        because the settings are the user's to change and a stale cap would
        quietly disagree with the number shown in Settings.
        """
        try:
            limit = int(self.settings.get("max_avatar_log", MAX_LOG_ENTRIES))
        except (TypeError, ValueError):
            return MAX_LOG_ENTRIES
        if storage.MIN_LOG_LIMIT <= limit <= storage.MAX_LOG_LIMIT:
            return limit
        return MAX_LOG_ENTRIES

    def _change_limit(self) -> int:
        """Cap on the player-changes log, from settings. See :meth:`_log_limit`."""
        try:
            limit = int(self.settings.get("max_player_changes", MAX_CHANGE_ENTRIES))
        except (TypeError, ValueError):
            return MAX_CHANGE_ENTRIES
        if storage.MIN_LOG_LIMIT <= limit <= storage.MAX_LOG_LIMIT:
            return limit
        return MAX_CHANGE_ENTRIES

    def _touch(self, section: str) -> None:
        self._revs[section] = self._revs.get(section, 0) + 1

    # ------------------------------------------------------------- blocklist
    def _ignored(self) -> set[str]:
        """The blocklist as a set, for lookup.

        Rebuilt from settings on demand rather than mirrored into an attribute:
        it is read on every recorded row, and settings.json is the user's to
        edit, so a mirror could disagree with what was actually saved.
        """
        return set(self.settings.get("ignored_avatars") or [])

    def _is_ignored(self, avatar_id) -> bool:
        # Rows come from JSON and hand-edited files, so id may be anything.
        return isinstance(avatar_id, str) and avatar_id in self._ignored()

    def _ignored_names(self) -> set[str]:
        """Case-folded names of every blocked avatar that is known by name.

        VRChat logs only a *name* for a remote player's avatar, so blocking a
        player change can only work by name. Names come from the rows and
        favourites already on hand rather than being fetched: a block must not
        depend on being logged in.

        Cached, because this is consulted on every player-change row and
        rebuilding the set each time would walk both lists per sighting. The
        cache is keyed on the blocklist, so adding or removing an ignore
        invalidates it, and it is dropped whenever the underlying name data
        changes by touching :attr:`_ignored_names_stamp`.
        """
        blocked = self._ignored()
        stamp = (len(blocked), self._ignore_version)
        if self._ignored_names_cache is None or self._ignored_names_stamp != stamp:
            names: set[str] = set()
            for source in (self.entries, self.log):
                for row in source:
                    name = row.get("name")
                    if isinstance(name, str) and name.strip():
                        names.add(name.strip().casefold())
            self._ignored_names_cache = names
            self._ignored_names_stamp = stamp
        return self._ignored_names_cache

    def _is_change_ignored(self, avatar_name: str) -> bool:
        name = (avatar_name or "").strip()
        if not name:
            return False
        return name.casefold() in self._ignored_names()

    def is_ignored(self, avatar_id: str) -> bool:
        """Whether an avatar is on the blocklist. Used by the UI for badges."""
        return self._is_ignored(self._norm_id(avatar_id))

    def add_ignore(self, avatar_id: str, name: str = "") -> dict:
        """Stop an avatar ever being logged again, and drop any row it has.

        The existing row is removed rather than hidden, because a blocklist that
        leaves yesterday's findings on screen is not doing what the user asked.
        """
        avatar_id = self._norm_id(avatar_id)
        if not avatar_id:
            return {"ok": False, "message": "No avatar ID given."}
        with self._lock:
            current = storage.normalize_ignored(self.settings.get("ignored_avatars"))
            if avatar_id in current:
                return {"ok": True, "id": avatar_id, "already": True}
            if len(current) >= storage.MAX_IGNORED:
                return {"ok": False, "message":
                        f"Ignore list is full ({storage.MAX_IGNORED})."}
            # Read the name before the row is dropped: the log row is often the
            # only place it has ever been recorded, and losing it would leave
            # the Settings list showing "Unnamed avatar" for exactly the avatars
            # the user just blocked. The UI's label is preferred because it has
            # already resolved the same way the user sees it.
            label = self._known_name(avatar_id) or name
            self.settings["ignored_avatars"] = [*current, avatar_id]
            if label:
                names = dict(storage.normalize_ignored_names(
                    self.settings.get("ignored_names"), current))
                names[avatar_id] = label
                self.settings["ignored_names"] = names
            storage.save_settings(self.settings)
            self._invalidate_ignore_cache()
            removed = self._drop_ignored_rows_locked()
        label = name or avatar_id
        self._set_status(
            f"Ignoring {label}. It will not be logged again."
            + (f" Removed {removed} existing row(s)." if removed else "")
        )
        return {"ok": True, "id": avatar_id, "removed": removed}

    def remove_ignore(self, avatar_id: str) -> dict:
        """Un-ignore, so the avatar can be discovered again."""
        avatar_id = self._norm_id(avatar_id)
        if not avatar_id:
            return {"ok": False, "message": "No avatar ID given."}
        with self._lock:
            current = storage.normalize_ignored(self.settings.get("ignored_avatars"))
            if avatar_id not in current:
                return {"ok": True, "id": avatar_id, "already": True}
            self.settings["ignored_avatars"] = [i for i in current if i != avatar_id]
            # Drop the stored name with the entry, or the file would keep a label
            # for an avatar that is no longer blocked.
            names = dict(storage.normalize_ignored_names(
                self.settings.get("ignored_names"), self.settings["ignored_avatars"]))
            names.pop(avatar_id, None)
            self.settings["ignored_names"] = names
            storage.save_settings(self.settings)
            self._invalidate_ignore_cache()
        self._set_status("No longer ignoring this avatar.")
        return {"ok": True, "id": avatar_id}

    def _known_name(self, avatar_id: str) -> str:
        """Best available display name for an avatar, from data already held.

        A favourite wins over a log row: it is the one the user has actually
        named or accepted. Never fetches, so blocking works while logged out.
        """
        if not avatar_id:
            return ""
        entry = self._entry(avatar_id)
        if entry:
            name = entry.get("name")
            if isinstance(name, str) and name.strip() and name != "Unnamed avatar":
                return name.strip()
        row = next((e for e in self.log if e.get("id") == avatar_id), None)
        if row:
            name = row.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return ""

    def _invalidate_ignore_cache(self) -> None:
        """Drop the derived block-by-name set after the blocklist or a name changes."""
        self._ignore_version += 1
        self._ignored_names_cache = None
        self._ignored_names_stamp = None

    def _drop_ignored_rows_locked(self) -> int:
        """Remove log rows and player-change rows for blocked avatars.

        Returns how many rows went. Player changes are matched by avatar *name*
        because VRChat never logs a remote player's id, so the block is applied
        to the name there; the id half of the block cannot help at all.
        """
        blocked_names = self._ignored_names()
        blocked = self._ignored()
        if not blocked:
            return 0
        removed = 0

        if self.log:
            kept_logs = [e for e in self.log if not self._is_ignored(e.get("id"))]
            if len(kept_logs) != len(self.log):
                removed += len(self.log) - len(kept_logs)
                self.log = kept_logs
                storage.save_log(self.log)
                self._touch("logs")

        # Collected before the log rows above are dropped, since the row being
        # removed is often the only place the avatar's name is known.
        blocked_names = self._ignored_names()
        if blocked_names:
            kept_changes = [
                c for c in self.changes
                if str(c.get("avatar") or "").strip().casefold() not in blocked_names
            ]
            if len(kept_changes) != len(self.changes):
                removed += len(self.changes) - len(kept_changes)
                self.changes = kept_changes
                storage.save_changes(self.changes)
                self._touch("changes")
        return removed

    def get_ignores(self) -> dict:
        """The blocklist for the Settings panel, with names where known."""
        with self._lock:
            ids = storage.normalize_ignored(self.settings.get("ignored_avatars"))
            # The stored label first, because blocking already removed the row it
            # came from; live data only as a fallback for an id blocked while the
            # app was closed and so never captured one.
            names: dict[str, str] = dict(storage.normalize_ignored_names(
                self.settings.get("ignored_names"), ids))
            for avatar_id in ids:
                names.setdefault(avatar_id, self._known_name(avatar_id))
        return {"ok": True, "ids": ids, "names": names}

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

    @staticmethod
    def _debug_wear(source: str, avatar_id: str, reason: str, timestamp: str = "") -> None:
        timestamp = timestamp or datetime.now().astimezone().isoformat(timespec="milliseconds")
        LOGGER.debug(
            "wear_event timestamp=%s source=%s avatar_id=%s reason=%s",
            timestamp,
            source,
            avatar_id,
            reason,
        )

    def _log_event_matches_pending(self, event_time: str) -> bool:
        """Reject log events missing a timestamp or predating the active click."""
        request_time = self._pending_wear_wall_time
        if request_time is None or not event_time:
            return False
        try:
            parsed = datetime.fromisoformat(event_time)
        except (TypeError, ValueError):
            return False
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        # VRChat timestamps have one-second precision, so compare at that same
        # precision rather than rejecting a line written during the request's
        # timestamp second due to sub-second wall-clock differences.
        return parsed.replace(microsecond=0) >= request_time.replace(microsecond=0)

    def _on_avatar_change(self, avatar_id: str) -> None:
        avatar_id = self._norm_id(avatar_id)
        with self._lock:
            pending = self._pending_wear
            is_echo = bool(pending and pending[0] == avatar_id)
            if is_echo:
                # VRChat can echo /avatar/change before it decides whether the
                # avatar can be loaded. Keep _pending_wear live so the matching
                # refusal line can still be correlated; this echo alone is not
                # proof that the client actually loaded the avatar.
                self._last_request = None
            elif pending is None:
                self._last_request = None
        self._debug_wear(
            SOURCE_OSC,
            avatar_id,
            "requested id echoed; awaiting client load/refusal" if is_echo
            else "avatar-change OSC event received",
        )
        self._record_log(avatar_id, source=SOURCE_OSC)
        entry = self._entry(avatar_id)
        label = entry.get("name") if entry and entry.get("name") else avatar_id
        if is_echo:
            name = f"VRChat echoed the request for {label}; waiting for load confirmation."
        else:
            name = f"Now wearing {label}."
        self._set_status(name)

    def _confirm_wear_from_log(self, avatar_id: str, event_time: str = "") -> bool:
        """Confirm a wear only from a fresh client log load/save event."""
        avatar_id = self._norm_id(avatar_id)
        with self._lock:
            pending = self._pending_wear
            if pending is None or pending[0] != avatar_id:
                return False
            age = time.monotonic() - pending[1]
            if age > WEAR_REFUSAL_WINDOW:
                return False
            if not self._log_event_matches_pending(event_time):
                self._debug_wear(SOURCE_LOG, avatar_id,
                                 "ignored load event: timestamp missing or predates active request",
                                 event_time)
                return False
            self._pending_wear = None
            self._pending_wear_wall_time = None
            self._last_request = None
            self._api_fallback_wear = None
            entry = self._entry(avatar_id)
            if entry is not None and entry.pop("inaccessible", None) is not None:
                storage.save_favourites(self.entries)
                self._touch("entries")
            label = entry.get("name") if entry and entry.get("name") else avatar_id
        self._debug_wear(SOURCE_LOG, avatar_id, "client log confirmed avatar data loaded", event_time)
        self._set_status(f"Now wearing {label} (confirmed by VRChat's client log).")
        return True

    def _note_inaccessible(self, avatar_id: str, event_time: str = "") -> None:
        """VRChat refused an avatar change; correct the optimistic success.

        VRChat broadcasts the requested id over OSC before it decides whether it
        can honour the request, so an inaccessible avatar arrives looking exactly
        like a successful switch. Nothing follows it in the log either -- no load,
        no revert -- so the active request must stay pending long enough for this
        current-session log event to explain the refusal.

        The log reports that this request was refused, but does not give a
        reason or establish that future requests will also fail. Keep that
        distinction: a refusal is useful history, not an eligibility verdict.
        """
        avatar_id = self._norm_id(avatar_id)
        request: tuple[str, float] | None
        with self._lock:
            # Only the currently active request can be refused. A timestamp
            # left behind by an older click is never sufficient to attribute a
            # later line from the client log.
            request = self._pending_wear
            if request is None:
                self._debug_wear(SOURCE_LOG, avatar_id, "ignored refusal: no active wear request",
                                 event_time)
                return
            age = time.monotonic() - request[1]
            if request[0] != avatar_id:
                self._debug_wear(SOURCE_LOG, avatar_id,
                                 f"ignored refusal: active request is for {request[0]}",
                                 event_time)
                return
            if age > WEAR_REFUSAL_WINDOW:
                self._debug_wear(SOURCE_LOG, avatar_id,
                                 f"ignored refusal: active request is {age:.2f}s old",
                                 event_time)
                return
            if not self._log_event_matches_pending(event_time):
                self._debug_wear(SOURCE_LOG, avatar_id,
                                 "ignored refusal: timestamp missing or predates active request",
                                 event_time)
                return
            self._last_request = None
            entry = self._entry(avatar_id)
            name = entry.get("name") if entry else None
            # Keep the last refusal as useful history, but do not treat it as a
            # permanent property of the avatar. A refusal can be transient (for
            # example, while VRChat is resolving an avatar it has not loaded
            # before), and VRChat exposes no API field that makes this a reliable
            # pre-flight eligibility check.
            if entry is not None and not entry.get("inaccessible"):
                entry["inaccessible"] = True
                storage.save_favourites(self.entries)
                self._touch("entries")
            logged_in = self.api.is_logged_in()
            if not logged_in:
                self._pending_wear = None
                self._pending_wear_wall_time = None
                self._api_fallback_wear = None
        self._debug_wear(SOURCE_LOG, avatar_id,
                         f"matched active request (age={age:.2f}s); client refusal",
                         event_time)
        label = f'"{name}"' if name else avatar_id
        if logged_in:
            self._start_api_fallback(avatar_id, request[1], "VRChat client refusal")
            return
        self._set_status(
            f"VRChat's client refused the OSC wear request for {label}. "
            "The app is not logged in, so the API fallback was not available. "
            "Log in in Settings and try again."
        )

    def _record_log(self, avatar_id: str, when: str = "", save: bool = True,
                     source: str = SOURCE_OSC) -> bool:
        """Record that an avatar id was seen.

        ``source`` records where the id came from. De-duplication uses a
        minute-resolution bucket rather than the exact timestamp: the same
        avatar change arrives twice in normal operation, once over OSC and once
        via the log file, with different timestamps, so the old exact comparison
        counted every change twice.

        VRChat's built-in default avatars are dropped before anything else
        touches them. Robot and Unity-chan are wearable, so the moment anyone in
        an instance wears one it lands in VRChat's cache and reaches us like any
        other discovery. There is no way to clone a default and no metadata
        worth fetching, so they were pure noise -- but worse than noise, because
        they looked like real findings and could be saved by mistake.
        """
        avatar_id = (avatar_id or "").strip().lower()
        if not avatar_id or is_default_avatar(avatar_id):
            return False
        stamp = when or storage.now_iso()
        bucket = stamp[:16]
        with self._lock:
            # Checked inside the lock so a block added while a batch is being
            # ingested cannot be raced past by the rest of the batch.
            if self._is_ignored(avatar_id):
                return False
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
            limit = self._log_limit()
            if len(self.log) > limit:
                self.log.sort(key=lambda e: e.get("last_seen", ""))
                self.log = self.log[-limit:]
            if save:
                storage.save_log(self.log)
            self._touch("logs")
        return True

    def _record_change(self, player: str, avatar: str, when: str = "") -> bool:
        player = (player or "").strip()
        avatar = (avatar or "").strip()
        if not player or not avatar:
            return False
        # These rows carry a name and no id -- VRChat only logs the name for
        # remote players -- so the name is the sole thing available to match.
        # Exact match, so a community avatar called "Robot Deluxe" survives.
        if is_default_avatar_name(avatar):
            return False
        stamp = when or storage.now_iso()
        with self._lock:
            # A player-change row has no id, only a name, so a blocked avatar can
            # only be honoured here by name. That is the same limitation the rest
            # of this feature has: VRChat simply does not log remote ids.
            if self._is_change_ignored(avatar):
                return False
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
            limit = self._change_limit()
            if len(self.changes) > limit:
                self.changes.sort(key=lambda e: e.get("last_seen", ""))
                self.changes = self.changes[-limit:]
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

    def prune_defaults(self) -> tuple[int, int]:
        """Drop VRChat's built-in default avatars from the stored logs.

        Called once at start-up, because the filter in :meth:`_record_log` and
        :meth:`_record_change` only stops *new* rows. Without this, an install
        that has been running for a while keeps every default it already
        collected -- on one real install, 11 avatar rows and 12 player-change
        rows -- and the user has no way to tell which are stale without
        deleting the whole log by hand.

        Returns (avatars removed, player changes removed).
        """
        removed_logs = 0
        removed_changes = 0
        with self._lock:
            if self.log:
                kept = [e for e in self.log if not is_default_avatar(e.get("id"))]
                removed_logs = len(self.log) - len(kept)
                if removed_logs:
                    self.log = kept
                    storage.save_log(self.log)
                    self._touch("logs")
            if self.changes:
                kept_changes = [c for c in self.changes
                                if not is_default_avatar_name(c.get("avatar"))]
                removed_changes = len(self.changes) - len(kept_changes)
                if removed_changes:
                    self.changes = kept_changes
                    storage.save_changes(self.changes)
                    self._touch("changes")
        return removed_logs, removed_changes

    def trim_logs_to_limits(self) -> tuple[int, int]:
        """Drop the oldest rows until both lists respect their caps.

        Run at start-up and whenever the caps change. Recording already trims,
        but a cap lowered from 800 to 100 would otherwise leave the list at its
        current size until enough new rows arrived to cross the old limit --
        so the number in Settings would not describe what is on screen.
        """
        with self._lock:
            return self._trim_logs_locked()

    def _trim_logs_locked(self) -> tuple[int, int]:
        """Trim both lists to their caps. Caller must hold the lock."""
        removed_logs = removed_changes = 0
        log_limit = self._log_limit()
        if len(self.log) > log_limit:
            before = len(self.log)
            self.log.sort(key=lambda e: e.get("last_seen", ""))
            self.log = self.log[-log_limit:]
            removed_logs = before - len(self.log)
            storage.save_log(self.log)
            self._touch("logs")
        change_limit = self._change_limit()
        if len(self.changes) > change_limit:
            before = len(self.changes)
            self.changes.sort(key=lambda e: e.get("last_seen", ""))
            self.changes = self.changes[-change_limit:]
            removed_changes = before - len(self.changes)
            storage.save_changes(self.changes)
            self._touch("changes")
        return removed_logs, removed_changes

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
                "job": self._jobs.active(),
                "motion": self.settings.get("motion", storage.DEFAULT_MOTION),
                "tray": bool(getattr(self, "_tray", None)),
                "osc": {
                    "listening": self.osc.listening,
                    "error": self.osc.error,
                    "seen_traffic": self.osc.seen_traffic(),
                },
                # Non-empty when the loopback thumbnail server is up. The UI
                # points <img src> straight at it so image bytes never have to
                # be base64-encoded across the bridge and held in JavaScript.
                "thumb_base": self._thumbs.base_url,
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
            "exit_on_close": bool(self.settings.get("exit_on_close", True)),
            "motion": self.settings.get("motion", storage.DEFAULT_MOTION),
            "max_avatar_log": self._log_limit(),
            "max_player_changes": self._change_limit(),
            "tray": bool(getattr(self, "_tray", None)),
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
            # Reported so the filter is visible rather than mysterious. A log
            # that quietly omits rows reads as broken; one that says what it
            # ignored reads as working.
            "defaults": len(DEFAULT_AVATAR_IDS),
        }

    def get_backlog(self, limit: int = 60, offset: int = 0) -> dict:
        """A page of previously-seen ids, newest first."""
        ids = self._cache.backlog(limit=limit, offset=offset)
        return {"ok": True, "ids": ids, "total": self._cache.backlog_size()}

    def get_thumbnail(self, avatar_id: str) -> str:
        """Return one thumbnail as a base64 data URI.

        Only used when the loopback thumbnail server is unavailable, so image
        bytes have to be carried across the bridge by hand. Keeping it working
        matters more than it being fast: an unreachable server must degrade to
        slow, not to blank posters.
        """
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
        with self._lock:
            entry = self._entry(avatar_id)
            # A previous refusal is only the result of one request, not reliable
            # evidence that this avatar can never be worn. In particular, do not
            # let a stale/local flag prevent the user's next explicit OSC send.
            if entry is not None and entry.get("inaccessible"):
                entry.pop("inaccessible", None)
                storage.save_favourites(self.entries)
                self._touch("entries")
            # VRChat does not acknowledge /avatar/change. It broadcasts the new id
            # back before load/refusal is decided, so retain the request until a
            # client log confirms load, VRChat refuses it, or it times out.
            request = (avatar_id, time.monotonic())
            self._pending_wear = request
            self._pending_wear_wall_time = datetime.now().astimezone().replace(microsecond=0)
            self._last_request = self._pending_wear
            self._api_fallback_wear = None

        osc_reason = self.osc.error or "OSC listener is not running"
        if not self.osc.error and self.osc.listening:
            if self.osc.change_avatar(avatar_id):
                self._set_status("Requested avatar change over OSC; waiting for VRChat confirmation.")
                self._debug_wear(SOURCE_OSC, avatar_id, "OSC /avatar/change packet sent")
                return {"ok": True, "pending": avatar_id}
            osc_reason = "OSC /avatar/change packet could not be sent"

        if self._start_api_fallback(avatar_id, request[1], f"OSC send unavailable: {osc_reason}"):
            return {"ok": True, "pending": avatar_id, "fallback": "api"}

        with self._lock:
            if self._pending_wear == request:
                self._pending_wear = None
                self._pending_wear_wall_time = None
                self._last_request = None
                self._api_fallback_wear = None
        message = (
            f"{osc_reason}. The VRChat API fallback was not attempted because the app "
            "is not authenticated. Log in in Settings to use API avatar selection."
        )
        self._debug_wear(SOURCE_OSC, avatar_id, f"send failed; {message}")
        self._set_status(message)
        return {"ok": False, "title": "Avatar wear request failed", "message": message}

    def _start_api_fallback(self, avatar_id: str, requested_at: float, reason: str) -> bool:
        """Start one authenticated API selection for the active wear request."""
        if not self.api.is_logged_in():
            return False
        request = (avatar_id, requested_at)
        with self._lock:
            if self._pending_wear != request:
                return False
            if self._api_fallback_wear == request:
                return True
            self._api_fallback_wear = request
        self._debug_wear("vrchat-api", avatar_id, f"starting fallback: {reason}")
        self._set_status(f"{reason}; trying authenticated VRChat API avatar selection...")
        threading.Thread(
            target=self._api_wear_worker,
            args=(avatar_id, requested_at, reason),
            daemon=True,
        ).start()
        return True

    def _api_wear_worker(self, avatar_id: str, requested_at: float, reason: str) -> None:
        """Run the API selection away from the UI and log-poll threads."""
        request = (avatar_id, requested_at)
        api_error = ""
        auth_expired = False
        outcome = ""
        try:
            # Read-only check first. The client reports its active avatar to
            # VRChat, so a matching value proves the request landed and avoids a
            # second, mutating switch on every successful wear.
            if self._norm_id(self.api.current_avatar()) == avatar_id:
                outcome = "confirmed"
            else:
                result = self.api.select_avatar(avatar_id)
                selected_id = self._norm_id(result.get("currentAvatar"))
                if selected_id and selected_id != avatar_id:
                    api_error = (f"API returned currentAvatar={selected_id} "
                                 f"instead of {avatar_id}")
                else:
                    outcome = "selected"
        except AuthError as exc:
            auth_expired = getattr(exc, "status", None) == 401
            api_error = str(exc) or "VRChat denied API avatar selection"
            if auth_expired:
                self._handle_session_expired(exc)
        except ApiError as exc:
            api_error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive bridge boundary
            api_error = f"Unexpected API error: {exc}"

        with self._lock:
            if self._pending_wear != request or self._api_fallback_wear != request:
                self._debug_wear("vrchat-api", avatar_id,
                                 "ignored result for a superseded wear request")
                return
            self._pending_wear = None
            self._pending_wear_wall_time = None
            self._last_request = None
            self._api_fallback_wear = None
            entry = self._entry(avatar_id)
            if outcome and entry is not None and entry.pop("inaccessible", None) is not None:
                storage.save_favourites(self.entries)
                self._touch("entries")
            label = entry.get("name") if entry and entry.get("name") else avatar_id

        if outcome == "confirmed":
            message = f"VRChat already reports {label} as your current avatar."
            self._debug_wear("vrchat-api", avatar_id,
                             f"confirmed server-side after {reason}")
        elif outcome == "selected":
            message = f"VRChat API selected {label} after {reason.lower()}."
            self._debug_wear("vrchat-api", avatar_id, f"selection accepted after {reason}")
        elif auth_expired:
            message = (
                f"{reason}; OSC was not confirmed and the VRChat API fallback failed because "
                "the app session expired. Log in again in Settings."
            )
            self._debug_wear("vrchat-api", avatar_id, f"authentication failed: {api_error}")
        else:
            message = f"{reason}; VRChat API avatar selection failed: {api_error}"
            self._debug_wear("vrchat-api", avatar_id, f"selection failed: {api_error}")
        self._set_status(message)

    def _expire_pending_wear(self) -> None:
        """Use the authenticated API if OSC receives no load/refusal confirmation."""
        with self._lock:
            pending = self._pending_wear
            if pending is None:
                return
            avatar_id, requested_at = pending
            age = time.monotonic() - requested_at
            if age < WEAR_CONFIRM_TIMEOUT or self._api_fallback_wear == pending:
                return
            entry = self._entry(avatar_id)
            name = entry.get("name") if entry and entry.get("name") else avatar_id

        reason = f"OSC request timed out after {age:.1f}s without client load confirmation"
        self._debug_wear("osc-timeout", avatar_id,
                         f"{reason}; current OSC avatar={self.osc.current_avatar_id or 'unknown'}")
        if self._start_api_fallback(avatar_id, requested_at, reason):
            return

        with self._lock:
            if self._pending_wear != pending:
                return
            self._pending_wear = None
            self._pending_wear_wall_time = None
            self._last_request = None
            self._api_fallback_wear = None
        self._set_status(
            f"OSC timed out without confirming {name}. The VRChat API fallback was skipped "
            "because the app is not authenticated; log in in Settings and try again."
        )

    def delete(self, avatar_id: str) -> dict:
        avatar_id = self._norm_id(avatar_id)
        with self._lock:
            before = len(self.entries)
            self.entries = [e for e in self.entries if self._norm_id(e.get("id")) != avatar_id]
            self._reindex()
            if len(self.entries) == before:
                return {"ok": False}
            storage.save_favourites(self.entries)
            self._touch("entries")
        # The cached image is deliberately left on disk: Undo re-inserts this
        # entry's thumb filename, and deleting the file here left Undo restoring
        # a name pointing at nothing, so the avatar came back with no picture.
        # Orphans are collected by prune_orphans_on_start and the Settings
        # "Clean thumbnails" action instead.
        self._set_status("Removed from favourites.")
        return {"ok": True}

    def copy_id(self, avatar_id: str) -> dict:
        return {"ok": _set_clipboard(avatar_id)}

    def copy_text(self, text: str) -> dict:
        return {"ok": _set_clipboard(text or "")}

    def save_details(self, avatar_id: str, name: str, notes: str, tags, group=None) -> dict:
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
            # None means "not supplied", so the drawer's older autosave -- which
            # passes four arguments -- leaves an existing group alone rather than
            # silently clearing it.
            if group is not None:
                entry["group"] = storage.normalize_group(group)
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
                if (not avatar_id or log_entry.get("private") or self._entry(avatar_id)
                        or is_default_avatar(avatar_id)):
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
            "VRChat session expired - log in again in Settings to fetch metadata and use API avatar selection."
        )

    def wear_last(self) -> dict:
        """Wear the most recently worn favourite. Used by the tray menu."""
        with self._lock:
            ordered = sorted(
                (e for e in self.log if e.get("id")),
                key=lambda e: e.get("last_seen", ""),
                reverse=True,
            )
            candidate = None
            for entry in ordered:
                found = self._entry(entry["id"])
                if found:
                    candidate = found
                    break
            if candidate is None and self.entries:
                candidate = max(self.entries, key=lambda e: e.get("added", ""))
        if candidate is None:
            return {"ok": False}
        return self.wear(candidate["id"])

    def set_tray(self, tray) -> None:
        """Attach the tray icon so status updates can raise a balloon."""
        self._tray = tray

    def _notify_tray(self, title: str, message: str) -> None:
        tray = getattr(self, "_tray", None)
        if tray is None:
            return
        try:
            tray.notify(title, message)
        except Exception:
            pass

    def _bulk_metadata(self, ids: list[str]) -> None:
        """Sequential metadata fetch. Used where no progress UI is wanted."""
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

    # -------------------------------------------------------------------- jobs
    def get_job(self, job_id: str = "") -> dict | None:
        """Progress for one job, or the most recent running one."""
        if job_id:
            return self._jobs.get(job_id)
        return self._jobs.active()

    def cancel_job(self, job_id: str) -> dict:
        if not self._jobs.cancel(job_id):
            return {"ok": False, "message": "That job is not running."}
        return {"ok": True}

    def start_metadata_job(self, ids) -> dict:
        """Fetch metadata for many avatars as a cancellable, visible job."""
        if not self.api.is_logged_in():
            return {"ok": False, "title": "Not logged in",
                    "message": "Log in first to look up names and thumbnails."}
        wanted = []
        seen: set[str] = set()
        for value in ids or []:
            avatar_id = self._norm_id(value)
            if avatar_id and avatar_id not in seen and self._entry(avatar_id):
                seen.add(avatar_id)
                wanted.append(avatar_id)
        if not wanted:
            return {"ok": False, "message": "Nothing selected."}

        job_id = self._runner.start(
            "metadata", wanted, self._metadata_worker,
            message=f"Fetching metadata for {len(wanted)} avatar(s)...",
        )
        self._set_status(f"Fetching metadata for {len(wanted)} avatar(s)...")
        return {"ok": True, "job": job_id, "total": len(wanted)}

    def bulk_action(self, ids, action: str, value: str = "") -> dict:
        """Apply one action to several favourites.

        Supported: favorite, unfavorite, tag, untag, group, ungroup, wear,
        refresh, delete.
        """
        targets = []
        seen: set[str] = set()
        for raw in ids or []:
            avatar_id = self._norm_id(raw)
            if avatar_id and avatar_id not in seen and self._entry(avatar_id):
                seen.add(avatar_id)
                targets.append(avatar_id)
        if not targets:
            return {"ok": False, "message": "Nothing selected."}

        action = (action or "").strip().lower()
        value = (value or "").strip()

        if action == "delete":
            removed = 0
            for avatar_id in targets:
                if self.delete(avatar_id).get("ok"):
                    removed += 1
            self._set_status(f"Removed {removed} avatar(s).")
            return {"ok": True, "changed": removed, "action": action}

        if action == "wear":
            # One switch only: the request is for a single avatar.
            self.wear(targets[0])
            return {"ok": True, "changed": 1, "action": action}

        if action == "refresh":
            job = self.start_metadata_job(targets)
            return {**job, "action": action}

        if action in ("favorite", "unfavorite", "tag", "untag", "group", "ungroup"):
            if action in ("tag", "group") and not value:
                return {"ok": False, "message": "Enter a value first."}
            with self._lock:
                changed = 0
                for avatar_id in targets:
                    entry = self._entry(avatar_id)
                    if entry is None:
                        continue
                    if action == "favorite":
                        entry["favorite"] = True
                    elif action == "unfavorite":
                        entry["favorite"] = False
                    elif action == "tag":
                        tags = [str(t) for t in entry.get("tags") or []]
                        if value not in tags:
                            tags.append(value)
                        entry["tags"] = tags
                    elif action == "untag":
                        entry["tags"] = [t for t in entry.get("tags") or [] if t != value]
                    elif action == "group":
                        entry["group"] = storage.normalize_group(value)
                    elif action == "ungroup":
                        entry["group"] = ""
                    else:
                        continue
                    changed += 1
                if changed:
                    storage.save_favourites(self.entries)
                    self._touch("entries")
            verb = {"favorite": "Favourited", "unfavorite": "Unfavourited",
                    "tag": "Tagged", "untag": "Untagged",
                    "group": "Moved", "ungroup": "Removed from group"}[action]
            detail = "" if action in ("group", "ungroup") else f" ({value})"
            self._set_status(f"{verb} {changed} avatar(s){detail}.")
            return {"ok": True, "changed": changed, "action": action}

        return {"ok": False, "message": f"Unknown action: {action}"}

    def restore_entry(self, entry: dict) -> dict:
        """Re-insert a deleted favourite, notes and tags intact. For undo."""
        if not isinstance(entry, dict):
            return {"ok": False}
        avatar_id = self._norm_id(entry.get("id"))
        if not avatar_id:
            return {"ok": False}
        with self._lock:
            if self._entry(avatar_id):
                return {"ok": False, "message": "Already in favourites."}
            restored = storage.new_entry(avatar_id)
            for key in ("name", "notes", "author", "release_status", "thumb",
                        "thumb_url", "added"):
                if entry.get(key):
                    restored[key] = entry[key]
            # Carried through an export/import round trip: a refusal is a fact
            # about the avatar, not about this install.
            if entry.get("inaccessible"):
                restored["inaccessible"] = True
            restored["group"] = storage.normalize_group(entry.get("group"))
            for key in ("tags", "platforms"):
                if isinstance(entry.get(key), list):
                    restored[key] = [str(v) for v in entry[key]]
            if isinstance(entry.get("favorite"), bool):
                restored["favorite"] = entry["favorite"]
            self.entries.append(restored)
            self._index_add(restored)
            storage.save_favourites(self.entries)
            self._touch("entries")
        self._set_status(f"Restored {restored.get('name') or avatar_id}.")
        return {"ok": True, "id": avatar_id}

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
        return self.start_metadata_job(ids)

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

        # Both locals are declared here, before the branch, on purpose.
        #
        # They used to be initialised inside the username/password branch, and
        # the shared error check at the bottom reads them. The 2FA branch never
        # touches that initialisation -- it verifies the code on the pending
        # client and falls straight through -- so a successful second-factor
        # verification reached `if last_error is not None` with the name never
        # bound, and raised UnboundLocalError. Every account with 2FA enabled
        # was therefore unable to log in at all.
        last_error: Exception | None = None
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

            # Normalise once, up front. The old code retried the whole
            # authenticated request with a stripped password, but every
            # credentialed request burns one of a limited number of
            # simultaneous VRChat sessions, so a pasted trailing space could
            # cost two.
            username = username.strip()
            password = password.strip()

            try:
                api = VRCApi()
                api.login(username, password)
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
            except Exception as exc:
                last_error = exc

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
    def save_settings(self, send_port, recv_port, exit_on_close=None, motion=None,
                       max_avatar_log=None, max_player_changes=None) -> dict:
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

        # Closing behaviour lives here too: the shell reads it when the window
        # is asked to close. Only overwrite when explicitly supplied, so the
        # older two-argument call from the self-test still works.
        if exit_on_close is not None:
            self.settings["exit_on_close"] = bool(exit_on_close)

        # Animation preference. "full" overrides a Windows accessibility setting
        # that would otherwise silently suppress every transition.
        if motion is not None:
            mode = str(motion).strip().lower()
            self.settings["motion"] = (
                mode if mode in storage.MOTION_MODES else storage.DEFAULT_MOTION
            )

        # Log caps, applied the same way: only when supplied, so the older
        # narrower calls still work. Rejected outright rather than clamped --
        # silently turning 0 into 800 while the user watches would be worse than
        # telling them the number is unusable.
        limits = (("max_avatar_log", max_avatar_log),
                  ("max_player_changes", max_player_changes))
        for key, value in limits:
            if value is None:
                continue
            try:
                limit = int(value)
            except (TypeError, ValueError):
                return {"ok": False, "message": "Log limits must be whole numbers."}
            if not (storage.MIN_LOG_LIMIT <= limit <= storage.MAX_LOG_LIMIT):
                return {
                    "ok": False,
                    "message": f"Log limits must be between {storage.MIN_LOG_LIMIT} "
                               f"and {storage.MAX_LOG_LIMIT}.",
                }
            self.settings[key] = limit

        # Only the receive port is actually bound. If it has not changed there is
        # nothing to rebind -- and attempting one would fail against our own
        # listener, which is holding that very port. Only the outgoing target
        # needs updating.
        current_recv = int(self.osc.receive_port)
        if recv_port == current_recv:
            self.osc.retarget(self.settings.get("osc_send_ip", "127.0.0.1"), send_port)
            if self.osc.error:
                return {"ok": False, "message": self.osc.error}
        else:
            # Bind before persisting. The old order wrote the settings first and
            # only then discovered the port was unavailable, so a single conflict
            # left the app unable to start OSC on every later launch with no way
            # to recover from the UI.
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

        # Enforce a lowered cap straight away, so the number in Settings
        # describes the list on screen rather than the list plus a future
        # overflow. Report what went, because silently discarding rows the user
        # can still see is the one outcome they would not expect.
        dropped_logs, dropped_changes = self.trim_logs_to_limits()
        self._set_status(self._settings_applied_message(dropped_logs, dropped_changes))
        return {"ok": True, "dropped_logs": dropped_logs,
                "dropped_changes": dropped_changes}

    @staticmethod
    def _settings_applied_message(dropped_logs: int, dropped_changes: int) -> str:
        parts = ["Settings saved."]
        if dropped_logs or dropped_changes:
            bits = []
            if dropped_logs:
                bits.append(f"{dropped_logs} older logged avatar"
                            f"{'s' if dropped_logs != 1 else ''}")
            if dropped_changes:
                bits.append(f"{dropped_changes} older player change"
                            f"{'s' if dropped_changes != 1 else ''}")
            parts.append(f"Dropped {', '.join(bits)} to fit the new limit.")
        return " ".join(parts)

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
