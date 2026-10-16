# PyInstaller spec for OmniDL — bundles the server + spotdl/yt-dlp/scdl into one app.
# Optional: you don't need this to use OmniDL (start.bat is how you run it). Build with
# build_exe.bat in this folder; the app lands in packaging/dist/OmniDL/.
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.dirname(SPECPATH)        # the omnidl folder (this file lives in packaging/)
sys.path.insert(0, ROOT)

# Packages whose code AND data files we want fully bundled.
_PACKAGES = [
    "spotdl", "yt_dlp", "scdl",
    "uvicorn", "fastapi", "starlette", "pydantic", "pydantic_core",
    "websockets", "anyio", "sniffio", "h11", "click",
    "rich", "mutagen", "ytmusicapi", "rapidfuzz", "requests",
    "spotipy", "pykakasi", "syncedlyrics",
]

datas = [(os.path.join(ROOT, "app", "static"), "static")]
binaries = []
hiddenimports = []

for pkg in _PACKAGES:
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden
    except Exception as exc:  # a missing optional dep shouldn't kill the build
        print(f"[OmniDL.spec] skipping {pkg}: {exc}")

# Our own application package — every module, so a new one can't be left out of the build.
hiddenimports += collect_submodules("app")


a = Analysis(
    [os.path.join(SPECPATH, "omnidl_main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OmniDL",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OmniDL",
)
