"""A remote host's view documents drawn by the console (src/remotedoc.py, render.remote_view): the golden equality with the local screens, the
real documents of the web API, the allowlist, and hostile documents.

THE test is `Golden`: for each of the five views, in the demo of each OS at three sizes, the local screen is drawn by the existing code, the
very components it drew are turned into the document (webapi.document, console flavour), read back by remotedoc and drawn by
render.remote_view: every line but the header is the same bytes. Everything runs in tests/golden.FrozenWorld (a clock that stands still,
the demo's data), no host state, no network."""
import contextlib
import json
import os
import random
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import time
import tracemalloc
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import golden  # noqa: E402
import ansi  # noqa: E402
import cards  # noqa: E402
import peers  # noqa: E402
import prefs  # noqa: E402
import problems  # noqa: E402
import remotedoc  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import ui  # noqa: E402
import webapi  # noqa: E402

LF = "\x1b[K\r\n"
SGR = re.compile(r"\x1b\[[0-9;]*m")
RESETS = re.compile(r"(?:\x1b\[0m){2,}")  # the screens cut their lines (ansi.clip ends each with a reset) and the frame cuts them again: the same on a terminal
OSES = ("linux", "windows", "darwin")
SIZES = ((79, 24), (120, 33), (200, 50))
VIEWS = ("overview", "cpu", "health", "map", "events")


# ---- the local screens, and the document of what they drew ------------------------------------------------------------------------------

@contextlib.contextmanager
def outermost_renders():
    """The components handed to ansi.render by the code under it, outermost calls only (the last one of a screen is its whole body)."""
    calls, depth, real = [], [0], ansi.render

    def spy(node, w):
        depth[0] += 1
        try:
            if depth[0] == 1:
                calls.append(node)
            return real(node, w)
        finally:
            depth[0] -= 1
    ansi.render = spy
    try:
        yield calls
    finally:
        ansi.render = real


def figures():
    """The key figures the frame of the screen that was just drawn has (render.KEEP: what it last read)."""
    st, sm = render.KEEP["st"], render.KEEP["sm"]
    pb = problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])
    return cards.kpis(render.make_ctx(st, sm, pb), prefs.effective(render.ui_cfg())[0]["kpis"])


def local_overview(world, w, h):
    """(the frame of the overview as the scrolling page draws it, its cards in order, the key figures): page_overview(scroll=True) is the
    layout the remote overview shares (every card at the richest level, in columns as even as possible)."""
    st, sm = render.snapshot(w), render.host_sample(None)
    render.demo_defaults()
    pb = problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
    built, real = [], cards.build

    def spy(n, ctx, k, caps=None):
        out = real(n, ctx, k, caps)
        if k == -2 and caps is not None and caps.full and out.id not in [x.id for x in built]:
            built.append(out)
        return out
    cards.build = spy
    try:
        body = render.page_overview(sm, st["cont"], st["net"], st["boot"], w, 10 ** 6, pb=pb, baseline=st["baseline"], now=golden.NOW, scroll=True)
    finally:
        cards.build = real
    ctx = render.make_ctx(st, sm, pb)
    ks = cards.kpis(ctx, prefs.effective(render.ui_cfg())[0]["kpis"])
    return render.frame(("Overview", 1, 1, body), 0, 1, w, h, pb, ctx=ctx), built, ks


