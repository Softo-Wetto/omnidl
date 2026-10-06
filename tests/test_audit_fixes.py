"""Gaps found in the October 2026 audit, each pinned so it can't quietly come back."""
import asyncio
import json
import mimetypes
import re
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main, power
from app.jobs import Job, _failure_note

ROOT = Path(__file__).parents[1]


def request(sid="s1", path="/dashboard"):
    return types.SimpleNamespace(state=types.SimpleNamespace(sid=sid, remote=False), cookies={},
                                 headers={}, url=types.SimpleNamespace(path=path))


class SharedSettingsTests(unittest.IsolatedAsyncioTestCase):
    """A home install has one owner: the PC and the Mac must see one set of settings."""

    async def test_local_settings_are_saved_to_config_not_to_the_browser_session(self):
        with patch("app.main.settings_mod.LOCAL_MODE", True), \
             patch("app.main.settings_mod.save_settings") as save, \
             patch("app.main.settings_mod.public_settings", return_value={}):
            await main.post_settings(request("mac"), {"naming_order": "title-artist", "concurrency": 4})
        saved = save.call_args.args[0]
        self.assertEqual("title-artist", saved["naming_order"])
        self.assertEqual(4, saved["concurrency"])
        self.assertNotIn("mac", main.SESSION_PREFS)

    async def test_every_device_reads_the_same_settings_locally(self):
        main.SESSION_PREFS["pc"] = {"naming_order": "title-artist"}
        self.addCleanup(main.SESSION_PREFS.pop, "pc", None)
        with patch("app.main.settings_mod.LOCAL_MODE", True):
            self.assertIsNone(main._prefs(request("pc")))
        with patch("app.main.settings_mod.LOCAL_MODE", False):
            self.assertEqual({"naming_order": "title-artist"}, main._prefs(request("pc")))

    async def test_hosted_visitors_still_get_their_own_settings(self):
        with patch("app.main.settings_mod.LOCAL_MODE", False), \
             patch("app.main.settings_mod.save_settings") as save, \
             patch("app.main.settings_mod.public_settings", return_value={}):
            await main.post_settings(request("visitor"), {"naming_order": "title-artist",
                                                          "output_dir": r"C:\Windows"})
        save.assert_not_called()                       # never touches the server's config
        self.assertEqual("title-artist", main.SESSION_PREFS.pop("visitor")["naming_order"])


