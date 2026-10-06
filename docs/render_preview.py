"""Render docs/preview.gif by capturing the real UI in headless Chrome.

Why this exists
---------------
The GIF in the README is the first thing anyone sees, and it was last rebuilt
when the app was at v1.1.1: six avatars, no groups, no pages. A preview that
contradicts the screenshots beside it makes the project look abandoned.

Rebuilding it by hand means launching the app, which needs VRChat's OSC port,
and it would put the user's own avatars in a public README. So this drives the
shipped index.html/style.css/app.js in headless Chrome against fixture data and
captures a frame per beat.

Usage:  python docs/render_preview.py [--out docs/preview.gif]
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Imported from the screenshot renderer rather than duplicated: both drive the
# same page, and a copy of the bridge stub would be free to drift out of step
# with it -- which would show up as a preview that does not match the stills.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_screenshots import (
    BRIDGE_JS,
    FORCE_THUMBS_JS,
    NO_MOTION_CSS,
    WEB,
    build_fixture,
    find_chrome,
)

ROOT = Path(__file__).resolve().parent.parent

# One beat per frame. Each is a view state a user actually lands in.
#
# Every beat has to set the view itself: renderLogs and renderGrid both return
# early unless currentView matches, so a beat that only flips logTab from the
# home view silently captures the home view again.
#
# The sequence is deliberately calm. Consecutive frames are small changes --
# page 1 to page 2, one filter to another -- because a GIF that cuts hard
# between unrelated screens reads as the interface lurching or zooming rather
# than as a walkthrough. Holds are long for the same reason: about two seconds
# each is long enough to read, and short enough that the loop stays a preview.
BEATS = [
    {"name": "grid", "js": "setView('home');", "hold": 2000},
    {"name": "page2", "js": "setView('home'); goToPage('grid', 2);", "hold": 2000},
    {"name": "page1", "js": "setView('home'); goToPage('grid', 1);", "hold": 1600},
    {"name": "group", "js": "setView('home'); currentGroup = 'furry'; renderGrid(true);",
     "hold": 1800},
    {"name": "all", "js": "setView('home'); currentGroup = null; renderGrid(true);",
     "hold": 1600},
    {"name": "logs", "js": "setView('logs'); logTab = 'avatars'; renderLogs(true);",
     "hold": 2000},
    {"name": "players", "js": "setView('logs'); logTab = 'players'; renderLogs(true);",
     "hold": 2000},
    {"name": "home", "js": "setView('home');", "hold": 1800},
]


def build_page(fixture: dict) -> str:
    index = (WEB / "index.html").read_text(encoding="utf-8")
    return (index
            .replace("</head>", NO_MOTION_CSS + "</head>", 1)
            .replace(
                '<script src="app.js"></script>',
                '<script>window.__FIXTURE__ = ' + json.dumps(fixture) + ';</script>\n'
                '<script src="bridge.js"></script>\n'
                '<script src="app.js"></script>',
            ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "docs" / "preview.gif"))
    parser.add_argument("--width", type=int, default=1180)
    parser.add_argument("--height", type=int, default=740)
    parser.add_argument("--scale", type=float, default=1.0,
                        help="shrink the capture, for a smaller GIF")
    args = parser.parse_args()

    chrome = find_chrome()
    if chrome is None:
        print("No Chrome or Edge found; cannot render the preview.", file=sys.stderr)
        return 1

    # Frames are captured one at a time by reloading the page with a different
    # beat applied. Chrome's --screenshot cannot drive a session, so a small
    # headless server serves the page and each beat is fetched by index.
    fixture = build_fixture()
    page_html = build_page(fixture)

    frames: list[bytes] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "style.css").write_bytes((WEB / "style.css").read_bytes())
        (tmp_dir / "app.js").write_bytes((WEB / "app.js").read_bytes())
        (tmp_dir / "bridge.js").write_text(BRIDGE_JS, encoding="utf-8")

        for index, beat in enumerate(BEATS):
            shot = tmp_dir / f"beat-{index:02d}.html"
            shot.write_text(
                page_html.replace(
                    '<script src="app.js"></script>',
                    '<script src="app.js"></script>\n<script>setTimeout(function(){'
                    + beat["js"] + '}, 500);</script>' + FORCE_THUMBS_JS,
                ),
                encoding="utf-8",
            )
            target = tmp_dir / f"frame-{index:02d}.png"
            cmd = [
                str(chrome),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                f"--force-device-scale-factor={args.scale}",
                f"--window-size={args.width},{args.height}",
                # Generous, because thumbnails load lazily: the
                # IntersectionObserver has to fire and then resolve one async
                # get_thumbnail per visible card. A short budget photographs the
                # grid before any image arrives, which makes the capture
                # non-deterministic run to run.
                "--virtual-time-budget=8000",
                f"--screenshot={target}",
                f"file:///{shot.as_posix()}",
            ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if not target.exists():
                print(f"FAILED frame {index}", file=sys.stderr)
                return 1
            frames.append(target.read_bytes())
            print(f"  frame {index}: {beat['name']:12} "
                  f"{len(frames[-1]) / 1024:7.1f} KB")

    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        print("\nPillow is needed to assemble the GIF: pip install pillow",
              file=sys.stderr)
        return 1

    # Quantised to a small palette on purpose. A GIF is indexed colour, so full
    # colour frames would be clipped to 256 anyway -- better to choose the 256
    # rather than let Pillow dither them into noise. Gradients dominate this UI,
    # so the previous build was four times the size for no visible gain.
    images = []
    for raw in frames:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        images.append(img.quantize(colors=128, method=Image.MEDIANCUT,
                                   dither=Image.NONE))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Pillow's `duration` is in MILLISECONDS, and each beat's hold is already in
    # milliseconds, so it is passed straight through. Dividing here -- on the
    # assumption that GIF durations are centiseconds -- played the whole loop ten
    # times too fast, which read as the interface zooming rather than as a
    # walkthrough anyone could follow.
    durations = [int(beat["hold"]) for beat in BEATS]
    images[0].save(out, save_all=True, append_images=images[1:], loop=0,
                   duration=durations, optimize=True, disposal=2)
    total = sum(durations) / 1000
    print(f"\nWrote {out} ({out.stat().st_size / 1024:.1f} KB, "
          f"{len(images)} frames, {total:.1f}s per loop)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
