"""The implementation review of 16 October 2026 (docs/audits/2026-10-16): each finding, pinned.

Temporary folders and mocked providers only — no real library, account or download.
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import engines, jobs, main, scdl_tool, ytdlp_tool, zipstream
from app import settings as settings_mod
from app.jobs import Job, JobManager
from app.library import LibraryIndex, LibraryTrack, STAGING_DIR
from app.matching import Candidate
from app.review_report import TrackOutcome
from app.spotify_resolver import Track
from support import make_audio, output_of, write_audio_output, write_output


def request(sid="s1"):
    return types.SimpleNamespace(state=types.SimpleNamespace(sid=sid, remote=False), cookies={},
                                 headers={}, client=types.SimpleNamespace(host="127.0.0.1"))


def track_settings(tmp, **extra):
    return {"output_dir": str(tmp), "skip_existing": True, "audio_format": "opus", "concurrency": 1,
            **extra}


SONG = Candidate("youtube_music", "https://yt.test/song", "Midnight Run", "Nova", 3, True)


# ---- R01: every finished file is offered, from the job's own manifest ---------------------

class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_a_hosted_playlist_in_a_subfolder_is_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "job"
            nested = out / "Playlist" / "Artist - Saved.mp3"
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b"audio")
            (out / "Playlist" / "Artist - Half.mp3.part").write_bytes(b"x")
            (out / STAGING_DIR / "x").mkdir(parents=True)
            (out / STAGING_DIR / "x" / "Busy.opus").write_bytes(b"x")
            job = Job("sc", "soundcloud", ["scdl"], "")
            job.out_dir, job.status = out, "done"
            self.assertEqual([nested], job.list_files())
            self.assertTrue(job.to_dict()["has_files"])

    def test_nothing_outside_the_job_folder_is_ever_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "job"
            out.mkdir()
            outside = Path(tmp) / "secret.opus"
            outside.write_bytes(b"audio")
            job = Job("sc", "soundcloud", ["scdl"], "")
            job.out_dir, job.status = out, "done"
            job.produced = [str(out / ".." / "secret.opus"), str(outside)]
            self.assertEqual([], job.list_files())
            try:
                os.symlink(outside, out / "link.opus")
            except OSError:
                return                                   # symlinks need privileges on Windows
            self.assertEqual([], job.list_files())

    def test_every_link_job_gets_a_manifest_local_or_hosted(self):
        for local in (True, False):
            with tempfile.TemporaryDirectory() as tmp, patch("app.jobs.settings_mod.LOCAL_MODE", local), \
                    patch("app.jobs.DOWNLOAD_ROOT", Path(tmp)):
                manager = JobManager()
                manager._persist = lambda: None
                for link in ("https://soundcloud.com/a/sets/b", "https://www.youtube.com/watch?v=x"):
                    job = asyncio.run(manager.submit(link, {**settings_mod.DEFAULTS, "output_dir": tmp}, "s"))
                    manifest = job.settings["_produced_file"]
                    self.assertIn(manifest, job.argv)

    async def test_a_rerun_that_skips_everything_still_lists_its_files(self):
        """scdl writes the manifest even for tracks already there; Save then offers them."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "job"
            (out / "Playlist").mkdir(parents=True)
            kept = out / "Playlist" / "Artist - Old.mp3"
            new = out / "Playlist" / "Artist - New.mp3"
            kept.write_bytes(b"old")
            job = Job("https://soundcloud.com/a/sets/b", "soundcloud", None, "")
            job.out_dir = out
            manifest = Path(tmp) / "manifest.txt"
            job.settings = {"_produced_file": str(manifest), "output_dir": str(out)}

            async def scdl(_job, argv, **kwargs):
                new.write_bytes(b"new")
                manifest.write_text(f"{kept}\n{new}\n", encoding="utf-8")
                return 0

            job.argv = ["scdl"]
            with patch.object(JobManager, "_stream_subprocess", side_effect=scdl):
                manager = JobManager()
                manager._persist = lambda: None
                await manager._run_job(job)
            self.assertEqual([kept, new], job.list_files())
            self.assertFalse(manifest.exists())               # read, then cleaned up

    def test_the_scdl_wrapper_adds_only_the_manifest(self):
        argv, manifest = scdl_tool.split_manifest(["-l", "u", "--omnidl-manifest", "C:/m.txt", "-c"])
        self.assertEqual((["-l", "u", "-c"], "C:/m.txt"), (argv, manifest))
        params = ytdlp_tool.with_hooks({"postprocessors": [{"key": "FFmpegVideoRemuxer"}],
                                        "print_to_file": {"after_move": [("title", "t.txt")]}}, "C:/m.txt", {})
        self.assertEqual([{"key": "FFmpegVideoRemuxer"}], params["postprocessors"])   # scdl's kept
        self.assertEqual([("title", "t.txt"), (ytdlp_tool.MANIFEST_TEMPLATE, "C:/m.txt")],
                         params["print_to_file"]["after_move"])
        self.assertNotIn("match_filter", params)              # nothing known: nothing skipped

    def test_the_wrapper_hands_scdl_a_youtubedl_that_writes_the_manifest(self):
        import scdl.scdl as scdl_main
        seen = {}

        def fake_main():
            seen["argv"] = list(__import__("sys").argv)
            seen["params"] = scdl_main.YoutubeDL({"quiet": True}).params

        original = scdl_main.YoutubeDL
        try:
            with patch.object(scdl_main, "_main", fake_main):
                scdl_tool.main(["-l", "https://soundcloud.com/a", "--omnidl-manifest", "C:/m.txt"])
        finally:
            scdl_main.YoutubeDL = original
        self.assertEqual(["scdl", "-l", "https://soundcloud.com/a"], seen["argv"])
        self.assertIn((ytdlp_tool.MANIFEST_TEMPLATE, "C:/m.txt"), seen["params"]["print_to_file"]["after_move"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("yt-dlp"), "needs ffmpeg and yt-dlp")
    def test_yt_dlp_lists_a_file_it_found_already_downloaded(self):
        """The mechanism scdl and yt-dlp jobs rely on, on a local file (no network)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tone = make_audio(root / "tone.wav", 2)
            for run in (1, 2):
                manifest = root / f"m{run}.txt"
                argv = engines.media_url_command(tone.as_uri(), "Tone", {
                    "output_dir": str(root / "out"), "audio_format": "opus", "_produced_file": str(manifest)})
                argv.insert(-1, "--enable-file-urls")
                subprocess.run(argv, check=True, capture_output=True, timeout=120)
                lines = manifest.read_text(encoding="utf-8").split()
                self.assertEqual(1, len(lines), f"run {run}")
                self.assertTrue(lines[0].endswith("Tone.opus"))


# ---- R02: zips with unique names and consistent bytes -------------------------------------

class ZipIntegrityTests(unittest.TestCase):
    def test_repeated_names_and_case_never_collide(self):
        names = zipstream.arc_names(
            [Path(f"/m/album{i}/Disc 1/Song.mp3") for i in range(3)] + [Path("/m/x/song.MP3")])
        self.assertEqual(len(names), len({n.casefold() for n in names}))
        self.assertEqual("album2 - Disc 1 - Song.mp3", names[2])

    def test_a_file_that_changes_after_planning_fails_instead_of_corrupting(self):
        with tempfile.TemporaryDirectory() as tmp:
            for change in (b"original expanded", b"orig", b"ORIGINAL"):
                path = Path(tmp) / "changing.mp3"
                path.write_bytes(b"original")
                plan = zipstream.ZipPlan([path])
                time.sleep(0.01)
                path.write_bytes(change)
                with self.assertRaises(zipstream.FileChanged):
                    b"".join(plan.chunks())

    def test_a_change_in_the_middle_of_sending_fails_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.mp3"
            second = Path(tmp) / "b.mp3"
            first.write_bytes(b"a" * 3000)
            second.write_bytes(b"b" * 3000)
            plan = zipstream.ZipPlan([first, second])
            chunks = plan.chunks()
            next(chunks)                                   # a's header has gone out
            time.sleep(0.01)
            second.write_bytes(b"c" * 3000)                # same size, different bytes and time
            with self.assertRaises(zipstream.FileChanged):
                list(chunks)

    def test_an_unchanged_archive_is_still_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for i in range(3):
                path = Path(tmp) / f"album{i}" / "Disc 1" / "Song.mp3"
                path.parent.mkdir(parents=True)
                path.write_bytes(bytes([65 + i]) * 1000)
                files.append(path)
            data = b"".join(zipstream.ZipPlan(files).chunks())
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(archive.testzip())
        self.assertEqual(3, len(set(archive.namelist())))


# ---- R03 / R04: one writer per file; only real audio counts as existing --------------------

class OutputOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def fetch_many(self, calls, *, manager=None, tmp, fake=None, song=SONG):
        manager = manager or JobManager()
        started = []

        async def download(_job, argv, emit, **kwargs):
            started.append(output_of(argv))
            await asyncio.sleep(0.02)
            write_audio_output(argv)
            return 0

        with patch("app.jobs.candidate_search.search_all", return_value=[song]), \
             patch.object(manager, "_stream_subprocess", side_effect=fake or download), \
             patch.object(manager, "_tag"):
            results = await asyncio.gather(*calls(manager))
        return results, started

    async def test_duplicate_rows_and_two_jobs_never_write_the_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Job("a", "youtube", None, "", settings=track_settings(tmp))
            second = Job("b", "youtube", None, "", settings=track_settings(tmp))
            first.status = second.status = "running"
            results, started = await self.fetch_many(lambda m: [
                m._fetch_track(first, Track("Nova", "Midnight Run", 3), first.settings, False, 1, 3),
                m._fetch_track(first, Track("Nova", "Midnight Run", 3), first.settings, False, 2, 3),
                m._fetch_track(second, Track("Nova", "Midnight Run", 3), second.settings, False, 1, 1),
            ], tmp=tmp)
            self.assertEqual(1, len(started))                    # one download, not three
            self.assertEqual(["downloaded", "skipped", "skipped"], sorted(r.status for r in results))
            self.assertTrue((Path(tmp) / "Nova - Midnight Run.opus").is_file())
            self.assertFalse((Path(tmp) / STAGING_DIR).exists())  # staging cleaned up

    async def test_downloads_are_staged_and_published_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Job("a", "youtube", None, "", settings=track_settings(tmp))
            job.status = "running"
            seen = {}

            async def download(_job, argv, emit, **kwargs):
                seen["to"] = output_of(argv)
                seen["final_exists_meanwhile"] = (Path(tmp) / "Nova - Midnight Run.opus").exists()
                write_audio_output(argv)
                return 0

            (result,), _ = await self.fetch_many(
                lambda m: [m._fetch_track(job, Track("Nova", "Midnight Run", 3), job.settings, False, 1, 1)],
                tmp=tmp, fake=download)
            self.assertIn(STAGING_DIR, seen["to"].parts)
            self.assertFalse(seen["final_exists_meanwhile"])
            self.assertEqual("downloaded", result.status)

    async def test_a_failed_download_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Job("a", "youtube", None, "", settings=track_settings(tmp))
            job.status = "running"

            async def broken(_job, argv, emit, **kwargs):
                write_output(argv, b"")                      # an empty "success"
                output_of(argv).with_suffix(".webp").write_bytes(b"thumb")
                return 0

            with patch("app.jobs.asyncio.sleep", AsyncMock()):
                (result,), _ = await self.fetch_many(
                    lambda m: [m._fetch_track(job, Track("Nova", "Midnight Run", 3), job.settings, False, 1, 1)],
                    tmp=tmp, fake=broken)
            self.assertEqual("download_failed", result.status)
            self.assertEqual([], [p.name for p in Path(tmp).rglob("*") if p.is_file()])

    async def test_two_different_songs_with_the_same_file_name_both_get_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Job("a", "youtube", None, "", settings=track_settings(tmp))
            job.status = "running"
            one, two = Track("A/B", "Song", 3), Track("A_B", "Song", 3)    # both "A_B - Song"
            song_one = Candidate("youtube_music", "https://yt.test/1", "Song", "A/B", 3, True)
            song_two = Candidate("youtube_music", "https://yt.test/2", "Song", "A_B", 3, True)
            manager = JobManager()

            async def download(_job, argv, emit, **kwargs):
                await asyncio.sleep(0.02)
                write_audio_output(argv)
                return 0

            def search(artist, title, service, deep=False):
                return [song_one if artist == "A/B" else song_two]

            with patch("app.jobs.candidate_search.search_all", side_effect=search), \
                 patch.object(manager, "_stream_subprocess", side_effect=download), \
                 patch.object(manager, "_tag"):
                results = await asyncio.gather(
                    manager._fetch_track(job, one, job.settings, False, 1, 2),
                    manager._fetch_track(job, two, job.settings, False, 2, 2))
            self.assertEqual(["downloaded", "downloaded"], [r.status for r in results])
            self.assertEqual({"A_B - Song.opus", "A_B - Song (2).opus"}, {r.saved_as for r in results})

    async def test_a_cancelled_waiter_doesnt_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Job("a", "youtube", None, "", settings=track_settings(tmp))
            second = Job("b", "youtube", None, "", settings=track_settings(tmp, skip_existing=False))
            first.status = second.status = "running"
            calls = []

            async def download(_job, argv, emit, **kwargs):
                calls.append(_job.input)
                second.status = "cancelled"                   # cancelled while it waits
                await asyncio.sleep(0.02)
                write_audio_output(argv)
                return 0

            results, _ = await self.fetch_many(lambda m: [
                m._fetch_track(first, Track("Nova", "Midnight Run", 3), first.settings, False, 1, 1),
                m._fetch_track(second, Track("Nova", "Midnight Run", 3), second.settings, False, 1, 1),
            ], tmp=tmp, fake=download)
            self.assertEqual(["a"], calls)
            self.assertEqual("cancelled", results[1].status)

    async def test_an_empty_or_cut_off_file_is_replaced_and_kept_aside(self):
        def cut_off(path):                         # this song (by its tags), but only a second of it
            make_audio(path, 1.0)
            JobManager._tag(path, Track("Nova", "Midnight Run"))

        for name, prepare in (("empty", lambda p: p.write_bytes(b"")), ("cut off", cut_off)):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "Nova - Midnight Run.opus"
                prepare(target)
                job = Job("a", "youtube", None, "", settings=track_settings(tmp))
                job.status = "running"
                long_song = Candidate("youtube_music", "https://yt.test/song", "Midnight Run", "Nova", 20, True)
                (result,), started = await self.fetch_many(
                    lambda m: [m._fetch_track(job, Track("Nova", "Midnight Run", 20), job.settings, False, 1, 1)],
                    tmp=tmp, song=long_song)
                self.assertEqual("downloaded", result.status)
                self.assertEqual(1, len(started))
                self.assertTrue((Path(tmp) / "Nova - Midnight Run (unreadable).opus").exists())
                self.assertEqual([str(target)], job.produced)

    async def test_real_audio_without_tags_is_kept_but_not_taken_for_the_song(self):
        """Playable but unidentifiable (no tags, no record — put there by hand, say): it isn't
        broken, so it isn't set aside as "(unreadable)"; but nothing says it is this song either,
        so it's left exactly as it is and the song is saved beside it (follow-up review F03)."""
        with tempfile.TemporaryDirectory() as tmp:
            existing = make_audio(Path(tmp) / "Nova - Midnight Run.opus", 3)
            before = existing.read_bytes()
            job = Job("a", "youtube", None, "", settings=track_settings(tmp))
            job.status = "running"
            (result,), started = await self.fetch_many(
                lambda m: [m._fetch_track(job, Track("Nova", "Midnight Run", 3), job.settings, False, 1, 1)],
                tmp=tmp)
            self.assertEqual(("downloaded", 1), (result.status, len(started)))
            self.assertEqual("Nova - Midnight Run (2).opus", result.saved_as)
            self.assertEqual(before, existing.read_bytes())
            self.assertFalse((Path(tmp) / "Nova - Midnight Run (unreadable).opus").exists())

    async def test_real_audio_tagged_as_the_song_counts_as_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = make_audio(Path(tmp) / "Nova - Midnight Run.opus", 3)
            JobManager._tag(existing, Track("Nova", "Midnight Run"))
            job = Job("a", "youtube", None, "", settings=track_settings(tmp))
            job.status = "running"
            (result,), started = await self.fetch_many(
                lambda m: [m._fetch_track(job, Track("Nova", "Midnight Run", 3), job.settings, False, 1, 1)],
                tmp=tmp)
            self.assertEqual(("skipped", 0), (result.status, len(started)))

    def test_an_unreadable_library_copy_doesnt_count(self):
        def item(issues, size=100):
            return LibraryTrack(path=Path("x.mp3"), relative_path="x.mp3", title="Midnight Run",
                                artist="Nova", album="", duration=180, bitrate=0, sample_rate=0,
                                channels=0, codec="mp3", size=size, has_artwork=True, issues=issues)
        self.assertIsNone(LibraryIndex([item(("unreadable",))]).find("Nova", "Midnight Run", 180))
        self.assertIsNone(LibraryIndex([item((), size=0)]).find("Nova", "Midnight Run", 180))
        self.assertIsNotNone(LibraryIndex([item(("missing_album",))]).find("Nova", "Midnight Run", 180))


# ---- R05: the folder that's checked is the folder that's saved and used ---------------------

class OutputPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"output_dir": str(self.root)}), encoding="utf-8")
        for p in (patch.object(settings_mod, "CONFIG_PATH", self.config),
                  patch.dict(os.environ, {"OMNIDL_TEST_ROOT": str(self.root)})):
            p.start()
            self.addCleanup(p.stop)

    def test_variables_and_home_are_saved_expanded(self):
        for raw in (r"%OMNIDL_TEST_ROOT%\music", "$OMNIDL_TEST_ROOT/music"):
            saved = settings_mod.save_settings({"output_dir": raw})
            self.assertEqual(str(self.root / "music"), saved["output_dir"])
            stored = json.loads(self.config.read_text(encoding="utf-8"))["output_dir"]
            self.assertEqual(str(self.root / "music"), stored)
            self.assertTrue(Path(settings_mod.load_settings()["output_dir"]).is_absolute())
        self.assertEqual(os.path.expanduser("~"), settings_mod.canonical_dir("~"))

    def test_an_unknown_variable_is_refused(self):
        with self.assertRaises(settings_mod.SettingsError) as err:
            settings_mod.save_settings({"output_dir": r"%OMNIDL_NOT_SET_ANYWHERE%\music"})
        self.assertIn("isn't set", str(err.exception))

    def test_an_old_unexpanded_setting_means_the_same_folder_everywhere(self):
        self.config.write_text(json.dumps({"output_dir": r"%OMNIDL_TEST_ROOT%\old"}), encoding="utf-8")
        self.assertEqual(str(self.root / "old"), settings_mod.load_settings()["output_dir"])

    def test_unicode_paths_survive(self):
        saved = settings_mod.save_settings({"output_dir": str(self.root / "Nhạc Việt")})
        self.assertTrue(Path(saved["output_dir"]).is_dir())


