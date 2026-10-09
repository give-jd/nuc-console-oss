"""The web CPU page (web.py ?view=cpu): the console screen through the ANSI path, process rows that are links, sort and details in the
URL, one sampler for the whole server read once per refresh interval, bounded cache, escaping, no JavaScript."""
import contextlib
import html
import http.client
import io
import os
import re
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import demo  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
import weburl  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from webtest import classic_default  # noqa: E402

ESC, BEL, CSI8 = chr(27), chr(7), chr(0x9b)
EVIL = '<script>alert(1)</script>"onmouseover=alert(1) \'x' + ESC + "[2J" + BEL + CSI8 + "31m"
ROW = re.compile(r'<a class="pr" title="details" href="([^"]*)">(.*?)</a>')
SIZES = ((100, 40), (200, 60), (300, 120), (60, 20))


def serve(**cfg):
    conf = dict(nuc_config.load()["web"], refresh_seconds=2, **cfg)
    srv = web.Server(("127.0.0.1", 0), conf, "", demo=True)
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


def text_of(page):
    """The page's <pre> as plain text lines (tags gone, entities decoded)."""
    pre = page[page.index("<pre>") + 5:page.index("</pre>")]
    return html.unescape(re.sub(r"<[^>]+>", "", pre)).split("\n")


def rows_of(page):
    """[(href, pid, selected, text)] of the process rows of a page."""
    out = []
    for href, inner in ROW.findall(page):
        text = html.unescape(re.sub(r"<[^>]+>", "", inner))
        out.append((html.unescape(href), int(text.split()[0]), any("rv" in x.split() for x in re.findall(r'class="([^"]*)"', inner)), text))
    return out


def link(page, text):
    m = re.search(r'<a href="([^"]*)">' + re.escape(text) + "</a>", page)
    return html.unescape(m.group(1)) if m else None


def params(url):
    return weburl.view_params(web.parse_qs(web.urlsplit(url).query))


class CpuPageCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.webapps, cls.hostname = render.CFG["webapps"], render.socket.gethostname  # the demo declares [webapps] and renames the host
        cls.expose = render.CFG["expose"]  # and [expose]
        cls.srv = serve()
        cls.features = dict(render.CFG["features"])
        cls.cfg = (render.CFG["cpu_in_rotation"], render.CFG["map_in_rotation"])

    @classmethod
    def tearDownClass(cls):
        render.DEMO, render.DEMO_OS = False, None
        render.CFG["webapps"], render.socket.gethostname = cls.webapps, cls.hostname
        render.CFG["expose"] = cls.expose
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.srv.cache.clear()
        self.srv.cpu_feed.data = None
        render.DEMO, render.DEMO_OS = True, None
        render.CFG["features"].update(self.features)
        render.CFG["cpu_in_rotation"], render.CFG["map_in_rotation"] = self.cfg
        self.saved_demo = {k: getattr(demo, k) for k in ("cpu_sample", "proc_sample", "sensors")}

    def tearDown(self):
        for k, v in self.saved_demo.items():
            setattr(demo, k, v)
        render.DEMO_OS = None
        render.CFG["features"].update(self.features)
        render.CFG["cpu_in_rotation"], render.CFG["map_in_rotation"] = self.cfg
        self.srv.cpu_feed.data = None

    def page(self, path):
        st, h, body = get(self.srv, path)
        self.assertEqual(st, 200, path)
        self.assertNotIn("render error", body, path)
        return body


