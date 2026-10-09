"""The console MAP (render.py): the keys on the tree, `--view map` and its options, every frame inside its screen, the Map
among the rotating pages, the feature switch, hostile names and missing data.

Hermetic: the demo data at a clock that stands still (render, graph and demo read the time through it), a fake terminal for
the main loop (no TTY), no host state (no Sampler, no accepted.json, a fixed host name), on Linux, macOS and Windows alike.
Every module global a test changes is put back in tearDown.
"""
import contextlib
import io
import os
import re
import shutil
import signal
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the test helpers (cardlines.py)
import cardlines  # noqa: E402
import demo  # noqa: E402
import graph  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import ansi  # noqa: E402

NOW = 1_790_000_000
WEBAPPS = {"shop-web": [8080], "admin-console": [9443]}  # what demo_defaults() declares: the demo's [webapps]
SIZES = ((79, 24), (120, 33), (200, 50), (226, 50))       # console sizes (--cols --rows): the layout gets cols - 1, as in once()
OSES = (None, "windows", "darwin")
ESC, BEL, CSI8 = chr(27), chr(7), chr(0x9b)               # built at runtime: the file itself stays plain text
SGR = re.compile(r"\x1b\[[0-9;]*m")                        # the only escape sequences the renderer puts inside a line
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
LINE = "\x1b[K\r\n"                                        # frame(): every line but the last ends with erase-to-end + CRLF
RENDER_GLOBALS = ("DEMO", "DEMO_OS", "MODE", "WINDOWS", "ACCEPTED_PATH", "time", "os", "sys", "signal", "shutil", "socket",
                  "termios", "tty", "graph", "Sampler", "read_keys", "snapshot", "page_overview", "map_graph", "map_slide", "KPI_MIN_ROWS")


class Proxy(object):
    """A module as render (or graph, demo) sees it, with a few attributes replaced: the real module is never touched."""

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


def demo_graph(os_name=None, change=None):
    """The MAP graph of the demo at NOW, as tests/test_graph.py builds it."""
    cont, net, boot, base = demo.snapshot(now=NOW, os_name=os_name)
    if change:
        change(cont, net, boot)
    return graph.build(cont, net, boot, WEBAPPS, now=NOW, baseline=base)


def label(G, rs, mv):
    return G["nodes"][rs[mv.idx]["node"]]["label"]


def pos(lines):
    """(cursor row, rows) from the Map's footer: ' row 3/21 ...' or ' 3/21  ...' when narrow."""
    m = re.search(r"(\d+)/(\d+)", lines[-1])
    return int(m.group(1)), int(m.group(2))


def chosen(screen):
    """The highlighted (reverse video) lines of a frame, ANSI stripped: the cursor's row."""
    return [ansi.ANSI.sub("", x) for x in screen.split(LINE)[1:] if ESC + "[7m" in x]


def worker_up(cont, net, boot):
    for x in net["links"]["containers"]:
        if x["name"] == "worker-1":
            x.update(state="running", exit=None, restarts=0)
    for x in cont["containers"]:
        if x["name"] == "worker-1":
            x.update(state="running", status="Up 1 minute")


class MapCase(unittest.TestCase):
    """The demo at NOW, the map feature on and out of the rotation, nothing read from the host; all of it undone after."""

    def setUp(self):
        self.saved = {k: getattr(render, k) for k in RENDER_GLOBALS}
        self.saved_time = (graph.time, demo.time)
        cfg = render.CFG
        self.saved_cfg = (dict(cfg["features"]), cfg["webapps"], cfg["map_in_rotation"])
        self.saved_expose = cfg["expose"]
        self.tmp = tempfile.TemporaryDirectory()
        self.now = NOW
        self.on_sleep = self.pass_time
        clock = Proxy(time, time=lambda: self.now, sleep=lambda sec: self.on_sleep(sec),
                      strftime=lambda fmt, t=None: time.strftime(fmt, time.gmtime(self.now) if t is None else t))
        render.time = graph.time = demo.time = clock
        render.socket = Proxy(render.socket, gethostname=lambda: "test-host")  # demo_defaults() renames it on the proxy only
        render.ACCEPTED_PATH = os.path.join(self.tmp.name, "accepted.json")     # missing: nothing accepted, whatever the host has
        render.KPI_MIN_ROWS = 10 ** 6                                           # the KPI line is tested in test_console_ui.py
        render.DEMO, render.DEMO_OS = True, None
        cfg["features"]["map"], cfg["map_in_rotation"] = True, False

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(render, k, v)
        graph.time, demo.time = self.saved_time
        features, render.CFG["webapps"], render.CFG["map_in_rotation"] = self.saved_cfg
        render.CFG["expose"] = self.saved_expose
        render.CFG["features"].clear()
        render.CFG["features"].update(features)
        self.tmp.cleanup()

    def pass_time(self, sec):
        self.now += sec

    def feed(self, change=None, data=None):
        """render.snapshot() returns the demo changed by change(cont, net, boot), or data = (cont, net, boot) as it is."""
        def snapshot(w):
            if data is not None:
                return dict(cont=data[0], net=data[1], boot=data[2], baseline=None)
            cont, net, boot, base = demo.snapshot(now=self.now, os_name=render.DEMO_OS)
            if change:
                change(cont, net, boot)
            return dict(cont=cont, net=net, boot=boot, baseline=base)
        render.snapshot = snapshot

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
        """`render.py --once --view map OPTS --cols cols --rows rows`, without printing: (frame, its lines ANSI stripped)."""
        s = render.map_once(["render.py", "--once", "--view", "map"] + list(opts), cols - 1, rows)
        return s, self.check_frame(s, cols - 1, rows, (opts, cols, rows, render.DEMO_OS))

    def press(self, G, mv, keys, page=10):
        """Keys as the console loop hands them to map_key: the rows rebuilt and the cursor synced after 'rows'.
        -> (the rows afterwards, what map_key returned for each key). After every key the cursor is on one of the rows."""
        rs = graph.rows(G, mv.st)
        screens.map_sync(mv, rs)
        acts = []
        for k in keys:
            act = screens.map_key(mv, k, rs, page)
            if act == "rows":
                rs = graph.rows(G, mv.st)
                screens.map_sync(mv, rs)
            acts.append(act)
            if rs:
                self.assertTrue(0 <= mv.idx < len(rs), (k, mv.idx, len(rs)))
                self.assertEqual(mv.cur, rs[mv.idx]["key"], k)
            else:
                self.assertEqual((mv.idx, mv.cur), (0, None), k)
        return rs, acts

    def put(self, G, mv, name, depth=None):
        """The cursor on the first row on screen named name (at that depth). -> the rows."""
        rs = graph.rows(G, mv.st)
        mv.cur = next(r["key"] for r in rs if G["nodes"][r["node"]]["label"] == name and depth in (None, r["depth"]))
        screens.map_sync(mv, rs)
        return rs


