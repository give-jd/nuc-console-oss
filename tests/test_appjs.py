"""The live app (/app, src/appjs.py): its script under the rules of tests/jsrules.py, and the page web.py serves for it (the frame, the
settings and the first documents it carries, its CSP, the same access rules as the other pages). What it draws is compared with what
htmlview.py draws by tools/browser_check.py, in a real browser (the parity check)."""
import json
import os
import re
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import appjs  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
import weburl  # noqa: E402
import webapi  # noqa: E402
import webjs  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jsrules  # noqa: E402
from test_web import TOKEN, get_any as get, raw, serve  # noqa: E402

_SAVED = {}


def setUpModule():  # the demo servers' render.demo_defaults() renames the host and declares [webapps] and [expose] for good: put them back
    _SAVED.update(host=socket.gethostname, webapps=render.CFG["webapps"], expose=render.CFG["expose"], demo=render.DEMO)


def tearDownModule():
    socket.gethostname, render.CFG["webapps"], render.CFG["expose"], render.DEMO = _SAVED["host"], _SAVED["webapps"], _SAVED["expose"], _SAVED["demo"]

DATA = re.compile(r'<script type="application/json" id="(cfg|doc)">(.*?)</script>', re.S)


def blocks(body):
    return {k: json.loads(v) for k, v in DATA.findall(body)}


class Rules(unittest.TestCase):
    def test_the_script_obeys_its_policy(self):
        self.assertEqual(jsrules.check("app", appjs.APP_JS), [])

    def test_it_builds_only_harmless_elements_and_attributes(self):
        never = {"script", "style", "iframe", "frame", "object", "embed", "link", "meta", "base", "img", "image", "template", "foreignobject",
                 "use", "animate", "set", "math", "audio", "video", "source", "canvas"}
        self.assertFalse(never & {t.lower() for t in appjs.HTML_TAGS + appjs.SVG_TAGS})
        self.assertFalse(any(a.startswith("on") or a in ("style", "src", "srcdoc", "srcset", "formaction", "target", "xlink:href") for a in appjs.ATTRS))
        self.assertTrue(set(appjs.HTML_TAGS) <= set(webjs.FRAG_TAGS), "the tags a fragment of the shell may hold")
        self.assertEqual(set(appjs.ATTRS) - set(webjs.FRAG_ATTRS), {"data-truncated"})

    def test_the_lifted_bans_are_still_bans_for_the_other_scripts_and_markup_is_banned_for_the_app(self):
        for name, src in dict(webjs.SCRIPTS).items():
            for bad in ("document.createElement(\"div\")", "new EventSource(\"/api/v1/stream?view=cpu\")", "history.pushState(null, \"\", \"/x\")"):
                self.assertTrue(jsrules.check(name, src.replace("\"use strict\";", "\"use strict\";\n  " + bad + ";", 1)), (name, bad))
        start = "\"use strict\";"
        for bad in ("app.innerHTML = s;", "new DOMParser();", "history.pushState(null, \"\", \"/elsewhere\");", "new EventSource(u);",
                    "fetch(\"/x\");", "location.assign(u);", "document.cookie;", "const w = \"wss://example.com\";", "document.createElement(t);"):
            self.assertTrue(jsrules.check("app", appjs.APP_JS.replace(start, start + "\n  " + bad, 1)), bad)

    def test_its_doors_open_only_this_servers_api_and_forms(self):
        src = appjs.APP_JS
        self.assertIn('url.startsWith("/api/v1/stream?")', src)
        self.assertIn('/^\\/(ai|telegram)\\/[a-z-]+$/.test(url) : url.startsWith("/api/v1/")', src)
        self.assertIn('redirect: "manual"', src, "a form's redirect is not followed: the stream brings its effect")


