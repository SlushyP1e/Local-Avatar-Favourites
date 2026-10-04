"""Tail VRChat's client log to capture avatar changes and avatar IDs.

VRChat writes ``output_log_*.txt`` under
``%USERPROFILE%\\AppData\\LocalLow\\VRChat\\VRChat``. We watch the newest file
and extract:

* ``[Behaviour] Switching <player> to avatar <name>`` - who is wearing what
  (VRChat only logs the avatar *name* for remote players, not the ID).
* avatar IDs from the small set of lines that actually mean "this avatar is
  real and local" - see :data:`AVATAR_ID_LINE_RE`.

The second bullet used to be "every ``avtr_`` that appears anywhere in the file".
That was wrong. VRChat mentions avatar ids in a lot of places that have nothing
to do with an avatar being available, and on one real installation 562 of the
808 id-bearing lines were pure noise:

============================================  =====
``[API] [...] Avatar Not Found``               442
``[Image Download] ... /Home/avtr_....png``    120
``Target is empty: KeyDoesNotExist ...``        60
``Avatar 'avtr_...' did not pass initial ...``  3
============================================  =====

Only 161 lines were ``Saving/Loading Avatar Data:`` and 17 were the logged-in
user's own avatar. The image-URL lines were the worst of it: VRChat
preloads thumbnails for its own avatar-shop page, and every default avatar it
displays gets an id read straight out of a ``.png`` URL. That is where Robot,
Alien Rabbit and ［Protogen］Kuro came from in the first place -- VRChat never
downloaded them, it just fetched icons for a menu.

So the parser now matches only lines that positively indicate a real avatar,
rather than trying to enumerate everything that does not.
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

# The only line shapes from which an avatar id is treated as a discovery.
#
#   Loading Avatar Data:<id>   VRChat opened a locally cached avatar
#   Saving Avatar Data:<id>    VRChat wrote one into its local cache
#   - avatar: <id>             the login response dump; the local user's own
#                              current avatar
#
# Deliberately absent, each of which was measured on a real log set and each of
# which names ids that are not available avatars:
#
#   [API] [...avatars/<id>] Abandoning request, Avatar Not Found
#   [Image Download] Attempting to load image from URL '.../Home/<id>.png'
#   Target is empty: KeyDoesNotExist .../<id>.png
#   [AssetBundleDownloadManager] Avatar '<id>' did not pass initial checks and
#       won't be downloaded: AssetBundleFailedServerSideChecks
#
# The 404 lines were the single biggest source of clutter: they outnumbered real
# discoveries 2.7:1 and were the highest-count rows in the log, all of them
# avatars that do not exist. Re-widening this pattern to "any line containing an
# id" reintroduces all of it.
AVATAR_ID_LINE_RE = re.compile(
    r"(?:Loading Avatar Data:|Saving Avatar Data:|^\s*-\s+avatar:)\s*"
    r"(avtr_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.MULTILINE,
)

# Lines that name an avatar id but never mean the avatar is usable. Kept as a
# named set rather than folded into the pattern above so the reason each is
# excluded survives in the source, and so a stray "did not pass checks" id can
# never be mistaken for a discovery if the allowlist is ever widened.
IGNORED_ID_LINE_MARKERS = (
    "Avatar Not Found",
    "[Image Download]",
    "KeyDoesNotExist",
    "did not pass initial checks",
)

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

        # Cheap substring screen before the regex. The allowlist is narrow
        # enough that this never matched anything the pattern would have
        # rejected anyway, but it keeps the hot loop -- one call per log line,
        # thousands of times a second while a poll is catching up -- down to a
        # few string compares for the overwhelming majority of lines.
        if "avtr_" not in line:
            return
        if any(marker in line for marker in IGNORED_ID_LINE_MARKERS):
            return

        match = AVATAR_ID_LINE_RE.search(line)
        if not match:
            return
        events.append({
            "type": "avatar-id",
            "id": match.group(1).lower(),
            "time": _line_time(line),
        })