class Navigation(MapCase):
    """map_key on the demo tree: what each key does to the cursor, the open branches and the details pane."""

    def test_up_and_down_move_and_stop_at_the_ends(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        n = len(graph.rows(G))
        rs, acts = self.press(G, mv, ["up", "k"])
        self.assertEqual((mv.idx, acts), (0, ["", ""]))                              # the first row: up stays
        rs, acts = self.press(G, mv, ["down", "j", "down"])
        self.assertEqual((mv.idx, label(G, rs, mv), acts), (3, ":5432/tcp", ["", "", ""]))
        rs, _ = self.press(G, mv, ["down"] * (n + 5))
        self.assertEqual((mv.idx, label(G, rs, mv)), (n - 1, "shop-api-1"))         # the last row (OUTBOUND's): down stays
        rs, _ = self.press(G, mv, ["up"])
        self.assertEqual(mv.idx, n - 2)
        self.assertEqual(len(rs), n)                                                 # moving opens nothing
        self.assertEqual((mv.st.open, mv.st.shut, mv.st.all), (set(), set(), False))

    def test_right_opens_a_branch_then_enters_it_left_closes_it_then_goes_up(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        rs = self.put(G, mv, ":8080/tcp")
        n, key = len(rs), mv.cur
        self.assertEqual((rs[mv.idx]["open"], rs[mv.idx]["kids"]), (False, 4))
        rs, acts = self.press(G, mv, ["right"])
        self.assertEqual((acts, mv.cur, rs[mv.idx]["open"], len(rs)), (["rows"], key, True, n + 4))  # opened, the cursor stays
        rs, acts = self.press(G, mv, ["right"])
        self.assertEqual((acts, rs[mv.idx]["depth"], label(G, rs, mv)), ([""], 2, "100.64.0.2"))     # open: down to its first child
        rs, acts = self.press(G, mv, ["l"])
        self.assertEqual((acts, label(G, rs, mv)), ([""], "100.64.0.2"))                             # a leaf: right stays
        rs, acts = self.press(G, mv, ["left"])
        self.assertEqual((acts, mv.cur), ([""], key))                                                # a leaf: left goes to its parent
        rs, acts = self.press(G, mv, ["h"])
        self.assertEqual((acts, mv.cur, rs[mv.idx]["open"], len(rs)), (["rows"], key, False, n))     # open: left closes it
        rs, acts = self.press(G, mv, ["left"])
        self.assertEqual((acts, label(G, rs, mv), rs[mv.idx]["depth"]), ([""], "LAN", 0))            # closed: up to its parent
        rs, acts = self.press(G, mv, ["right"])
        self.assertEqual((acts, label(G, rs, mv)), ([""], ":5432/tcp"))                              # an open root: into it

    def test_a_root_closes_and_reopens_and_a_closed_root_has_no_parent(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        rs = self.put(G, mv, "LAN")
        n, key, kids = len(rs), mv.cur, rs[mv.idx]["kids"]
        rs, acts = self.press(G, mv, ["left"])
        self.assertEqual((acts, mv.cur, rs[mv.idx]["open"], len(rs)), (["rows"], key, False, n - kids))
        self.assertIn(key, mv.st.shut)                                                 # roots are open unless shut
        rs, acts = self.press(G, mv, ["left", "h"])
        self.assertEqual((acts, mv.cur), (["", ""], key))                              # nothing above a root
        rs, acts = self.press(G, mv, ["right"])
        self.assertEqual((acts, mv.cur, rs[mv.idx]["open"], len(rs)), (["rows"], key, True, n))
        rs = self.put(G, mv, ":5433/tcp")                                              # a leaf (nothing behind it)
        self.assertEqual(rs[mv.idx]["kids"], 0)
        rs, acts = self.press(G, mv, ["right"])
        self.assertEqual((acts, label(G, rs, mv)), ([""], ":5433/tcp"))
        rs, acts = self.press(G, mv, ["left"])
        self.assertEqual(label(G, rs, mv), "LOCAL")

    def test_e_expands_everything_c_collapses_back_to_the_roots(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        default = [r["key"] for r in graph.rows(G)]
        rs = self.put(G, mv, ":8080/tcp")
        key = mv.cur
        rs, acts = self.press(G, mv, ["e"])
        self.assertEqual((acts, mv.cur, mv.st.all), (["rows"], key, True))
        self.assertEqual([r["key"] for r in rs], [r["key"] for r in graph.rows(G, graph.State(all=True))])
        self.assertFalse([r for r in rs if r["kids"] and not r["open"]])               # no closed branch left
        deep = next(i for i, r in enumerate(rs) if r["depth"] == 3)
        rs, _ = self.press(G, mv, ["down"] * (deep - mv.idx))
        self.assertEqual(rs[mv.idx]["depth"], 3)
        root = next(r for r in reversed(rs[:mv.idx + 1]) if r["depth"] == 0)
        rs, acts = self.press(G, mv, ["c"])
        self.assertEqual((acts, mv.cur, mv.st.all, mv.st.open, mv.st.shut), (["rows"], root["key"], False, set(), set()))
        self.assertEqual([r["key"] for r in rs], default)                               # the roots, open, as when the Map opens
        self.assertEqual(label(G, rs, mv), "LAN")

    def test_p_shows_problems_only_and_the_cursor_stays_on_a_row(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        only = [r["key"] for r in graph.rows(G, graph.State(only=True))]
        rs = self.put(G, mv, "LOCAL")                                                  # nothing wrong under it: not a problem path
        self.assertNotIn(mv.cur, only)
        rs, acts = self.press(G, mv, ["p"])                                            # press() checks the cursor is on a row
        self.assertEqual((acts, mv.st.only, [r["key"] for r in rs]), (["rows"], True, only))
        rs = self.put(G, mv, "worker-1")                                               # on both views: it keeps its row
        key = mv.cur
        rs, acts = self.press(G, mv, ["p"])
        self.assertEqual((acts, mv.st.only, mv.cur, label(G, rs, mv)), (["rows"], False, key, "worker-1"))
        rs, _ = self.press(G, mv, ["end", "p"])                                        # the last row of a longer list: clamped
        self.assertEqual((len(rs), mv.idx), (len(only), len(only) - 1))
        rs, _ = self.press(G, mv, ["e", "p", "p"])                                     # with everything open too
        self.assertTrue(mv.st.all)

    def test_enter_and_space_toggle_the_details_pane_of_the_selected_row(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        rs, acts = self.press(G, mv, ["enter"])
        self.assertEqual((acts, mv.details), ([""], True))
        for name, kind in (("INTERNET", "view"), (":8444/tcp", "entry port"), ("admin-console", "web app"), ("shop", "compose project")):
            self.put(G, mv, name)
            txt = "\n".join(self.check_frame(render.map_screen(G, [], mv, 119, 33)[0], 119, 33, name))
            self.assertIn("── DETAILS", txt)
            self.assertIn(kind + " ", txt)                                             # the pane follows the cursor
        rs, acts = self.press(G, mv, ["space"])
        self.assertEqual((acts, mv.details), ([""], False))
        self.assertNotIn("DETAILS", ansi.ANSI.sub("", render.map_screen(G, [], mv, 119, 33)[0]))

    def test_page_keys_move_a_page_and_stop_at_the_ends(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        n = len(graph.rows(G))
        seen = []
        for k in ("pgdn", "pgdn", "pgup", "end", "pgdn", "pgup", "home", "pgup"):
            self.press(G, mv, [k], page=5)
            seen.append(mv.idx)
        self.assertEqual(seen, [5, 10, 5, n - 1, n - 1, n - 6, 0, 0])

    def test_esc_q_leave_the_map_and_change_nothing(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        self.press(G, mv, ["down", "right"])
        before = (mv.idx, mv.cur, mv.details, set(mv.st.open), mv.st.all, mv.st.only)
        for k in ("esc", "q"):
            rs, acts = self.press(G, mv, [k])
            self.assertEqual(acts, ["back"], k)
            self.assertEqual((mv.idx, mv.cur, mv.details, mv.st.open, mv.st.all, mv.st.only), before, k)
        for k in ("m", "tab", "btab", "1", "2", "?", "r", "Z"):  # the keys of every screen are the dispatcher's: the Map does nothing
            rs, acts = self.press(G, mv, [k])
            self.assertEqual(acts, [""], k)
            self.assertEqual((mv.idx, mv.cur, mv.details, mv.st.open, mv.st.all, mv.st.only), before, k)

    def test_esc_closes_the_details_pane_before_it_goes_back(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        self.press(G, mv, ["down", "enter"])
        self.assertTrue(mv.details)
        self.assertEqual(self.press(G, mv, ["esc", "esc"])[1], ["", "back"])
        self.press(G, mv, ["enter"])
        self.assertEqual(self.press(G, mv, ["q", "q"])[1], ["", "back"])

    def test_other_keys_do_nothing_and_no_rows_is_not_an_error(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        self.press(G, mv, ["down", "down"])
        before = (mv.idx, mv.cur, set(mv.st.open))
        rs, acts = self.press(G, mv, ["x", "1", "", "f1", "PGDN"])                       # key names are exact: PGDN is no pgdn
        self.assertEqual((acts, (mv.idx, mv.cur, mv.st.open)), ([""] * 5, before))
        empty = graph.build(None, None)
        mv = screens.MapView(now=NOW)
        keys = ["up", "down", "left", "right", "h", "l", "pgup", "pgdn", "home", "end", "c", "x"]
        rs, acts = self.press(empty, mv, keys)                                         # press() checks: no cursor, index 0
        self.assertEqual((rs, acts), ([], [""] * len(keys)))
        self.assertEqual(self.press(empty, mv, ["e", "p", "enter", "esc", "esc"])[1], ["rows", "rows", "", "", "back"])

    def test_the_cursor_follows_its_row_through_a_refresh(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        self.put(G, mv, ":8080/tcp")
        rs, _ = self.press(G, mv, ["right", "right", "down", "right", "right"])       # LAN > :8080/tcp > shop-api-1 > shop-db-1
        self.assertEqual((label(G, rs, mv), rs[mv.idx]["depth"]), ("shop-db-1", 3))
        key, idx = mv.cur, mv.idx
        G2 = demo_graph()                                                              # the next refresh: a new graph, same data
        self.assertIsNot(G2, G)
        rs2 = graph.rows(G2, mv.st)
        self.assertEqual(screens.map_sync(mv, rs2), idx)
        self.assertEqual((mv.cur, label(G2, rs2, mv)), (key, "shop-db-1"))
        rs = self.put(G, mv, "STACKS")                                                 # rows appear above it: it moves with them
        key, idx = mv.cur, mv.idx

        def failed_unit(cont, net, boot):
            boot.update(failed=["nfs-server.service"], deps={})
        G3 = demo_graph(change=failed_unit)
        rs3 = graph.rows(G3, mv.st)
        self.assertEqual(screens.map_sync(mv, rs3), idx + 1)
        self.assertEqual((mv.cur, label(G3, rs3, mv)), (key, "STACKS"))

    def test_the_cursor_stays_in_place_when_its_row_disappears(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        rs = self.put(G, mv, "worker-1", depth=1)                                      # IMPACT > worker-1 (exited)
        idx, gone = mv.idx, mv.cur
        G2 = demo_graph(change=worker_up)                                              # it runs again: out of IMPACT
        rs2 = graph.rows(G2, mv.st)
        self.assertIsNone(graph.find(rs2, gone))
        self.assertEqual(screens.map_sync(mv, rs2), idx)                                # the same place on the screen
        self.assertEqual((mv.cur, label(G2, rs2, mv)), (rs2[idx]["key"], "STACKS"))
        rs = self.put(G, mv, "shop-api-1", depth=1)                                    # the last row (OUTBOUND)

        def quiet(cont, net, boot):
            net["links"]["conns"] = [e for e in net["links"]["conns"] if e["from"] != "ct:shop-api-1" or not e["to"].startswith("ext:")]
        G3 = demo_graph(change=quiet)
        rs3 = graph.rows(G3, mv.st)
        self.assertEqual(len(rs3), len(rs) - 1)
        self.assertEqual(screens.map_sync(mv, rs3), len(rs3) - 1)                       # clamped to the new last row
        self.assertEqual(label(G3, rs3, mv), "node")
        self.assertEqual((screens.map_sync(mv, []), mv.cur), (0, None))                 # everything gone
        self.assertEqual((screens.map_sync(mv, rs3), mv.cur), (0, rs3[0]["key"]))       # and back

    def test_the_selected_row_is_always_on_screen(self):
        G, mv = demo_graph(), screens.MapView(now=NOW)
        rs, _ = self.press(G, mv, ["e"])
        w, h = 119, 24
        tree = screens.map_layout(G, w, h - 2)[1]
        self.assertLess(tree, len(rs))                                                 # it has to scroll
        for k in ["down"] * len(rs) + ["pgup", "up", "up"] + ["pgup"] * 9 + ["end", "home"]:
            rs, _ = self.press(G, mv, [k], page=tree - 1)
            s = render.map_screen(G, [], mv, w, h)[0]
            self.check_frame(s, w, h, k)
            sel = chosen(s)
            self.assertEqual(len(sel), 1, (k, mv.idx))
            self.assertIn(graph.safe(label(G, rs, mv)), sel[0], (k, mv.idx))
            self.assertTrue(mv.top <= mv.idx < mv.top + tree and 0 <= mv.top <= len(rs) - tree, (k, mv.idx, mv.top))
        self.assertEqual((mv.idx, mv.top), (0, 0))

    def test_map_scroll_keeps_the_row_in_sight_away_from_the_edges(self):
        for n, rows in ((54, 20), (10, 20), (21, 21), (54, 5), (54, 2), (54, 1)):
            m, top = min(2, max(0, (rows - 1) // 2)), 0
            for i in list(range(n)) + list(range(n - 1, -1, -1)) + [n - 1, 0, n // 2, 3, n - 4]:
                top = ansi.scroll(top, i, n, rows)
                self.assertTrue(top <= i < top + rows, (n, rows, i, top))
                self.assertTrue(0 <= top <= max(0, n - rows), (n, rows, i, top))
                if m <= i < n - m:
                    self.assertTrue(i - top >= m and top + rows - 1 - i >= m, (n, rows, i, top))


class Once(MapCase):
    """map_once: `render.py --once --view map` with --expand, --select, --details, --only, as the README screenshot uses it."""

    OPTIONS = ([], ["--expand", "none"], ["--expand", "all"], ["--expand", "fit"], ["--expand", "12"], ["--expand", "0"],
               ["--expand", "all", "--details"], ["--expand", "fit", "--select", "shop-api", "--details"],
               ["--select", "198.51.100.25", "--details"], ["--only"], ["--only", "--expand", "all", "--details", "--select", "worker"],
               ["--select", "no such row", "--details"], ["--expand", "bogus"], ["--expand"], ["--select"])

    def test_every_frame_fills_its_screen_and_nothing_more(self):
        for os_name in OSES:
            render.DEMO_OS = os_name
            for cols, rows in SIZES:
                for opts in self.OPTIONS:
                    with self.subTest(os=os_name, size=(cols, rows), opts=opts):
                        s, lines = self.screen(opts, cols, rows)
                        self.assertIn("── MAP ", lines[1])
                        self.assertRegex(lines[0], r"^ *demo-host │ .*\[\d[ ·]Map\]")
                        self.assertTrue(pos(lines)[0] >= 1)

    def test_expand_none_all_fit_and_a_row_count(self):
        cols, rows = 200, 50
        G = render.map_graph()[0]
        count = lambda st: len(graph.rows(G, st))  # noqa: E731
        tree = screens.map_layout(G, cols - 1, rows - 2)[1]
        fit = graph.rows(G, graph.State(open=graph.fit_open(G, tree)))
        want = {"none": count(graph.State()), "all": count(graph.State(all=True)), "fit": len(fit),
                "30": count(graph.State(open=graph.fit_open(G, 30))), "bogus": count(graph.State())}
        for opt, n in want.items():
            self.assertEqual(pos(self.screen(["--expand", opt], cols, rows)[1]), (1, n), opt)
        self.assertEqual(pos(self.screen([], cols, rows)[1]), (1, want["none"]))      # no option: only the roots open, as `m` does
        self.assertTrue(want["none"] < want["fit"] <= tree < want["all"], want)
        self.assertTrue(want["none"] < want["30"] <= 30, want)
        lines = self.screen(["--expand", "fit"], cols, rows)[1]
        drawn = [x for x in lines[2:-1] if x.strip()]
        self.assertIn(G["nodes"][fit[-1]["node"]]["label"], drawn[-1])                # fit: down to the last row, nothing scrolled away
        self.assertNotIn("more rows", "\n".join(lines))

    def test_select_opens_the_branches_above_the_first_match(self):
        G = render.map_graph()[0]
        mv = screens.MapView(now=NOW)
        self.assertTrue(screens.map_select(G, mv, "198.51.100.25"))                    # not on screen: found in the whole tree
        everything = graph.rows(G, graph.State(all=True))
        first = next(r for r in everything if G["nodes"][r["node"]]["label"] == "198.51.100.25")
        rs = graph.rows(G, mv.st)
        i = graph.find(rs, mv.cur)
        self.assertEqual((mv.cur, rs[i]["depth"]), (first["key"], 3))
        above, d = [], 3
        for r in reversed(rs[:i]):
            if r["depth"] < d:
                d = r["depth"]
                above.append(r)
        self.assertEqual([G["nodes"][r["node"]]["label"] for r in above], ["shop-api-1", ":8080/tcp", "LAN"])
        self.assertTrue(all(r["open"] for r in above))
        self.assertEqual(mv.st.open, {r["key"] for r in above if r["depth"]})         # those branches, and nothing else
        for cols, rows in SIZES:
            s, lines = self.screen(["--select", "198.51.100.25"], cols, rows)
            self.assertEqual(pos(lines), (i + 1, len(rs)), cols)
            self.assertIn("198.51.100.25", chosen(s)[0])                                # highlighted and scrolled into sight

    def test_select_any_case_by_owner_and_what_is_in_sight_first(self):
        G = render.map_graph()[0]

        def sel(text, st=None):
            mv = screens.MapView(now=NOW)
            mv.st = st or mv.st
            found = screens.map_select(G, mv, text)
            rs = graph.rows(G, mv.st)
            i = graph.find(rs, mv.cur) if found else None
            return found, (G["nodes"][rs[i]["node"]]["label"], rs[i]["depth"]) if found else None, mv
        self.assertEqual(sel("SSHD")[1], (":22/tcp", 1))                                # a port row is found by the process behind it
        found, where, mv = sel("shop-api")
        self.assertEqual((where, mv.st.open), (("shop-api-1", 1), set()))              # already on screen (OUTBOUND): nothing opened
        self.assertEqual(sel("shop-api", graph.State(all=True))[1], ("shop-api-1", 2))  # all open: the first one, under :8080/tcp
        found, where, mv = sel("no such row")
        self.assertEqual((found, mv.cur, mv.st.open), (False, None, set()))
        self.assertEqual(pos(self.screen(["--select", "no such row"], 120, 33)[1])[0], 1)  # the cursor on the first row

    def test_details_pane_beside_or_below_the_tree(self):
        G = render.map_graph()[0]
        first = graph.details(G, "port:5432/tcp@lan")[0][0]                            # --select shop-db-1: its entry port, by owner
        self.assertEqual(first, "entry port")
        for cols, rows in SIZES:
            w = cols - 1
            s, lines = self.screen(["--select", "shop-db-1", "--details"], cols, rows)
            at = next(i for i, x in enumerate(lines) if "── DETAILS" in x)
            side = w >= screens.MAP_PANE_W
            if side:
                tw = w - (int(w * 0.42) + 3)
                pane = [x[tw + 3:] for x in lines[at:-1]]
                self.assertEqual(lines[at].index("── DETAILS"), tw + 3, cols)          # beside the tree, after ' │ '
                self.assertTrue(all(x[tw:tw + 3] == " │ " for x in lines[at:-1]), cols)
            else:
                pane = lines[at:-1]
                self.assertTrue(lines[at].startswith("── DETAILS"), cols)              # below the tree
                self.assertGreater(at, 3)
            txt = "\n".join(pane)
            self.assertIn("entry port", txt, cols)
            self.assertIn(":5432/tcp", txt, cols)
            if rows >= 50:                                                             # (the demo's [expose] adds two lines to this pane)
                self.assertIn("postgres:16", txt, cols)                                # what is behind it: the database's image
            self.assertIn(":5432/tcp", chosen(s)[0], cols)
            self.assertNotIn("DETAILS", "\n".join(self.screen(["--select", "shop-db-1"], cols, rows)[1]))

    def test_only_shows_the_paths_that_lead_to_a_problem(self):
        G = render.map_graph()[0]
        only = graph.rows(G, graph.State(only=True))
        for cols, rows in SIZES:
            s, lines = self.screen(["--only"], cols, rows)
            self.assertEqual(pos(lines), (1, len(only)), cols)
            txt = "\n".join(lines)
            self.assertIn("problems only", lines[1])
            self.assertIn("p: all", lines[-1])                                         # the key now brings every path back
            self.assertNotIn("LOCAL", txt)
            for r in only:
                self.assertIn(G["nodes"][r["node"]]["label"], txt)

    def test_the_command_line_prints_the_map_for_each_demo_os(self):
        for os_name, note in (("windows", "Docker Desktop"), ("darwin", "Docker Desktop"), ("", "connections sampled")):
            argv = ["render.py", "--once", "--demo", "--view", "map", "--cols", "79", "--rows", "24", "--expand", "all"]
            argv += ["--demo-os", os_name] if os_name else []
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertIsNone(render.once(argv))
            self.assertEqual(render.DEMO_OS, os_name or None)
            text = out.getvalue()
            self.assertTrue(text.endswith("\n"))
            lines = text[:-1].split("\r\n")
            self.assertEqual(len(lines), 24, os_name)
            self.assertFalse([x for x in lines if len(x) > 78], os_name)
            self.assertNotIn(ESC, text)                                                # without --color: plain text
            self.assertIn(note, text, os_name)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render.once(["render.py", "--once", "--demo", "--view", "map", "--color", "--cols", "120", "--rows", "33"])
        self.assertIn(ESC + "[", out.getvalue())                                       # --color keeps them (tools/ansi2svg.py)

    def test_title_and_footer_fit_any_width(self):
        G = demo_graph()
        mv = screens.MapView(now=NOW)
        self.press(G, mv, ["down"] * 4)
        for w in range(20, 260, 3):
            for only in (False, True):
                t = ansi.ANSI.sub("", cardlines.map_title(G, w, only))
                self.assertLessEqual(len(t), w, (w, t))
                self.assertTrue(t.startswith("── MAP "), t)
            for details, truncated in ((False, False), (True, True)):
                mv.details = details
                f = ansi.ANSI.sub("", render.map_footer(mv, 21, w, truncated))
                self.assertLessEqual(len(f), w, (w, f))
                if w >= 12:
                    self.assertIn("5/21" + ("+" if truncated else ""), f, w)
        wide = ansi.ANSI.sub("", render.map_footer(mv, 21, 200))
        self.assertIn("row 5/21", wide)
        for k in ("PgUp/PgDn/Home/End: page", "e/c: expand/collapse all", "Esc: back", "?: help", "1-5: screens", "Enter: hide details"):
            self.assertIn(k, wide)
        narrow = ansi.ANSI.sub("", render.map_footer(mv, 21, 78))
        self.assertIn("Esc: back", narrow)                                                # the way out is the last key to go
        self.assertNotIn("PgUp", narrow)
        self.assertIn("━━► seen", ansi.ANSI.sub("", cardlines.map_title(G, 200)))       # the legend when there is room
        self.assertIn("row 0/0", ansi.ANSI.sub("", render.map_footer(mv, 0, 200)))  # no rows at all


class Rotation(MapCase):
    """[dashboard] map_in_rotation: the Map as one of the rotating pages (a monitor nobody types on)."""

    def setUp(self):
        super().setUp()
        render.page_overview = overview_stub                                           # the overview reads the host: not the subject here
        self.cont, self.net, self.boot, self.base = demo.snapshot(now=NOW)

    def slides(self, w, body_h, **kw):
        return render.slides(FakeSampler().sample(), self.cont, self.net, w, body_h, self.boot, self.base, mode="overview", **kw)

    def test_the_map_joins_the_rotation_when_asked(self):
        self.assertEqual([x[0] for x in self.slides(119, 31)], ["Overview"])
        render.CFG["map_in_rotation"] = True
        for cols, rows in SIZES:
            w, body_h = cols - 1, rows - 2
            sl = self.slides(w, body_h)
            self.assertEqual([x[0] for x in sl], ["Overview", "Map"], cols)
            name, part, parts, body = sl[-1]
            self.assertEqual((part, parts), (1, 1))
            self.assertLessEqual(len(body), body_h)
            self.assertFalse([x for x in body if len(ansi.ANSI.sub("", x)) > w], cols)
            self.assertFalse([x for x in body if ESC + "[7m" in x], cols)               # no cursor on a monitor without a keyboard
            txt = "\n".join(ansi.ANSI.sub("", x) for x in body)
            self.assertIn("── MAP ", txt)
            self.assertNotIn("more rows", txt)                                         # opened level by level while it fits
            G = graph.build(self.cont, self.net, self.boot, render.CFG["webapps"], now=NOW, baseline=self.base)
            fit = graph.rows(G, graph.State(open=graph.fit_open(G, screens.map_layout(G, w, body_h)[1])))
            self.assertIn(G["nodes"][fit[-1]["node"]]["label"], ansi.ANSI.sub("", body[-1]))  # down to the last row it opened
            frame = render.frame(sl[-1], 1, len(sl), w, rows, [], keys=False)
            self.check_frame(frame, w, rows, cols)
        sl = self.slides(119, 31)
        self.assertEqual(render.pick_slide(sl, sum(render.slide_seconds(x, len(sl)) for x in sl) - 1), 1)  # its turn comes
        self.assertEqual([x[0] for x in self.slides(119, 31, scroll=True)], ["Overview"])  # the scrolling web page: one page only

    def test_a_screen_too_small_for_the_roots_counts_what_it_cuts(self):
        render.CFG["map_in_rotation"] = True
        body = self.slides(78, 8)[-1][3]
        self.assertLessEqual(len(body), 8)
        self.assertRegex(ansi.ANSI.sub("", body[-1]), r"… \+\d+ more rows")

    def test_no_map_slide_when_off_or_when_the_feature_is_disabled(self):
        render.CFG["map_in_rotation"] = False
        self.assertNotIn("Map", [x[0] for x in self.slides(199, 48)])
        render.CFG["map_in_rotation"] = True
        render.CFG["features"]["map"] = False
        self.assertNotIn("Map", [x[0] for x in self.slides(199, 48)])
        frame = render.frame(self.slides(199, 48)[0], 0, 1, 199, 50, [], keys=True)
        self.assertNotIn("1-5: screens", ansi.ANSI.sub("", frame))                   # and the footer does not offer 2
        self.assertIn("1 3-5: screens", ansi.ANSI.sub("", frame))

    def test_a_broken_map_slide_is_an_error_line_not_a_crash(self):
        render.CFG["map_in_rotation"] = True

        def broken(*a, **kw):
            raise ValueError("bad state " + ESC + "[2J")
        render.map_slide = broken
        sl = self.slides(119, 31)
        self.assertEqual(sl[-1][0], "Map")
        line = sl[-1][3][0]
        self.assertIn("error on page Map", ansi.ANSI.sub("", line))
        self.assertNotIn(ESC + "[2J", line)

    def test_config_map_in_rotation_yes_no_and_not_a_boolean(self):
        path = os.path.join(self.tmp.name, "config.ini")
        got = {}
        for value in ("yes", "no", "on", "maybe"):
            with open(path, "w", encoding="utf-8") as f:
                f.write("[dashboard]\nmap_in_rotation = " + value + "\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                got[value] = (nuc_config.load(path)["map_in_rotation"], err.getvalue())
        self.assertEqual((got["yes"], got["no"], got["on"]), ((True, ""), (False, ""), (True, "")))
        self.assertFalse(got["maybe"][0])                                              # the default (off) stays
        self.assertIn("[dashboard] map_in_rotation is not a boolean: kept off", got["maybe"][1])
        self.assertFalse(nuc_config.load(os.path.join(self.tmp.name, "missing.ini"))["map_in_rotation"])
        with open(path, "w", encoding="utf-8") as f:
            f.write("[dashboard]\nmap_in_rotation = yes\n[features]\nmap = no\n")
        cfg = nuc_config.load(path)
        self.assertEqual((cfg["map_in_rotation"], cfg["features"]["map"]), (True, False))


class MainLoop(MapCase):
    """render.main() on a fake terminal: `m` opens the Map, its keys move it, 1/Esc go back, an idle Map gives the
    monitor back. KEY_CHAR & co. decode the raw reads, as on a real console."""

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
        self.on_sleep = lambda sec: next_read()                                        # no keyboard: the loop sleeps between frames
        with self.assertRaises(Stop):
            render.main(["render.py"])
        text = out.getvalue()
        self.assertTrue(text.endswith("\x1b[?25h\x1b[0m"))                             # the cursor is given back
        frames = []
        for chunk in text[:-len("\x1b[?25h\x1b[0m")].split("\x1b[H")[1:]:
            chunk = chunk[:-len("\x1b[2J")] if chunk.endswith("\x1b[2J") else chunk  # cleared when the screen changes
            frames.append(self.check_frame(chunk, cols - 1, rows, len(frames)))
        return frames, restored

    @staticmethod
    def is_map(frame):
        return bool(re.search(r"\[\d[ ·]Map\]", frame[0]) and "── MAP " in frame[1])

    def test_m_opens_the_map_keys_move_it_tab_and_esc_go_back(self):
        G = render.map_graph()[0]
        n0 = len(graph.rows(G))
        page = max(1, screens.map_layout(G, 79, 22, True)[1] - 1)                        # PgDn: a tree page minus one, details open
        frames, restored = self.run_main([b"m", b"\x1b[B\x1bOB", b"\x1b[C\x1b[C\x1b[B", b"\r", b"\x1b[6~", b"\x1b[H", b"1",
                                          b"2", b"\x1b"])
        self.assertEqual([self.is_map(f) for f in frames], [False, True, True, True, True, True, True, False, True, False])
        self.assertIn("1-5: screens", frames[0][-1])                                   # a keyboard: the footer offers the screens (2 is the Map)
        self.assertIn("overview page (stub)", "\n".join(frames[0]))
        self.assertEqual(pos(frames[1]), (1, n0))                                      # opened like `--expand none`
        self.assertEqual(pos(frames[2]), (3, n0))                                      # ↓ (CSI) ↓ (SS3): LAN
        self.assertEqual(pos(frames[3]), (5, n0 + 1))                                  # → into it, → opens :5432/tcp, ↓ onto its client
        self.assertNotIn("DETAILS", "\n".join(frames[3]))
        self.assertIn("── DETAILS", "\n".join(frames[4]))                              # Enter
        self.assertEqual(pos(frames[5]), (5 + page, n0 + 1))                           # PgDn
        self.assertEqual(pos(frames[6]), (1, n0 + 1))                                  # Home
        self.assertEqual(pos(frames[8]), (1, n0))                                      # 1 back, 2 again: a new Map
        self.assertNotIn("DETAILS", "\n".join(frames[8]))
        self.assertEqual(restored, [["attrs", 5]])                                     # the terminal's settings are put back

    def test_an_idle_map_gives_the_monitor_back_to_the_rotation(self):
        idle = screens.MAP_IDLE_S
        frames, _ = self.run_main([b"m", idle - 1, b"\x1b[B", idle - 1, 2])
        self.assertEqual([self.is_map(f) for f in frames], [False, True, True, True, True, False])
        self.assertEqual(pos(frames[4]), (2, pos(frames[1])[1]))                       # a key restarts the count

    def test_without_a_keyboard_the_map_is_not_offered(self):
        frames, restored = self.run_main([2, 2], keyboard=False)
        self.assertEqual(len(frames), 3)
        self.assertFalse([f for f in frames if self.is_map(f) or "screens" in f[-1]])
        self.assertEqual(restored, [])                                                 # no terminal settings were changed

    def test_feature_off_m_and_2_do_nothing(self):
        render.CFG["features"]["map"] = False
        frames, restored = self.run_main([b"m", b"2", b"m", b"\x1b[B"])
        self.assertEqual(len(frames), 5)
        self.assertFalse([f for f in frames if self.is_map(f) or "1-5: screens" in f[-1]])
        self.assertEqual(restored, [["attrs", 5]])

    def failing_once(self):
        """map_graph() that fails on its first call (state unreadable), then works: -> the times it was called at."""
        calls, real = [], render.map_graph

        def flaky(smp=None):
            calls.append(self.now - NOW)
            if len(calls) == 1:
                raise ValueError("state unreadable " + ESC + "[2J")
            return real(smp)
        render.map_graph = flaky                                                       # restored by tearDown (RENDER_GLOBALS)
        return calls

    @staticmethod
    def error_frame(frame):
        return bool(re.search(r"\[\d[ ·]Map\]", frame[0]) and "error on the map" in frame[1])

    def test_a_broken_map_is_an_error_frame_esc_still_works_and_it_retries(self):
        calls = self.failing_once()
        frames, _ = self.run_main([b"m", render.REFRESH_S, b"\x1b", b"m", b"\x1b"])   # check_frame: no raw escape on screen
        self.assertEqual([self.error_frame(f) for f in frames], [False, True, False, False, False, False])
        self.assertIn("ValueError('state unreadable", frames[1][1])
        self.assertEqual(frames[1][-1].strip(), "Esc: back   1-5: screens")
        self.assertEqual([self.is_map(f) for f in frames], [False, False, True, False, True, False])  # the next refresh draws it
        self.assertEqual(calls, [0, render.REFRESH_S, render.REFRESH_S])

    def test_a_failed_map_load_keeps_its_own_error_on_the_next_frames(self):
        self.failing_once()
        frames, _ = self.run_main([b"m", b"\x1b[B", b"\x1b"])
        self.assertTrue(self.error_frame(frames[2]))
        self.assertIn("state unreadable", frames[2][1])                                # not: TypeError("'NoneType' object is not subscriptable")

    def test_keys_do_not_postpone_the_retry_of_a_failed_map_load(self):
        calls = self.failing_once()
        half = render.REFRESH_S / 2.0
        frames, _ = self.run_main([b"m", (half, b"\x1b[B"), (half, b"\x1b[B"), b"\x1b"])
        self.assertEqual(calls, [0, render.REFRESH_S])                                 # retried REFRESH_S after the failure
        self.assertTrue(self.is_map(frames[3]))

    def test_a_failed_map_load_does_not_say_all_ok(self):
        self.failing_once()
        frames, _ = self.run_main([b"m", b"\x1b"])
        self.assertTrue(self.error_frame(frames[1]))
        self.assertIn("PROBLEMS", frames[0][0])                                        # the rotation, just before: ✖ 5 PROBLEMS
        self.assertNotIn("ALL OK", frames[1][0])                                       # nothing could be read: never a reassuring status

    def test_the_demo_map_carries_the_declared_reach(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render.once(["render.py", "--once", "--demo", "--view", "map", "--cols", "160", "--rows", "30"])
        text = out.getvalue()
        self.assertIn("declared local in config.ini, reachable from the LAN and the tailnet", text)
        self.assertIn("declared tailnet in config.ini, reachable from the Internet", text)
        cont, net, boot, base = demo.snapshot(now=NOW)
        render.CFG["expose"] = {"shop-db": "LOCALE"}                                   # the rotation's map page builds with [expose] as well
        self.assertIn("declared local in config.ini", "\n".join(ansi.ANSI.sub("", x) for x in render.map_slide(cont, net, boot, base, 160, 30)))

    def test_the_command_line_refuses_the_map_when_the_feature_is_off(self):
        render.CFG["features"]["map"] = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = render.once(["render.py", "--once", "--demo", "--view", "map", "--cols", "120", "--rows", "33"])
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertIn("the map is off: [features] map = no", err.getvalue())


class HostileAndEmpty(MapCase):
    """Names come from containers and processes: never a raw escape sequence on the console. No data: said, never a crash."""

    EVIL_CT = "x" + ESC + "[2J" + "y"
    EVIL_PROC = "p" + ESC + "]0;owned" + BEL + "q" + CSI8 + "31m\r\nz"
    EVIL_NET = "net" + ESC + "[5m" + "\t"

    def hostile(self, cont, net, boot):
        links = net["links"]
        base = dict(links["containers"][0], depends_on=[], env_refs=[], ports=[], health="", listen=[5432])
        links["containers"] += [dict(base, name=self.EVIL_CT, image="img" + ESC + "[1m", service="s" + BEL, db="postgres",
                                     nets={self.EVIL_NET: ""}),
                                dict(base, name="peer" + ESC + "[8m", nets={self.EVIL_NET: ""}, listen=[], db=None)]
        links["conns"] += [{"from": "proc:" + self.EVIL_PROC, "to": "ct:" + self.EVIL_CT, "port": 5432, "n": 1, "last": NOW},
                           {"from": "ct:" + self.EVIL_CT, "to": "ext:203.0.113.7" + ESC + "[2J", "port": 443, "n": 1, "last": NOW}]
        links["errors"] = ["sampler said " + ESC + "[2J" + BEL]

    def test_control_characters_in_names_never_reach_the_console(self):
        self.feed(change=self.hostile)
        raw = (ESC + "[2J", ESC + "]0", ESC + "[5m", ESC + "[8m", BEL, CSI8, "\t")
        for cols, rows in SIZES:
            for opts in (["--expand", "all", "--select", "x?[2Jy", "--details"], ["--expand", "all", "--select", "peer"],
                         ["--expand", "all", "--details", "--select", "203.0.113.7"], ["--expand", "fit"]):
                with self.subTest(size=(cols, rows), opts=opts):
                    s, lines = self.screen(opts, cols, rows)                           # check_frame: only SGR and CRLF survive
                    for x in raw:
                        self.assertNotIn(x, s)
                    self.assertEqual(s.count("\r"), rows - 1)
                    self.assertEqual(s.count("\n"), rows - 1)
                    self.assertTrue("x?[2Jy" in "\n".join(lines) or "peer?[8m" in "\n".join(lines) or opts[-1] == "fit")
        s, lines = self.screen(["--expand", "all", "--select", "x?[2Jy", "--details"], 226, 50)
        txt = "\n".join(lines)
        for shown in ("x?[2Jy", "img?[1m", "net?[5m?", "p?]0;owned?q?31m??z", "203.0.113.7?[2J", "sampler said ?[2J?"):
            self.assertIn(shown, txt)                                                  # drawn, with '?' for every control character
        self.assertIn("x?[2Jy", chosen(s)[0])
        render.CFG["map_in_rotation"] = True                                           # the rotation slide draws the same names
        render.page_overview = overview_stub
        cont, net, boot, base = demo.snapshot(now=NOW)
        self.hostile(cont, net, boot)
        body = render.slides(FakeSampler().sample(), cont, net, 225, 48, boot, base, mode="overview")[-1][3]
        frame = render.frame(("Map", 1, 1, body), 1, 2, 225, 50, [], keys=False)
        self.check_frame(frame, 225, 50)
        self.assertIn("sampler said ?[2J?", ansi.ANSI.sub("", frame))

    def test_the_renderer_cleans_even_what_the_graph_would_let_through(self):
        raw = "evil" + ESC + "[2J" + BEL + CSI8 + "1m\r\n" + "end"                     # graph.build cleans names: here it did not
        build = graph.build

        def unclean(*a, **kw):
            G = build(*a, **kw)
            for n in G["nodes"].values():
                if n["label"] in ("shop-api-1", "shop-db-1", "sshd"):
                    n["label"] = n["sub"] = raw + n["label"]
                    n["facts"].append((raw, raw))
                    n["findings"].append(("err", raw))
            for e in G["edges"]:
                e["why"].append(raw)
            G["notes"].insert(0, raw)
            return G
        render.graph = Proxy(graph, build=unclean)                                     # map_graph() and map_slide() build through it
        render.CFG["map_in_rotation"], render.page_overview = True, overview_stub
        for cols, rows in SIZES:
            for opts in (["--expand", "all", "--select", "evil", "--details"], ["--expand", "all", "--select", "sshd", "--details"],
                         ["--only", "--details"]):
                with self.subTest(size=(cols, rows), opts=opts):
                    s, lines = self.screen(opts, cols, rows)
                    self.assertNotIn(ESC + "[2J", s)
                    self.assertNotIn(BEL, s)
                    self.assertIn("evil?[2J??1m??end", "\n".join(lines))
            cont, net, boot, base = demo.snapshot(now=NOW)
            body = render.slides(FakeSampler().sample(), cont, net, cols - 1, rows - 2, boot, base, mode="overview")[-1][3]
            self.check_frame(render.frame(("Map", 1, 1, body), 1, 2, cols - 1, rows, [], keys=False), cols - 1, rows, cols)
            self.assertIn("evil?[2J??1m??end", ansi.ANSI.sub("", "\n".join(body)))

    def test_no_collector_data_says_there_is_nothing_to_draw(self):
        render.DEMO, render.Sampler = False, FakeSampler                                # not the demo: it declares two [webapps]
        render.CFG["webapps"], render.CFG["map_in_rotation"] = {}, True
        render.page_overview = overview_stub
        for data, note in (((None, None, None), "network collector not running"), (({}, {}, {}), "restart the collector"),
                           ((None, {}, None), "container collector not running")):
            self.feed(data=data)
            for cols, rows in SIZES:
                for opts in ([], ["--expand", "all", "--details", "--select", "x"], ["--expand", "fit"]):
                    with self.subTest(data=data, size=(cols, rows), opts=opts):
                        s, lines = self.screen(opts, cols, rows)
                        txt = "\n".join(lines)
                        self.assertIn("nothing to draw: see the notes above", txt)
                        self.assertIn(note, txt)
                        self.assertIn("0 nodes", lines[1])
                        self.assertIn("0/0", lines[-1])
                        self.assertNotIn("DETAILS", txt)
                s, lines = self.screen(["--only"], 120, 33)
                self.assertIn("no problem on any path", "\n".join(lines))
                body = render.slides(FakeSampler().sample(), data[0], data[1], cols - 1, rows - 2, data[2], None, mode="overview")[-1][3]
                self.assertIn("nothing to draw", "\n".join(ansi.ANSI.sub("", x) for x in body), cols)
        self.feed(data=(demo.snapshot(now=NOW)[0], None, None))                       # containers, but no network collector
        s, lines = self.screen(["--expand", "all"], 120, 33)
        self.assertIn("network collector not running", "\n".join(lines))
        self.assertIn("worker-1", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