# ---- R06: one job per submission token, even when copies arrive together -------------------

class SubmissionTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = JobManager()
        self.manager._persist = lambda: None

        async def slow_send(*_args):
            await asyncio.sleep(0.01)                       # queueing yields, as it really does

        self.manager._send = slow_send
        for p in (patch.object(main, "manager", self.manager),
                  patch.object(settings_mod, "effective_settings",
                               return_value={**settings_mod.DEFAULTS, "output_dir": self.tmp.name}),
                  patch.object(main, "_RECENT_SUBMITS", {}), patch.object(main, "_INFLIGHT", {})):
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_simultaneous_copies_make_one_job(self):
        payload = {"input": "Nova - Midnight Run", "client_token": "same-token-123"}
        one, two, three = await asyncio.gather(*(main.download(request(), dict(payload)) for _ in range(3)))
        self.assertEqual(1, len(self.manager.jobs))
        self.assertEqual({one["id"]}, {two["id"], three["id"]})
        again = await main.download(request(), dict(payload))          # a later replay
        self.assertEqual(one["id"], again["id"])

    async def test_the_same_token_for_a_different_request_is_refused(self):
        await main.download(request(), {"input": "Nova - Midnight Run", "client_token": "tok-12345678"})
        reply = await main.download(request(), {"input": "Other - Song", "client_token": "tok-12345678"})
        self.assertEqual(409, reply.status_code)
        self.assertEqual(1, len(self.manager.jobs))

    async def test_a_failed_attempt_can_be_retried_with_the_same_token(self):
        payload = {"input": "Nova - Midnight Run", "client_token": "retry-token-1"}
        with patch.object(self.manager, "check_limit", return_value="busy"):
            refused = await main.download(request(), dict(payload))
        self.assertEqual(429, refused.status_code)
        accepted = await main.download(request(), dict(payload))
        self.assertIn("id", accepted)

    async def test_tokens_are_per_visitor_and_per_kind_of_request(self):
        payload = {"input": "Nova - Midnight Run", "client_token": "shared-token"}
        await main.download(request("a"), dict(payload))
        await main.download(request("b"), dict(payload))
        self.assertEqual(2, len(self.manager.jobs))


