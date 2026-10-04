"""Tail VRChat's client log to capture avatar changes and avatar IDs.

VRChat writes ``output_log_*.txt`` under
``%USERPROFILE%\\AppData\\LocalLow\\VRChat\\VRChat``. We watch the newest file
and extract:

* ``[Behaviour] Switching <player> to avatar <name>`` - who is wearing what
  (VRChat only logs the avatar *name* for remote players, not the ID).
* every ``avtr_<uuid>`` that appears - the local user's avatar changes, avatars
  loaded from the local cache, and API lookups. These carry the real ID, which
  is what's needed to clone an avatar.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

AVATAR_ID_RE = re.compile(
    r"(avtr_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
SWITCH_RE = re.compile(r"\[Behaviour\] Switching (.+) to avatar (.+?)\s*$")
TIMESTAMP_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2}) (\d{2}):(\d{2}):(\d{2})")

# VRChat's log grows without bound during a session. On a new session we start
# this far from the end rather than at byte 0: re-parsing a few hundred
# megabytes in one gulp blocks the poll loop and holds the GIL.
TAIL_BYTES = 4 * 1024 * 1024
# Upper bound on how much is consumed per poll, so a burst cannot stall the loop.
MAX_CHUNK_BYTES = 2 * 1024 * 1024


def log_directory() -> Path:
    return (Path(os.environ.get("USERPROFILE", str(Path.home())))
            / "AppData" / "LocalLow" / "VRChat" / "VRChat")


def _line_time(line: str) -> str:
    match = TIMESTAMP_RE.match(line)
    if not match:
        return ""
    try:
        year, month, day, hour, minute, second = (int(g) for g in match.groups())
        dt = datetime(year, month, day, hour, minute, second)
        return dt.astimezone().isoformat(timespec="seconds")
    except ValueError:
        return ""


class VRCLogWatcher:
    """Incrementally reads the newest VRChat log file and yields parsed events."""

    def __init__(self) -> None:
        self._path: Path | None = None
        self._offset = 0

    def latest_log(self) -> Path | None:
        directory = log_directory()
        if not directory.exists():
            return None
        files = [p for p in directory.glob("output_log_*.txt") if p.is_file()]
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    def poll(self) -> list[dict]:
        events: list[dict] = []
        latest = self.latest_log()
        if latest is None:
            return events

        try:
            size = latest.stat().st_size
        except OSError:
            return events

        if self._path != latest:
            # New session (or VRChat restarted). Start near the end of the file:
            # anything older has already been reported by a previous run, and
            # reading the whole file from byte 0 is slow enough to stall us.
            self._path = latest
            self._offset = max(0, size - TAIL_BYTES)

        if size < self._offset:
            # Truncated or replaced underneath us; start over from the tail.
            self._offset = max(0, size - TAIL_BYTES)
        if size == self._offset:
            return events

        try:
            with open(latest, encoding="utf-8", errors="ignore") as handle:
                if self._offset:
                    # A mid-file seek can land inside a line; drop the fragment.
                    handle.seek(self._offset)
                    handle.readline()
                    handle.seek(handle.tell())
                chunk = handle.read(MAX_CHUNK_BYTES)
                self._offset = handle.tell()
        except OSError:
            return events

        for line in chunk.splitlines():
            self._parse_line(line, events)
        return events

    @staticmethod
    def _parse_line(line: str, events: list[dict]) -> None:
        switch = SWITCH_RE.search(line)
        if switch:
            player = switch.group(1).strip()
            avatar = switch.group(2).strip()
            if player and avatar:
                events.append({
                    "type": "avatar-change",
                    "player": player,
                    "avatar": avatar,
                    "time": _line_time(line),
                })
            return

        for avatar_id in AVATAR_ID_RE.findall(line):
            events.append({
                "type": "avatar-id",
                "id": avatar_id.lower(),
                "time": _line_time(line),
            })
