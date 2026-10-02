"""One keymap for every screen: ui.KEYMAP is the table, render.main() dispatches from it, every footer and the `?` help are made from it.

The screens are real where they are cheap (the Map, on the demo) and stubbed where they need the host (CPU, Health, AI: their keys are
tested on their own views, in the files of their screens); the dispatcher is the real render.main() on a fake terminal."""
import io
import os
import re
import shutil
import signal
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import ansi  # noqa: E402
import graph  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import ui  # noqa: E402
from test_map_console import FakeSampler, MapCase, Proxy, Stop, overview_stub  # noqa: E402

WIDTHS = (79, 120, 200, 226)                 # console columns (the footers get one less on the Linux console, a browser page all of them)
SCREENS = ("overview", "map", "cpu", "health", "ai")
NAMES = {"overview": "Overview", "map": "Map", "cpu": "CPU", "health": "Health", "ai": "AI"}
SHORT = {"overview": "Ov", "map": "Map", "cpu": "CPU", "health": "Hlth", "ai": "AI"}  # the tab bar on a narrow line
PROCS = [{"pid": 100 + i, "name": f"p{i}", "user": "u", "cpu": 50.0 - i, "mem": 10.0 + i, "time": 100.0 - i} for i in range(8)]
STUBBED = ("CpuFeed", "cpu_screen", "health_state", "health_screen", "ai_state", "ai_screen", "ai_do", "ai_busy", "slides", "map_graph")


class Exit(Exception):
    """render.main() returned (a portable `q`)."""


def plain(lines):
    return [ansi.ANSI.sub("", x) for x in lines]


class KeyTable(unittest.TestCase):
    def test_rows_are_well_formed(self):
        for r in ui.KEYMAP:
            self.assertIn(r.scope, ui.SCOPES, r)
            self.assertTrue(r.keys and all(isinstance(k, str) and k for k in r.keys), r)
            self.assertTrue(r.action and r.label and r.show, r)
            self.assertTrue(r.prio is None or isinstance(r.prio, (int, float)), r)
            self.assertIn(r.feat, ("", "portable", "map", "cpu", "health", "ai"), r)

    def test_no_key_is_bound_twice_in_a_scope_or_with_the_global_ones(self):
        for scope in ui.SCOPES:
            seen = {}
            for s in ui.chain(scope):
                for r in ui.KEYMAP:
                    if r.scope == s:
                        for k in r.keys:
                            self.assertNotIn(k, seen, f"{k!r} is bound in {s} and in {seen.get(k)} (screen {scope})")
                            seen[k] = s
        for r in ui.KEYMAP:  # one action = one row of one scope, whatever the keys
            self.assertEqual([x for x in ui.KEYMAP if x.scope == r.scope and x.action == r.action and x.keys == r.keys], [r])

    def test_letters_are_local_digits_and_symbols_are_global(self):
        for r in ui.KEYMAP:
            for k in r.keys:
                if r.scope == "global" and len(k) == 1:
                    self.assertFalse(k.isalpha() and k not in "qrZ", r)  # the global letters: q r Z
                if len(k) == 1 and k in "12345?":
                    self.assertEqual(r.scope, "global", r)

    def test_lookup_finds_the_screen_row_then_the_list_row_then_the_global_one(self):
        self.assertEqual(ui.action("map", "c"), "collapse")
        self.assertEqual(ui.action("ai", "c"), "cancel")
        self.assertEqual(ui.action("overview", "c"), "open-cpu")
        self.assertEqual(ui.action("cpu", "c"), "")
        self.assertEqual(ui.action("cpu", "m"), "sort")
        self.assertEqual(ui.action("health", "m"), "period")
        self.assertEqual(ui.action("overview", "m"), "open-map")
        self.assertEqual(ui.action("map", "down"), "move")
        self.assertEqual(ui.action("overview", "down"), "")
        self.assertEqual(ui.action("overview", "right"), "slide-next")
        self.assertEqual(ui.action("map", "right"), "open")
        for scope in SCREENS:
            for k, act in (("3", "screen"), ("?", "help"), ("r", "redraw"), ("Z", "pause"), ("tab", "next"), ("btab", "prev"), ("esc", "back"),
                           ("q", "back")):
                self.assertEqual(ui.action(scope, k), act, (scope, k))
        self.assertEqual(ui.action("overview", "h", lambda f: f != "health"), "")  # a disabled feature: no action

    def test_the_digits_follow_the_features(self):
        self.assertEqual(ui.screen_keys(), [("1", "overview"), ("2", "map"), ("3", "cpu"), ("4", "health"), ("5", "ai")])
        self.assertEqual(ui.screens_show(), "1-5")
        self.assertEqual(ui.screens_show(lambda f: f != "map"), "1 3-5")
        self.assertEqual(ui.screens_show(lambda f: f not in ("cpu", "ai")), "1 2 4")
        self.assertEqual(ui.screens_show(lambda f: f in ("cpu",)), "1 3")
        self.assertEqual(ui.screens_show(lambda f: False), "1")

    def test_fit_drops_the_largest_prio_first_and_keeps_one_item(self):
        items = [(0, "Esc: back", None), (2, "a: aaaa", "a"), (5, "b: bbbb", "b"), (9, "c: cccc", "")]
        self.assertEqual(ui.fit(" ", items, 99), " Esc: back   a: aaaa   b: bbbb   c: cccc")
        self.assertEqual(ui.fit(" ", items, 30), "  Esc: back  a  b")                          # the short forms; the one without goes
        self.assertEqual(ui.fit(" ", items, 12), "  Esc: back")
        self.assertEqual(ui.fit(" ", items, 3), "  Esc: back")                                 # one item is always kept
        self.assertEqual(ui.fit(" x ", [], 9), " x ")


