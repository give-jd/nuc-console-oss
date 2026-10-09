"""The shell as a wall display: `?app=1&ui=1.dw&kiosk=1` (wall density, kiosk), and the URL the display opens for `[ui] web = app`."""
import base64
import hashlib
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import unittest
from unittest import mock
from urllib.parse import parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import render  # noqa: E402
import web  # noqa: E402
import weburl  # noqa: E402
import webcss  # noqa: E402
import webjs  # noqa: E402
from test_web import get_any as get, serve  # noqa: E402

WALL = "/?app=1&ui=1.dw&kiosk=1"


class WallPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = serve()

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        cls.srv.shutdown()
        cls.srv.server_close()

    def page(self, path):
        st, h, body = get(self.srv, path)
        self.assertEqual(st, 200)
        return h, body

    def test_data_rotate_only_on_a_kiosk_page_in_the_wall_density(self):
        _, wall = self.page(WALL)
        self.assertRegex(wall, r'<main [^>]*data-rotate="%d"' % render.CFG["rotate_seconds"])
        for path in ("/?app=1&ui=1.dw", "/?app=1&ui=1.dd&kiosk=1", "/?app=1&ui=1.dc&kiosk=1", "/?app=1", "/?app=1&ui=1.dw&kiosk=1&view=settings",
                     "/?app=1&ui=1.dw&kiosk=1&edit=1", "/?app=0&kiosk=1&ui=1.dw"):
            self.assertNotRegex(self.page(path)[1], r"<main [^>]*data-rotate", path)
        for view in ("cpu", "health", "ai"):
            self.assertIn('data-rotate="', self.page(WALL + "&view=" + view)[1], view)

    def test_the_seconds_come_from_the_url_or_the_configuration(self):
        self.assertIn('data-rotate="7"', self.page(WALL + "&rotate=7")[1])
        for bad in ("2", "601", "x", "0"):  # rotate=1 and anything out of range: the configured seconds
            self.assertIn('data-rotate="%d"' % render.CFG["rotate_seconds"], self.page(WALL + "&rotate=" + bad)[1], bad)
        with mock.patch.dict(render.CFG, {"rotate_seconds": 40}):
            self.assertIn('data-rotate="40"', self.page(WALL + "&refresh=9")[1])  # a new cache key

    def test_the_html_carries_the_wall_and_the_kiosk_and_a_shift_class(self):
        _, body = self.page(WALL)
        html = re.search(r"<html [^>]*>", body).group(0)
        self.assertIn('data-density="wall"', html)
        self.assertIn(" data-kiosk", html)
        self.assertRegex(html, r'class="z\d+ shift-[012]')
        for now, shift in ((600 * 3 + 5, 0), (600 * 4 + 5, 1), (600 * 5 + 5, 2)):  # one position every ten minutes
            with mock.patch("time.time", return_value=now):
                self.assertIn("shift-%d" % shift, re.search(r"<html [^>]*>", self.page(WALL + "&refresh=%d" % (3 + shift))[1]).group(0))

    def test_the_footer_has_the_hint_and_nothing_to_click_that_makes_no_sense(self):
        _, body = self.page(WALL)
        foot = re.search(r'<footer class="foot">.*?</footer>', body, re.S).group(0)
        self.assertIn(render.KIOSK_HINT, foot)
        self.assertNotIn("<a ", foot)
        for word in ("pause", "Edit layout", "theme:", "density:", "text "):
            self.assertNotIn(word, foot)
        _, desk = self.page("/?app=1&ui=1.dd")
        self.assertIn("Edit layout", desk)  # the same page without the kiosk keeps its controls

    def test_a_wall_has_nothing_to_click_in_its_cards(self):
        _, body = self.page(WALL)
        self.assertNotIn("the whole card", body)  # the link to /?card= is not even sent
        self.assertNotIn("?card=", body)
        _, desk = self.page("/?app=1&ui=1.dd")
        self.assertIn("the whole card", desk)
        css = webcss.CSS
        for rule in ('html[data-density="wall"] .card p.more', 'html[data-density="wall"] details.fix',
                     'html[data-density="wall"] details.more>summary{pointer-events:none',
                     'html[data-density="wall"] .cb a{pointer-events:none'):
            self.assertIn(rule, css)

    def test_the_wall_keeps_its_scroll_position_across_the_ten_minute_reload(self):
        js = webjs.REFRESH_JS
        self.assertIn('sessionStorage.setItem("nuc-wall-y"', js)
        self.assertIn('sessionStorage.getItem("nuc-wall-y")', js)
        self.assertIn('nav.type === "reload"', js)  # a fresh visit starts at the top
        self.assertIn("setTimeout(again, 600000", js)  # the periodic reload goes through the function that saves the position
        self.assertNotIn("setTimeout(() => location.reload()", js)

    def test_without_scripts_the_meta_refresh_stays(self):
        _, body = self.page(WALL)
        self.assertRegex(body, r'<noscript><meta http-equiv="refresh" content="\d+"></noscript>')
        self.assertIn('data-card="__top"', body)  # the first screen is there

    def test_the_csp_lists_exactly_the_scripts_of_the_page(self):
        h, body = self.page(WALL)
        inline = re.findall(r"<script>(.*?)</script>", body, re.S)
        hashes = ["'sha256-%s'" % base64.b64encode(hashlib.sha256(x.encode()).digest()).decode() for x in inline]
        csp = h["Content-Security-Policy"]
        self.assertEqual(sorted(re.search(r"script-src ([^;]*)", csp).group(1).split()), sorted(hashes))
        self.assertIn(webjs.REFRESH_JS, inline)
        self.assertEqual(len(inline), 3)  # refresh, keys, prefs

    def test_a_classic_kiosk_page_is_not_a_wall(self):
        _, body = self.page("/?app=0&rotate=1&kiosk=1&fit=1")
        self.assertNotRegex(body, r"<main [^>]*data-rotate")
        self.assertIn(render.KIOSK_HINT, body)


