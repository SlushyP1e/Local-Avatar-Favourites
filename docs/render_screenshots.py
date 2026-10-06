"""Render the real frontend in headless Chrome to produce README screenshots.

Why a browser and not the packaged app
--------------------------------------
These are documentation images of the *interface*, so they must come from the
actual index.html/style.css/app.js that ship. Driving them through a real
Chromium via pywebview would also mean starting the app, which needs VRChat's
OSC port and the user's own favourites.json -- neither of which belongs in a
documentation build, and both of which would put someone's real avatars in a
public README.

Instead this loads the same three files, stubs the pywebview bridge with fixture
data, and screenshots the result. The styles and markup are the shipped ones, so
what the images show is what users get.

Usage:  python docs/render_screenshots.py [--out docs]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"

CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

# Window size matches the app's own default (1120x720) closely enough that the
# grid gets the same column count a user sees on launch.
WIDTH, HEIGHT = 1400, 860

# Fixture data. Names and authors are invented; the thumbnails are generated
# locally as SVG data URIs so no real avatar artwork is redistributed.
NAMES = [
    ("Robo Buddy", "Mecha"), ("Kitsune Fox", "Foxi"), ("Ghost Cat", "Nyano"),
    ("Cyber Ronin", "Kikyo"), ("E-girl Base", "Vrael"), ("Nardragon", "Puppy"),
    ("SCP-079", "Krdzex"), ("Kaiju Kitty", "okKeith"), ("Simple", "PhantomWithin"),
    ("Cake ~ SKIP4D CHIBI", "ashenfang9"), ("Noah by Hayewee", "Hayewee"),
    ("Yuji Itadori", "gooo boy"), ("15th Anniversary Chop...", "Alykeiii"),
    ("Ryuon", "X_togi_X"), ("alban yayyyyyyyyyyyy", "SuperN0vak"),
    ("Varex'Zorame", "Void"), ("GREEN V2 // AVA/VM", "Retro"),
    ("chiika.", "vermillion"), ("Knight-Warrior Teym L...", "Elyse"),
    ("Konahmaru Sarutobi", "Fennec"), ("Hatsune Miku", "Rin"),
    ("Angel Boy", "Stereo"), ("Dust Bunny", "Kumo"), ("Prism Guard", "Sable"),
    ("Lime Gremlin", "Pickles"), ("Sea Witch", "Marina"), ("Tidepool", "Nix"),
    ("Ember Fox", "Cinder"), ("Paper Crane", "Orizuru"), ("Night Bloom", "Yoru"),
    ("Copper Moth", "Ember"), ("Quiet Storm", "Rai"), ("Velvet Ghost", "Moth"),
    ("Salt Marsh", "Brine"), ("Glass Koi", "Nishiki"), ("Amber Deer", "Fern"),
    ("Static Fox", "Noise"), ("Pine Hollow", "Cedar"), ("Cobalt Hare", "Hop"),
    ("Wireframe Kid", "Polygon"), ("Slate Wolf", "Grey"), ("Lantern Fish", "Abyss"),
    ("Clay Automaton", "Terra"), ("Dusk Moth", "Vesper"), ("Iron Finch", "Brass"),
    ("Paper Tiger", "Origami"), ("Driftwood", "Tide"), ("Pale Heron", "Wade"),
    ("Tin Compass", "North"), ("Quiet Ember", "Coal"), ("Small Nebula", "Void"),
    ("Long Grass", "Prairie"), ("Rust Lantern", "Forge"), ("Grey Seal", "Splash"),
    ("Old Coin", "Tally"), ("Blue Hour", "Dusk"), ("Fern Shadow", "Shade"),
    ("Cold Tea", "Kettle"), ("Wide Field", "Meadow"), ("Two Stones", "Cairn"),
    ("Low Tide", "Ebb"), ("Warm Static", "Snow"), ("Green Glass", "Bottle"),
    ("Third Winter", "Frost"), ("Nightjar", "Wren"), ("Copper Wire", "Spool"),
    ("Slow River", "Drift"), ("Open Hand", "Palm"), ("Paper Moon", "Luna"),
    ("Dust Devil", "Canyon"), ("Bright Nail", "Steel"), ("Sea Glass", "Shore"),
    ("Thin Rain", "Drizzle"), ("Amber Road", "Track"), ("Quiet Bell", "Toll"),
    ("Grey Orchard", "Bough"), ("Long Winter", "Frost"), ("Low Bell", "Hollow"),
    ("Six of Cups", "Recall"), ("Nine of Wands", "Guard"), ("The Fool", "Zero"),
    ("The Magician", "One"), ("The Hermit", "Nine"), ("Wheel of Fortune", "Turn"),
]
GROUPS = ["emo", "chibi", "furry", "mecha", "humans", "abstract"]

# Which avatars get a thumbnail tile rather than the letter placeholder.
# A real collection is mostly thumbbed once metadata has been fetched, with a
# scattering of stragglers still showing the placeholder. Both states appear, and
# page 2 is as representative as page 1.
NOT_THUMBED = {7, 22, 31, 38, 44, 47, 52, 57, 63, 68, 72, 79, 85, 89, 92}
THUMBED = set(range(0, 94)) - NOT_THUMBED

HUES = [206, 268, 18, 172, 316, 42, 96, 348, 224, 12]


def _thumb(index: int, name: str) -> str:
    """A generated placeholder tile, so no real artwork is redistributed."""
    hue = HUES[index % len(HUES)]
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='200' height='300'>"
        f"<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        f"<stop offset='0' stop-color='hsl({hue},58%,52%)'/>"
        f"<stop offset='1' stop-color='hsl({hue + 40},52%,34%)'/>"
        f"</linearGradient></defs>"
        f"<rect width='200' height='300' fill='url(#g)'/>"
        f"<text x='100' y='168' font-size='84' font-family='Segoe UI, sans-serif'"
        f" fill='rgba(255,255,255,0.35)' text-anchor='middle'>"
        f"{(name or '?').strip()[0].upper()}</text>"
        f"</svg>"
    )
    return "data:image/svg+xml;utf8," + svg.replace("#", "%23")


def entries(count: int) -> list[dict]:
    # Strictly decreasing dates, one per entry. The grid defaults to newest
    # first, so this fixes the sort order to the fixture order. Repeating a short
    # cycle instead would shuffle the rows and put the un-thumbbed tail of the
    # collection on page 1, which is not what a real list looks like.
    newest = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    out = []
    for i in range(count):
        name, author = NAMES[i % len(NAMES)]
        # A slice of the collection has no thumbnail, which is what the
        # placeholder is for and keeps the screenshot honest.
        thumb = f"avtr_{i:032x}.png" if i in THUMBED else None
        added = newest - timedelta(days=i, hours=i % 7)
        out.append({
            "id": f"avtr_{i:08x}-1111-2222-3333-444444444444",
            "name": name,
            "notes": "",
            "tags": [],
            "group": "" if i % 7 == 0 else GROUPS[i % len(GROUPS)],
            "added": added.isoformat(timespec="seconds"),
            "thumb": thumb,
            "thumb_url": "",
            "author": author,
            "platforms": [["PC", "Quest", "iOS"][i % 3]],
            "release_status": "public" if i % 5 else "private",
            "favorite": i % 6 == 0,
            "_fixture_thumb": _thumb(i, name) if thumb else None,
        })
    return out


def log_rows(count: int, entry_ids: list[str]) -> list[dict]:
    rows = []
    for i in range(count):
        name, _ = NAMES[(i * 3) % len(NAMES)]
        rows.append({
            "id": entry_ids[i % len(entry_ids)],
            "name": name if i % 4 else "",
            "first_seen": "2026-10-01T09:00:00+00:00",
            "last_seen": f"2026-10-06T0{(i % 9) + 1}:1{i % 9}:00+00:00",
            "seen_bucket": f"2026-10-06T0{(i % 9) + 1}:1{i % 9}",
            "count": (i % 5) + 1,
            "private": i % 11 == 0,
            "source": ["cache-db", "log", "osc"][i % 3],
        })
    return rows


def changes(count: int) -> list[dict]:
    names = [n for n, _ in NAMES]
    out = []
    for i in range(count):
        out.append({
            "player": names[(i * 5) % len(names)] + str(i % 9),
            "avatar": names[(i * 7) % len(names)],
            "first_seen": "2026-10-05T20:00:00+00:00",
            "last_seen": f"2026-10-06T{(i % 20):02d}:30:00+00:00",
            "count": (i % 3) + 1,
        })
    return out


def build_fixture() -> dict:
    """Counts chosen to show the paginated UI doing real work.

    94 favourites is two pages of 50, which is the case that was reported as
    showing no pagination at all.
    """
    ents = entries(94)
    ids = [e["id"] for e in ents]
    return {
        "entries": ents,
        "logs": log_rows(118, ids),
        "changes": changes(64),
        "ignores": ["avtr_0000002f-1111-2222-3333-444444444444",
                    "avtr_00000031-1111-2222-3333-444444444444"],
        "ignore_names": {
            "avtr_0000002f-1111-2222-3333-444444444444": "Varex'Zorame",
            "avtr_00000031-1111-2222-3333-444444444444": "chiika.",
        },
    }


# The app animates cards in on a stagger and slides panels between views. Under
# a headless capture that means photographing rows mid-fade, which looks like a
# rendering fault. The app already supports a "none" motion preference for
# exactly this kind of environment, so the capture uses it rather than patching
# the stylesheet -- the images then show the layout, not the animation.
NO_MOTION_CSS = """
<style>
  *, *::before, *::after {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
    transition-duration: 0s !important;
    transition-delay: 0s !important;
  }
  /* Rows are mid-fade at 0% opacity before their stagger delay elapses, so the
     duration override alone would still catch them invisible. */
  .card, .log-row { opacity: 1 !important; }