class Overlay(unittest.TestCase):
    def test_the_box_goes_over_the_middle_and_the_rest_stays(self):
        lines = ["\x1b[1m" + "a" * 10 + "\x1b[0m" + "b" * 10 for _ in range(5)]
        out = ansi.overlay(lines, ["XXXX", "YYYY"], 20)
        text = [ansi.ANSI.sub("", x) for x in out]
        self.assertEqual(text[0], "a" * 10 + "b" * 10)
        self.assertEqual(text[1], "a" * 8 + "XXXX" + "b" * 8)
        self.assertEqual(text[2], "a" * 8 + "YYYY" + "b" * 8)
        self.assertEqual(text[3:], [text[0]] * 2)
        self.assertEqual(out[0], lines[0])                                                # a line the box does not touch is not changed
        self.assertTrue(out[1].startswith("\x1b[1m"))                                     # the colour before the box stays
        self.assertEqual(len(out), 5)

    def test_the_colour_in_force_carries_over_to_what_is_right_of_the_box(self):
        out = ansi.overlay(["\x1b[31m" + "r" * 20 + "\x1b[0m"], ["BB"], 20)
        self.assertEqual(ansi.ANSI.sub("", out[0]), "r" * 9 + "BB" + "r" * 9)
        self.assertIn("\x1b[31m" + "r" * 9, out[0].split("BB")[1])

    def test_a_short_line_keeps_its_length_a_tall_box_is_cut_and_a_wide_one_too(self):
        out = ansi.overlay(["ab", ""], ["XXXXXX"], 10)
        self.assertEqual(ansi.ANSI.sub("", out[0]), "ab" + "XXXXXX")                     # centred at column 2 of 10
        self.assertEqual(ansi.ANSI.sub("", out[1]), "")
        self.assertEqual(ansi.ANSI.sub("", ansi.overlay(["abc"], ["1", "2", "3"], 3)[0]), "a1c")
        self.assertEqual(len(ansi.overlay(["abc", "def"], ["1", "2", "3"], 3)), 2)
        self.assertEqual(ansi.ANSI.sub("", ansi.overlay(["abcd"], ["123456"], 4)[0]), "1234")


