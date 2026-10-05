"""Headless self-test for the Local Avatar Favourites core.

Run with:  python -m app.selftest
Exits with code 0 on success, 1 on failure. Does not open any windows or
make network calls.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
from osc import AVATAR_CHANGE_ADDRESS, OSCBridge

RESULTS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    RESULTS.append(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_storage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage.DATA_DIR = Path(tmp)
        storage.FAVS_FILE = Path(tmp) / "favourites.json"
        storage.SETTINGS_FILE = Path(tmp) / "settings.json"
        storage.THUMBS_DIR = Path(tmp) / "thumbs"

        assert storage.load_favourites() == []
        entry = storage.new_entry("avtr_1234-5678", "Test Bot")
        entry["notes"] = "hello"
        entry["tags"] = ["furry", "quest"]
        storage.save_favourites([entry])

        loaded = storage.load_favourites()
        check("storage round-trip", len(loaded) == 1 and loaded[0]["id"] == entry["id"])
        check("storage fields", loaded[0]["name"] == "Test Bot" and loaded[0]["tags"] == ["furry", "quest"])
        check("storage thumb path", storage.thumb_file_path("avtr_x", "jpg").name == "avtr_x.jpg")

        settings = storage.load_settings()
        check("default settings", settings["osc_send_port"] == 9000)
        settings["auth_token"] = "tok123"
        storage.save_settings(settings)
        check("settings round-trip", storage.load_settings()["auth_token"] == "tok123")


def test_osc_receive() -> None:
    port = 18901
    bridge = OSCBridge(send_ip="127.0.0.1", send_port=19990, receive_port=port)
    received: list[str] = []
    bridge.add_avatar_change_listener(received.append)
    bridge.start()
    check("osc starts listening", bridge.listening, bridge.error or "")
    try:
        from pythonosc.udp_client import SimpleUDPClient

        client = SimpleUDPClient("127.0.0.1", port)
        client.send_message(AVATAR_CHANGE_ADDRESS, "avtr_aaaa-bbbb")
        deadline = time.time() + 5
        while not received and time.time() < deadline:
            time.sleep(0.05)
        check("osc avatar change received", received == ["avtr_aaaa-bbbb"])
        check("osc current id", bridge.current_avatar_id == "avtr_aaaa-bbbb")
    finally:
        bridge.stop()


def test_osc_send() -> None:
    bridge = OSCBridge(send_ip="127.0.0.1", send_port=19990, receive_port=18902)
    bridge.start()
    try:
        ok = bridge.change_avatar("avtr_cccc-dddd")
        check("osc send no exception", ok is True)
    finally:
        bridge.stop()


def test_api_helpers() -> None:
    from api import VRCApi, _auth_cookie

    api = VRCApi("my-token")
    check("api token from cookie", api.token == "my-token")
    check("api logged in", api.is_logged_in())

    fresh = VRCApi()
    check("api logged out", not fresh.is_logged_in())

    cookie = _auth_cookie("tok2")
    check("cookie name", cookie.name == "auth")
    check("cookie domain", cookie.domain == "api.vrchat.cloud")


def test_storage_merge() -> None:
    existing = [storage.new_entry("avtr_aaaa", "Existing")]
    incoming = [
        {"id": "AVTR_aaaa", "name": "Duplicate"},          # duplicate (case-insensitive)
        {"id": "avtr_bbbb", "name": "New", "tags": ["x"], "favorite": True},
        {"name": "No id"},                                  # skipped
    ]
    merged, added = storage.merge_favourites(existing, incoming)
    check("merge skips duplicates", added == 1 and len(merged) == 2)
    by_id = {e["id"]: e for e in merged}
    check("merge imports fields", by_id["avtr_bbbb"]["name"] == "New"
          and by_id["avtr_bbbb"]["favorite"] is True)
    check("normalize id", storage.normalize_id("  AVTR_X  ") == "avtr_x")


def test_vrclog_parse() -> None:
    import vrclog

    events: list[dict] = []
    vrclog.VRCLogWatcher._parse_line(
        "2024.01.02 03:04:05 Log        -  [Behaviour] Switching Bob to avatar Cool Avatar", events)
    check("vrclog switch parse", len(events) == 1 and events[0]["type"] == "avatar-change"
          and events[0]["player"] == "Bob" and events[0]["avatar"] == "Cool Avatar")

    # Regression: the id test used to be a bare "avtr_... loaded" line, which
    # passed only because every id on every line was harvested. The parser now
    # matches an allowlist of real discovery lines, so the fixture has to name
    # one -- otherwise this test would keep passing against a parser that
    # returns nothing at all.
    for label, line in (
        ("saving", "2024.01.02 03:04:05 Debug - Saving Avatar Data:"
                   "avtr_12345678-1234-1234-1234-123456789abc"),
        ("loading", "2024.01.02 03:04:05 Debug - Loading Avatar Data:"
                    "avtr_12345678-1234-1234-1234-123456789abc"),
        ("login dump", "2024.01.02 03:04:05 Debug - User Authenticated: Someone\n"
                       "- avatar: avtr_12345678-1234-1234-1234-123456789abc"),
    ):
        id_events: list[dict] = []
        vrclog.VRCLogWatcher._parse_line(line, id_events)
        check(f"vrclog id parse ({label})", len(id_events) == 1
              and id_events[0]["type"] == "avatar-id"
              and id_events[0]["id"] == "avtr_12345678-1234-1234-1234-123456789abc",
              str(id_events))


def test_vrclog_ignores_noise_lines() -> None:
    """Lines that name an avatar id without the avatar being available.

    Measured on a real 145k-line log set, these four shapes accounted for 625 of
    the 808 id-bearing lines. The 404s alone outnumbered real discoveries 2.7:1
    and were the highest-count rows in the log. They are reproduced here
    verbatim in shape, because the whole point is that the parser recognises
    them as noise rather than as findings.
    """
    import vrclog

    aid = "avtr_0fc33a74-b8ff-4e27-9b7d-c15b03d3d07e"
    noise = {
        "api 404": (f"2026.10.04 16:20:24 Error      -  [API] [202, 404, Get, -1 "
                    f"https://api.vrchat.cloud/api/1/avatars/{aid}]       Abandoning "
                    f"request, because - Avatar Not Found"),
        "thumbnail url": ("2026.10.05 01:07:34 Debug      -  [Image Download] Attempting to "
                          f"load image from URL 'https://assets.vrchat.com/content-home-upload"
                          f"/Home/{aid}.png'"),
        "missing image": (f"2026.10.05 01:07:34 Error      -  Target is empty: "
                          f"KeyDoesNotExist https://assets.vrchat.com/content-home-upload"
                          f"/Home/{aid}.png"),
        "failed download": (f"2026.10.04 16:20:37 Error      -  "
                            f"[AssetBundleDownloadManager] Avatar '{aid}' did not pass "
                            f"initial checks and won't be downloaded: "
                            f"AssetBundleFailedServerSideChecks"),
    }
    for label, line in noise.items():
        events: list[dict] = []
        vrclog.VRCLogWatcher._parse_line(line, events)
        check(f"vrclog ignores {label}", events == [], str(events))

    # And the neighbouring real shape must still parse, or the test above would
    # pass for the wrong reason.
    events = []
    vrclog.VRCLogWatcher._parse_line(
        f"2026.10.04 16:20:24 Debug      -  Saving Avatar Data:{aid}", events)
    check("vrclog still accepts a real line", len(events) == 1, str(events))


def test_default_avatar_list() -> None:
    """The curated default-avatar data has to stay well-formed.

    This cannot check that the ids are the *right* ones -- a wrong-but-valid
    UUID would pass every assertion here and silently fail to filter. What it
    can do is pin the format and a few sentinels, so a bad edit to the generated
    file is caught rather than shipped.
    """
    import re

    import vrcdetails

    pattern = re.compile(
        r"avtr_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    check("default ids are non-empty", len(vrcdetails.DEFAULT_AVATAR_IDS) > 200,
          str(len(vrcdetails.DEFAULT_AVATAR_IDS)))
    malformed = [i for i in vrcdetails.DEFAULT_AVATAR_IDS
                 if not pattern.fullmatch(i)]
    check("every default id is well formed", not malformed, str(malformed[:5]))
    check("default ids are lowercase",
          all(i == i.lower() for i in vrcdetails.DEFAULT_AVATAR_IDS))
    check("default names are case folded",
          all(n == n.casefold() for n in vrcdetails.DEFAULT_AVATAR_NAMES))

    # Sentinels spanning every source table, including the community-authored
    # defaults that an authorName == "VRChat" check would have missed.
    for label, avatar_id in (
        ("Robot", "avtr_c38a1615-5bf5-42b4-84eb-a8b6c37cbd11"),
        ("Unity-chan", "avtr_712e5c3c-2deb-4cae-a414-79b2a814a90b"),
        ("Papyrus", "avtr_c0d0b0ac-3a29-4f5a-a6d4-3ef3c24c6d75"),
        ("Alien Rabbit", "avtr_8c2625bb-9234-40d8-92f2-e7da98fb556b"),
        ("Protogen Kuro", "avtr_faf40a9f-ce39-4ff2-a069-223797ba11af"),
        ("Sand sculpture protogen", "avtr_41357a9c-b1c8-43ef-a34f-5a5ddaaa29c8"),
        ("VRRat", "avtr_26187637-0c30-4a09-86e1-bc928c07309e"),
    ):
        check(f"default list contains {label}",
              vrcdetails.is_default_avatar(avatar_id), avatar_id)

    check("an ordinary id is not a default",
          not vrcdetails.is_default_avatar("avtr_32611e23-2508-4cbf-accb-b8625e37f546"))
    check("default id match ignores case and padding",
          vrcdetails.is_default_avatar("  AVTR_C38A1615-5BF5-42B4-84EB-A8B6C37CBD11  "))
    check("default id match rejects non-strings",
          not vrcdetails.is_default_avatar(123) and not vrcdetails.is_default_avatar(None))


def test_default_avatar_name_matching() -> None:
    """Name matching on the player-changes tab is exact, never substring."""
    import vrcdetails

    for name in ("Robot", "Unity-chan", "Papyrus", "VRRat", "［Protogen］Kuro"):
        check(f"default name matches {name}", vrcdetails.is_default_avatar_name(name))
    check("default name match ignores case and padding",
          vrcdetails.is_default_avatar_name("  rObOt  "))

    # The false-positive risk of matching on names alone. "Fallback" is a real
    # user avatar by Nolando that players pick as their Quest fallback -- it is
    # not a VRChat default and must survive.
    for name in ("Fallback", "FallBack", "Robot Deluxe", "Papyrus Mark II",
                 "X-Bot 3000", "", None, 42):
        check(f"not a default name: {name!r}",
              not vrcdetails.is_default_avatar_name(name))


def test_update_compare() -> None:
    # Imported from a dependency-free module: pulling in backend here would drag
    # pywebview into a self-test that claims to need no GUI stack.
    from versions import is_newer, parse_version

    check("version newer", is_newer("1.2.0", "1.1.0"))
    check("version same", not is_newer("1.1.0", "1.1.0"))
    check("version older", not is_newer("1.0.0", "1.1.0"))
    check("version minor ordering", is_newer("1.10.0", "1.9.0"))
    check("version v-prefix", is_newer("v1.3.0", "1.2.9"))
    check("version patch ordering", is_newer("1.1.1", "1.1.0"))
    check("version trailing zeros equal", not is_newer("1.2", "1.2.0"))
    check("version longer wins", is_newer("1.2.1", "1.2"))

    # Regressions: a prerelease must never be advertised as an update.
    check("remote prerelease ignored", not is_newer("1.2.0-beta.1", "1.2.0"))
    check("remote prerelease ignored (older)", not is_newer("1.3.0-rc1", "1.2.0"))
    check("local prerelease beaten by final", is_newer("1.2.0", "1.2.0-beta.1"))
    check("local prerelease not beaten", not is_newer("1.2.0-beta.2", "1.2.0-beta.1"))

    # Malformed tags are ignored rather than guessed at.
    check("garbage tag rejected", not is_newer("garbage", "1.1.1"))
    check("empty tag rejected", not is_newer("", "1.1.1"))
    check("release- prefix rejected", not is_newer("release-1.3.0", "1.2.0"))
    check("unparseable current rejected", not is_newer("1.2.0", "banana"))
    check("parse_version shape", parse_version("v2.1.0") == ((2, 1, 0), False))
    check("parse_version prerelease", parse_version("2.1.0-rc1") == ((2, 1, 0), True))


# --------------------------------------------------------------- vrcache
def _fake_ids(count: int, start: int = 0) -> list[str]:
    return [f"avtr_{i:08x}-1234-1234-1234-{i:012x}" for i in range(start, start + count)]


def _write_sqlite(path: Path, ids: list[str]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE avatars (id TEXT PRIMARY KEY, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
        "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
        "provider_bits INT DEFAULT 0)"
    )
    con.executemany("INSERT INTO avatars (id) VALUES (?)", [(i,) for i in ids])
    con.commit()
    con.close()


def _append_sqlite(path: Path, ids: list[str]) -> None:
    con = sqlite3.connect(path)
    con.executemany("INSERT INTO avatars (id) VALUES (?)", [(i,) for i in ids])
    con.commit()
    con.close()


def _fixture(tmp: Path, ids=None, amplitude=None, config=None, make_db=True):
    """Build a synthetic VRChat data directory. Never touches the real one."""
    low = tmp / "LocalLow" / "VRChat" / "VRChat"
    low.mkdir(parents=True, exist_ok=True)
    if make_db and ids is not None:
        _write_sqlite(low / "avatars.sqlite", ids)
    if config is not None:
        (low / "config.json").write_text(json.dumps(config), encoding="utf-8")
    amp = tmp / "amp.cache"
    amp.write_bytes(amplitude if amplitude is not None else b"")
    return low, amp


def test_vrcache_pattern() -> None:
    from vrcache import AVATAR_ID_RE, is_avatar_id, normalize_avatar_id, scan_ids

    good = "avtr_12345678-1234-1234-1234-123456789abc"
    check("vrcache accepts valid id", is_avatar_id(good))
    check("vrcache rejects short id", not is_avatar_id("avtr_1234"))
    check("vrcache rejects wrong prefix", not is_avatar_id("usr_12345678-1234-1234-1234-123456789abc"))
    # Third-party tooling uses a loose \w pattern that matches non-hex ids.
    check("vrcache rejects non-hex", not is_avatar_id("avtr_zzzzzzzz-1234-1234-1234-123456789abc"))
    check("vrcache fullmatch anchored", AVATAR_ID_RE.fullmatch(good) is not None)
    check("vrcache normalize", normalize_avatar_id("  AVTR_X ") == "avtr_x")

    text = f"a {good} b {good.upper()} c avtr_zzzzzzzz-1234-1234-1234-123456789abc d"
    found = scan_ids(text)
    check("vrcache scan dedupes and lowercases", found == [good], str(found))


def test_vrcache_db_path() -> None:
    from vrcache import avatar_db_candidates, avatar_db_path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        low = root / "LocalLow" / "VRChat" / "VRChat"
        low.mkdir(parents=True)
        check("vrcache default path", avatar_db_path(low).name == "avatars.sqlite")
        check("vrcache default parent", avatar_db_path(low).parent == low)

        # No config.json at all.
        check("vrcache missing config -> default", avatar_db_path(low).parent == low)

        # A relocated cache. avatars.sqlite is not inside Cache-WindowsPlayer --
        # that folder holds only hashed asset bundles whose __info files carry a
        # timestamp and a filename and nothing else, so an avatar ID is not
        # recoverable from them. The old code looked there unconditionally, so
        # anyone whose cache was relocated was told the local cache was
        # unavailable via a path that could never exist.
        relocated = root / "SomeOtherCache"
        relocated.mkdir(parents=True)
        (low / "config.json").write_text(
            json.dumps({"cache_directory": str(relocated)}), encoding="utf-8")
        check("relocated cache is probed at the cache root",
              relocated / "avatars.sqlite" in avatar_db_candidates(low),
              str(avatar_db_candidates(low)))
        check("the default is still probed first",
              avatar_db_candidates(low)[0] == low / "avatars.sqlite",
              str(avatar_db_candidates(low)))
        check("the relocated candidate is not nested under Cache-WindowsPlayer",
              "Cache-WindowsPlayer" not in (relocated / "avatars.sqlite").parts)
        # The nested path survives only as a last-resort fallback, so it must
        # never be preferred over one that does not exist higher up.
        nested_candidate = relocated / "Cache-WindowsPlayer" / "avatars.sqlite"
        check("the nested fallback is probed last",
              avatar_db_candidates(low)[-1] == nested_candidate,
              str(avatar_db_candidates(low)))

        # The default wins when it exists, because avatars.sqlite is written by
        # third-party trackers that hardcode the default path even when VRChat's
        # asset cache has been relocated.
        (low / "avatars.sqlite").write_bytes(b"")
        check("an existing default database wins",
              avatar_db_path(low) == low / "avatars.sqlite",
              str(avatar_db_path(low)))

        # With no default present, the relocated location is found instead.
        (low / "avatars.sqlite").unlink()
        (relocated / "avatars.sqlite").write_bytes(b"")
        check("existing relocated database is used",
              avatar_db_path(low) == relocated / "avatars.sqlite",
              str(avatar_db_path(low)))

        # A nested layout, if one ever appears, must still be found rather than
        # reported missing: whichever candidate exists wins.
        (relocated / "avatars.sqlite").unlink()
        nested = relocated / "Cache-WindowsPlayer"
        nested.mkdir(parents=True)
        (nested / "avatars.sqlite").write_bytes(b"")
        check("nested layout still resolves",
              avatar_db_path(low) == nested / "avatars.sqlite",
              str(avatar_db_path(low)))

        # Configured, nothing written anywhere: report the documented relocated
        # location, so the path shown to the user is worth checking.
        (nested / "avatars.sqlite").unlink()
        nested.rmdir()
        check("configured and absent -> documented location",
              avatar_db_path(low) == relocated / "avatars.sqlite",
              str(avatar_db_path(low)))
        check("absent and unconfigured -> default reported",
              avatar_db_path(low.parent) == low.parent / "avatars.sqlite",
              str(avatar_db_path(low.parent)))

        # A hand-edited config may use env vars or ~ shortcuts.
        (low / "config.json").write_text(
            json.dumps({"cache_directory": "~/vrc-cache/"}), encoding="utf-8")
        resolved = avatar_db_path(low)
        check("~ in cache_directory is expanded",
              resolved == Path.home() / "vrc-cache" / "avatars.sqlite",
              str(resolved))

        # A corrupt config must not break resolution.
        (low / "config.json").write_text("{not json", encoding="utf-8")
        check("vrcache corrupt config -> default", avatar_db_path(low).parent == low)
        # So must a blank one, or one with an unrelated shape.
        (low / "config.json").write_text(
            json.dumps({"cache_directory": "   "}), encoding="utf-8")
        check("blank cache_directory -> default", avatar_db_path(low).parent == low)
        (low / "config.json").write_text(json.dumps(["not", "a", "dict"]),
                                         encoding="utf-8")
        check("non-dict config -> default", avatar_db_path(low).parent == low)
def test_vrcache_sqlite_watermark() -> None:
    from vrcache import EMPTY, OK, SOURCE_SQLITE, VRCacheWatcher

    with tempfile.TemporaryDirectory() as tmp:
        low, amp = _fixture(Path(tmp), ids=_fake_ids(5))
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        seeded = w.bootstrap()
        check("vrcache seeds backlog", seeded == 5, str(seeded))
        check("vrcache status ok after seed", w.status[SOURCE_SQLITE] == OK)

        check("vrcache no new ids initially", w.poll() == [])
        check("vrcache status empty when caught up", w.status[SOURCE_SQLITE] == EMPTY)

        new_ids = _fake_ids(3, start=100)
        _append_sqlite(low / "avatars.sqlite", new_ids)
        check("vrcache reports exactly the new ids", w.poll() == new_ids)

        check("vrcache does not repeat", w.poll() == [])

        more = _fake_ids(2, start=200)
        _append_sqlite(low / "avatars.sqlite", more)
        check("vrcache second batch", w.poll() == more)

        check("vrcache backlog paging", w.backlog(limit=2) == more[::-1],
              str(w.backlog(limit=2)))
        check("vrcache backlog offset", w.backlog(limit=2, offset=2) == new_ids[::-1][:2],
              str(w.backlog(limit=2, offset=2)))
        check("vrcache backlog size", w.backlog_size() == 10)


def test_vrcache_rowid_regression() -> None:
    """A rebuilt/vacuumed table restarts rowids; that must not replay history."""
    from vrcache import VRCacheWatcher

    with tempfile.TemporaryDirectory() as tmp:
        low, amp = _fixture(Path(tmp), ids=_fake_ids(5))
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        w.bootstrap()
        check("vrcache seeded", w.poll() == [])

        # Rebuild with fewer rows, so rowids restart below the watermark.
        (low / "avatars.sqlite").unlink()
        _write_sqlite(low / "avatars.sqlite", _fake_ids(2, start=500))
        check("vrcache does not replay on rowid reset", w.poll() == [])

        # And it keeps working afterwards.
        fresh = _fake_ids(1, start=900)
        _append_sqlite(low / "avatars.sqlite", fresh)
        check("vrcache resumes after reset", w.poll() == fresh)


def test_vrcache_degraded_sources() -> None:
    from vrcache import MISSING, SOURCE_SQLITE, UNSUPPORTED, VRCacheWatcher

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # 1. No database at all.
        low, amp = _fixture(root / "a", make_db=False)
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        check("vrcache missing db -> missing", w.status[SOURCE_SQLITE] == MISSING
              or w.poll() == [])
        w.poll()
        check("vrcache missing db status", w.status[SOURCE_SQLITE] == MISSING)

        # 2. Database present but not a database (VRChat has encrypted it before).
        low2, amp2 = _fixture(root / "b", make_db=False)
        (low2 / "avatars.sqlite").write_bytes(b"\x00\x01\x02not-a-sqlite-file" * 40)
        w2 = VRCacheWatcher(low_dir=low2, amp_path=amp2)
        check("vrcache garbage db yields nothing", w2.poll() == [])
        check("vrcache garbage db -> unsupported", w2.status[SOURCE_SQLITE] == UNSUPPORTED,
              w2.status[SOURCE_SQLITE])

        # 3. Valid sqlite, but no avatars table.
        low3, amp3 = _fixture(root / "c", make_db=False)
        con = sqlite3.connect(low3 / "avatars.sqlite")
        con.execute("CREATE TABLE something_else (x INTEGER)")
        con.commit()
        con.close()
        w3 = VRCacheWatcher(low_dir=low3, amp_path=amp3)
        check("vrcache missing table yields nothing", w3.poll() == [])
        check("vrcache missing table -> unsupported", w3.status[SOURCE_SQLITE] == UNSUPPORTED,
              w3.status[SOURCE_SQLITE])

        # 4. Empty but valid avatars table.
        low4, amp4 = _fixture(root / "d", ids=[])
        w4 = VRCacheWatcher(low_dir=low4, amp_path=amp4)
        check("vrcache empty table yields nothing", w4.poll() == [])
        check("vrcache empty table -> empty", w4.status[SOURCE_SQLITE] == "empty",
              w4.status[SOURCE_SQLITE])


def test_vrcache_amplitude() -> None:
    from vrcache import EMPTY, MISSING, OK, SOURCE_AMPLITUDE, VRCacheWatcher

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ids = _fake_ids(3)

        # Amplitude file absent.
        low, amp = _fixture(root / "missing", make_db=False, amplitude=None)
        amp.unlink()
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        w.poll()
        check("vrcache amplitude missing", w.status[SOURCE_AMPLITUDE] == MISSING)

        # Amplitude present but empty: the normal steady state.
        low, amp = _fixture(root / "empty", make_db=False, amplitude=b"")
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        check("vrcache amplitude empty yields nothing", w.poll() == [])
        check("vrcache amplitude empty status", w.status[SOURCE_AMPLITUDE] == EMPTY)

        # Amplitude containing ids.
        low, amp = _fixture(root / "full", make_db=False,
                            amplitude=("noise " + " ".join(ids) + " noise").encode())
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        check("vrcache amplitude yields ids", w.poll() == ids, str(w.poll()))
        check("vrcache amplitude ok status", w.status[SOURCE_AMPLITUDE] == OK)

        # Unchanged file must not be rescanned.
        before = dict(w.status)
        amp.write_bytes(amp.read_bytes())
        time.sleep(0.01)
        os.utime(amp, (time.time() + 5, time.time() + 5))
        check("vrcache amplitude skips unchanged", w.poll() == [])
        check("vrcache amplitude status preserved", w.status[SOURCE_AMPLITUDE] == before[SOURCE_AMPLITUDE])

        # VRChat clears the file after uploading: new ids still get picked up.
        amp.write_bytes(" ".join(_fake_ids(2, start=700)).encode())
        check("vrcache amplitude after rewrite", w.poll() == _fake_ids(2, start=700))


def test_vrcache_dedupe_across_layers() -> None:
    from vrcache import VRCacheWatcher

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        backlog = _fake_ids(3)
        live = _fake_ids(2, start=50)
        shared = _fake_ids(1, start=90)

        low, amp = _fixture(
            root / "dedupe",
            ids=[*backlog, shared[0]],
            amplitude=(" ".join(live + shared)).encode(),
        )
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        # Seed only from the database so amplitude entries are still "new".
        w._read_sqlite(emit=False)
        w._seen.update([*backlog, shared[0]])

        got = w.poll()
        check("vrcache dedupes shared id across layers", got == live, str(got))
        check("vrcache shared id not repeated", w.poll() == [])


def test_vrcache_is_read_only() -> None:
    """The watcher must never modify anything under VRChat's data directory."""
    import hashlib

    from vrcache import VRCacheWatcher

    with tempfile.TemporaryDirectory() as tmp:
        low, amp = _fixture(Path(tmp), ids=_fake_ids(4),
                            amplitude=(" ".join(_fake_ids(2, start=800))).encode())
        db = low / "avatars.sqlite"

        def digest():
            h = hashlib.sha256()
            for path in sorted(low.rglob("*")):
                if path.is_file():
                    h.update(path.name.encode())
                    h.update(path.read_bytes())
            return h.hexdigest()

        before = digest()
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        w.bootstrap()
        w.poll()
        w.backlog(limit=5)
        w.backlog_size()
        check("vrcache leaves VRChat files untouched", digest() == before)

        # A later external write must be picked up without the watcher leaving
        # anything of its own behind (journal/wal/temp files).
        paths_before = {p.name for p in low.rglob("*") if p.is_file()}
        _append_sqlite(db, _fake_ids(1, start=900))
        check("vrcache picks up external write", w.poll() == _fake_ids(1, start=900))
        paths_after = {p.name for p in low.rglob("*") if p.is_file()}
        check("vrcache creates no sidecar files",
              paths_after == paths_before | {"avatars.sqlite"},
              str(paths_after - paths_before))


