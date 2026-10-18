"""The Codex audit of 15 October 2026 (docs/audits/2026-10-15): each reproduced defect, pinned.

Everything here runs on temporary folders and mocks — no real download, process kill, tag
write to a real library, or network call.
"""
import asyncio
import io
import json
import shutil
import subprocess
import tempfile
import time
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import run
from app import engines, instance, library, main
from app import settings as settings_mod
from app.jobs import Job, JobManager
from app.library import LibraryIndex, LibraryTrack, find_duplicate_groups
from app.matching import Candidate
from app.review_report import TrackOutcome
from app.spotify_resolver import Track
from support import make_audio, write_output


def request(sid="s1"):
    return types.SimpleNamespace(state=types.SimpleNamespace(sid=sid, remote=False), cookies={},
                                 headers={}, client=types.SimpleNamespace(host="203.0.113.9"))


# ---- D01: launching never kills a program it can't identify ---------------------------------

class LauncherOwnershipTests(unittest.TestCase):
    def test_decisions(self):
        mine = {"app": "omnidl", "build": "b1", "active": 0}
        self.assertEqual("start", instance.decide(False, None, "b1"))
        self.assertEqual("occupied", instance.decide(True, None, "b1"))
        self.assertEqual("reuse", instance.decide(True, mine, "b1"))
        self.assertEqual("restart", instance.decide(True, mine, "b2"))
        self.assertEqual("reuse-busy", instance.decide(True, {**mine, "active": 2}, "b2"))
        self.assertEqual("restart", instance.decide(True, {**mine, "active": 2}, "b2", force_restart=True))

    def test_an_unknown_listener_is_reported_and_never_terminated(self):
        with patch.object(run.instance, "port_open", return_value=True), \
             patch.object(run.instance, "probe", return_value=None), \
             patch.object(run, "_listening_pids_on_port", return_value={424242}), \
             patch.object(run, "_terminate_process_tree") as kill:
            with self.assertRaises(SystemExit) as stop:
                run._prepare_port()
        kill.assert_not_called()
        self.assertIn("another program", str(stop.exception))

    def test_a_restart_never_forces_a_process_that_does_not_hold_the_port(self):
        with patch.object(run.instance, "request_shutdown", return_value=False), \
             patch.object(run, "_listening_pids_on_port", return_value={424242}), \
             patch.object(run, "_terminate_process_tree") as kill:
            with self.assertRaises(SystemExit):
                run._stop_instance({"app": "omnidl", "pid": 999})
        kill.assert_not_called()

    def test_a_busy_omnidl_is_reused_rather_than_cut_off(self):
        busy = {"app": "omnidl", "build": "old", "active": 3, "pid": 5}
        with patch.object(run.instance, "port_open", return_value=True), \
             patch.object(run.instance, "probe", return_value=busy), \
             patch.object(run, "_stop_instance") as stop:
            self.assertEqual("reuse-busy", run._prepare_port())
        stop.assert_not_called()


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    """An open dashboard tab used to hang shutdown forever, so a relaunch couldn't restart."""

    async def test_shutdown_closes_open_tabs_as_service_restart_and_arms_a_backstop(self):
        tab = AsyncMock()
        manager = MagicMock()
        manager.jobs = {}
        manager.subscribers = {tab: "s1"}
        local = types.SimpleNamespace(state=types.SimpleNamespace(sid="s1"), cookies={}, headers={},
                                      client=types.SimpleNamespace(host="127.0.0.1"))
        with patch("app.main.manager", manager), patch("app.main.settings_mod.LOCAL_MODE", True),              patch("app.main.signal.raise_signal") as raised,              patch("app.main.threading.Timer") as timer:
            reply = await main.instance_shutdown(local)
            await asyncio.sleep(0.6)
        self.assertEqual({"ok": True}, reply)
        tab.close.assert_awaited_once_with(code=1012)
        self.assertEqual({}, manager.subscribers)
        self.assertTrue(raised.called)
        timer.return_value.start.assert_called_once()
        self.assertTrue(timer.return_value.daemon)       # never keeps the process alive

    async def test_uvicorn_asks_connections_to_close_before_waiting_for_them(self):
        import uvicorn.server as server_module
        from app import compat
        compat.fix_uvicorn_shutdown()
        order = []

        class Listener:
            def close(self):
                order.append("stop listening")

            async def wait_closed(self):
                order.append("wait for connections")

        connection = MagicMock()
        connection.shutdown.side_effect = lambda: order.append("ask to close")
        server = types.SimpleNamespace(servers=[Listener()], force_exit=True,
                                       server_state=types.SimpleNamespace(connections={connection}, tasks=set()),
                                       config=types.SimpleNamespace(timeout_graceful_shutdown=None))
        server._wait_tasks_to_complete = AsyncMock()
        with patch("app.main.asyncio.sleep", AsyncMock()):
            await server_module.Server.shutdown(server)
        self.assertLess(order.index("ask to close"), order.index("wait for connections"))


