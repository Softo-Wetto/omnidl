import unittest
from email.message import Message
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from app.spotify_resolver import Track, _api, resolve


class SpotifyResolverTests(unittest.TestCase):
    def test_api_retries_rate_limit_then_returns_playlist_page(self):
        headers = Message()
        headers["Retry-After"] = "0"
        limited = HTTPError("https://api.spotify.com/test", 429, "Too Many Requests", headers, None)
        response = MagicMock()
        response.read.return_value = b'{"total": 101}'

        with patch("app.spotify_resolver.urllib.request.urlopen", side_effect=[limited, response]) as urlopen, \
             patch("time.sleep") as sleep:
            self.assertEqual({"total": 101}, _api("https://api.spotify.com/test", "token"))

        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0.0)

    def test_does_not_silently_truncate_a_rate_limited_large_playlist(self):
        embed_tracks = [Track("Artist", f"Track {number}") for number in range(100)]

        with patch("app.spotify_resolver._fetch_embed", return_value="embed"), \
             patch("app.spotify_resolver._parse_embed", return_value=("Large playlist", embed_tracks)), \
             patch("app.spotify_resolver._token", return_value="token"), \
             patch("app.spotify_resolver._api_playlist", side_effect=RuntimeError("rate limited")):
            with self.assertRaisesRegex(ValueError, "could not retrieve the complete playlist"):
                resolve("https://open.spotify.com/playlist/abc123")

if __name__ == "__main__":
    unittest.main()