def local_screen(view, argv, w, h):
    """(the frame, the components of its body) of the screen `view` as `render.py --once --view` draws it."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    if view == "cpu":
        feed = render.CpuFeed(settle=0.5)
        d = feed.read()
        cv = render.CpuView()
        cv.sort = opt("--sort") if opt("--sort") in render.CPU_SORTS else "cpu"
        cv.details = "--details" in argv
        if opt("--select"):
            render.cpu_select(render.cpu_rows(d["procs"]["procs"], cv.sort), cv, opt("--select"))
        probe = render.CpuView()
        probe.sort, probe.details, probe.cur, probe.idx = cv.sort, cv.details, cv.cur, cv.idx
        rows = render.cpu_rows(d["procs"]["procs"], probe.sort)
        render.cpu_sync(probe, rows)
        nodes = screens.cpu_view(d, render.cpu_ctx(), w, render.body_rows(h), probe.sort, probe.cur, probe.details, probe.top).nodes
        return render.cpu_once(argv, w, h), nodes
    once = {"map": render.map_once, "health": render.health_once, "events": render.events_once}[view]
    with outermost_renders() as calls:
        frame = once(argv, w, h)
    group = calls[-1]
    assert isinstance(group, ui.Group), group
    return frame, group.children


def document(view, nodes=None, cards_=None, ks=()):
    """The bytes of the console-flavour document of the components, as the API would serve them."""
    if view == "overview":
        body = {"host": "demo-host", "status": {"state": "ok", "text": "ALL OK"}, "stale": False, "kpis": list(ks), "cards": cards_,
                "layout": [{"id": x.id, "w": 1} for x in cards_]}
    else:
        body = {"nodes": nodes}
    return bytes(webapi.document(view, body, golden.NOW, console=True))


CASES = {  # view -> the arguments of the screens that have a state (a selection, a sort, the details pane)
    "overview": [()],
    "cpu": [("--view", "cpu"), ("--view", "cpu", "--sort", "mem", "--select", "java", "--details")],
    "health": [("--view", "health"), ("--view", "health", "--period", "30", "--select", golden.SELECTED["finding"], "--details")],
    "map": [("--view", "map"), ("--view", "map", "--expand", "fit", "--select", "shop-api", "--details"), ("--view", "map", "--expand", "all", "--only")],
    "events": [("--view", "events"), ("--view", "events", "--select", "8", "--details"), ("--view", "events", "--kind", "hang")],
}


class Golden(unittest.TestCase):
    """The remote screen is the local one, line for line, but for the header."""

    def check(self, view, os_name, cols, rows, args):
        w, h = cols - 1, rows
        with golden.FrozenWorld() as world:
            world.once(["--cols", str(cols), "--rows", str(rows), "--demo-os", os_name])  # sets the demo; its output is not used
            argv = list(args) + ["--demo-os", os_name]
            if view == "overview":
                frame, built, ks = local_overview(world, w, h)
                raw = document(view, cards_=built, ks=ks)
                nodes_in = built
            else:
                frame, nodes = local_screen(view, argv, w, h)
                ks = figures()
                raw = document(view, nodes=nodes)
                nodes_in = nodes
            local = frame.split(LF)
            doc = remotedoc.decode_bytes(raw)
            self.assertTrue(doc.ok, doc.nodes)
            self.assertEqual((doc.rejected, doc.cut), (0, False))
            decoded = doc.cards if view == "overview" else doc.nodes
            self.assertEqual(decoded, list(nodes_in), "the components came back different")  # equal as objects, not only as lines
            kpis = remotedoc.kpis({"flavour": "console", "kpis": webapi.data(ks, True)})
            remote = render.remote_view("nuc-b", doc, w, h, age_s=12, kpis=kpis, foot=local[-1])
        self.assertEqual(len(remote), len(local))
        self.assertEqual(remote[0], render.remote_header("nuc-b", 12, w))
        self.assertNotEqual(remote[0], local[0])
        for i, (a, b) in enumerate(zip(remote, local)):
            if i:
                self.assertEqual(RESETS.sub("\x1b[0m", a), RESETS.sub("\x1b[0m", b), "line %d of %s %s %dx%d %s" % (i, view, os_name, cols, rows, " ".join(args)))

    def test_every_view_every_os_every_size(self):
        n = 0
        for view in VIEWS:
            for args in CASES[view]:
                for os_name in OSES:
                    for cols, rows in SIZES:
                        with self.subTest(view=view, os=os_name, size="%dx%d" % (cols, rows), args=" ".join(args)):
                            self.check(view, os_name, cols, rows, args)
                            n += 1
        self.assertGreater(n, 60)

    def test_the_screens_are_not_empty(self):
        """A golden equality of two empty screens proves nothing: the lines have the words of the demo."""
        with golden.FrozenWorld() as world:
            world.once(["--cols", "120", "--rows", "33"])
            frame, built, _ks = local_overview(world, 119, 33)
        text = ansi.ANSI.sub("", "\n".join(render.remote_body(remotedoc.decode_bytes(document("overview", cards_=built)), 119)))
        for word in ("EXPOSURE", "FIREWALL", "SYSTEM", "CONTAINERS", "ATTENTION"):
            self.assertIn(word, text)
        self.assertGreater(len(built), 8)
        self.assertIn("EXPOSURE", ansi.ANSI.sub("", frame))


# ---- the real documents of the web API (the lossy flavour) ------------------------------------------------------------------------------

class ApiDocuments(unittest.TestCase):
    """What a peer really serves (web.Server.api in the demo): everything in it is allowed, and it is drawn at every size."""

    @classmethod
    def setUpClass(cls):
        cls.docs = {}
        with golden.FrozenWorld() as world:
            srv = world.server
            for name in VIEWS:
                cls.docs[name] = bytes(srv.api(name, {}))

    def test_nothing_the_demo_serves_is_refused(self):
        for name, raw in self.docs.items():
            with self.subTest(view=name):
                d = remotedoc.decode_bytes(raw)
                self.assertTrue(d.ok)
                self.assertEqual((d.rejected, d.cut, d.flavour), (0, False, "api"))
                self.assertEqual(d.view, name)

    def test_drawn_at_every_size(self):
        for name, raw in self.docs.items():
            for cols, rows in SIZES + ((60, 20), (300, 80)):
                with self.subTest(view=name, size=(cols, rows)):
                    w = cols - 1
                    lines = render.remote_view("nuc-b", json.loads(raw), w, rows, age_s=3)
                    self.assertEqual(len(lines), rows)
                    for line in lines:
                        self.assertLessEqual(ansi.vlen(line), w)
                        self.assertNotIn("\x1b", SGR.sub("", line))
                    self.assertTrue(lines[0].startswith("\x1b[1;7;35m"))

    def test_the_overview_has_every_card_and_its_title(self):
        d = remotedoc.decode_bytes(self.docs["overview"])
        text = ansi.ANSI.sub("", "\n".join(render.remote_body(d, 199)))
        self.assertGreater(len(d.cards), 8)
        for card in d.cards:
            self.assertIn(card.title, text)

    def test_the_tables_of_the_api_flavour_line_up(self):
        """The API drops the widths: each column is as wide as its widest cell (remotedoc._fit), so the columns line up."""
        col = lambda k: {"type": "Col", "key": k, "label": k.upper()}  # noqa: E731
        row = lambda a, b: {"type": "Row", "cells": [{"type": "Span", "text": a}, {"type": "Span", "text": b}]}  # noqa: E731
        table = {"type": "Table", "head": True, "cols": [col("a"), col("b")], "rows": [row("x", "1"), row("a long cell", "2"), row("mid", "3")]}
        lines = [ansi.ANSI.sub("", x) for x in render.remote_body({"api": 1, "view": "cpu", "nodes": [table]}, 80)]
        self.assertEqual({x.index(y) for x, y in zip(lines, ("B", "1", "2", "3"))}, {13})  # 1 (indent) + 11 (the widest cell) + 1 (gap)
        self.assertEqual(len(lines), 4)

    def test_what_the_api_flavour_cannot_carry_is_said(self):
        """Parts side by side have no widths in the API: they are stacked, never overlapped; and the console's own wrappers are simply absent."""
        ok = lambda t: {"type": "Msg", "level": "ok", "text": t}  # noqa: E731
        doc = {"api": 1, "view": "cpu", "nodes": [{"type": "Cols", "children": [ok("a"), ok("b")]},
                                                  {"type": "Split", "left": [ok("l")], "right": [ok("r")], "sep": " | "}]}
        out = [ansi.ANSI.sub("", x).strip() for x in render.remote_body(doc, 80)]
        self.assertEqual(out, ["\u2714 a", "\u2714 b", "\u2714 l", "\u2714 r"])