class Footers(MapCase):
    """Every screen's footer fits every width, offers the way back and the help, and shortens by priority."""

    def test_every_footer_fits_every_width(self):
        mv, cv, hv, av = screens.MapView(now=1), render.CpuView(now=1), screens.HealthView(now=1), screens.AiView(now=1)
        for cols in WIDTHS + (60, 40, 24, 12):
            for w in (cols, cols - 1):
                feet = {"map": render.map_footer(mv, 21, w, True), "cpu": render.cpu_footer(cv, 40, w), "health": render.health_footer(hv, 10, w),
                        "ai": render.ai_footer(av, 12, w, {"locked": False}), "ai locked": render.ai_footer(av, 12, w, {"locked": True})}
                for n, details, hint in ((1, "Overview", ""), (3, "Details", "hint here"), (3, "System", "")):
                    for keys in (True, False):
                        feet[f"overview {n} {details} {keys}"] = render.frame((details, 1, 1, ["x"]), 0, n, w, 24, [], keys=keys, hint=hint
                                                                           ).split("\r\n")[-1]
                for name, f in feet.items():
                    self.assertLessEqual(len(ansi.ANSI.sub("", f)), w, (name, w))

    def test_the_way_out_and_the_help_stay_when_there_is_room_and_the_way_out_is_the_last_to_go(self):
        mv, cv, hv, av = screens.MapView(now=1), render.CpuView(now=1), screens.HealthView(now=1), screens.AiView(now=1)
        for name, foot in (("map", lambda w: render.map_footer(mv, 21, w)), ("cpu", lambda w: render.cpu_footer(cv, 40, w)),
                           ("health", lambda w: render.health_footer(hv, 10, w)), ("ai", lambda w: render.ai_footer(av, 12, w, {"locked": False}))):
            for w in WIDTHS:
                text = ansi.ANSI.sub("", foot(w))
                self.assertIn("Esc: back", text, (name, w))
                self.assertIn("?: help", text, (name, w))
                self.assertIn("1-5: screens", text, (name, w)) if w >= 120 else None
            narrow = ansi.ANSI.sub("", foot(40))
            self.assertIn("Esc: back", narrow, name)
            self.assertNotIn("PgUp", narrow)

    def test_the_footers_name_the_keys_of_the_table(self):
        mv, cv, hv, av = screens.MapView(now=1), render.CpuView(now=1), screens.HealthView(now=1), screens.AiView(now=1)
        got = {"map": ansi.ANSI.sub("", render.map_footer(mv, 21, 226)), "cpu": ansi.ANSI.sub("", render.cpu_footer(cv, 40, 226)),
               "health": ansi.ANSI.sub("", render.health_footer(hv, 10, 226)),
               "ai": ansi.ANSI.sub("", render.ai_footer(av, 12, 226, {"locked": False}))}
        want = {"map": ("↑↓: move", "←→: close/open", "Enter: details", "e/c: expand/collapse all", "p: problems only"),
                "cpu": ("↑↓: move", "Enter: details", "sort: P cpu"), "health": ("↑↓: move", "d/w/m: 24h/7d/30d", "Enter: details"),
                "ai": ("↑↓: move", "e: AI on/off", "u: use model", "x: delete", "X: delete all", "c: cancel", "Enter: details")}
        for scope, texts in want.items():
            for t in texts:
                self.assertIn(t, got[scope], scope)
        locked = ansi.ANSI.sub("", render.ai_footer(av, 12, 226, {"locked": True}))
        for t in ("e: AI on/off", "x: delete", "c: cancel"):
            self.assertNotIn(t, locked)                                                    # [ai] web_actions = no: the keys that act are not offered

    def test_the_overview_footer_offers_what_exists(self):
        slide = ("Overview", 1, 3, ["x"])
        foot = lambda **kw: ansi.ANSI.sub("", render.frame(slide, 0, 3, 199, 24, [], **kw)).split("\r\n")[-1]  # noqa: E731
        self.assertEqual(foot(keys=True), " screen 1/3   ←→: slide   1-5: screens   ?: help   console 200x24")
        self.assertEqual(foot(keys=False), " screen 1/3   console 200x24")
        self.assertNotIn("jump to page", foot(keys=True))                                  # there is no such page: the slides are ←→
        self.assertEqual(ansi.ANSI.sub("", render.frame(slide, 0, 1, 199, 24, [], keys=True)).split("\r\n")[-1],
                         " single screen   1-5: screens   ?: help   console 200x24")      # one screen: nothing to move through
        nuc_config.PORTABLE, old = "/x", nuc_config.PORTABLE
        try:
            self.assertEqual(foot(keys=True), " screen 1/3   ←→: slide   1-5: screens   q: quit   ?: help   console 200x24")
        finally:
            nuc_config.PORTABLE = old
        det = ansi.ANSI.sub("", render.frame(("Details", 1, 1, ["x"]), 1, 3, 226, 24, [], keys=True)).split("\r\n")[-1]
        self.assertIn("details: everything the overview cut ('… +N more')", det)
        self.assertEqual(ansi.ANSI.sub("", render.frame(slide, 0, 3, 120, 24, [], keys=True, hint="HINT")).split("\r\n")[-1].count("HINT"), 1)

    def test_the_footer_does_not_name_a_screen_that_is_off(self):
        render.CFG["features"]["health"] = False
        slide = ("Overview", 1, 1, ["x"])
        self.assertIn("1-3 5: screens", ansi.ANSI.sub("", render.frame(slide, 0, 1, 200, 24, [], keys=True)))
        self.assertIn("1-3 5: screens", ansi.ANSI.sub("", render.frame(slide, 0, 1, 200, 24, [], keys=True, mapkey=True)))
        self.assertIn("1 2 5: screens", ansi.ANSI.sub("", render.frame(slide, 0, 1, 200, 24, [], keys=True, cpukey=False)))


