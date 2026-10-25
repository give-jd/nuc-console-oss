"""Peer tokens ([web] peer_tokens_file, docs/WEB.md#peer-tokens): the token another host uses to watch this one. It must open a few read-only
GETs and nothing else, never through a cookie or the URL, and never reveal a CSRF token. Hermetic: temporary files, the real handler on a
loopback port (port 0), the demo data."""
import contextlib
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import nuc_config  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
import webhttp  # noqa: E402

VIEWER = "v" * 24
SUM = "s" * 40
VIEWS = "w" * 40
POSIX = os.name == "posix"
SUMMARY_PATHS = ("/api/v1", "/api/v1/summary")
VIEW_PATHS = SUMMARY_PATHS + tuple("/api/v1/" + v for v in ("overview", "cpu", "health", "map", "events"))
DENIED_FOR_SUMMARY = tuple(p for p in VIEW_PATHS if p not in SUMMARY_PATHS)
NEVER = ("/api/v1/ai", "/api/v1/telegram", "/api/v1/stream", "/api/v1/stream?view=summary", "/api/v1/hosts", "/api/v1/nothing", "/api/v1/summary/",
         "/api/v1//summary", "/metrics", "/", "/app", "/setup", "/?view=telegram", "/?view=settings", "/?view=ai", "/?view=events&format=csv",
         "/s/app.12345678.css", "/ai/on", "/telegram/pair", "/settings/config", "/?set=theme-dark&back=", "/?token=" + SUM)
FILE = "# peers\nsum-host summary %s\nview-host views %s  # a trailing comment\n" % (SUM, VIEWS)


