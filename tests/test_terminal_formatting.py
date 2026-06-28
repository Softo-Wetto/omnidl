import re
import unittest

from app.jobs import format_tool_output_line


def plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TerminalFormattingTests(unittest.TestCase):
    def test_formats_ytdlp_download_steps(self):
        samples = [
            "[youtube] Extracting URL: https://www.youtube.com/watch?v=Am_UfnV-niA",
            "[download] Destination: C:\\Users\\User\\Downloads\\Test\\Song.webm",
            "[ExtractAudio] Destination: C:\\Users\\User\\Downloads\\Test\\Song.opus",
            '[Metadata] Adding metadata to "C:\\Users\\User\\Downloads\\Test\\Song.opus"',
            '[EmbedThumbnail] mutagen: Adding thumbnail to "C:\\Users\\User\\Downloads\\Test\\Song.opus"',
        ]

        formatted = plain("".join(format_tool_output_line(line + "\n") for line in samples))

        self.assertIn("\U0001f50e Extracting YouTube URL", formatted)
        self.assertIn("\U0001f4e5 Saving media: Song.webm", formatted)
        self.assertIn("\U0001f3a7 Extracting audio: Song.opus", formatted)
        self.assertIn("\U0001f3f7 Adding metadata: Song.opus", formatted)
        self.assertIn("\U0001f5bc Embedding thumbnail: Song.opus", formatted)
        self.assertNotIn("[youtube]", formatted)
        self.assertNotIn("[ExtractAudio]", formatted)

    def test_keeps_progress_but_removes_raw_prefix(self):
        formatted = plain(format_tool_output_line("[download] 100% of    1.73MiB in 00:00:00 at 3.19MiB/s\n"))

        self.assertIn("\u23f3 100% of", formatted)
        self.assertNotIn("[download]", formatted)


if __name__ == "__main__":
    unittest.main()