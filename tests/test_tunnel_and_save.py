"""Running cloudflared alongside OmniDL, and handing a home install's files to another device."""
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app import engines, main
from app.jobs import Job, JobManager, _audio_snapshot, _collect_produced
from app.matching import Candidate
from app.spotify_resolver import Track
from app.tunnel import Tunnel, close_job, kill_with_this_process, start_from_config


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


@unittest.skipUnless(sys.platform == "win32", "Windows job objects")
class KillWithParentTests(unittest.TestCase):
    def test_the_child_dies_when_the_job_handle_closes(self):
        """Closing the handle is what happens when OmniDL dies (crash, console closed) — the OS
        then kills cloudflared, so no tunnel outlives the app."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            job = kill_with_this_process(proc)
            self.assertIsNotNone(job)
            self.assertTrue(_alive(proc))
            close_job(job)
            proc.wait(timeout=10)
            self.assertFalse(_alive(proc))
        finally:
            if _alive(proc):
                proc.kill()


class TunnelTests(unittest.TestCase):
    def test_connects_reports_and_stops_cleanly(self):
        script = ("import sys, time\n"
                  "print('INF Registered tunnel connection connIndex=0', flush=True)\n"
                  "time.sleep(60)\n")
        with tempfile.TemporaryDirectory() as tmp, \
             patch("app.tunnel.LOG_PATH", Path(tmp) / "cloudflared.log"):
            tunnel = Tunnel("cloudflared", "cfg.yml", "omnidl-home.example.com",
                            argv=[sys.executable, "-c", script])
            tunnel.start()
            deadline = time.monotonic() + 15
            while not tunnel.connected and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(tunnel.connected)
            proc = tunnel._proc
            tunnel.stop()
            self.assertFalse(_alive(proc))
            self.assertIn("Registered tunnel connection", (Path(tmp) / "cloudflared.log").read_text())

    def test_nothing_starts_without_a_config(self):
        self.assertIsNone(start_from_config({}))
        self.assertIsNone(start_from_config({"tunnel_config": r"Z:\does\not\exist.yml"}))


def local_job(**kw):
    job = Job("x", "youtube", None, "", **kw)
    job.out_dir = None          # local mode: files go straight into the library
    return job


class LocalSaveTests(unittest.TestCase):
    def test_a_local_job_lists_exactly_the_files_it_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "A - Song.opus", Path(tmp) / "B - Song.opus"
            a.touch(); b.touch()
            (Path(tmp) / "Someone Else - Old.opus").touch()       # already in the library
            job = local_job()
            job.produced = [str(a), str(b), str(a), str(Path(tmp) / "deleted.opus")]
            self.assertEqual([a, b], job.list_files())           # deduped, missing ones dropped

    def test_has_files_doesnt_stat_the_library_on_every_snapshot(self):
        job = local_job()
        job.status = "done"
        job.produced = [r"Z:\not\there.opus"]
        self.assertTrue(job.has_files())

    def test_what_a_job_made_survives_a_restart(self):
        job = local_job()
        job.produced = [r"C:\Music\A - Song.opus"]
        restored = Job.from_record(json.loads(json.dumps(job.to_record())))
        self.assertEqual([r"C:\Music\A - Song.opus"], restored.produced)

    def test_deleting_a_local_job_never_touches_the_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = Path(tmp) / "A - Song.opus"
            song.touch()
            job = local_job()
            job.produced = [str(song)]
            JobManager._remove_files(job)
            self.assertTrue(song.exists())


class ReportingTests(unittest.TestCase):
    def test_ytdlp_is_asked_to_report_what_it_saved(self):
        s = {"audio_format": "opus", "output_dir": ".", "_produced_file": r"C:\tmp\job.txt"}
        cmd = engines.build_command("youtube", "https://youtu.be/abc", s)
        i = cmd.index("--print-to-file")
        self.assertEqual(["after_move:filepath", r"C:\tmp\job.txt"], cmd[i + 1:i + 3])
        self.assertNotIn("--print-to-file", engines.build_command("youtube", "https://youtu.be/abc",
                                                                  {"audio_format": "opus", "output_dir": "."}))

    def test_a_ytdlp_report_is_read_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "job.txt"
            report.write_text("C:\\Music\\A.opus\nC:\\Music\\B.opus\n\n", encoding="utf-8")
            job = local_job(settings={"_produced_file": str(report)})
            self.assertEqual([r"C:\Music\A.opus", r"C:\Music\B.opus"], _collect_produced(job, None))
            self.assertFalse(report.exists())

    def test_tools_that_cant_report_are_tracked_by_what_appeared(self):
        """scdl and spotdl can't say what they saved, so new/rewritten files are picked up."""
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "Old - Song.mp3"
            old.touch()
            job = local_job(settings={"output_dir": tmp})
            before = _audio_snapshot(job)
            (Path(tmp) / "playlist").mkdir()
            new = Path(tmp) / "playlist" / "New - Song.mp3"
            new.touch()
            (Path(tmp) / "cover.jpg").touch()                    # not media
            self.assertEqual([str(new)], _collect_produced(job, before))

    def test_a_local_link_job_is_set_up_to_report(self):
        import asyncio
        manager = JobManager()
        manager._persist = lambda: None
        with tempfile.TemporaryDirectory() as tmp, patch("app.jobs.settings_mod.LOCAL_MODE", True):
            job = asyncio.run(manager.submit("https://www.youtube.com/watch?v=abc",
                                             {"output_dir": tmp, "audio_format": "opus"}, "s"))
        self.assertIn("--print-to-file", job.argv)


class TrackListSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_matched_track_is_recorded_as_produced(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager()
            job = local_job(settings={"output_dir": tmp, "skip_existing": False, "audio_format": "opus"})
            song = Candidate("youtube_music", "https://yt/1", "Midnight Run", "Nova", 180, True)
            target = Path(tmp) / "Nova - Midnight Run.opus"

            async def download(_job, _argv, emit, **_kwargs):
                target.touch()
                return 0

            with patch("app.jobs.candidate_search.search_all", return_value=[song]), \
                 patch.object(manager, "_stream_subprocess", side_effect=download):
                await manager._fetch_track(job, Track("Nova", "Midnight Run", 180), job.settings, True, 1, 1)

        self.assertEqual([str(target)], job.produced)


class MetaTests(unittest.IsolatedAsyncioTestCase):
    async def test_meta_says_when_you_are_using_it_remotely(self):
        req = types.SimpleNamespace(state=types.SimpleNamespace(remote=True, sid="s"),
                                    cookies={}, headers={})
        with patch("app.main.engines.ytdlp_version", return_value=""), \
             patch("app.main.engines.ytdlp_age_days", return_value=None):
            self.assertTrue((await main.meta(req))["remote"])
            req.state.remote = False
            self.assertFalse((await main.meta(req))["remote"])


if __name__ == "__main__":
    unittest.main()
