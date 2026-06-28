# PyInstaller spec for OmniDL — bundles the server + spotdl/yt-dlp/scdl into one app.
# Build with:  pyinstaller OmniDL.spec   (output in dist/OmniDL/)
from PyInstaller.utils.hooks import collect_all

# Packages whose code AND data files we want fully bundled.
_PACKAGES = [
    "spotdl", "yt_dlp", "scdl",
    "uvicorn", "fastapi", "starlette", "pydantic", "pydantic_core",
    "websockets", "anyio", "sniffio", "h11", "click",
    "rich", "mutagen", "ytmusicapi", "rapidfuzz", "requests",
    "spotipy", "pykakasi", "syncedlyrics",
]

datas = [("app/static", "static")]
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

# Our own application package.
hiddenimports += [
    "app", "app.main", "app.jobs", "app.engines", "app.settings",
    "app.spotify_resolver", "app.ytmusic_match", "app.tagging",
    "app.candidate_search", "app.matching", "app.review_report",
]


a = Analysis(
    ["omnidl_main.py"],
    pathex=[],
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