class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = serve()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def page(self, path="/app", headers=None):
        st, h, body = get(self.srv, path, headers=headers)
        self.assertEqual(st, 200, path)
        return h, body

    def test_the_page_carries_the_frame_the_settings_and_the_first_documents(self):
        h, body = self.page()
        self.assertEqual(h["Content-Type"], "text/html; charset=utf-8")
        for landmark in ('<header class="topbar"', '<nav class="tabs"', '<main id="app" class="grid">', 'id="kpis"', 'id="stale"', '<footer class="foot">',
                         'id="help"', "the live app needs JavaScript"):
            self.assertIn(landmark, body)
        got = blocks(body)
        self.assertEqual(set(got), {"cfg", "doc"})
        self.assertEqual(set(got["doc"]), {"overview"})
        self.assertEqual(got["doc"]["overview"]["api"], webapi.VERSION)
        cfg = got["cfg"]
        self.assertEqual(cfg["refresh"], 2)
        self.assertEqual([t[1] for t in cfg["tabs"]], ["overview", "map", "cpu", "health", "ai"])
        self.assertTrue(cfg["layout"] and all(len(x) == 2 for x in cfg["layout"]))
        self.assertIn(cfg["order"], ("severity", "fixed"))
        self.assertIn('href="/app?view=cpu"', body, "the tabs are the app's own links")

    def test_a_screen_carries_its_document_and_the_summary(self):
        _h, body = self.page("/app?view=cpu&sort=mem")
        docs = blocks(body)["doc"]
        self.assertEqual(set(docs), {"cpu", "summary"})
        self.assertEqual(docs["cpu"]["sort"], "mem")
        self.assertIn('<main id="app" class="view">', body)
        for path in ("/app?view=settings", "/app?view=telegram", "/app?view=bogus"):
            self.assertEqual(set(blocks(self.page(path)[1])["doc"]), {"overview"}, path)
        with mock.patch.dict(render.CFG["features"], {"health": False}):
            self.assertEqual(set(blocks(self.page("/app?view=health")[1])["doc"]), {"overview"}, "a screen that is off: the overview")

    def test_the_csp_lists_its_three_scripts_and_no_markup_policy(self):
        h, body = self.page()
        csp = h["Content-Security-Policy"]
        hashes = set(re.findall(r"'sha256-[^']+'", csp))
        self.assertEqual(hashes, {appjs.csp_source(s) for s in (appjs.APP_JS, webjs.KEYS_JS, webjs.PREFS_JS)})
        for part in ("default-src 'none'", "style-src 'self'", "connect-src 'self'", "form-action 'self'", "require-trusted-types-for 'script'",
                     "trusted-types 'none'", "frame-ancestors 'none'", "base-uri 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe", csp)
        inline = re.findall(r"<script>(.*?)</script>", body, re.S)
        self.assertEqual(inline, [appjs.APP_JS, webjs.KEYS_JS, webjs.PREFS_JS])
        self.assertEqual(h["Referrer-Policy"], "same-origin")
        self.assertEqual(h["Vary"], "Cookie")

    def test_nothing_in_a_document_can_end_its_element(self):
        hostile = webapi.document("overview", {"note": "</script><script>alert(1)</script><!-- & -->"}, 1.0)
        with mock.patch.object(self.srv, "api", return_value=hostile):
            _h, body = self.page()
        data = body.split('id="doc">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<", data)
        self.assertNotIn(">", data)
        self.assertEqual(blocks(body)["doc"]["overview"]["note"], "</script><script>alert(1)</script><!-- & -->")

    def test_live_0_is_a_snapshot(self):
        self.assertIn('<main id="app" class="grid" data-static>', self.page("/app?live=0")[1])
        self.assertIn('<main id="app" class="grid">', self.page("/app")[1])

    def test_the_preferences_of_the_browser(self):
        _h, body = self.page("/app?ui=1.td.dc")
        self.assertIn('data-theme="dark"', body)
        self.assertIn('data-density="compact"', body)
        self.assertIn('data-prefs-src="url"', body)
        self.assertIn('data-prefs-src="config"', self.page("/app")[1], "no cookie: the preferences are config.ini's (PREFS_JS reads it)")
        _h, body = self.page("/app", headers={"Cookie": "nuc_ui=1.tl"})
        self.assertIn('data-theme="light"', body)
        self.assertIn('data-prefs-src="cookie" data-prefs="1.tl"', body)

    def test_access_is_the_pages(self):
        self.assertIn(" 421 ", raw(self.srv, "/app", [("Host", "evil.example.com")]).split("\r\n")[0] + " ")
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(get(self.srv, "/app", m)[0], 405, m)
        for path in ("/app/", "/app/x", "/apps", "/app.js"):
            self.assertEqual(get(self.srv, path)[0], 404, path)
        wire = raw(self.srv, "/app", [("Host", "localhost")], "HEAD")
        self.assertIn(" 200 ", wire.split("\r\n")[0])
        self.assertEqual(wire.partition("\r\n\r\n")[2], "")

    def test_the_token(self):
        srv = serve(TOKEN)
        try:
            self.assertEqual(get(srv, "/app")[0], 401)
            st, h, _b = get(srv, "/app?view=cpu&sort=mem&token=" + TOKEN)
            self.assertEqual((st, h["Location"]), (302, "/app?view=cpu&sort=mem"), "the token moves to a cookie, the screen stays")
            self.assertIn("nuc_token=" + TOKEN, h["Set-Cookie"])
            self.assertEqual(get(srv, "/app", headers={"Cookie": "nuc_token=" + TOKEN})[0], 200)
        finally:
            srv.shutdown()
            srv.server_close()


class Addresses(unittest.TestCase):
    def test_app_url_keeps_the_screen_and_what_it_takes(self):
        p = lambda **q: weburl.view_params({k: [v] for k, v in q.items()})  # noqa: E731
        self.assertEqual(weburl.app_url(p()), "/app")
        self.assertEqual(weburl.app_url(p(view="cpu", sort="mem", zoom="150", sel="12")), "/app?view=cpu&sort=mem&sel=12")
        self.assertEqual(weburl.app_url(p(view="health", period="30")), "/app?view=health&period=30")
        self.assertEqual(weburl.app_url(p(view="map", open="0123456789.abcdefabcd", only="1")), "/app?view=map&open=0123456789.abcdefabcd&only=1")
        self.assertEqual(weburl.app_url(p(view="settings")), "/app")
        self.assertEqual(weburl.app_url(p(view="map", **{"as": "graph"})), "/app?view=map")


if __name__ == "__main__":
    unittest.main()
