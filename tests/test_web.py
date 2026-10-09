import html
import http.client
import re
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import nuc_config  # noqa: E402
import render  # noqa: E402
import ansi  # noqa: E402
import web  # noqa: E402
import webmap  # noqa: E402
import weburl  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from webtest import classic_default  # noqa: E402

TOKEN = "t" * 24


def raw(srv, path="/", headers=(), method="GET"):
    import socket
    c = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
    c.sendall((f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode())
    data = b""
    while True:
        try:
            chunk = c.recv(4096)
        except ConnectionError:  # Windows reports a connection closed by the server as aborted/reset
            break
        if not chunk:
            break
        data += chunk
    c.close()
    return data.decode(errors="replace")


def serve(token=""):
    cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
    srv = web.Server(("127.0.0.1", 0), cfg, token, demo=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def get_any(srv, path="/", method="GET", headers=None):  # the interface the configuration says (the shell by default)
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, dict(r.getheaders()), body


get = classic_default()(get_any)  # the classic pages: what most of this file looks at (the shell has its own files)


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.open, cls.locked = serve(), serve(TOKEN)
        cls.expose = render.CFG["expose"]  # the demo declares [expose]: put back after

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        render.CFG["expose"] = cls.expose
        for s in (cls.open, cls.locked):
            s.shutdown()
            s.server_close()

    def test_page_is_the_dashboard_and_is_locked_down(self):
        st, h, body = get(self.open)
        self.assertEqual(st, 200)
        self.assertIn("EXPOSURE", body)
        self.assertNotIn("<script", body.lower())
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")

    def test_only_get_and_head_on_known_paths(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            st, h, _ = get(self.open, "/", m)
            self.assertEqual((st, h["Allow"]), (405, "GET, HEAD"), m)
        self.assertEqual(get(self.open, "/api/state")[0], 404)
        self.assertEqual(get(self.open, "/healthz")[2].strip(), "ok")

    def test_head_is_get_without_the_body(self):
        """A monitor or a browser asking HEAD gets the answer GET would give: the same status, the same headers (Content-Length included)
        and nothing after them. Not read through http.client, which would hide a body written anyway."""
        for path in ("/", "/?refresh=10", "/?view=map&refresh=10", "/healthz", "/nothing", "/?token=x"):
            st, h, body = get(self.open, path, "GET")
            hst, hh, hbody = get(self.open, path, "HEAD")
            self.assertEqual((hst, hbody), (st, ""), path)
            same = lambda d: {k: v for k, v in d.items() if k not in ("Date", "Server")}  # noqa: E731 - the clock and the name differ by nothing that matters
            self.assertEqual(same(hh), same(h), path)
            self.assertEqual(int(hh["Content-Length"]), len(body.encode()), path)
            wire = raw(self.open, path, [("Host", "localhost")], "HEAD")
            self.assertEqual(wire.split("\r\n\r\n", 1)[1], "", path)                            # nothing after the headers, on the wire
            self.assertEqual(wire.split(" ", 2)[1], str(st), path)
        self.assertGreater(int(get(self.open, "/", "HEAD")[1]["Content-Length"]), 1000)             # the length of the page, not of nothing

    def test_head_has_the_same_access_rules_as_get(self):
        self.assertEqual(get(self.locked, "/", "HEAD")[0], 401)                                      # no token
        st, h, body = get(self.locked, "/", "HEAD", {"Authorization": "Bearer wrong"})
        self.assertEqual((st, h["WWW-Authenticate"], body), (401, 'Bearer realm="nuc-console"', ""))
        self.assertEqual(get(self.locked, "/", "HEAD", {"Authorization": "Bearer " + TOKEN})[0], 200)
        self.assertEqual(get(self.locked, "/", "HEAD", {"Cookie": "nuc_token=" + TOKEN})[0], 200)
        st, h, _ = get(self.locked, "/?token=" + TOKEN, "HEAD")                                      # the cookie is set by HEAD too
        self.assertEqual((st, h["Location"]), (302, "/"))
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertEqual(get(self.locked, "/?token=nope", "HEAD")[0], 401)
        self.assertEqual(get(self.locked, "/healthz", "HEAD")[0], 200)                               # like GET: no token needed
        self.assertEqual(get(self.open, "/", "HEAD", {"Host": "evil.example.com"})[0], 421)         # DNS rebinding
        self.assertEqual(get(self.open, "/", "HEAD", {"Host": "localhost:8787"})[0], 200)

    def test_cross_origin_isolation_headers_on_every_response(self):
        """Every answer says that no other site may embed it (CORP) and that its window is not shared with another origin's (COOP)."""
        answers = [get(self.open, "/"), get(self.open, "/", "HEAD"), get(self.open, "/healthz"), get(self.open, "/nothing"),
                   get(self.open, "/", "POST"), get(self.open, "/", "OPTIONS"), get(self.open, "/", headers={"Host": "evil.example.com"}),
                   get(self.open, "/?view=map&as=graph"), get(self.locked, "/"), get(self.locked, "/?token=" + TOKEN),
                   get(self.locked, "/", headers={"Authorization": "Bearer " + TOKEN})]
        self.assertEqual([a[0] for a in answers], [200, 200, 200, 404, 405, 405, 421, 200, 401, 302, 200])  # pages, errors, redirect
        for st, h, _ in answers:
            self.assertEqual(h["Cross-Origin-Resource-Policy"], "same-origin", st)
            self.assertEqual(h["Cross-Origin-Opener-Policy"], "same-origin", st)

    def test_token_bearer_cookie_and_query(self):
        self.assertEqual(get(self.locked)[0], 401)
        self.assertEqual(get(self.locked, "/", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(get(self.locked, "/", headers={"Authorization": "Bearer " + TOKEN})[0], 200)
        self.assertEqual(get(self.locked, "/", headers={"Cookie": "nuc_token=" + TOKEN})[0], 200)
        st, h, _ = get(self.locked, "/?token=" + TOKEN)   # token moves from the URL into a cookie
        self.assertEqual((st, h["Location"]), (302, "/"))
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])
        self.assertEqual(get(self.locked, "/?token=nope")[0], 401)
        self.assertEqual(get(self.locked, "/healthz")[0], 200)

    def test_token_redirect_keeps_the_view(self):
        """`/?token=X&view=map` used to land on the dashboard. The redirect carries the view back, rebuilt from what view_params() validated:
        what the view does not read, what is not a parameter at all, and the token itself are left out."""
        def moved(query):
            st, h, _ = get(self.locked, "/?token=" + TOKEN + query)
            self.assertEqual(st, 302, query)
            self.assertTrue(h["Set-Cookie"].startswith("nuc_token=" + TOKEN + ";"), query)
            self.assertNotIn(TOKEN, h["Location"])
            return h["Location"]
        self.assertEqual(moved("&view=map&sort=mem"), "/?view=map")                                  # sort= is the CPU page's
        self.assertEqual(moved("&sort=mem&view=map&refresh=5&zoom=133&fit=1"), "/?view=map&zoom=125&fit=1&refresh=5")  # clamped like the page
        self.assertEqual(moved("&view=cpu&sort=mem&sel=0042&cols=133"), "/?view=cpu&cols=140&sort=mem&sel=42")
        self.assertEqual(moved("&view=health&period=30&pause=1&sel=cpu-hog:chrome"), "/?view=health&period=30&sel=cpu-hog%3Achrome&pause=1")
        self.assertEqual(moved("&view=ai&sel=qwen&pause=1&period=30"), "/?view=ai&sel=qwen&pause=1")
        self.assertEqual(moved("&view=map&open=0123456789.abcdef0123&shut=zz&all=1&only=1&sel=0123456789&pause=1"),
                         "/?view=map&open=0123456789.abcdef0123&all=1&sel=0123456789&only=1&pause=1")
        self.assertEqual(moved("&view=map&as=graph&local=2&sel=0123456789&z=140&stacks=1&ext=0&open=0123456789"),
                         "/?view=map&sel=0123456789&as=graph&stacks=1&ext=0&local=2&z=150")          # the tree's open= means nothing to the graph
        self.assertEqual(moved("&open=0123456789&sel=1&pause=1&only=1&fit=1"), "/?fit=1")            # the dashboard reads only the size and refresh
        self.assertEqual(moved(""), "/")
        self.assertEqual(moved("&view=map&token=again"), "/?view=map")                               # the first token is the one checked; no token comes back
        for junk in ("&evil=1&view=nope", "&view=%0d%0aSet-Cookie:%20a=b", "&cols=%C2%B2&refresh=" + "9" * 5000):  # unknown, malformed, huge
            self.assertEqual(moved(junk), "/", junk)
        loc = moved("&view=health&sel=x%0d%0aSet-Cookie:%20evil=1%3b%20Domain=.example.com")         # a free-text value is encoded, never echoed raw
        self.assertEqual(loc.count("\n") + loc.count("\r") + loc.count(" "), 0)
        self.assertEqual(get(self.locked, "/?token=nope&view=map")[0], 401)                          # a wrong token never redirects
        st, _, page = get(self.locked, moved("&view=map&refresh=9"), headers={"Cookie": "nuc_token=" + TOKEN})
        self.assertEqual(st, 200)
        self.assertIn("· map ·", page)                                                               # the redirect lands on the map, not the dashboard

    def test_view_url_is_the_pages_own_address(self):
        """view_url() writes a view as the links of the pages do, so that the redirect and a click on the same view agree."""
        q = lambda s: weburl.view_url(weburl.view_params(web.parse_qs(s)))  # noqa: E731
        self.assertEqual(q(""), "/")
        self.assertEqual(q("view=map&open=bbbbbbbbbb.aaaaaaaaaa"), weburl.page_url(dict(webmap.map_here({k: 0 for k in weburl.HERE_KEYS}, web.graph.State(
            open=("aaaaaaaaaa", "bbbbbbbbbb")), "", False))))
        self.assertEqual(q("view=cpu&sort=cpu"), "/?view=cpu")                                       # the default is not written
        self.assertEqual(q("fit=1&kiosk=1&rotate=1&cols=220&rows=64"), "/?cols=220&rows=64&fit=1&rotate=1&kiosk=1")

    def test_host_header_guard_blocks_dns_rebinding(self):
        self.assertIn(" 421 ", raw(self.open, "/", [("Host", "evil.example.com")]).split("\r\n")[0] + " ")
        for ok in ("127.0.0.1:8787", "localhost", "[::1]:8787", "box.tail1234.ts.net"):
            self.assertIn(" 200 ", raw(self.open, "/", [("Host", ok)]).split("\r\n")[0] + " ", ok)

    def test_connection_cap_drops_instead_of_queueing(self):
        srv = serve()
        srv.slots = __import__("threading").BoundedSemaphore(1)
        srv.slots.acquire()  # no free slot
        try:
            self.assertEqual(raw(srv), "")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_full_view_shows_everything_the_overview_cuts(self):
        saved = dict(render.CFG["webapps"])
        render.CFG["webapps"] = {"app%02d" % i: [9000 + i] for i in range(20)}   # 20 declared apps: the compact overview cuts them
        render.CFG["details"] = True
        try:
            self.open.cache.clear()
            _, _, short = get(self.open, "/?cols=100")
            _, _, full = get(self.open, "/?cols=100&full=1")
        finally:
            render.CFG["webapps"] = saved
            render.CFG["details"] = False
        self.assertNotIn("app19", short)
        self.assertIn("more", short)
        for i in range(20):
            self.assertIn("app%02d" % i, full)
        self.assertIn("full details", short)

    def test_cols_parameter_is_clamped(self):
        self.assertEqual(get(self.open, "/?cols=abc")[0], 200)
        self.assertEqual(get(self.open, "/?cols=5")[0], 200)
        self.assertEqual(get(self.open, "/?cols=99999")[0], 200)

    def test_numbers_are_ascii_digits_only(self):
        """'²' is a digit to str.isdigit() but not to int(), nor are 5000 digits (Python 3.11+): the ValueError dropped the
        connection. Anything but a few ASCII digits is ignored: a normal page, the parameter at its default."""
        q = lambda s: weburl.view_params(web.parse_qs(s))  # noqa: E731
        for bad in ("%C2%B2", "%D9%A3", "1%C2%B2", "%EF%BC%95", "9" * 5000, "+5", "-5", " 5"):
            p = q(f"zoom={bad}&cols={bad}&rows={bad}&refresh={bad}")
            self.assertEqual([p[k] for k in ("zoom", "cols", "rows", "refresh")], [0, 0, 0, 0], bad)
        for path in ("/?view=map&zoom=%C2%B2", "/?cols=%C2%B2&rows=%D9%A3&refresh=%C2%B9&zoom=%EF%BC%95", "/?zoom=" + "9" * 5000):
            st, _, body = get(self.open, path)
            self.assertEqual(st, 200, path)
            self.assertNotIn("render error", body)
        self.assertIn("font-size:14.0px", get(self.open, "/?zoom=%C2%B2")[2])                         # ignored: the default size

    def test_text_size_and_fit(self):
        q = lambda s: weburl.view_params(web.parse_qs(s))  # noqa: E731
        self.assertEqual(q("zoom=133")["zoom"], 125)                                                 # snapped to a step
        self.assertEqual((q("zoom=9999")["zoom"], q("zoom=1")["zoom"], q("zoom=x")["zoom"]), (200, 50, 0))  # 0 = [display] zoom
        self.assertEqual((q("rows=7")["rows"], q("rows=999")["rows"], q("cols=133")["cols"]), (20, 120, 140))
        self.assertIn("font-size:14.0px", get(self.open, "/")[2])                                    # [display] zoom (100) by default
        _, _, big = get(self.open, "/?fit=1&zoom=150")
        _, _, small = get(self.open, "/?fit=1&zoom=75")
        self.assertIn("calc(98vw /", big)                                                            # the text fills the width
        self.assertIn("console 133x", big)                                                           # bigger text = fewer columns
        self.assertIn("console 267x", small)
        names = lambda page: set(re.findall(r"── ([A-Z][A-Z ·]+?) ─", ansi.ANSI.sub("", html.unescape(re.sub("<[^>]+>", "", page)))))  # noqa: E731
        _, _, huge = get(self.open, "/?fit=1&zoom=200")
        self.assertEqual(names(huge), names(small))                                                  # zooming in never loses a section
        self.assertIn("NETWORK TRAFFIC", names(huge))
        self.assertNotIn("… +", html.unescape(re.sub("<[^>]+>", "", huge)))                         # nor an item: the page scrolls
        self.assertIn('href="/?zoom=125&amp;fit=1">A−</a> 150% <a href="/?zoom=175&amp;fit=1">A+', big)
        self.assertIn('href="/?cols=100&amp;zoom=150&amp;fit=1">compact', big)                    # the other links keep the size
        _, _, kiosk = get(self.open, "/?fit=1&cols=220&rows=64&rotate=1&kiosk=1")
        self.assertIn("calc(93vh /", kiosk)                                                          # full screen: the height too
        self.assertIn("closes", kiosk)                                                               # how to get out of it
        self.assertNotIn("keys 1-", kiosk)                                                           # no keyboard on a web page
        _, _, plain = get(self.open, "/?zoom=150")
        self.assertIn("font-size:21.0px", plain)                                                     # without fit: the font grows
        self.assertNotIn("<script", (big + kiosk + plain).lower())
        self.assertIn("footer{position:sticky;bottom:0", big)                                        # the bar follows the scroll

    def test_local_mode_serves_loopback_only_when_web_is_off(self):
        seen = {}
        saved = web.Server
        web.Server = lambda addr, cfg, token, demo, zoom=100: seen.update(addr=addr, cfg=cfg, token=token) or (_ for _ in ()).throw(OSError("stop"))
        try:
            self.assertEqual(web.main(["web.py", "--local"]), 2)                                     # the fake Server stops it
        finally:
            web.Server = saved
        self.assertEqual((seen["addr"][0], seen["token"]), ("127.0.0.1", ""))

    def test_refresh_interval_between_1_and_10_seconds(self):
        q = lambda s: weburl.view_params(web.parse_qs(s))["refresh"]  # noqa: E731
        self.assertEqual((q(""), q("refresh=0"), q("refresh=1"), q("refresh=7"), q("refresh=99"), q("refresh=x")), (0, 0, 1, 7, 10, 0))
        _, _, page = get(self.open, "/?refresh=1")
        self.assertIn('<meta http-equiv="refresh" content="1">', page)
        self.assertIn('href="/?refresh=2">+</a>', page)                                             # + = less often
        self.assertIn('href="/?zoom=110&amp;refresh=1">A+', page)                                   # the size links keep it
        self.assertNotIn('refresh=0', page)                                                          # never under 1 s
        _, _, slow = get(self.open, "/?refresh=10&zoom=150")
        self.assertIn('content="10"', slow)
        self.assertIn('href="/?zoom=150&amp;refresh=9">−</a> 10s +', slow)                          # never over 10 s
        _, _, default = get(self.open, "/")
        self.assertIn('content="2"', default)                                                        # the config's value
        self.assertIn('href="/?refresh=1">−</a> 2s <a href="/?refresh=3">+</a>', default)

    def test_wide_is_the_two_column_layout_with_every_section(self):
        _, _, wide = get(self.open, "/?cols=200")
        for name in ("NETWORK TRAFFIC", "SESSIONS", "DISKS", "DOCKER · DISK"):                       # the wide-only sections
            self.assertIn(name, wide)
        self.assertIn("console 200x", wide)

    def test_html_escapes_everything(self):
        out = web.to_html("\x1b[31m<img src=x onerror=1>\x1b[0m & \x1b[2J\x1b[H\x1b[1;36mok\x1b[0m")
        self.assertNotIn("<img", out)
        self.assertIn("&lt;img", out)
        self.assertIn('class="c B"', out)
        self.assertNotIn("\x1b", out)


class Safety(unittest.TestCase):
    def test_non_loopback_bind_needs_token(self):
        for bind in ("0.0.0.0", "::", "192.168.0.5", "100.64.0.1"):
            with self.assertRaises(ValueError):
                web.check_bind(bind, "")
            web.check_bind(bind, TOKEN)
        for bind in ("127.0.0.1", "::1", "localhost"):
            web.check_bind(bind, "")

    def test_token_file_rules(self):
        self.assertEqual(web.read_token(""), "")
        for bad in ("short", "abcdefghijklmnopqrst;x y", "x" * 20 + "\u20ac"):
            with tempfile.NamedTemporaryFile("w", delete=False) as f:
                f.write(bad + "\n")
            try:
                with self.assertRaises(ValueError, msg=bad):
                    web.read_token(f.name)
            finally:
                os.unlink(f.name)
        if os.name == "posix":  # mode bits: Windows protects the token with the folder ACL (install-windows.ps1)
            with tempfile.NamedTemporaryFile("w", delete=False) as f:
                f.write(TOKEN + "\n")
            os.chmod(f.name, 0o644)
            try:
                with self.assertRaises(ValueError):          # group/other readable
                    web.read_token(f.name)
            finally:
                os.unlink(f.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("short\n")
        try:
            with self.assertRaises(ValueError):
                web.read_token(f.name)
        finally:
            os.unlink(f.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(TOKEN + "\n")
        try:
            self.assertEqual(web.read_token(f.name), TOKEN)
        finally:
            os.unlink(f.name)

    def test_web_config_defaults_and_clamps(self):
        d = nuc_config.load("/nonexistent")["web"]
        self.assertEqual((d["enabled"], d["bind"], d["port"]), (False, "127.0.0.1", 8787))
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write("[web]\nenabled = yes\nport = 99999\ncolumns = 5\nrefresh_seconds = x\n")
        try:
            w = nuc_config.load(f.name)["web"]
        finally:
            os.unlink(f.name)
        self.assertEqual((w["enabled"], w["port"], w["columns"], w["refresh_seconds"]), (True, 65535, 60, 2))  # bad: the default

    def test_disabled_by_default_exits_cleanly(self):
        self.assertEqual(web.main(["web.py"]), 0)
        self.assertEqual(web.main(["web.py", "--enabled"]), 1)


if __name__ == "__main__":
    unittest.main()
