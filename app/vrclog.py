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


def log_directory() -> Path:
    return (Path(os.environ.get("USERPROFILE", str(Path.home())))
            / "AppData" / "LocalLow" / "VRChat" / "VRChat")


def _line_time(line: str) -> str:
    match = TIMESTAMP_RE.match(line)
    if not match:
        return ""
    try:
        dt = datetime(*(int(g) for g in match.groups()))
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

        if self._path != latest:
            # New session (or VRChat restarted): read the current file from the
            # start so events made before the app launched are still captured.
            self._path = latest
            self._offset = 0

        try:
            size = latest.stat().st_size
            if size < self._offset:
                self._offset = 0
            if size == self._offset:
                return events
            with open(latest, "r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
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
