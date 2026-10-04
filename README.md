# Local Avatar Favourites

A small standalone Windows tool for VRChat that keeps an **unlimited local list of favourite avatars** (with notes, tags, search, and thumbnails) and lets you **switch to them with one click** while in-game.

![uses: OSC](https://img.shields.io/badge/uses-OSC-8a2be2) ![platform: Windows](https://img.shields.io/badge/platform-Windows-blue) ![built: Python](https://img.shields.io/badge/built-Python_3.11-green) ![license: MIT](https://img.shields.io/badge/license-MIT-green)

![Local Avatar Favourites preview](docs/preview.gif)

---

## Screenshots

| Favourites grid | Avatar details |
| --- | --- |
| ![Favourites grid](docs/screenshot-home.png) | ![Avatar details](docs/screenshot-drawer.png) |

| Avatar logs | Settings |
| --- | --- |
| ![Avatar logs](docs/screenshot-logs.png) | ![Settings](docs/screenshot-settings.png) |

---

## Download

Prebuilt Windows executables are published on the
[Releases page](https://github.com/SlushyP1e/Local-Avatar-Favourites/releases/latest).
Grab the latest `LocalAvatarFavourites.exe` and run it — no Python install needed.

---

## Quick start

1. **Launch VRChat** and get into any world.
2. Open the **Action Menu** → **OSC** → **Enabled**. (Toggle it on.)
3. Run `LocalAvatarFavourites.exe`.
4. Switch your avatar once in-game so VRChat tells the tool what you're wearing.
5. Click **+ Add Current** to save it. Click **+ Add by ID** to paste an avatar ID.
6. **Double-click** an avatar in the list (or press **Wear Avatar**) to wear it.

Your favourites are saved locally in:

```
%APPDATA%\LocalAvatarFavourites\favourites.json
```

Thumbnails are cached in `%APPDATA%\LocalAvatarFavourites\thumbs\`.

---

## Important: how avatar switching works (read this!)

The tool talks to VRChat over **OSC** using the `/avatar/change` message.

- To know what you're wearing, VRChat **broadcasts** the current avatar ID whenever an avatar loads.
- To **wear** an avatar, the tool sends that avatar ID back to VRChat.

**VRChat-side rule:** OSC can only switch to avatars that are in your in-game **Favorites**, your **Recents**, or **your own uploads**. This is a VRChat limitation — no tool can bypass it.

Practical advice:

- Add the avatars you want to hot-swap to your in-game favourites too.
- Use this tool for the parts VRChat is bad at: **unlimited** lists, notes, tags, search, and thumbnails.

---

## Optional: VRChat login (for names & thumbnails)

Fetching an avatar's **name** and **thumbnail** uses the VRChat API, which requires being logged in.

1. Open **Settings** in the app.
2. Enter your VRChat **username or email** and **password** (add your **2FA code** if you have 2-step enabled) and click **Log in**. If 2FA is on, the app detects whether you use an authenticator app, an emailed code, or a recovery code and shows the matching field.
3. The auth token is stored only on your PC (in `settings.json`) and is used just for these lookups.

Without login, everything still works — new favourites just show a placeholder thumbnail and can be renamed by hand.

---

## Features

- **Unlimited local favourites** stored as a plain JSON file
- **Add Current** — one-click save of the avatar you're wearing
- **Add by ID** — paste any `avtr_...` ID
- **Wear / switch** via OSC (see limitation above)
- **Live tracking** — the avatar you're currently wearing is highlighted in the list
- **Notes & tags** per avatar
- **Search** across names, notes, tags, and IDs
- **Sort** by name or newest
- **Thumbnails** (with optional login)
- **Copy avatar ID** to clipboard
- **Export / import** your favourites as a JSON file (backup or share)
- **Update check** against GitHub Releases

---

## Development

```bat
rem install dependencies
python -m pip install -r requirements.txt

rem run the self-test (no GUI, no network)
python -m app.selftest

rem run the app from source
python app\main.py

rem build a single-file exe (output: dist\LocalAvatarFavourites.exe)
build.bat
```

### Project layout

```
app/
  main.py       desktop shell (pywebview window)
  backend.py    Python <-> web bridge: OSC, VRChat API, storage, state
  web/          HTML/CSS/JS frontend (Seanime-style responsive dark UI)
    index.html
    style.css
    app.js
  osc.py        OSC sender + receiver (python-osc)
  api.py        optional VRChat API login + metadata/thumbnails
  storage.py    favourites.json, settings.json, thumbnail cache
  selftest.py   headless tests
  version.py    application version (single source of truth)
docs/           README screenshots and preview GIF
build.bat       PyInstaller build script
requirements.txt
```

The UI is web-based: `app/backend.py` is exposed to the frontend as
`window.pywebview.api`, and `app/web/` is plain HTML/CSS/JS with no build step.
Edit the files in `app/web/` and re-run to see changes.

---

## Troubleshooting

- **"OSC: waiting for VRChat..."** — VRChat isn't sending OSC traffic. Make sure OSC is enabled (Action Menu → OSC → Enabled) and that you're inside a world.
- **"Could not listen on UDP port 9001"** — another app is using the port (e.g. a second copy of this tool, or another OSC receiver). Change the receive port in Settings and restart.
- **Wear does nothing** — the avatar must be in your VRChat Favorites / Recents / own uploads (see above).
- **No thumbnails** — log in via Settings; private avatars may still refuse and will show a placeholder.

---

## Disclaimer

This is an unofficial community tool. It is **not** created by or affiliated with VRChat. Use of VRChat's API should comply with VRChat's [Creator Guidelines](https://hello.vrchat.com/creator-guidelines) — keep request rates low and use the tool responsibly.

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.