def test_vrcache_locked_database() -> None:
    """A database held under an exclusive lock must degrade, not crash."""
    from vrcache import VRCacheWatcher

    with tempfile.TemporaryDirectory() as tmp:
        low, amp = _fixture(Path(tmp), ids=_fake_ids(3))
        holder = sqlite3.connect(low / "avatars.sqlite", isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            w = VRCacheWatcher(low_dir=low, amp_path=amp, busy_timeout=0.2)
            try:
                result = w.poll()
            except Exception as exc:
                result = f"raised {type(exc).__name__}: {exc}"
            check("vrcache locked db does not raise", result == [], str(result))
        finally:
            try:
                holder.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            holder.close()

        # Recovered once the lock is gone. A fresh watcher has no watermark, so
        # bootstrap first: without it the whole backlog is legitimately "new".
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        w.bootstrap()
        check("vrcache recovers after unlock", w.poll() == [])


def test_storage_security() -> None:
    """Stored thumbnail names are untrusted and must stay inside the cache."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        storage.DATA_DIR = root
        storage.THUMBS_DIR = root / "thumbs"
        storage.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        (storage.THUMBS_DIR / "good.png").write_bytes(b"png-bytes")

        ok = storage.resolve_thumb("good.png")
        check("resolve_thumb accepts a plain name", ok is not None and ok.exists())

        escapes = [
            "../../../Windows/win.ini",
            r"..\..\..\Windows\win.ini",
            "C:\\Windows\\win.ini",
            "/etc/passwd",
            "sub/dir/other.png",
            "..",
            "",
            None,
        ]
        for candidate in escapes:
            check(f"resolve_thumb rejects {candidate!r}", storage.resolve_thumb(candidate) is None,
                  str(storage.resolve_thumb(candidate)))


def test_storage_import_ignores_local_paths() -> None:
    """A shared export must not be able to point at a local file."""
    merged, added = storage.merge_favourites([], [{
        "id": "avtr_aaaa-bbbb",
        "name": "Shared",
        "thumb": "../../../../Windows/win.ini",
        "thumb_url": "https://example.invalid/t.png",
    }])
    check("import adds the entry", added == 1 and len(merged) == 1)
    check("import drops the local thumb path", merged[0].get("thumb") is None,
          str(merged[0].get("thumb")))
    check("import keeps the remote thumb url", merged[0].get("thumb_url") == "https://example.invalid/t.png")


def test_settings_sanitize() -> None:
    sanitize = storage.sanitize_settings

    defaults = sanitize({})
    check("sanitize empty -> defaults", defaults["osc_send_port"] == 9000
          and defaults["osc_receive_port"] == 9001)

    cases = [
        ({"osc_send_port": "abc"}, "osc_send_port", 9000, "non-numeric port"),
        ({"osc_send_port": None}, "osc_send_port", 9000, "null port"),
        ({"osc_send_port": 0}, "osc_send_port", 9000, "zero port"),
        ({"osc_send_port": 70000}, "osc_send_port", 9000, "out-of-range port"),
        ({"osc_receive_port": "not a number"}, "osc_receive_port", 9001, "bad recv port"),
        ({"osc_receive_port": -5}, "osc_receive_port", 9001, "negative recv port"),
        ({"osc_send_port": 9010}, "osc_send_port", 9010, "valid port preserved"),
        ({"osc_send_port": "9011"}, "osc_send_port", 9011, "numeric string port preserved"),
    ]
    for payload, key, expected, label in cases:
        got = sanitize(payload)[key]
        check(f"sanitize {label}", got == expected, str(got))

    check("sanitize non-dict", sanitize(None)["osc_send_port"] == 9000)
    check("sanitize list", sanitize([1, 2])["osc_send_port"] == 9000)

    junk = sanitize({"auth_token": 12345, "auth_username": None,
                     "osc_send_ip": "", "auth_expires": "soon", "unknown_key": "x"})
    check("sanitize coerces non-string token", junk["auth_token"] == "")
    check("sanitize coerces null username", junk["auth_username"] == "")
    check("sanitize blank host -> loopback", junk["osc_send_ip"] == "127.0.0.1")
    check("sanitize drops unknown keys", "unknown_key" not in junk)
    check("sanitize drops retired keys", "auth_expires" not in junk)

    good = sanitize({"osc_send_ip": "10.0.0.5"})
    check("sanitize keeps real host", good["osc_send_ip"] == "10.0.0.5")


def test_settings_load_recovers_from_corrupt_file() -> None:
    """A hand-edited or truncated settings.json must not break start-up."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        storage.DATA_DIR = root
        storage.SETTINGS_FILE = root / "settings.json"

        storage.SETTINGS_FILE.write_text("{ this is not json", encoding="utf-8")
        check("corrupt settings fall back to defaults",
              storage.load_settings()["osc_send_port"] == 9000)

        storage.SETTINGS_FILE.write_text(json.dumps({"osc_send_port": "oops"}), encoding="utf-8")
        check("invalid port in file repaired", storage.load_settings()["osc_send_port"] == 9000)

        storage.SETTINGS_FILE.write_text(json.dumps({"osc_send_port": 9123}), encoding="utf-8")
        check("valid settings still load", storage.load_settings()["osc_send_port"] == 9123)


def test_api_image_download_guards() -> None:
    from api import MAX_IMAGE_BYTES, VRCApi

    check("image cap is sane", 0 < MAX_IMAGE_BYTES <= 16 * 1024 * 1024)
    api = VRCApi()
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "out.png"
        # Rejected before any network access: non-http schemes.
        for bad in ("file:///C:/Windows/win.ini", "ftp://example.invalid/x.png", "", None):
            check(f"download_image rejects {bad!r}", api.download_image(bad, str(dest)) is False)
        check("rejected download wrote nothing", not dest.exists())


def _isolated_backend():
    """A Backend with no OSC socket, no threads and no real VRChat files."""
    from backend import Backend
    from vrcache import VRCacheWatcher

    root = Path(tempfile.mkdtemp())
    storage.DATA_DIR = root
    storage.FAVS_FILE = root / "favourites.json"
    storage.SETTINGS_FILE = root / "settings.json"
    storage.LOG_FILE = root / "avatar_log.json"
    storage.CHANGES_FILE = root / "avatar_changes.json"
    storage.THUMBS_DIR = root / "thumbs"
    storage.THUMBS_DIR.mkdir(parents=True, exist_ok=True)

    cache = VRCacheWatcher(low_dir=root / "low", amp_path=root / "amp.cache")
    cache.amp_file.parent.mkdir(parents=True, exist_ok=True)
    cache.amp_file.write_bytes(b"")
    return Backend(start_services=False, cache=cache)


def test_log_dedupe_across_sources() -> None:
    """OSC and the log file both report the same change; count it once."""
    from datetime import datetime, timedelta

    b = _isolated_backend()
    avatar_id = "avtr_11111111-2222-3333-4444-555555555555"

    check("first sighting recorded", b._record_log(avatar_id, source="osc") is True)
    check("count starts at 1", b.log[0]["count"] == 1, str(b.log))

    # The log file reports the same change with its own timestamp. Both formats
    # come from datetime.isoformat(), so the minute bucket lines up.
    first = datetime.fromisoformat(b.log[0]["last_seen"])
    same_minute = first.replace(second=30).isoformat(timespec="seconds")
    check("osc then log in same minute is a duplicate",
          b._record_log(avatar_id, when=same_minute, source="log") is False)
    check("count still 1", b.log[0]["count"] == 1, str(b.log))

    # A genuinely later sighting does count.
    later = (first + timedelta(minutes=5)).isoformat(timespec="seconds")
    check("later sighting counts", b._record_log(avatar_id, when=later, source="log") is True)
    check("count becomes 2", b.log[0]["count"] == 2, str(b.log))
    check("source updated", b.log[0]["source"] == "log", str(b.log[0]))

    # A different avatar is independent.
    other = "avtr_99999999-8888-7777-6666-555555555555"
    check("other avatar recorded", b._record_log(other, source="cache-db") is True)
    check("two log entries", len(b.log) == 2)


def test_log_size_limits() -> None:
    """The two caps are separate, and the UI's number is the real one.

    They are separate because the lists fill at completely different rates --
    one row per avatar seen against one row per player who changed avatar -- so
    a single shared cap would let the faster list silently eat the slower one's
    budget.
    """
    import storage

    b = _isolated_backend()

    check("avatar cap default", b._log_limit() == storage.DEFAULT_MAX_AVATAR_LOG,
          str(b._log_limit()))
    check("changes cap default",
          b._change_limit() == storage.DEFAULT_MAX_PLAYER_CHANGES, str(b._change_limit()))
    check("defaults match the old hard-coded caps",
          storage.DEFAULT_MAX_AVATAR_LOG == 800
          and storage.DEFAULT_MAX_PLAYER_CHANGES == 1000,
          f"{storage.DEFAULT_MAX_AVATAR_LOG} / {storage.DEFAULT_MAX_PLAYER_CHANGES}")

    b.settings["max_avatar_log"] = 3
    b.settings["max_player_changes"] = 2
    check("caps read from settings",
          b._log_limit() == 3 and b._change_limit() == 2,
          f"{b._log_limit()} / {b._change_limit()}")

    # A junk value must fall back rather than raise or go unbounded, because the
    # limits are read on every single recorded row.
    for bad in (0, -5, 10**9, "abc", None, [100]):
        b.settings["max_avatar_log"] = bad
        b.settings["max_player_changes"] = bad
        check(f"junk cap {bad!r} falls back",
              b._log_limit() == storage.DEFAULT_MAX_AVATAR_LOG
              and b._change_limit() == storage.DEFAULT_MAX_PLAYER_CHANGES,
              f"{b._log_limit()} / {b._change_limit()}")


def test_log_size_limits_enforced() -> None:
    """Recording must respect each cap independently, keeping the newest rows."""
    b = _isolated_backend()
    b.settings["max_avatar_log"] = 3
    b.settings["max_player_changes"] = 2

    for i in range(6):
        b._record_log(_avtr_id(i),
                      when=f"2026-01-0{i + 1}T00:00:00+00:00")
        b._record_change(f"Player{i}", f"Avatar{i}",
                         when=f"2026-01-0{i + 1}T00:00:00+00:00")

    check("avatar log capped at 3", len(b.log) == 3, str(len(b.log)))
    check("player changes capped at 2", len(b.changes) == 2, str(len(b.changes)))
    # Oldest first, so the survivors are the tail. Written as indices rather
    # than literals because the fixture id is hex-formatted, which makes a
    # hand-computed "expected" string wrong in a way that looks like a bug.
    check("the newest avatars are kept",
          [e["id"] for e in b.log] == [_avtr_id(i) for i in (3, 4, 5)],
          str([e["id"] for e in b.log]))
    check("the oldest avatars are dropped",
          all(_avtr_id(i) not in [e["id"] for e in b.log] for i in (0, 1, 2)))
    check("the newest changes are kept",
          [c["avatar"] for c in b.changes] == ["Avatar4", "Avatar5"],
          str([c["avatar"] for c in b.changes]))
    check("trim persisted", len(storage.load_log()) == 3, str(len(storage.load_log())))


def test_lowering_the_cap_trims_immediately() -> None:
    """Setting a smaller cap must shrink the list now, not at the next overflow."""
    b = _isolated_backend()
    for i in range(10):
        b._record_log(_avtr_id(i),
                      when=f"2026-01-{i + 1:02d}T00:00:00+00:00")
        b._record_change(f"Player{i}", f"Avatar{i}",
                         when=f"2026-01-{i + 1:02d}T00:00:00+00:00")
    check("ten recorded", len(b.log) == 10 and len(b.changes) == 10)

    res = b.save_settings(9000, 9001, max_avatar_log=4, max_player_changes=100)
    check("save accepted", res["ok"] is True, str(res))
    check("avatar log trimmed to the new cap", len(b.log) == 4, str(len(b.log)))
    check("player changes left alone", len(b.changes) == 10, str(len(b.changes)))
    check("the drop was reported", res.get("dropped_logs") == 6, str(res))
    check("status mentions the drop", "Dropped 6" in b.status, b.status)
    check("the newest four kept",
          [e["id"] for e in b.log] == [_avtr_id(i) for i in (6, 7, 8, 9)],
          str([e["id"] for e in b.log]))

    # Raising the cap must not resurrect anything.
    b.save_settings(9000, 9001, max_avatar_log=50)
    check("raising the cap does not restore rows", len(b.log) == 4, str(len(b.log)))

    # Trimming twice in a row is a no-op.
    b.save_settings(9000, 9001, max_avatar_log=2)
    again = b.save_settings(9000, 9001, max_avatar_log=2)
    check("second trim drops nothing", again.get("dropped_logs") == 0, str(again))


def test_log_size_caps_applied_at_startup() -> None:
    """A cap lowered while the app was closed must still take effect on launch."""
    b = _isolated_backend()
    stamp = "2026-01-01T00:00:00+00:00"
    storage.save_log([
        {"id": _avtr_id(i), "name": "",
         "first_seen": stamp, "last_seen": f"2026-01-{i + 1:02d}T00:00:00+00:00",
         "count": 1, "private": False, "source": "log"}
        for i in range(20)
    ])
    storage.save_changes([
        {"player": f"P{i}", "avatar": f"A{i}", "first_seen": stamp,
         "last_seen": f"2026-01-{i + 1:02d}T00:00:00+00:00", "count": 1}
        for i in range(20)
    ])
    # Lower the caps on disk, as if from a previous session's Settings.
    storage.save_settings({"max_avatar_log": 5, "max_player_changes": 3})

    from backend import Backend
    b2 = Backend(start_services=False, cache=b._cache)
    check("avatar log trimmed on start", len(b2.log) == 5, str(len(b2.log)))
    check("player changes trimmed on start", len(b2.changes) == 3, str(len(b2.changes)))
    check("newest avatars kept",
          b2.log[-1]["id"] == _avtr_id(19), b2.log[-1]["id"])
    check("trim persisted to disk", len(storage.load_log()) == 5, str(len(storage.load_log())))


def test_log_size_caps_reject_nonsense() -> None:
    """A bad number is refused outright rather than quietly clamped."""
    b = _isolated_backend()
    for value in (0, -1, 10001, 10**9):
        res = b.save_settings(9000, 9001, max_avatar_log=value)
        check(f"avatar cap {value} refused", res["ok"] is False, str(res))
        check(f"avatar cap {value} explains", "between" in res.get("message", ""), str(res))
        check(f"avatar cap {value} not stored",
              b._log_limit() == storage.DEFAULT_MAX_AVATAR_LOG, str(b._log_limit()))
    for value in ("abc", [3], 1.5):
        res = b.save_settings(9000, 9001, max_player_changes=value)
        check(f"changes cap {value!r} refused or normalised",
              res["ok"] is False or b._change_limit() == int(value), str(res))

    # Bounds are inclusive.
    check("1 accepted",
          b.save_settings(9000, 9001, max_avatar_log=1, max_player_changes=1)["ok"])
    check("10000 accepted",
          b.save_settings(9000, 9001, max_avatar_log=10000, max_player_changes=10000)["ok"])

    # Both caps reported to the UI.
    reported = b.get_settings()
    check("caps reported by get_settings",
          reported["max_avatar_log"] == 10000 and reported["max_player_changes"] == 10000,
          str({k: reported[k] for k in ("max_avatar_log", "max_player_changes")}))


def test_log_size_caps_survive_a_hand_edited_file() -> None:
    """A hand-edited settings.json must not break the log on the next row."""
    import storage

    check("string number accepted",
          storage.sanitize_settings({"max_avatar_log": "250"})["max_avatar_log"] == 250)
    check("padded string accepted",
          storage.sanitize_settings({"max_avatar_log": " 250 "})["max_avatar_log"] == 250)
    check("float string accepted",
          storage.sanitize_settings({"max_player_changes": "120"})
          ["max_player_changes"] == 120)
    # A decimal string is rejected rather than truncated, because picking 120
    # out of "120.5" is a guess. The UI only ever sends integers, so this only
    # affects a hand-edited file, where the default is the safe answer.
    check("decimal string falls back rather than truncating",
          storage.sanitize_settings({"max_player_changes": "120.0"})
          ["max_player_changes"] == storage.DEFAULT_MAX_PLAYER_CHANGES)
    unusable: list[dict] = [
        {"max_avatar_log": 0}, {"max_avatar_log": -3},
        {"max_avatar_log": 99999}, {"max_avatar_log": "nope"},
        {"max_avatar_log": None}, {"max_avatar_log": True},
        {"max_player_changes": []}, {"max_player_changes": {}},
    ]
    for bad in unusable:
        clean = storage.sanitize_settings(bad)
        key = next(iter(bad))
        check(f"{bad} falls back",
              clean[key] == (storage.DEFAULT_MAX_AVATAR_LOG if "avatar" in key
                             else storage.DEFAULT_MAX_PLAYER_CHANGES), str(clean[key]))


def test_log_dedupe_persisted() -> None:
    """A saved log entry without a bucket must not be replayed forever."""
    b = _isolated_backend()
    avatar_id = "avtr_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    storage.save_log([{
        "id": avatar_id, "name": "", "first_seen": "2026-01-01 00:00:00+00:00",
        "last_seen": "2026-01-01 00:00:00+00:00", "count": 4, "private": False,
    }])
    b.log = storage.load_log()
    check("legacy entry has no bucket", "seen_bucket" not in b.log[0])
    check("legacy entry is counted again", b._record_log(avatar_id) is True)
    check("legacy count incremented", b.log[0]["count"] == 5, str(b.log[0]))