# ---- the allowlist -------------------------------------------------------------------------------------------------------------------

LEFT_OUT = {  # the components that are not allowed, and why
    "Badge": "the AI screen's verdict pill", "Action": "a button that posts a form: the console follows none", "Controls": "buttons",
    "Question": "a form", "Spec": "the AI screen's details", "Qa": "the AI chat", "Prompt": "the AI chat", "Log": "the AI chat"}
LINKS = {"href", "mark_href", "close", "close_href", "no_href", "prompt_href"}


def components():
    out, todo = {}, [ui.Component]
    while todo:
        for k in todo.pop().__subclasses__():
            out[k.__name__] = k
            todo.append(k)
    return out


class Allowlist(unittest.TestCase):
    def test_every_component_is_decided(self):
        """A new class in ui.py is either read by remotedoc or listed above with its reason: nobody forgets one."""
        names = set(components())
        self.assertEqual(sorted(names - set(remotedoc.ALLOWED) - set(LEFT_OUT)), [])
        self.assertEqual(sorted((set(remotedoc.ALLOWED) | set(LEFT_OUT)) - names - {"Raw"}), [])
        self.assertFalse(set(remotedoc.ALLOWED) & set(LEFT_OUT))

    def test_every_field_is_read_or_is_a_link(self):
        for name in remotedoc.ALLOWED:
            cls = components()[name] if name != "Raw" else ui.Raw
            read = {f for f, _p, _d in remotedoc.SPECS[name][1]} | {"Accepted": {"level"}, "RichMsg": {"text"}}.get(name, set())  # worked out by the constructor
            theirs = set(webapi.fields(cls, True))
            with self.subTest(cls=name):
                self.assertEqual(sorted(theirs - read - LINKS), [])
                self.assertEqual(sorted(read - theirs - {"widths"}), [])

    def test_the_api_version_and_the_views_are_the_clients(self):
        self.assertEqual(remotedoc.API, webapi.VERSION)
        self.assertEqual(remotedoc.VIEWS, peers.VIEWS)
        for banned in ("ai", "telegram", "settings", "summary"):
            self.assertNotIn(banned, remotedoc.VIEWS)

    def test_no_name_of_the_data_is_ever_looked_up_as_an_attribute(self):
        with open(os.path.join(os.path.dirname(remotedoc.__file__), "remotedoc.py"), encoding="utf-8") as f:
            code = re.sub(r'"""(.|\n)*?"""|#.*', "", f.read())
        for banned in (r"\bgetattr\(", r"\bsetattr\(", r"\beval\(", r"\bexec\(", r"__import__", r"\bimportlib\b", r"\bpickle\b", r"\bglobals\(", r"\blocals\(",
                       r"(?<!re\.)\bcompile\(", r"\bsubprocess\b", r"\bsocket\b", r"\bopen\("):
            self.assertIsNone(re.search(banned, code), banned)

    def test_an_unknown_class_is_a_visible_line_not_an_error(self):
        doc = {"api": 1, "view": "cpu", "nodes": [{"type": "Evil", "text": "x"}, {"type": "__class__"}, {"type": ["Msg"]}, "Msg", 5, None,
                                                  {"type": "Msg", "level": "ok", "text": "fine"}, {"type": "Action", "action": "/x", "label": "go"}]}
        d = remotedoc.decode(doc)
        self.assertTrue(d.ok)
        self.assertEqual(d.rejected, 7)
        text = [ansi.ANSI.sub("", x) for x in render.remote_body(d, 80)]
        self.assertEqual(sum("unsupported" in x for x in text), 7)
        self.assertTrue(any("fine" in x for x in text))

    def test_a_table_of_other_shape_is_refused_not_drawn_ragged(self):
        bad = {"type": "Table", "cols": [{"type": "Col", "key": "a", "label": "A"}], "rows": [{"type": "Row", "cells": []}]}
        d = remotedoc.decode({"api": 1, "view": "cpu", "nodes": [bad]})
        self.assertEqual(d.rejected, 1)
        self.assertIsInstance(d.nodes[0], ui.Msg)

    def test_strict_types_and_ranges(self):
        node = lambda **kw: remotedoc.decode({"api": 1, "view": "cpu", "nodes": [dict({"type": "Bar", "frac": 0.5}, **kw)]}).nodes[0]  # noqa: E731
        self.assertIsInstance(node(w=True), ui.Msg)                      # a bool is not a number
        self.assertIsInstance(node(w="9"), ui.Msg)
        self.assertEqual(node(w=10 ** 12).w, 200)                        # clamped
        self.assertEqual(node(w=-5).w, 1)
        self.assertIsNone(node(frac=float("nan")).frac)                  # not a number: unknown
        self.assertEqual(node(frac=7).frac, 1.0)
        self.assertEqual(node(frac=-1).frac, 0.0)
        self.assertEqual(node(tone="nonsense").tone, None)               # a word of a newer version: neutral
        card = remotedoc.decode({"api": 1, "view": "overview", "cards": [{"type": "Card", "id": "x", "state": "radiant"}]}).cards[0]
        self.assertEqual(card.state, "unknown")                          # never green

    def test_older_and_newer_versions(self):
        doc = {"api": 1, "view": "cpu", "future": {"x": 1}, "nodes": [{"type": "Msg", "level": "ok", "text": "t", "newfield": [1, 2], "href": "http://x/"}]}
        d = remotedoc.decode(doc)
        self.assertEqual(d.nodes, [ui.Msg("ok", "t")])                   # extra fields ignored
        self.assertEqual(remotedoc.decode({"api": 1, "view": "cpu", "nodes": [{"type": "Pill"}]}).nodes, [ui.Pill("", "unknown")])  # defaults
        newer = remotedoc.decode({"api": 2, "view": "cpu", "nodes": [{"type": "Msg", "level": "ok", "text": "t"}]})
        self.assertFalse(newer.ok)
        self.assertEqual(newer.notice, "incompatible")
        self.assertIn("update this console", newer.nodes[0].text)
        self.assertEqual(newer.api, 2)
        for v in ("ai", "telegram", "settings", "summary", "", None, 5, ["cpu"]):
            d = remotedoc.decode({"api": 1, "view": v, "nodes": []})
            self.assertFalse(d.ok, v)
            self.assertEqual(d.notice, "view")
        for api in (None, 0, -1, True, "1", 1.5, [1]):
            self.assertEqual(remotedoc.decode({"api": api, "view": "cpu"}).notice, "invalid", api)
        for junk in (None, 5, "x", [], [{"api": 1}], 1.5, True):
            self.assertEqual(remotedoc.decode(junk).notice, "invalid")

    def test_text_is_clean(self):
        evil = "a\x1b[2Jb\x9bc\x07d\x1b]0;title\x07e‮f​ǵh\ud800iあj\x00k\nl\tm n"
        d = remotedoc.decode({"api": 1, "view": "cpu", "nodes": [{"type": "Msg", "level": "err", "text": evil}]})
        got = d.nodes[0].text
        self.assertEqual(got, "a?[2Jb?c?d?]0;title?e?f?\u01f5h?i?j?k?l?m?n")
        for ch in got:
            self.assertGreaterEqual(ord(ch), 32)
            self.assertLess(ord(ch), 0x3000)
        raw = remotedoc.decode({"api": 1, "view": "cpu", "nodes": [{"type": "Raw", "lines": ["\x1b[31mred\x1b[0m \x1b]8;;http://x\x07z"]}]})
        self.assertEqual(raw.nodes[0].lines, ["red ?]8;;http://x?z"])    # the colours of a Raw block go, whatever is left is text
        self.assertEqual(remotedoc.clean("café ✔ ──"), "café ✔ ──")  # what the screens write themselves stays
        out = remotedoc.clean("é")
        self.assertEqual(out, "é")                                  # a combined letter is composed, not dropped

    def test_links_are_never_read(self):
        doc = {"api": 1, "view": "map", "nodes": [{"type": "Outline", "rows": [{"type": "Branch", "key": "k", "href": "http://evil/", "mark_href": "/x",
                                                                               "mark": {"type": "Span", "text": "+"}, "body": {"type": "Line", "spans": []}}]},
                                                  {"type": "More", "n": 3, "what": "rows", "href": "http://evil/"}]}
        d = remotedoc.decode(doc)
        self.assertIsNone(d.nodes[0].rows[0].href)
        self.assertIsNone(d.nodes[0].rows[0].mark_href)
        self.assertIsNone(d.nodes[1].href)


