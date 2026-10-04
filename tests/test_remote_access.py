"""The lock in front of a home OmniDL reached through Cloudflare Tunnel.

Local mode trusts every caller, so everything here is about one guarantee: a request that came
through Cloudflare gets in only with a genuine, current Cloudflare Access token for this app.
"""
import asyncio
import base64
import hashlib
import json
import random
import time
import types
import unittest
from unittest.mock import patch

from app import main, remote_access
from app.remote_access import AccessError, AccessVerifier, parse_access_redirect

TEAM = "withered-breeze-00ea"
AUD = "a" * 64
HOST = "omnidl-home.softowetto.com"


# ---- a throwaway RSA key, so tokens are signed for real rather than mocked ---------------------
def _probable_prime(bits: int, rng: random.Random) -> int:
    while True:
        n = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if all(n % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)) and _miller_rabin(n, rng):
            return n


def _miller_rabin(n: int, rng: random.Random, rounds: int = 24) -> bool:
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _keypair(seed: int):
    rng = random.Random(seed)
    e = 65537
    while True:
        p, q = _probable_prime(512, rng), _probable_prime(512, rng)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(n: int, d: int, message: bytes) -> bytes:
    k = (n.bit_length() + 7) // 8
    info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(info) - 3) + b"\x00" + info
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


KEY = _keypair(1)
OTHER_KEY = _keypair(2)


def token(claims=None, header=None, key=KEY, kid="k1"):
    n, _e, d = key
    head = {"alg": "RS256", "kid": kid, "typ": "JWT", **(header or {})}
    body = {"iss": f"https://{TEAM}.cloudflareaccess.com", "aud": [AUD], "email": "me@example.com",
            "exp": time.time() + 3600, "iat": time.time(), **(claims or {})}
    signing_input = f"{_b64(json.dumps(head).encode())}.{_b64(json.dumps(body).encode())}"
    return f"{signing_input}.{_b64(_sign(n, d, signing_input.encode()))}"