DEFAULT_ROBOT = "avtr_c38a1615-5bf5-42b4-84eb-a8b6c37cbd11"
DEFAULT_KURO = "avtr_faf40a9f-ce39-4ff2-a069-223797ba11af"
ORDINARY_ID = "avtr_32611e23-2508-4cbf-accb-b8625e37f546"


def _avtr_id(index: int) -> str:
    """A distinct well-formed avatar id for fixtures.

    Hex-formatted, so the digits run 0-9 then a-f: writing an expected id as a
    decimal literal in a test just gets the wrong answer.
    """
    return f"avtr_{index:08x}-1111-2222-3333-444444444444"


def test_defaults_are_never_recorded() -> None:
    """A built-in default must not become a log entry at all.

    Not "recorded but hidden": dropped before anything is written, so it never
    reaches avatar_log.json, never consumes one of the 800 capped slots, and
    never appears to be a finding the user could save.
    """
    b = _isolated_backend()

    check("default avatar id rejected", b._record_log(DEFAULT_ROBOT, source="cache-db") is False)
    check("default avatar id rejected (community-authored)",
          b._record_log(DEFAULT_KURO, source="log") is False)
    check("nothing written to the log", b.log == [], str(b.log))
    check("nothing persisted", storage.load_log() == [], str(storage.load_log()))

    check("ordinary avatar still recorded",
          b._record_log(ORDINARY_ID, source="cache-db") is True)
    check("exactly one entry", len(b.log) == 1, str(b.log))

    # An existing entry for a default must not have its count inflated either.
    # The early return sits above the de-duplication scan specifically so that a
    # default worn repeatedly cannot grow a row that should not exist.
    b._record_log(DEFAULT_ROBOT, when="2026-01-01 00:00:00+00:00")
    b._record_log(DEFAULT_ROBOT, when="2026-06-01 12:00:00+00:00")
    check("no default row appeared", len(b.log) == 1, str(b.log))
    check("ordinary row untouched", b.log[0]["id"] == ORDINARY_ID and b.log[0]["count"] == 1,
          str(b.log[0]))


