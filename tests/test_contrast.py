"""Text colours stay readable (WCAG AA, 4.5:1) on every panel colour, in both themes."""
import re
import unittest
from pathlib import Path

CSS = (Path(__file__).parents[1] / "app" / "static" / "css" / "style.css").read_text(encoding="utf-8")
TEXT_TOKENS = ("text", "muted", "dim", "accent", "green", "red", "yellow")
SURFACES = ("panel", "panel-2", "bg", "bg-2")


def tokens(selector: str) -> dict[str, str]:
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", CSS, re.S).group(1)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ContrastTests(unittest.TestCase):
    def test_text_colours_are_readable_in_both_themes(self):
        dark = tokens(":root")
        light = {**dark, **tokens("html.light")}
        for name, theme in (("dark", dark), ("light", light)):
            for fg in TEXT_TOKENS:
                for bg in SURFACES:
                    with self.subTest(theme=name, text=fg, on=bg):
                        self.assertGreaterEqual(contrast(theme[fg], theme[bg]), 4.5)

    def test_white_text_on_the_accent_button_is_readable(self):
        light = {**tokens(":root"), **tokens("html.light")}
        self.assertGreaterEqual(contrast("#ffffff", light["accent"]), 4.5)
