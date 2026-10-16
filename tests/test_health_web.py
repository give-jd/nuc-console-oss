"""The web HEALTH page (web.py ?view=health): the link, the findings as links that carry the view, period and sel validation, the report
computed once a minute per period, escaping, the Windows and macOS demos, the advisor hook.

Hermetic: the demo reports (src/demo.py) through a real server on a loopback port; no history.db is read; every module global a test
changes is put back in tearDown.
"""
import html
import http.client
import os
import re
import socket
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import threading
from unittest import mock
import time
import unittest
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import demo  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import ui  # noqa: E402
import web  # noqa: E402
import webpages  # noqa: E402
import weburl  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ESC, BEL = chr(27), chr(7)
EVIL = '<script>alert(1)</script>"onmouseover=alert(1) \'x ' + ESC + "[2J" + ESC + "]0;pwn" + BEL
ROW = re.compile(r'<div class="fd [^"]*" data-k="f-([^"]*)"><details data-k="[^"]*"( open)?><summary>.*?<strong class="ft">(.*?)</strong> <span class="fx">(.*?)</span>')


def serve():
    cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
    srv = web.Server(("127.0.0.1", 0), cfg, "", demo=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def get(srv, path="/"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, dict(r.getheaders()), body


def rows_of(page):
    """[(id, open, title, text)] of the findings of a health page (a selected finding is the one whose details are open)."""
    return [(html.unescape(i), bool(o), html.unescape(t), html.unescape(x)) for i, o, t, x in ROW.findall(page)]


def link(page, text):
    m = re.search(r'<a (?:[^>]*? )?href="([^"]*)"(?: [^>]*)?>(?:<kbd>\d</kbd>)?' + re.escape(text) + "(?: <span.*?</span>)?</a>", page)
    return html.unescape(m.group(1)) if m else None


def params(url):
    return weburl.view_params(web.parse_qs(web.urlsplit(url).query))


def plain(page):
    return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<style>.*?</style>", "", page, flags=re.S)))


class HealthPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host, cls.webapps, cls.expose = socket.gethostname, render.CFG["webapps"], render.CFG["expose"]  # demo_defaults() changes them for good: put them back
        cls.srv = serve()
        cls.saved = (dict(render.CFG["features"]), render.DEMO_OS, render.DEMO_HEALTH, render.health_build)

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        socket.gethostname, render.CFG["webapps"], render.CFG["expose"] = cls.host, cls.webapps, cls.expose
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        render.DEMO, render.DEMO_OS, render.DEMO_HEALTH = True, None, ""
        render.CFG["features"]["health"] = True
        render._HEALTH.clear()
        self.srv.cache.clear()
        self.calls = []                                                      # the days of every health_build()
        real = render.health_build
        render.health_build = lambda days, now: (self.calls.append(days), real(days, now))[1]

    def tearDown(self):
        features, render.DEMO_OS, render.DEMO_HEALTH, render.health_build = self.saved
        render.CFG["features"].clear()
        render.CFG["features"].update(features)
        render._HEALTH.clear()

    def page(self, path):
        st, h, body = get(self.srv, path)
        self.assertEqual(st, 200, path)
        self.assertNotIn("render error", body, path)
        return body

    def seed(self, rep, days=7):
        render._HEALTH[days] = {"report": rep, "msg": "", "err": False, "at": time.time()}

    # ---- the link ---------------------------------------------------------------------------------------------------------------------

    def test_health_link_in_the_bottom_bar_with_and_without_the_feature(self):
        dash = self.page("/")
        self.assertEqual(link(dash, "Health"), "/?view=health")
        self.assertEqual(link(dash, "Map"), "/?view=map")                    # next to it
        self.assertLess(dash.index(">Map "), dash.index(">Health "))
        self.assertEqual(params(link(self.page("/?zoom=125"), "Health"))["zoom"], 125)   # the size is kept
        render.CFG["features"]["health"] = False
        self.srv.cache.clear()
        del self.calls[:]
        off = self.page("/")
        self.assertIsNone(link(off, "Health"))
        self.assertNotIn("view=health", off)
        self.assertEqual(self.calls, [])                                     # off: nothing reads the history (on, the tab's badge asks for the 7 days)
    def test_feature_off_the_page_says_so_and_reads_nothing(self):
        render.CFG["features"]["health"] = False
        st, h, off = get(self.srv, "/?view=health&sel=cpu-hog%3Achrome")
        self.assertEqual(st, 200)
        self.assertIn("health disabled in config.ini", off)
        self.assertNotIn('class="fd', off)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(self.calls, [])
    # ---- the page ---------------------------------------------------------------------------------------------------------------------

    def test_page_has_header_title_findings_sections_and_is_locked_down(self):
        st, h, body = get(self.srv, "/?view=health")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual((h["Cache-Control"], h["X-Content-Type-Options"], h["Referrer-Policy"]), ("no-store", "nosniff", "no-referrer"))
        self.assertNotIn("<form", body.lower())
        self.assertIsNone(re.search(r"<[^>]*\son[a-z]+=", body))             # no inline event handler
        self.assertIn('<span class="host">demo-host</span><span class="status err"', body)   # the demo has problems: the red status
        self.assertIn('<noscript><meta http-equiv="refresh" content="2"></noscript>', body)
        self.assertIn("<title>demo-host · health · nuc-console</title>", body)
        txt = plain(body)
        self.assertIn("HEALTH", txt)
        self.assertIn("last 7 days", txt)
        self.assertIn("168 h of data", txt)
        self.assertIn("✖ 1 err", txt)
        rs = rows_of(body)
        self.assertEqual(len(rs), 10)
        self.assertEqual([r[2] for r in rs][:3], ["Out of memory: shop-worker-1", "Restarting: shop-worker-1", "Disk filling up: /data"])
        self.assertEqual(len({r[0] for r in rs}), len(rs))                   # one entry per finding
        for fid, sel, title, text in rs:
            self.assertIn(fid, {f["id"] for f in screens.health_findings(render.health_data(7)["report"])})
            self.assertGreater(len(text), 20)
        for pill in ('<span class="tag err">✖ ERR</span>', '<span class="tag warn">! WARN</span>', '<span class="tag info">· INFO</span>'):
            self.assertIn(pill, body)                                        # a symbol besides the colour
        for section in ("TOP CPU", "TOP MEMORY", "EVENTS", "NOISY / NEW LOGS", "DISKS", "THERMAL", "BOOTS"):
            self.assertIn(section, txt)
        self.assertEqual([r for r in rs if r[1]], [])                        # nothing selected: nothing open
        self.assertEqual(self.calls, [7])
    def test_footer_links_keep_the_view(self):
        body = self.page("/?view=health&period=30&sel=disk-full%3A%2Fdata&zoom=125&refresh=5")
        self.assertIsNone(link(body, "30d"))                                 # the period in use is not a link
        self.assertIn('<span aria-current="true">30d</span>', body)
        p = params(link(body, "24h"))
        self.assertEqual((p["view"], p["period"], p["sel"], p["zoom"], p["refresh"]), ("health", 1, "disk-full:/data", 125, 5))
        self.assertEqual(params(link(body, "7d"))["period"], 0)              # 0 = the default
        self.assertNotIn("period=7", link(body, "7d"))
        self.assertEqual(params(link(body, "pause"))["pause"], True)
        self.assertEqual(params(link(body, "A+"))["zoom"], 150)
        self.assertEqual(params(link(body, "+"))["refresh"], 6)
        self.assertIn("read-only", body)

    def test_selecting_a_finding_shows_its_details_beside_or_under_the_list(self):
        body = self.page("/?view=health")
        self.assertEqual([r[1] for r in rows_of(body)].count(True), 0)
        fid = rows_of(body)[3][0]                                            # mem-leak:node
        self.assertEqual(fid, "mem-leak:node")
        sel = self.page("/?view=health&sel=" + quote(fid, safe=""))
        rs = rows_of(sel)
        self.assertEqual([r[1] for r in rs].count(True), 1)
        self.assertTrue(rs[3][1])                                            # its details are open
        panel = sel[sel.index('data-k="f-mem-leak:node"'):]
        panel = panel[:panel.index("</details>")]
        txt = plain(panel)
        self.assertIn("Memory keeps growing: node", txt)
        self.assertIn("a leak is possible", txt)
        self.assertIn("slope_mb_day", txt)
        self.assertIn("180.2", txt)
        self.assertIn("fix", txt)
        self.assertIn("docker update --memory 1g", txt)
        self.assertEqual(self.calls, [7])                                    # the report was asked for once
    def test_every_finding_of_every_demo_and_period_opens(self):
        for os_name in (None, "windows", "darwin"):
            render.DEMO_OS = os_name
            for days in (1, 7, 30):
                render._HEALTH.clear()
                self.srv.cache.clear()
                ids = [f["id"] for f in screens.health_findings(render.health_data(days)["report"])]
                self.assertTrue(ids)
                for fid in ids:
                    with self.subTest(os=os_name, days=days, finding=fid):
                        body = self.page("/?view=health&period=%d&sel=%s" % (days, quote(fid, safe="")))
                        self.assertEqual([r[0] for r in rows_of(body) if r[1]], [fid])
    def test_windows_and_macos_demos(self):
        render.DEMO_OS = "windows"
        win = self.page("/?view=health")
        txt = plain(win)
        for word in ("Unexpected shutdown", "Crashing: contoso-sync.exe", "Crashing: Spooler", "MsMpEng.exe", "C:", "never hot", "max 91", "Application Error 1000"):
            self.assertIn(word, txt)
        self.assertNotIn("WHEA", txt)                                        # none reported: no finding, no event kind
        self.assertNotIn("hardware", txt)
        self.assertIn("unexpected off", txt)
        render.DEMO_OS = "darwin"
        render._HEALTH.clear()
        self.srv.cache.clear()
        mac = plain(self.page("/?view=health"))
        for word in ("Out of memory: Google Chrome Helper", "Crashing: photolibraryd", "mds_stores", "/Volumes/Data", "max 74"):
            self.assertIn(word, mac)
        self.assertNotIn("contoso", mac)
        self.assertIn("none recorded in this period", mac)                   # no log history on macOS
        self.assertIn("no boot times recorded", mac)

    def test_little_data_and_no_history(self):
        render.DEMO_HEALTH = "little"
        little = self.page("/?view=health")
        self.assertIn("· collecting: 5 hours so far; trends need 24 hours of data", plain(little))
        self.assertEqual(len(rows_of(little)), 2)
        self.assertIn("5 h of data", plain(little))
        render.DEMO_HEALTH = "none"
        render._HEALTH.clear()
        self.srv.cache.clear()
        none = self.page("/?view=health")
        self.assertIn(html.escape(screens.HEALTH_NONE), none)
        self.assertEqual(rows_of(none), [])
        self.assertNotIn('class="dp"', none)
        self.assertNotIn("── TOP CPU", plain(none))
        self.assertIn('<meta http-equiv="refresh"', none)                     # it will appear after the first hour

    def test_the_findings_of_a_report_with_none_say_so(self):
        rep = demo.health_report(None, 7, time.time())
        rep["findings"] = []
        self.seed(rep)
        self.assertIn("nothing to report in this period", self.page("/?view=health"))
        rep["coverage"] = {"hours": 6, "since": time.time() - 6 * 3600}
        self.srv.cache.clear()
        self.assertIn("too little data to conclude anything yet", self.page("/?view=health"))
        body = self.page("/?view=health")
        self.assertNotIn("✔", body[body.index("<main"):body.index("</main>")])

    def test_history_unreadable_is_a_message(self):
        render._HEALTH[7] = {"report": None, "msg": "the history could not be read: DatabaseError('x')", "err": True, "at": time.time()}
        body = self.page("/?view=health")
        self.assertIn("the history could not be read: DatabaseError(&#x27;x&#x27;)", body)
        self.assertIn('<p class="msg lv-err">', body)
        self.assertEqual(rows_of(body), [])

    # ---- parameters -------------------------------------------------------------------------------------------------------------------

    def test_invalid_parameters_are_dropped(self):
        q = lambda s: weburl.view_params(web.parse_qs(s))  # noqa: E731
        self.assertEqual(q("view=health")["period"], 0)
        self.assertEqual([q("view=health&period=%s" % x)["period"] for x in ("1", "7", "30", "2", "0", "-1", "31", "x", "1.0", "%C2%B2", "", "7" * 5000)],
                         [1, 7, 30, 0, 0, 0, 0, 0, 0, 0, 0, 0])
        self.assertNotIn("period", q("view=map&period=30"))                  # not the map's
        self.assertNotIn("period", q("period=30"))
        self.assertEqual(q("view=health&sel=cpu-hog:chrome")["sel"], "cpu-hog:chrome")  # any text: the page checks it against the report
        self.assertEqual(len(q("view=health&sel=" + "a" * 5000)["sel"]), weburl.HEALTH_SEL_MAX)
        self.assertEqual(q("view=map&sel=cpu-hog:chrome")["sel"], "")        # the map's sel stays ten hex digits
        self.assertEqual(q("view=map&sel=0123456789")["sel"], "0123456789")
        self.assertEqual((q("view=HEALTH")["view"], q("view=health")["view"], q("view=healthy")["view"]), ("", "health", ""))
        for path in ("/?view=health&period=99&sel=" + "a" * 5000, "/?view=health&sel=%00&period=%C2%B2", "/?view=health&sel=<script>&zoom=%C2%B2&refresh=-1",
                     "/?view=health&all=1&open=x&shut=y&only=1"):
            body = self.page(path)
            self.assertNotIn("<script>alert", body.lower())
            self.assertEqual([r for r in rows_of(body) if r[1]], [], path)   # no such finding: none open, the default period
            self.assertIn("last 7 days", plain(body))

    def test_a_selection_that_is_not_in_the_report_is_dropped(self):
        for sel in ("nope", "cpu-hog:", "cpu-hog:chrome ", "CPU-HOG:chrome", "oom:shop-worker-1\n", "cpu-hog:chrome\x00", "../etc/passwd", "x" * 300,
                    "<script>alert(1)</script>"):
            body = self.page("/?view=health&sel=" + quote(sel))
            self.assertEqual([r for r in rows_of(body) if r[1]], [], repr(sel))
            self.assertNotIn("alert(1)", body)                               # and never repeated in a link
            self.assertNotIn("../etc", body)
            self.assertNotIn("x" * 50, body)
        good = self.page("/?view=health&sel=cpu-hog%3Achrome")
        self.assertEqual([r[0] for r in rows_of(good) if r[1]], ["cpu-hog:chrome"])
        self.assertEqual([r for r in rows_of(self.page("/?view=health&period=1&sel=mem-leak%3Anode")) if r[1]], [])   # not in the 24 hour report

    def test_a_selection_of_another_period_follows_the_period(self):
        body = self.page("/?view=health&period=30&sel=mem-leak%3Anode")
        self.assertEqual([r[0] for r in rows_of(body) if r[1]], ["mem-leak:node"])
        self.assertIn("last 30 days", plain(body))
        self.assertEqual(self.calls.count(30), 1)                            # (the tab's badge asks for the 7 days)

    def test_the_periods_show_their_own_report(self):
        day, week, month = (plain(self.page("/?view=health&period=" + p)) for p in ("1", "7", "30"))
        self.assertIn("last 24 hours", day)
        self.assertIn("per hour", day)
        self.assertNotIn("Memory keeps growing: node", day)
        self.assertIn("memory trends need a period of at least 3 days", day)
        self.assertIn("Memory keeps growing: node", week)
        self.assertIn("384 h of data", month)
        self.assertIn("last 30 days", month)
        self.assertEqual(sorted(self.calls), [1, 7, 30])

    def test_pause_stops_the_reload(self):
        body = self.page("/?view=health&pause=1")
        self.assertIn('data-paused="1"', body)
        self.assertEqual(params(link(body, "resume"))["pause"], False)
        self.assertNotIn("pause=", link(body, "resume"))

    # ---- the report cache -------------------------------------------------------------------------------------------------------------

    def test_the_report_is_computed_once_a_minute_per_period_whatever_the_requests(self):
        for i in range(30):
            self.page("/?view=health")
            self.page("/?view=health&sel=%s" % ("cpu-hog%3Achrome", "oom%3Ashop-worker-1", "nope")[i % 3])
            self.page("/?view=health&zoom=%d&refresh=%d" % (50 + 25 * (i % 3), 1 + i % 10))
            self.srv.cache.clear()                                           # not even the page cache helps: only the report's own
        self.assertEqual(self.calls, [7])
        for p in ("1", "30", "1", "30", "7"):
            self.page("/?view=health&period=" + p)
        self.assertEqual(self.calls, [7, 1, 30])
        render._HEALTH[7]["at"] -= render.HEALTH_TTL + 1                     # a minute later
        self.page("/?view=health")
        self.assertEqual(self.calls, [7, 1, 30, 7])
        self.page("/")                                                       # the dashboard does not ask for it
        self.assertEqual(self.calls, [7, 1, 30, 7])

    def test_concurrent_requests_share_one_computation(self):
        slow = []
        inner = render.health_build

        def slow_build(days, now):
            slow.append(days)
            time.sleep(0.2)
            return inner(days, now)
        render.health_build = slow_build
        out = []
        threads = [threading.Thread(target=lambda: out.append(get(self.srv, "/?view=health&zoom=%d" % (50 + 10 * i))[0])) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(out, [200] * 6)
        self.assertEqual(slow, [7])

    def test_the_page_cache_holds_only_values_that_exist(self):
        for i in range(web.CACHE_MAX * 3):
            self.page("/?view=health&sel=junk%d&period=%d&pause=%d" % (i, (1, 7, 30, 99)[i % 4], i % 2))
        keys = [k for k in self.srv.cache if k[0] == "health"]
        self.assertLessEqual(len(self.srv.cache), web.CACHE_MAX)
        self.assertLessEqual(len(keys), 3 * 2)                               # 3 periods x paused or not: a junk selection is the empty one
        self.assertNotIn("junk", "".join(str(k) for k in self.srv.cache))
        first = self.srv.page(**weburl.view_params(web.parse_qs("view=health")))
        self.assertIs(self.srv.page(**weburl.view_params(web.parse_qs("view=health"))), first)    # within r/2: the same render

    # ---- hostile data -----------------------------------------------------------------------------------------------------------------

    def hostile(self):
        e = EVIL
        rep = demo.health_report(None, 7, time.time())
        rep["findings"] = [
            {"id": "oom:" + e, "level": "err", "title": "Out of memory: " + e, "text": "text " + e, "fix": "fix <img src=x onerror=alert(1)> " + e,
             "facts": {"n": 3, e: e, "<b>": "<i>"}, "subject": e},
            {"id": "crash-loop:b", "level": "bogus", "title": None, "text": None, "fix": None, "facts": "no", "subject": None}]
        rep["top_cpu"] = [{"app": e, "cpu_s": 1, "share": 0.5, "avg_pct": 3, "peak_hour": 10, "series": [1, 2]}]
        rep["top_mem"] = [{"app": e, "rss_max": 1, "rss_avg": 1, "trend_mb_day": 1}]
        rep["events"] = {e: [{"subject": e, "n": 1, "last": 1}], "crash": [{"subject": e, "n": 3, "last": 1}]}
        rep["logs"] = [{"source": e, "unit": e, "template": e, "n": 3, "new": True}]
        rep["disks"] = [{"mount": e, "used_pct": 50, "days_to_full": 3}]
        rep["thermal"] = {"hours_hot": 1, "max": 90, "apps_when_hot": [{"app": e, "share": 0.5}]}
        rep["boots"] = []
        rep["notes"] = [e]
        return rep

    def test_html_and_control_characters_in_the_report_are_inert(self):
        self.seed(self.hostile())
        fid = screens.health_findings(render.health_data(7)["report"])[0]["id"]
        for path in ("/?view=health", "/?view=health&sel=" + quote(fid, safe="")):
            body = self.page(path)
            self.assertEqual(re.findall(r"<script>alert|<img|<iframe|<svg onload|<form|<object|<embed", body, re.I), [], path)
            self.assertIsNone(re.search(r"<[^>]*\son[a-z]+=", body), path)                 # no event handler inside any tag
            self.assertNotIn(ESC, body)
            self.assertNotIn(BEL, body)
            self.assertNotIn("<script>alert(1)</script>", body)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)     # shown, as text
        body = self.page("/?view=health&sel=" + quote(fid, safe=""))
        self.assertIn("?[2J?]0;pwn?", plain(body))                           # the escape sequence is visible text, not an escape
        self.assertEqual(len([r for r in rows_of(body) if r[1]]), 1)
        for href in re.findall(r'<a [^>]*?href="([^"]*)"', body):            # every link is one of ours, the id percent-encoded
            self.assertTrue((href.startswith("/") and not href.startswith("//")) or href.startswith("#"), href)
            self.assertNotIn("<", html.unescape(href))
            self.assertNotIn('"', html.unescape(href))

    def test_a_hostile_message_is_escaped_too(self):
        render._HEALTH[7] = {"report": None, "msg": "the history " + EVIL + " could not be read", "err": True, "at": time.time()}
        body = self.page("/?view=health")
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertNotIn(ESC, body)
        self.assertIn("&lt;script&gt;", body)

    def test_a_failing_state_read_is_an_error_line_not_a_crash(self):
        real = render.health_state

        def broken(smp, days):
            raise ValueError("state unreadable " + ESC + "[2J")
        render.health_state = broken
        try:
            st, _, body = get(self.srv, "/?view=health&sel=cpu-hog%3Achrome")
        finally:
            render.health_state = real
        self.assertEqual(st, 200)
        self.assertIn("could not be drawn (see the service log)", body)
        self.assertNotIn("state unreadable", body)                           # the detail goes to the log, not to the page
        self.assertNotIn(ESC, body)

    def test_a_malformed_report_is_an_error_line_not_a_crash(self):
        for bad in ({"period": {"days": 7}, "coverage": {"hours": 5, "since": 1}, "findings": [None, 3, {"id": 5}, {"id": "a:b", "title": ["x"], "facts": {"k": [1]}}],
                     "top_cpu": [None, "x"], "events": [], "logs": "no", "disks": {}, "thermal": [], "notes": [None, 3, "ok"]},):
            self.seed(bad)
            st, _, body = get(self.srv, "/?view=health")
            self.assertEqual(st, 200)
            self.assertNotIn("Traceback", body)
            self.assertNotIn("repr(", body)

    # ---- the advisor hook -------------------------------------------------------------------------------------------------------------

    def test_the_hook_is_empty_by_default_and_its_block_goes_under_the_findings(self):
        self.assertIsNone(webpages.health_advice_node(demo.health_report(None, 7)))
        self.assertNotIn("ADVICE", self.page("/?view=health"))
        seen = []

        def advice(rep):
            seen.append(rep["period"]["days"])
            return ui.Advice("ADVICE (AI) check before acting", [["restart <b>x</b>"]])
        with mock.patch.object(webpages, "health_advice_node", advice):
            self.srv.cache.clear()
            body = self.page("/?view=health")
        self.assertIn('<div class="advice"><p class="advice-head">ADVICE (AI) check before acting</p><p>restart &lt;b&gt;x&lt;/b&gt;</p>', body)
        self.assertLess(body.index("Slower boot"), body.index('class="advice"'))          # after the findings ...
        self.assertLess(body.index('class="advice"'), body.index("TOP CPU"))               # ... before the sections
        self.assertEqual(seen, [7])


if __name__ == "__main__":
    unittest.main()
