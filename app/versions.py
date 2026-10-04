"""Semantic version comparison for the update check.

Kept free of GUI and network imports so the self-test can exercise it without
pulling in pywebview.
"""

from __future__ import annotations

import re

# A leading "v" is conventional in Git tags; anything else is not a version.
_TAG_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(value) -> tuple[tuple[int, ...], bool] | None:
    """Parse ``value`` into ``(release, is_prerelease)``.

    Returns None when the tag is not a recognisable version, so callers can
    ignore malformed data instead of guessing.
    """
    match = _TAG_RE.match(str(value or "").strip())
    if not match:
        return None
    release = tuple(int(part) for part in match.group(1).split("."))
    return release, bool(match.group(2))


def is_newer(latest, current) -> bool:
    """True when ``latest`` is a newer *release* than ``current``.

    A prerelease on the remote side is never offered as an update, and a local
    prerelease is treated as older than the same-numbered final release.
    """
    parsed_latest = parse_version(latest)
    parsed_current = parse_version(current)
    if parsed_latest is None or parsed_current is None:
        return False

    latest_release, latest_pre = parsed_latest
    current_release, current_pre = parsed_current

    if latest_pre:
        # Do not advertise 1.2.0-beta.1 to someone on 1.2.0.
        return False

    length = max(len(latest_release), len(current_release))
    a = latest_release + (0,) * (length - len(latest_release))
    b = current_release + (0,) * (length - len(current_release))
    if a != b:
        return a > b

    # Same numbers: a final release beats a local prerelease.
    return bool(current_pre)


__all__ = ["is_newer", "parse_version"]
