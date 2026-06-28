import unittest

from app.candidate_search import candidates_from_ytdlp, candidates_from_ytmusic


class CandidateSearchTests(unittest.TestCase):
    def test_converts_youtube_music_song_to_official_candidate(self):
        items = [{
            "videoId": "abc",
            "title": "Midnight Run",
            "artists": [{"name": "Nova"}],
            "duration_seconds": 180,
        }]

        result = candidates_from_ytmusic(items)

        self.assertEqual("youtube_music", result[0].source)
        self.assertTrue(result[0].official)
        self.assertEqual("https://www.youtube.com/watch?v=abc", result[0].url)

    def test_converts_soundcloud_flat_search_entry_to_candidate(self):
        items = [{
            "webpage_url": "https://soundcloud.com/nova/midnight-run",
            "title": "Midnight Run",
            "uploader": "Nova",
            "duration": 180,
        }]

        result = candidates_from_ytdlp(items, "soundcloud")

        self.assertEqual("soundcloud", result[0].source)
        self.assertEqual("Nova", result[0].artist)


if __name__ == "__main__":
    unittest.main()
