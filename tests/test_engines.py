import unittest

from app.engines import media_url_command


class EngineCommandTests(unittest.TestCase):
    def test_mp3_downloads_use_compact_high_quality_bitrate(self):
        command = media_url_command(
            "https://www.youtube.com/watch?v=abc123",
            "Artist - Track",
            {"output_dir": "downloads", "audio_format": "mp3"},
        )

        quality_index = command.index("--audio-quality")
        self.assertEqual("192K", command[quality_index + 1])


if __name__ == "__main__":
    unittest.main()
