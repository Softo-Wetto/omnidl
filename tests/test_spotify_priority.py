"""Spotify metadata priority and safe audio fallback; provider I/O is mocked."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app import candidate_search, spotify_resolver as sr
from app.jobs import Job, JobManager
from app.matching import Candidate
from app.provenance import Provenance
from support import write_audio_output


def song(title="Midnight Run", artist="Nova", **extra):
    return {"name": title, "artists": [{"name": artist}], "duration_ms": 180000,
            "album": {"name": "Night Drive", "images": [{"url": "cover.jpg"}],
                      "release_date": "2026-01-02"}, "track_number": 3, **extra}


class SpotifyMetadataPriorityTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.free = patch("SpotipyFree.Spotify", return_value=self.client).start()
        self.addCleanup(patch.stopall)
        # A usable independent fallback, intentionally distinguishable from the preferred data.
        patch.object(sr, "_fetch_embed", return_value="embed").start()
        self.embed = patch.object(sr, "_parse_embed", return_value=("Old name", [sr.Track("Old", "Old song")])).start()
        patch.object(sr, "_token", return_value=None).start()

    def test_track_uses_spotipyfree_and_preserves_rich_metadata(self):
        self.client.track.return_value = song()
        result = sr.resolve("https://open.spotify.com/track/abc123")
        track = result.tracks[0]
        self.assertEqual(("Nova", "Midnight Run", 180, "Night Drive", 3, "2026-01-02"),
                         (track.artist, track.title, track.duration, track.album, track.track_number, track.date))
        self.assertEqual(["Nova"], track.artists)
        self.assertEqual("cover.jpg", track.cover_url)
        self.assertFalse(result.truncated)

    def test_playlist_uses_complete_library_response_beyond_embed_limit(self):
        self.client.playlist.return_value = {"name": "All songs", "content": {"totalCount": 240}}
        self.client.playlist_items.return_value = {
            "items": [{"track": song(f"Song {i}")} for i in range(240)], "total": 240, "next": False}
        result = sr.resolve("spotify:playlist:abc123")
        self.assertEqual(("All songs", 240, "Song 239"), (result.name, len(result.tracks), result.tracks[-1].title))
        self.assertFalse(result.truncated)

    def test_album_reads_remaining_pages_and_applies_album_metadata(self):
        self.client.album.return_value = {"name": "The album", "images": [{"url": "album.jpg"}],
                                          "release_date": "2025-10-10", "total_tracks": 3}
        self.client.album_tracks.side_effect = [
            {"items": [song("A", album=None), song("B", album=None)], "total": 3, "next": "page2"},
            {"items": [song("C", album=None)], "total": 3, "next": None},
        ]
        result = sr.resolve("https://open.spotify.com/album/abc123")
        self.assertEqual(["A", "B", "C"], [t.title for t in result.tracks])
        self.assertEqual(("The album", "album.jpg", "2025-10-10"),
                         (result.tracks[-1].album, result.tracks[-1].cover_url, result.tracks[-1].date))

    def test_incomplete_primary_is_filled_in_fallback_order_with_preferred_metadata(self):
        self.client.playlist.return_value = {"name": "Preferred", "content": {"totalCount": 3}}
        self.client.playlist_items.return_value = {"items": [{"track": song("A")}, {"track": song("C")}],
                                                  "total": 2, "next": False}
        self.embed.return_value = ("Fallback", [sr.Track("Nova", t) for t in ("A", "B", "C")])
        result = sr.resolve("spotify:playlist:abc123")
        self.assertEqual(["A", "B", "C"], [t.title for t in result.tracks])
        self.assertEqual("Night Drive", result.tracks[0].album)
        self.assertEqual("Preferred", result.name)
        self.assertFalse(result.truncated)

    def test_partial_recovery_keeps_intentional_duplicate_rows(self):
        self.client.playlist.return_value = {"name": "Repeats", "content": {"totalCount": 3}}
        self.client.playlist_items.return_value = {"items": [{"track": song("A")}], "total": 1, "next": False}
        self.embed.return_value = ("Repeats", [sr.Track("Nova", t) for t in ("A", "B", "A")])
        result = sr.resolve("spotify:playlist:abc123")
        self.assertEqual(["A", "B", "A"], [t.title for t in result.tracks])
        self.assertFalse(result.truncated)

    def test_primary_failure_uses_web_metadata(self):
        self.client.track.side_effect = RuntimeError("provider unavailable")
        result = sr.resolve("spotify:track:abc123")
        self.assertEqual("Old song", result.tracks[0].title)

    def test_recovery_failure_keeps_partial_primary_and_marks_incomplete(self):
        self.client.playlist.return_value = {"name": "Partial", "content": {"totalCount": 3}}
        self.client.playlist_items.return_value = {"items": [{"track": song("A")}], "total": 1, "next": False}
        self.embed.side_effect = ValueError("web unavailable")
        result = sr.resolve("spotify:playlist:abc123")
        self.assertEqual(["A"], [t.title for t in result.tracks])
        self.assertTrue(result.truncated)

    def test_no_token_at_embed_cap_is_still_marked_incomplete(self):
        self.client.playlist.side_effect = RuntimeError("unavailable")
        self.embed.return_value = ("Capped", [sr.Track("Nova", f"Song {i}") for i in range(100)])
        result = sr.resolve("spotify:playlist:abc123")
        self.assertEqual(100, len(result.tracks))
        self.assertTrue(result.truncated)


class SpotifyAudioPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, candidates, failures=0, retry_from_history=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        manager = JobManager()
        manager.owners = Provenance(root / "provenance.json")
        settings = {"output_dir": str(root), "audio_format": "opus", "skip_existing": False}
        job = Job("spotify:track:abc123", "spotify", None, "", spotify_url="spotify:track:abc123", settings=settings)
        if retry_from_history:
            job.status = "done"
            job.failed_tracks = [sr.Track("Nova", "Midnight Run", 180)]
            job = Job.from_record(job.to_record())
            manager.jobs[job.id] = job
            manager._persist = lambda: None
            with patch("app.jobs.settings_mod.LOCAL_MODE", True):
                job = await manager.retry(job.id, "", settings, failed_only=True)
            settings = job.settings
        job.status = "running"
        attempts = []

        async def download(_job, argv, **kwargs):
            attempts.append(argv[-1])
            if len(attempts) <= failures:
                return 1
            write_audio_output(argv)
            return 0

        with patch("app.jobs.candidate_search.search_all", return_value=candidates), \
             patch.object(manager, "_stream_subprocess", side_effect=download), \
             patch("app.jobs.asyncio.sleep", AsyncMock()):
            result = await manager._fetch_track(job, sr.Track("Nova", "Midnight Run", 180), settings, False, 1, 1)
        return result, attempts

    async def test_spotify_prefers_verified_music_over_higher_scored_youtube(self):
        music = Candidate("youtube_music", "https://music.youtube.com/watch?v=music", "Midnight Run", "Nova", 189, True)
        video = Candidate("youtube", "https://www.youtube.com/watch?v=video", "Midnight Run", "Nova", 180, True)
        result, attempts = await self.fetch([video, music])
        self.assertEqual("downloaded", result.status)
        self.assertEqual([music.url], attempts)

    async def test_unavailable_music_download_falls_back_to_same_youtube_recording(self):
        music = Candidate("youtube_music", "https://music.youtube.com/watch?v=music", "Midnight Run", "Nova", 180, True)
        video = Candidate("youtube", "https://www.youtube.com/watch?v=video", "Midnight Run", "Nova", 180, True)
        result, attempts = await self.fetch([music, video], failures=3)
        self.assertEqual("downloaded", result.status)
        self.assertEqual([music.url, music.url, music.url, video.url], attempts)

    async def test_retry_failed_spotify_tracks_keeps_music_priority_after_restart(self):
        music = Candidate("youtube_music", "https://music.youtube.com/watch?v=music", "Midnight Run", "Nova", 189, True)
        video = Candidate("youtube", "https://www.youtube.com/watch?v=video", "Midnight Run", "Nova", 180, True)
        result, attempts = await self.fetch([video, music], retry_from_history=True)
        self.assertEqual("downloaded", result.status)
        self.assertEqual([music.url], attempts)

    async def test_priority_never_accepts_a_wrong_version(self):
        live = Candidate("youtube_music", "https://music.youtube.com/watch?v=live", "Midnight Run (Live)", "Nova", 180, True)
        video = Candidate("youtube", "https://www.youtube.com/watch?v=studio", "Midnight Run", "Nova", 180, True)
        result, attempts = await self.fetch([live, video])
        self.assertEqual("downloaded", result.status)
        self.assertEqual([video.url], attempts)

    async def test_many_music_candidates_do_not_crowd_out_youtube_fallback(self):
        music = [Candidate("youtube_music", f"https://music.youtube.com/watch?v=music{i}",
                           "Midnight Run", "Nova", 180, True) for i in range(5)]
        video = Candidate("youtube", "https://www.youtube.com/watch?v=backup", "Midnight Run", "Nova", 180, True)
        result, attempts = await self.fetch([*music, video], failures=15)
        self.assertEqual("downloaded", result.status)
        self.assertEqual(video.url, attempts[-1])

    def test_music_search_links_use_music_host(self):
        found = candidate_search.candidates_from_ytmusic([
            {"videoId": "abc", "title": "Song", "artists": [{"name": "Nova"}], "duration_seconds": 180}])
        self.assertEqual("https://music.youtube.com/watch?v=abc", found[0].url)