def test_defaults_are_not_recorded_as_player_changes() -> None:
    """The player-changes tab only has a name, so it matches on the name."""
    b = _isolated_backend()

    check("default name rejected", b._record_change("SomePlayer", "Robot") is False)
    check("default name rejected, cased differently",
          b._record_change("SomePlayer", "robot") is False)
    check("no change rows", b.changes == [], str(b.changes))

    # Regression guard for the substring-matching failure mode. "Fallback" is a
    # real uploaded avatar that players select as their Quest fallback; treating
    # it as a default because it looks default-ish would silently hide it.
    check("Fallback is kept", b._record_change("Mrblok 75a0", "Fallback") is True)
    check("a longer name is kept", b._record_change("Someone", "Robot Deluxe") is True)
    check("two change rows kept", len(b.changes) == 2, str(b.changes))


def test_defaults_are_pruned_on_start() -> None:
    """The filter only stops new rows; a log written before it must be cleaned.

    Without this an install that has run for a while keeps every default it
    already collected, with no way to tell them from real findings short of
    deleting the entire log.
    """
    stamp = "2026-10-05T01:07:34+11:00"

    # Seed into the isolated directory this backend owns. _isolated_backend()
    # repoints the storage module at a fresh temp dir on every call, so writing
    # before constructing it would land in the *previous* test's directory.
    from backend import Backend

    b = _isolated_backend()
    storage.save_log([
        {"id": DEFAULT_ROBOT, "name": "Robot", "first_seen": stamp, "last_seen": stamp,
         "count": 3, "private": False, "source": "log"},
        {"id": ORDINARY_ID, "name": "Keep Me", "first_seen": stamp, "last_seen": stamp,
         "count": 1, "private": False, "source": "cache-db"},
        {"id": DEFAULT_KURO, "name": "", "first_seen": stamp, "last_seen": stamp,
         "count": 1, "private": False, "source": "log"},
    ])
    storage.save_changes([
        {"player": "bray201333 eb31", "avatar": "Robot", "first_seen": stamp,
         "last_seen": stamp, "count": 4},
        {"player": "CoHayOh", "avatar": "256PolyKikyo", "first_seen": stamp,
         "last_seen": stamp, "count": 30},
    ])

    # A second Backend over the same on-disk state: exactly what a relaunch
    # does, and the only way to observe the prune that __init__ performs.
    b2 = Backend(start_services=False, cache=b._cache)
    check("defaults removed from the log", len(b2.log) == 1, str(b2.log))
    check("the ordinary avatar survived", b2.log[0]["id"] == ORDINARY_ID, str(b2.log[0]))
    check("default change rows removed", len(b2.changes) == 1, str(b2.changes))
    check("the ordinary change survived",
          b2.changes[0]["avatar"] == "256PolyKikyo", str(b2.changes[0]))

    # Pruned on disk too, not just in memory, or the next launch re-prunes and
    # reports it again.
    check("prune persisted", len(storage.load_log()) == 1, str(storage.load_log()))
    check("change prune persisted", len(storage.load_changes()) == 1,
          str(storage.load_changes()))

    # Idempotent: a second pass must not remove anything or bump revs.
    logs_rev, changes_rev = b2._revs["logs"], b2._revs["changes"]
    removed = b2.prune_defaults()
    check("second prune is a no-op", removed == (0, 0), str(removed))
    check("second prune does not touch revs",
          (b2._revs["logs"], b2._revs["changes"]) == (logs_rev, changes_rev))