# ---- R10: progress only moves forward; retries are their own phase -------------------------

class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def run_list(self, statuses, delays, retry_to=None):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager()
            manager._persist = lambda: None
            tracks = [Track("Nova", f"Song {i}") for i in range(len(statuses))]
            job = Job("list", "youtube", None, "", tracks=tracks, kind="bulk",
                      settings=track_settings(tmp, skip_existing=False, concurrency=3))
            seen = []

            async def send(_job, event):
                if event["type"] == "progress":
                    seen.append((event["progress"], event["label"]))

            async def fetch(_job, track, settings, detailed, index, total):
                await asyncio.sleep(delays[index - 1])
                if retry_to and seen and detailed and len([1 for p, _ in seen if p is not None]) > len(statuses):
                    return TrackOutcome(track, retry_to, "", [])
                status = statuses[index - 1]
                if status == "retry":
                    return TrackOutcome(track, "no_candidate", "search failed", [], retryable=True)
                return TrackOutcome(track, status, "", [])

            manager._send = send
            with patch.object(manager, "_fetch_track", side_effect=fetch), \
                 patch.object(JobManager, "_sleep_unless_cancelled", AsyncMock()):
                job.status = "running"
                await manager._run_tracklist_job(job)
            return [p for p, _ in seen], [label for _, label in seen], job

    async def test_staggered_parallel_tracks_never_move_progress_backwards(self):
        progress, _labels, _job = await self.run_list(["skipped"] * 4, [0.004, 0.008, 0.012, 0.016])
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(100, progress[-1])

    async def test_retries_are_shown_as_retries_and_finish_at_100(self):
        progress, labels, job = await self.run_list(["downloaded", "retry", "downloaded", "downloaded"],
                                                    [0.001] * 4, retry_to="downloaded")
        self.assertEqual(progress, sorted(progress))
        self.assertTrue(any(label.startswith("Retrying") for label in labels))
        before_retry = [p for p, label in zip(progress, labels) if label.startswith("Waiting to retry")]
        self.assertTrue(before_retry and before_retry[0] < 100)       # not "finished" before retrying
        self.assertEqual(100, job.progress)


