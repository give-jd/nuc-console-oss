"""tools/browser_check.py: the parts that need no browser (the page matrix, reading Chrome's log, the landmark check, the scriptless proxy)."""
import http.server
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "src"))
import browser_check as bc  # noqa: E402
import prefs  # noqa: E402

PFX = "[3344:3344:1002/125139.077449:%s:CONSOLE(1)] "


class Matrix(unittest.TestCase):
    def test_every_view_theme_density_with_scripts_on(self):
        names = {p.name for p in bc.build_matrix() if p.js}
        for view in bc.VIEWS:
            for theme, _c in bc.THEMES:
                for density, _d in bc.DENSITIES:
                    self.assertIn("%s/%s/%s/js" % (view, theme, density), names)
        self.assertEqual(len([p for p in bc.build_matrix() if p.js and p.kind == "wall"]), len(bc.THEMES))

    def test_scripts_off_covers_every_view_and_the_wall(self):
        off = [p for p in bc.build_matrix() if not p.js]
        self.assertEqual({p.kind for p in off}, set(bc.VIEWS) | {"wall"})

    def test_names_are_unique_and_quick_is_smaller(self):
        full, quick = bc.build_matrix(), bc.build_matrix(quick=True)
        self.assertEqual(len({p.name for p in full}), len(full))
        self.assertLess(len(quick), len(full))

    def test_paths_use_the_ui_grammar_of_prefs(self):
        for p in bc.build_matrix():
            self.assertTrue(p.path.startswith("/app?ui=1." if p.kind.startswith("app-") else "/?app=1&ui=1."), p.path)
            ui = p.path.split("ui=")[1].split("&")[0]
            got = prefs.parse_cookie(ui)
            self.assertEqual(set(got), {"theme", "density"}, (p.path, got))
        self.assertIn("kiosk=1", bc.page_path("overview", "a", "w", kiosk=True))
        self.assertIn("as=graph", bc.page_path("map-graph", "a", "k"))
        self.assertIn("edit=1", bc.page_path("layout", "a", "k"))
        self.assertEqual(prefs.parse_cookie(bc.ui_string("d", "w")), {"theme": "dark", "density": "wall"})


    def test_the_live_app_in_every_theme_with_its_stream_off(self):
        """It is the script that draws the app: no scriptless variant; live=0, or the stream keeps the page from ever settling."""
        app = [p for p in bc.build_matrix() if p.kind.startswith("app-")]
        self.assertEqual({p.kind for p in app}, {"app-" + v for v in bc.APP_VIEWS})
        self.assertTrue(all(p.js and "live=0" in p.path for p in app))
        for view in bc.APP_VIEWS:
            for theme, _c in bc.THEMES:
                self.assertIn("app-%s/%s/desk/js" % (view, theme), {p.name for p in app})
            self.assertIn("app-%s" % view, bc.VIEW_LANDMARKS)
        self.assertEqual(bc.app_path("overview", "d", "k"), "/app?ui=1.td.dk&live=0")
        self.assertEqual(bc.app_path("cpu", "a", "c"), "/app?ui=1.ta.dc&live=0&view=cpu")


class Parity(unittest.TestCase):
    DOM = ('<div class="kpiblock" id="kpis"><div class="kpis">K</div></div><!--END-KPIS--><main id="app" class="grid">'
           '<article class="card s2 st-ok r14" data-card="x">A</article></main><!--END-APP--><div id="expect-kpis"><div class="kpis">K</div></div>'
           '<!--END-EXPECT-KPIS--><div id="expect"><article class="card s2 st-ok" data-card="x">A</article></div><!--END-EXPECT-->')

    def test_the_same_markup_passes_and_the_measured_rows_do_not_count(self):
        self.assertEqual(bc.parity_problems(self.DOM, "overview"), [])

    def test_a_difference_is_named_with_where(self):
        got = bc.parity_problems(self.DOM.replace(">A</article></main>", ">B</article></main>"), "overview")
        self.assertEqual(len(got), 1)
        self.assertIn("the screen differs", got[0])
        got = bc.parity_problems(self.DOM.replace('<div class="kpis">K</div></div><!--END-KPIS-->', '<div class="kpis">Q</div></div><!--END-KPIS-->'), "overview")
        self.assertIn("the key figures differs", got[0])
        self.assertEqual(bc.parity_problems(self.DOM.replace('<div class="kpis">K</div></div><!--END-KPIS-->', '<div class="kpis">Q</div></div><!--END-KPIS-->'), "cpu"), [],
                         "the key figures are compared on the overview only")

    def test_a_missing_region_is_a_problem(self):
        self.assertEqual(bc.parity_problems("<html></html>", "cpu")[0], "no app region in the DOM")

    def test_the_parity_page_holds_both_drawings(self):
        text, csp = bc.parity_page("cpu")
        for marker in ("<!--END-APP-->", "<!--END-KPIS-->", '<div id="expect">', "<!--END-EXPECT-->", 'class="scr scr-cpu"', 'id="doc"'):
            self.assertIn(marker, text)
        self.assertIn("script-src 'sha256-", csp)
        self.assertNotIn("data-rev=", text.split('<div id="expect-kpis">')[1], "the shell's revisions are not the app's")


