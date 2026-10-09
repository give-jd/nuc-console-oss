"""The access token of the loopback view in a portable run / the desktop app (docs/WEB.md): where it comes from, who may read it, and that the
server asks for it on every way in (GET, the data API, the forms). Hermetic: a temporary data folder, the real handler on a loopback port."""
import http.client
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import nuc_config  # noqa: E402
import web  # noqa: E402
import webhttp  # noqa: E402


def call(srv, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read().decode("utf-8", "replace")
    c.close()
    return r.status, {k.title(): v for k, v in r.getheaders()}, data


class TokenFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))
        self.path = os.path.join(self.dir, webhttp.LOCAL_TOKEN)

    def test_created_once_private_and_stable_until_deleted(self):
        tok = webhttp.local_token(self.path)
        self.assertTrue(webhttp.TOKEN_OK.fullmatch(tok), tok)
        self.assertGreaterEqual(len(tok), 16)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(webhttp.local_token(self.path), tok, "the next start reuses it: the browser holds it as a cookie")
        with open(self.path) as f:
            self.assertEqual(f.read().strip(), tok)
        os.remove(self.path)
        self.assertNotEqual(webhttp.local_token(self.path), tok, "deleting the file is how to get a new one")

    @unittest.skipUnless(os.name == "posix", "mode bits")
    def test_a_file_others_can_read_or_a_bad_token_is_an_error_not_replaced(self):
        tok = webhttp.local_token(self.path)
        os.chmod(self.path, 0o644)
        with self.assertRaises(ValueError):
            webhttp.local_token(self.path)
        os.chmod(self.path, 0o600)
        with open(self.path, "w") as f:
            f.write("short")
        with self.assertRaises(ValueError):
            webhttp.local_token(self.path)
        with open(self.path) as f:
            self.assertEqual(f.read(), "short", "not replaced behind the user's back")
        self.assertNotEqual(tok, "short")


class Main(unittest.TestCase):
    """web.main(): which runs get a token."""

    def run_main(self, argv, portable, home=None):
        seen = {}

        def fake(addr, cfg, token, demo, zoom=100):
            seen.update(addr=addr, token=token, demo=demo)
            raise OSError("stop")

        if home is None:
            home = tempfile.mkdtemp()
            self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        with mock.patch.object(web, "Server", fake), mock.patch.object(nuc_config, "PORTABLE", home if portable else ""):
            self.assertEqual(web.main(["web.py"] + argv), 2)
        return seen, home

    def test_a_portable_run_has_a_token_that_is_in_the_data_folder_and_stable(self):
        seen, home = self.run_main(["--local"], True)
        with open(os.path.join(home, "web.token")) as f:
            self.assertEqual(seen["token"], f.read().strip())
        self.assertEqual(seen["addr"][0], "127.0.0.1")
        self.assertGreaterEqual(len(seen["token"]), 16)
        self.assertEqual(self.run_main(["--local"], True, home)[0]["token"], seen["token"], "the next start uses the same token")

    def test_the_demo_and_an_installation_without_token_file_have_none(self):
        self.assertEqual(self.run_main(["--local", "--demo"], True)[0]["token"], "")
        seen, home = self.run_main(["--local"], False)
        self.assertEqual(seen["token"], "")
        self.assertFalse(os.path.exists(os.path.join(home, "web.token")), "an installation writes no token file")


    def test_a_token_file_the_user_configured_wins_in_an_installation_and_a_portable_run_ignores_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        tf = os.path.join(d, "mine.token")
        with open(tf, "w") as f:
            f.write("my-own-token-0123456789\n")
        os.chmod(tf, 0o600)
        conf = os.path.join(d, "config.ini")
        with open(conf, "w") as f:
            f.write("[web]\nenabled = no\ntoken_file = %s\n" % tf)
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": conf}):
            self.assertEqual(self.run_main(["--local"], False)[0]["token"], "my-own-token-0123456789")
            seen, home = self.run_main(["--local"], True)
            self.assertNotEqual(seen["token"], "my-own-token-0123456789")
            self.assertTrue(os.path.exists(os.path.join(home, "web.token")))


class Served(unittest.TestCase):
    TOKEN = webhttp.secrets.token_urlsafe(24)

    @classmethod
    def setUpClass(cls):
        cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
        cls.srv = web.Server(("127.0.0.1", 0), cfg, cls.TOKEN, demo=True)
        threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_no_token_no_page_and_no_csrf_token(self):
        for path in ("/", "/?app=1&view=ai", "/app", "/api/v1/summary"):
            st, _h, body = call(self.srv, "GET", path)
            self.assertEqual(st, 401, path)
            self.assertNotIn(self.srv.csrf, body)
            self.assertNotIn("csrf", body.lower())

    def test_wrong_token_is_refused_every_way(self):
        for hdr in ({"Authorization": "Bearer nope"}, {"Cookie": "nuc_token=nope"}, {"Authorization": "Bearer " + self.TOKEN[:-1]}):
            self.assertEqual(call(self.srv, "GET", "/", hdr)[0], 401, hdr)
        self.assertEqual(call(self.srv, "GET", "/?token=nope")[0], 401)

    def test_bearer_cookie_and_url_token_work_and_the_url_one_becomes_a_cookie(self):
        self.assertEqual(call(self.srv, "GET", "/", {"Authorization": "Bearer " + self.TOKEN})[0], 200)
        self.assertEqual(call(self.srv, "GET", "/api/v1/summary", {"Authorization": "Bearer " + self.TOKEN})[0], 200)
        st, _h, body = call(self.srv, "GET", "/app", {"Cookie": "nuc_token=" + self.TOKEN})
        self.assertEqual(st, 200)
        st, h, _b = call(self.srv, "GET", "/app?token=" + self.TOKEN)
        self.assertEqual((st, h["Location"]), (302, "/app"))
        self.assertIn("nuc_token=" + self.TOKEN, h["Set-Cookie"])
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])
        self.assertNotIn(self.TOKEN, h["Location"])

    def test_a_post_without_the_token_reaches_no_action(self):
        form = "csrf=" + self.srv.csrf
        base = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:%d" % self.srv.server_address[1]}
        with mock.patch.object(web.aiweb, "engine") as eng, mock.patch.object(nuc_config, "write_features", create=True) as wf:
            for path in ("/ai/on", "/ai/ask", "/telegram/pair", "/settings/feature", "/settings/config"):
                st, _h, _b = call(self.srv, "POST", path, base, form)
                self.assertEqual(st, 401, path)
                st, _h, _b = call(self.srv, "POST", path, dict(base, Authorization="Bearer wrong"), form)
                self.assertEqual(st, 401, path)
            self.assertEqual(eng.mock_calls, [], "no action was reached")
            self.assertEqual(wf.mock_calls, [])
        st, _h, _b = call(self.srv, "POST", "/ai/on", dict(base, Cookie="nuc_token=" + self.TOKEN), form)
        self.assertNotEqual(st, 401, "with the token the post gets to the usual checks")

    def test_healthz_stays_open(self):
        self.assertEqual(call(self.srv, "GET", "/healthz")[0], 200)


if __name__ == "__main__":
    unittest.main()