# ---- header, banners ---------------------------------------------------------------------------------------------------------------------

class Marker(unittest.TestCase):
    def test_the_header_is_never_the_local_one(self):
        line = render.remote_header("nuc-b", 12, 60)
        self.assertEqual(ansi.ANSI.sub("", line), " ⇄ nuc-b (remote 12s)".ljust(60))
        self.assertTrue(line.startswith("\x1b[1;7;35m"))                 # bold, reverse video, magenta: no status pill has this code
        for code in ui.ANSI_THEMES["default"].values():
            self.assertNotEqual(line[2:line.index("m")], code)
        self.assertEqual(render.remote_header("a", 5, 0), "\x1b[1;7;35m ⇄ a (remote 5s) \x1b[0m")
        for age, shown in ((0, "0s"), (119, "119s"), (120, "2m"), (7199, "119m"), (7200, "2h"), (200000, "2d"), (None, "?"), (-3, "?"),
                           (float("nan"), "?"), (True, "?"), ("x", "?")):
            self.assertIn("(remote %s)" % shown, render.remote_header("n", age))

    def test_the_name_cannot_forge_a_line(self):
        line = render.remote_header("x\x1b[0m\n‮" + "n" * 100, 1, 80)
        self.assertEqual(len(ansi.ANSI.sub("", line)), 80)
        self.assertNotIn("\n", line)
        self.assertEqual(SGR.sub("", line).count("\x1b"), 0)

    def test_banners(self):
        ok = {"state": "ok", "insecure": False, "identity": "ok"}
        self.assertEqual(render.remote_banner(ok, 80), [])
        got = [ansi.ANSI.sub("", x).strip() for x in render.remote_banner(dict(ok, state="stale", insecure=True, identity="changed"), 80)]
        self.assertEqual(len(got), 3)
        self.assertTrue(got[0].startswith("✖ IDENTITY CHANGED"))
        self.assertTrue(got[1].startswith("! STALE"))
        self.assertTrue(got[2].startswith("! INSECURE"))
        for state in ("unreachable", "unauthorized", "untrusted", "incompatible", "down"):
            self.assertEqual(len(render.remote_banner(dict(ok, state=state), 80)), 1, state)
        self.assertEqual(len(render.remote_banner("junk", 80)), 1)       # not an entry: untrusted
        for line in render.remote_banner(dict(ok, state="stale", insecure=True, identity="changed"), 40):
            self.assertEqual(ansi.vlen(line), 40)

    def test_banners_take_room_from_the_body_not_from_the_footer(self):
        doc = {"api": 1, "view": "cpu", "nodes": [{"type": "Msg", "level": "ok", "text": "line %d" % i} for i in range(40)]}
        banner = render.remote_banner({"state": "stale", "identity": "changed", "insecure": True}, 70)
        lines = render.remote_view("n", doc, 70, 24, age_s=1, banner=banner)
        self.assertEqual(len(lines), 24)
        self.assertEqual(lines[1:4], banner)
        self.assertIn("remote host", lines[-1])
        self.assertEqual(len(render.remote_view("n", doc, 70, 3, banner=banner * 5)), 3)

    def test_the_pager(self):
        doc = {"api": 1, "view": "cpu", "nodes": [{"type": "Msg", "level": "ok", "text": "line %d" % i} for i in range(100)]}
        a = render.remote_view("n", doc, 60, 24, top=0)
        b = render.remote_view("n", doc, 60, 24, top=10)
        c = render.remote_view("n", doc, 60, 24, top=10 ** 9)
        self.assertIn("line 0", a[1])
        self.assertIn("line 10", b[1])
        self.assertIn("line 99", c[-2])                                  # past the end: the last page
        self.assertIn("lines 1-", a[-1])
        self.assertIn("of 100", a[-1])
        self.assertEqual(len(render.remote_view("n", doc, 60, 24, top=-5)), 24)