def test_save_all_skips_defaults() -> None:
    """Defence in depth: a default must not be promoted even if one is present.

    _record_log cannot create a default row and the start-up prune removes any
    that exist, so this path is only reachable by hand-editing avatar_log.json.
    It is still worth pinning, because save_all_logs is the one action that
    writes to the favourites list without asking per row.
    """
    b = _isolated_backend()
    stamp = "2026-10-05T01:07:34+11:00"
    # Assign directly, bypassing the filter, to simulate a log written by an
    # older version of the app.
    b.log = [
        {"id": DEFAULT_ROBOT, "name": "Robot", "first_seen": stamp, "last_seen": stamp,
         "count": 1, "private": False, "source": "log"},
        {"id": ORDINARY_ID, "name": "", "first_seen": stamp, "last_seen": stamp,
         "count": 1, "private": False, "source": "log"},
    ]
    result = b.save_all_logs()
    check("only the ordinary avatar saved", result.get("added") == 1, str(result))
    check("favourites hold the ordinary id",
          [e["id"] for e in b.entries] == [ORDINARY_ID], str(b.entries))


def test_expired_token_is_not_private() -> None:
    """A 401 must never be recorded as 'this avatar is private'."""
    from api import AuthError, VRCApi

    b = _isolated_backend()
    avatar_id = "avtr_12341234-1234-1234-1234-123412341234"
    b._record_log(avatar_id, source="cache-db")

    # get_avatar raises AuthError on 401 rather than returning None.
    api = VRCApi("token")
    status = {"code": 401}
    def fake_request(method, path, data=None, basic=None, timeout=20):
        return status["code"], b'{"error":{"message":"Missing Credentials"}}'
    api._request = fake_request
    b.api = api

    b._metadata_worker(avatar_id)
    check("session flagged expired", b.session_expired is True)
    check("status asks for re-login", "log in again" in b.status.lower(), b.status)
    check("avatar NOT marked private", b.log[0].get("private") is False, str(b.log[0]))

    # And a genuine 404 is still treated as private.
    b.session_expired = False
    b.status = ""
    status["code"] = 404
    b.api._request = fake_request
    b._metadata_worker(avatar_id)
    check("404 does not expire the session", b.session_expired is False)
    check("404 marks the avatar private", b.log[0].get("private") is True, str(b.log[0]))

    # Re-authenticating clears the flag.
    b._handle_session_expired(AuthError("nope"))
    check("expiry recorded", b.session_expired is True)


def test_osc_settings_not_persisted_on_failure() -> None:
    """A failed bind must leave settings.json untouched."""
    b = _isolated_backend()

    # Occupy the receive port so the candidate bridge cannot bind it.
    import socket
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", 0))
    taken = blocker.getsockname()[1]
    try:
        storage.save_settings({"osc_send_port": 9000, "osc_receive_port": 9001})
        res = b.save_settings(9100, taken)
        check("failed bind reports failure", res.get("ok") is False, str(res))

        reloaded = storage.load_settings()
        check("failed bind did not persist send port", reloaded["osc_send_port"] == 9000,
              str(reloaded["osc_send_port"]))
        check("failed bind did not persist recv port", reloaded["osc_receive_port"] == 9001,
              str(reloaded["osc_receive_port"]))
    finally:
        blocker.close()

    check("identical ports rejected", b.save_settings(9000, 9000).get("ok") is False)
    check("non-numeric rejected", b.save_settings("x", 9001).get("ok") is False)
    check("out of range rejected", b.save_settings(0, 9001).get("ok") is False)


def test_discovery_state_reported() -> None:
    b = _isolated_backend()
    state = b.discovery_state()
    check("discovery reports sources", "cache-db" in state["sources"] and "log" in state["sources"],
          str(state.get("sources")))
    check("discovery reports backlog count", isinstance(state["backlog"], int))
    check("discovery reports db path", state["db_path"].endswith("avatars.sqlite"), state["db_path"])

    live = b.get_state()
    check("get_state includes discovery", "discovery" in live)
    check("get_state includes session_expired", live.get("session_expired") is False)

    settings = b.get_settings()
    check("get_settings includes discovery", "discovery" in settings)


def test_auth_token_encryption() -> None:
    """The VRChat token must never be written to disk in the clear."""
    if not storage.dpapi_available():
        return

    encrypted = storage.protect_secret("tok-secret-value")
    check("dpapi produces ciphertext", bool(encrypted))
    check("ciphertext hides the token", "tok-secret-value" not in encrypted)
    check("dpapi round-trips", storage.unprotect_secret(encrypted) == "tok-secret-value")
    check("decrypting garbage yields empty", storage.unprotect_secret("not-base64!!") == "")
    check("decrypting empty yields empty", storage.unprotect_secret("") == "")
    check("encrypting empty yields empty", storage.protect_secret("") == "")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        storage.DATA_DIR = root
        storage.SETTINGS_FILE = root / "settings.json"
        storage.save_settings({"auth_token": "tok-on-disk", "auth_username": "me"})
        raw = storage.SETTINGS_FILE.read_text(encoding="utf-8")
        check("token absent from settings.json", "tok-on-disk" not in raw, raw)
        check("encrypted field present", storage.AUTH_TOKEN_ENC_KEY in raw)
        check("token loads back", storage.load_settings()["auth_token"] == "tok-on-disk")
        check("username loads back", storage.load_settings()["auth_username"] == "me")

        # A plaintext token from an older version is still read.
        storage.SETTINGS_FILE.write_text(
            json.dumps({"auth_token": "legacy-plain"}), encoding="utf-8")
        check("legacy plaintext token loads",
              storage.load_settings()["auth_token"] == "legacy-plain")

        # ...and is upgraded to encrypted on the next write.
        loaded = storage.load_settings()
        storage.save_settings(loaded)
        check("legacy token upgraded",
              "legacy-plain" not in storage.SETTINGS_FILE.read_text(encoding="utf-8"))
        check("upgraded token still loads",
              storage.load_settings()["auth_token"] == "legacy-plain")


def test_wear_confirms_or_reports_failure() -> None:
    """VRChat never acknowledges /avatar/change, so we infer the outcome."""
    import backend

    b = _isolated_backend()
    avatar_id = "avtr_4d4d4d4d-1111-2222-3333-444444444444"
    other = "avtr_5e5e5e5e-1111-2222-3333-444444444444"
    # Give it a name so the confirmation message can use it.
    b.add_by_id(avatar_id)
    entry = b._entry(avatar_id)
    entry["name"] = "Test Avatar"

    # No OSC socket in the isolated backend: sending fails and nothing is pending.
    res = b.wear(avatar_id)
    check("wear reports the OSC problem", res["ok"] is False, str(res))
    check("failed send leaves nothing pending", b._pending_wear is None)

    # Simulate a successful send so the confirmation path can be exercised.
    b.osc.listening = True
    b.osc.error = None
    sent = []

    def fake_change(avatar_id):
        sent.append(avatar_id)
        return True

    b.osc.change_avatar = fake_change

    res = b.wear(avatar_id)
    check("wear accepted", res["ok"] is True and sent == [avatar_id], str(res))
    check("wear is pending confirmation", b._pending_wear is not None)

    # Not yet due: nothing should be reported.
    b._expire_pending_wear()
    check("pending wear is not expired early", b._pending_wear is not None)
    check("no failure status yet", "did not switch" not in b.status.lower(), b.status)

    # VRChat echoes the id back -> confirmed, no failure message.
    b.osc.current_avatar_id = avatar_id
    b._on_avatar_change(avatar_id)
    check("confirmation clears the pending wear", b._pending_wear is None)
    check("confirmation is reported", "now wearing" in b.status.lower(), b.status)
    check("confirmation uses the name when known", "Test Avatar" in b.status, b.status)

    # Time passes with no echo -> VRChat refused it.
    b.status = ""
    b.osc.current_avatar_id = other
    b.wear(avatar_id)
    b._pending_wear = (avatar_id, time.monotonic() - (backend.WEAR_CONFIRM_TIMEOUT + 1))
    b._expire_pending_wear()
    check("stale wear is cleared", b._pending_wear is None)
    check("refusal is reported", "did not switch" in b.status.lower(), b.status)
    check("refusal explains why", "private" in b.status.lower(), b.status)

    # An unrelated avatar change must not confirm or expire the request.
    b.status = ""
    b.wear(avatar_id)
    b._pending_wear = (avatar_id, time.monotonic() - (backend.WEAR_CONFIRM_TIMEOUT + 1))
    b._on_avatar_change(other)
    check("wrong avatar does not confirm", b._pending_wear is not None)
    b._expire_pending_wear()
    check("wrong avatar still expires as a refusal",
          "did not switch" in b.status.lower(), b.status)