class KeepAwakeTests(unittest.TestCase):
    def run_until(self, keep, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_sleep_is_held_off_only_while_needed_and_released_on_exit(self):
        calls, busy = [], {"on": True}
        keep = power.KeepAwake(lambda: busy["on"], interval=0.05, setter=calls.append)
        keep.start()
        self.run_until(keep, lambda: keep.awake)
        self.assertEqual(power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED, calls[0])
        busy["on"] = False
        self.run_until(keep, lambda: not keep.awake)
        self.assertEqual(power.ES_CONTINUOUS, calls[-1])
        busy["on"] = True
        self.run_until(keep, lambda: keep.awake)
        keep.stop()
        self.assertEqual(power.ES_CONTINUOUS, calls[-1])    # never left holding the PC up

    def test_a_broken_check_lets_the_pc_sleep(self):
        calls = []
        keep = power.KeepAwake(lambda: 1 / 0, interval=0.05, setter=calls.append)
        keep.start()
        time.sleep(0.15)
        keep.stop()
        self.assertFalse(keep.awake)
        self.assertNotIn(power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED, calls)


class ReviewAndFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_review_report_opens_from_any_device_and_cannot_call_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "omnidl-review.html"
            report.write_text("<html>report</html>", encoding="utf-8")
            job = Job("x", "youtube", None, "")
            job.report_path, job.review_count = str(report), 3
            self.assertEqual((True, 3), (job.to_dict()["has_report"], job.to_dict()["review_count"]))
            with patch("app.main._owned", return_value=job):
                resp = await main.job_report(request(), job.id)
            self.assertEqual(200, resp.status_code)
            csp = resp.headers["content-security-policy"]
            self.assertIn("connect-src 'none'", csp)
            self.assertIn("form-action 'none'", csp)

    async def test_no_report_is_a_clean_404(self):
        with patch("app.main._owned", return_value=Job("x", "youtube", None, "")):
            self.assertEqual(404, (await main.job_report(request(), "x")).status_code)

    def test_review_details_survive_a_restart(self):
        job = Job("x", "youtube", None, "")
        job.report_path, job.review_count, job.note = r"C:\r.html", 2, "video unavailable"
        restored = Job.from_record(json.loads(json.dumps({**job.to_record(), "review_count": 2,
                                                          "note": "video unavailable"})))
        self.assertEqual((r"C:\r.html", 2, "video unavailable"),
                         (restored.report_path, restored.review_count, restored.note))

    def test_a_failed_job_says_why(self):
        log = ("\x1b[1;36m$ yt-dlp ...\x1b[0m\r\nERROR: [youtube] abc: Private video. Sign in\r\n"
               "\x1b[31m[error] exit=1\x1b[0m\r\n")
        self.assertEqual("private video", _failure_note(log))
        self.assertEqual("[error] executable not found: scdl",
                         _failure_note("\x1b[31m[error] executable not found: scdl\x1b[0m\r\n"))
        self.assertEqual("", _failure_note(""))

    def test_a_typed_song_names_where_it_searched_not_the_tool(self):
        job = Job("Drake - One Dance", "youtube", None, "", kind="search",
                  settings={"bulk_service": "soundcloud"})
        self.assertEqual(("Song search", "SoundCloud"),
                         (job.to_dict()["engine_label"], job.to_dict()["tool"]))


class PagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_wrong_address_gets_a_page_and_a_wrong_api_call_gets_json(self):
        from starlette.exceptions import HTTPException
        page = await main.http_error(request(path="/dashbaord"), HTTPException(404))
        api = await main.http_error(request(path="/api/nope"), HTTPException(404))
        self.assertIn(b"nothing at this address", page.body)
        self.assertEqual({"detail": "Not Found"}, json.loads(api.body))

    def test_the_app_can_be_installed(self):
        manifest = json.loads((ROOT / "app/static/manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual("/dashboard", manifest["start_url"])
        for icon in manifest["icons"]:
            self.assertTrue((ROOT / icon["src"].lstrip("/").replace("static/", "app/static/", 1)).is_file(),
                            icon["src"])
        self.assertEqual("application/manifest+json", mimetypes.guess_type("x.webmanifest")[0])
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        # Through the tunnel every request needs the Access cookie; manifests omit cookies by default.
        self.assertIn('rel="manifest" href="/static/manifest.webmanifest" crossorigin="use-credentials"', html)
        self.assertIn('rel="apple-touch-icon"', html)

    def test_home_page_stats_show_real_numbers_without_javascript(self):
        html = (ROOT / "app/static/home.html").read_text(encoding="utf-8")
        values = re.findall(r'class="num" data-target="(\d+)"[^>]*>([^<]*)<', html)
        self.assertTrue(values)
        for target, shown in values:
            self.assertTrue(shown.startswith(target), (target, shown))


class HardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_response_refuses_to_be_framed(self):
        """Clickjacking: any site could load the dashboard invisibly and steer your clicks."""
        async def call_next(_req):
            from fastapi.responses import HTMLResponse
            return HTMLResponse("ok")

        req = types.SimpleNamespace(headers={"host": "127.0.0.1:8000"}, cookies={}, method="GET",
                                    url=types.SimpleNamespace(path="/dashboard"),
                                    state=types.SimpleNamespace())
        with patch("app.main.settings_mod.LOCAL_MODE", True):
            resp = await main.remote_access_guard(req, call_next)
        self.assertEqual("DENY", resp.headers["x-frame-options"])
        self.assertEqual("nosniff", resp.headers["x-content-type-options"])
        self.assertEqual("same-origin", resp.headers["referrer-policy"])

    def test_old_temp_files_are_swept_but_fresh_ones_kept(self):
        import os
        with tempfile.TemporaryDirectory() as tmp, patch("app.main.tempfile.gettempdir", return_value=tmp):
            produced = Path(tmp) / "omnidl-produced"
            produced.mkdir()
            old_zip, new_zip = Path(tmp) / "omnidl_zip_old.zip", Path(tmp) / "omnidl_zip_new.zip"
            old_txt, other = produced / "job.txt", Path(tmp) / "someone-elses.zip"
            for f in (old_zip, new_zip, old_txt, other):
                f.write_bytes(b"x")
            for f in (old_zip, old_txt, other):
                os.utime(f, (time.time() - 3 * 86400,) * 2)
            main._sweep_temp()
            self.assertFalse(old_zip.exists())
            self.assertFalse(old_txt.exists())
            self.assertTrue(new_zip.exists())           # may still be downloading
            self.assertTrue(other.exists())             # not ours


class UpdateYtdlpTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, local=True, frozen=False, busy=False, returncode=0):
        jobs = {"j": types.SimpleNamespace(status="running")} if busy else {}
        result = types.SimpleNamespace(returncode=returncode, stdout="", stderr="ERROR: no network")
        versions = iter(["2026.09.27", "2026.10.05"])
        with patch("app.main.settings_mod.LOCAL_MODE", local), \
             patch("app.main.engines.FROZEN", frozen), \
             patch("app.main.manager.jobs", jobs), \
             patch("app.main.engines.ytdlp_version", side_effect=lambda: next(versions)), \
             patch("app.main.engines.ytdlp_age_days", return_value=1), \
             patch("app.main.subprocess.run", return_value=result) as run:
            return await main.update_ytdlp(), run

    async def test_updates_and_reports_the_new_version(self):
        resp, run = await self.call()
        self.assertEqual({"before": "2026.09.27", "after": "2026.10.05", "changed": True, "age_days": 1}, resp)
        self.assertIn("yt-dlp", run.call_args.args[0])

    async def test_refused_where_it_cant_or_shouldnt_run(self):
        for kwargs, code in (({"local": False}, 403), ({"frozen": True}, 400), ({"busy": True}, 409)):
            with self.subTest(**kwargs):
                resp, run = await self.call(**kwargs)
                self.assertEqual(code, resp.status_code)
                run.assert_not_called()

    async def test_a_failed_update_says_why(self):
        resp, _ = await self.call(returncode=1)
        self.assertEqual(500, resp.status_code)
        self.assertIn("no network", json.loads(resp.body)["error"])


class CloudflaredAgeTests(unittest.TestCase):
    def test_age_comes_from_the_build_date(self):
        from datetime import date, timedelta
        from app.tunnel import cloudflared_age_days
        built = (date.today() - timedelta(days=200)).isoformat()
        self.assertEqual(200, cloudflared_age_days(f"cloudflared version 2026.1.0 (built {built}T08:31 UTC)"))
        self.assertIsNone(cloudflared_age_days("cloudflared version 2026.1.0"))


class LiveOutputSymbolsTests(unittest.TestCase):
    def test_status_lines_never_show_mangled_question_marks(self):
        """Every ✓/✗/⏭/🔎 in the live output had been mangled into "?" in the source."""
        source = (ROOT / "app/jobs.py").read_text(encoding="utf-8")
        self.assertEqual([], re.findall(r'x1b\[[0-9;]*m\s*\?{1,2} ', source))


if __name__ == "__main__":
    unittest.main()
