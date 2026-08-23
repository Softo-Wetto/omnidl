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

        with (

              patch("app.spotify_resolver.urllib.request.urlopen", side_effect=[limited, response]) as urlopen,

              patch("time.sleep") as sleep,

        ):
            self.assertEqual({"total": 101}, _api("https://api.spotify.com/test", "token"))

        self.assertEqual(2, urlopen.call_count)
        # The server's own Retry-After wins, even when it says "retry immediately".
        sleep.assert_called_once_with(0.0)

    def test_api_backs_off_when_no_retry_after_header(self):
        """Without guidance, wait progressively rather than hammering a quota-limited API."""
        limited = HTTPError("https://api.spotify.com/test", 429, "Too Many Requests", Message(), None)
        response = MagicMock()
        response.read.return_value = b'{"total": 5}'

        with (

              patch("app.spotify_resolver.urllib.request.urlopen", side_effect=[limited, response]),

              patch("time.sleep") as sleep,

        ):
            _api("https://api.spotify.com/test", "token")

        self.assertEqual(2.0, sleep.call_args[0][0])

    def test_does_not_silently_truncate_a_rate_limited_large_playlist(self):
        """A rate-limited big playlist still yields the 100 tracks Spotify did return, but is
        flagged truncated so the caller can warn. The invariant is *not silent*: an earlier
        version raised instead, which meant a 150-track playlist downloaded nothing at all."""
        embed_tracks = [Track("Artist", f"Track {number}") for number in range(100)]

        with (

              patch("app.spotify_resolver._fetch_embed", return_value="embed"),

              patch("app.spotify_resolver._parse_embed", return_value=("Large playlist", embed_tracks)),

              patch("app.spotify_resolver._token", return_value="token"),

              patch("app.spotify_resolver._api_playlist", side_effect=RuntimeError("rate limited")),

        ):
            resolved = resolve("https://open.spotify.com/playlist/abc123")

        self.assertEqual(100, len(resolved.tracks))
        self.assertTrue(resolved.truncated, "truncation must be flagged, never silent")

    def test_complete_small_playlist_is_not_flagged_truncated(self):
        embed_tracks = [Track("Artist", f"Track {number}") for number in range(12)]

        with (

              patch("app.spotify_resolver._fetch_embed", return_value="embed"),

              patch("app.spotify_resolver._parse_embed", return_value=("Small playlist", embed_tracks)),

              patch("app.spotify_resolver._token", return_value=None),

        ):
            resolved = resolve("https://open.spotify.com/playlist/abc123")

        self.assertEqual(12, len(resolved.tracks))
        self.assertFalse(resolved.truncated)

    def test_full_api_list_wins_over_embed_preview(self):
        """When the API works, its complete list must replace the 100-track embed preview."""
        embed_tracks = [Track("Artist", f"Track {n}") for n in range(100)]
        full = [Track("Artist", f"Track {n}") for n in range(150)]

        with (

              patch("app.spotify_resolver._fetch_embed", return_value="embed"),

              patch("app.spotify_resolver._parse_embed", return_value=("Big", embed_tracks)),

              patch("app.spotify_resolver._token", return_value="token"),

              patch("app.spotify_resolver._api_playlist", return_value=full),

        ):
            resolved = resolve("https://open.spotify.com/playlist/abc123")

        self.assertEqual(150, len(resolved.tracks))
        self.assertFalse(resolved.truncated)


if __name__ == "__main__":
    unittest.main()
