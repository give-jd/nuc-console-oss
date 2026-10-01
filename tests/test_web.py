import html
import http.client
import re
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import nuc_config  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402

TOKEN = "t" * 24


def raw(srv, path="/", headers=()):
    import socket
    c = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
    c.sendall((f"GET {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode())
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


def get(srv, path="/", method="GET", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, dict(r.getheaders()), body


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.open, cls.locked = serve(), serve(TOKEN)

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
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

    def test_only_get_on_known_paths(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            self.assertEqual(get(self.open, "/", m)[0], 405, m)
        self.assertEqual(get(self.open, "/api/state")[0], 404)
        self.assertEqual(get(self.open, "/healthz")[2].strip(), "ok")

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

    def test_text_size_and_fit(self):
        q = lambda s: web.view_params(web.parse_qs(s))  # noqa: E731
        self.assertEqual(q("zoom=133")["zoom"], 125)                                                 # snapped to a step
        self.assertEqual((q("zoom=9999")["zoom"], q("zoom=1")["zoom"], q("zoom=x")["zoom"]), (200, 50, 100))
        self.assertEqual((q("rows=7")["rows"], q("rows=999")["rows"], q("cols=133")["cols"]), (20, 120, 140))
        self.assertEqual(web.view_params({}, 150)["zoom"], 150)                                      # [display] zoom is the default
        _, _, big = get(self.open, "/?fit=1&zoom=150")
        _, _, small = get(self.open, "/?fit=1&zoom=75")
        self.assertIn("calc(98vw /", big)                                                            # the text fills the width
        self.assertIn("console 133x", big)                                                           # bigger text = fewer columns
        self.assertIn("console 267x", small)
        names = lambda page: set(re.findall(r"── ([A-Z][A-Z ·]+?) ─", render.ANSI.sub("", html.unescape(re.sub("<[^>]+>", "", page)))))  # noqa: E731
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
        self.assertEqual((w["enabled"], w["port"], w["columns"], w["refresh_seconds"]), (True, 65535, 60, 5))

    def test_disabled_by_default_exits_cleanly(self):
        self.assertEqual(web.main(["web.py"]), 0)
        self.assertEqual(web.main(["web.py", "--enabled"]), 1)


if __name__ == "__main__":
    unittest.main()