# ---- D02: library repair reads names one explicit way and never guesses -----------------------

def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 800)


class LibraryNamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        src = Path(cls._tmp.name) / "src.wav"
        _wav(src)
        cls.template = Path(cls._tmp.name) / "template.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), str(cls.template)],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def mp3(self, name, artist="", title=""):
        from mutagen.id3 import ID3, TIT2, TPE1
        path = self.root / name
        shutil.copy(self.template, path)
        tags = ID3()
        if artist:
            tags.add(TPE1(encoding=3, text=[artist]))
        if title:
            tags.add(TIT2(encoding=3, text=[title]))
        tags.save(path)
        return path

    def tags(self, path):
        from mutagen.id3 import ID3
        saved = ID3(path)
        artist = saved.getall("TPE1")
        title = saved.getall("TIT2")
        return (artist[0].text[0] if artist else ""), (title[0].text[0] if title else "")

    def test_title_artist_names_are_written_the_right_way_round(self):
        """Repair wrote artist=Song, title=Artist for a 'Song - Artist' file."""
        path = self.mp3("Midnight Run - Nova.mp3")
        preview = library.propose_tags(self.root, path.name, "title-artist")
        self.assertEqual({"artist": "Nova", "title": "Midnight Run"}, preview["proposed"])
        library.repair_missing_tags(self.root, path.name, order="title-artist")
        self.assertEqual(("Nova", "Midnight Run"), self.tags(path))

    def test_artist_title_names_still_work(self):
        path = self.mp3("Nova - Midnight Run.mp3")
        library.repair_missing_tags(self.root, path.name, order="artist-title")
        self.assertEqual(("Nova", "Midnight Run"), self.tags(path))

    def test_several_separators_are_refused_not_split_at_the_last_one(self):
        """'Nova - Midnight Run - Live' became artist='Nova - Midnight Run', title='Live'."""
        path = self.mp3("Nova - Midnight Run - Live.mp3")
        preview = library.propose_tags(self.root, path.name, "artist-title")
        self.assertEqual({}, preview["proposed"])
        self.assertTrue(preview["problem"])
        with self.assertRaisesRegex(ValueError, "Artist - Title"):
            library.repair_missing_tags(self.root, path.name)
        self.assertEqual(("", ""), self.tags(path))
        # ...but values you confirm are written as given.
        library.repair_missing_tags(self.root, path.name, artist="Nova", title="Midnight Run - Live")
        self.assertEqual(("Nova", "Midnight Run - Live"), self.tags(path))

    def test_an_existing_tag_that_matches_one_half_settles_the_order(self):
        path = self.mp3("Midnight Run - Nova.mp3", artist="Nova")
        preview = library.propose_tags(self.root, path.name, "artist-title")
        self.assertEqual({"title": "Midnight Run"}, preview["proposed"])

    def test_existing_values_are_never_overwritten(self):
        path = self.mp3("Nova - Midnight Run.mp3", title="Real Title")
        result = library.repair_missing_tags(self.root, path.name, artist="Nova", title="Other")
        self.assertEqual(["artist"], result["changed"])
        self.assertEqual(("Nova", "Real Title"), self.tags(path))

    def test_preview_writes_nothing(self):
        path = self.mp3("Nova - Midnight Run.mp3")
        before = path.read_bytes()
        library.propose_tags(self.root, path.name, "artist-title")
        self.assertEqual(before, path.read_bytes())

    def _untagged(self, name, order):
        return LibraryTrack(path=Path(name), relative_path=name, title="", artist="", album="",
                            duration=180, bitrate=0, sample_rate=0, channels=0, codec="opus",
                            size=1, has_artwork=False, issues=("missing_artist", "missing_title"),
                            order=order)

    def test_display_and_duplicates_read_names_the_same_way_as_repair(self):
        untagged = self._untagged("Midnight Run - Nova.opus", "title-artist")
        self.assertEqual(("Nova", "Midnight Run"),
                         (untagged.to_dict()["display_artist"], untagged.to_dict()["display_title"]))
        tagged = LibraryTrack(path=Path("x.mp3"), relative_path="x.mp3", title="Midnight Run",
                              artist="Nova", album="", duration=181, bitrate=0, sample_rate=0,
                              channels=0, codec="mp3", size=1, has_artwork=True, issues=())
        self.assertEqual(1, len(find_duplicate_groups([untagged, tagged])))
        ambiguous = self._untagged("Nova - Midnight Run - Live.opus", "artist-title")
        self.assertEqual("Unknown artist", ambiguous.to_dict()["display_artist"])
        self.assertIsNone(ambiguous.identity)

    def test_skip_existing_recognises_an_untagged_file_named_in_the_other_order(self):
        index = LibraryIndex([self._untagged("Midnight Run - Nova.opus", "artist-title")])
        self.assertIsNotNone(index.find("Nova", "Midnight Run", 180))

    def test_a_missing_or_file_library_root_is_an_error_not_a_clean_library(self):
        with self.assertRaises(library.LibraryRootError):
            library.scan_library(self.root / "nope")
        file_root = self.root / "file.txt"
        file_root.write_text("x")
        with self.assertRaises(library.LibraryRootError):
            library.scan_library(file_root)
        self.assertEqual(0, library.scan_library(self.root)["summary"]["total_files"])


