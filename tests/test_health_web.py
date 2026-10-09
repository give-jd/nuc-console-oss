"""The web HEALTH page (web.py ?view=health): the link, the findings as links that carry the view, period and sel validation, the report
computed once a minute per period, escaping, the Windows and macOS demos, the advisor hook, no JavaScript.

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
import time
import unittest
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import demo  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import web  # noqa: E402
import webpages  # noqa: E402
import weburl  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from webtest import classic_default  # noqa: E402

ESC, BEL = chr(27), chr(7)
EVIL = '<script>alert(1)</script>"onmouseover=alert(1) \'x ' + ESC + "[2J" + ESC + "]0;pwn" + BEL
ROW = re.compile(r'<div class="ro( sel)?" id="f-(\d+)">(.*?)</div>')


def serve():
    cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
    srv = web.Server(("127.0.0.1", 0), cfg, "", demo=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@classic_default()
def get(srv, path="/"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, dict(r.getheaders()), body


def rows_of(page):
    """[(index, selected, href of the title link, title, text)] of the findings rows of a health page."""
    out = []
    for sel, i, inner in ROW.findall(page):
        m = re.search(r'<a class="lb [^"]*" href="([^"]*)">([^<]*)</a>\s*<span class="d">([^<]*)</span>', inner)
        out.append((int(i), bool(sel), html.unescape(m.group(1)), html.unescape(m.group(2)), html.unescape(m.group(3))))
    return out


def link(page, text):
    m = re.search(r'<a href="([^"]*)">' + re.escape(text) + "</a>", page)
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
        cls.saved = (dict(render.CFG["features"]), render.DEMO_OS, render.DEMO_HEALTH, render.health_build, webpages.health_extra_html)

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
        features, render.DEMO_OS, render.DEMO_HEALTH, render.health_build, webpages.health_extra_html = self.saved
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
        self.assertEqual(link(dash, "health"), "/?view=health")
        self.assertEqual(link(dash, "map"), "/?view=map")                    # next to it
        self.assertLess(dash.index(">map</a>"), dash.index(">health</a>"))
        self.assertEqual(link(self.page("/?cols=100&zoom=125"), "health"), "/?view=health&cols=100&zoom=125")   # the size is kept
        render.CFG["features"]["health"] = False
        self.srv.cache.clear()
        off = self.page("/")
        self.assertIsNone(link(off, "health"))
        self.assertNotIn("view=health", off)
        self.assertEqual(self.calls, [])                                     # the dashboard never reads the history

    def test_feature_off_the_page_says_so_and_reads_nothing(self):
        render.CFG["features"]["health"] = False
        st, h, off = get(self.srv, "/?view=health&sel=cpu-hog%3Achrome")
        self.assertEqual(st, 200)
        self.assertIn("health disabled in config.ini", off)
        self.assertNotIn('class="ro', off)
        self.assertNotIn("http-equiv", off)                                  # nothing to reload
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(link(off, "dashboard"), "/?")
        self.assertEqual(self.calls, [])

    # ---- the page ---------------------------------------------------------------------------------------------------------------------

    def test_page_has_header_title_findings_sections_and_is_locked_down(self):
        st, h, body = get(self.srv, "/?view=health")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual((h["Cache-Control"], h["X-Content-Type-Options"]), ("no-store", "nosniff"))
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("<form", body.lower())
        self.assertIsNone(re.search(r"\son[a-z]+=", body))                   # no inline event handler
        self.assertIn("demo-host │ HEALTH", body)
        self.assertIn('class="hd w bR B"', body)                             # the demo has problems: the red banner
        self.assertIn('<meta http-equiv="refresh" content="2">', body)
        self.assertIn("<title>demo-host · health · nuc-console</title>", body)
        txt = plain(body)
        self.assertIn("HEALTH  last 7 days", txt)
        self.assertIn("168 h of data", txt)
        self.assertIn("✖ 1 err", txt)
        rs = rows_of(body)
        self.assertEqual(len(rs), 10)
        self.assertEqual([r[3] for r in rs][:3], ["Out of memory: shop-worker-1", "Restarting: shop-worker-1", "Disk filling up: /data"])
        self.assertEqual(len({r[0] for r in rs}), len(rs))                   # one anchor per finding
        for i, sel, href, title, text in rs:                                 # the browser stays on the clicked row; the id is in the URL
            self.assertTrue(href.endswith("#f-%d" % i), href)
            self.assertIn(params(href)["sel"], {f["id"] for f in screens.health_findings(render.health_data(7)["report"])})
            self.assertGreater(len(text), 20)
        for pill in ('<span class="pl r">✖ ERR</span>', '<span class="pl y">! WARN</span>', '<span class="pl d">· INFO</span>'):
            self.assertIn(pill, body)                                        # a symbol besides the colour
        for section in ("TOP CPU", "TOP MEMORY", "EVENTS", "NOISY / NEW LOGS", "DISKS", "THERMAL", "BOOTS"):
            self.assertIn("── " + section + " ", txt)
        self.assertNotIn("DETAILS", txt)                                     # nothing selected: no panel
        self.assertEqual(self.calls, [7])

    def test_footer_links_keep_the_view(self):
        body = self.page("/?view=health&period=30&sel=disk-full%3A%2Fdata&zoom=125&refresh=5&cols=100")
        self.assertEqual(link(body, "dashboard"), "/?cols=100&zoom=125&refresh=5")
        self.assertIsNone(link(body, "30d"))                                 # the period in use is not a link
        self.assertIn("<b>30d</b>", body)
        p = params(link(body, "24h"))
        self.assertEqual((p["view"], p["period"], p["sel"], p["zoom"], p["refresh"], p["cols"]), ("health", 1, "disk-full:/data", 125, 5, 100))
        self.assertEqual(params(link(body, "7d"))["period"], 0)              # 0 = the default
        self.assertNotIn("period=7", link(body, "7d"))
        self.assertTrue(link(body, "24h").endswith("#f-2"))                  # the selected finding stays in sight
        self.assertEqual(params(link(body, "compact"))["cols"], 100)
        self.assertEqual(params(link(body, "wide"))["cols"], 200)
        self.assertEqual(params(link(body, "pause"))["pause"], True)
        self.assertIn("text", body)
        self.assertEqual(params(link(body, "A+"))["zoom"], 150)
        self.assertEqual(params(link(body, "+"))["refresh"], 6)
        self.assertIn("read-only", body)

    def test_default_width_is_a_laptops_and_wide_gets_three_columns(self):
        def side_by_side(page):  # the most sections on one line of the tables
            pre = re.findall(r'<pre class="ht">(.*?)</pre>', page, re.S)[-1]
            return max(len(re.findall(r"── [A-Z]", x)) for x in plain(pre).split("\n"))
        self.assertEqual(side_by_side(self.page("/?view=health")), 2)
        self.assertEqual(side_by_side(self.page("/?view=health&cols=200")), 3)
        self.assertEqual(side_by_side(self.page("/?view=health&cols=100")), 1)

    def test_selecting_a_finding_shows_its_details_beside_or_under_the_list(self):
        body = self.page("/?view=health")
        first = rows_of(body)[3]                                             # mem-leak:node
        href = first[2].split("#")[0]
        sel = self.page(href)
        rs = rows_of(sel)
        self.assertEqual([r[1] for r in rs].count(True), 1)
        self.assertTrue(rs[3][1])
        self.assertIn('<aside class="dp" id="details">', sel)
        self.assertIn('<main class="mp two">', sel)
        panel = sel[sel.index('<aside class="dp"'):sel.index("</aside>")]
        txt = plain(panel)
        self.assertIn("Memory keeps growing: node", txt)
        self.assertIn("a leak is possible", txt)
        self.assertIn("slope_mb_day", txt)
        self.assertIn("180.2", txt)
        self.assertIn("fix", txt)
        self.assertIn("docker update --memory 1g", txt)
        close = re.search(r'<a href="([^"]*)">close ✕</a>', panel)
        self.assertEqual(params(html.unescape(close.group(1)))["sel"], "")
        self.assertTrue(html.unescape(close.group(1)).endswith("#f-3"))
        self.assertIn(">details ↓</a>", sel)                                 # a narrow window gets a link down to it
        again = [r for r in rs if r[1]][0][2]                                # the selected title link toggles it off
        self.assertEqual(params(again)["sel"], "")
        self.assertEqual(self.page(again.split("#")[0]).count("<aside"), 0)
        self.assertIn("slope_mb_day", plain(self.page("/?view=health&sel=mem-leak%3Anode&cols=100")))
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
                        self.assertIn('<aside class="dp"', body)
                        self.assertEqual(len([r for r in rows_of(body) if r[1]]), 1)

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
        self.assertNotIn("✔", self.page("/?view=health"))

    def test_history_unreadable_is_a_message(self):
        render._HEALTH[7] = {"report": None, "msg": "the history could not be read: DatabaseError('x')", "err": True, "at": time.time()}
        body = self.page("/?view=health")
        self.assertIn("the history could not be read: DatabaseError(&#x27;x&#x27;)", body)
        self.assertIn('<p class="hn r">', body)
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
            self.assertNotIn("<script", body.lower())
            self.assertEqual(body.count("<aside"), 0, path)                  # no such finding: no panel, the default period
            self.assertIn("last 7 days", plain(body))

    def test_a_selection_that_is_not_in_the_report_is_dropped(self):
        for sel in ("nope", "cpu-hog:", "cpu-hog:chrome ", "CPU-HOG:chrome", "oom:shop-worker-1\n", "cpu-hog:chrome\x00", "../etc/passwd", "x" * 300,
                    "<script>alert(1)</script>"):
            body = self.page("/?view=health&sel=" + quote(sel))
            self.assertEqual(body.count("<aside"), 0, repr(sel))
            self.assertEqual([r for r in rows_of(body) if r[1]], [], repr(sel))
            self.assertNotIn("alert(1)", body)                               # and never repeated in a link
            self.assertNotIn("../etc", body)
            self.assertNotIn("x" * 50, body)
        good = self.page("/?view=health&sel=cpu-hog%3Achrome")
        self.assertEqual(good.count("<aside"), 1)
        self.assertEqual(self.page("/?view=health&period=1&sel=mem-leak%3Anode").count("<aside"), 0)   # not in the 24 hour report

    def test_a_selection_of_another_period_follows_the_period(self):
        body = self.page("/?view=health&period=30&sel=mem-leak%3Anode")
        self.assertEqual(body.count("<aside"), 1)
        self.assertIn("last 30 days", plain(body))
        self.assertEqual(self.calls, [30])

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
        self.assertNotIn("http-equiv", body)
        self.assertIn("paused", body)
        self.assertEqual(params(link(body, "live"))["pause"], False)
        self.assertNotIn("pause=", link(body, "live"))

    # ---- the report cache -------------------------------------------------------------------------------------------------------------

    def test_the_report_is_computed_once_a_minute_per_period_whatever_the_requests(self):
        for i in range(30):
            self.page("/?view=health")
            self.page("/?view=health&sel=%s" % ("cpu-hog%3Achrome", "oom%3Ashop-worker-1", "nope")[i % 3])
            self.page("/?view=health&zoom=%d&refresh=%d&cols=%d" % (50 + 25 * (i % 3), 1 + i % 10, 100 + 20 * (i % 4)))
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
        threads = [threading.Thread(target=lambda: out.append(get(self.srv, "/?view=health&cols=%d" % (100 + 20 * i))[0])) for i in range(6)]
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
        for path in ("/?view=health", "/?view=health&cols=100", "/?view=health&cols=200", "/?view=health&sel=" + quote(fid, safe="")):
            body = self.page(path)
            self.assertEqual(re.findall(r"<script|<img|<iframe|<svg|<form|<object|<embed", body, re.I), [], path)
            self.assertIsNone(re.search(r"<[^>]*\son[a-z]+=", body), path)                 # no event handler inside any tag
            self.assertNotIn(ESC, body)
            self.assertNotIn(BEL, body)
            self.assertNotIn("<script>alert(1)</script>", body)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)     # shown, as text
        body = self.page("/?view=health&sel=" + quote(fid, safe=""))
        self.assertIn("?[2J?]0;pwn?", plain(body))                           # the escape sequence is visible text, not an escape
        self.assertEqual(body.count("<aside"), 1)
        self.assertEqual(len([r for r in rows_of(body) if r[1]]), 1)
        for href in re.findall(r'href="([^"]*)"', body):                     # every link is one of ours, the id percent-encoded
            self.assertTrue(href.startswith("/?") or href.startswith("#"), href)
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
        self.assertIn("render error (see the service log)", body)
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
        self.assertEqual(webpages.health_extra_html(demo.health_report(None, 7)), "")
        self.assertNotIn("ADVICE", self.page("/?view=health"))
        seen = []

        def advice(rep):
            seen.append(rep["period"]["days"])
            return '<div class="nt" id="advice">AI, check before acting: <b>restart</b></div>'
        webpages.health_extra_html = advice
        self.srv.cache.clear()
        body = self.page("/?view=health")
        self.assertIn('id="advice"', body)
        self.assertLess(body.index("Slower boot"), body.index('id="advice"'))             # after the findings ...
        self.assertLess(body.index('id="advice"'), body.index("TOP CPU"))                  # ... before the sections
        self.assertEqual(seen, [7])


if __name__ == "__main__":
    unittest.main()
