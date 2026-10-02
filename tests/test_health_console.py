"""The console HEALTH screen (render.py): the keys on the findings, the periods, `--view health` and its options, every frame inside
its screen, the screen among the rotating pages, the feature switch, the report cache, little data, no history and hostile names.

Hermetic: the demo reports (src/demo.py) at a clock that stands still, a fake terminal for the main loop (no TTY), no host state (no
Sampler, no accepted.json, a fixed host name, no history.db), on Linux, macOS and Windows demos alike. Every module global a test changes
is put back in tearDown.
"""
import contextlib
import io
import os
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import demo  # noqa: E402
import health  # noqa: E402
import history  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import ui  # noqa: E402
import screens  # noqa: E402
import ansi  # noqa: E402

NOW = 1_790_000_000
SIZES = ((79, 24), (120, 33), (200, 50), (226, 50))       # console sizes (--cols --rows): the layout gets cols - 1, as in once()
OSES = (None, "windows", "darwin")
PERIODS = (1, 7, 30)
VARIANTS = ("", "little", "none")
ESC, BEL, CSI8 = chr(27), chr(7), chr(0x9b)               # built at runtime: the file itself stays plain text
SGR = re.compile(r"\x1b\[[0-9;]*m")                        # the only escape sequences the renderer puts inside a line
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
LINE = "\x1b[K\r\n"                                        # frame(): every line but the last ends with erase-to-end + CRLF
RENDER_GLOBALS = ("DEMO", "DEMO_OS", "DEMO_HEALTH", "MODE", "WINDOWS", "ACCEPTED_PATH", "time", "os", "sys", "signal", "shutil", "socket",
                  "termios", "tty", "Sampler", "read_keys", "snapshot", "page_overview", "health_build", "health_slide", "health_extra_lines", "health_screen", "health_state", "KPI_MIN_ROWS")


class Proxy(object):
    """A module as render (or demo) sees it, with a few attributes replaced: the real module is never touched."""

    def __init__(self, real, **over):
        self._real = real
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Stop(Exception):
    """Ends render.main()'s endless loop from inside a fake keyboard read."""


class FakeSampler(object):
    """No /proc, no hostinfo: what Sampler.sample() returns on a machine with nothing to say."""

    def sample(self):
        return {"cpu": {}, "thermal": {}, "net": {}, "sessions": None, "fs": None}


def overview_stub(*a, **kw):
    return ["   overview page (stub)"]


def report(os_name=None, days=7, variant="", change=None):
    """The demo report at NOW (what health.report() would return), changed by change(report)."""
    rep = demo.health_report(os_name, days, NOW, variant=variant)
    if change:
        change(rep)
    return rep


def finding(fid, level="warn", title="a title", text="what happened", fix="what to do", facts=None):
    return {"id": fid, "level": level, "title": title, "text": text, "fix": fix, "facts": facts or {"n": 3}, "subject": None}


class HealthCase(unittest.TestCase):
    """The demo report at NOW, the health feature on and out of the rotation, nothing read from the host; all of it undone after."""

    def setUp(self):
        self.saved = {k: getattr(render, k) for k in RENDER_GLOBALS}
        self.saved_time = demo.time
        cfg = render.CFG
        self.saved_cfg = (dict(cfg["features"]), cfg["webapps"], cfg["expose"], cfg["health_in_rotation"], cfg["map_in_rotation"])
        self.saved_cache = dict(render._HEALTH)
        render._HEALTH.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.now = NOW
        self.on_sleep = self.pass_time
        clock = Proxy(time, time=lambda: self.now, sleep=lambda sec: self.on_sleep(sec),
                      strftime=lambda fmt, t=None: time.strftime(fmt, time.gmtime(self.now) if t is None else t))
        render.time = demo.time = clock
        render.socket = Proxy(render.socket, gethostname=lambda: "test-host")  # demo_defaults() renames it on the proxy only
        render.ACCEPTED_PATH = os.path.join(self.tmp.name, "accepted.json")     # missing: nothing accepted, whatever the host has
        render.KPI_MIN_ROWS = 10 ** 6                                           # the KPI line is tested in test_console_ui.py
        render.DEMO, render.DEMO_OS, render.DEMO_HEALTH = True, None, ""
        cfg["features"]["health"], cfg["health_in_rotation"], cfg["map_in_rotation"] = True, False, False
        self.calls = []                                                         # the days of every health_build(): the report cache
        real = render.health_build
        render.health_build = lambda days, now: (self.calls.append(days), real(days, now))[1]

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(render, k, v)
        demo.time = self.saved_time
        features, render.CFG["webapps"], render.CFG["expose"], render.CFG["health_in_rotation"], render.CFG["map_in_rotation"] = self.saved_cfg
        render.CFG["features"].clear()
        render.CFG["features"].update(features)
        render._HEALTH.clear()
        render._HEALTH.update(self.saved_cache)
        self.tmp.cleanup()

    def pass_time(self, sec):
        self.now += sec

    def seed(self, rep, days=None):
        """The report the screen gets for this period: no build, whatever the demo says."""
        days = days or rep["period"]["days"]
        render._HEALTH[days] = {"report": rep, "msg": "", "err": False, "at": self.now}

    def check_frame(self, screen, w, h, what=""):
        """Exactly h lines, none wider than w once the colours are stripped, and no control character or escape sequence but
        the renderer's own colours and line ends. -> the lines, ANSI stripped."""
        rows = screen.split(LINE)
        self.assertEqual(len(rows), h, what)
        out = [ansi.ANSI.sub("", x) for x in rows]
        for x in out:
            self.assertLessEqual(len(x), w, (what, x))
        self.assertIsNone(CONTROL.search(SGR.sub("", "".join(rows))), what)
        return out

    def screen(self, opts, cols, rows):
        """`render.py --once --view health OPTS --cols cols --rows rows`, without printing: (frame, its lines ANSI stripped)."""
        s = render.health_once(["render.py", "--once", "--view", "health"] + list(opts), cols - 1, rows)
        return s, self.check_frame(s, cols - 1, rows, (opts, cols, rows, render.DEMO_OS))

    def view(self, rep=None, **kw):
        """A HealthView and the findings of a report (the demo's by default)."""
        rep = rep or report()
        hv = screens.HealthView(rep["period"]["days"], now=NOW)
        for k, v in kw.items():
            setattr(hv, k, v)
        return hv, screens.health_findings(rep)

    def press(self, hv, fl, keys):
        """Keys as the console loop hands them to health_key. -> what health_key returned for each. After every key the cursor is on one finding."""
        acts = []
        screens.health_sync(hv, fl)                                                       # what health_screen() does before every key
        for k in keys:
            acts.append(screens.health_key(hv, k, fl))
            if fl:
                self.assertTrue(0 <= hv.idx < len(fl), (k, hv.idx, len(fl)))
                self.assertEqual(hv.cur, fl[hv.idx]["id"], k)
        return acts