# ---- D03: "SoundCloud only" never touches YouTube --------------------------------------------

def track_settings(temp_dir, **extra):
    return {"output_dir": temp_dir, "skip_existing": False, "audio_format": "opus",
            "cookie_file": r"C:\secret\youtube-cookies.txt", **extra}


class SoundCloudOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = JobManager()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def fetch(self, track, **extra):
        job = Job("x", "youtube", None, "", settings=track_settings(self.tmp.name, **extra))
        argvs = []

        async def download(_job, argv, emit, **kwargs):
            argvs.append(argv)
            write_output(argv)
            return 0

        with patch("app.jobs.candidate_search.resolve_phrase") as resolve, \
             patch("app.jobs.candidate_search.search_all", return_value=self.results) as search, \
             patch.object(self.manager, "_stream_subprocess", side_effect=download):
            result = await self.manager._fetch_track(job, track, job.settings, True, 1, 1)
        return result, argvs, resolve, search

    async def test_a_typed_phrase_is_not_identified_on_youtube_music_or_searched_on_youtube(self):
        self.results = []
        result, argvs, resolve, search = await self.fetch(Track("", "unidentifiable phrase"),
                                                          bulk_service="soundcloud")
        resolve.assert_not_called()
        search.assert_not_called()
        self.assertEqual([], argvs)                      # no ytsearch1: fallback, no cookies
        self.assertEqual("no_candidate", result.status)
        self.assertIn("SoundCloud", result.reason)

    async def test_a_row_downloads_only_from_soundcloud_without_the_youtube_login(self):
        yt = Candidate("youtube_music", "https://music.youtube.com/watch?v=x", "Midnight Run", "Nova", 180, True)
        sc = Candidate("soundcloud", "https://soundcloud.com/nova/midnight-run", "Midnight Run", "Nova", 180)
        self.results = [yt, sc]                          # even if the search layer leaked one
        result, argvs, _resolve, search = await self.fetch(Track("Nova", "Midnight Run", 180),
                                                           bulk_service="soundcloud")
        search.assert_called_once_with("Nova", "Midnight Run", "soundcloud", deep=False)
        self.assertEqual("downloaded", result.status)
        self.assertEqual(sc.url, argvs[0][-1])
        self.assertNotIn("--cookies", argvs[0])

    async def test_a_locked_visitors_job_stays_on_soundcloud_whatever_it_asks_for(self):
        self.results = []
        _result, _argvs, resolve, search = await self.fetch(Track("Nova", "Midnight Run", 180),
                                                            bulk_service="auto", _youtube_ok=False)
        resolve.assert_not_called()
        self.assertTrue(search.call_args_list)          # first search, then the second wording
        for call in search.call_args_list:
            self.assertEqual(("Nova", "Midnight Run", "soundcloud"), call.args)

    def test_youtube_cookies_and_tokens_only_go_to_youtube(self):
        s = track_settings(self.tmp.name)
        with patch.object(engines._settings, "POT_PROVIDER_URL", "http://pot.test"):
            sc = engines.media_url_command("https://soundcloud.com/a/b", "A - B", s)
            yt = engines.media_url_command("https://www.youtube.com/watch?v=x", "A - B", s)
        self.assertNotIn("--cookies", sc)
        self.assertFalse(any("youtubepot" in arg for arg in sc))
        self.assertIn("--cookies", yt)
        self.assertTrue(any("youtubepot" in arg for arg in yt))

    async def test_the_access_decision_travels_with_hosted_jobs(self):
        manager = MagicMock()
        manager.check_limit.return_value = None
        manager.submit_bulk = AsyncMock(return_value=MagicMock(to_dict=lambda: {"id": "bulk"}))
        manager.submit = AsyncMock(return_value=MagicMock(to_dict=lambda: {"id": "one"}))
        entries = [{"kind": "search", "artist": "", "title": "unidentifiable phrase"}]
        with patch("app.main.manager", manager), \
             patch("app.main.settings_mod.effective_settings", return_value={"audio_format": "opus"}), \
             patch("app.main.settings_mod.gate_enabled", return_value=True):
            await main.bulk_download(request(), {"entries": entries, "service": "soundcloud"})
            await main.download(request(), {"input": "unidentifiable phrase", "engine_override": "soundcloud"})
        self.assertFalse(manager.submit_bulk.await_args.args[1]["_youtube_ok"])
        self.assertFalse(manager.submit.await_args.args[1]["_youtube_ok"])