def test_import_vrchat_favourites() -> None:
    from api import VRCApi

    b = _isolated_backend()

    class FakeApi(VRCApi):
        def __init__(self):
            super().__init__("token")

        def is_logged_in(self):
            return True

        def list_favorite_avatars(self, limit=100, offset=0):
            return [
                {"id": "avtr_aaaaaaaa-1111-2222-3333-444444444444", "name": "Alpha",
                 "authorName": "Ann", "releaseStatus": "public",
                 "thumbnailImageUrl": "https://example.invalid/a.png",
                 "unityPackages": [{"platform": "android"}, {"platform": "standalonewindows"}]},
                {"id": "avtr_bbbbbbbb-1111-2222-3333-444444444444", "name": "Beta"},
            ]

    b.api = FakeApi()
    res = b.import_vrchat_favourites()
    check("import reports both", res["ok"] is True and res["added"] == 2, str(res))
    by_id = {e["id"]: e for e in b.entries}
    check("import kept the name", by_id["avtr_aaaaaaaa-1111-2222-3333-444444444444"]["name"] == "Alpha")
    check("import kept the author", by_id["avtr_aaaaaaaa-1111-2222-3333-444444444444"]["author"] == "Ann")
    check("import derived platforms",
          by_id["avtr_aaaaaaaa-1111-2222-3333-444444444444"]["platforms"] == ["Quest", "PC"])

    # Second run must not duplicate.
    res = b.import_vrchat_favourites()
    check("re-import adds nothing", res["added"] == 0 and len(b.entries) == 2, str(res))


def test_import_vrchat_requires_login() -> None:
    b = _isolated_backend()
    res = b.import_vrchat_favourites()
    check("import requires login", res["ok"] is False and res["title"] == "Not logged in")


def test_login_with_two_factor() -> None:
    """The whole 2FA round trip, which is how most accounts actually log in.

    Regression: `last_error` and `api` were first assigned inside the
    username/password branch, but the shared error check below reads them. The
    second call -- the one that submits the code -- never goes through that
    branch, so a successful verification reached `if last_error is not None`
    with the name unbound and raised UnboundLocalError. Every account with 2FA
    enabled was unable to log in, and nothing tested it.

    VRCApi is replaced outright so no network call can happen here; the point
    is the branch structure of Backend.login, not VRChat's API.
    """
    from api import TwoFactorRequired

    b = _isolated_backend()

    class FakeApi:
        def __init__(self) -> None:
            self.token = ""
            self.verified = ""
            self.method = ""

        def login(self, username: str, password: str) -> dict:
            raise TwoFactorRequired(["totp", "emailotp"])

        def verify_2fa(self, code: str, method: str = "totp") -> dict:
            self.verified = code
            self.method = method
            self.token = "authCookie_2fa"
            return {"verified": True}

    fake = FakeApi()

    # setattr rather than `backend.VRCApi = ...`: assigning to an imported
    # class name reads as assigning to a type, and mypy is right to object.
    import backend as backend_module
    original = backend_module.VRCApi
    setattr(backend_module, "VRCApi", lambda *a, **k: fake)
    try:
        # Step 1: credentials are accepted but VRChat demands a second factor.
        first = b.login("someone", "hunter2")
        check("first step asks for 2FA", first["status"] == "2fa", str(first))
        check("methods reported", first.get("methods") == ["totp", "emailotp"], str(first))
        check("a method is pre-selected", first.get("method") == "totp", str(first))
        check("pending client stored", b._pending_api is fake)

        # Step 2: the code is submitted. This is the call that used to raise.
        second = b.login("someone", "hunter2", "123456", "totp")
        check("second step logs in", second["status"] == "ok", str(second))
        check("code submitted", fake.verified == "123456", fake.verified)
        check("method submitted", fake.method == "totp", fake.method)
        check("token adopted", b.api is fake and fake.token == "authCookie_2fa")
        check("token persisted", b.settings.get("auth_token") == "authCookie_2fa",
              str(b.settings.get("auth_token")))
        check("username persisted", b.settings.get("auth_username") == "someone")
        check("pending state cleared",
              b._pending_api is None and b._pending_2fa_methods == []
              and b._pending_2fa_method == "",
              f"{b._pending_api} {b._pending_2fa_methods} {b._pending_2fa_method}")
    finally:
        setattr(backend_module, "VRCApi", original)


def test_login_reports_bad_credentials() -> None:
    """The non-2FA failure path still works, and leaves no pending client."""
    from api import AuthError

    b = _isolated_backend()

    class FailingApi:
        token = ""

        def login(self, username: str, password: str) -> dict:
            raise AuthError("Username or password is incorrect")

    import backend as backend_module
    original = backend_module.VRCApi
    setattr(backend_module, "VRCApi", lambda *a, **k: FailingApi())
    try:
        res = b.login("someone", "wrong")
        check("bad credentials reported", res["status"] == "error", str(res))
        check("message surfaced", "incorrect" in res["message"], res["message"])
        check("no pending client left", b._pending_api is None)
        check("still not logged in", not b.api.is_logged_in())
    finally:
        setattr(backend_module, "VRCApi", original)


def test_login_rejects_a_wrong_2fa_code() -> None:
    """A rejected code reports cleanly instead of stranding the pending client."""
    from api import ApiError

    b = _isolated_backend()

    class PendingApi:
        token = ""

        def verify_2fa(self, code: str, method: str = "totp") -> dict:
            raise ApiError("Invalid two-factor code")

    b._pending_api = PendingApi()
    b._pending_2fa_methods = ["totp"]
    b._pending_2fa_method = "totp"

    res = b.login("someone", "hunter2", "000000", "totp")
    check("wrong code reported", res["status"] == "error", str(res))
    check("message surfaced", "Invalid" in res["message"], res["message"])
    check("pending client cleared", b._pending_api is None)
    check("still not logged in", not b.api.is_logged_in())


def test_login_requires_credentials() -> None:
    b = _isolated_backend()
    check("empty form rejected",
          b.login("", "")["status"] == "error", str(b.login("", "")))
    check("no credentials stored", not b.api.is_logged_in())


def test_entry_index_stays_consistent() -> None:
    """The id index must never go stale, or lookups silently miss entries."""
    b = _isolated_backend()
    ids = [f"avtr_{i:08x}-1111-2222-3333-444444444444" for i in range(6)]

    for avatar_id in ids[:4]:
        b.add_by_id(avatar_id)
    for position, avatar_id in enumerate(ids[:4]):
        entry = b._entry(avatar_id)
        check(f"index finds entry {position}", entry is not None)
        check("entry identity is stable", entry is not None
              and b.entries[b.entries.index(entry)] is entry)

    # Duplicates are still rejected via the index.
    check("duplicate rejected", b.add_by_id(ids[0])["ok"] is False)

    # Deletion must drop the key, not leave a dangling reference.
    b.delete(ids[1])
    check("deleted entry is gone from the index", b._entry(ids[1]) is None)
    check("deleted entry is gone from the list",
          all(e["id"] != ids[1] for e in b.entries))
    check("index size matches list", len(b._index) == len(b.entries),
          f"{len(b._index)} vs {len(b.entries)}")

    # Mutating through the found entry keeps the index valid.
    entry = b._entry(ids[2])
    entry["name"] = "Renamed"
    check("in-place mutation is visible", b._entry(ids[2])["name"] == "Renamed")

    # An import replaces the list wholesale and must be reindexed.
    merged, _added = storage.merge_favourites(
        b.entries, [{"id": ids[4], "name": "Imported"}])
    b.entries = merged
    b._reindex()
    check("reindex picks up replacements", b._entry(ids[4]) is not None)
    check("index still matches after import", len(b._index) == len(b.entries))

    # Case-insensitive lookup.
    check("lookup is case-insensitive", b._entry(ids[4].upper()) is not None)


def test_save_all_logs_is_not_quadratic() -> None:
    """save_all_logs used a linear scan per log entry; the index fixes that."""
    b = _isolated_backend()
    log_ids = [f"avtr_{i:08x}-5555-6666-7777-888888888888" for i in range(200)]
    for avatar_id in log_ids:
        b.log.append({
            "id": avatar_id, "name": "", "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00", "seen_bucket": "2026-01-01T00:00",
            "count": 1, "private": False, "source": "cache-db",
        })

    start = time.perf_counter()
    res = b.save_all_logs()
    elapsed = time.perf_counter() - start

    check("bulk save reported all", res["added"] == 200, str(res["added"]))
    check("bulk save created the entries", len(b.entries) == 200)
    check("bulk save index matches", len(b._index) == 200)
    # Linear would be 200*200/2 = 20k scans; a dict lookup keeps this trivial.
    check("bulk save is fast", elapsed < 2.0, f"{elapsed:.3f}s")


def test_api_rate_limit_backoff() -> None:
    """429 must trigger a growing penalty rather than hammering the API."""
    from api import VRCApi

    api = VRCApi("tok", min_interval=0.01)
    check("starts with no penalty", api._backoff == 0.0)

    api._note_rate_limit()
    first = api._backoff
    check("first 429 sets a penalty", first > 0, str(first))

    api._note_rate_limit()
    check("penalty grows", api._backoff > first, f"{first} -> {api._backoff}")

    for _ in range(20):
        api._note_rate_limit()
    check("penalty is capped", api._backoff <= api.max_backoff, str(api._backoff))

    api._clear_rate_limit()
    check("cleared after success", api._backoff == 0.0)

    # Throttling must actually delay a burst.
    slow = VRCApi("tok", min_interval=0.05)
    start = time.perf_counter()
    for _ in range(4):
        slow._throttle()
    elapsed = time.perf_counter() - start
    # 4 calls at 50ms apart spans 150ms.
    check("throttle spaces requests", elapsed >= 0.14, f"{elapsed:.3f}s")