class Log(unittest.TestCase):
    def test_csp_violation(self):
        line = PFX % "INFO" + '"Refused to execute inline script because it violates the following Content Security Policy directive: ..." source: http://127.0.0.1:1/ (12)'
        got = bc.parse_log(line)
        self.assertEqual([k for k, _m in got], ["CSP"])
        self.assertTrue(got[0][1].startswith('"Refused to execute'))  # the prefix with pid and time is gone

    def test_refused_to_load_and_connect(self):
        for what in ("load the image 'http://x/y.png'", "connect to 'http://x/'", "apply inline style"):
            self.assertEqual(bc.parse_log(PFX % "ERROR" + '"Refused to %s because it violates ..."' % what)[0][0], "CSP", what)

    def test_trusted_types(self):
        for text in ("This document requires 'TrustedHTML' assignment.", "Failed to set the 'innerHTML' property: require-trusted-types-for"):
            self.assertEqual(bc.parse_log(PFX % "ERROR" + '"%s"' % text)[0][0], "Trusted Types", text)

    def test_script_errors_and_failed_loads(self):
        self.assertEqual(bc.parse_log(PFX % "ERROR" + '"Uncaught TypeError: x is not a function", source: http://h/ (3)')[0][0], "error")
        self.assertEqual(bc.parse_log(PFX % "ERROR" + '"Failed to load resource: net::ERR_BLOCKED_BY_CSP"')[0][0], "error")

    def test_an_error_level_console_line_counts(self):
        self.assertEqual(bc.parse_log(PFX % "ERROR" + '"something went wrong"')[0][0], "console error")

    def test_browser_noise_and_plain_console_logs_are_not_failures(self):
        noise = "\n".join([
            "[1:2:1002/124937.946675:ERROR:dbus/bus.cc:408] Failed to connect to the bus: No such file or directory",
            "[1:1:1002/124937.993620:WARNING:chrome/browser/signin/account_consistency_mode_manager.cc:74] Desktop Identity Consistency cannot be enabled",
            "[1:1:1002/124937.9:ERROR:gpu/command_buffer/service/gles2_cmd_decoder.cc:1] GL error",
            PFX % "INFO" + '"hello from the page"',
            "", "random text"])
        self.assertEqual(bc.parse_log(noise), [])


class Landmarks(unittest.TestCase):
    COMMON = ('<!doctype html><html><body><header class="topbar"></header><nav class="tabs" aria-label="Screens"></nav>'
              '<main class="view">%s</main><footer></footer></body></html>')

    def test_complete_pages(self):
        pages = {"overview": '<div class="kpis"></div><article class="card st-ok">', "map": '<div class="scr mapv"></div>',
                 "map-graph": '<div id="gv"><svg></svg></div>', "cpu": '<div class="scr scr-cpu"></div>', "health": '<div class="hv"></div>',
                 "ai": '<div class="scr av"></div>', "settings": '<div class="settings"></div>',
                 "telegram": '<div class="settings av" id="tg"><form action="/telegram/pair"></form></div>',
                 "layout": '<span data-edit></span><article class="card x">', "wall": '<div class="kpis"></div><article class="card st-ok">',
                 "app-overview": '<a class="kpi st-ok" role="listitem"></a><article class="card s1">', "app-cpu": '<div class="scr scr-cpu"></div>',
                 "app-health": '<div class="hv"></div>', "app-map": '<div class="scr mapv"></div>', "app-ai": '<div class="scr av"></div>'}
        self.assertEqual(set(pages), set(bc.VIEW_LANDMARKS))
        for kind, inner in pages.items():
            self.assertEqual(bc.missing_landmarks(self.COMMON % inner, kind), [], kind)

    def test_missing_pieces_are_named(self):
        dom = self.COMMON % '<div class="kpis"></div>'
        self.assertEqual(bc.missing_landmarks(dom, "overview"), ["a card"])
        self.assertEqual(bc.missing_landmarks(dom.replace('<header class="topbar">', "<header>"), "overview")[0], "the top bar")
        self.assertIn("<main>", bc.missing_landmarks(dom.replace("<main", "<div"), "overview"))
        self.assertEqual(bc.missing_landmarks("", "cpu"), ["a document"])

    def test_an_error_page_has_no_landmarks(self):
        self.assertGreaterEqual(len(bc.missing_landmarks("<html><body>421 Misdirected</body></html>", "cpu")), 4)


class Browser(unittest.TestCase):
    def test_find_chrome_prefers_the_argument_then_the_environment(self):
        me = os.path.abspath(__file__)
        self.assertEqual(bc.find_chrome(me, {}), me)
        self.assertEqual(bc.find_chrome(None, {"CHROME": me}), me)
        self.assertIsNone(bc.find_chrome("/nonexistent/chrome-" + "x" * 8, {}))

    def test_arguments(self):
        a = bc.chrome_args("/c", "/p", root=True)
        for want in ("--headless=new", "--user-data-dir=/p", "--enable-logging=stderr", "--no-sandbox"):
            self.assertIn(want, a)
        self.assertNotIn("--no-sandbox", bc.chrome_args("/c", "/p", root=False))
        self.assertTrue(any(x.startswith("--virtual-time-budget=") for x in a))


class NoScript(unittest.TestCase):
    def test_strip_scripts(self):
        html = '<p>a</p><script>var x = 1;\n</script><script type="module" src="/a.js"></script ><b>c</b>'
        self.assertEqual(bc.strip_scripts(html), "<p>a</p><b>c</b>")

    def test_proxy_forwards_and_strips(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b"<html><script>1</script><main>hi</main></html>" if self.path == "/" else b"nope"
                self.send_response(200 if self.path == "/" else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8" if self.path == "/" else "text/plain")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        up = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        proxy = bc.NoScriptProxy("http://127.0.0.1:%d" % up.server_address[1])
        try:
            r = urllib.request.urlopen(proxy.base + "/", timeout=10)
            self.assertEqual(r.read(), b"<html><main>hi</main></html>")
            self.assertEqual(r.headers["Content-Security-Policy"], "default-src 'none'")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(proxy.base + "/other", timeout=10)
            self.assertEqual(cm.exception.code, 404)
        finally:
            proxy.close()
            up.shutdown()
            up.server_close()


if __name__ == "__main__":
    unittest.main()