class DisplayUrl(unittest.TestCase):
    def url(self, web_mode, **kw):
        with mock.patch.dict(render.CFG, {"ui": {"web": web_mode}}), mock.patch.dict(render.CFG["web"], {"port": 8787}):
            return render.dashboard_url(**kw)

    def test_classic_is_unchanged(self):
        self.assertEqual(self.url("classic"), "http://127.0.0.1:8787/?fit=1")
        self.assertEqual(self.url("classic", fullscreen=True, cols=200, rows=64), "http://127.0.0.1:8787/?fit=1&cols=200&rows=64&rotate=1&kiosk=1")

    def test_app_opens_the_shell_and_its_wall_display(self):
        self.assertEqual(self.url("app", fullscreen=True, cols=200, rows=64), "http://127.0.0.1:8787/?app=1&ui=1.dw&kiosk=1")
        self.assertEqual(self.url("app"), "http://127.0.0.1:8787/?app=1")

    def test_the_url_is_a_wall_page_for_the_server(self):
        q = weburl.view_params(parse_qs("app=1&ui=1.dw&kiosk=1"))
        self.assertEqual((q["app"], q["ui"], q["kiosk"]), ("1", "1.dw", True))

    def test_the_launcher_hands_the_browser_that_url(self):
        import tempfile
        for mode, want in (("app", "--app=http://127.0.0.1:8787/?app=1&ui=1.dw&kiosk=1"), ("classic", "--app=http://127.0.0.1:8787/?fit=1&cols=")):
            started = []
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(render.CFG, {"ui": {"web": mode}}), \
                    mock.patch.dict(render.CFG["web"], {"port": 8787, "token_file": ""}), \
                    mock.patch.object(render, "web_up", lambda port, wait: True), mock.patch.object(render, "find_browser", lambda *a: "/opt/browser"), \
                    mock.patch.object(render, "launch", started.append), mock.patch.object(render, "user_dir", lambda: d), \
                    mock.patch.object(render, "kiosk_grid", lambda: (200, 64)), \
                    mock.patch.object(render.nuc_config, "log_to", lambda path: None):  # --log would send this process's stdout and stderr to the file
                self.assertEqual(render.kiosk(["render.py", "--kiosk", "--log", os.path.join(d, "k.log")]), 0)
            self.assertTrue(started[0][1].startswith(want), (mode, started[0][1]))


if __name__ == "__main__":
    unittest.main()
