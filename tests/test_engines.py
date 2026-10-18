import unittest

from app.engines import media_url_command


class EngineCommandTests(unittest.TestCase):
    def test_mp3_downloads_default_to_balanced_variable_bitrate(self):
        """Phone-sized by default: LAME -V4, about 1.1 MB a minute from YouTube audio."""
        command = media_url_command(
            "https://www.youtube.com/watch?v=abc123",
            "Artist - Track",
            {"output_dir": "downloads", "audio_format": "mp3"},
        )

        quality_index = command.index("--audio-quality")
        self.assertEqual("4", command[quality_index + 1])


if __name__ == "__main__":
    unittest.main()
