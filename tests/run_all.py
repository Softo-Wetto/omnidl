"""Run every OmniDL check, and record what it passed with.

    python tests/run_all.py              Python tests, JavaScript tests, browser checks
    python tests/run_all.py --no-browser   skip the browser checks (no Edge/Chrome needed)
    python tests/run_all.py --exe          also smoke-test packaging/dist/OmniDL/OmniDL.exe

Exits 0 only if everything passes. On success it writes docs/tested-versions.txt: the Python,
package and tool versions these checks just passed against. That matters because OmniDL carries
a fix for one specific uvicorn release (app/compat.py) and depends on yt-dlp/scdl internals —
"it worked" is only meaningful next to "with which versions".
"""
from __future__ import annotations

import importlib.metadata as metadata
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ["fastapi", "starlette", "uvicorn", "websockets", "h11", "yt-dlp", "yt-dlp-ejs", "spotdl",
            "scdl", "ytmusicapi", "mutagen", "pyinstaller"]


def step(name: str, argv: list[str]) -> bool:
    print(f"\n=== {name} ===", flush=True)
    started = time.time()
    code = subprocess.run(argv, cwd=ROOT).returncode
    print(f"--- {name}: {'passed' if code == 0 else f'FAILED (exit {code})'} in {time.time() - started:.0f}s")
    return code == 0


def tool_version(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return "not found"


def record_versions() -> None:
    lines = [f"# Versions the full check suite (tests/run_all.py) last passed with — "
             f"{time.strftime('%Y-%m-%d %H:%M')}",
             f"python      {platform.python_version()} ({platform.system()} {platform.release()})"]
    for name in PACKAGES:
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = "not installed"
        lines.append(f"{name:<11} {version}")
    lines.append(f"node        {tool_version(['node', '--version'])}")
    lines.append(f"ffmpeg      {tool_version(['ffmpeg', '-version'])}")
    (ROOT / "docs" / "tested-versions.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nRecorded docs/tested-versions.txt")


def main() -> int:
    results = [
        step("Python tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests"]),
        step("JavaScript tests", ["node", "--test", *sorted(str(p.relative_to(ROOT))
                                                              for p in (ROOT / "tests" / "js").glob("*.test.cjs"))]),
    ]
    if "--no-browser" not in sys.argv:
        results.append(step("Browser checks", [sys.executable, str(ROOT / "tests" / "browser" / "checks.py")]))
    if "--exe" in sys.argv:
        results.append(step("Standalone exe smoke test", [sys.executable, str(ROOT / "packaging" / "smoke_test.py")]))
    if all(results):
        record_versions()
        print("\nAll checks passed.")
        return 0
    print("\nSome checks FAILED — see above.")
    return 1


if __name__ == "__main__":
    if shutil.which("node") is None:
        print("Node.js is needed for the JavaScript tests.")
    sys.exit(main())