class Page(CpuPageCase):
    def test_a_cpu_link_sits_next_to_map_in_the_bottom_bar_when_the_feature_is_on(self):
        body = self.page("/")
        self.assertIn('">map</a> · <a href="/?view=cpu">cpu</a> · <a href="/?view=health">health</a> · <a href="/?view=ai">ai</a> · read-only', body)
        wide = self.page("/?cols=100&zoom=150")
        self.assertIn('<a href="/?view=cpu&amp;cols=100&amp;zoom=150">cpu</a>', wide)            # the other links' size and layout are kept
        render.CFG["features"]["cpu"] = False
        self.srv.cache.clear()
        self.assertNotIn(">cpu</a>", self.page("/"))
        self.assertIn(">map</a>", self.page("/"))
        render.CFG["features"].update(cpu=True, map=False)
        self.srv.cache.clear()
        body = self.page("/")
        self.assertIn(">cpu</a>", body)
        self.assertNotIn(">map</a>", body)

    def test_the_page_has_the_screen_the_rows_as_links_and_is_locked_down(self):
        st, h, body = get(self.srv, "/?view=cpu")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual((h["Cache-Control"], h["X-Content-Type-Options"]), ("no-store", "nosniff"))
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("<form", body.lower())
        self.assertNotIn("<input", body.lower())
        self.assertIsNone(re.search(r"\son[a-z]+=", body))                                       # no inline event handler
        self.assertIn('<meta http-equiv="refresh" content="2">', body)
        self.assertIn("<title>demo-host · cpu · nuc-console</title>", body)
        txt = "\n".join(text_of(body))
        for want in ("demo-host │ CPU │", "── CPU ", "AMD Ryzen 7 5800X 8-Core Processor", "ALL ", "── TEMPERATURES", "source k10temp", "── PROCESSES",
                     "PID USER", "NAME", "ffmpeg", "by CPU%", "processes listed"):
            self.assertIn(want, txt)
        self.assertIn('class="w bR B"', body)                                                     # the demo has problems: the red banner
        rows = rows_of(body)
        self.assertGreater(len(rows), 20)
        self.assertEqual(len({r[1] for r in rows}), len(rows))
        shown = [x for x in text_of(body) if re.match(r"\s+\d+ \S", x) and "PID" not in x]
        self.assertEqual([r[3] for r in rows], [x for x in shown if x.split()[0] in {str(r[1]) for r in rows}])
        for href, pid, selected, text in rows:
            q = params(href)
            self.assertEqual((q["view"], q["sel"], q["sort"]), ("cpu", str(pid), ""), href)      # a row's link: its details, the view kept
            self.assertFalse(selected)
        self.assertEqual(rows[0][1], 2210)                                                        # sorted by CPU%: ffmpeg first

    def test_footer_links_sort_text_size_refresh_dashboard_and_map(self):
        body = self.page("/?view=cpu&zoom=150&refresh=5&sort=mem&sel=2350")
        foot = body[body.index("<footer>"):]
        self.assertIn("sort <a href=", foot)
        self.assertIn("<b>mem</b>", foot)
        for name in ("cpu", "time", "pid", "user"):
            href = link(foot, name)
            self.assertTrue(href, name)
            q = params(href)
            self.assertEqual((q["view"], q["sort"] if name != "cpu" else "", q["sel"], q["zoom"], q["refresh"]), ("cpu", name if name != "cpu" else "", "2350", 150, 5))
        self.assertEqual(link(foot, "dashboard"), "/?zoom=150&refresh=5")
        self.assertEqual(params(link(foot, "map"))["view"], "map")
        self.assertEqual(params(link(foot, "close details"))["sel"], "")
        self.assertEqual(params(link(foot, "close details"))["sort"], "mem")                       # closing keeps the sort
        self.assertEqual(params(link(foot, "A+"))["sel"], "2350")
        self.assertIn(" 150% ", foot)
        self.assertIn("read-only", foot)
        self.assertEqual(params(link(foot, "−"))["refresh"], 4)
        render.CFG["features"]["map"] = False
        self.srv.cache.clear()
        self.assertIsNone(link(self.page("/?view=cpu"), "map"))

    def test_the_page_is_the_screen_at_the_asked_size_and_every_line_fits(self):
        for cols, rows in SIZES:
            body = self.page(f"/?view=cpu&cols={cols}&rows={rows}&sel=2210")
            lines = text_of(body)
            self.assertLessEqual(max(len(x) for x in lines), cols, (cols, rows))
            self.assertEqual(len(lines), rows, (cols, rows))
            self.assertIn("demo-host │ CPU │", lines[0])
            self.assertIn("── PROCESS 2210", "\n".join(lines))

    def test_fit_without_rows_scrolls_and_shows_every_process(self):
        body = self.page("/?view=cpu&fit=1&cols=140&zoom=100")
        self.assertIn("calc(98vw /", body)
        lines = text_of(body)
        self.assertEqual(len(rows_of(body)), 40)                                                  # all of them: the page is as tall as its content
        self.assertNotIn("more processes", "\n".join(lines))
        self.assertLessEqual(max(len(x) for x in lines), 140)
        self.assertNotIn("\n\n\n", "\n".join(lines))                                              # no blank filler
        short = self.page("/?view=cpu&fit=1&rows=32&cols=140")                                    # with rows: one screen, cut and counted
        self.assertIn("calc(93vh /", short)
        self.assertEqual(len(text_of(short)), 32)
        self.assertIn("more processes", "\n".join(text_of(short)))
        big = self.page("/?view=cpu&fit=1&cols=200&zoom=200")                                       # a bigger text = fewer columns, re-laid out
        self.assertLessEqual(max(len(x) for x in text_of(big)), 100)

    def test_windows_and_macos_demos_render(self):
        for name, wants in (("windows", ("AMD Ryzen 7 5800X", "source LibreHardwareMonitor", "hottest core 3 90°C")),
                            ("darwin", ("Apple M2", "thermal pressure Moderate", "E-Cluster 2064 MHz", "source smctemp"))):
            render.DEMO_OS = name
            self.srv.cache.clear()                                                                # the page cache is keyed by the URL, not by the demo's OS
            body = self.page("/?view=cpu&cols=200&rows=60")
            txt = "\n".join(text_of(body))
            for want in wants:
                self.assertIn(want, txt, name)
            self.assertGreater(len(rows_of(body)), 20, name)
        render.DEMO_OS = "windows"
        self.srv.cache.clear()
        txt = "\n".join(text_of(self.page("/?view=cpu&cols=200&rows=60&sel=3990")))
        self.assertIn("── PROCESS 3990", txt)
        self.assertIn("state     ?", txt)                                                            # Windows has no process state: '?'
        render.DEMO_OS = None
        self.srv.cache.clear()
        self.assertIn("Ryzen 7 5800X 8-Core", "\n".join(text_of(self.page("/?view=cpu"))))                      # and back: the feed follows the demo OS

    def test_a_render_error_is_logged_not_shown(self):
        saved = render.cpu_problems

        def broken(smp=None):
            raise ValueError("secret detail /etc/x")
        render.cpu_problems = broken
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                st, _, body = get(self.srv, "/?view=cpu&sel=2210")
        finally:
            render.cpu_problems = saved
        self.assertEqual(st, 200)
        self.assertIn("render error (see the service log)", body)
        self.assertNotIn("secret detail", body)
        self.assertNotIn("Traceback", body)
        self.assertIn("secret detail", err.getvalue())
        self.assertIn(">dashboard</a>", body)                                                       # the way out is still there

    def test_the_feature_off_says_so(self):
        render.CFG["features"]["cpu"] = False
        st, _, body = get(self.srv, "/?view=cpu")
        self.assertEqual(st, 200)
        self.assertIn("CPU screen disabled in config.ini", body)
        self.assertIn("[features] cpu = no", body)
        self.assertNotIn("http-equiv", body)                                                        # nothing to refresh
        self.assertNotIn("PROCESSES", body)
        self.assertNotIn("ffmpeg", body)

    def test_the_cpu_slide_joins_the_full_and_rotating_dashboard_views_when_asked(self):
        self.assertNotIn("PROCESSES", self.page("/?full=1"))                                        # off by default: the full view has no CPU page
        render.CFG["cpu_in_rotation"] = True
        self.srv.cache.clear()
        full = self.page("/?full=1")
        self.assertIn("PROCESSES", html.unescape(re.sub(r"<[^>]+>", "", full)))
        self.assertIn("│ CPU │", html.unescape(re.sub(r"<[^>]+>", "", full)))
        self.assertNotIn("PROCESSES", html.unescape(re.sub(r"<[^>]+>", "", self.page("/"))))       # the plain page is the overview only
        render.CFG["cpu_in_rotation"] = False


