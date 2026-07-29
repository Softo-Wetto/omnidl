import unittest
from html.parser import HTMLParser

from app.main import dashboard


async def response_body(response) -> bytes:
    """Run a Starlette response as ASGI and return the bytes a browser receives."""
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await response(
        {"type": "http", "method": "GET", "path": "/dashboard", "headers": []},
        receive,
        send,
    )
    return b"".join(message.get("body", b"") for message in messages)

class HeaderSemantics(HTMLParser):
    def __init__(self):
        super().__init__()
        self.navigation_labels = []
        self.action_ids = set()
        self.utility_groups = 0
        self.navigation_icons = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "nav":
            self.navigation_labels.append(attributes.get("aria-label"))
        if attributes.get("id") in {
            "open-library", "open-folder", "open-settings",
        } or (tag == "a" and attributes.get("href") == "/"):
            self.action_ids.add(attributes.get("id") or "home")
        if "header-utilities" in attributes.get("class", "").split():
            self.utility_groups += 1
        if tag == "svg" and "nav-icon-svg" in attributes.get("class", "").split():
            self.navigation_icons += 1

class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_sends_real_unicode_icons(self):
        body = await response_body(await dashboard())

        self.assertIn("\u2600".encode("utf-8"), body)
        self.assertIn(b'<svg class="nav-icon-svg"', body)
        self.assertIn("\U0001f50e".encode("utf-8"), body)
        self.assertNotIn("\u00e2\u02dc\u20ac".encode("utf-8"), body)

    async def test_dashboard_groups_navigation_actions_with_consistent_icons(self):
        parser = HeaderSemantics()
        parser.feed((await response_body(await dashboard())).decode("utf-8-sig"))

        self.assertIn("Dashboard navigation", parser.navigation_labels)
        self.assertEqual(
            {"home", "open-library", "open-folder", "open-settings"},
            parser.action_ids,
        )
        self.assertEqual(1, parser.utility_groups)
        self.assertEqual(4, parser.navigation_icons)

if __name__ == "__main__":
    unittest.main()