class Handlers(MapCase):
    """Every key of every scope does its action on the view that owns it; the keys of every screen are the dispatcher's, not theirs."""

    def graph(self):
        import demo
        cont, net, boot, base = demo.snapshot(now=1_790_000_000)
        return graph.build(cont, net, boot, {}, now=1_790_000_000, baseline=base)

    def test_map(self):
        G = self.graph()
        results = {}
        for r in (x for x in ui.KEYMAP if x.scope in ("map", "list")):
            for k in r.keys:
                mv = screens.MapView(now=1)
                rs = graph.rows(G, mv.st)
                screens.map_sync(mv, rs)
                screens.map_key(mv, "down", rs)
                screens.map_key(mv, "right", rs)
                rs = graph.rows(G, mv.st)
                before = (mv.idx, mv.details, set(mv.st.open), mv.st.all, mv.st.only)
                act = screens.map_key(mv, k, rs, 5)
                after = (mv.idx, mv.details, set(mv.st.open), mv.st.all, mv.st.only)
                results[(r.action, k)] = (act, before != after)
        for (action, k), (act, changed) in results.items():
            if action in ("move", "page"):
                self.assertTrue(changed or k in ("up", "k", "home", "pgup"), (k, "moves"))  # the cursor starts below the first row
                self.assertEqual(act, "", k)
            elif action == "details":
                self.assertEqual((act, changed), ("", True), k)
            elif action in ("expand", "collapse", "problems"):
                self.assertEqual(act, "rows", (action, k))
            else:  # open, close: a branch opens or closes, or the cursor goes to its first child or its parent
                self.assertTrue(changed, (action, k))
        self.assertEqual({a for a, _k in results}, {"move", "page", "details", "close", "open", "expand", "collapse", "problems"})

    def test_cpu(self):
        rows = screens.cpu_rows(PROCS)
        for k in ("p", "m", "t", "n", "u", "P", "M", "T", "N", "U"):
            cv = render.CpuView(now=1)
            cv.sort = "pid" if k.lower() == "n" else "cpu" if k.lower() != "p" else "mem"
            self.assertEqual(screens.cpu_key(cv, k, rows), "rows", k)
            self.assertEqual(cv.sort, screens.CPU_SORT_KEYS[k.lower()], k)
        for k in ("down", "j"):
            cv = render.CpuView(now=1)
            screens.cpu_key(cv, k, rows)
            self.assertEqual(cv.idx, 1, k)
        for k in ("pgdn", "end"):
            cv = render.CpuView(now=1)
            screens.cpu_key(cv, k, rows, 3)
            self.assertEqual(cv.idx, 3 if k == "pgdn" else len(rows) - 1, k)
        cv = render.CpuView(now=1)
        for k in ("enter", "space"):
            screens.cpu_key(cv, k, rows)
            self.assertTrue(cv.details, k)
            screens.cpu_key(cv, k, rows)
            self.assertFalse(cv.details, k)

    def test_health(self):
        fl = [{"id": f"f{i}"} for i in range(6)]
        for k, days in (("d", 1), ("w", 7), ("m", 30)):
            hv = screens.HealthView(days=7 if days != 7 else 30, now=1)
            self.assertEqual(screens.health_key(hv, k, fl), "period", k)
            self.assertEqual(hv.days, days)
        for k in ("1", "7", "3"):  # the digits are the screens: they are not periods
            hv = screens.HealthView(now=1)
            self.assertEqual((screens.health_key(hv, k, fl), hv.days), ("", 7), k)

    def test_ai(self):
        rows = [{"id": f"m{i}", "name": f"M{i}", "rec": i == 2, "installed": True} for i in range(5)]
        for k, act in (("e", "toggle"), ("u", "use"), ("x", "delete"), ("X", "delete-all"), ("c", "cancel")):
            av = screens.AiView(now=1)
            self.assertEqual(screens.ai_key(av, k, rows), act, k)
        av = screens.AiView(now=1)
        av.confirm = ("delete", "m1", "Delete?")                                         # a question: y does it, any other key is no
        self.assertEqual(screens.ai_key(av, "y", rows), "yes")
        for k in ("n", "esc", "q", "1", "?", "tab", "e", "x"):
            av.confirm = ("delete", "m1", "Delete?")
            self.assertEqual(screens.ai_key(av, k, rows), "", k)
            self.assertIsNone(av.confirm, k)

    def test_the_keys_of_every_screen_are_not_theirs(self):
        G = self.graph()
        rows, fl, mrows = screens.cpu_rows(PROCS), [{"id": "f0"}], graph.rows(G)
        airows = [{"id": "m0", "name": "M", "rec": True, "installed": True}]
        for k in ("tab", "btab", "1", "2", "3", "4", "5", "?", "r", "Z"):
            self.assertEqual(screens.map_key(screens.MapView(now=1), k, mrows), "", k)
            self.assertEqual(screens.cpu_key(render.CpuView(now=1), k, rows), "", k)
            self.assertEqual(screens.health_key(screens.HealthView(now=1), k, fl), "", k)
            self.assertEqual(screens.ai_key(screens.AiView(now=1), k, airows), "", k)

    def test_esc_closes_the_details_pane_first_on_every_list(self):
        G = self.graph()
        mv, cv, hv, av = screens.MapView(now=1), render.CpuView(now=1), screens.HealthView(now=1), screens.AiView(now=1)
        calls = ((mv, lambda v, k: screens.map_key(v, k, graph.rows(G))), (cv, lambda v, k: screens.cpu_key(v, k, screens.cpu_rows(PROCS))),
                 (hv, lambda v, k: screens.health_key(v, k, [{"id": "f"}])),
                 (av, lambda v, k: screens.ai_key(v, k, [{"id": "m", "name": "M", "rec": True, "installed": True}])))
        for key in ("esc", "q"):
            for v, call in calls:
                v.details = True
                self.assertEqual((call(v, key), v.details), ("", False))
                self.assertEqual((call(v, key), v.details), ("back", False))


