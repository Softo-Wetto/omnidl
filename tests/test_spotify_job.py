import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.jobs import Job, JobManager
from app.library import LibraryIndex, LibraryTrack
from app.matching import Candidate, MatchDecision
from app.review_report import TrackOutcome
from app.spotify_resolver import Resolved, Track


class SpotifyJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_candidate_is_attempted_before_higher_scored_unverified_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job(
                "spotify:test", "spotify", None, "",
                settings={"output_dir": temp_dir, "skip_existing": False, "audio_format": "opus"},
            )
            unverified = Candidate("youtube", "https://youtube.test/unverified", "Near Title", "Nova", 180)
            verified = Candidate("youtube_music", "https://youtube.test/verified", "Midnight Run", "Nova", 180, True)
            decisions = [
                MatchDecision(unverified, 0.95, 1.0, 0, 99, False, "duration differs by more than 20 seconds"),
                MatchDecision(verified, 1.0, 1.0, 0, 85, True, "verified match"),
            ]
            target = Path(temp_dir) / "Nova - Midnight Run.opus"

            attempts = []

            async def download(_job, argv, emit, **_kwargs):
                attempts.append(argv[-1])
                target.touch()
                return 0

            with patch("app.jobs.candidate_search.search_all", return_value=[unverified, verified]), \
                 patch("app.jobs.decide_match", side_effect=decisions), \
                 patch.object(manager, "_stream_subprocess", side_effect=download):
                result = await manager._fetch_track(job, Track("Nova", "Midnight Run", 180), job.settings, False, 1, 1)

        self.assertEqual("downloaded", result.status)
    async def test_plausible_nonexact_candidate_is_downloaded_and_marked_for_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job(
                "spotify:test", "spotify", None, "",
                settings={"output_dir": temp_dir, "skip_existing": False, "audio_format": "opus"},
            )
            # Same song, but extra words drop title similarity below the verified bar.
            nonexact = Candidate(
                "youtube", "https://youtube.test/x", "Midnight Run (feat. Echo)", "Nova", 180,
            )
            target = Path(temp_dir) / "Nova - Midnight Run.opus"

            async def download(_job, _argv, emit, **_kwargs):
                target.touch()
                return 0

            with patch("app.jobs.candidate_search.search_all", return_value=[nonexact]), \
                 patch.object(manager, "_stream_subprocess", side_effect=download) as run:
                result = await manager._fetch_track(job, Track("Nova", "Midnight Run", 180), job.settings, False, 1, 1)

        self.assertEqual("downloaded_for_review", result.status)
        run.assert_awaited_once()
        self.assertIn("saved for review", job.output)

    async def test_wrong_song_is_never_downloaded(self):
        """A same-artist, same-length but different-titled track must NOT be saved."""
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job(
                "spotify:test", "spotify", None, "",
                settings={"output_dir": temp_dir, "skip_existing": False, "audio_format": "opus"},
            )
            wrong = Candidate(
                "youtube_music", "https://youtube.test/x", "Boogie (Slowed)", "Nova", 180,
            )

            async def download(_job, _argv, emit, **_kwargs):
                (Path(temp_dir) / "Nova - Midnight Run.opus").touch()
                return 0

            with patch("app.jobs.candidate_search.search_all", return_value=[wrong]), \
                 patch.object(manager, "_stream_subprocess", side_effect=download) as run:
                result = await manager._fetch_track(job, Track("Nova", "Midnight Run", 180), job.settings, False, 1, 1)

        self.assertEqual("download_failed", result.status)
        run.assert_not_awaited()  # never even attempted

    async def test_existing_cross_format_track_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            existing_path = Path(temp_dir) / "archive" / "Nova - Midnight Run.mp3"
            existing = LibraryTrack(
                path=existing_path,
                relative_path="archive/Nova - Midnight Run.mp3",
                title="Midnight Run",
                artist="Nova",
                album="Night Drive",
                duration=180,
                bitrate=192_000,
                sample_rate=44_100,
                channels=2,
                codec="mp3",
                size=4_000_000,
                has_artwork=True,
                issues=(),
            )
            settings = {
                "output_dir": temp_dir,
                "skip_existing": True,
                "audio_format": "opus",
                "_library_index": LibraryIndex([existing]),
            }
            job = Job("spotify:test", "spotify", None, "", settings=settings)

            with patch(
                "app.jobs.candidate_search.search_all",
                side_effect=AssertionError("source search should not run for an existing track"),
            ):
                result = await manager._fetch_track(
                    job, Track("Nova", "Midnight Run", 180), settings, False, 1, 1,
                )

        self.assertEqual("skipped", result.status)
        self.assertIn("archive/Nova - Midnight Run.mp3", result.reason)
    async def test_transient_failure_is_recovered_by_retry_sweep(self):
        """A track that fails to download during the burst is retried after it and saved."""
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job("spotify:test", "spotify", None, "", spotify_url="spotify:test",
                      settings={"output_dir": temp_dir, "skip_existing": False, "concurrency": 1})
            dec = MatchDecision(
                Candidate("youtube_music", "https://yt/x", "Song", "Artist", 180),
                1.0, 1.0, 0, 100, True, "verified match",
            )
            calls = {"n": 0}

            async def fetch(_job, track, _settings, _detailed, _index, _total):
                calls["n"] += 1
                if calls["n"] == 1:   # first (burst) attempt fails transiently
                    return TrackOutcome(track, "download_failed", "all candidate downloads failed",
                                        [dec], failed_attempts=(dec,))
                return TrackOutcome(track, "downloaded", "verified match", [dec], selected=dec,
                                    saved_as="Artist - Song.opus")

            async def no_sleep(_):
                return None

            with patch("app.spotify_resolver.resolve",
                       return_value=Resolved("playlist", "Test", [Track("Artist", "Song", 180)])), \
                 patch.object(manager, "_fetch_track", side_effect=fetch), \
                 patch("app.jobs.asyncio.sleep", side_effect=no_sleep):
                await manager._run_spotify_job(job)

        self.assertEqual(2, calls["n"])            # tried again after the burst
        self.assertEqual("done", job.status)
        self.assertEqual(0, job.code)              # no failures left
        self.assertIn("1 downloaded", job.output)

    async def test_unresolved_outcomes_write_report_and_emit_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job("spotify:test", "spotify", None, "", settings={"output_dir": temp_dir})
            outcome = TrackOutcome(Track("Nova", "Missing Song", 180), "no_candidate", "no candidate found", [])
            path = Path(temp_dir) / "omnidl-review-test.html"
            with patch("app.jobs.write_review_report", return_value=path):
                await manager._emit_review_report(job, Path(temp_dir), "Test playlist", [outcome])

        self.assertIn(str(path), job.output)


if __name__ == "__main__":
    unittest.main()