# ---- hostile documents ---------------------------------------------------------------------------------------------------------------------

def mutate(rnd, v, budget=4):
    """A copy of the JSON value with a few random hostile changes (type swaps, big numbers, NaN, control characters, unknown classes)."""
    big = ["\x1b[2J\x1b[31m" * 50, "\x9b31m", "\x1b]0;x\x07", "A" * 100000, "‮" * 100, "\ud800", "あ" * 300, "\x00\x01\x02" * 100]
    nums = [10 ** 30, -10 ** 30, 1e308, -1e308, float("nan"), float("inf"), 0, -1, 2 ** 63, 1e-300]

    def walk(x, depth=0):
        if depth > 60:
            return x
        if isinstance(x, dict):
            out = {}
            for k, val in x.items():
                r = rnd.random()
                if r < 0.01 * budget:
                    continue
                out[k] = walk(val, depth + 1)
            if rnd.random() < 0.02 * budget:
                out[rnd.choice(["type", "x", "children", "w", "rows", "cells", "href"])] = rnd.choice(
                    [None, True, [], {}, "Evil", ["Msg"], rnd.choice(nums), rnd.choice(big)])
            if "type" in out and rnd.random() < 0.01 * budget:
                out["type"] = rnd.choice(["Evil", "__class__", "Action", "Raw", "Table", "Cols", "Split", "Card", 5, None, [], {}])
            return out
        if isinstance(x, list):
            out = [walk(y, depth + 1) for y in x]
            if out and rnd.random() < 0.02 * budget:
                out = out * rnd.choice([2, 3, 50])
            if rnd.random() < 0.01 * budget:
                rnd.shuffle(out)
            return out
        r = rnd.random()
        if r < 0.03 * budget:
            return rnd.choice([None, True, False, [], {}, "x", rnd.choice(nums), rnd.choice(big), [[[[]]]], {"type": "Msg", "text": "n"}])
        if isinstance(x, str) and r < 0.05 * budget:
            return x + rnd.choice(big)
        return x
    return walk(v)