def call(srv, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    out = (r.status, {k.title(): v for k, v in r.getheaders()}, data.decode("utf-8", "replace"))
    c.close()
    return out


def bearer(tok):
    return {"Authorization": "Bearer " + tok}


def keys(x, path=""):
    """Every key name in a JSON document."""
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            for y in keys(v):
                yield y
    elif isinstance(x, list):
        for v in x:
            for y in keys(v):
                yield y


class Peer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.path = os.path.join(cls.dir, "peer.tokens")
        cls.write(FILE)
        cfg = dict(nuc_config.load()["web"], refresh_seconds=2, peer_tokens_file=cls.path)
        cls.srv = web.Server(("127.0.0.1", 0), cfg, VIEWER, demo=True)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.expose = render.CFG["expose"]

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        render.CFG["expose"] = cls.expose
        cls.srv.shutdown()
        cls.srv.server_close()
        __import__("shutil").rmtree(cls.dir, True)

    @classmethod
    def write(cls, text, mode=0o600):
        if os.path.lexists(cls.path):
            os.remove(cls.path)
        fd = os.open(cls.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as f:  # bytes: the file is UTF-8 whatever the platform's text encoding (Windows: cp1252) or newline translation
            f.write(text.encode("utf-8"))
        if POSIX:
            os.chmod(cls.path, mode)

    def setUp(self):
        self.write(FILE)
        self.srv.peers._seen.clear()

    def get(self, path, tok=SUM, method="GET", **h):
        return call(self.srv, method, path, dict(bearer(tok), **h))

    def refused(self, res, tok=SUM):
        status, headers, body = res
        self.assertEqual(status, 403, res)
        for leak in ("csrf", tok, VIEWER):
            self.assertNotIn(leak, body.lower() if leak == "csrf" else body)
        self.assertNotIn("Set-Cookie", headers)

    # ---- what is allowed ------------------------------------------------------------------------------------------------------------------

    def test_scope_summary_reads_the_index_and_the_summary(self):
        for p in SUMMARY_PATHS:
            for method in ("GET", "HEAD"):
                status, headers, body = self.get(p, method=method)
                self.assertEqual(status, 200, (p, method))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn("Set-Cookie", headers)
                self.assertEqual(bool(body), method == "GET")

    def test_scope_summary_is_refused_the_other_views(self):
        for p in DENIED_FOR_SUMMARY:
            self.refused(self.get(p))

    def test_scope_views_reads_the_read_only_screens(self):
        for p in VIEW_PATHS:
            viewer = call(self.srv, "GET", p, bearer(VIEWER))
            peer = self.get(p, VIEWS)
            self.assertEqual(peer[0], viewer[0], p)
            self.assertNotIn("Set-Cookie", peer[1])
            self.assertEqual(peer[1]["Cache-Control"], "no-store")
            if peer[0] == 200 and p != "/api/v1":
                self.assertEqual(json.loads(peer[2])["rev"], json.loads(viewer[2])["rev"], "the document the viewer gets")

    def test_views_take_the_screens_own_parameters(self):
        status, _h, body = self.get("/api/v1/events?limit=5&age=24h", VIEWS)
        self.assertIn(status, (200, 404))
        self.assertEqual(self.get("/api/v1/events?limit=", VIEWS)[0], 400, "a bad parameter is the API's 400, as for the viewer")

    def test_the_documents_a_peer_may_read_carry_no_csrf_or_token(self):
        walked = 0
        for p in VIEW_PATHS:
            status, _h, body = self.get(p, VIEWS)
            if status != 200:
                continue
            walked += 1
            names = set(keys(json.loads(body)))
            bad = [k for k in names if re.search(r"csrf|token|secret|passw|cookie", k, re.I)]
            self.assertEqual(bad, [], p)
        self.assertGreaterEqual(walked, 5)

    def test_the_walk_would_see_a_csrf_in_the_documents_that_have_one(self):
        found = False
        for p in ("/api/v1/ai", "/api/v1/telegram"):
            status, _h, body = call(self.srv, "GET", p, bearer(VIEWER))
            found = found or (status == 200 and "csrf" in set(keys(json.loads(body))))
        self.assertTrue(found, "ai or telegram carries a csrf in the demo: this is why they are not on a peer's list")

    # ---- what is not ----------------------------------------------------------------------------------------------------------------------

    def test_everything_else_is_403_for_both_scopes(self):
        for tok in (SUM, VIEWS):
            for p in NEVER:
                for method in ("GET", "HEAD"):
                    status, headers, body = call(self.srv, method, p, bearer(tok))
                    if p in ("/ai/on", "/telegram/pair", "/settings/config"):
                        self.assertEqual(status, 405, "a POST-only route says so to everyone, before any token")
                        continue
                    self.refused((status, headers, body), tok)

    def test_a_post_or_any_other_method_with_a_peer_token_is_403(self):
        form = "csrf=x&back="
        for tok in (SUM, VIEWS):
            for p in ("/ai/on", "/telegram/pair", "/settings/config", "/setup/finish", "/api/v1/summary", "/"):
                self.refused(call(self.srv, "POST", p, dict(bearer(tok), **{"Content-Type": "application/x-www-form-urlencoded"}), form), tok)
            for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
                self.refused(call(self.srv, method, "/api/v1/summary", bearer(tok)), tok)

    def test_a_refused_request_does_not_leak_the_page_csrf(self):
        csrf = self.srv.csrf
        for p in NEVER + SUMMARY_PATHS:
            for tok in (SUM, VIEWS):
                self.assertNotIn(csrf, call(self.srv, "GET", p, bearer(tok))[2])

    def test_bearer_only_never_a_cookie_or_the_url(self):
        for p in ("/api/v1/summary", "/api/v1"):
            for h, path in (({"Cookie": "nuc_token=" + SUM}, p), ({}, p + "?token=" + SUM), ({"Cookie": "nuc_token=" + SUM}, "/"),
                            ({}, "/?token=" + SUM)):
                status, headers, body = call(self.srv, "GET", path, h)
                self.assertEqual(status, 401, (h, path))
                self.assertNotIn("Set-Cookie", headers)
                self.assertNotIn("csrf", body)

    def test_the_viewer_cookie_cannot_be_got_with_a_peer_token(self):
        for tok in (SUM, VIEWS):
            for p in ("/?token=" + VIEWER, "/api/v1/summary?token=" + VIEWER, "/"):
                status, headers, _b = call(self.srv, "GET", p, bearer(tok))
                self.assertNotIn("Set-Cookie", headers, p)
                self.assertNotEqual(status, 302)

    def test_a_peer_token_does_not_unlock_the_site_fetch_rule(self):
        for site in ("cross-site", "same-site"):
            self.assertEqual(self.get("/api/v1/summary", Sec_Fetch_Site=site)[0], 200, "http.client sends the underscore name: not the header")
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", "/api/v1/summary", headers={"Authorization": "Bearer " + SUM, "Sec-Fetch-Site": "cross-site"})
        r = c.getresponse()
        self.assertEqual(r.status, 403)
        self.assertNotIn(b"csrf", r.read())
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", "/api/v1/summary", headers={"Authorization": "Bearer " + SUM, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(c.getresponse().status, 200)
        c.close()

    # ---- wrong tokens ---------------------------------------------------------------------------------------------------------------------

    def test_wrong_partial_prefix_long_and_unicode_tokens_are_401_like_a_wrong_viewer_token(self):
        wrong = ["", "x", SUM[:-1], SUM[:16], SUM + "x", SUM.upper(), SUM + " " + VIEWS, "é" * 40, "ÿ" * 33, "a" * 5000, VIEWS[:31], "Bearer " + SUM]
        base = call(self.srv, "GET", "/api/v1/summary", bearer("w" * 8))
        self.assertEqual(base[0], 401)
        for tok in wrong:
            status, headers, body = call(self.srv, "GET", "/api/v1/summary", {"Authorization": "Bearer " + tok})
            self.assertEqual((status, body, headers.get("Www-Authenticate")), (base[0], base[2], base[1].get("Www-Authenticate")), tok[:10])

    def test_a_very_long_header_gets_no_data(self):
        try:
            status, _h, body = call(self.srv, "GET", "/api/v1/summary", {"Authorization": "Bearer " + "a" * 100000})
        except (OSError, http.client.HTTPException):
            return  # the connection was closed: also nothing
        self.assertIn(status, (400, 401, 431))
        self.assertNotIn("csrf", body)

    def test_the_viewer_token_is_unchanged(self):
        for h in (bearer(VIEWER), {"Cookie": "nuc_token=" + VIEWER}):
            self.assertEqual(call(self.srv, "GET", "/api/v1/ai", h)[0], 200)
            self.assertEqual(call(self.srv, "GET", "/", h)[0], 200)
            self.assertEqual(call(self.srv, "GET", "/api/v1/stream?view=nothing", h)[0], 404)
        status, headers, _b = call(self.srv, "GET", "/?token=" + VIEWER)
        self.assertEqual(status, 302)
        self.assertIn("nuc_token=" + VIEWER, headers["Set-Cookie"])
        self.assertEqual(call(self.srv, "GET", "/metrics", bearer(VIEWER))[0], 200)
        self.assertEqual(call(self.srv, "GET", "/api/v1/summary")[0], 401)

    def test_stream_and_metrics_are_denied(self):
        for tok in (SUM, VIEWS):
            self.refused(self.get("/api/v1/stream?view=summary", tok), tok)
            self.refused(self.get("/api/v1/stream", tok), tok)
            self.refused(self.get("/metrics", tok), tok)

    # ---- the file -------------------------------------------------------------------------------------------------------------------------

    def test_a_revoked_line_stops_working_and_a_new_one_starts_without_a_restart(self):
        self.assertEqual(self.get("/api/v1/summary")[0], 200)
        newtok = "n" * 33
        self.write("view-host views %s\nnew-host summary %s\n" % (VIEWS, newtok))
        self.assertEqual(self.get("/api/v1/summary")[0], 401, "revoked: the line is gone")
        self.assertEqual(self.get("/api/v1/summary", newtok)[0], 200)
        self.assertEqual(self.get("/api/v1/overview", VIEWS)[0], 200)
        self.write("")
        self.assertEqual(self.get("/api/v1/summary", VIEWS)[0], 401)
        os.remove(self.path)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.get("/api/v1/summary", VIEWS)[0], 401, "a missing file: no peer token")
        self.assertNotIn(VIEWS, err.getvalue())

    def test_a_revoked_and_re_added_label_forgets_its_last_use(self):
        self.get("/api/v1/summary")
        self.assertIsNotNone(self.srv.peers.last()[0][2])
        self.write("view-host views %s\n" % VIEWS)
        self.assertEqual([e[0] for e in self.srv.peers.last()], ["view-host"])
        self.write(FILE)
        self.assertEqual({e[0]: e[2] for e in self.srv.peers.last()}["sum-host"], None, "never used again since it came back")

    def test_malformed_lines_are_ignored_with_a_message_that_has_no_token(self):
        secret = "z" * 40
        lines = ["good summary %s" % SUM, "BAD summary %s" % secret, "bad-scope admin %s" % secret + "1", "short summary abc", "two-parts only",
                 "dup views %s" % ("d" * 40), "good2 views %s" % ("e" * 40), "ünï summary %s" % secret,
                 "four summary %s extra" % secret, "dup2 summary %s" % SUM, "x" * 40, "-lead summary %s" % secret, "tab\tsummary\t%s" % ("t" * 40)]
        self.write("\n".join(lines) + "\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.get("/api/v1/summary")[0], 200)
        out = err.getvalue()
        self.assertIn("line 2 ignored", out)
        for t in (secret, SUM, "d" * 40, "e" * 40, "abc", VIEWER):
            self.assertNotIn(t, out)
        self.assertEqual(self.get("/api/v1/summary", secret)[0], 401)
        self.assertEqual(self.get("/api/v1/summary", "e" * 40)[0], 200, "a good line after bad ones counts")
        self.assertEqual(self.get("/api/v1/overview", "e" * 40)[0], 200)
        self.assertEqual(self.get("/api/v1/summary", "t" * 40)[0], 200, "white space is tabs or spaces")

    def test_at_most_16_entries(self):
        toks = ["%02d" % i * 20 for i in range(20)]
        self.write("".join("h%d summary %s\n" % (i, t) for i, t in enumerate(toks)))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.get("/api/v1/summary", toks[15])[0], 200)
        self.assertEqual(self.get("/api/v1/summary", toks[16])[0], 401)
        self.assertEqual(self.get("/api/v1/summary", toks[0])[0], 200, "every token is compared, the first and the last")
        self.assertNotIn(toks[16], err.getvalue())

    def test_an_oversized_file_is_refused_whole(self):
        self.write(FILE + "# " + "x" * webhttp.PEER_FILE_MAX + "\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.get("/api/v1/summary")[0], 401)
        self.assertIn("refused", err.getvalue())

    def test_a_file_that_is_not_utf8_is_refused(self):
        os.remove(self.path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(b"a summary " + SUM.encode() + b"\n\xff\xfe\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.get("/api/v1/summary")[0], 401)

    @unittest.skipUnless(POSIX, "mode bits and links: POSIX only (Windows relies on the folder's ACL)")
    def test_a_file_others_can_read_is_refused_and_works_again_when_fixed(self):
        os.chmod(self.path, 0o640)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.get("/api/v1/summary")[0], 401)
        self.assertIn("chmod 600", err.getvalue())
        self.assertNotIn(SUM, err.getvalue())
        os.chmod(self.path, 0o600)
        self.assertEqual(self.get("/api/v1/summary")[0], 200)

    @unittest.skipUnless(POSIX, "links: POSIX only")
    def test_a_link_is_refused(self):
        real = os.path.join(self.dir, "real.tokens")
        with open(real, "w") as f:
            f.write(FILE)
        os.chmod(real, 0o600)
        os.remove(self.path)
        os.symlink(real, self.path)
        self.addCleanup(os.remove, real)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.get("/api/v1/summary")[0], 401)

    @unittest.skipUnless(POSIX and hasattr(os, "mkfifo"), "a fifo: POSIX only")
    def test_a_fifo_is_refused_and_does_not_block(self):
        os.remove(self.path)
        os.mkfifo(self.path, 0o600)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.get("/api/v1/summary")[0], 401)

    @unittest.skipUnless(POSIX and hasattr(os, "geteuid") and os.geteuid() == 0, "needs root to give a file to another user")
    def test_a_file_of_another_user_is_refused(self):
        os.chown(self.path, 12345, -1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.get("/api/v1/summary")[0], 401)

    # ---- bookkeeping and the settings page ------------------------------------------------------------------------------------------------

    def test_last_use_is_shown_read_only_on_the_settings_page_without_a_token(self):
        self.get("/api/v1/summary")
        status, _h, page = call(self.srv, "GET", "/?view=settings", bearer(VIEWER))
        self.assertEqual(status, 200)
        self.assertIn("peer tokens", page)
        self.assertIn("sum-host", page)
        self.assertIn("view-host", page)
        self.assertRegex(page, r"view-host</b> · views · never used")
        self.assertRegex(page, r"sum-host</b> · summary · last used \d+ s ago")
        for t in (SUM, VIEWS):
            self.assertNotIn(t, page)

    def test_no_access_log_and_no_token_on_stderr(self):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            self.get("/api/v1/summary")
            self.get("/", SUM)
            self.get("/api/v1/summary", "q" * 40)
        self.assertEqual(err.getvalue() + out.getvalue(), "")


class Off(unittest.TestCase):
    """No file configured (the default), or no viewer token: nothing changes and a peer token is nothing."""

    def serve(self, token, **cfg):
        srv = web.Server(("127.0.0.1", 0), dict(nuc_config.load()["web"], refresh_seconds=2, **cfg), token, demo=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), setattr(render, "DEMO", False)))
        return srv

    def test_without_the_key_a_bearer_that_is_not_the_viewers_is_401(self):
        srv = self.serve(VIEWER)
        self.assertEqual(nuc_config.load()["web"]["peer_tokens_file"], "")
        self.assertEqual(call(srv, "GET", "/api/v1/summary", bearer(SUM))[0], 401)
        self.assertEqual(call(srv, "GET", "/api/v1/summary", bearer(VIEWER))[0], 200)
        _s, _h, page = call(srv, "GET", "/?view=settings", bearer(VIEWER))
        self.assertNotIn("peer tokens", page)

    def test_without_a_viewer_token_the_file_is_not_read(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        p = os.path.join(d, "t")
        with open(p, "w") as f:
            f.write(FILE)
        os.chmod(p, 0o600)
        srv = self.serve("", peer_tokens_file=p)
        self.assertEqual(srv.peers.path, "")

    def test_a_peer_line_may_not_repeat_the_viewers_token(self):
        long = "L" * 40
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        p = os.path.join(d, "t")
        with open(p, "w") as f:
            f.write("same summary %s\nok summary %s\n" % (long, SUM))
        os.chmod(p, 0o600)
        srv = self.serve(long, peer_tokens_file=p)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(call(srv, "GET", "/api/v1/ai", bearer(long))[0], 200)  # the viewer, with all of it
            self.assertEqual(call(srv, "GET", "/api/v1/summary", bearer(SUM))[0], 200)
        self.assertEqual(call(srv, "GET", "/api/v1/ai", bearer(SUM))[0], 403)
        self.assertEqual([e[0] for e in srv.peers.entries], ["ok"])
        self.assertNotIn(long, err.getvalue())


if __name__ == "__main__":
    unittest.main()