class Parameters(CpuPageCase):
    def test_sort_is_one_of_the_five_names_else_dropped(self):
        q = lambda s: weburl.view_params(web.parse_qs(s))  # noqa: E731
        for name, want in (("mem", "mem"), ("time", "time"), ("pid", "pid"), ("user", "user"), ("cpu", ""), ("", ""), ("MEM", ""), ("memory", ""),
                           ("mem ", ""), ("<script>", ""), ("%00", ""), ("p", "")):
            self.assertEqual(q("view=cpu&sort=" + name)["sort"], want, repr(name))
        self.assertEqual(q("sort=mem")["sort"], "")                                                  # only the CPU view has a sort
        self.assertEqual(q("view=map&sort=mem")["sort"], "")
        self.assertEqual(q("view=CPU")["view"], "")
        self.assertEqual((q("view=cpu")["view"], q("view=map")["view"], q("view=other")["view"]), ("cpu", "map", ""))
        self.assertEqual(q("view=cpu&sort=mem&sort=pid")["sort"], "mem")                             # the first one counts

    def test_sel_is_a_pid_digits_only_in_range_normalised(self):
        q = lambda s: weburl.view_params(web.parse_qs(s))["sel"]  # noqa: E731
        for raw, want in (("2210", "2210"), ("0", "0"), ("007", "7"), ("4294967295", "4294967295"), ("4294967296", ""), ("12345678901", ""),
                          ("", ""), ("-1", ""), ("+5", ""), ("1.5", ""), ("1e3", ""), (" 5", ""), ("5 ", ""), ("0x10", ""), ("%C2%B2", ""),
                          ("%D9%A3", ""), ("<script>", ""), ("2210%00", ""), ("a" * 5000, ""), ("9" * 5000, "")):
            self.assertEqual(q("view=cpu&sel=" + raw), want, repr(raw))
        self.assertEqual(q("sel=2210"), "")                                                          # only the CPU view has pids
        self.assertEqual(q("view=map&sel=2210"), "")                                                 # a map row is 10 hex digits: not a pid
        key = "0123456789"
        self.assertEqual(q("view=map&sel=" + key), key)                                              # (10 digits: a row key, as before)
        self.assertEqual(q("view=cpu&sel=1&sel=2"), "1")

    def test_other_parameters_of_the_dashboard_still_apply(self):
        q = lambda s: weburl.view_params(web.parse_qs(s))  # noqa: E731
        got = q("view=cpu&cols=133&rows=7&zoom=133&fit=1&refresh=99&kiosk=1")
        self.assertEqual((got["cols"], got["rows"], got["zoom"], got["fit"], got["refresh"], got["kiosk"]), (140, 20, 125, True, 10, True))

    def test_hostile_values_never_reach_the_page(self):
        for path in ("/?view=cpu&sel=%3Cscript%3E", "/?view=cpu&sort=%22%3E%3Cscript%3E", "/?view=cpu&sel=" + "9" * 5000, "/?view=cpu&sel=%C2%B2",
                     "/?view=cpu&zoom=99999999999999999999&refresh=-1&cols=x&sel=%3Cb%3E", "/?view=cpu&sort=&sel=", "/?view=cpu&sel=%00&sort=%00"):
            body = self.page(path)
            self.assertNotIn("<script", body.lower(), path)
            self.assertNotIn("PROCESS 9", body, path)
            self.assertNotIn("── PROCESS ", "\n".join(text_of(body)).replace("PROCESSES", ""), path)

    def test_a_pid_that_does_not_exist_is_dropped_everywhere(self):
        body = self.page("/?view=cpu&sel=99999")
        self.assertNotIn("PROCESS 99999", body)
        self.assertNotIn("close details", body)
        self.assertNotIn("sel=99999", html.unescape(body))                                           # not repeated in any link
        self.assertEqual(self.page("/?view=cpu&sel=0") == "", False)
        self.assertNotIn("close details", self.page("/?view=cpu&sel=0"))                              # pid 0 is not in the Linux demo
        render.DEMO_OS = "darwin"
        self.srv.cache.clear()
        self.assertIn("close details", self.page("/?view=cpu&sel=0"))                                 # but it is kernel_task on the macOS one
        self.assertIn("── PROCESS 0 ", "\n".join(text_of(self.page("/?view=cpu&sel=0"))))

    def test_sel_shows_the_details_and_the_selected_row_is_highlighted_and_closes_on_click(self):
        body = self.page("/?view=cpu&sel=2210&cols=200&rows=60")
        txt = "\n".join(text_of(body))
        self.assertIn("── PROCESS 2210", txt)
        for want in ("parent    1  systemd", "user      alice", "state     R  running", "threads   18", "CPU time  2:32:00", "started   "):
            self.assertIn(want, txt)
        rows = rows_of(body)
        selected = [r for r in rows if r[2]]
        self.assertEqual([r[1] for r in selected], [2210])                                           # the reverse-video row
        self.assertEqual(params(selected[0][0])["sel"], "")                                          # clicking it again closes the pane
        other = next(r for r in rows if r[1] != 2210)
        self.assertEqual(params(other[0])["sel"], str(other[1]))
        self.assertIn("details of the highlighted row", txt)
        self.assertIn(">close details</a>", body)
        narrow = "\n".join(text_of(self.page("/?view=cpu&sel=2210&cols=100&rows=40")))
        self.assertIn("── PROCESS 2210", narrow)                                                     # below the table, not beside it
        self.assertNotIn(" │ ── PROCESS", narrow)
        self.assertIn(" │ ── PROCESS 2210", txt)


