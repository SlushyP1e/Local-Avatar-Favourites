# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the single-file Windows executable.

The previous version called ``collect_all('webview')``, which walks every
submodule and dependency of pywebview. That pulled in all of its platform
backends (gtk, qt, cocoa, cef, android, mshtml) along with numpy, pygments,
setuptools and bottle -- none of which can ever be used on Windows. pywebview
already ships its own ``hook-webview`` hook, so the collection was both
redundant and responsible for most of the 38 MB output.

pythonnet / clr_loader / cryptography / cffi are deliberately NOT excluded:
webview.platforms.winforms genuinely needs them.

bottle is not excluded either: webview/__init__.py imports webview.http, which
imports bottle. Excluding it produced an exe that failed at startup, which is
what the --selftest flag below exists to catch.
"""

# Only the Windows backends are reachable.
hiddenimports = [
    'webview.platforms.winforms',
    'webview.platforms.edgechromium',
]

excludes = [
    # Bundled by the old collect_all, unreachable on Windows.
    'numpy',
    'pygments',
    'setuptools',
    'distutils',
    'pytest',
    'IPython',
    'pyreadline3',
    # Non-Windows webview backends, plus the GUI toolkits they need.
    'webview.platforms.gtk',
    'webview.platforms.qt',
    'webview.platforms.cocoa',
    'webview.platforms.cef',
    'webview.platforms.android',
    'webview.platforms.mshtml',
    'gi',
    'objc',
    'AppKit',
    'Foundation',
    'WebKit',
    'cefpython3',
    'PyQt5',
    'PyQt6',
    'PySide2',
    'PySide6',
    'qtpy',
    'tkinter',
]

a = Analysis(
    ['app\\main.py'],
    pathex=[],
    binaries=[],
    # app/web is the UI; assets carries icon.ico, which the tray icon loads at
    # runtime. Without it in the bundle the notification-area icon renders blank
    # because LoadImage finds no file.
    datas=[('app\\web', 'web'), ('assets', 'assets')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LocalAvatarFavourites',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPEX compression is a well-known source of antivirus false positives on
    # freshly built unsigned executables. Measured cost is a few MB.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
)