def assert_drawable(test, doc, w=80, h=24):
    """The public functions never raise, and what they give is lines of at most w columns that carry no escape sequence but ours."""
    lines = render.remote_view("peer", doc, w, h, age_s=1)
    test.assertEqual(len(lines), h)
    for line in lines:
        test.assertLessEqual(ansi.vlen(line), w)
        bare = SGR.sub("", line)
        test.assertNotIn("\x1b", bare)
        for ch in bare:
            test.assertTrue(ch >= " " and not "\x7f" <= ch <= "\x9f", repr(ch))
            test.assertLess(ord(ch), 0x3000 if ch not in "─│" else 0x10000)
        bare.encode("utf-8")  # a lone surrogate cannot be written


class Hostile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {}
        with golden.FrozenWorld() as world:
            srv = world.server
            for name in VIEWS:
                cls.docs[name] = json.loads(bytes(srv.api(name, {})))

    def test_seeded_mutations_of_the_demo_documents(self):
        rnd = random.Random(20261025)
        n = 0
        for name, doc in self.docs.items():
            for _ in range(60):
                bad = mutate(rnd, doc, budget=rnd.choice([1, 3, 8]))
                t0 = time.perf_counter()
                assert_drawable(self, bad, rnd.choice([40, 80, 120]), rnd.choice([10, 24, 40]))
                self.assertLess(time.perf_counter() - t0, 2.0)
                n += 1
        self.assertEqual(n, 300)

    def test_the_demo_documents_in_the_console_flavour_too(self):
        with golden.FrozenWorld() as world:
            world.once(["--cols", "120", "--rows", "33"])
            frame, built, ks = local_overview(world, 119, 33)
            base = {"api": 1, "view": "overview", "flavour": "console", "cards": webapi.data(built, True), "kpis": webapi.data(ks, True),
                    "layout": [{"id": x.id, "w": 1} for x in built]}
        rnd = random.Random(7)
        for _ in range(60):
            assert_drawable(self, mutate(rnd, base, budget=rnd.choice([1, 3, 8])), 100, 30)

    def test_garbage_at_the_top(self):
        for junk in (None, 1, "x", [], {}, {"api": 1}, {"api": 1, "view": "overview", "cards": 5, "kpis": "x", "layout": {}},
                     {"api": 1, "view": "overview", "cards": [None, 1, "x", [], {}]}, {"api": 1, "view": "cpu", "nodes": {"type": "Msg"}},
                     {"api": 10 ** 400, "view": "cpu"}, {"api": 1, "view": "cpu", "nodes": [[]] * 10}):
            assert_drawable(self, junk)

    def test_bytes(self):
        for raw in (b"", b"\xff\xfe", b"{", b"[1,2", b'{"api": NaN}', b'{"api": 1, "view": "cpu", "nodes": [], "x": Infinity}', "é".encode("latin1"),
                    b'{"api": 1' + b"0" * 5000 + b', "view": "cpu"}', b"[" * 200000, b'{"a":' * 100000, None, "str", 5):
            d = remotedoc.decode_bytes(raw)
            self.assertFalse(d.ok)
            self.assertEqual(d.notice, "invalid")
        self.assertFalse(remotedoc.decode_bytes(b" " * (remotedoc.MAX_BYTES + 1)).ok)

    def test_nesting_100k_levels(self):
        deep = {"type": "Msg", "level": "ok", "text": "bottom"}
        for _ in range(100000):
            deep = {"type": "Group", "children": [deep]}
        t0 = time.perf_counter()
        d = remotedoc.decode({"api": 1, "view": "cpu", "nodes": [deep]})
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(d.ok)
        self.assertEqual(d.rejected, 1)                                  # refused at MAX_DEPTH, with its line
        assert_drawable(self, {"api": 1, "view": "cpu", "nodes": [deep]})
        lst = []
        for _ in range(100000):
            lst = [lst]
        assert_drawable(self, {"api": 1, "view": "cpu", "nodes": lst})
        assert_drawable(self, {"api": 1, "view": "overview", "cards": lst, "layout": lst, "kpis": lst})
        del deep, lst

    def test_a_node_nested_to_the_cap_is_still_drawn(self):
        node = {"type": "Msg", "level": "ok", "text": "bottom"}
        for _ in range(remotedoc.MAX_DEPTH - 2):
            node = {"type": "Group", "children": [node]}
        d = remotedoc.decode({"api": 1, "view": "cpu", "nodes": [node]})
        self.assertEqual(d.rejected, 0)

    def test_repeated_big_arrays_hit_the_caps_not_the_memory(self):
        row = {"type": "Row", "cells": [{"type": "Span", "text": "x" * 1000}] * 5}
        table = {"type": "Table", "cols": [{"type": "Col", "key": str(i), "label": "c"} for i in range(5)], "rows": [row] * 40000}
        doc = {"api": 1, "view": "cpu", "nodes": [table] * 3 + [{"type": "Group", "children": [{"type": "Span", "text": "y" * 100000}] * 200}]}
        raw = json.dumps(doc).encode()
        self.assertGreater(len(raw), 2 * 1024 * 1024)
        doc = json.loads(raw)
        tracemalloc.start()
        try:
            t0 = time.perf_counter()
            lines = render.remote_view("peer", doc, 100, 40)
            took = time.perf_counter() - t0
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(lines), 40)
        self.assertLess(took, 5.0)   # tracemalloc slows the run down about three times: the bound outside it is the ~1 s below
        self.assertLess(peak, 150 * 1024 * 1024)
        d = remotedoc.decode(doc)
        self.assertTrue(d.cut)
        self.assertLessEqual(remotedoc.MAX_NODES, 20000)

    def test_two_megabytes_in_about_a_second(self):
        rnd = random.Random(3)
        rows = [{"type": "Row", "cells": [{"type": "Span", "text": "".join(rnd.choice("abcdef ") for _ in range(60)), "tone": "ok"} for _ in range(4)]}
                for _ in range(4300)]
        table = {"type": "Table", "cols": [{"type": "Col", "key": str(i), "label": "c%d" % i, "w": 30} for i in range(4)], "rows": rows}
        raw = json.dumps({"api": 1, "view": "cpu", "nodes": [table]}).encode()
        self.assertGreater(len(raw), 1500000)
        self.assertLessEqual(len(raw), remotedoc.MAX_BYTES)
        t0 = time.perf_counter()
        d = remotedoc.decode_bytes(raw)
        lines = render.remote_view("peer", d, 120, 40)
        took = time.perf_counter() - t0
        self.assertTrue(d.ok)
        self.assertEqual(len(lines), 40)
        self.assertLess(took, 1.0 + 3.0)  # 1 s on a quiet machine; the margin is for a loaded CI runner

    def test_a_timeline_cannot_ask_for_a_gigabyte(self):
        doc = {"api": 1, "view": "cpu", "nodes": [{"type": "Timeline", "parts": [["a", 1e9]], "total": 0.001, "w": 200}]}
        t0 = time.perf_counter()
        assert_drawable(self, doc)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_a_10_megabyte_string_is_cut_where_it_is_read(self):
        doc = {"api": 1, "view": "cpu", "nodes": [{"type": "Msg", "level": "ok", "text": "A" * (10 * 1024 * 1024)}]}
        t0 = time.perf_counter()
        d = remotedoc.decode(doc)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(len(d.nodes[0].text), 2000)

    def test_widths_are_small_numbers(self):
        for node in ({"type": "Grid", "items": [{"type": "Span", "text": "x"}] * 100, "cw": 10 ** 9, "per": 10 ** 9, "gap": 10 ** 9, "lead": 10 ** 9},
                     {"type": "KV", "pairs": [["a", {"type": "Span", "text": "b"}]], "lw": 10 ** 9, "indent": 10 ** 9, "cols": 10 ** 9},
                     {"type": "Indent", "n": 10 ** 9, "children": [{"type": "Msg", "level": "ok", "text": "x"}]},
                     {"type": "Split", "left": [], "right": [], "lw": 10 ** 9, "h": 10 ** 9},
                     {"type": "Cols", "children": [{"type": "Msg", "level": "ok", "text": "x"}] * 3, "widths": [10 ** 9] * 3, "gap": 10 ** 9, "h": 10 ** 9},
                     {"type": "Series", "values": [1] * 10, "w": 10 ** 9}, {"type": "Meter", "parts": [[10 ** 9, "user"]], "w": 10 ** 9},
                     {"type": "Wrap", "items": [{"type": "Span", "text": "x"}] * 50, "indent": 10 ** 9, "max_lines": 10 ** 9},
                     {"type": "Cap", "n": 10 ** 12, "children": []}, {"type": "Pane", "level": "err", "title": "t", "what": "w", "h": 10 ** 12}):
            t0 = time.perf_counter()
            tracemalloc.start()
            try:
                assert_drawable(self, {"api": 1, "view": "cpu", "nodes": [node], "flavour": "console"})
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 60 * 1024 * 1024, node["type"])
            self.assertLess(time.perf_counter() - t0, 2.0, node["type"])

    def test_wide_characters_cannot_make_a_line_wider_than_the_terminal(self):
        doc = {"api": 1, "view": "cpu", "nodes": [{"type": "Msg", "level": "ok", "text": "あ" * 80 + "\U0001f600" * 20 + "é" * 20}]}
        for line in render.remote_view("pああ", doc, 40, 5):
            self.assertLessEqual(ansi.vlen(line), 40)
            self.assertTrue(all(ord(ch) < 0x3000 or ch in "─│" for ch in ansi.ANSI.sub("", line)))


if __name__ == "__main__":
    unittest.main()
