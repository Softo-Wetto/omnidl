"""200+ song playlists: every track is read, searched, retried and counted.

Spotify, the searches and the downloads are all mocked — nothing here touches the network.
"""
import asyncio
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError

from app import candidate_search, spotify_resolver as sr
from app.jobs import Job, JobManager
from app.matching import Candidate
from app.review_report import TrackOutcome
from app.spotify_resolver import Track
from support import write_output


def page(offset, size, total):
    items = [{"itemV2": {"data": {
        "name": f"Song {n}", "artists": {"items": [{"profile": {"name": "Nova"}}]},
        "trackDuration": {"totalMilliseconds": 180_000}}}} for n in range(offset, min(offset + size, total))]
    return {"data": {"playlistV2": {"content": {"items": items, "totalCount": total}}}}


def limited():
    headers = Message()
    headers["Retry-After"] = "0"
    return HTTPError("https://api-partner.spotify.com", 429, "Too Many Requests", headers, None)


class PlaylistPagingTests(unittest.TestCase):
    def test_a_650_song_playlist_survives_a_rate_limited_page(self):
        calls = []

        def fetch(pid, token, offset, limit, timeout):
            calls.append(offset)
            if offset == 300 and calls.count(300) == 1:
                raise limited()                       # one blip on page 4
            return page(offset, limit, 650)

        with patch("app.spotify_resolver._partner_page_once", side_effect=fetch), \
             patch("app.spotify_resolver.time.sleep") as sleep:
            tracks, total = sr._partner_playlist("pid", "token", 20)
        self.assertEqual((650, 650), (len(tracks), total))
        self.assertEqual(["Song 0", "Song 649"], [tracks[0].title, tracks[-1].title])
        sleep.assert_called_once_with(0.0)            # Retry-After honoured

    def test_a_rotated_query_id_is_refreshed_once(self):
        answers = [{"errors": [{"message": "PersistedQueryNotFound"}]}, page(0, 100, 50)]
        with patch("app.spotify_resolver._partner_page_once", side_effect=answers), \
             patch.dict(sr._HASH_CACHE, {"h": "stale"}, clear=True):
            tracks, _ = sr._partner_playlist("pid", "token", 20)
            self.assertNotIn("h", sr._HASH_CACHE)      # forced a re-scrape
        self.assertEqual(50, len(tracks))

    def test_a_page_that_keeps_failing_keeps_everything_before_it(self):
        """It used to drop to the embed's 100; now a 300-song playlist that fails at 250 gets 250."""
        def fetch(pid, token, offset, limit, timeout):
            if offset >= 200:
                raise limited()
            return page(offset, limit, 300)

        embed = [Track("Nova", f"Song {n}") for n in range(100)]
        with patch("app.spotify_resolver._partner_page_once", side_effect=fetch), \
             patch("app.spotify_resolver.time.sleep"), \
             patch("app.spotify_resolver._fetch_embed", return_value="embed"), \
             patch("app.spotify_resolver._parse_embed", return_value=("Big", embed)), \
             patch("app.spotify_resolver._token", return_value="token"):
            resolved = sr.resolve("https://open.spotify.com/playlist/abc123")
        self.assertEqual(200, len(resolved.tracks))
        self.assertTrue(resolved.truncated)

    def test_a_permanent_error_is_not_retried(self):
        forbidden = HTTPError("https://api-partner.spotify.com", 401, "Unauthorized", Message(), None)
        with patch("app.spotify_resolver._partner_page_once", side_effect=forbidden) as fetch, \
             patch("app.spotify_resolver.time.sleep") as sleep:
            with self.assertRaises(HTTPError):
                sr._partner_playlist("pid", "token", 20)
        self.assertEqual(1, fetch.call_count)
        sleep.assert_not_called()


class SearchReportingTests(unittest.TestCase):
    def test_a_source_that_errors_is_reported_not_silently_empty(self):
        with patch("app.candidate_search.ytmusic_match.search_songs_or_raise", side_effect=RuntimeError("429")), \
             patch("app.candidate_search._ytdlp_or_raise", return_value=[]):
            results = candidate_search.search_all("Nova", "Midnight Run", "auto")
        self.assertEqual([], list(results))
        self.assertEqual(("youtube_music",), results.failed)

    def test_the_second_search_uses_the_plain_song_name_and_more_results(self):
        with patch("app.candidate_search.ytmusic_match.search_songs_or_raise", return_value=[]) as ytm, \
             patch("app.candidate_search._ytdlp_or_raise", return_value=[]) as ytdlp:
            candidate_search.search_all("Queen", "Bohemian Rhapsody - Remastered 2011", "auto", deep=True)
        ytm.assert_called_once_with("Queen", "Bohemian Rhapsody", limit=10)
        self.assertEqual(("Queen Bohemian Rhapsody audio", "ytsearch", "youtube", 10), ytdlp.call_args_list[0].args)


def settings(tmp, **extra):
    return {"output_dir": tmp, "skip_existing": False, "audio_format": "opus", **extra}


class SecondChanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_song_with_no_verified_match_is_searched_again_and_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager()
            job = Job("x", "spotify", None, "", settings=settings(tmp))
            video = Candidate("youtube", "https://yt/video", "Nova - Midnight Run (Official Video)", "Nova", 240)
            audio = Candidate("youtube_music", "https://yt/audio", "Midnight Run", "Nova", 180, True)
            target = Path(tmp) / "Nova - Midnight Run.opus"

            def search(artist, title, service, deep=False):
                return [audio] if deep else [video]

            async def download(_job, argv, emit, **kwargs):
                write_output(argv)
                return 0

            with patch("app.jobs.candidate_search.search_all", side_effect=search), \
                 patch.object(manager, "_stream_subprocess", side_effect=download):
                result = await manager._fetch_track(job, Track("Nova", "Midnight Run", 180), job.settings, True, 1, 1)
        self.assertEqual(("downloaded", "https://yt/audio"), (result.status, result.selected.candidate.url))

    async def test_a_search_that_didnt_answer_is_retried_at_the_end_and_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager()
            manager._persist = lambda: None
            tracks = [Track("Nova", f"Song {n}", 180) for n in range(3)]
            job = Job("x", "youtube", None, "", tracks=tracks, kind="bulk",
                      settings=settings(tmp, concurrency=1))
            answered = {"n": 0}

            def search(artist, title, service, deep=False):
                if title == "Song 1" and answered["n"] < 2:     # first search + second wording
                    answered["n"] += 1
                    return candidate_search.SearchResults([], ["youtube_music", "youtube", "soundcloud"])
                return [Candidate("youtube_music", f"https://yt/{title}", title, "Nova", 180, True)]

            async def download(_job, argv, emit, **kwargs):
                write_output(argv)
                return 0

            with patch("app.jobs.candidate_search.search_all", side_effect=search), \
                 patch.object(manager, "_stream_subprocess", side_effect=download), \
                 patch.object(JobManager, "_sleep_unless_cancelled", AsyncMock()):
                job.status = "running"
                await manager._run_tracklist_job(job)
        self.assertEqual({"downloaded": 3, "review": 0, "skipped": 0, "failed": 0, "temporary": 0,
                          "total": 3}, job.counts)
        self.assertIn("retrying 1 track", job.output)


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_youtube_rate_limit_pauses_downloads_instead_of_failing_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager()
            job = Job("x", "spotify", None, "", settings=settings(tmp))
            audio = Candidate("youtube_music", "https://yt/audio", "Midnight Run", "Nova", 180, True)
            target = Path(tmp) / "Nova - Midnight Run.opus"
            attempts = {"n": 0}
            waits = []

            async def download(_job, argv, emit, errors=None, **kwargs):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    errors.append("ERROR: [youtube] x: HTTP Error 429: Too Many Requests")
                    return 1
                write_output(argv)
                return 0

            async def sleep(_job, seconds):
                waits.append(round(seconds))

            with patch("app.jobs.candidate_search.search_all", return_value=[audio]), \
                 patch.object(manager, "_stream_subprocess", side_effect=download), \
                 patch.object(JobManager, "_sleep_unless_cancelled", side_effect=sleep), \
                 patch("app.jobs.asyncio.sleep", AsyncMock()):
                result = await manager._fetch_track(job, Track("Nova", "Midnight Run", 180), job.settings, True, 1, 1)
        self.assertEqual("downloaded", result.status)
        self.assertIn(30, waits)                       # paused ~30s before trying again
        self.assertIn("rate-limiting", job.output)


class TwoHundredFiftyTracksTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_250_song_list_runs_every_track_with_live_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JobManager()
            manager._persist = lambda: None
            sent = []

            async def send(_job, message):
                sent.append(message)

            manager._send = send
            tracks = [Track("Nova", f"Song {n}", 180) for n in range(250)]
            job = Job("x", "youtube", None, "", tracks=tracks, kind="bulk", list_name="Big",
                      settings=settings(tmp, concurrency=4))
            running = {"now": 0, "max": 0}

            async def fetch(_job, track, *_args):
                running["now"] += 1
                running["max"] = max(running["max"], running["now"])
                await asyncio.sleep(0)
                running["now"] -= 1
                n = int(track.title.split()[1])
                status = "no_candidate" if n % 50 == 7 else "skipped" if n % 10 == 3 else "downloaded"
                return TrackOutcome(track, status, "", [])

            with patch.object(manager, "_fetch_track", side_effect=fetch):
                job.status = "running"
                await manager._run_tracklist_job(job)
        self.assertEqual({"downloaded": 220, "review": 0, "skipped": 25, "failed": 5, "temporary": 0,
                          "total": 250}, job.counts)
        self.assertEqual(("done", 100), (job.status, job.progress))
        self.assertLessEqual(running["max"], 4)
        live = [m["counts"] for m in sent if m.get("type") == "progress" and m.get("counts")]
        self.assertEqual(250, len(live))
        self.assertEqual(250, live[-1]["downloaded"] + live[-1]["skipped"] + live[-1]["failed"])
        self.assertEqual(5, len(job.failed_tracks))
        record = json.dumps(job.to_record())
        self.assertLess(len(record), 200_000)          # history stays small


if __name__ == "__main__":
    unittest.main()
