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


def test_delete_removes_thumbnail() -> None:
    b = _isolated_backend()
    avatar_id = "avtr_7a7a7a7a-1111-2222-3333-444444444444"
    b.add_by_id(avatar_id)
    entry = b._entry(avatar_id)
    entry["thumb"] = "orphan-me.png"
    storage.thumb_file_path(avatar_id).parent.mkdir(parents=True, exist_ok=True)
    (storage.THUMBS_DIR / "orphan-me.png").write_bytes(b"x")

    check("delete succeeds", b.delete(avatar_id)["ok"] is True)
    check("thumbnail deleted with the favourite",
          not (storage.THUMBS_DIR / "orphan-me.png").exists())


def main() -> int:
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
        test_expired_token_is_not_private,
        test_osc_settings_not_persisted_on_failure,
        test_discovery_state_reported,
        test_wear_confirms_or_reports_failure,
        test_import_vrchat_favourites,
        test_import_vrchat_requires_login,
        test_entry_index_stays_consistent,
        test_save_all_logs_is_not_quadratic,
        test_api_rate_limit_backoff,
        test_thumbnail_pruning,
        test_delete_removes_thumbnail,
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