def test_thumbnail_pruning() -> None:
    """Cached images must not accumulate for ever."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        storage.THUMBS_DIR = root / "thumbs"
        storage.THUMBS_DIR.mkdir(parents=True, exist_ok=True)

        keep = "keep.png"
        (storage.THUMBS_DIR / keep).write_bytes(b"x")
        (storage.THUMBS_DIR / "orphan.png").write_bytes(b"x")
        (storage.THUMBS_DIR / "nested").mkdir()

        # Age the orphan past the undo grace window so it is collectable.
        aged = 1_000_000_000
        os.utime(storage.THUMBS_DIR / "orphan.png", (aged, aged))
        os.utime(storage.THUMBS_DIR / keep, (aged, aged))

        orphans, trimmed = storage.prune_thumbs({keep})
        check("orphan removed", orphans == 1, str(orphans))
        check("kept file survives", (storage.THUMBS_DIR / keep).exists())
        check("orphan is gone", not (storage.THUMBS_DIR / "orphan.png").exists())
        check("directories are left alone", (storage.THUMBS_DIR / "nested").is_dir())

        # Ageing referenced files so mtime ordering is deterministic. These are all in
# `keep`, so they survive the orphan pass and are only removed by the cap.
        referenced = {keep} | {f"old{i}.png" for i in range(10)}
        for index in range(10):
            path = storage.THUMBS_DIR / f"old{index}.png"
            path.write_bytes(b"x")
            os.utime(path, (1_000_000 + index, 1_000_000 + index))

        orphans, trimmed = storage.prune_thumbs(referenced, max_files=5)
        check("referenced files are not orphans", orphans == 0, f"orphans={orphans}")
        check("over-cap files trimmed", trimmed == 6, f"trimmed={trimmed}")
        check("newest survivors kept", (storage.THUMBS_DIR / "old9.png").exists())
        check("oldest trimmed away", not (storage.THUMBS_DIR / "old0.png").exists())
        check("explicit keep survives trimming", (storage.THUMBS_DIR / keep).exists())
        check("five files remain",
              len([p for p in storage.THUMBS_DIR.iterdir() if p.is_file()]) == 5)

        # delete_thumb refuses traversal and removes a real file.
        check("delete_thumb rejects traversal", storage.delete_thumb("../escape.png") is False)
        check("delete_thumb rejects absolute paths",
              storage.delete_thumb(r"C:\Windows\win.ini") is False)
        check("delete_thumb removes a real file", storage.delete_thumb(keep) is True)
        check("file is gone", not (storage.THUMBS_DIR / keep).exists())
        check("delete_thumb on a missing file", storage.delete_thumb(keep) is False)

        remaining = storage.clear_thumbs()
        check("clear_thumbs removes the rest", remaining == 4, str(remaining))
        check("no thumbnail files remain",
              not [p for p in storage.THUMBS_DIR.iterdir() if p.is_file()])
        check("clear_thumbs on empty dir", storage.clear_thumbs() == 0)


def test_delete_leaves_thumbnail_for_undo() -> None:
    """Deleting keeps the cached image so Undo can restore it.

    Replaced by test_undo_keeps_the_thumbnail, which covers the whole round
    trip including actually serving the image again.
    """
    b = _isolated_backend()
    avatar_id = "avtr_7a7a7a7a-1111-2222-3333-444444444444"
    b.add_by_id(avatar_id)
    entry = b._entry(avatar_id)
    entry["thumb"] = "orphan-me.png"
    storage.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    image = storage.THUMBS_DIR / "orphan-me.png"
    image.write_bytes(b"x")

    check("delete succeeds", b.delete(avatar_id)["ok"] is True)
    check("entry removed", b._entry(avatar_id) is None)
    check("image kept for undo", image.exists())
    check("image is collectable once past the grace window",
          storage.prune_thumbs(set(), grace_seconds=0.0)[0] == 1)
    check("image gone after pruning", not image.exists())


def test_job_progress() -> None:
    """Long bulk work must be visible and cancellable."""
    from jobs import JobRegistry, JobRunner

    registry = JobRegistry()
    runner = JobRunner(registry)

    release = threading.Event()
    processed: list[str] = []

    def worker(avatar_id):
        processed.append(avatar_id)
        release.wait(timeout=2.0)
        return True

    ids = _fake_ids(4)
    job_id = runner.start("metadata", ids, worker)
    check("job id returned", bool(job_id), job_id)

    job = registry.get(job_id)
    check("job is visible", job is not None)
    check("job knows its total", job["total"] == 4, str(job))
    check("job exposes its id", job["id"] == job_id)
    check("job starts unfinished", job["finished"] is False)

    # Progress appears as work completes.
    deadline = time.time() + 5
    while time.time() < deadline:
        job = registry.get(job_id)
        if job and job["done"] >= 1:
            break
        time.sleep(0.02)
    check("progress advances", job["done"] >= 1, str(job))
    check("percent is derived from done", job["percent"] > 0, str(job))

    check("cancel is accepted", registry.cancel(job_id) is True)
    check("cancel marks the job", registry.get(job_id)["cancelled"] is True)
    check("cancelling twice is refused", registry.cancel(job_id) is True)

    release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        job = registry.get(job_id)
        if job and job["finished"]:
            break
        time.sleep(0.02)
    job = registry.get(job_id)
    check("job finishes", job["finished"] is True, str(job))
    check("cancel is reported in the summary", "cancel" in job["message"].lower(), job["message"])
    check("cancelling a finished job is refused", registry.cancel(job_id) is False)
    check("no active job once finished", registry.active() is None)


def test_job_counts_failures() -> None:
    from jobs import JobRegistry, JobRunner

    registry = JobRegistry()
    runner = JobRunner(registry)

    def worker(avatar_id):
        if avatar_id.endswith(("1", "3")):
            return False
        if avatar_id.endswith("2"):
            raise RuntimeError("boom")
        return True

    job_id = runner.start("metadata", _fake_ids(4), worker)
    deadline = time.time() + 5
    while time.time() < deadline:
        job = registry.get(job_id)
        if job and job["finished"]:
            break
        time.sleep(0.02)

    job = registry.get(job_id)
    check("all work attempted", job["done"] == 4, str(job))
    check("successes counted", job["ok"] == 1, str(job))
    check("failures counted", job["failed"] == 3, str(job))
    check("summary mentions failures", "failed" in job["message"], job["message"])
    check("finished job reaches 100%", job["percent"] == 100.0, str(job))


def test_bulk_actions() -> None:
    b = _isolated_backend()
    ids = [f"avtr_{i:08x}-aaaa-bbbb-cccc-dddddddddddd" for i in range(4)]
    for avatar_id in ids:
        b.add_by_id(avatar_id)
    b._entry(ids[0])["tags"] = ["keep"]

    res = b.bulk_action(ids, "favorite")
    check("bulk favorite ok", res["ok"] and res["changed"] == 4, str(res))
    check("all favorited", all(b._entry(i)["favorite"] for i in ids))

    res = b.bulk_action(ids, "unfavorite")
    check("bulk unfavorite ok", res["changed"] == 4, str(res))
    check("none favorited", not any(b._entry(i)["favorite"] for i in ids))

    res = b.bulk_action(ids, "tag", "quest")
    check("bulk tag ok", res["changed"] == 4, str(res))
    check("tag applied", all("quest" in b._entry(i)["tags"] for i in ids))
    check("existing tags kept", "keep" in b._entry(ids[0])["tags"])

    res = b.bulk_action(ids, "untag", "quest")
    check("bulk untag ok", res["changed"] == 4, str(res))
    check("tag removed", not any("quest" in b._entry(i)["tags"] for i in ids))
    check("other tags survive", b._entry(ids[0])["tags"] == ["keep"])

    check("tag requires a value", b.bulk_action(ids, "tag", "")["ok"] is False)
    check("unknown action rejected", b.bulk_action(ids, "frobnicate")["ok"] is False)
    check("empty selection rejected", b.bulk_action([], "favorite")["ok"] is False)
    check("unknown ids rejected",
          b.bulk_action(["avtr_00000000-0000-0000-0000-000000000000"], "favorite")["ok"] is False)

    res = b.bulk_action(ids, "delete")
    check("bulk delete ok", res["changed"] == 4, str(res))
    check("entries removed", len(b.entries) == 0)
    check("index cleared", len(b._index) == 0)


def test_restore_entry_for_undo() -> None:
    b = _isolated_backend()
    avatar_id = "avtr_12341234-1234-1234-1234-123412341234"
    b.add_by_id(avatar_id)
    entry = b._entry(avatar_id)
    entry["name"] = "Keeper"
    entry["notes"] = "important note"
    entry["tags"] = ["a", "b"]
    entry["platforms"] = ["Quest"]
    entry["favorite"] = True

    b.delete(avatar_id)
    check("entry deleted", b._entry(avatar_id) is None)

    res = b.restore_entry(entry)
    check("restore ok", res["ok"] is True, str(res))
    back = b._entry(avatar_id)
    check("entry is back", back is not None)
    check("name restored", back["name"] == "Keeper")
    check("notes restored", back["notes"] == "important note")
    check("tags restored", back["tags"] == ["a", "b"])
    check("platforms restored", back["platforms"] == ["Quest"])
    check("favorite restored", back["favorite"] is True)
    check("index rebuilt", len(b._index) == 1)

    check("restoring twice is refused", b.restore_entry(entry)["ok"] is False)
    check("restoring junk is refused", b.restore_entry({"nope": 1})["ok"] is False)
    check("restoring a non-dict is refused", b.restore_entry("nope")["ok"] is False)


def test_start_metadata_job_requires_login() -> None:
    b = _isolated_backend()
    res = b.start_metadata_job(["avtr_00000000-1111-2222-3333-444444444444"])
    check("metadata job needs login", res["ok"] is False and res["title"] == "Not logged in")


def test_tray_icon() -> None:
    """The tray icon is native, so exercise it for real when possible."""
    from tray import TrayIcon, available

    if not available():
        return

    repo_icon = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
    icon = TrayIcon("Test", tip="tip",
                    icon_path=str(repo_icon) if repo_icon.exists() else None)
    commands: list[int] = []
    icon.on_command = commands.append
    started = icon.start()
    check("tray icon starts", started is True)

    if started:
        check("tray window created", icon._hwnd is not None)
        check("tray menu built", icon._menu is not None)
        check("notify data prepared", icon._nid is not None)
        # A blank tray icon is the visible symptom of a missing/failed icon.
        check("tray has an icon handle", bool(icon._nid and icon._nid.hIcon),
              "no icon loaded")
        icon.notify("Hi", "there")
        check("notify does not raise", True)
        icon.set_tip("new tip")
        check("tip updated", icon.current_tip() == "new tip", icon.current_tip())

        # Commands are delivered through the callback.
        icon._fire(TrayIcon.OPEN)
        icon._fire(TrayIcon.WEAR_LAST)
        check("commands dispatched", commands == [TrayIcon.OPEN, TrayIcon.WEAR_LAST],
              str(commands))

        # A raising handler must not take the message loop down with it.
        icon.on_command = lambda command: 1 / 0
        icon._fire(TrayIcon.QUIT)
        check("raising handler is contained", True)

    icon.stop()
    check("tray stops cleanly", icon._added is False)
    icon.notify("x", "y")
    check("notify after stop is a no-op", True)

    # Even with no icon file at all, a fallback handle must be produced so the
    # tray slot is never blank.
    bare = TrayIcon("Test", tip="tip", icon_path=None)
    fallback = bare._load_icon()
    check("missing icon file falls back", bool(fallback), "fallback handle was 0")


def test_wear_last() -> None:
    """The tray menu's "Wear last avatar" needs something to pick."""
    from tray import TrayIcon  # noqa: F401  (import guard for Windows-only path)

    b = _isolated_backend()
    check("nothing to wear yet", b.wear_last()["ok"] is False)

    first = "avtr_11111111-1111-1111-1111-111111111111"
    second = "avtr_22222222-2222-2222-2222-222222222222"
    b.add_by_id(first)
    b.add_by_id(second)

    # Only log entries count, and the most recent one wins.
    b._record_log(first, when="2026-01-01T00:00:00+00:00")
    b._record_log(second, when="2026-01-02T00:00:00+00:00")
    b.osc.listening = True
    b.osc.error = None
    sent: list[str] = []

    def record(avatar_id):
        sent.append(avatar_id)
        return True

    b.osc.change_avatar = record

    res = b.wear_last()
    check("wear_last picks the newest", sent == [second], str(sent))
    check("wear_last reports ok", res["ok"] is True)


def test_exit_on_close_setting() -> None:
    b = _isolated_backend()
    check("exit_on_close defaults on", b.get_settings()["exit_on_close"] is True)

    res = b.save_settings(9000, 9001, exit_on_close=False)
    check("settings saved with the flag", res["ok"] is True, str(res))
    check("flag persisted", b.settings["exit_on_close"] is False)
    check("flag reported back", b.get_settings()["exit_on_close"] is False)

    res = b.save_settings(9000, 9001, exit_on_close=True)
    check("flag toggled back", b.settings["exit_on_close"] is True)

    # The two-argument form used by older callers must not clear the flag.
    b.settings["exit_on_close"] = False
    b.save_settings(9000, 9001)
    check("omitted flag is left alone", b.settings["exit_on_close"] is False)


def test_tray_state_reported() -> None:
    b = _isolated_backend()
    check("no tray by default", b.get_state()["tray"] is False)

    class FakeTray:
        def __init__(self):
            self.shown = []

        def notify(self, title, message):
            self.shown.append((title, message))

    tray = FakeTray()
    b.set_tray(tray)
    check("tray reported once attached", b.get_state()["tray"] is True)
    b._notify_tray("Title", "Message")
    check("notify reaches the tray", tray.shown == [("Title", "Message")], str(tray.shown))