class Dispatch(MapCase):
    """render.main() on a fake terminal: the global keys, the overview's, the help, the pause, the redraw."""

    def setUp(self):
        super().setUp()
        self.saved_stub = {k: getattr(render, k) for k in STUBBED}
        self.saved_portable = nuc_config.PORTABLE
        for f in ("cpu", "health", "ai"):
            render.CFG["features"][f] = True
        self.calls = {"map": [], "cpu": [], "health": [], "ai": [], "do": []}
        self.slide_names = None
        render.CpuFeed = lambda: types.SimpleNamespace(read=lambda: {"procs": {"procs": PROCS}})
        render.cpu_screen = self.cpu_screen
        render.health_state = self.health_state
        render.health_screen = self.health_screen
        render.ai_state = self.ai_state
        render.ai_screen = self.ai_screen
        render.ai_do = self.ai_do
        render.ai_busy = lambda: False
        real_graph = render.map_graph
        render.map_graph = lambda smp=None: self.calls["map"].append(self.now) or real_graph(smp)

    def tearDown(self):
        for k, v in self.saved_stub.items():
            setattr(render, k, v)
        nuc_config.PORTABLE = self.saved_portable
        render.PAUSED = False
        super().tearDown()

    # -- the stubbed screens: a frame with the real footer, so that the keys have something to show
    def cpu_screen(self, d, pb, cv, w, h):
        self.calls["cpu"].append(self.now)
        rows = screens.cpu_rows(d["procs"]["procs"], cv.sort)
        screens.cpu_sync(cv, rows)
        cv.page = 4
        return render.frame(("CPU", 1, 1, [f"CPU sort={cv.sort} row={cv.idx} details={cv.details}"]), 0, 1, w, h, pb,
                            foot=render.cpu_footer(cv, len(rows), w)), rows

    def health_state(self, smp, days):
        self.calls["health"].append((self.now, days))
        return {"days": days}, []

    def health_screen(self, data, pb, hv, w, h):
        fl = [{"id": f"f{i}"} for i in range(6)]
        screens.health_sync(hv, fl)
        hv.rows = 4
        return render.frame(("Health", 1, 1, [f"HEALTH days={hv.days} row={hv.idx} details={hv.details}"]), 0, 1, w, h, pb,
                            foot=render.health_footer(hv, len(fl), w)), fl

    def ai_state(self, smp):
        self.calls["ai"].append(self.now)
        return {}, []

    def ai_screen(self, data, pb, av, w, h, wait=0.0):
        rows = [{"id": f"m{i}", "name": f"M{i}", "rec": i == 2, "installed": True} for i in range(5)]
        screens.ai_sync(av, rows)
        av.rows = 4
        return render.frame(("AI", 1, 1, [f"AI row={av.idx} details={av.details}"]), 0, 1, w, h, pb,
                            foot=render.ai_footer(av, len(rows), w, {"locked": False})), rows

    def ai_do(self, av, act, rows):
        """What ai_do does to the view: a question first for delete, the answer clears it."""
        self.calls["do"].append(act)
        av.confirm = ("delete", "m1", "Delete it? ") if act == "delete" else None

    def three_slides(self):
        render.slides = lambda *a, **kw: [("Overview", 1, 3, ["slide a"]), ("System", 2, 3, ["slide b"]), ("Boot", 3, 3, ["slide c"])]

    def run_main(self, script, cols=80, rows=24, keyboard=True, exits=False):
        """The frames drawn (ANSI stripped, lists of lines) for a script of raw reads, seconds or (seconds, raw)."""
        out, err, todo = io.StringIO(), io.StringIO(), list(script)

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
        render.termios = types.SimpleNamespace(tcgetattr=lambda fd: ["attrs", fd], TCSADRAIN=1, tcsetattr=lambda fd, when, attrs: None)
        render.tty = types.SimpleNamespace(setcbreak=lambda fd: None)
        render.signal = Proxy(signal, signal=lambda *a: None)
        render.shutil = Proxy(shutil, get_terminal_size=lambda fallback=None: os.terminal_size((cols, rows)))
        render.WINDOWS, render.MODE, render.Sampler = False, "overview", FakeSampler
        render.read_keys, render.page_overview = next_read, overview_stub
        self.on_sleep = lambda sec: next_read()
        if exits:
            code = render.main(["render.py"])
            self.assertEqual(code, 0)
        else:
            with self.assertRaises(Stop):
                render.main(["render.py"])
        text = out.getvalue()
        self.assertTrue(text.endswith("\x1b[?25h\x1b[0m"))
        frames = []
        for chunk in text[:-len("\x1b[?25h\x1b[0m")].split("\x1b[H")[1:]:
            chunk = chunk[:-len("\x1b[2J")] if chunk.endswith("\x1b[2J") else chunk
            frames.append(self.check_frame(chunk, cols - 1, rows, len(frames)))
        return frames

    def shown(self, frames):
        """The screen each frame is on, from its header."""
        return [next((s for s in SCREENS if re.search(r"\[\d[ ·](%s|%s)\]" % (NAMES[s], SHORT[s]), f[0])), "?") for f in frames]

    # -- the screens
    def test_the_digits_open_the_screens_from_anywhere(self):
        frames = self.run_main([b"2", b"3", b"4", b"5", b"1", b"4", b"2", b"5", b"3", b"1"])
        self.assertEqual(self.shown(frames), ["overview", "map", "cpu", "health", "ai", "overview", "health", "map", "ai", "cpu", "overview"])

    def test_a_digit_of_the_screen_you_are_on_does_nothing_and_a_disabled_screen_is_no_screen(self):
        render.CFG["features"]["health"] = False
        frames = self.run_main([b"2", b"2", b"4", b"3", b"4", b"5"])
        self.assertEqual(self.shown(frames), ["overview", "map", "map", "map", "cpu", "cpu", "ai"])
        self.assertNotIn("4", frames[0][-1].split(": screens")[0])
        self.assertIn("1-3 5: screens", frames[0][-1])

    def test_tab_and_shift_tab_go_round_the_enabled_screens(self):
        frames = self.run_main([b"\t", b"\t", b"\t", b"\t", b"\t", b"\x1b[Z", b"\x1b[Z", b"\x1b[Z"])
        self.assertEqual(self.shown(frames), ["overview", "map", "cpu", "health", "ai", "overview", "ai", "health", "cpu"])
        render.CFG["features"]["cpu"] = False
        frames = self.run_main([b"\t", b"\t", b"\t", b"\t", b"\x1b[Z"])
        self.assertEqual(self.shown(frames), ["overview", "map", "health", "ai", "overview", "ai"])

    def test_without_a_keyboard_no_key_is_offered_and_none_is_read(self):
        frames = self.run_main([2, 2], keyboard=False)
        self.assertEqual(self.shown(frames), ["overview"] * 3)
        for f in frames:
            self.assertEqual(f[-1].strip(), "single screen   console 80x24")

    def test_esc_and_q_go_back_to_the_overview_after_the_details_pane(self):
        for screen, digit in (("map", b"2"), ("cpu", b"3"), ("health", b"4"), ("ai", b"5")):
            frames = self.run_main([digit, b"\r", b"\x1b", b"\x1b", b"\x1b", digit, b"q"])
            self.assertEqual(self.shown(frames), ["overview", screen, screen, screen, "overview", "overview", screen, "overview"], screen)
            self.assertIn("details=True" if screen != "map" else "DETAILS", "\n".join(frames[2]))
            self.assertNotIn("details=True" if screen != "map" else "DETAILS", "\n".join(frames[3]))   # Esc: the pane only

    def test_the_overview_aliases_open_the_screens_and_are_the_overviews_only(self):
        frames = self.run_main([b"m", b"1", b"c", b"1", b"h", b"1", b"a"])
        self.assertEqual(self.shown(frames), ["overview", "map", "overview", "cpu", "overview", "health", "overview", "ai"])
        frames = self.run_main([b"4", b"a", b"c", b"h", b"m"])       # on a screen the letters are that screen's: m = 30 days, the others nothing
        self.assertEqual(self.shown(frames), ["overview", "health", "health", "health", "health", "health"])
        self.assertIn("days=30", frames[-1][1])
        render.CFG["features"]["ai"] = False
        frames = self.run_main([b"a", b"5"])
        self.assertEqual(self.shown(frames), ["overview"] * 3)

    def test_q_quits_a_portable_console_and_does_nothing_on_an_installed_monitor(self):
        nuc_config.PORTABLE = ""
        frames = self.run_main([b"q", b"\x1b", b"q"])
        self.assertEqual(self.shown(frames), ["overview"] * 4)           # nothing happens: the monitor of an install must not quit
        self.assertNotIn("q: quit", frames[0][-1])
        nuc_config.PORTABLE = "/somewhere"
        frames = self.run_main([b"\x1b", b"q"], exits=True)              # Esc does nothing, q returns
        self.assertEqual(self.shown(frames), ["overview"] * 2)
        self.assertIn("q: quit", frames[0][-1])
        frames = self.run_main([b"3", b"q", b"q"], exits=True)            # on a screen q goes back first, then quits
        self.assertEqual(self.shown(frames), ["overview", "cpu", "overview"])

    def test_the_rotation_waits_while_a_screen_is_open_and_goes_on_where_it_was(self):
        self.three_slides()
        stay = render.CFG["overview_seconds"] + 1                      # on its own the clock would have moved on to the second slide
        self.assertGreater(stay, 1)
        frames = self.run_main([b"2", stay // 2, b"3", stay // 2, b"1"])
        names = ["".join(f[1].split()) for f in frames]
        self.assertEqual((names[0], names[-1]), ("slidea", "slidea"))
        self.assertEqual(self.shown(frames)[1:-1], ["map", "map", "cpu", "cpu"])

    # -- the overview's slides
    def test_left_and_right_move_through_the_slides_and_hold_them(self):
        self.three_slides()
        frames = self.run_main([b"\x1b[C", 10, b"\x1b[C", b"\x1b[C", b"\x1b[D", b"\x1b[6~", b"\x1b[5~", b"\x1b[5~", b"1"])
        names = ["".join(f[1].split()) for f in frames]
        self.assertEqual(names, ["slidea", "slideb", "slideb", "slidec", "slidea", "slidec", "slidea", "slidec", "slideb", "slidea"])
        self.assertTrue(all(re.search(r"\[1[ ·](Overview|Ov)\]", f[0]) for f in frames))          # the overview's pages are the first tab
        self.assertTrue(any(" │ System 2/3" in f[0] for f in frames) and any(" │ Boot 3/3" in f[0] for f in frames))  # and the page is named after it

    def test_a_held_slide_stays_for_hold_s_then_the_rotation_goes_on(self):
        self.three_slides()
        frames = self.run_main([b"\x1b[C", render.HOLD_S - 5, 10])
        names = ["".join(f[1].split()) for f in frames]
        self.assertEqual(names[:3], ["slidea", "slideb", "slideb"])                          # → holds the second slide, 5 s short of HOLD_S still
        sl = [("Overview", 1, 3, []), ("System", 2, 3, []), ("Boot", 3, 3, [])]
        self.assertEqual(names[3], ["slidea", "slideb", "slidec"][render.pick_slide(sl, render.HOLD_S + 5)])  # then the clock decides again

    # -- the help
    def test_the_help_overlay_lists_the_screens_keys_and_the_global_ones_and_any_key_closes_it(self):
        for scope, digit, own in (("overview", b"", ("←→ PgUp/PgDn", "open the Map")), ("map", b"2", ("←→ h l", "expand/collapse all", "problems only")),
                                  ("cpu", b"3", ("p m t n u", "sort")), ("health", b"4", ("d/w/m", "24h/7d/30d")),
                                  ("ai", b"5", ("AI on/off", "use model", "delete all", "answer a question"))):
            script = ([digit] if digit else []) + [b"?", b"x"]
            frames = self.run_main(script)
            f = frames[-2]
            text = "\n".join(f)
            self.assertIn("┌─ Keys", text, scope)
            self.assertIn("any key closes this help", text, scope)
            self.assertIn("Everywhere", text)
            self.assertIn("Esc", text)
            self.assertIn("pause the redraw", text)
            for t in own:
                self.assertIn(t, text, (scope, t))
            self.assertEqual(len(f), 24)
            self.assertNotIn("┌─ Keys", "\n".join(frames[-1]), scope)                           # the key closed it
            self.assertEqual(self.shown(frames)[-1], scope)                                      # and did nothing else
            self.assertRegex(f[0], r"\[\d[ ·](%s|%s)\]" % (NAMES[scope], SHORT[scope]))                                                    # the header and the footer stay visible
            self.assertIn("?: help", f[-1])

    def test_a_key_that_closes_the_help_does_not_act(self):
        frames = self.run_main([b"?", b"2", b"2"])
        self.assertEqual(self.shown(frames), ["overview", "overview", "overview", "map"])
        frames = self.run_main([b"?", b"?"])
        self.assertEqual(["┌─ Keys" in "\n".join(f) for f in frames], [False, True, False])

    def test_the_help_fits_the_smallest_console_and_says_how_many_rows_it_dropped(self):
        for scope, digit in (("map", b"2"), ("ai", b"5"), ("overview", b"")):
            frames = self.run_main(([digit] if digit else []) + [b"?"], cols=80, rows=24)
            f = frames[-1]
            self.assertEqual(len(f), 24)
            box = [x for x in f if "│" in x or "┌" in x or "└" in x]
            self.assertGreaterEqual(len(box), 8, scope)
            self.assertLessEqual(len(box), 22)
            self.assertTrue(f[0].strip() and f[-1].strip())
        frames = self.run_main([b"2", b"?"], cols=80, rows=12)
        f = frames[-1]
        self.assertEqual(len(f), 12)
        self.assertRegex("\n".join(f), r"\+\d+ more")
        self.assertIn("any key closes this help", "\n".join(f))

    def test_the_help_box_follows_the_features_and_the_install_kind(self):
        nuc_config.PORTABLE = ""
        box = "\n".join(ansi.ANSI.sub("", x) for x in render.help_box("overview", 80, 24, lambda f: f != "ai"))
        self.assertNotIn("open the AI screen", box)
        self.assertIn("open the Health screen (as 4)", box)
        self.assertNotIn("quit", box)                                                   # an installed monitor does not quit
        self.assertIn("1-4", box)
        box = "\n".join(ansi.ANSI.sub("", x) for x in render.help_box("overview", 80, 24, lambda f: True, portable=True))
        self.assertRegex(box, r"q\s+quit")
        box = "\n".join(ansi.ANSI.sub("", x) for x in render.help_box("map", 80, 24, lambda f: True, paused=True))
        self.assertIn("resume the redraw", box)
        self.assertRegex(box, r"q\s+like Esc")

    # -- redraw and pause
    def test_r_asks_for_new_data_now(self):
        self.run_main([b"2", b"\x1b[B"])
        self.assertEqual(len(self.calls["map"]), 1)                                       # the first load: a key does not read again
        self.calls["map"][:] = []
        frames = self.run_main([b"2", b"r"])
        self.assertEqual(self.shown(frames), ["overview", "map", "map"])
        self.assertEqual(len(self.calls["map"]), 2)                                       # and `r` asked for the data now

    def test_z_pauses_the_redraw_the_header_says_so_and_nothing_new_is_read_until_it_goes_on(self):
        step = render.REFRESH_S
        frames = self.run_main([b"2", b"Z", step, step, step, b"\x1b[B", step, b"Z"])
        self.assertEqual(self.shown(frames), ["overview", "map", "map", "map", "map"])
        self.assertEqual(["paused" in f[0] for f in frames], [False, False, True, True, False])  # a frame for each key, none for the waits
        self.assertEqual(len(self.calls["map"]), 2)                                      # one load when it opened, one when it went on: none in between
        self.assertEqual(self.calls["map"][1] - self.calls["map"][0], 4 * step)

    def test_a_paused_frame_is_not_redrawn_without_a_key_and_the_cursor_still_moves(self):
        step = render.REFRESH_S
        frames = self.run_main([b"2", b"Z", step, step, b"\x1b[B", step])
        self.assertEqual(len(frames), 4)                                                  # opened, paused, the key: the waits drew nothing
        rows = [int(re.search(r"(\d+)/(\d+)", f[-1]).group(1)) for f in frames[1:]]
        self.assertEqual(rows, [1, 1, 2])

    def test_z_on_the_overview_freezes_the_slide_and_resuming_goes_on_from_it(self):
        self.three_slides()
        frames = self.run_main([b"Z", 100, 100, b"\x1b[C", 5, b"Z", 0])
        names = ["".join(f[1].split()) for f in frames]
        self.assertEqual(names[1], names[0])                                              # paused on this slide
        self.assertTrue("paused" in frames[1][0])
        self.assertEqual(names[2], "slideb")                                              # ← → still move a paused overview
        self.assertNotIn("paused", frames[-1][0])

    def test_moving_to_another_screen_or_an_idle_screen_ends_the_pause(self):
        frames = self.run_main([b"2", b"Z", b"3"])
        self.assertIn("paused", frames[2][0])
        self.assertNotIn("paused", frames[3][0])
        frames = self.run_main([b"2", b"Z", screens.MAP_IDLE_S + 5])
        self.assertEqual(self.shown(frames)[-1], "overview")
        self.assertNotIn("paused", frames[-1][0])

    # -- [ui] start_view: the screen the console opens at
    def start_view(self, name):
        render.CFG["ui"]["start_view"] = name
        self.addCleanup(render.CFG["ui"].pop, "start_view", None)

    def test_start_view_opens_that_screen_at_the_start_when_a_keyboard_is_there(self):
        for name in ("map", "cpu", "health", "ai"):
            self.start_view(name)
            frames = self.run_main([1, 1])
            self.assertEqual(self.shown(frames), [name] * 3, name)
        self.start_view("overview")
        self.assertEqual(self.shown(self.run_main([1])), ["overview"] * 2)  # the default: the rotation, as it was

    def test_start_view_needs_a_keyboard_and_a_feature_that_is_on(self):
        self.start_view("map")
        self.assertEqual(self.shown(self.run_main([1, 1], keyboard=False)), ["overview"] * 3)  # a monitor: nobody could close it
        render.CFG["features"]["map"] = False
        self.assertEqual(self.shown(self.run_main([1])), ["overview"] * 2)

    def test_esc_leaves_the_start_view_and_the_digits_go_where_they_say(self):
        self.start_view("health")
        frames = self.run_main([b"\x1b", 1, b"3", b"1"])
        self.assertEqual(self.shown(frames)[:2], ["health", "overview"])  # it is the start, not a home the console goes back to by itself
        self.assertEqual(self.shown(frames)[-2:], ["cpu", "overview"])

    def test_an_idle_screen_goes_back_to_the_start_view_not_to_the_rotation(self):
        self.start_view("health")
        frames = self.run_main([b"2", screens.MAP_IDLE_S + 5, 1])
        self.assertEqual(self.shown(frames), ["health", "map", "health", "health"])
        self.assertEqual(self.shown(self.run_main([render.HEALTH_IDLE_S + 5, 1])), ["health", "health", "health"])  # idle on the start itself: a fresh one
        render.CFG["ui"].pop("start_view")
        frames = self.run_main([b"2", screens.MAP_IDLE_S + 5, 1])
        self.assertEqual(self.shown(frames), ["overview", "map", "overview", "overview"])  # no start_view: the rotation, as it was

    def test_new_view_opens_one_screen_and_only_that_one(self):
        for name, cls in (("map", screens.MapView), ("cpu", render.CpuView), ("health", screens.HealthView), ("ai", screens.AiView)):
            got = render.new_view(name, 123.0)
            self.assertEqual([type(x) is cls if x is not None else None for x in got].count(True), 1)
            view = next(x for x in got if x is not None)
            self.assertEqual((view.opened, view.touched), (123.0, 123.0))
            self.assertEqual(len([x for x in got if x is None]), 3)

    # -- the AI question
    def test_a_question_that_waits_is_answered_by_the_next_key_and_the_global_keys_do_not_slip_past_it(self):
        frames = self.run_main([b"5", b"x", b"1", b"x", b"y", b"x", b"3"])
        self.assertEqual(self.shown(frames), ["overview"] + ["ai"] * 7)                  # neither 1 nor 3 left the AI screen: they said no
        self.assertEqual(self.calls["do"], ["delete", "delete", "yes", "delete"])

    def test_the_ai_screens_keys_reach_the_engine(self):
        self.run_main([b"5", b"e", b"u", b"X", b"c"])
        self.assertEqual(self.calls["do"], ["toggle", "use", "delete-all", "cancel"])

    # -- the keys are the same on every OS
    def test_key_names_decode_the_same_on_every_os(self):
        self.assertEqual(render.decode_keys(b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[5~\x1b[6~\x1b[H\x1b[F\t\x1b[Z\r\n \x1b?Z"),
                         ["up", "down", "right", "left", "pgup", "pgdn", "home", "end", "tab", "btab", "enter", "space", "esc", "?", "Z"])
        self.assertEqual([render.win_key("\xe0", n) for n in "HPKMGOIQ"], ["up", "down", "left", "right", "home", "end", "pgup", "pgdn"])
        self.assertEqual([render.win_key(ch) for ch in ("\t", "\r", "\x1b", " ", "q", "Z", "?", "1")], ["tab", "enter", "esc", "space", "q", "Z", "?", "1"])
        self.assertEqual(render.win_key("\x00", "\x0f"), "btab")
        self.assertEqual(render.win_key("\x00"), "")


if __name__ == "__main__":
    unittest.main()