class Navigation(HealthCase):
    """health_key on the demo findings: what each key does to the cursor, the details pane and the period."""

    def test_up_and_down_move_and_stop_at_the_ends(self):
        hv, fl = self.view()
        n = len(fl)
        self.assertEqual(n, 10)
        self.assertEqual(self.press(hv, fl, ["up", "k"]), ["", ""])                   # the first finding: up stays
        self.assertEqual(hv.idx, 0)
        self.press(hv, fl, ["down", "j", "down"])
        self.assertEqual((hv.idx, hv.cur), (3, "mem-leak:node"))
        self.press(hv, fl, ["down"] * (n + 5))
        self.assertEqual((hv.idx, hv.cur), (n - 1, "boot-regression:host"))           # the last: down stays
        self.press(hv, fl, ["up"])
        self.assertEqual(hv.idx, n - 2)

    def test_page_keys_move_a_page_and_stop_at_the_ends(self):
        hv, fl = self.view(rows=4)
        self.press(hv, fl, ["pgdn"])
        self.assertEqual(hv.idx, 3)                                                    # a page is the rows on screen minus one
        self.press(hv, fl, ["pgdn", "pgdn", "pgdn"])
        self.assertEqual(hv.idx, len(fl) - 1)
        self.press(hv, fl, ["pgup"])
        self.assertEqual(hv.idx, len(fl) - 4)
        self.press(hv, fl, ["home"])
        self.assertEqual(hv.idx, 0)
        self.press(hv, fl, ["end"])
        self.assertEqual(hv.idx, len(fl) - 1)

    def test_enter_and_space_toggle_the_details_pane(self):
        hv, fl = self.view()
        self.assertFalse(hv.details)
        self.assertEqual(self.press(hv, fl, ["enter"]), [""])
        self.assertTrue(hv.details)
        self.press(hv, fl, ["space"])
        self.assertFalse(hv.details)

    def test_esc_q_leave_the_screen_and_change_nothing(self):
        hv, fl = self.view()
        self.press(hv, fl, ["down", "down"])
        for key in ("esc", "q"):
            self.assertEqual(screens.health_key(hv, key, fl), "back")
        for key in ("h", "1", "4", "tab", "?"):  # h is the overview's letter, the digits and Tab are the dispatcher's
            self.assertEqual(screens.health_key(hv, key, fl), "", key)
        self.assertEqual((hv.idx, hv.days, hv.details), (2, 7, False))
        self.press(hv, fl, ["enter"])
        self.assertEqual([screens.health_key(hv, "esc", fl), hv.details, screens.health_key(hv, "esc", fl)], ["", False, "back"])  # details first

    def test_period_keys_say_so_only_when_the_period_changes(self):
        hv, fl = self.view()
        self.assertEqual([screens.health_key(hv, k, fl) for k in ("7", "w")], ["", ""])  # the period it already has
        self.assertEqual(self.press(hv, fl, ["d"]), ["period"])
        self.assertEqual(hv.days, 1)
        self.assertEqual(self.press(hv, fl, ["d"]), [""])
        self.assertEqual((self.press(hv, fl, ["m"]), hv.days), (["period"], 30))
        self.assertEqual((self.press(hv, fl, ["w"]), hv.days), (["period"], 7))
        self.assertEqual((self.press(hv, fl, ["1", "3", "7"]), hv.days), ([""] * 3, 7))  # the digits are the screens now

    def test_other_keys_do_nothing_and_no_findings_is_not_an_error(self):
        hv, fl = self.view()
        self.press(hv, fl, ["down"])
        before = (hv.idx, hv.days, hv.details)
        self.assertEqual(self.press(hv, fl, ["x", "tab", "btab", "left", "right", "5", "c", "z"]), [""] * 8)   # m, w and d are periods: not here
        self.assertEqual((hv.idx, hv.days, hv.details), before)
        hv2, none = self.view(report(variant="none"))
        self.assertEqual(none, [])
        self.assertEqual(self.press(hv2, none, ["up", "down", "pgdn", "pgup", "home", "end", "enter", "enter"]), [""] * 8)
        self.assertEqual((hv2.idx, hv2.cur), (0, None))

    def test_the_cursor_follows_its_finding_through_a_refresh_and_a_period_change(self):
        hv, fl = self.view()
        self.press(hv, fl, ["down"] * 4)
        self.assertEqual(hv.cur, "cpu-hog:chrome")
        rep = report(change=lambda r: r["findings"].insert(0, finding("oom:db", "err", "Out of memory: db")))
        fl2 = screens.health_findings(rep)
        self.assertEqual(screens.health_sync(hv, fl2), 5)                                # one more above it: the cursor moved with its finding
        self.assertEqual(hv.cur, "cpu-hog:chrome")
        fl1 = screens.health_findings(report(days=1))                                    # 24 hours: the memory leak is not in the report
        self.assertEqual(screens.health_sync(hv, fl1), 3)
        self.assertEqual(hv.cur, "cpu-hog:chrome")

    def test_the_cursor_stays_in_place_when_its_finding_disappears(self):
        hv, fl = self.view()
        self.press(hv, fl, ["down"] * 6)
        self.assertEqual(hv.cur, "login-fail:sshd")
        fl2 = [f for f in fl if f["id"] != "login-fail:sshd"]
        self.assertEqual(screens.health_sync(hv, fl2), 6)                                # the same place, on what moved up into it
        self.assertEqual(hv.cur, fl2[6]["id"])
        self.assertEqual(screens.health_sync(hv, fl2[:3]), 2)                            # fewer than that: the last one
        self.assertEqual(screens.health_sync(hv, []), 0)
        self.assertIsNone(hv.cur)

    def test_the_cursor_row_is_always_on_screen(self):
        rep = report(change=lambda r: r["findings"].extend(finding("x:%d" % i, "info", "extra %d" % i) for i in range(30)))
        self.seed(rep)
        for cols, rows in SIZES:
            hv, fl = self.view(rep)
            for step in range(len(fl)):
                with self.subTest(size=(cols, rows), step=step):
                    frame, _ = render.health_screen(render.health_data(7), [], hv, cols - 1, rows)
                    out = self.check_frame(frame, cols - 1, rows)
                    chosen = [ansi.ANSI.sub("", x) for x in frame.split(LINE)[1:] if ESC + "[7m" in x and "─" not in x]
                    self.assertEqual(len(chosen), 1)                                     # exactly one highlighted finding ...
                    self.assertIn(fl[hv.idx]["title"], chosen[0])                        # ... the selected one
                    self.assertRegex(out[-1], r"finding %d/%d" % (hv.idx + 1, len(fl)))
                screens.health_key(hv, "down", fl)
            self.assertEqual(hv.idx, len(fl) - 1)

    def test_scrolling_keeps_the_cursor_a_few_rows_from_the_edge(self):
        for top, i, n, rows, want in ((0, 0, 40, 10, 0), (0, 9, 40, 10, 2), (2, 5, 40, 10, 2), (0, 39, 40, 10, 30), (30, 20, 40, 10, 18), (0, 3, 5, 10, 0)):
            self.assertEqual(ansi.scroll(top, i, n, rows), want, (top, i, n, rows))