def jwks(key=KEY, kid="k1"):
    n, e, _d = key
    return {"keys": [{"kid": kid, "kty": "RSA", "alg": "RS256",
                      "n": _b64(n.to_bytes((n.bit_length() + 7) // 8, "big")),
                      "e": _b64(e.to_bytes(3, "big"))}]}


def verifier(**kwargs):
    fetches = []

    def fetch_json(url):
        fetches.append(url)
        return kwargs.pop("certs", None) or jwks()

    v = AccessVerifier(team=kwargs.get("team", TEAM), aud=kwargs.get("aud", AUD), hostname=HOST,
                       fetch_json=fetch_json, fetch_redirect=kwargs.get("redirect", lambda url: ""))
    v.fetches = fetches
    return v


def run(coro):
    return asyncio.run(coro)


class TokenTests(unittest.TestCase):
    def test_a_genuine_token_for_this_app_is_accepted(self):
        self.assertEqual("me@example.com", run(verifier().verify(token()))["email"])

    def test_aud_may_be_a_plain_string(self):
        run(verifier().verify(token({"aud": AUD})))

    def assertRefused(self, tok, v=None, why=""):
        with self.assertRaises(AccessError) as ctx:
            run((v or verifier()).verify(tok))
        if why:
            self.assertIn(why, str(ctx.exception))

    def test_no_token_is_refused(self):
        self.assertRefused("", why="login")

    def test_a_token_for_another_application_is_refused(self):
        """e.g. a friend's token for your Minecraft app, replayed against OmniDL."""
        self.assertRefused(token({"aud": ["b" * 64]}), why="different application")

    def test_a_token_from_another_cloudflare_team_is_refused(self):
        self.assertRefused(token({"iss": "https://someone-else.cloudflareaccess.com"}), why="different Cloudflare team")

    def test_an_expired_token_is_refused(self):
        self.assertRefused(token({"exp": time.time() - 3600}), why="expired")

    def test_a_token_without_an_expiry_is_refused(self):
        self.assertRefused(token({"exp": None}))

    def test_a_token_signed_with_someone_elses_key_is_refused(self):
        self.assertRefused(token(key=OTHER_KEY), why="signature")

    def test_an_edited_token_is_refused(self):
        head, body, sig = token().split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=="))
        claims["email"] = "attacker@example.com"
        forged = f"{head}.{_b64(json.dumps(claims).encode())}.{sig}"
        self.assertRefused(forged, why="signature")

    def test_unsigned_and_symmetric_algorithms_are_refused(self):
        """alg=none and HS256-keyed-with-the-public-key are the classic JWT forgeries."""
        for alg in ("none", "HS256", "RS512"):
            with self.subTest(alg=alg):
                self.assertRefused(token(header={"alg": alg}), why="algorithm")

    def test_a_token_naming_an_unknown_key_is_refused(self):
        self.assertRefused(token(kid="nope"), why="unknown key")

    def test_garbage_is_refused_not_crashed_on(self):
        for junk in ("abc", "a.b.c", "....", "e30.e30.e30"):
            with self.subTest(junk=junk):
                self.assertRefused(junk)

    def test_signing_keys_are_fetched_once_not_per_request(self):
        v = verifier()
        for _ in range(5):
            run(v.verify(token()))
        self.assertEqual(1, len(v.fetches))

    def test_a_flood_of_unknown_key_ids_cannot_hammer_cloudflare(self):
        v = verifier()
        run(v.verify(token()))
        for i in range(20):
            with self.assertRaises(AccessError):
                run(v.verify(token(kid=f"x{i}")))
        self.assertEqual(1, len(v.fetches))


class FailClosedTests(unittest.TestCase):
    def test_with_no_access_app_found_everything_is_refused(self):
        v = verifier(team="", aud="", redirect=lambda url: "")
        with self.assertRaises(AccessError) as ctx:
            run(v.verify(token()))
        self.assertIn("locked", str(ctx.exception))

    def test_the_access_app_is_learned_from_cloudflares_redirect(self):
        location = (f"https://{TEAM}.cloudflareaccess.com/cdn-cgi/access/login/{HOST}"
                    f"?kid={AUD}&meta=x&redirect_url=%2F")
        v = verifier(team="", aud="", redirect=lambda url: location)
        with patch("app.remote_access.save_config") as save:
            self.assertEqual("me@example.com", run(v.verify(token()))["email"])
        save.assert_called_once_with({"access_team": TEAM, "access_aud": AUD})

    def test_a_new_access_app_is_picked_up_without_waiting_for_a_visit(self):
        location = (f"https://{TEAM}.cloudflareaccess.com/cdn-cgi/access/login/{HOST}"
                    f"?kid={AUD}&meta=x&redirect_url=%2F")
        answers = iter(["", "", location])             # app created between checks
        v = verifier(team="", aud="", redirect=lambda url: next(answers))
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            v._last_discovery = 0                       # let the next minute's check run

        with patch("app.main.ACCESS", v), patch("app.main.asyncio.sleep", fake_sleep), \
             patch("app.remote_access.save_config"):
            run(main._watch_for_access_app())
        self.assertEqual((TEAM, AUD), (v.team, v.aud))
        self.assertEqual([60, 60], sleeps)

    def test_discovery_never_waits_on_itself(self):
        """Without Access, Cloudflare forwards our own lookup back through the tunnel to us. That
        inner request must be refused at once, not queue behind the lookup that caused it."""
        v = verifier(team="", aud="")
        v._discovering = True
        self.assertFalse(run(v.discover()))


class RedirectParsingTests(unittest.TestCase):
    def test_the_real_cloudflare_format(self):
        loc = f"https://{TEAM}.cloudflareaccess.com/cdn-cgi/access/login/{HOST}?kid={AUD}&meta=abc"
        self.assertEqual((TEAM, AUD), parse_access_redirect(loc, HOST))

    def test_anything_else_is_ignored(self):
        bad = [
            f"https://{TEAM}.cloudflareaccess.com/cdn-cgi/access/login/mc.softowetto.com?kid={AUD}",  # another app
            f"http://{TEAM}.cloudflareaccess.com/cdn-cgi/access/login/{HOST}?kid={AUD}",              # not https
            f"https://evil.example.com/cdn-cgi/access/login/{HOST}?kid={AUD}",                         # not Cloudflare
            f"https://a.b.cloudflareaccess.com/cdn-cgi/access/login/{HOST}?kid={AUD}",                 # odd team
            f"https://{TEAM}.cloudflareaccess.com/cdn-cgi/access/login/{HOST}?kid=NOT-HEX",
            f"https://{TEAM}.cloudflareaccess.com/cdn-cgi/access/login/{HOST}",
            "", "/relative",
        ]
        for loc in bad:
            with self.subTest(loc=loc):
                self.assertIsNone(parse_access_redirect(loc, HOST))


def request(headers=None, cookies=None):
    return types.SimpleNamespace(headers=headers or {}, cookies=cookies or {},
                                 url=types.SimpleNamespace(path="/api/meta"),
                                 state=types.SimpleNamespace())


class GuardTests(unittest.TestCase):
    def setUp(self):
        p = patch("app.main.ACCESS", verifier())
        p.start()
        self.addCleanup(p.stop)

    def refusal(self, headers, cookies=None, local=True):
        with patch("app.main.settings_mod.LOCAL_MODE", local):
            return run(main._remote_refusal(headers, cookies or {}))

    def test_using_it_at_the_pc_needs_nothing(self):
        self.assertIsNone(self.refusal({}))

    def test_through_the_tunnel_without_a_login_is_refused(self):
        self.assertTrue(self.refusal({"cf-connecting-ip": "203.0.113.5", "cf-ray": "x"}))

    def test_through_the_tunnel_with_a_login_is_allowed(self):
        self.assertIsNone(self.refusal({"cf-ray": "x", "cf-access-jwt-assertion": token()}))

    def test_the_access_cookie_also_counts(self):
        self.assertIsNone(self.refusal({"cf-ray": "x"}, {"CF_Authorization": token()}))

    def test_the_hosted_site_is_untouched(self):
        """The VPS sits behind Cloudflare legitimately and has its own passphrase gate."""
        self.assertIsNone(self.refusal({"cf-ray": "x"}, local=False))

    def test_a_refused_page_request_gets_a_page_and_an_api_call_gets_json(self):
        async def call_next(_request):
            raise AssertionError("a refused request must not reach the app")

        with patch("app.main.settings_mod.LOCAL_MODE", True):
            api = run(main.remote_access_guard(request({"cf-ray": "x"}), call_next))
            page_req = request({"cf-ray": "x"})
            page_req.url.path = "/dashboard"
            page = run(main.remote_access_guard(page_req, call_next))
        self.assertEqual((403, "application/json"), (api.status_code, api.media_type))
        self.assertEqual(403, page.status_code)
        self.assertIn(b"This OmniDL is locked", page.body)

    def test_an_allowed_remote_request_is_marked_remote(self):
        async def call_next(req):
            return req.state.remote

        with patch("app.main.settings_mod.LOCAL_MODE", True):
            self.assertTrue(run(main.remote_access_guard(
                request({"cf-ray": "x", "cf-access-jwt-assertion": token()}), call_next)))
            self.assertFalse(run(main.remote_access_guard(request({}), call_next)))

    def test_the_live_feed_websocket_is_guarded_too(self):
        closed = []

        class FakeSocket:
            headers = {"cf-ray": "x"}
            cookies = {}

            async def close(self, code):
                closed.append(code)

            async def accept(self):
                raise AssertionError("a refused socket must not be accepted")

        with patch("app.main.settings_mod.LOCAL_MODE", True):
            run(main.websocket_endpoint(FakeSocket()))
        self.assertEqual([1008], closed)


class ConfigIsolationTests(unittest.TestCase):
    def test_remote_settings_cannot_be_changed_from_the_web_ui(self):
        """The UI is exactly what a remote caller reaches, so it mustn't control who gets in."""
        from app import settings as settings_mod
        for key in ("tunnel_config", "access_team", "access_aud", "hostname", "cloudflared"):
            self.assertNotIn(key, settings_mod.DEFAULTS)
            self.assertNotIn(key, settings_mod.SESSION_PREF_KEYS)


if __name__ == "__main__":
    unittest.main()