# ---- D08: Save offers every finished file, including ones that were already there -------------

class DeliverableFilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_track_that_already_existed_is_part_of_the_jobs_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = make_audio(Path(tmp) / "Nova - Midnight Run.opus")
            job = Job("x", "youtube", None, "", settings={"output_dir": tmp, "audio_format": "opus"})
            result = await JobManager()._fetch_track(job, Track("Nova", "Midnight Run"), job.settings, True, 1, 1)
            job.status = "done"
            self.assertEqual("skipped", result.status)
            self.assertEqual([target], job.list_files())
            self.assertEqual((True, 1), (job.to_dict()["has_files"], job.to_dict()["file_count"]))

    async def test_a_library_match_in_another_format_is_delivered_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "old" / "Nova - Midnight Run.mp3"
            existing.parent.mkdir()
            existing.write_bytes(b"\x00" * 64)
            item = LibraryTrack(path=existing, relative_path="old/Nova - Midnight Run.mp3",
                                title="Midnight Run", artist="Nova", album="", duration=180,
                                bitrate=0, sample_rate=0, channels=0, codec="mp3", size=1,
                                has_artwork=True, issues=())
            job = Job("x", "youtube", None, "", settings={
                "output_dir": tmp, "audio_format": "opus", "_library_index": LibraryIndex([item])})
            result = await JobManager()._fetch_track(job, Track("Nova", "Midnight Run", 181), job.settings, True, 1, 1)
            self.assertEqual("skipped", result.status)
            self.assertEqual([existing], job.list_files())
            job.status = "done"
            self.assertEqual(["mp3"], job.to_dict()["formats"])

    async def test_clearing_history_never_deletes_music_from_the_library(self):
        with tempfile.TemporaryDirectory() as tmp, patch("app.jobs.settings_mod.LOCAL_MODE", True):
            song = Path(tmp) / "Nova - Midnight Run.opus"
            song.write_bytes(b"x")
            manager = JobManager()
            manager._persist = lambda: None
            job = Job("x", "youtube", None, "")
            job.status = "done"
            job.produced = [str(song)]
            manager.jobs[job.id] = job
            manager.order.append(job.id)
            self.assertTrue(await manager.delete(job.id, "s"))
            self.assertTrue(song.is_file())

    def test_failed_and_cancelled_jobs_still_offer_what_they_finished_but_not_scraps(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ("Song.opus", "Other.webm.part", "Other.f251.webm", "Third.temp.opus",
                         "Review report.html"):
                (out / name).write_bytes(b"\x00" * 64)
            for status in ("error", "cancelled"):
                job = Job("x", "youtube", None, "")
                job.out_dir = out
                job.status = status
                self.assertEqual([out / "Song.opus"], job.list_files())
                self.assertEqual((True, 1), (job.to_dict()["has_files"], job.to_dict()["file_count"]))
            job.status = "running"
            self.assertFalse(job.to_dict()["has_files"])


# ---- D09: a list with failures says so and can retry just those -------------------------------

class OutcomeCountTests(unittest.IsolatedAsyncioTestCase):
    async def run_list(self, statuses):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager()
            manager._persist = lambda: None
            tracks = [Track("Nova", f"Song {i}") for i in range(len(statuses))]
            job = Job("x", "youtube", None, "", tracks=tracks, list_name="Mix", kind="bulk",
                      settings={"output_dir": tmp, "skip_existing": False, "audio_format": "opus"})
            by_title = {t.title: status for t, status in zip(tracks, statuses)}

            async def fetch(_job, track, *_args):
                return TrackOutcome(track, by_title[track.title], "", [])

            with patch.object(manager, "_fetch_track", side_effect=fetch):
                job.status = "running"
                await manager._run_tracklist_job(job)
            return job

    async def test_partial_failure_is_counted_and_the_failures_kept_for_retry(self):
        job = await self.run_list(["downloaded", "download_failed", "no_candidate", "skipped"])
        self.assertEqual({"downloaded": 1, "review": 0, "skipped": 1, "failed": 2, "temporary": 0,
                          "total": 4}, job.counts)
        self.assertEqual(["Song 1", "Song 2"], [t.title for t in job.failed_tracks])
        self.assertEqual(("done", 1), (job.status, job.code))
        self.assertTrue(job.to_dict()["can_retry_failed"])
        self.assertEqual(job.counts, job.to_dict()["counts"])

    async def test_a_failed_single_song_says_why_on_its_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager()
            manager._persist = lambda: None
            job = Job("x", "youtube", None, "", tracks=[Track("", "lofi beats")], kind="search",
                      settings={"output_dir": tmp, "skip_existing": False, "audio_format": "opus"})

            async def fetch(_job, track, *_args):
                return TrackOutcome(track, "no_candidate", "couldn't tell which song this is", [])

            with patch.object(manager, "_fetch_track", side_effect=fetch):
                await manager._run_tracklist_job(job)
        self.assertEqual(("error", "couldn't tell which song this is"), (job.status, job.note))

    async def test_all_success_all_failed_all_skipped_and_unverified(self):
        ok = await self.run_list(["downloaded", "downloaded"])
        self.assertEqual((0, 0, "done"), (ok.counts["failed"], len(ok.failed_tracks), ok.status))
        bad = await self.run_list(["download_failed", "no_candidate"])
        self.assertEqual(("error", 2), (bad.status, bad.counts["failed"]))
        skipped = await self.run_list(["skipped", "skipped"])
        self.assertEqual(("done", 2), (skipped.status, skipped.counts["skipped"]))
        review = await self.run_list(["downloaded_for_review"])
        self.assertEqual((1, 1, 0), (review.counts["downloaded"], review.counts["review"], review.counts["failed"]))

    async def test_counts_and_failures_survive_a_restart(self):
        job = await self.run_list(["downloaded", "download_failed"])
        job.status = "done"
        back = Job.from_record(json.loads(json.dumps(job.to_record())))
        self.assertEqual(job.counts, back.counts)
        self.assertEqual(["Song 1"], [t.title for t in back.failed_tracks])

    async def test_retry_failed_runs_only_the_failed_tracks_on_the_same_service(self):
        manager = JobManager()
        manager._persist = lambda: None
        old = Job("x", "youtube", None, "", list_name="Mix", kind="bulk",
                  tracks=[Track("A", "1"), Track("B", "2")],
                  settings={"bulk_service": "soundcloud", "audio_format": "mp3"})
        old.status = "done"
        old.failed_tracks = [Track("B", "2")]
        manager.jobs[old.id] = old
        with tempfile.TemporaryDirectory() as tmp, patch("app.jobs.settings_mod.LOCAL_MODE", True):
            new = await manager.retry(old.id, "s", {"output_dir": tmp, "audio_format": "opus"},
                                      failed_only=True)
        self.assertEqual([("B", "2")], [(t.artist, t.title) for t in new.tracks])
        self.assertEqual(("soundcloud", "mp3"), (new.settings["bulk_service"], new.settings["audio_format"]))
        self.assertIn("failed", new.list_name)


# ---- D10: bitrate and SoundCloud formats do what the controls say ----------------------------

class FormatControlTests(unittest.TestCase):
    def quality(self, **s):
        argv = engines.media_url_command("https://www.youtube.com/watch?v=x", "A - B",
                                         {"output_dir": "C:/out", **s})
        return argv[argv.index("--audio-quality") + 1] if "--audio-quality" in argv else None

    def test_the_bitrate_setting_reaches_ytdlp(self):
        self.assertEqual("64K", self.quality(audio_format="mp3", bitrate="64k"))
        self.assertEqual("320K", self.quality(audio_format="mp3", bitrate="320k"))
        self.assertEqual("4", self.quality(audio_format="mp3", bitrate="auto"))      # old saved value
        self.assertEqual("5", self.quality(audio_format="mp3", bitrate="small"))
        self.assertEqual("4", self.quality(audio_format="mp3", bitrate="balanced"))
        self.assertEqual("2", self.quality(audio_format="mp3", bitrate="high"))
        self.assertEqual("256K", self.quality(audio_format="m4a", bitrate="256k"))
        self.assertIsNone(self.quality(audio_format="flac", bitrate="320k"))   # lossless: no bitrate

    def test_soundcloud_links_follow_the_format_where_soundcloud_can(self):
        def cmd(fmt):
            return engines.build_command("soundcloud", "https://soundcloud.com/a/b",
                                         {"output_dir": "C:/out", "audio_format": fmt})
        self.assertIn("--flac", cmd("flac"))
        self.assertIn("--opus", cmd("opus"))
        self.assertIn("--onlymp3", cmd("mp3"))
        self.assertNotEqual(cmd("flac"), cmd("wav"))
        self.assertTrue(engines.soundcloud_format_note("wav"))       # and the UI says so
        self.assertEqual("", engines.soundcloud_format_note("mp3"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("yt-dlp"), "needs ffmpeg and yt-dlp")
    def test_the_saved_file_really_has_the_chosen_bitrate(self):
        """The real output, not just the command: a local 3-second tone, no network."""
        from mutagen.mp3 import MP3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tone = root / "tone.wav"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                            "-ac", "2", str(tone)], check=True)
            for bitrate, expected in (("64k", 64_000), ("320k", 320_000)):
                out = root / bitrate
                argv = engines.media_url_command(tone.as_uri(), "Tone",
                                                 {"output_dir": str(out), "audio_format": "mp3", "bitrate": bitrate})
                argv.insert(-1, "--enable-file-urls")
                subprocess.run(argv, check=True, capture_output=True, timeout=120)
                self.assertAlmostEqual(expected, MP3(out / "Tone.mp3").info.bitrate, delta=2_000)

    def test_bitrate_values_are_validated(self):
        self.assertEqual("320k", settings_mod._coerce_pref("bitrate", "320K"))
        self.assertEqual("balanced", settings_mod._coerce_pref("bitrate", "9999k"))
        self.assertEqual("balanced", settings_mod._coerce_pref("bitrate", "loud"))
        self.assertEqual("balanced", settings_mod._coerce_pref("bitrate", "auto"))
        self.assertEqual("small", settings_mod._coerce_pref("bitrate", "Small"))

    def test_mp3_at_balanced_quality_is_the_default(self):
        self.assertEqual(("mp3", "balanced"), (settings_mod.DEFAULTS["audio_format"], settings_mod.DEFAULTS["bitrate"]))
        spotdl = engines.build_command("spotify", "https://open.spotify.com/track/x",
                                       {"output_dir": "C:/out", "audio_format": "mp3", "bitrate": "balanced"})
        self.assertEqual("4", spotdl[spotdl.index("--bitrate") + 1])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("yt-dlp"), "needs ffmpeg and yt-dlp")
    def test_the_presets_make_variable_bitrate_mp3s_that_get_smaller(self):
        """Converted for real (a local 20-second test signal, no network)."""
        from mutagen.mp3 import MP3, BitrateMode
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.wav"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                            "anoisesrc=d=20:c=pink:a=0.3", "-af", "lowpass=f=12000", "-ac", "2", str(source)],
                           check=True)
            sizes = {}
            for preset in ("small", "balanced", "high", "320k"):
                out = root / preset
                argv = engines.media_url_command(source.as_uri(), "Song", {
                    "output_dir": str(out), "audio_format": "mp3", "bitrate": preset})
                argv.insert(-1, "--enable-file-urls")
                subprocess.run(argv, check=True, capture_output=True, timeout=120)
                mp3 = MP3(out / "Song.mp3")
                sizes[preset] = (out / "Song.mp3").stat().st_size
                expected = BitrateMode.CBR if preset == "320k" else BitrateMode.VBR
                self.assertEqual(expected, mp3.info.bitrate_mode, preset)
            self.assertLess(sizes["small"], sizes["balanced"])
            self.assertLess(sizes["balanced"], sizes["high"])
            self.assertLess(sizes["high"], sizes["320k"])