# ---- R11: interrupted hosted jobs expire; orphaned folders are reaped ----------------------

class ExpiryTests(unittest.IsolatedAsyncioTestCase):
    def test_an_interrupted_job_gets_a_finish_time_and_an_expiry(self):
        job = Job("x", "youtube", ["yt-dlp"], "")
        job.out_dir = Path("C:/srv/downloads/s/j")
        job.status = "running"
        restored = Job.from_record(job.to_record())
        self.assertEqual("cancelled", restored.status)
        self.assertIsNotNone(restored.finished)
        self.assertIsNotNone(restored.to_dict()["expires"])

    async def test_interrupted_and_orphaned_hosted_files_are_deleted_after_the_ttl(self):
        with tempfile.TemporaryDirectory() as tmp, patch("app.jobs.settings_mod.LOCAL_MODE", False), \
                patch("app.jobs.DOWNLOAD_ROOT", Path(tmp)), patch("app.jobs.LEGACY_DOWNLOAD_ROOT", Path(tmp)):
            job = Job("x", "youtube", ["yt-dlp"], "", session="s")
            gone = Job("y", "youtube", ["yt-dlp"], "", session="s")     # history no longer has it
            JobManager._prepare_out_dir(job, {}, "s")
            JobManager._prepare_out_dir(gone, {}, "s")
            known, orphan = job.out_dir, gone.out_dir
            for folder in (known, orphan):
                (folder / "a.opus").write_bytes(b"x")
            manager = JobManager()
            manager._persist = lambda: None
            manager._send = AsyncMock()
            job.status = "running"
            restored = Job.from_record(job.to_record())
            manager.jobs[restored.id] = restored
            await manager._expire_files(now=time.time() + 60)
            self.assertTrue(known.exists() and orphan.exists())          # not old enough yet
            old = time.time() - settings_mod.FILE_TTL_SECONDS - 10
            os.utime(orphan, (old, old))
            await manager._expire_files(now=time.time() + settings_mod.FILE_TTL_SECONDS + 60)
            self.assertFalse(known.exists())
            self.assertFalse(orphan.exists())

    async def test_a_local_install_never_reaps_anything(self):
        with tempfile.TemporaryDirectory() as tmp, patch("app.jobs.settings_mod.LOCAL_MODE", True), \
                patch("app.jobs.DOWNLOAD_ROOT", Path(tmp)):
            music = Path(tmp) / "Artist" / "Album"
            music.mkdir(parents=True)
            (music / "song.opus").write_bytes(b"x")
            old = time.time() - 10 * settings_mod.FILE_TTL_SECONDS
            os.utime(music, (old, old))
            manager = JobManager()
            manager._send = AsyncMock()
            await manager._expire_files(now=time.time())
            self.assertTrue((music / "song.opus").exists())


