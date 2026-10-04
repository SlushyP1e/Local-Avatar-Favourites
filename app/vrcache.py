"""Discover real avatar IDs from VRChat's own local data files.

VRChat writes several artefacts to this PC that name avatars by ID. Two of them
are read here:

1. ``avatars.sqlite`` - a cumulative backlog table. Very cheap to query (a
   ``rowid`` high-water mark) and holds every avatar seen in the past, but it is
   only populated once some tool has written it; on a fresh VRChat install the
   table is missing or empty.
2. ``amplitude.cache`` - the *live* feed. VRChat rewrites, uploads and then
   clears this file on every world switch, so it has to be polled quickly and
   its contents de-duplicated against the backlog.

A third, lowest-priority source -- VRChat's text log -- lives in ``vrclog.py``.

Nothing here ever writes to VRChat's files. The database is opened through a
read-only URI, and no VRChat path is ever created when missing.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

# Strict hex-only pattern. Some third-party tooling uses the looser
# ``avtr_\w{8}-\w{4}-...`` which also matches non-hexadecimal ids.
AVATAR_ID_RE = re.compile(
    r"avtr_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Source layer keys, surfaced to the UI so that a silent fallback never looks
# like "no new avatars".
SOURCE_SQLITE = "cache-db"
SOURCE_AMPLITUDE = "amplitude"

# Per-layer status values.
OK = "ok"
EMPTY = "empty"
MISSING = "missing"
LOCKED = "locked"
UNREADABLE = "unreadable"
UNSUPPORTED = "unsupported"

_BUSY_CODES = ("database is locked", "database table is locked")
_BAD_CODES = ("file is not a database", "file is encrypted")


def normalize_avatar_id(value) -> str:
    return str(value or "").strip().lower()


def is_avatar_id(value: str) -> bool:
    return bool(AVATAR_ID_RE.fullmatch(value or ""))


def scan_ids(text: str) -> list[str]:
    """Extract unique, well-formed avatar ids from a block of text."""
    found: dict[str, None] = {}
    for match in AVATAR_ID_RE.findall(text or ""):
        found.setdefault(match.lower(), None)
    return list(found)


def _clean_ids(values) -> list[str]:
    out: dict[str, None] = {}
    for value in values:
        avatar_id = normalize_avatar_id(value)
        if is_avatar_id(avatar_id):
            out.setdefault(avatar_id, None)
    return list(out)


# --------------------------------------------------------------------- paths
def vrchat_low_dir() -> Path:
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / "AppData" / "LocalLow" / "VRChat" / "VRChat"


def amplitude_path() -> Path:
    temp = os.environ.get("TEMP") or str(Path.home() / "AppData" / "Local" / "Temp")
    return Path(temp) / "VRChat" / "VRChat" / "amplitude.cache"


def avatar_db_path(low_dir: Path | None = None) -> Path:
    """Locate the avatar cache database, honouring a relocated cache directory."""
    low = Path(low_dir) if low_dir else vrchat_low_dir()
    default = low / "avatars.sqlite"

    cache_dir = ""
    try:
        raw = json.loads((low / "config.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cache_dir = str(raw.get("cache_directory") or "").strip()
    except (OSError, json.JSONDecodeError):
        cache_dir = ""

    if not cache_dir:
        return default

    # VRChat stores the cache *parent* here and appends the folder itself.
    relocated = Path(os.path.expandvars(cache_dir)) / "Cache-WindowsPlayer" / default.name
    if relocated.exists() or not default.exists():
        return relocated
    return default


# -------------------------------------------------------------------- watcher
class VRCacheWatcher:
    """Polls VRChat's local avatar-ID sources and reports newly seen ids.

    Call :meth:`bootstrap` once at start-up so the existing backlog counts as
    already-seen instead of being emitted as tens of thousands of "new"
    discoveries. After that, :meth:`poll` yields only ids seen since the last
    call. The ``seen`` set holds one short string per known avatar, so a backlog
    of tens of thousands costs a few megabytes.
    """

    def __init__(
        self,
        low_dir: Path | None = None,
        amp_path: Path | None = None,
        busy_timeout: float = 2.0,
    ) -> None:
        self._low_dir = Path(low_dir) if low_dir else None
        self._amp_path = Path(amp_path) if amp_path else None
        self._busy_timeout = busy_timeout
        self._seen: set[str] = set()
        self._rowid = 0
        self._amp_sig: tuple[int, int] | None = None
        self.status: dict[str, str] = {
            SOURCE_SQLITE: MISSING,
            SOURCE_AMPLITUDE: MISSING,
        }

    # ------------------------------------------------------------------ paths
    @property
    def db_path(self) -> Path:
        return avatar_db_path(self._low_dir)

    @property
    def amp_file(self) -> Path:
        return self._amp_path if self._amp_path else amplitude_path()

    # --------------------------------------------------------------- start-up
    def bootstrap(self, seed_backlog: bool = True) -> int:
        """Record existing ids as already-seen. Returns the total now recorded."""
        if seed_backlog:
            self._seen.update(self._read_sqlite(emit=False))
        else:
            self._read_sqlite(emit=False)
        self._read_amplitude(emit=False)
        return len(self._seen)

    def backlog(self, limit: int = 200, offset: int = 0) -> list[str]:
        """One page of historical ids, newest first, for an explicit browse."""
        try:
            path = self.db_path
            if not path.exists():
                return []
            con = self._connect(path)
            rows = con.execute(
                "SELECT id FROM avatars ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
            con.close()
        except (sqlite3.Error, OSError, ValueError):
            return []
        return _clean_ids(row[0] for row in rows)

    def backlog_size(self) -> int:
        try:
            path = self.db_path
            if not path.exists():
                return 0
            con = self._connect(path)
            count = con.execute("SELECT COUNT(*) FROM avatars").fetchone()[0]
            con.close()
            return int(count or 0)
        except (sqlite3.Error, OSError):
            return 0

    # ------------------------------------------------------------------- poll
    def poll(self) -> list[str]:
        """Avatar ids seen since the previous call, in discovery order."""
        found: list[str] = []
        found.extend(self._read_sqlite())
        found.extend(self._read_amplitude())
        return self._accept(found)

    def _accept(self, ids) -> list[str]:
        fresh: list[str] = []
        for avatar_id in ids:
            if avatar_id and avatar_id not in self._seen:
                self._seen.add(avatar_id)
                fresh.append(avatar_id)
        return fresh

    # ---------------------------------------------------------- layer: sqlite
    def _connect(self, path: Path) -> sqlite3.Connection:
        uri = path.resolve().as_uri() + "?mode=ro"
        # Read-only, always. busy_timeout matters: VRChat uses a rollback
        # journal and takes an exclusive lock while writing, so without this a
        # poll raises "database is locked" instead of waiting a moment.
        return sqlite3.connect(uri, uri=True, timeout=self._busy_timeout)

    @staticmethod
    def _classify(exc: Exception) -> str:
        message = str(exc).lower()
        if any(code in message for code in _BUSY_CODES):
            return LOCKED
        if any(code in message for code in _BAD_CODES) or "encrypted" in message:
            return UNSUPPORTED
        if isinstance(exc, sqlite3.OperationalError):
            return UNSUPPORTED
        return UNREADABLE

    def _read_sqlite(self, emit: bool = True) -> list[str]:
        try:
            path = self.db_path
        except (OSError, ValueError):
            self.status[SOURCE_SQLITE] = UNREADABLE
            return []
        if not path.exists():
            self.status[SOURCE_SQLITE] = MISSING
            return []

        try:
            con = self._connect(path)
        except (sqlite3.Error, ValueError) as exc:
            self.status[SOURCE_SQLITE] = self._classify(exc)
            return []

        try:
            top = con.execute("SELECT MAX(rowid) FROM avatars").fetchone()[0]
            if top is None:
                # Table present but empty.
                self.status[SOURCE_SQLITE] = EMPTY
                return []

            top = int(top)
            if top < self._rowid:
                # Table was rebuilt or vacuumed and rowids restarted: resync
                # the watermark rather than replaying the backlog as new.
                self._rowid = top
                self.status[SOURCE_SQLITE] = OK
                return []

            previous = self._rowid
            self._rowid = top

            if not emit:
                rows = con.execute("SELECT id FROM avatars").fetchall()
            else:
                # rowid, not updated_at: re-inserts reorder updated_at, so it is
                # not monotonic and is unusable as a high-water mark.
                rows = con.execute(
                    "SELECT id FROM avatars WHERE rowid > ? ORDER BY rowid", (previous,)
                ).fetchall()

            ids = _clean_ids(row[0] for row in rows)
            self.status[SOURCE_SQLITE] = OK if (ids or not emit) else EMPTY
            return ids
        except sqlite3.Error as exc:
            self.status[SOURCE_SQLITE] = self._classify(exc)
            return []
        finally:
            con.close()

    # ------------------------------------------------------- layer: amplitude
    def _read_amplitude(self, emit: bool = True) -> list[str]:
        path = self.amp_file
        try:
            stat = path.stat()
        except OSError:
            self.status[SOURCE_AMPLITUDE] = MISSING
            return []

        signature = (stat.st_size, stat.st_mtime_ns)
        if emit and signature == self._amp_sig:
            # VRChat rewrites this constantly; most polls find nothing changed.
            return []
        self._amp_sig = signature

        if stat.st_size == 0:
            # VRChat uploads and clears it after every world switch, so empty is
            # the normal steady state rather than a failure.
            self.status[SOURCE_AMPLITUDE] = EMPTY
            return []

        try:
            data = path.read_bytes()
        except OSError as exc:
            self.status[SOURCE_AMPLITUDE] = self._classify(exc)
            return []

        ids = scan_ids(data.decode("utf-8", errors="ignore"))
        self.status[SOURCE_AMPLITUDE] = OK if ids else EMPTY
        return ids

    # ------------------------------------------------------------------ debug
    def describe(self) -> dict:
        return {
            "db_path": str(self.db_path),
            "amplitude_path": str(self.amp_file),
            "backlog": self.backlog_size(),
            "seen": len(self._seen),
            "rowid": self._rowid,
            "status": dict(self.status),
        }


__all__ = [
    "AVATAR_ID_RE", "EMPTY", "LOCKED", "MISSING", "OK", "SOURCE_AMPLITUDE",
    "SOURCE_SQLITE", "UNREADABLE", "UNSUPPORTED", "VRCacheWatcher",
    "amplitude_path", "avatar_db_path", "is_avatar_id", "normalize_avatar_id",
    "scan_ids", "vrchat_low_dir",
]