# ---- D11: an unusable output folder is refused, and config.json is replaced atomically ------

class OutputFolderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        good = self.root / "music"
        good.mkdir()
        self.config.write_text(json.dumps({"output_dir": str(good)}), encoding="utf-8")
        p = patch.object(settings_mod, "CONFIG_PATH", self.config)
        p.start()
        self.addCleanup(p.stop)

    def test_a_folder_under_a_file_is_refused_and_nothing_is_saved(self):
        blocker = self.root / "a-file"
        blocker.write_text("x")
        before = self.config.read_text(encoding="utf-8")
        with self.assertRaises(settings_mod.SettingsError) as err:
            settings_mod.save_settings({"output_dir": str(blocker / "sub"), "naming_order": "title-artist"})
        self.assertEqual("output_dir", err.exception.field)
        self.assertEqual(before, self.config.read_text(encoding="utf-8"))

    def test_relative_and_missing_but_creatable_paths(self):
        with self.assertRaises(settings_mod.SettingsError):
            settings_mod.save_settings({"output_dir": "relative/music"})
        new = self.root / "new" / "deeper"
        saved = settings_mod.save_settings({"output_dir": str(new)})
        self.assertTrue(new.is_dir())
        self.assertEqual(str(new), json.loads(self.config.read_text(encoding="utf-8"))["output_dir"])
        self.assertEqual(str(new), saved["output_dir"])
        self.assertEqual([], list(self.root.glob(".config-*")))      # no temp file left behind

    def test_an_unwritable_folder_is_refused(self):
        with patch("app.settings.tempfile.mkstemp", side_effect=PermissionError(13, "Access is denied")):
            self.assertIn("can't write", settings_mod.output_dir_problem(str(self.root / "music")))

    async def test_the_api_points_at_the_field(self):
        blocker = self.root / "a-file"
        blocker.write_text("x")
        with patch("app.main.settings_mod.LOCAL_MODE", True):
            response = await main.post_settings(request(), {"output_dir": str(blocker / "sub")})
        self.assertEqual(400, response.status_code)
        self.assertEqual("output_dir", json.loads(response.body)["field"])

    async def test_scanning_a_missing_library_says_so(self):
        with patch("app.main.settings_mod.LOCAL_MODE", True), \
             patch("app.main.settings_mod.load_settings", return_value={"output_dir": str(self.root / "gone"),
                                                                         "naming_order": "artist-title"}):
            response = await main.library_scan()
        self.assertEqual(400, response.status_code)
        self.assertIn("doesn't exist", json.loads(response.body)["error"])