def test_settings_save_unchanged_ports() -> None:
    """Saving settings without changing the ports must not try to rebind.

    The rebind binds the same receive port the live listener is already holding,
    so it failed with WinError 10048 against ourselves.
    """
    from osc import OSCBridge

    b = _isolated_backend()
    # Give the backend a real listener, as in normal operation.
    b.osc = OSCBridge(send_ip="127.0.0.1", send_port=9000, receive_port=18771)
    b.osc.start()
    check("test bridge is listening", b.osc.listening, b.osc.error or "")
    try:
        # Same receive port: only the outgoing target can have changed.
        res = b.save_settings(9000, 18771, exit_on_close=False)
        check("unchanged ports save cleanly", res.get("ok") is True, str(res))
        check("still listening after save", b.osc.listening, b.osc.error or "")
        check("exit_on_close applied", b.settings["exit_on_close"] is False)
        check("settings persisted", storage.load_settings()["osc_receive_port"] == 18771)

        # Only the send port changed: still no rebind needed.
        res = b.save_settings(9002, 18771)
        check("send-only change saves cleanly", res.get("ok") is True, str(res))
        check("send port retargeted", b.osc.send_port == 9002, str(b.osc.send_port))
        check("same listener object reused", b.osc.listening, b.osc.error or "")

        # A genuinely new receive port does rebind, and works.
        res = b.save_settings(9000, 18772)
        check("port change rebinds", res.get("ok") is True, str(res))
        check("new receive port active", b.osc.receive_port == 18772)
        check("listening on the new port", b.osc.listening, b.osc.error or "")
    finally:
        b.osc.stop()

    # An unavailable new port must still fail without persisting.
    import socket
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", 0))
    taken = blocker.getsockname()[1]
    try:
        storage.save_settings({"osc_send_port": 9000, "osc_receive_port": 9001})
        res = b.save_settings(9100, taken)
        check("genuine conflict still reported", res.get("ok") is False, str(res))
        check("conflict left settings alone",
              storage.load_settings()["osc_send_port"] == 9000)
    finally:
        blocker.close()


def test_undo_keeps_the_thumbnail() -> None:
    """Delete then Undo must bring the picture back, not just the record."""
    b = _isolated_backend()
    avatar_id = "avtr_beefbeef-1111-2222-3333-444444444444"
    b.add_by_id(avatar_id)
    entry = b._entry(avatar_id)
    entry["name"] = "Has a picture"
    entry["thumb"] = "beef.png"
    image = storage.THUMBS_DIR / "beef.png"
    image.write_bytes(b"fake-png-bytes")

    check("delete succeeds", b.delete(avatar_id)["ok"] is True)
    check("thumbnail survives the delete", image.exists(),
          "undo would restore a filename pointing at nothing")

    res = b.restore_entry(entry)
    check("restore succeeds", res["ok"] is True, str(res))
    back = b._entry(avatar_id)
    check("entry is back", back is not None)
    check("thumb filename restored", back.get("thumb") == "beef.png")
    check("image still readable", image.exists())

    # And the picture is actually served again.
    served = b.get_thumbnail(avatar_id)
    check("thumbnail served after undo", served.startswith("data:image/png;base64,"),
          served[:40])


def test_prune_respects_undo_grace() -> None:
    """A just-deleted avatar must keep its image until undo expires."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        storage.THUMBS_DIR = root / "thumbs"
        storage.THUMBS_DIR.mkdir(parents=True, exist_ok=True)

        fresh = storage.THUMBS_DIR / "fresh.png"
        fresh.write_bytes(b"x")
        # An orphan that predates the undo window.
        old = storage.THUMBS_DIR / "old.png"
        old.write_bytes(b"x")
        ancient = 1_000_000_000
        os.utime(old, (ancient, ancient))

        orphans, _ = storage.prune_thumbs(set())
        check("fresh orphan is spared", fresh.exists(), "undo would lose the image")
        check("old orphan removed", not old.exists())
        check("only the old one counted", orphans == 1, str(orphans))

        # Once it is old enough it goes.
        orphans, _ = storage.prune_thumbs(set(), grace_seconds=0.0)
        check("fresh orphan removed with no grace", not fresh.exists())
        check("counted", orphans == 1, str(orphans))


def test_close_to_tray_contract() -> None:
    """Pin pywebview's cancel polarity, which is the opposite of the obvious.

    ``Event.set()`` returns True -- meaning "cancel" -- when a handler returns
    False, and the WinForms backend then sets ``args.Cancel = True``. Returning
    True from a closing handler lets the window close.
    """
    from webview.event import Event

    from main import _Shell

    class FakeTray:
        _added = True

        def __init__(self):
            self.shown: list[tuple[str, str]] = []

        def notify(self, title, message):
            self.shown.append((title, message))

    b = _isolated_backend()
    shell = _Shell(b)
    fake = FakeTray()
    shell.tray = fake

    def cancelled_by_pywebview() -> bool:
        event = Event(None, should_lock=True)
        event += shell.on_closing
        return bool(event.set())

    # No tray: the close must go through, whatever the setting says.
    no_tray = _Shell(b)
    no_tray.tray = None
    event = Event(None, should_lock=True)
    event += no_tray.on_closing
    check("close proceeds with no tray icon", event.set() is False)

    # Tray present, but the user asked to exit on close.
    b.settings["exit_on_close"] = True
    check("close proceeds when exit_on_close is set", cancelled_by_pywebview() is False)

    # Tray present and hide-to-tray enabled: the close must be cancelled.
    b.settings["exit_on_close"] = False
    check("close is cancelled when hiding to tray", cancelled_by_pywebview() is True)
    check("the user is told it is still running", bool(fake.shown), str(fake.shown))

    # Quitting from the tray menu must not be intercepted.
    b.settings["exit_on_close"] = False
    shell.quitting = True
    check("quit from the tray is not intercepted", cancelled_by_pywebview() is False)
    shell.quitting = False


def test_tray_icon_asset_is_bundled() -> None:
    """A missing icon file means a blank notification-area icon.

    The PyInstaller spec has to ship assets/, because the tray loads icon.ico
    from disk at runtime rather than from the executable's icon resource.
    """
    spec = (Path(__file__).resolve().parent.parent
            / "LocalAvatarFavourites.spec").read_text(encoding="utf-8")
    check("spec ships the assets folder", "'assets'" in spec and "assets" in spec,
          "assets missing from datas")
    check("icon exists in the repo",
          (Path(__file__).resolve().parent.parent / "assets" / "icon.ico").exists())


def test_motion_setting() -> None:
    """Animation preference must survive a round trip and reject junk."""
    b = _isolated_backend()

    check("motion defaults to full", b.settings.get("motion") == "full",
          str(b.settings.get("motion")))
    check("default is documented", storage.DEFAULT_MOTION == "full")
    check("motion reported in settings", b.get_settings()["motion"] == "full")
    check("motion reported in state", b.get_state()["motion"] == "full")

    for mode in ("system", "full", "none"):
        b.save_settings(9000, 9001, motion=mode)
        check(f"motion accepts {mode}", b.settings["motion"] == mode, str(b.settings["motion"]))
        check(f"motion {mode} persists",
              storage.load_settings()["motion"] == mode,
              str(storage.load_settings()["motion"]))

    # Junk must fall back to the default, not to a value that becomes an
    # unrecognised attribute on <html> and silently disables the override.
    for junk in ("fullish", "", "reduced", None, 5, ["full"]):
        b.save_settings(9000, 9001, motion=junk)
        check(f"motion rejects {junk!r}",
              b.settings["motion"] == storage.DEFAULT_MOTION,
              str(b.settings["motion"]))

    # Casing and padding are accepted rather than treated as junk, so the two
    # entry points (UI save and hand-edited file) behave the same.
    for variant in ("FULL", "  Full  ", "NoNe"):
        b.save_settings(9000, 9001, motion=variant)
        check(f"motion normalises {variant!r}",
              b.settings["motion"] == variant.strip().lower(), str(b.settings["motion"]))
        storage.save_settings({"motion": variant})
        check(f"file normalises {variant!r}",
              storage.load_settings()["motion"] == variant.strip().lower(),
              str(storage.load_settings()["motion"]))

    # Omitted means untouched, like exit_on_close.
    b.settings["motion"] = "none"
    b.save_settings(9000, 9001)
    check("omitted motion is left alone", b.settings["motion"] == "none")


def main() -> int:
    # Results carry real VRChat data, and VRChat names are full of characters a
    # legacy console cannot encode -- ［Protogen］Kuro alone is enough to raise
    # UnicodeEncodeError, because the fullwidth bracket ［ is not in cp1252,
    # which is the Windows ANSI default. The reporter then dies mid-run and the
    # whole suite reports as failed, hiding the results it had already produced.
    # Switch to UTF-8 and replace anything still unencodable, so a name can never
    # turn a passing run into a failing one.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    tests = [
        test_storage,
        test_storage_merge,
        test_storage_security,
        test_storage_import_ignores_local_paths,
        test_settings_sanitize,
        test_settings_load_recovers_from_corrupt_file,
        test_auth_token_encryption,
        test_osc_receive,
        test_osc_send,
        test_api_helpers,
        test_api_image_download_guards,
        test_vrclog_parse,
        test_vrclog_ignores_noise_lines,
        test_default_avatar_list,
        test_default_avatar_name_matching,
        test_update_compare,
        test_vrcache_pattern,
        test_vrcache_db_path,
        test_vrcache_sqlite_watermark,
        test_vrcache_rowid_regression,
        test_vrcache_degraded_sources,
        test_vrcache_amplitude,
        test_vrcache_dedupe_across_layers,
        test_vrcache_is_read_only,
        test_vrcache_locked_database,
        test_log_dedupe_across_sources,
        test_log_dedupe_persisted,
        test_log_size_limits,
        test_log_size_limits_enforced,
        test_lowering_the_cap_trims_immediately,
        test_log_size_caps_applied_at_startup,
        test_log_size_caps_reject_nonsense,
        test_log_size_caps_survive_a_hand_edited_file,
        test_defaults_are_never_recorded,
        test_defaults_are_not_recorded_as_player_changes,
        test_defaults_are_pruned_on_start,
        test_save_all_skips_defaults,
        test_expired_token_is_not_private,
        test_osc_settings_not_persisted_on_failure,
        test_discovery_state_reported,
        test_wear_confirms_or_reports_failure,
        test_import_vrchat_favourites,
        test_import_vrchat_requires_login,
        test_login_with_two_factor,
        test_login_reports_bad_credentials,
        test_login_rejects_a_wrong_2fa_code,
        test_login_requires_credentials,
        test_entry_index_stays_consistent,
        test_save_all_logs_is_not_quadratic,
        test_api_rate_limit_backoff,
        test_job_progress,
        test_job_counts_failures,
        test_bulk_actions,
        test_restore_entry_for_undo,
        test_start_metadata_job_requires_login,
        test_tray_icon,
        test_close_to_tray_contract,
        test_tray_icon_asset_is_bundled,
        test_wear_last,
        test_exit_on_close_setting,
        test_motion_setting,
        test_settings_save_unchanged_ports,
        test_undo_keeps_the_thumbnail,
        test_prune_respects_undo_grace,
        test_tray_state_reported,
        test_thumbnail_pruning,
        test_delete_leaves_thumbnail_for_undo,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            RESULTS.append(f"[PASS] {test.__name__}")
        except Exception as exc:
            failed += 1
            RESULTS.append(f"[FAIL] {test.__name__}: {exc}")
    for line in RESULTS:
        print(line)
    passed = len([r for r in RESULTS if r.startswith("[PASS]")])
    failed_count = len([r for r in RESULTS if r.startswith("[FAIL]")])
    print(f"\n{passed} passed, {failed_count} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
