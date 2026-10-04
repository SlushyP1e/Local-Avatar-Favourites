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
from osc import OSCBridge, AVATAR_CHANGE_ADDRESS
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

    id_events: list[dict] = []
    vrclog.VRCLogWatcher._parse_line(
        "2024.01.02 03:04:05 Log - avtr_12345678-1234-1234-1234-123456789abc loaded", id_events)
    check("vrclog id parse", len(id_events) == 1 and id_events[0]["type"] == "avatar-id"
          and id_events[0]["id"].startswith("avtr_"))


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
    return ["avtr_%08x-1234-1234-1234-%012x" % (i, i) for i in range(start, start + count)]


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
    from vrcache import avatar_db_path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        low = root / "LocalLow" / "VRChat" / "VRChat"
        low.mkdir(parents=True)
        check("vrcache default path", avatar_db_path(low).name == "avatars.sqlite")
        check("vrcache default parent", avatar_db_path(low).parent == low)

        # No config.json at all.
        check("vrcache missing config -> default", avatar_db_path(low).parent == low)

        relocated = root / "SomeOtherCache"
        (relocated / "Cache-WindowsPlayer").mkdir(parents=True)
        (low / "config.json").write_text(
            json.dumps({"cache_directory": str(relocated)}), encoding="utf-8")
        check("vrcache honours cache_directory",
              avatar_db_path(low) == relocated / "Cache-WindowsPlayer" / "avatars.sqlite",
              str(avatar_db_path(low)))

        # A corrupt config must not break resolution.
        (low / "config.json").write_text("{not json", encoding="utf-8")
        check("vrcache corrupt config -> default", avatar_db_path(low).parent == low)


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
    from vrcache import MISSING, SOURCE_AMPLITUDE, SOURCE_SQLITE, UNSUPPORTED, VRCacheWatcher

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
        amp.write_bytes((" ".join(_fake_ids(2, start=700)).encode()))
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
            ids=backlog + [shared[0]],
            amplitude=(" ".join(live + shared)).encode(),
        )
        w = VRCacheWatcher(low_dir=low, amp_path=amp)
        # Seed only from the database so amplitude entries are still "new".
        w._read_sqlite(emit=False)
        w._seen.update(backlog + [shared[0]])

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
            except Exception as exc:  # noqa: BLE001
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


def main() -> int:
    tests = [
        test_storage,
        test_storage_merge,
        test_osc_receive,
        test_osc_send,
        test_api_helpers,
        test_vrclog_parse,
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
    ]
    failed = 0
    for test in tests:
        try:
            test()
            RESULTS.append(f"[PASS] {test.__name__}")
        except Exception as exc:  # noqa: BLE001
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