# ---- D12: Save zips stream without freezing the app ----------------------------------------

async def collect(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


def hosted_job(out: Path) -> Job:
    job = Job("x", "youtube", None, "", list_name="Road trip")
    job.out_dir = out
    job.status = "done"
    return job


class ZipTests(unittest.IsolatedAsyncioTestCase):
    def files(self, root: Path) -> list[Path]:
        (root / "a").mkdir()
        (root / "b").mkdir()
        made = [root / "a" / "Sơn Tùng M-TP - Lạc Trôi.opus", root / "a" / "Same.opus", root / "b" / "Same.opus"]
        for n, path in enumerate(made):
            path.write_bytes(bytes([n]) * (100_000 + n))
        return made

    async def test_the_streamed_zip_is_valid_and_its_length_is_known_up_front(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            files = self.files(Path(tmp))
            job = Job("x", "youtube", None, "")
            job.status = "done"
            job.produced = [str(f) for f in files]
            with patch("app.main._owned", return_value=job):
                response = await main.job_file(request(), job.id)
            data = await collect(response)
        self.assertEqual(str(len(data)), response.headers["content-length"])
        self.assertIn("attachment", response.headers["content-disposition"])
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(archive.testzip())                # every CRC checks out
        self.assertEqual(["Sơn Tùng M-TP - Lạc Trôi.opus", "Same.opus", "b - Same.opus"], archive.namelist())
        self.assertEqual(100_002, len(archive.read("b - Same.opus")))
        # No trailing data descriptors: sizes are in the local headers (best for Macs/streaming).
        self.assertFalse(any(info.flag_bits & 0x08 for info in archive.infolist()))

    async def test_zip64_records_are_written_when_an_archive_needs_them(self):
        import zipfile
        from app import zipstream
        with tempfile.TemporaryDirectory() as tmp:
            files = self.files(Path(tmp))
            with patch.object(zipstream, "ZIP64_LIMIT", 150_000):   # pretend 150 kB is 4 GB
                plan = zipstream.ZipPlan(files)
                data = b"".join(plan.chunks())
        self.assertTrue(plan.zip64_end)
        self.assertEqual(plan.length, len(data))
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(archive.testzip())
        self.assertEqual(3, len(archive.namelist()))

    async def test_the_app_stays_responsive_while_a_zip_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ("a.opus", "b.opus"):
                (out / name).write_bytes(b"x" * 1000)
            job = hosted_job(out)
            from app import zipstream
            real_open = open

            def slow_open(path, mode="r", *args, **kwargs):
                if "b" in mode and str(path).endswith(".opus"):
                    time.sleep(0.3)                          # a slow disk
                return real_open(path, mode, *args, **kwargs)

            ticks = 0
            done = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not done.is_set():
                    ticks += 1
                    await asyncio.sleep(0.02)

            async def download():
                with patch("app.main._owned", return_value=job):
                    response = await main.job_file(request(), job.id)
                with patch.object(zipstream, "open", slow_open, create=True):
                    data = await collect(response)
                done.set()
                return data

            data, _ = await asyncio.gather(download(), ticker())
        self.assertGreater(len(data), 2000)
        self.assertGreater(ticks, 15)

    async def test_a_cancelled_download_frees_its_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ("a.opus", "b.opus"):
                (out / name).write_bytes(b"x" * 3_000_000)
            job = hosted_job(out)
            with patch("app.main._owned", return_value=job):
                response = await main.job_file(request(), job.id)
            stream = response.body_iterator
            await stream.__anext__()                          # the browser starts, then goes away
            self.assertEqual(1, main._ZIP_SLOTS._value)
            await stream.aclose()
        self.assertEqual(2, main._ZIP_SLOTS._value)

    async def test_a_file_that_vanished_is_reported_before_anything_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ("a.opus", "b.opus"):
                (out / name).write_bytes(b"x")
            job = hosted_job(out)
            files = job.list_files()
            files[1].unlink()
            with patch("app.main._owned", return_value=job),                  patch.object(Job, "list_files", return_value=files):
                response = await main.job_file(request(), job.id)
        self.assertEqual(500, response.status_code)


if __name__ == "__main__":
    unittest.main()