class CacheAndSampling(CpuPageCase):
    def pg(self, query):
        return self.srv.page(**weburl.view_params(web.parse_qs(query)))

    @classic_default()
    def test_the_cache_key_has_the_view_the_sort_and_the_selection(self):
        a = self.pg("view=cpu")
        self.assertIs(self.pg("view=cpu"), a)                                                       # within r/2: the same render
        self.assertIs(self.pg("view=cpu&sort=cpu"), a)                                              # equal views, one entry
        self.assertIs(self.pg("view=cpu&sel=007") is self.pg("view=cpu&sel=7"), True)
        b, c, d = self.pg("view=cpu&sort=mem"), self.pg("view=cpu&sel=2210"), self.pg("view=cpu&sort=mem&sel=2210")
        self.assertEqual(len({id(x) for x in (a, b, c, d)}), 4)
        self.assertEqual(len([k for k in self.srv.cache if k[0] == "cpu"]), 5)
        keys = [k for k in self.srv.cache if k[0] == "cpu"]
        self.assertTrue(any("mem" in k and "2210" in k for k in keys))
        self.assertNotIn(a, [self.srv.cache[k][1] for k in self.srv.cache if k[0] != "cpu"])
        self.assertNotEqual(self.pg("view=cpu&cols=100&rows=40"), a)                                # the size is part of it too
        self.assertNotEqual(self.pg("view=cpu&zoom=150"), a)
        self.assertNotEqual(self.pg("view=cpu&refresh=5") is a, True)

    def test_the_cache_stays_bounded_whatever_the_urls(self):
        for i in range(web.CACHE_MAX * 3):
            self.pg(f"view=cpu&sel={i}&sort={('mem', 'time', 'pid', 'user', '')[i % 5]}")
        self.assertLessEqual(len(self.srv.cache), web.CACHE_MAX)
        one = len(self.pg("view=cpu&sel=2210"))
        orig, web.CACHE_CHARS = web.CACHE_CHARS, one * 3                                            # room for three pages
        try:
            for i in range(10):
                self.pg(f"view=cpu&sel={2200 + i}&sort=pid")
            self.assertLessEqual(self.srv.cache_chars(), web.CACHE_CHARS)
        finally:
            web.CACHE_CHARS = orig

    def producers(self, clock):
        """Fake cpuinfo/procs modules, not the demo: they count what is made and sampled. The clock is ours."""
        log = dict(cpu_made=0, cpu_sampled=0, proc_made=0, proc_sampled=0, slept=[])

        class CpuSampler(object):
            def __init__(self):
                log["cpu_made"] += 1

            def sample(self):
                log["cpu_sampled"] += 1
                return demo.cpu_sample(None, clock[0])

        class ProcSampler(object):
            def __init__(self):
                log["proc_made"] += 1

            def sample(self):
                log["proc_sampled"] += 1
                return demo.proc_sample(None, clock[0])
        fake = types.SimpleNamespace(time=lambda: clock[0], sleep=lambda sec: log["slept"].append(sec), strftime=time.strftime, localtime=time.localtime,
                                     gmtime=time.gmtime, monotonic=time.monotonic)
        saved = (render.time, web.time, render.cpuinfo, render.procs, render.DEMO)
        render.time = web.time = fake
        render.cpuinfo, render.procs, render.DEMO = types.SimpleNamespace(CpuSampler=CpuSampler), types.SimpleNamespace(ProcSampler=ProcSampler), False
        self.addCleanup(lambda: (setattr(render, "time", saved[0]), setattr(web, "time", saved[1]), setattr(render, "cpuinfo", saved[2]),
                                 setattr(render, "procs", saved[3]), setattr(render, "DEMO", saved[4])))
        return log

    def test_one_sampler_for_the_server_read_once_per_refresh_interval(self):
        clock = [1_790_000_000.0]
        srv = web.Server(("127.0.0.1", 0), dict(nuc_config.load()["web"], refresh_seconds=2), "", demo=False)
        self.addCleanup(srv.server_close)
        log = self.producers(clock)
        render.DEMO = False
        for q in ("", "full=1", "cols=100", "view=map"):                                               # nobody looks at the CPU: nothing is made or read
            try:
                srv.page(**weburl.view_params(web.parse_qs(q)))
            except Exception:  # noqa: BLE001 - this server has no containers/net state: not what is tested here
                pass
        self.assertEqual((log["cpu_made"], log["proc_made"], log["proc_sampled"]), (0, 0, 0))
        views = ["view=cpu", "view=cpu&sort=mem", "view=cpu&sort=time&sel=2210", "view=cpu&sel=1", "view=cpu&cols=100&rows=40", "view=cpu&zoom=150",
                 "view=cpu&sort=pid", "view=cpu&sort=user&sel=2350"]
        pages = [srv.page(**weburl.view_params(web.parse_qs(v))) for v in views]
        self.assertTrue(all("PROCESSES" in x for x in pages))
        self.assertEqual((log["cpu_made"], log["proc_made"]), (1, 1))                                 # one pair for the whole server
        self.assertEqual((log["cpu_sampled"], log["proc_sampled"]), (1, 2))                           # the 2nd process reading: the first had no CPU%
        self.assertEqual([x for x in log["slept"] if x == 0.4], [0.4])                              # (the sampler's own threads sleep too: other values)
        self.assertNotIn("measuring", "".join(pages))                                                # so the first page already has a CPU%
        clock[0] += 1.9                                                                              # within the interval: every other view, no new reading
        for v in views + ["view=cpu&sort=mem&sel=2600", "view=cpu&sel=3301"]:
            srv.page(**weburl.view_params(web.parse_qs(v)))
        self.assertEqual((log["cpu_sampled"], log["proc_sampled"]), (1, 2))
        clock[0] += 1.2                                                                              # the interval is over (and the cached pages are old): one reading, whoever asks first
        for v in views:
            srv.page(**weburl.view_params(web.parse_qs(v)))
        self.assertEqual((log["cpu_made"], log["proc_made"], log["cpu_sampled"], log["proc_sampled"]), (1, 1, 2, 3))
        clock[0] += 8                                                                                # a slower refresh asks for fewer readings
        slow = [srv.page(**weburl.view_params(web.parse_qs("view=cpu&refresh=7&sel=%d" % i))) for i in (1, 2, 3)]
        self.assertEqual(log["proc_sampled"], 4)
        clock[0] += 5
        srv.page(**weburl.view_params(web.parse_qs("view=cpu&refresh=7")))
        self.assertEqual(log["proc_sampled"], 4)
        self.assertEqual(len(slow), 3)

    def test_many_viewers_at_once_still_one_reading(self):
        clock = [1_790_000_000.0]
        srv = web.Server(("127.0.0.1", 0), dict(nuc_config.load()["web"], refresh_seconds=2), "", demo=False)
        self.addCleanup(srv.server_close)
        log = self.producers(clock)
        out, errors = [], []

        def viewer(i):
            try:
                out.append(srv.page(**weburl.view_params(web.parse_qs("view=cpu&sel=%d&sort=%s" % (1 + i % 5, ("mem", "time", "")[i % 3])))))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=viewer, args=(i,)) for i in range(24)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(out), 24)
        self.assertEqual((log["cpu_made"], log["proc_made"], log["cpu_sampled"], log["proc_sampled"]), (1, 1, 1, 2))


