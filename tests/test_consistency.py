"""One song should come back the same way however it was asked for, and a job should re-run
as what it actually was."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app import main
from app.jobs import Job, JobManager, _short_error
from app.matching import Candidate
from app.spotify_resolver import Track


def settings(temp_dir, **extra):
    return {"output_dir": temp_dir, "skip_existing": False, "audio_format": "opus",
            "concurrency": 1, **extra}


class TypedSearchRoutingTests(unittest.IsolatedAsyncioTestCase):
    """A typed song used to be "ytsearch1:" — YouTube's #1 result, a different algorithm from
    playlists and Bulk. On 80 real songs it got the right version 45 times; the matcher 78."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = JobManager()
        self.manager._persist = lambda: None

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_a_typed_song_goes_through_the_same_matcher_as_a_playlist(self):
        job = await self.manager.submit("Drake - One Dance", settings(self.tmp.name), "s")
        self.assertEqual("search", job.kind)
        self.assertIsNone(job.argv)
        self.assertEqual([("Drake", "One Dance")], [(t.artist, t.title) for t in job.tracks])
        self.assertEqual("Drake - One Dance", job.input)        # the queue shows what was typed
        self.assertEqual("Song search", job.to_dict()["engine_label"])

    async def test_video_mode_still_searches_youtube_for_the_video(self):
        job = await self.manager.submit("drake one dance", settings(self.tmp.name, media_type="video"), "s")
        self.assertEqual("command", job.kind)
        self.assertTrue(any("ytsearch1:" in part for part in job.argv))

    async def test_links_are_not_treated_as_searches(self):
        job = await self.manager.submit("https://www.youtube.com/watch?v=abc", settings(self.tmp.name), "s")
        self.assertEqual("command", job.kind)

    async def test_the_engine_dropdown_picks_the_search_service(self):
        """SoundCloud + typed text used to run scdl on the words, which needs a URL and failed."""
        job = await self.manager.submit("Drake - One Dance", settings(self.tmp.name), "s", "soundcloud")
        self.assertEqual("soundcloud", job.settings["bulk_service"])
        job = await self.manager.submit("Drake - One Dance", settings(self.tmp.name), "s", "spotify")
        self.assertEqual("auto", job.settings["bulk_service"])   # Spotify can't search by text


class StandInTests(unittest.IsolatedAsyncioTestCase):
    """If the right version won't download, a different version must not be saved instead."""

    async def _run(self, candidates, fails):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job("x", "spotify", None, "", settings=settings(temp_dir))
            target = Path(temp_dir) / "Nova - Midnight Run.opus"
            tried = []

            async def download(_job, argv, emit, **kwargs):
                url = argv[-1]
                tried.append(url)
                if url in fails:
                    kwargs["errors"].append("ERROR: [youtube] abc: HTTP Error 403: Forbidden")
                    return 1
                target.touch()
                return 0

            async def no_sleep(_):
                return None

            with patch("app.jobs.candidate_search.search_all", return_value=candidates), \
                 patch.object(manager, "_stream_subprocess", side_effect=download), \
                 patch("app.jobs.asyncio.sleep", side_effect=no_sleep):
                result = await manager._fetch_track(job, Track("Nova", "Midnight Run", 180),
                                                    job.settings, True, 1, 1)
            return result, tried, job.output

    async def test_a_different_version_is_not_saved_when_the_right_one_fails(self):
        right = Candidate("youtube_music", "https://yt/right", "Midnight Run", "Nova", 181, True)
        shorter = Candidate("youtube", "https://yt/edit", "Nova - Midnight Run", "Uploader", 164)
        result, tried, output = await self._run([right, shorter], fails={"https://yt/right"})
        self.assertEqual("download_failed", result.status)     # handed to the retry sweep
        self.assertNotIn("https://yt/edit", tried)
        self.assertEqual(("https://yt/right",), tuple(d.candidate.url for d in result.failed_attempts))
        self.assertIn("HTTP Error 403", output)                  # and it says why

    async def test_the_same_recording_from_another_source_may_stand_in(self):
        right = Candidate("youtube_music", "https://yt/right", "Midnight Run", "Nova", 181, True)
        same = Candidate("youtube", "https://yt/same", "Nova - Midnight Run (Official Audio)", "Nova", 180)
        result, tried, _ = await self._run([right, same], fails={"https://yt/right"})
        self.assertEqual("downloaded", result.status)
        self.assertEqual("https://yt/same", result.selected.candidate.url)


class PhraseTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_named_phrase_is_matched_as_that_song(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job("x", "youtube", None, "", settings=settings(temp_dir))
            song = Candidate("youtube_music", "https://yt/1", "One Dance", "Drake", 174, True)
            target = Path(temp_dir) / "Drake - One Dance.opus"

            async def download(_job, argv, emit, **kwargs):
                target.touch()
                return 0

            with patch("app.jobs.candidate_search.resolve_phrase", return_value=(["Drake"], "One Dance")), \
                 patch("app.jobs.candidate_search.search_all", return_value=[song]) as search, \
                 patch.object(manager, "_stream_subprocess", side_effect=download):
                result = await manager._fetch_track(job, Track("", "drake one dance"), job.settings, True, 1, 1)

        search.assert_called_once_with("Drake", "One Dance", "auto")
        self.assertEqual(("Drake", "One Dance"), (result.track.artist, result.track.title))
        self.assertEqual("Drake - One Dance.opus", result.saved_as)

    async def test_an_unidentifiable_phrase_falls_back_to_youtubes_top_result_for_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job("x", "youtube", None, "", settings=settings(temp_dir))
            target = Path(temp_dir) / "lofi beats to study to.opus"
            urls = []

            async def download(_job, argv, emit, **kwargs):
                urls.append(argv[-1])
                target.touch()
                return 0

            with patch("app.jobs.candidate_search.resolve_phrase", return_value=None), \
                 patch.object(manager, "_stream_subprocess", side_effect=download):
                result = await manager._fetch_track(job, Track("", "lofi beats to study to"), job.settings, True, 1, 1)

        self.assertEqual(["ytsearch1:lofi beats to study to"], urls)
        self.assertEqual("downloaded_for_review", result.status)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = JobManager()
        self.manager._persist = lambda: None

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_a_pasted_list_is_retried_as_that_list(self):
        """Retry used to send the display text, searching YouTube for "Wedding songs (3 tracks)"."""
        tracks = [Track("Luong Khanh Vy", "Con Gai Mien Tay"), Track("George Lam", "Mr Strong Man")]
        old = await self.manager.submit_bulk(tracks, settings(self.tmp.name), "s",
                                             service="soundcloud", list_name="Wedding songs")
        new = await self.manager.retry(old.id, "s", settings(self.tmp.name))
        self.assertEqual("bulk", new.kind)
        self.assertEqual([(t.artist, t.title) for t in tracks], [(t.artist, t.title) for t in new.tracks])
        self.assertEqual("soundcloud", new.settings["bulk_service"])
        self.assertEqual("Wedding songs (2 tracks)", new.input)

    async def test_a_list_survives_a_restart_and_can_still_be_retried(self):
        tracks = [Track("Frances Yip", "Shanghai Bund", artists=["Frances Yip"], duration=200)]
        old = await self.manager.submit_bulk(tracks, settings(self.tmp.name), "s", list_name="Songs")
        restored = Job.from_record(json.loads(json.dumps(old.to_record())))
        self.assertEqual("bulk", restored.kind)
        self.assertEqual(("Frances Yip", "Shanghai Bund", 200),
                         (restored.tracks[0].artist, restored.tracks[0].title, restored.tracks[0].duration))

        fresh = JobManager()
        fresh._persist = lambda: None
        fresh.jobs[restored.id] = restored
        new = await fresh.retry(restored.id, "s", settings(self.tmp.name))
        self.assertEqual("Shanghai Bund", new.tracks[0].title)

    async def test_a_list_saved_before_lists_were_kept_is_refused_not_mangled(self):
        legacy = Job.from_record({"id": "old", "input": "Wedding songs (3 tracks)", "engine": "youtube",
                                  "status": "done", "session": "s"})
        self.manager.jobs["old"] = legacy
        result = await self.manager.retry("old", "s", settings(self.tmp.name))
        self.assertIsInstance(result, str)
        self.assertIn("paste it into Bulk again", result)

    async def test_retry_keeps_the_original_mode(self):
        old = await self.manager.submit("https://www.youtube.com/watch?v=abc",
                                        settings(self.tmp.name, media_type="video", video_quality="720p"), "s")
        new = await self.manager.retry(old.id, "s", settings(self.tmp.name))
        self.assertEqual(("video", "720p"), (new.settings["media_type"], new.settings["video_quality"]))


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracks_saved_for_review_are_counted_as_such(self):
        """It said "0 needs review" beside a review report listing every track."""
        from app.review_report import TrackOutcome
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job("x", "youtube", None, "", tracks=[Track("A", "B")], kind="bulk",
                      settings=settings(temp_dir))

            async def fetch(_job, track, *_args):
                return TrackOutcome(track, "downloaded_for_review", "score 80", [], saved_as="A - B.opus")

            with patch.object(manager, "_fetch_track", side_effect=fetch), \
                 patch("app.jobs.write_review_report", return_value=Path(temp_dir) / "r.html"):
                await manager._run_tracklist_job(job)

        self.assertIn("1 downloaded", job.output)
        self.assertIn("(1 to review)", job.output)
        self.assertIn("0 failed", job.output)


class ErrorSummaryTests(unittest.TestCase):
    def test_the_useful_part_of_a_download_error_is_kept(self):
        self.assertEqual("HTTP Error 403: Forbidden",
                         _short_error(["ERROR: [youtube] x: HTTP Error 403: Forbidden"]))
        self.assertEqual("YouTube bot check - cookies needed",
                         _short_error(["ERROR: [youtube] x: Sign in to confirm you're not a bot"]))
        self.assertEqual("", _short_error([]))


def request(sid="s1"):
    return types.SimpleNamespace(state=types.SimpleNamespace(sid=sid), cookies={}, headers={},
                                 client=types.SimpleNamespace(host="203.0.113.9"))


class ApiConsistencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = MagicMock()
        self.manager.check_limit.return_value = None
        self.manager.submit_bulk = AsyncMock(return_value=MagicMock(to_dict=lambda: {"id": "bulk"}))
        self.manager.submit = AsyncMock(return_value=MagicMock(to_dict=lambda: {"id": "link"}))
        patches = [
            patch("app.main.manager", self.manager),
            patch("app.main.settings_mod.effective_settings", return_value={"audio_format": "opus"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def locked(self):
        return patch("app.main.settings_mod.gate_enabled", return_value=True)

    async def test_a_soundcloud_only_list_needs_no_passphrase_like_a_soundcloud_link(self):
        entries = [{"kind": "search", "artist": "A", "title": "B"}]
        with self.locked():
            ok = await main.bulk_download(request(), {"entries": entries, "service": "soundcloud"})
            refused = await main.bulk_download(request(), {"entries": entries, "service": "auto"})
        self.assertEqual({"id": "bulk"}, ok["jobs"][0])
        self.assertEqual(403, refused.status_code)

    async def test_a_typed_soundcloud_search_needs_no_passphrase_either(self):
        with self.locked():
            ok = await main.download(request(), {"input": "A - B", "engine_override": "soundcloud"})
            refused = await main.download(request(), {"input": "A - B"})
        self.assertEqual({"id": "link"}, ok)
        self.assertEqual(403, refused.status_code)

    async def test_a_video_quality_is_never_accepted_as_an_audio_format(self):
        """Bulk from Video mode sent "1080p" as the audio format and every track failed."""
        await main.bulk_download(request(), {"entries": [{"kind": "search", "artist": "A", "title": "B"}],
                                             "format": "1080p"})
        sent = self.manager.submit_bulk.await_args.args[1]
        self.assertEqual("opus", sent["audio_format"])

    async def test_pasted_links_are_each_held_to_the_download_limit(self):
        """One paste of many links used to become that many jobs past a 3-at-a-time cap."""
        verdicts = iter([None, None, "Too many active downloads", "Too many active downloads"])
        self.manager.check_limit.side_effect = lambda *a, **k: next(verdicts)
        with patch("app.main.settings_mod.gate_enabled", return_value=True), \
             patch("app.main._unlocked", return_value=True), \
             patch("app.main._tier", return_value="user"):
            res = await main.bulk_download(request(), {"entries": [
                {"kind": "url", "url": f"https://soundcloud.com/a/{i}"} for i in range(3)]})
        self.assertEqual((1, 2), (res["links"], res["links_held_back"]))
        self.assertEqual(1, self.manager.submit.await_count)

    async def test_the_label_toggle_cannot_force_stripping_a_real_title(self):
        res = await main.bulk_preview(request(), {"text": "Black Betty - Single Edit by Spiderbait",
                                                  "strip_labels": True})
        self.assertEqual("Black Betty - Single Edit", res["entries"][0]["title"])

    async def test_retrying_a_missing_job_is_a_clear_404(self):
        self.manager.jobs = {}
        res = await main.retry_job(request(), "nope")
        self.assertEqual(404, res.status_code)


if __name__ == "__main__":
    unittest.main()
