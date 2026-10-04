"""Headless self-test for the Local Avatar Favourites core.

Run with:  python -m app.selftest
Exits with code 0 on success, 1 on failure. Does not open any windows or
make network calls.
"""

from __future__ import annotations

import os
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
    from backend import Backend

    check("version newer", Backend._is_newer("1.2.0", "1.1.0"))
    check("version same", not Backend._is_newer("1.1.0", "1.1.0"))
    check("version older", not Backend._is_newer("1.0.0", "1.1.0"))
    check("version minor ordering", Backend._is_newer("1.10.0", "1.9.0"))


def main() -> int:
    tests = [
        test_storage,
        test_storage_merge,
        test_osc_receive,
        test_osc_send,
        test_api_helpers,
        test_vrclog_parse,
        test_update_compare,
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