</style>
"""

BRIDGE_JS = r"""
// A stand-in for window.pywebview.api. app.js is written against the bridge, so
// the real UI can be driven without VRChat, OSC or the user's own data.
(function () {
  const FIXTURE = window.__FIXTURE__;
  // One entry, resolved at call time, for beats that need a specific avatar.
  window.FIXTURE_ENTRY_ID = FIXTURE.entries[3].id;
  const api = {
    get_state: async () => ({
      version: "1.9.0",
      current_avatar_id: FIXTURE.entries[6].id,
      status: "",
      logged_in: true,
      session_expired: false,
      username: "example",
      pending_2fa: false,
      revs: { entries: 1, logs: 1, changes: 1 },
      discovery: {
        sources: { "cache-db": "ok", amplitude: "empty", log: "ok" },
        backlog: 22670, db_path: "LocalLow/VRChat/VRChat/avatars.sqlite",
        defaults: 257,
      },
      job: null,
      motion: "full",
      tray: true,
      osc: { listening: true, error: null, seen_traffic: true },
      thumb_base: "",
      entries: FIXTURE.entries,
      logs: FIXTURE.logs,
      changes: FIXTURE.changes,
    }),
    get_settings: async () => ({
      version: "1.9.0",
      osc_send_port: 9000, osc_receive_port: 9001,
      username: "example", logged_in: true, session_expired: false,
      pending_2fa: false, twofa_methods: [], twofa_method: "",
      discovery: {
        sources: { "cache-db": "ok", amplitude: "empty", log: "ok" },
        backlog: 22670, db_path: "LocalLow/VRChat/VRChat/avatars.sqlite",
        defaults: 257,
      },
      exit_on_close: true, motion: "full",
      max_avatar_log: 200, max_player_changes: 1000, tray: true,
    }),
    get_ignores: async () => ({
      ok: true, ids: FIXTURE.ignores, names: FIXTURE.ignore_names,
    }),
    // Thumbnails are served as data URIs here, which is what the app does when
    // its loopback server is unavailable.
    get_thumbnail: async (id) => {
      const hit = FIXTURE.entries.find((e) => e.id === id);
      return (hit && hit._fixture_thumb) || "";
    },
    check_updates: async () => ({ ok: true, update: false, current: "1.9.0" }),
    save_settings: async () => ({ ok: true }),
    add_ignore: async () => ({ ok: true }),
    remove_ignore: async () => ({ ok: true }),
    // Present because app.js calls them on any delete path. Without them the
    // bridge throws, call() turns it into {ok:false}, and every action that
    // depends on a successful delete silently does nothing -- which is exactly
    // the class of bug a screenshot harness can otherwise hide.
    delete: async () => ({ ok: true }),
    restore_entry: async () => ({ ok: true }),
    save_from_log: async () => ({ ok: true }),
    import_vrchat_favourites: async () => ({ ok: true, added: 0 }),
    import_favourites: async () => ({ ok: true, added: 0 }),
    delete_log: async () => ({ ok: true }),
    clear_logs: async () => ({ ok: true }),
    clear_changes: async () => ({ ok: true }),
    get_job: async () => null,
  };
  window.pywebview = { api };
  window.addEventListener("load", () => {
    window.dispatchEvent(new Event("pywebviewready"));
  });
})();
"""


def find_chrome() -> Path | None:
    for path in CHROME_CANDIDATES:
        if path.exists():
            return path
    found = shutil.which("chrome") or shutil.which("msedge")
    return Path(found) if found else None


# Forced after a beat, before the capture.
#
# The app loads thumbnails lazily: an IntersectionObserver fires when an image
# nears the viewport, and only then is the bridge asked for it. That is exactly
# right for a running app and exactly wrong for a screenshot, where the capture
# races the observer and comes out as a grid of blank placeholders -- and not
# even reproducibly blank. Writing the sources in directly uses the same images
# the app would have fetched, without the race.
FORCE_THUMBS_JS = """
setTimeout(function () {
  var byId = {};
  window.__FIXTURE__.entries.forEach(function (e) { byId[e.id] = e; });
  document.querySelectorAll('[data-thumb-id]').forEach(function (img) {
    var hit = byId[img.dataset.thumbId];
    if (hit && hit._fixture_thumb) img.src = hit._fixture_thumb;
  });
  // The drawer preview carries the same data attribute and would otherwise be
  // left blank by the same race.
  var preview = document.getElementById('preview');
  if (preview) {
    var p = byId[preview.dataset.thumbId];
    if (p && p._fixture_thumb) preview.src = p._fixture_thumb;
  }
}, 2200);
"""

# One entry per image: the file, the setup run before the capture, and whether
# to scroll to the bottom first.
#
# The JS uses bare identifiers, not `app.something`. app.js is a classic script,
# so its top-level `let`/`function` declarations land in the shared global
# lexical environment and are reachable from a later inline script -- but they
# are not properties of `window`, so `app.setView` would be undefined.
#
# `bottom` matters for the pager: the page buttons sit after the last row, so at
# a normal scroll position they are below the fold and the one feature these
# images exist to show would be invisible.
SHOTS = [
    {
        "file": "screenshot-home.png",
        "caption": "Favourites grid",
        # The default landing view, which is what a user sees on launch.
        "js": "setView('home');",
    },
    {
        "file": "screenshot-pager.png",
        "caption": "Page buttons",
        # Scrolled to the end of page 1, where the pager sits.
        "js": "setView('home'); document.getElementById('grid-wrap').scrollTop = 99999;",
        "bottom": True,
    },
    {
        "file": "screenshot-page2.png",
        "caption": "Page 2",
        "js": "setView('home'); goToPage('grid', 2);",
        "bottom": True,
    },
    {
        "file": "screenshot-drawer.png",
        "caption": "Avatar details",
        # The drawer is the only place notes, tags and groups are edited, so it
        # is worth documenting even though these releases did not change it.
        "js": "setView('home'); openDrawer(FIXTURE_ENTRY_ID);",
    },
    {
        "file": "screenshot-logs.png",
        "caption": "Avatar logs",
        "js": "setView('logs'); logTab = 'avatars'; renderLogs(true);",
    },
    {
        "file": "screenshot-settings.png",
        "caption": "Settings",
        # The panel is taller than a normal window, and .modal-card caps itself at
        # 90vh with its own scrollbar. Scrolling it to show the new Ignored
        # avatars section then crops the top half of the dialog, which is worse
        # than not showing the section at all. So this shot gets a viewport tall
        # enough to hold the whole panel, and no scrolling.
        "js": "openSettings();",
        "height": 1560,
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "docs"),
                        help="where to write the PNGs (default: docs/)")
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--height", type=int, default=HEIGHT)
    args = parser.parse_args()

    chrome = find_chrome()
    if chrome is None:
        print("No Chrome or Edge found; cannot render screenshots.", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    fixture = build_fixture()
    index = (WEB / "index.html").read_text(encoding="utf-8")
    if '<script src="app.js"></script>' not in index:
        print("Could not find the app.js script tag in index.html.", file=sys.stderr)
        return 1
    # Inject the bridge before app.js runs, since app.js binds to it at load,
    # and the no-motion override into <head> so it wins over style.css.
    index = index.replace(
        "</head>", NO_MOTION_CSS + "</head>", 1
    ).replace(
        '<script src="app.js"></script>',
        '<script>window.__FIXTURE__ = ' + json.dumps(fixture) + ';</script>\n'
        '<script src="bridge.js"></script>\n'
        '<script src="app.js"></script>',
    )

    written: list[Path] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "style.css").write_bytes((WEB / "style.css").read_bytes())
        (tmp_dir / "app.js").write_bytes((WEB / "app.js").read_bytes())
        (tmp_dir / "bridge.js").write_text(BRIDGE_JS, encoding="utf-8")
        page = tmp_dir / "index.html"
        page.write_text(index, encoding="utf-8")

        for shot in SHOTS:
            target = out_dir / shot["file"]
            # A shot may ask for a taller window than the default. The settings
            # dialog needs one, because it is a scrolling panel taller than a
            # normal viewport and cropping it looks like a broken screenshot.
            height = int(shot.get("height") or args.height)
            # --virtual-time-budget fast-forwards timers, so the 700ms state poll
            # and the stagger animations have both settled before the capture.
            cmd = [
                str(chrome),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=2",
                f"--window-size={args.width},{height}",
                "--virtual-time-budget=2000",
                f"--screenshot={target}",
                f"file:///{page.as_posix()}",
            ]
            # The setup JS is appended to the page so it runs inside the document
            # alongside app.js, with app.js's own globals in scope. The optional
            # `after` step runs later, for anything that has to wait on an async
            # render before it can move the scroll position.
            extra = ""
            if shot.get("after"):
                extra = "setTimeout(function(){" + shot["after"] + "}, 900);"
            inject = tmp_dir / f"shot-{shot['file']}.html"
            inject.write_text(
                index.replace(
                    '<script src="app.js"></script>',
                    '<script src="app.js"></script>\n<script>setTimeout(function(){'
                    + shot["js"]
                    + '}, 400);</script>' + extra + FORCE_THUMBS_JS,
                ),
                encoding="utf-8",
            )
            cmd[-1] = f"file:///{inject.as_posix()}"
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if not target.exists():
                print(f"FAILED {shot['file']}\n{result.stderr[-2000:]}", file=sys.stderr)
                return 1
            written.append(target)
            print(f"  {shot['file']:26} {target.stat().st_size / 1024:7.1f} KB"
                  f"  {shot['caption']}")

    print(f"\nWrote {len(written)} screenshots to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
