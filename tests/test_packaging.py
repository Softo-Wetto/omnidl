"""The launcher and the optional exe build report failures honestly.

Runs copies of start.bat / build_exe.bat against stand-in `pip` and `PyInstaller` modules in a
temporary folder (python -m finds them first) — nothing is installed or built.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows batch files")
class BuildScriptTests(unittest.TestCase):
    def run_build(self, pip_code=0, build_code=0, make_exe=True, user_data=False):
        tmp = Path(tempfile.mkdtemp(prefix="omnidl-build-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pack = tmp / "packaging"
        pack.mkdir()
        shutil.copyfile(REPO / "packaging" / "build_exe.bat", pack / "build_exe.bat")
        (pack / "pip.py").write_text(f"print('STUB pip'); raise SystemExit({pip_code})\n")
        exe_line = ("import pathlib; p = pathlib.Path('dist/OmniDL'); p.mkdir(parents=True, exist_ok=True); "
                    "(p / 'OmniDL.exe').write_bytes(b'MZ')\n") if make_exe else ""
        (pack / "PyInstaller.py").write_text(
            "import shutil; shutil.rmtree('dist/OmniDL', ignore_errors=True)\n"   # as PyInstaller does
            + exe_line + f"print('STUB build'); raise SystemExit({build_code})\n")
        if user_data:
            out = pack / "dist" / "OmniDL"
            (out / "downloads").mkdir(parents=True)
            (out / "config.json").write_text('{"output_dir": "x"}')
            (out / "downloads" / "song.opus").write_bytes(b"x")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
        env["OMNIDL_NO_PAUSE"] = "1"
        result = subprocess.run(["cmd.exe", "/d", "/c", str(pack / "build_exe.bat")], cwd=pack, env=env,
                                capture_output=True, text=True, timeout=60)
        return result, pack

    def test_a_failed_install_stops_before_building(self):
        result, _ = self.run_build(pip_code=7)
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("STUB build", result.stdout)
        self.assertNotIn("Done.", result.stdout)

    def test_a_failed_build_is_reported_and_user_data_is_put_back(self):
        result, pack = self.run_build(build_code=23, make_exe=False, user_data=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("did NOT complete", result.stdout)
        self.assertTrue((pack / "dist" / "OmniDL" / "config.json").exists())
        self.assertTrue((pack / "dist" / "OmniDL" / "downloads" / "song.opus").exists())

    def test_a_build_that_leaves_no_exe_is_a_failure(self):
        result, _ = self.run_build(make_exe=False)
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Done.", result.stdout)

    def test_a_good_build_says_done_and_keeps_user_data(self):
        result, pack = self.run_build(user_data=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("Done.", result.stdout)
        self.assertTrue((pack / "dist" / "OmniDL" / "OmniDL.exe").exists())
        self.assertTrue((pack / "dist" / "OmniDL" / "config.json").exists())


@unittest.skipUnless(sys.platform == "win32", "Windows batch files")
class LauncherTests(unittest.TestCase):
    def test_start_bat_passes_its_options_to_run_py(self):
        tmp = Path(tempfile.mkdtemp(prefix="omnidl-launch-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copyfile(REPO / "start.bat", tmp / "start.bat")
        (tmp / "run.py").write_text("import json, sys\nprint('ARGS=' + json.dumps(sys.argv[1:]))\n")
        result = subprocess.run(["cmd.exe", "/d", "/c", str(tmp / "start.bat"), "--restart", "--no-browser"],
                                cwd=tmp, capture_output=True, text=True, input="", timeout=30)
        self.assertEqual(0, result.returncode)
        self.assertIn('ARGS=["--restart", "--no-browser"]', result.stdout)