class Once(HealthCase):
    """`render.py --once --view health`: the screen at the four sizes, for the three demos, the periods and the variants."""

    def test_every_frame_fills_its_screen_and_nothing_more(self):
        for os_name in OSES:
            render.DEMO_OS = os_name
            for variant in VARIANTS:
                render.DEMO_HEALTH = variant
                for days in PERIODS:
                    render._HEALTH.clear()
                    for cols, rows in SIZES:
                        for opts in (["--period", str(days)], ["--period", str(days), "--details"]):
                            with self.subTest(os=os_name, variant=variant, size=(cols, rows), opts=opts):
                                s, lines = self.screen(opts, cols, rows)
                                self.assertRegex(lines[0], r"\[\d[ ·](?:Health|Hlth)\]")
                                self.assertNotIn("Traceback", s)

    def test_odd_sizes_never_break_the_frame(self):
        """From a 40x10 console to a 300x80 one, either side of the widths where the layout changes (110, 140, 190)."""
        for os_name in OSES:
            render.DEMO_OS = os_name
            for variant in VARIANTS:
                render.DEMO_HEALTH = variant
                render._HEALTH.clear()
                data, pb = render.health_state(None, 7)
                fl = screens.health_findings(data["report"])
                for cols in (40, 60, 100, 109, 110, 139, 140, 189, 190, 300):
                    for rows in (10, 14, 20, 40, 80):
                        for details in (False, True):
                            with self.subTest(os=os_name, variant=variant, size=(cols, rows), details=details):
                                hv = screens.HealthView(7, now=NOW)
                                hv.details = details
                                screens.health_select(fl, hv, "crash")
                                frame, _ = render.health_screen(data, pb, hv, cols - 1, rows)
                                self.check_frame(frame, cols - 1, rows)
                        body = render.health_slide(cols - 1, rows - 2)
                        self.assertLessEqual(len(body), rows - 2)
                        self.assertFalse([x for x in body if len(ansi.ANSI.sub("", x)) > cols - 1], (os_name, variant, cols, rows))

    def test_every_finding_with_its_details_fits_every_size(self):
        for os_name in OSES:
            render.DEMO_OS = os_name
            render._HEALTH.clear()
            names = [f["id"] for f in screens.health_findings(report(os_name))]
            for fid in names:
                for cols, rows in SIZES:
                    with self.subTest(os=os_name, finding=fid, size=(cols, rows)):
                        s, lines = self.screen(["--select", fid, "--details"], cols, rows)
                        txt = "\n".join(lines)
                        self.assertIn("── DETAILS", txt)
                        title = next(f["title"] for f in screens.health_findings(report(os_name)) if f["id"] == fid)
                        self.assertRegex(txt, r"(?m)^ %s$|│  %s" % (re.escape(title), re.escape(title)))   # the pane's first line: the whole title
                        self.assertIn(" what ", txt)
                        self.assertIn(" fix ", txt)

    def test_the_title_says_the_period_the_history_and_the_findings(self):
        s, lines = self.screen([], 200, 50)
        self.assertRegex(lines[1], r"^── HEALTH  last 7 days · since 20\d\d-\d\d-\d\d \d\d:\d\d UTC · 168 h of data · ✖ 1 err  ! 6 warn  · 3 info ─+  d:24h   w:7d   m:30d")
        self.assertIn(ESC + "[7m w:7d ", s)                                            # the period in use is reversed
        s, lines = self.screen(["--period", "1"], 200, 50)
        self.assertIn("last 24 hours", lines[1])
        self.assertIn("24 h of data", lines[1])
        self.assertIn(ESC + "[7m d:24h ", s)
        s, lines = self.screen(["--period", "30"], 200, 50)
        self.assertIn("last 30 days", lines[1])
        self.assertIn("384 h of data", lines[1])                                        # the demo machine has 16 days of history
        s, lines = self.screen([], 79, 24)
        self.assertIn("last 7 days", lines[1])                                          # narrow: the least needed (the coverage) goes first
        self.assertNotIn("h of data", lines[1])
        self.assertIn("✖ 1 err", lines[1])
        self.assertIn("d:24h", lines[1])
        self.assertIn("PROBLEMS", lines[0])                                              # the header's status is the dashboard's

    def test_the_sections_follow_the_width(self):
        txt = {c: "\n".join(self.screen([], c, 50)[1]) for c, _ in SIZES}
        for c in (79, 120, 200, 226):
            for title in ("FINDINGS", "TOP CPU", "TOP MEMORY", "EVENTS", "NOISY / NEW LOGS", "DISKS", "THERMAL", "BOOTS"):
                self.assertIn("── " + title + " ", txt[c], (c, title))                  # 50 rows: all of them at every width
        cols = lambda c: max(len(re.findall(r"── [A-Z]", x)) for x in txt[c].split("\n")[4:])  # sections side by side  # noqa: E731
        self.assertEqual((cols(79), cols(120), cols(200), cols(226)), (1, 2, 3, 3))

    def test_period_switch_changes_what_is_shown(self):
        week = "\n".join(self.screen([], 120, 33)[1])
        day = "\n".join(self.screen(["--period", "1"], 120, 33)[1])
        month = "\n".join(self.screen(["--period", "30"], 120, 33)[1])
        self.assertIn("Memory keeps growing: node", week)
        self.assertNotIn("Memory keeps growing: node", day)                             # memory trends need 3 days: the report says so
        self.assertIn("memory trends need a period of at least 3 days", day)
        self.assertIn("per hour", day)
        self.assertIn("per day", week)
        self.assertIn("Memory keeps growing: node", month)
        self.assertEqual(self.calls, [7, 1, 30])                                        # one report per period asked for

    def test_select_and_details_open_the_pane_below_or_beside_the_list(self):
        s, lines = self.screen(["--select", "DISK", "--details"], 79, 24)               # any case, id or title
        self.assertIn("finding 3/10", lines[-1].replace("  ", " "))
        i = next(j for j, x in enumerate(lines) if x.startswith("── DETAILS"))
        self.assertGreater(i, 4)                                                         # below the findings
        pane = "\n".join(lines[i:])
        self.assertIn("Disk filling up: /data", pane)
        self.assertIn("what", pane)
        self.assertIn("facts", pane)
        self.assertIn("used_gb 249", pane)
        self.assertIn("days_to_full 12.4", pane)
        self.assertIn("fix", pane)
        self.assertIn("du -xh --max-depth=1", pane)
        self.assertNotIn("── TOP CPU", "\n".join(lines))                                # the pane takes the place of the sections
        self.assertEqual([x.strip() for x in lines if "Disk filling up" in x][0][:8], "! WARN  ")
        s, lines = self.screen(["--select", "hog", "--details"], 200, 50)               # wide: beside the list, the sections stay
        row = next(x for x in lines if "── DETAILS" in x)
        self.assertGreater(row.index("── DETAILS"), 60)
        self.assertIn("   │ ── DETAILS", row)
        txt = "\n".join(lines)
        self.assertIn("what   chrome used over 80% of one core", txt)
        self.assertIn("cpu_s 375000", txt)
        self.assertIn("── TOP CPU", txt)
        s, lines = self.screen(["--select", "hog"], 200, 50)                            # a selection alone: no pane
        self.assertNotIn("DETAILS", "\n".join(lines))
        self.assertTrue([x for x in s.split(LINE)[1:] if ESC + "[7m" in x and "─" not in x])  # the cursor is still on its row
        s, lines = self.screen(["--select", "nothing like this", "--details"], 120, 33)  # no match: the cursor stays on the first finding
        self.assertIn("finding 1/10", lines[-1])

    def test_epochs_in_the_facts_are_dates_and_the_text_wraps_inside_the_pane(self):
        s, lines = self.screen(["--select", "oom", "--details"], 79, 24)
        txt = "\n".join(lines)
        self.assertRegex(txt, r"last 20\d\d-\d\d-\d\d \d\d:\d\d UTC")
        self.assertNotIn("1789", txt)                                                    # not the raw number
        for x in lines:
            self.assertLessEqual(len(x), 78)

    def test_the_cursor_row_is_highlighted_and_the_pills_keep_their_symbols(self):
        s, lines = self.screen(["--select", "restart", "--details"], 120, 33)
        shown = [x for x in s.split(LINE)[1:] if ESC + "[7m" in x and "FINDINGS" not in x and "d:24h" not in x and "keys" not in x and "finding" not in x]
        self.assertEqual(len(shown), 1)
        self.assertIn("Restarting: shop-worker-1", shown[0])
        self.assertNotIn(ESC + "[41m", shown[0].replace(ESC + "[1;41;37m", ""))        # the row is plain reverse video: no pill colour left in it
        txt = "\n".join(lines)
        for pill in ("✖ ERR", "! WARN", "· INFO"):
            self.assertIn(pill, txt)

    def test_the_command_line_prints_the_screen_for_each_demo_os(self):
        for os_name in OSES:
            render._HEALTH.clear()                                                       # (a demo's kind is set once per process: no key for it)
            out = io.StringIO()
            args = ["render.py", "--once", "--demo", "--view", "health", "--cols", "120", "--rows", "33"] + (["--demo-os", os_name] if os_name else [])
            with contextlib.redirect_stdout(out):
                self.assertIsNone(render.once(args))
            lines = out.getvalue().replace("\r", "").rstrip("\n").split("\n")
            self.assertEqual(len(lines), 33)
            self.assertTrue(all(len(x) <= 119 for x in lines), os_name)
            self.assertNotIn(ESC, out.getvalue())                                        # without --color: no escape codes at all
            self.assertIn({None: "Out of memory: shop-worker-1", "windows": "Crashing: contoso-sync.exe",
                           "darwin": "Crashing: photolibraryd"}[os_name], out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render.once(["render.py", "--once", "--demo", "--view", "health", "--color", "--cols", "120", "--rows", "33"])
        self.assertIn(ESC + "[", out.getvalue())                                         # --color keeps them (tools/ansi2svg.py)

    def test_the_command_line_refuses_a_bad_period_and_the_feature_off(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = render.once(["render.py", "--once", "--demo", "--view", "health", "--period", "9"])
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertIn("--period must be 1, 7 or 30", err.getvalue())
        render.CFG["features"]["health"] = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = render.once(["render.py", "--once", "--demo", "--view", "health"])
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertIn("[features] health = no", err.getvalue())
        self.assertEqual(self.calls, [])                                                 # nothing was computed

    def test_the_demo_variants(self):
        render.DEMO_HEALTH = "little"
        s, lines = self.screen([], 120, 33)
        txt = "\n".join(lines)
        self.assertIn("· collecting: 5 hours so far; trends need 24 hours of data", txt)
        self.assertIn("5 h of data", lines[1])
        self.assertIn("finding 1/2", lines[-1])
        self.assertNotIn("Memory keeps growing", txt)                                    # no conclusion that needs days
        self.assertNotIn("full in", txt)
        render._HEALTH.clear()
        render.DEMO_HEALTH = "none"
        s, lines = self.screen([], 120, 33)
        txt = "\n".join(lines)
        self.assertIn(screens.HEALTH_NONE, txt)
        self.assertNotIn("── FINDINGS", txt)
        self.assertNotIn("no findings ─", lines[1])                                      # nothing to count: no "no findings" in the title
        self.assertIn("no findings", lines[-1])


class DataSource(HealthCase):
    """health_data(): the history opened read-only, health.report() at most once a minute per period, a failure that does not escape."""

    def setUp(self):
        super().setUp()
        render.DEMO = False
        self.reports = []
        self.conns = []

        def fake_report(conn, now=None, days=7, cores=None):
            self.reports.append(days)
            return report(days=days)

        def fake_open(path=None):
            conn = mock.Mock()
            conn.execute.side_effect = sqlite3.OperationalError("no such table: app_hour")  # health_series must live with it
            self.conns.append(conn)
            return conn
        self.patches = [mock.patch.object(health, "report", fake_report), mock.patch.object(history, "open_ro", fake_open)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        render.health_build = self.saved["health_build"]

    def test_once_a_minute_per_period_whatever_the_number_of_calls(self):
        for _ in range(50):
            render.health_data(7)
        self.assertEqual(self.reports, [7])
        self.now += 59
        render.health_data(7)
        self.assertEqual(self.reports, [7])
        self.now += 2                                                                    # a minute has passed
        render.health_data(7)
        self.assertEqual(self.reports, [7, 7])
        render.health_data(1)
        render.health_data(30)
        render.health_data(30)
        self.assertEqual(self.reports, [7, 7, 1, 30])
        render.health_data(5)                                                            # not a period: the default one, from its cache
        render.health_data(None)
        self.assertEqual(self.reports, [7, 7, 1, 30])
        self.assertEqual([c.close.call_count for c in self.conns], [1, 1, 1, 1])        # every connection is closed

    def test_keys_and_frames_never_ask_for_the_report_again(self):
        data = render.health_data(7)
        hv, fl = self.view(data["report"])
        for _ in range(3):
            for k in ("down", "enter", "up", "enter", "end", "home"):
                screens.health_key(hv, k, fl)
                render.health_screen(render.health_data(hv.days), [], hv, 119, 33)
        self.assertEqual(self.reports, [7])
        screens.health_key(hv, "d", fl)                                                   # another period: its own report, once
        for _ in range(5):
            render.health_screen(render.health_data(hv.days), [], hv, 119, 33)
        self.assertEqual(self.reports, [7, 1])

    def test_report_is_called_with_the_period_and_the_screen_shows_it(self):
        s, lines = self.screen(["--period", "30"], 120, 33)
        self.assertEqual(self.reports, [30])
        self.assertIn("last 30 days", lines[1])

    def test_no_history_yet(self):
        with mock.patch.object(history, "open_ro", lambda path=None: None):
            data = render.health_data(7)
        self.assertEqual((data["report"], data["msg"], data["err"]), (None, screens.HEALTH_NONE, False))
        self.assertIn("starts recording when [features] health is on", data["msg"])
        self.assertIn("after the first hour", data["msg"])
        s, lines = self.screen([], 79, 24)
        self.assertIn("no history yet", "\n".join(lines))
        self.assertIn("no findings", lines[-1])
        self.assertEqual(self.reports, [])
        self.now += 61
        self.assertIsNotNone(render.health_data(7)["report"])                           # the collector has started: the next minute shows it

    def test_an_unreadable_history_is_a_message_kept_for_the_minute(self):
        def broken(conn, now=None, days=7, cores=None):
            self.reports.append(days)
            raise sqlite3.DatabaseError("file is not a database " + ESC + "[2J")
        with mock.patch.object(health, "report", broken):
            data = render.health_data(7)
            for _ in range(5):
                render.health_data(7)
            self.assertEqual(self.reports, [7])                                          # not retried on every key
            s, lines = self.screen([], 120, 33)
        self.assertIsNone(data["report"])
        self.assertTrue(data["err"])
        self.assertIn("the history could not be read", data["msg"])
        self.assertNotIn(ESC, data["msg"])
        self.assertIn("the history could not be read: DatabaseError", "\n".join(lines))
        self.assertEqual([c.close.call_count for c in self.conns], [1])                 # and the connection was closed anyway

    def test_a_missing_health_module_costs_the_screen_not_the_dashboard(self):
        with mock.patch.dict(sys.modules, {"health": None}):
            data = render.health_data(7)
        self.assertIsNone(data["report"])
        self.assertTrue(data["err"])
        s, lines = self.screen([], 120, 33)
        self.assertIn("the history could not be read", "\n".join(lines))

    def test_sparklines_come_from_app_hour_when_the_report_has_none(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE app_hour(hour INTEGER, app TEXT, cpu_s REAL, rss_max INTEGER, rss_avg INTEGER, procs_max INTEGER, samples INTEGER, "
                   "PRIMARY KEY(hour, app))")
        day0 = NOW // 86400
        for d in range(5):                                                               # 5 days, 2 hours each: chrome grows, node is flat
            for h in (3, 4):
                db.execute("INSERT INTO app_hour VALUES (?, 'chrome', ?, ?, ?, 1, 60)", ((day0 - 4 + d) * 24 + h, 100.0 * (d + 1), 10 * 2 ** 20 * (d + 1), 10 * 2 ** 20 * (d + 1)))
                db.execute("INSERT INTO app_hour VALUES (?, 'node', 50, 1, 1, 1, 60)", ((day0 - 4 + d) * 24 + h,))
        rep = {"period": {"from": NOW - 7 * 86400, "to": NOW, "days": 7}, "top_cpu": [{"app": "chrome"}, {"app": "node"}, {"app": "gone"}],
               "top_mem": [{"app": "chrome"}, {"app": "kept", "series": [1, 2]}]}
        render.health_series(db, rep)
        chrome, node, gone = rep["top_cpu"]
        self.assertEqual(len(chrome["series"]), 7)
        self.assertEqual(chrome["series"][:2], [None, None])                             # before the first record: not a zero
        self.assertEqual(chrome["series"][2:], [200.0, 400.0, 600.0, 800.0, 1000.0])    # CPU seconds per day: two hours of 100, 200 ... each
        self.assertEqual(node["series"][2:], [100.0] * 5)
        self.assertEqual(gone["series"], [])
        self.assertEqual(rep["top_mem"][0]["series"][2:], [10.0, 20.0, 30.0, 40.0, 50.0])   # MB
        self.assertEqual(rep["top_mem"][1]["series"], [1, 2])                            # a report's own series is left alone
        rep1 = {"period": {"from": NOW - 86400, "to": NOW, "days": 1}, "top_cpu": [{"app": "chrome"}], "top_mem": []}
        db.execute("INSERT INTO app_hour VALUES (?, 'chrome', 7, 1, 1, 1, 60)", (NOW // 3600 - 2,))
        render.health_series(db, rep1)
        self.assertEqual(len(rep1["top_cpu"][0]["series"]), 24)                          # 24 hours: per hour
        self.assertEqual(rep1["top_cpu"][0]["series"][-3], 7.0)
        db.close()
        render.health_series(db, rep1)                                                   # a closed database: no series, no exception
        render.health_series(None, {"period": None})

    def test_the_screen_draws_with_and_without_sparklines(self):
        rep = report(change=lambda r: [x.pop("series") for k in ("top_cpu", "top_mem") for x in r[k]])
        self.seed(rep)
        s, lines = self.screen([], 120, 33)
        self.assertNotIn("▇", "".join(lines[4:]))
        self.assertIn("chrome", "\n".join(lines))
        render._HEALTH.clear()
        s, lines = self.screen([], 120, 33)                                              # with: the demo has them
        self.assertRegex("\n".join(lines), "chrome .*[▁▂▃▄▅▆▇█]{5}")


class Rotation(HealthCase):
    """[dashboard] health_in_rotation: the Health screen as one of the rotating pages (a monitor nobody types on)."""

    def setUp(self):
        super().setUp()
        render.page_overview = overview_stub                                             # the overview reads the host: not the subject here
        self.cont, self.net, self.boot, self.base = demo.snapshot(now=NOW)

    def slides(self, w, body_h, **kw):
        return render.slides(FakeSampler().sample(), self.cont, self.net, w, body_h, self.boot, self.base, mode="overview", **kw)

    def test_the_screen_joins_the_rotation_when_asked(self):
        self.assertEqual([x[0] for x in self.slides(119, 31)], ["Overview"])
        render.CFG["health_in_rotation"] = True
        for os_name in OSES:
            render.DEMO_OS = os_name
            for cols, rows in SIZES:
                w, body_h = cols - 1, rows - 2
                with self.subTest(os=os_name, size=(cols, rows)):
                    sl = self.slides(w, body_h)
                    self.assertEqual([x[0] for x in sl], ["Overview", "Health"])
                    name, part, parts, body = sl[-1]
                    self.assertEqual((part, parts), (1, 1))
                    self.assertLessEqual(len(body), body_h)
                    self.assertFalse([x for x in body if len(ansi.ANSI.sub("", x)) > w])
                    self.assertFalse([x for x in body if ESC + "[7m" in x and "d:24h" in x])   # no period selector, no cursor on a monitor without a keyboard
                    txt = "\n".join(ansi.ANSI.sub("", x) for x in body)
                    self.assertIn("── HEALTH ", txt)
                    self.assertIn("── FINDINGS ", txt)
                    self.assertNotIn("d:24h", txt)
                    frame = render.frame(sl[-1], 1, len(sl), w, rows, [], keys=False)
                    self.check_frame(frame, w, rows, cols)
        render.DEMO_OS = None
        sl = self.slides(119, 31)
        self.assertEqual(render.pick_slide(sl, sum(render.slide_seconds(x, len(sl)) for x in sl) - 1), 1)  # its turn comes
        self.assertEqual([x[0] for x in self.slides(119, 31, scroll=True)], ["Overview"])  # the scrolling web page: one page only

    def test_findings_that_fit_then_the_top_apps(self):
        render.CFG["health_in_rotation"] = True
        txt = "\n".join(ansi.ANSI.sub("", x) for x in self.slides(199, 48)[-1][3])
        self.assertIn("Out of memory: shop-worker-1", txt)
        self.assertIn("Slower boot", txt)                                                # all ten fit
        self.assertNotIn("more findings", txt)
        self.assertIn("── TOP CPU", txt)
        self.assertIn("── TOP MEMORY", txt)
        self.assertNotIn("── EVENTS", txt)                                              # only the apps
        body = [ansi.ANSI.sub("", x) for x in self.slides(78, 22)[-1][3]]
        self.assertEqual(len(body), 22)
        self.assertRegex("\n".join(body), r"… \+\d+ more findings")                      # a small screen counts what it cuts
        self.assertIn("Out of memory", body[2])                                          # the worst first
        self.assertIn("TOP CPU", "\n".join(body))

    def test_a_screen_too_small_for_the_apps_keeps_the_findings(self):
        render.CFG["health_in_rotation"] = True
        body = [ansi.ANSI.sub("", x) for x in self.slides(78, 6)[-1][3]]
        self.assertLessEqual(len(body), 6)
        self.assertIn("── FINDINGS", "\n".join(body))
        self.assertRegex("\n".join(body), "more findings")

    def test_no_slide_when_off_or_when_the_feature_is_disabled(self):
        render.CFG["health_in_rotation"] = False
        self.assertNotIn("Health", [x[0] for x in self.slides(199, 48)])
        render.CFG["health_in_rotation"] = True
        render.CFG["features"]["health"] = False
        self.assertNotIn("Health", [x[0] for x in self.slides(199, 48)])
        frame = render.frame(self.slides(199, 48)[0], 0, 1, 199, 50, [], keys=True)
        self.assertNotIn("4", ansi.ANSI.sub("", frame).splitlines()[-1].split(": screens")[0])  # and the footer does not offer 4
        render.CFG["features"]["health"] = True
        frame = render.frame(self.slides(199, 48)[0], 0, 1, 199, 50, [], keys=True)
        self.assertIn("1-5: screens", ansi.ANSI.sub("", frame))
        frame = render.frame(self.slides(199, 48)[0], 0, 1, 199, 50, [], keys=False)    # a monitor with no keyboard: nothing to press
        self.assertNotIn("screens", ansi.ANSI.sub("", frame))

    def test_no_history_and_little_data_are_said_in_the_slide(self):
        render.CFG["health_in_rotation"] = True
        for variant, word in (("none", "no history yet"), ("little", "collecting: 5 hours so far")):
            render.DEMO_HEALTH = variant
            render._HEALTH.clear()
            for cols, rows in SIZES:
                with self.subTest(variant=variant, size=(cols, rows)):
                    body = self.slides(cols - 1, rows - 2)[-1][3]
                    self.assertIn(word, "\n".join(ansi.ANSI.sub("", x) for x in body))
                    self.check_frame(render.frame(("Health", 1, 1, body), 1, 2, cols - 1, rows, [], keys=False), cols - 1, rows)

    def test_a_broken_slide_is_an_error_line_not_a_crash(self):
        render.CFG["health_in_rotation"] = True

        def broken(*a, **kw):
            raise ValueError("bad state " + ESC + "[2J")
        render.health_slide = broken
        sl = self.slides(119, 31)
        self.assertEqual(sl[-1][0], "Health")
        line = sl[-1][3][0]
        self.assertIn("error on page Health", ansi.ANSI.sub("", line))
        self.assertNotIn(ESC + "[2J", line)

    def test_config_health_in_rotation_yes_no_and_not_a_boolean(self):
        path = os.path.join(self.tmp.name, "config.ini")
        got = {}
        for value in ("yes", "no", "on", "maybe"):
            with open(path, "w", encoding="utf-8") as f:
                f.write("[dashboard]\nhealth_in_rotation = " + value + "\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                got[value] = (nuc_config.load(path)["health_in_rotation"], err.getvalue())
        self.assertEqual((got["yes"], got["no"], got["on"]), ((True, ""), (False, ""), (True, "")))
        self.assertFalse(got["maybe"][0])                                                # the default (off) stays
        self.assertIn("[dashboard] health_in_rotation is not a boolean: kept off", got["maybe"][1])
        self.assertFalse(nuc_config.load(os.path.join(self.tmp.name, "missing.ini"))["health_in_rotation"])
        with open(path, "w", encoding="utf-8") as f:
            f.write("[dashboard]\nhealth_in_rotation = yes\n[features]\nhealth = no\n")
        cfg = nuc_config.load(path)
        self.assertEqual((cfg["health_in_rotation"], cfg["features"]["health"]), (True, False))


class MainLoop(HealthCase):
    """render.main() on a fake terminal: `h` opens the Health screen, its keys move it, Esc/q/1 go back, an idle one gives the monitor
    back. KEY_CHAR & co. decode the raw reads, as on a real console."""

    def run_main(self, script, keyboard=True, cols=80, rows=24):
        """script: what each keyboard read returns (raw bytes), a number of seconds that pass without a key, or (seconds, bytes):
        a key typed that long after the frame. -> (the frames drawn, ANSI stripped, each a list of lines; what tcsetattr got back)."""
        out, err, restored, todo = io.StringIO(), io.StringIO(), [], list(script)

        def next_read(fd=None, wait=None):
            if not todo:
                raise Stop()
            x = todo.pop(0)
            if isinstance(x, tuple):
                self.now += x[0]
                x = x[1]
            if isinstance(x, bytes):
                return render.decode_keys(x)
            self.now += x
            return []
        render.sys = Proxy(sys, stdin=types.SimpleNamespace(fileno=lambda: 5), stdout=out, stderr=err)
        render.os = Proxy(os, isatty=lambda fd: keyboard)
        render.termios = types.SimpleNamespace(tcgetattr=lambda fd: ["attrs", fd], TCSADRAIN=1,
                                               tcsetattr=lambda fd, when, attrs: restored.append(attrs))
        render.tty = types.SimpleNamespace(setcbreak=lambda fd: None)
        render.signal = Proxy(signal, signal=lambda *a: None)
        render.shutil = Proxy(shutil, get_terminal_size=lambda fallback=None: os.terminal_size((cols, rows)))
        render.WINDOWS, render.MODE, render.Sampler = False, "overview", FakeSampler
        render.read_keys, render.page_overview = next_read, overview_stub
        self.on_sleep = lambda sec: next_read()                                          # no keyboard: the loop sleeps between frames
        with self.assertRaises(Stop):
            render.main(["render.py"])
        text = out.getvalue()
        self.assertTrue(text.endswith("\x1b[?25h\x1b[0m"))                               # the cursor is given back
        frames = []
        for chunk in text[:-len("\x1b[?25h\x1b[0m")].split("\x1b[H")[1:]:
            chunk = chunk[:-len("\x1b[2J")] if chunk.endswith("\x1b[2J") else chunk    # cleared when the screen changes
            frames.append(self.check_frame(chunk, cols - 1, rows, len(frames)))
        return frames, restored

    @staticmethod
    def is_health(frame):
        return bool(re.search(r"\[\d[ ·](?:Health|Hlth)\]", frame[0]) and "── HEALTH " in frame[1])

    @staticmethod
    def at(frame):
        m = re.search(r"finding (\d+)/(\d+)", frame[-1].replace("  ", " "))
        return (int(m.group(1)), int(m.group(2))) if m else None

    def test_h_opens_the_screen_keys_move_it_and_h_esc_q_go_back(self):
        frames, restored = self.run_main([b"h", b"\x1b[B\x1bOB", b"\x1b[B", b"\r", b"\x1b[6~", b"\x1b[H", b"1", b"4", b"\x1b", b"h", b"q"])
        self.assertEqual([self.is_health(f) for f in frames], [False, True, True, True, True, True, True, False, True, False, True, False])
        self.assertIn("1-5: screens", frames[0][-1])                                     # a keyboard: the footer offers the screens (4 is this one)
        self.assertIn("overview page (stub)", "\n".join(frames[0]))
        self.assertEqual(self.at(frames[1]), (1, 10))
        self.assertEqual(self.at(frames[2]), (3, 10))                                    # ↓ (CSI) ↓ (SS3)
        self.assertEqual(self.at(frames[3]), (4, 10))
        self.assertNotIn("── DETAILS", "\n".join(frames[3]))
        self.assertIn("── DETAILS", "\n".join(frames[4]))                                # Enter
        self.assertIn("Memory keeps growing: node", "\n".join(frames[4]))
        self.assertEqual(self.at(frames[6])[0], 1)                                       # Home
        self.assertEqual(self.at(frames[6]), (1, 10))
        self.assertEqual(restored, [["attrs", 5]])                                       # the terminal's settings are put back

    def test_the_period_keys_change_the_period_and_ask_for_each_report_once(self):
        frames, _ = self.run_main([b"h", b"d", b"\x1b[B", b"m", b"m", b"w", b"w", b"d", b"m", b"\x1b"])
        health = [f for f in frames if self.is_health(f)]
        titles = [re.search(r"last (\d+) (?:days|hours)", f[1]).group(0) for f in health]
        self.assertEqual(titles, ["last 7 days", "last 24 hours", "last 24 hours", "last 30 days", "last 30 days", "last 7 days", "last 7 days",
                                  "last 24 hours", "last 30 days"])
        self.assertEqual(sorted(self.calls), [1, 7, 30])                                 # one report per period for the whole session

    def test_the_report_is_not_asked_for_again_within_a_minute_but_is_after(self):
        self.run_main([b"h", b"\x1b[B", (render.REFRESH_S, b"\x1b[B"), 5, b"\x1b[B", b"\x1b"])
        self.assertEqual(self.calls, [7])
        self.calls[:] = []
        render._HEALTH.clear()
        self.run_main([b"h", render.REFRESH_S * 20, render.HEALTH_TTL, b"\x1b"])
        self.assertEqual(self.calls, [7, 7])                                             # the screen stayed open past the minute: a new report

    def test_an_idle_screen_gives_the_monitor_back_to_the_rotation(self):
        idle = render.HEALTH_IDLE_S
        frames, _ = self.run_main([b"h", idle - 1, b"\x1b[B", idle - 1, 2])
        self.assertEqual([self.is_health(f) for f in frames], [False, True, True, True, True, False])
        self.assertEqual(self.at(frames[4]), (2, 10))                                    # a key restarts the count

    def test_without_a_keyboard_the_screen_is_not_offered(self):
        frames, restored = self.run_main([2, 2], keyboard=False)
        self.assertEqual(len(frames), 3)
        self.assertFalse([f for f in frames if self.is_health(f) or "screens" in f[-1]])
        self.assertEqual(restored, [])                                                   # no terminal settings were changed

    def test_feature_off_h_does_nothing(self):
        render.CFG["features"]["health"] = False
        frames, restored = self.run_main([b"h", b"4", b"\x1b[B"])
        self.assertEqual(len(frames), 4)
        self.assertFalse([f for f in frames if self.is_health(f) or "1-5: screens" in f[-1]])
        self.assertEqual(restored, [["attrs", 5]])
        self.assertEqual(self.calls, [])                                                 # and the history is never read

    def test_the_other_screens_keys_are_not_taken(self):
        frames, _ = self.run_main([b"1", b"h", b"\x1b"])                                 # 1 is the Overview here, not a period
        self.assertEqual([self.is_health(f) for f in frames], [False, False, True, False])
        self.assertEqual(self.calls, [7])
        self.assertIn("last 7 days", frames[2][1])

    def test_a_broken_report_is_an_error_frame_esc_still_works_and_it_retries(self):
        calls, real = [], render.health_state

        def flaky(smp, days):
            calls.append(self.now - NOW)
            if len(calls) == 1:
                raise ValueError("state unreadable " + ESC + "[2J")
            return real(smp, days)
        render.health_state = flaky
        frames, _ = self.run_main([b"h", render.REFRESH_S, b"\x1b", b"h", b"\x1b"])  # check_frame: no raw escape on screen
        error = lambda f: bool(re.search(r"\[\d[ ·](?:Health|Hlth)\]", f[0]) and "error on the health screen" in f[1])  # noqa: E731
        self.assertEqual([error(f) for f in frames], [False, True, False, False, False, False])
        self.assertIn("ValueError('state unreadable", frames[1][1])
        self.assertEqual(frames[1][-1].strip(), "Esc: back   1-5: screens")
        self.assertEqual([self.is_health(f) for f in frames], [False, False, True, False, True, False])  # the next refresh draws it
        self.assertEqual(calls, [0, render.REFRESH_S, render.REFRESH_S])
        self.assertNotIn("ALL OK", frames[1][0])                                         # nothing could be read: never a reassuring status

    def test_a_broken_screen_does_not_say_all_ok(self):
        def broken(*a, **kw):
            raise ValueError("boom")
        render.health_screen = broken
        frames, _ = self.run_main([b"h", b"\x1b"])
        self.assertIn("PROBLEMS", frames[0][0])
        self.assertIn("error on the health screen", frames[1][1])
        self.assertNotIn("ALL OK", frames[1][0])

    def test_no_history_and_a_narrow_console(self):
        render.DEMO_HEALTH = "none"
        frames, _ = self.run_main([b"h", b"\x1b[B", b"\r", b"d", b"\x1b", b"\x1b"], cols=80, rows=24)  # Esc: the details, then back
        health = [f for f in frames if re.search(r"\[\d[ ·](?:Health|Hlth)\]", f[0])]
        self.assertEqual(len(health), 5)
        for f in health:
            self.assertIn(screens.HEALTH_NONE[:60], "\n".join(f))
            self.assertIn("no findings", f[-1])


class HostileAndEmpty(HealthCase):
    """Names, templates, texts and facts come from logs and process lists: control characters and escape sequences stay inert."""

    EVIL = ESC + "[2J" + ESC + "]0;pwn" + BEL + "evil" + CSI8 + "1m"
    CJK = "\u30e1\u30e2\u5e33.exe"                                                       # wide characters would shift every column
    FORMAT = "a\u202eb\u200bc\u2028d"                                                    # right-to-left override, zero width space, line separator

    def hostile(self):
        e, wide, fmt = self.EVIL, self.CJK, self.FORMAT
        rep = report()
        rep["findings"] = [finding("oom:" + e, "err", "Out of memory: " + e + "<b>x</b>", "text " + e + " " + wide + " " + fmt + " <script>alert(1)</script>",
                                   "fix " + e + " <name> & " + wide, {"n": 3, "last": NOW - 3600, e: e, "bad": float("nan"), "big": 10 ** 30, "flag": True,
                                                                  "x" * 200: "y" * 500}),
                           finding("crash-loop:" + wide, "warn", wide * 40, "t" * 3000, "f" * 3000), finding("weird:1", "bogus", None, None, None, facts="not a dict"),
                           finding("weird:2", "info", "", "", "", facts={})]
        rep["top_cpu"] = [{"app": e, "cpu_s": "x", "share": None, "avg_pct": float("inf"), "peak_hour": "never", "series": [e, None, "a", 3]},
                          {"app": wide, "cpu_s": 1, "share": 0.5, "avg_pct": 3, "peak_hour": 10 ** 30, "series": [1, 2]}]
        rep["top_mem"] = [{"app": e + fmt, "rss_max": None, "rss_avg": "x", "trend_mb_day": "up"}, {"app": "ok", "rss_max": 1, "rss_avg": 1, "trend_mb_day": 500}]
        rep["events"] = {e: [{"subject": e, "n": "many", "last": None}], "crash": [{"subject": wide + e, "n": 3, "last": "x"}, "not a dict"], "oom": []}
        rep["logs"] = [{"source": e, "unit": wide, "template": e + " " + fmt + " <img src=x onerror=alert(1)>", "n": 10 ** 12, "new": True}]
        rep["disks"] = [{"mount": e, "used_pct": "full", "days_to_full": "soon"}, {"mount": wide, "used_pct": 250, "days_to_full": -5}]
        rep["thermal"] = {"hours_hot": 5, "max": 80, "apps_when_hot": [{"app": e, "share": "x"}, {"app": wide, "share": 0.1}]}
        rep["boots"] = [{"boot": e, "total_s": e}, {"boot": 1, "total_s": None}]
        rep["notes"] = ["note " + e + " " + wide] * 5
        return rep

    def test_control_characters_in_the_report_never_reach_the_console(self):
        self.seed(self.hostile())
        for cols, rows in SIZES:
            for opts in ([], ["--details"], ["--select", "crash", "--details"], ["--select", "weird:1", "--details"], ["--select", "oom", "--details"]):
                with self.subTest(size=(cols, rows), opts=opts):
                    s, lines = self.screen(opts, cols, rows)                              # check_frame: no control character, nothing too wide
                    self.assertNotIn(ESC + "[2J", s)
                    self.assertNotIn(ESC + "]", s)
                    self.assertNotIn(BEL, s)
                    self.assertNotIn(CSI8, s)
                    self.assertNotIn("\u202e", s)
                    self.assertNotIn(self.CJK, s)
                    self.assertIn("evil", "\n".join(lines)) if "--select" in opts and opts[1] == "oom" else None
        s, lines = self.screen(["--select", "oom", "--details"], 200, 50)
        self.assertIn("?[2J?]0;pwn?evil?1m", "\n".join(lines))                            # visible, but plain text

    def test_every_size_and_cursor_position_of_a_hostile_report(self):
        self.seed(self.hostile())
        fl = screens.health_findings(render.health_data(7)["report"])
        for cols, rows in SIZES:
            hv = screens.HealthView(7, now=NOW)
            for details in (False, True):
                hv.details = details
                for i in range(len(fl)):
                    with self.subTest(size=(cols, rows), details=details, finding=i):
                        frame, _ = render.health_screen(render.health_data(7), [], hv, cols - 1, rows)
                        self.check_frame(frame, cols - 1, rows)
                    screens.health_key(hv, "down", fl)

    def test_a_slide_of_a_hostile_report(self):
        self.seed(self.hostile())
        for cols, rows in SIZES:
            body = render.health_slide(cols - 1, rows - 2)
            self.check_frame(render.frame(("Health", 1, 1, body), 1, 2, cols - 1, rows, [], keys=False), cols - 1, rows)

    def test_a_message_from_the_history_is_inert_too(self):
        render._HEALTH[7] = {"report": None, "msg": "the history " + self.EVIL + " could not be read", "err": True, "at": self.now}
        for cols, rows in SIZES:
            s, lines = self.screen([], cols, rows)
            self.assertIn("?[2J", "\n".join(lines))
            self.assertNotIn(ESC + "[2J", s)
        self.assertEqual(ui.hclean(None), "")
        self.assertEqual(ui.hclean("a\x00b\x9bc\nd"), "a?b?c?d")
        self.assertEqual(ui.hclean("x" * 50, 10), "x" * 9 + "…")
        self.assertEqual(ui.hclean("\u202e\u200b\u30e1"), "???")

    def test_malformed_reports_are_an_error_not_a_crash_of_the_loop(self):
        for bad in ({}, {"period": None}, {"findings": "no"}, {"findings": [None, 3, {"id": 5}], "coverage": {"hours": 5}, "period": {"days": 7}}):
            self.seed(dict(bad, period=bad.get("period", {"days": 7})), 7)
            try:
                render.health_screen(render.health_data(7), [], screens.HealthView(7), 119, 33)
            except Exception:  # noqa: BLE001 - the loop shows an error frame for it (MainLoop); it never gets past that
                pass

    def test_empty_sections_say_so_and_never_look_fine(self):
        rep = report(change=lambda r: r.update(top_cpu=[], top_mem=[], events={}, logs=[], disks=[], boots=[],
                                               thermal={"hours_hot": 0, "max": None, "apps_when_hot": []}, findings=[]))
        self.seed(rep)
        s, lines = self.screen([], 200, 50)
        txt = "\n".join(lines)
        for word in ("no CPU data", "no memory data", "none recorded in this period", "no disk data", "no boot times recorded"):
            self.assertIn(word, txt)
        self.assertIn("? no temperature data in this period", txt)                      # unknown is not "cool"
        self.assertIn("nothing to report in this period", txt)
        self.assertNotIn("✔", txt)
        self.seed(report(variant="little", change=lambda r: r.update(findings=[])))
        s, lines = self.screen([], 120, 33)
        self.assertIn("too little data to conclude anything yet", "\n".join(lines))      # less than a day without findings is not "all fine"

    def test_no_data_in_the_period(self):
        rep = report(change=lambda r: r.update(coverage={"hours": 0, "since": NOW - 40 * 86400}, notes=["no data in this period"]))
        self.seed(rep)
        s, lines = self.screen([], 120, 33)
        self.assertIn("no data in this period", "\n".join(lines))
        self.assertNotIn("── TOP CPU", "\n".join(lines))


class Advisor(HealthCase):
    """The hook the AI advisor plugs into: ADVICE under the findings, before the sections."""

    def test_the_hook_returns_nothing_by_default(self):
        self.assertEqual(render.health_extra_lines(report(), 120), [])
        s, lines = self.screen([], 120, 33)
        self.assertNotIn("ADVICE", "\n".join(lines))

    def test_advice_goes_under_the_findings_and_over_the_sections(self):
        seen = []

        def advice(rep, w):
            seen.append((rep["period"]["days"], w))
            return ["  AI, check before acting: restart shop-worker-1 with a memory limit", "x" * 500, ESC + "[2J"] + ["line"] * 10
        render.health_extra_lines = advice
        for cols, rows in SIZES:
            with self.subTest(size=(cols, rows)):
                s, lines = self.screen([], cols, rows)                                   # check_frame: still exact, and capped to w
                txt = "\n".join(lines)
                self.assertLess(txt.index("── FINDINGS"), txt.index("── ADVICE"))
                self.assertIn("AI, check before acting", txt)
                if rows >= 33:
                    self.assertLess(txt.index("── ADVICE"), txt.index("── TOP CPU"))
        self.assertEqual(seen[0], (7, 78))
        s, lines = self.screen(["--period", "30"], 120, 33)
        self.assertEqual(seen[-1][0], 30)

    def test_a_broken_hook_is_an_error_frame_not_a_crash(self):
        def broken(rep, w):
            raise RuntimeError("model unreachable")
        render.health_extra_lines = broken
        with self.assertRaises(RuntimeError):                                            # health_once lets it through: the main loop shows its error frame
            self.screen([], 120, 33)


class HelperFunctions(HealthCase):
    def test_hspark_scales_to_the_series_and_blanks_what_was_not_recorded(self):
        self.assertEqual(ansi.hspark([None, None, 0, 5, 10], 5), "  ▁▄█"[:2] + "▁▄█"[:3])
        self.assertEqual(ansi.hspark([None, None], 4), "    ")
        self.assertEqual(ansi.hspark([0, 0, 0], 3), "▁▁▁")
        self.assertEqual(len(ansi.hspark(list(range(40)), 10)), 10)                    # more values than room: the mean of each group
        self.assertEqual(ansi.hspark(["x", None, 1], 3), " " * 2 + "█")

    def test_hbucket_means(self):
        self.assertEqual(ansi.hbucket([1, 3, 5, 7], 2), [2.0, 6.0])
        self.assertEqual(ansi.hbucket([None, None, 4, 6], 2), [None, 5.0])
        self.assertEqual(ansi.hbucket([1, 2], 5), [1, 2])

    def test_hrows_never_hides_just_one(self):
        self.assertEqual(screens.hrows([1, 2, 3, 4, 5, 6], 5), ([1, 2, 3, 4, 5, 6], 0))
        self.assertEqual(screens.hrows([1, 2, 3, 4, 5, 6, 7], 5), ([1, 2, 3, 4], 3))
        self.assertEqual(screens.hrows([], 5), ([], 0))

    def test_counts_and_ages(self):
        self.assertEqual([ui.hcount(x) for x in (3, 999, 1200, 18420, 2500000, "x", None)], ["3", "999", "1.2k", "18k", "2.5M", "0", "0"])
        self.assertEqual([screens.hago(NOW - s, NOW) for s in (10, 600, 7200, 200000, 90000 * 5)], ["now", "10 min ago", "2 h ago", "2 d ago", "5 d ago"])
        self.assertEqual(screens.hwhen(NOW), "2026-09-21 14:13")
        self.assertEqual(screens.hwhen("x"), "?")
        self.assertEqual(screens.hwhen(10 ** 30), "?")

    def test_details_of_a_finding(self):
        f = finding("a:b", "err", "T", "X", "F", {"last": NOW, "n": 3, "r": 0.5, "t": 1.0, "s": "str", "b": True, "peak_hour": NOW - 7200})
        level, title, text, facts, fix = screens.health_details(f)
        self.assertEqual((level, title, text, fix), ("err", "T", "X", "F"))
        self.assertEqual(facts, [("last", "2026-09-21 14:13 UTC"), ("n", "3"), ("r", "0.5"), ("t", "1"), ("s", "str"), ("b", "yes"),
                                 ("peak_hour", "2026-09-21 12:13 UTC")])
        self.assertEqual(screens.health_details({"id": "x", "level": "weird"})[0], "info")


if __name__ == "__main__":
    unittest.main()