# ---- system: crash-safe history, shared back-off, search deadlines --------------------------

class HistoryTests(unittest.TestCase):
    def test_history_is_written_atomically_with_a_backup_and_recovers_from_damage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, backup = Path(tmp) / "history.json", Path(tmp) / "history.json.bak"
            with patch("app.jobs._HISTORY_PATH", path), patch("app.jobs._HISTORY_BACKUP", backup):
                manager = JobManager()
                for name in ("first", "second"):
                    job = Job(name, "youtube", ["yt-dlp"], "")
                    job.status = "done"
                    manager.jobs[job.id] = job
                    manager.order.append(job.id)
                    manager._persist()
                self.assertEqual([], list(Path(tmp).glob("*.tmp")))
                self.assertTrue(backup.exists())
                path.write_text('[{"id": "trunc', encoding="utf-8")       # a crash mid-write
                restored = JobManager()
                restored._load_history()
                self.assertEqual(["first"], [j.input for j in restored.jobs.values()])


class SharedBackoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_jobs_rate_limit_pauses_the_others(self):
        manager = JobManager()
        manager._send = AsyncMock()
        first = Job("a", "youtube", None, "")
        second = Job("b", "youtube", None, "")
        await manager._rate_limited(first)
        waits = []

        async def sleep(_job, seconds):
            waits.append((_job.input, round(seconds)))

        with patch.object(JobManager, "_sleep_unless_cancelled", side_effect=sleep):
            await manager._wait_for_cooldown(second)
        self.assertEqual("b", waits[0][0])
        self.assertGreater(waits[0][1], 20)


class SearchDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_search_that_hangs_is_retried_later_instead_of_blocking(self):
        import threading
        release = threading.Event()

        def hang(*_args, **_kwargs):
            release.wait(5)
            return []

        with tempfile.TemporaryDirectory() as tmp:
            job = Job("a", "youtube", None, "", settings=track_settings(tmp, skip_existing=False))
            job.status = "running"
            manager = JobManager()
            try:
                with patch("app.jobs._SEARCH_DEADLINE", 0.05), \
                     patch("app.jobs.candidate_search.search_all", side_effect=hang):
                    result = await manager._fetch_track(job, Track("Nova", "Midnight Run", 3), job.settings,
                                                        False, 1, 1)
            finally:
                release.set()
        self.assertEqual("no_candidate", result.status)
        self.assertTrue(result.retryable)
        self.assertIn("didn't answer", result.reason)


class FilesEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_is_checked_before_the_browser_starts_a_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            job = Job("x", "youtube", None, "")
            job.out_dir, job.status = out, "done"
            with patch("app.main._owned", return_value=job):
                missing = await main.job_files(request(), job.id)
                (out / "a.opus").write_bytes(b"x" * 10)
                (out / "b.opus").write_bytes(b"x" * 20)
                ready = await main.job_files(request(), job.id)
        self.assertEqual(404, missing.status_code)
        self.assertEqual({"count": 2, "bytes": 30, "zip": True}, ready)


if __name__ == "__main__":
    unittest.main()