class Hostile(CpuPageCase):
    def poison(self):
        real_proc, real_cpu, real_sens = demo.proc_sample, demo.cpu_sample, demo.sensors

        def procs_(os_name=None, now=None):
            r = real_proc(os_name, now)
            for p in r["procs"][:6]:
                p["name"], p["user"] = EVIL, EVIL
            r["procs"].append({"pid": 31337, "ppid": r["procs"][0]["pid"], "user": EVIL, "name": EVIL, "state": "<b>", "threads": 1, "nice": 0,
                               "prio": 20, "cpu": 99.9, "mem": 100, "mem_pct": 0.1, "time": 5.0, "start": time.time() - 5})
            r["notes"] = ["note " + EVIL]
            return r

        def cpu_(os_name=None, now=None):
            r = real_cpu(os_name, now)
            r["model"], r["arch"] = EVIL, EVIL
            r["freq"].update(governor=EVIL, driver=EVIL)
            r["temps"].update(source=EVIL, sensors=[{"label": EVIL, "c": 50.0, "high": None, "crit": None}])
            return r

        def sens(os_name=None, now=None):
            r = real_sens(os_name, now)
            if r:
                r["cpu"].update(source=EVIL, pressure=EVIL, sensors=[{"label": EVIL, "c": 50.0}],
                                clusters=[{"name": EVIL, "mhz": 1000.0, "active": 5.0, "cpus": {"0": 1.0}}])
                r["errors"] = {EVIL: EVIL}
            return r
        demo.proc_sample, demo.cpu_sample, demo.sensors = procs_, cpu_, sens

    def test_names_users_labels_and_notes_are_escaped_and_control_characters_are_gone(self):
        self.poison()
        for os_name in (None, "windows", "darwin"):
            render.DEMO_OS = os_name
            for query in ("", "&sel=31337", "&sort=user&sel=31337", "&sort=pid&cols=100&rows=40&sel=31337"):
                self.srv.cache.clear()
                self.srv.cpu_feed.data = None
                body = self.page("/?view=cpu" + query)
                for raw in (ESC, BEL, CSI8):
                    self.assertNotIn(raw, body, (os_name, query))
                self.assertNotIn("<script", body.lower(), (os_name, query))
                self.assertNotIn("<b>", re.sub(r"<footer>.*", "", body, flags=re.S).replace("<b class", ""), (os_name, query))   # the payload's <b>: text, not a tag
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;&quot;onmouseover=alert(1) &#x27;x?[2J??31m", body, (os_name, query))
                self.assertIsNone(re.search(r"\son[a-z]+=", body), (os_name, query))
                self.assertIsNone(re.search(r'href="[^"]*(<|>|script)', body), (os_name, query))                                   # no name in a link
        render.DEMO_OS = None
        body = self.page("/?view=cpu&sel=31337&cols=200&rows=60")
        txt = "\n".join(text_of(body))
        self.assertIn("── PROCESS 31337 ", txt)
        self.assertIn("note " + EVIL.replace(ESC, "?").replace(BEL, "?").replace(CSI8, "?"), txt)
        for href, pid, selected, text in rows_of(body):
            self.assertRegex(href, r"^/\?view=cpu(?:&[a-z]+=\w+)*$")                                  # a link holds digits and fixed words, never a name
            self.assertEqual(href.count("sel="), 0 if selected else 1)                              # the open one links back to the table


if __name__ == "__main__":
    unittest.main()
