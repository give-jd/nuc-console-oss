"""The AI screen as components (src/screens.py): the model (rows, view, keys, the pieces of the screen), what the console draws from it at several
sizes on the three demo machines, the components it added to ui.py (Badge, Spec, Question and the web's Action, Controls, Qa) drawn by ansi.py, and
what the web shell draws from the same data (a real page, no <pre>, escaped, forms with their CSRF token, nothing to click when it is locked).

Hermetic: the demo machines at a clock that stands still, the demo's engine in a temporary folder (tests/golden.py's FrozenWorld), nothing is
downloaded or started.
"""
import copy
import html as stdhtml
import os
import re
import sys
import unittest
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golden  # noqa: E402
import ansi  # noqa: E402
import htmlview  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import test_ai_screen as T  # noqa: E402
import ui  # noqa: E402
import webjs  # noqa: E402

SGR = re.compile(r"\x1b\[[0-9;]*m")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f-\x9f]")
EVIL = "<script>alert(1)</script>"
SIZES = ((79, 24), (120, 33), (200, 50), (226, 50), (40, 12))


def plain(lines):
    return [SGR.sub("", x) for x in lines]


def one(line):
    return SGR.sub("", line)


class Machine(object):
    """The demo's machine `os_name` in a frozen world: its catalog, its rows, its status (with the engine's snapshot)."""

    def __init__(self, os_name=None, world=None, cat=None):
        render.DEMO, render.DEMO_OS = True, os_name
        render._AI.clear()
        self.cat = cat if cat is not None else render.ai_engine().demo_catalog(os_name)
        self.data = {"cat": self.cat, "msg": "", "err": False, "at": golden.NOW}
        self.rows = screens.ai_rows(self.cat)
        self.st = render.ai_status()

    def view(self, **kw):
        av = screens.AiView(now=golden.NOW)
        for k, v in kw.items():
            setattr(av, k, v)
        return av

    def lines(self, w, h, av=None, st=None):
        return screens.ai_lines(self.data, self.st if st is None else st, av or self.view(), self.rows, w, h)


def hand():
    """tests/test_ai_screen.py's hand-made catalog, a copy of its own (it shares the demo's hardware: a test must not change that)."""
    return copy.deepcopy(T.catalog())


class Model(unittest.TestCase):
    def setUp(self):
        self.world = golden.FrozenWorld()
        self.world.__enter__()
        render.CFG["features"]["ai"] = True

    def tearDown(self):
        self.world.__exit__(None, None, None)

    def test_the_verdict_is_a_pill_with_a_symbol_and_a_tone_that_follows_it(self):
        want = {"gpu": ("✔ FITS GPU", "ok"), "partial": ("◐ GPU+CPU", "accent"), "ram": ("✔ FITS RAM", "ok"), "slow": ("! SLOW", "warn"),
                "no": ("✖ TOO BIG", "err"), None: ("? UNKNOWN", "muted")}
        for verdict, (text, tone) in want.items():
            badge = screens.ai_badge(verdict)
            self.assertEqual((badge.text, badge.tone), (text, tone), verdict)
            self.assertEqual(screens.ai_tone(verdict), tone)
            self.assertIn(text[0], "✔◐!✖?")  # a symbol besides the colour

    def test_the_title_gives_up_its_tagline_before_its_count(self):
        m = Machine()
        full = ansi.render(screens.ai_title(m.rows, 200), 200)[0][0]
        self.assertIn("what this machine can run", SGR.sub("", full))
        self.assertIn("12 models", SGR.sub("", full))
        narrow = SGR.sub("", ansi.render(screens.ai_title(m.rows, 60), 60)[0][0])
        self.assertNotIn("what this machine can run", narrow)
        self.assertIn("12 models", narrow)
        self.assertLessEqual(len(narrow), 60)
        tiny = SGR.sub("", ansi.render(screens.ai_title(m.rows, 20), 20)[0][0])
        self.assertEqual(len(tiny), 20)
        web = screens.ai_title(m.rows, None)
        self.assertEqual(len(web.parts), 3)  # the web has the room: tagline, count and the verdicts
        self.assertEqual(len(screens.ai_title(None, 80).parts), 0)

    def test_the_table_has_the_marks_the_name_and_the_columns_that_fit_and_the_cursor_is_solid(self):
        m = Machine()
        lay = screens.ai_layout(m.rows, 200)
        table = screens.ai_table(m.rows, lay, 0, 5, 2, 200)
        self.assertEqual([c.key for c in table.cols], ["marks", "name", "params", "size", "need", "verdict", "tok", "notes"])
        self.assertEqual([r.tone for r in table.rows], [None, None, "sel", None, None])
        self.assertEqual([r.key for r in table.rows], [r["id"] for r in m.rows[:5]])
        self.assertEqual((table.solid, table.clip), (200, 200))
        self.assertTrue(all(isinstance(r.cells[5], ui.Badge) for r in table.rows))
        lines = ansi.render(table, 200)[0]
        self.assertTrue(lines[2].startswith("\x1b[7m") and one(lines[2]) == one(lines[2]).ljust(200))  # reverse video, as wide as the list
        self.assertTrue(all(x.endswith("\x1b[0m") for x in lines))
        narrow = screens.ai_table(m.rows, screens.ai_layout(m.rows, 28), 0, 3, 0, 28)
        self.assertEqual([c.key for c in narrow.cols], ["marks", "name", "end"])  # only the name is left: it keeps its two spaces
        self.assertTrue(all(one(x).endswith("  ") for x in ansi.render(narrow, 28)[0][1:]))

    def test_a_download_is_a_bar_of_fourteen_columns_and_the_lock_takes_the_cancel_hint(self):
        m = Machine()
        snap = dict(m.st["snap"], state=("working", "downloading"), busy=True, job={"state": "running", "kind": "download", "phase": "downloading",
                                                                                       "done": 50, "total": 100, "rate": 1.0, "pct": 50, "eta": 5})
        st = dict(m.st, snap=snap)
        line = ansi.render(screens.ai_work_nodes(st, m.cat, m.view(), 120, 0)[0], 120)[0][0]
        self.assertIn("\x1b[36m" + "█" * 7 + "\x1b[0m\x1b[90m" + "░" * 7, line)
        self.assertIn("(c: cancel)", line)
        self.assertIn("◐ WORKING", line)
        locked = dict(m.st, snap=dict(snap, locked=True))
        line = SGR.sub("", ansi.render(screens.ai_work_nodes(locked, m.cat, m.view(), 120, 2)[0], 120)[0][0])
        self.assertNotIn("(c: cancel)", line)
        self.assertIn("[locked by config.ini]", line)
        self.assertEqual(screens.ai_work_nodes({}, m.cat, m.view(), 120, 0), [])  # no engine snapshot: no line

    def test_what_cannot_be_read_is_a_question_mark_never_fine(self):
        cat = hand()
        cat["hw"] = {"os": "linux", "cpu": {}, "ram": {}, "gpus": [{"name": "g"}]}
        m = Machine(cat=cat)
        text = "\n".join(plain(ansi.render(ui.Group(screens.ai_hw_nodes(cat["hw"], 120, 0)), 120)[0]))
        self.assertIn("?", text)
        self.assertIn("could not be read", text)
        self.assertNotIn("free of", text)
        st = screens.ai_status_parts({"enabled": True, "probe": None, "model": "gone"}, cat, {"m-gpu"})
        self.assertEqual(st["short"].text, "· checking…")
        self.assertEqual(st["model"][-1].tone, "warn")  # not in the catalog
        self.assertIn("not in the catalog", st["model"][-1].text)
        self.assertEqual(screens.ai_mb(None), "?")
        self.assertEqual(screens.ai_tok(None), "-")
        self.assertEqual(m.rows[0]["size_mb"], 4400.0)

    def test_the_details_are_a_spec_with_the_commands_whole(self):
        m = Machine()
        row = next(r for r in m.rows if not r["installed"] and r["pinned"] and "install" in r["commands"])
        spec = screens.ai_spec(row, False, 12)
        by = {label: (text, tone, whole) for label, text, tone, whole in spec.items}
        self.assertEqual(by["install"][1:], ("accent", True))
        self.assertEqual(by["verdict"][1], screens.ai_tone(row["verdict"]))
        self.assertEqual(spec.h, 12)
        win = screens.ai_spec(row, True)
        self.assertIn("prompt", [i[0] for i in win.items])

    def test_the_footer_is_the_question_while_one_waits_and_loses_the_acting_keys_when_locked(self):
        m = Machine()
        av = m.view(confirm=("delete", "x", "Delete the files of Alpha (4.4 GB)?"))
        foot = screens.ai_footer(av, 5, 120, m.st["snap"])
        self.assertIsInstance(foot, ui.Question)
        self.assertEqual(SGR.sub("", ansi.render(foot, 120)[0][0]), " Delete the files of Alpha (4.4 GB)?  y: yes   any other key: no")
        self.assertTrue(SGR.sub("", ansi.render(foot, 50)[0][0]).endswith("[y/n]"))
        keys = lambda snap: SGR.sub("", ansi.render(screens.ai_footer(m.view(), 5, 200, snap, render.on), 200)[0][0])  # noqa: E731
        self.assertIn("e: on/off", keys(m.st["snap"]))
        self.assertNotIn("e: on/off", keys(dict(m.st["snap"], locked=True)))
        self.assertNotIn("x: del", keys(dict(m.st["snap"], locked=True)))
        self.assertEqual(SGR.sub("", render.ai_footer(av, 5, 120, m.st["snap"])), SGR.sub("", ansi.render(foot, 120)[0][0]))


class Console(unittest.TestCase):
    def setUp(self):
        self.world = golden.FrozenWorld()
        self.world.__enter__()
        render.CFG["features"]["ai"] = True

    def tearDown(self):
        self.world.__exit__(None, None, None)

    def test_every_size_and_machine_fits_and_keeps_its_parts(self):
        for os_name in (None, "windows", "darwin"):
            m = Machine(os_name)
            for details in (False, True):
                for w, h in SIZES:
                    av = m.view(details=details)
                    lines = m.lines(w, h, av)
                    text = plain(lines)
                    self.assertLessEqual(len(text), h, (os_name, w, h))
                    self.assertTrue(all(len(x) <= w for x in text), (os_name, w, h, details))
                    self.assertIn("AI", text[0])
                    self.assertTrue(any("MODELS" in x for x in text), (os_name, w, h))
                    if (w, h) in ((120, 33), (200, 50)):
                        self.assertTrue(any("HARDWARE" in x for x in text))
                    self.assertTrue(av.rows >= 1 and 0 <= av.idx < len(m.rows))

    def test_the_cursor_row_is_in_reverse_video_and_the_details_sit_beside_the_list_when_wide(self):
        m = Machine()
        av = m.view(cur=m.rows[2]["id"], details=True)
        lines = m.lines(200, 50, av)
        self.assertEqual(sum(1 for x in lines if "\x1b[7m" in x), 1)
        self.assertTrue(any("DETAILS" in x and " │ " in x for x in plain(lines)))  # beside: from AI_PANE_W columns
        below = plain(m.lines(120, 40, m.view(cur=m.rows[2]["id"], details=True)))
        self.assertTrue(any(x.strip().startswith("DETAILS") or "── DETAILS" in x for x in below))
        self.assertFalse(any("DETAILS" in x and " │ " in x for x in below))

    def test_the_question_that_waits_is_the_footer_of_the_frame(self):
        m = Machine()
        av = m.view(confirm=("delete", m.rows[0]["id"], "Delete the files of Alpha (4.4 GB)?"))
        with mock.patch.object(render, "ai_status", lambda wait=0.0: m.st):
            frame, _rows = render.ai_screen(m.data, [], av, 119, 33)
        self.assertIn("Delete the files of Alpha (4.4 GB)?  y: yes   any other key: no", SGR.sub("", frame))

    def test_a_catalog_that_could_not_be_read_is_one_message(self):
        m = Machine()
        for err, level in ((True, "✖"), (False, "·")):
            data = {"cat": None, "msg": "the model catalog could not be read: boom " * 6, "err": err, "at": golden.NOW}
            lines = plain(screens.ai_lines(data, m.st, m.view(), [], 80, 24))
            self.assertIn("AI", lines[0])
            self.assertIn(level, lines[1])
            self.assertTrue(all(len(x) <= 80 for x in lines))

    def test_hostile_names_and_paths_stay_text(self):
        cat = hand()
        cat["models"][0]["name"] = EVIL + T.EVIL + T.CJK
        cat["models"][1]["notes"] = T.FORMAT * 5
        cat["dir"] = "/var/lib/" + T.EVIL
        cat["hw"]["gpus"] = [{"name": T.EVIL, "backend": "cuda", "vram_mb": 8192, "vram_free_mb": 100}]
        m = Machine(cat=cat)
        for details in (False, True):
            for w, h in ((79, 24), (200, 50)):
                for line in m.lines(w, h, m.view(cur="m-gpu", details=details)):
                    self.assertIsNone(CONTROL.search(SGR.sub("", line)), (w, h, line))
                    self.assertNotIn("\x1b[2J", line)

    def test_many_models_scroll_and_say_so(self):
        cat = hand()
        cat["models"] = [T.model("m%d" % i, "gpu", "Model %d" % i, i + 1) for i in range(30)]
        m = Machine(cat=cat)
        av = m.view(cur="m20")
        text = plain(m.lines(120, 24, av))
        self.assertTrue(any(re.search(r"MODELS.*\d+-\d+ of 30 · best first", x) for x in text))
        self.assertTrue(av.top > 0 and av.top <= 20 < av.top + av.rows)
        self.assertEqual(sum(1 for x in text if "Model " in x), av.rows)

    def test_an_empty_catalog_says_so(self):
        cat = hand()
        cat["models"] = []
        text = plain(Machine(cat=cat).lines(120, 33))
        self.assertTrue(any("the catalog lists no model" in x for x in text))


class Components(unittest.TestCase):
    def test_a_badge_is_the_pill_the_console_always_drew(self):
        for tone, code in (("ok", "1;42;30"), ("accent", "1;46;30"), ("warn", "1;43;30"), ("err", "1;41;37"), ("muted", "90")):
            self.assertEqual(ansi.render(ui.Badge("✔ FITS", tone, 10), 80)[0], ["\x1b[%sm %s \x1b[0m" % (code, "✔ FITS".ljust(10))])
        self.assertEqual(ansi.inline(ui.Line([ui.Span("a"), ui.Badge("x", "ok")])), "a\x1b[1;42;30m x \x1b[0m")
        with self.assertRaises(ValueError):
            ui.Badge("x", "purple")

    def test_a_spec_wraps_its_values_keeps_a_command_whole_and_cuts_to_h(self):
        items = [("why", "word " * 30, None, False), ("install", "sudo nuc-console-ai setup a-very-long-model-name", "accent", True), ("licence", "MIT", "ok", False)]
        lines = ansi.render(ui.Spec("Model", items), 40)[0]
        text = plain(lines)
        self.assertEqual(text[0].split()[0:2], ["──", "DETAILS"])
        self.assertEqual(text[1], " Model")
        self.assertTrue(sum(1 for x in text if x.startswith(" why") or x.startswith("          word")) > 2)  # wrapped under its label
        self.assertIn(" install  sudo nuc-console-ai setup a-very-long-model-name", "\n".join(ansi.render(ui.Spec("Model", items), 80)[0]).replace("\x1b[36m", "").replace("\x1b[0m", "").replace("\x1b[90m", ""))
        self.assertLessEqual(max(len(x) for x in text), 40)  # the command is cut at the edge
        cut = plain(ansi.render(ui.Spec("Model", items, h=5), 40)[0])
        self.assertEqual(len(cut), 5)
        self.assertRegex(cut[-1], r"… \+\d+ more lines")

    def test_a_question_is_one_bold_line_that_says_how_to_answer(self):
        line = ansi.render(ui.Question("Delete it?"), 80)[0][0]
        self.assertTrue(line.startswith("\x1b[1;33m Delete it?\x1b[0m\x1b[90m  y: yes   any other key: no"))
        self.assertTrue(SGR.sub("", ansi.render(ui.Question("Delete it?"), 20)[0][0]).endswith("[y/n]"))

    def test_what_is_for_the_web_only_draws_nothing_on_the_console(self):
        for node in (ui.Action("/ai/use", "use"), ui.Controls([ui.Span("x"), ui.Action("/ai/off", "off")]), ui.Qa("you", "why?", ui.Advice("h"))):
            self.assertEqual(ansi.render(node, 80), ([], False))
        self.assertEqual(ansi.inline(ui.Action("/ai/use", "use")), "")

    def test_cols_once_clips_where_the_lines_are_put_side_by_side_only(self):
        left = ui.Group([ui.Line([ui.Span("aaaa")], clip=10), ui.Head("H")])
        right = ui.Group([ui.Line([ui.Span("b")])])
        once = ansi.render(ui.Cols([(left, 10), (right, 10)], gap=3, once=True), 23)[0]
        every = ansi.render(ui.Cols([(left, 10), (right, 10)], gap=3), 23)[0]
        self.assertEqual((once[0].count("\x1b[0m"), every[0].count("\x1b[0m")), (3, 5))  # a cut fewer in each column

    def test_table_clip_cuts_the_rows_that_have_no_tone_and_leaves_the_solid_one(self):
        t = ui.Table([ui.Col("a", "A", gap=1), ui.Col("b", "B")], [ui.Row([ui.Span("x" * 30), ui.Span("y")]), ui.Row([ui.Span("x" * 30), ui.Span("y")], "sel")],
                     fill=True, solid=10, clip=10)
        lines = ansi.render(t, 80)[0]
        self.assertEqual(SGR.sub("", lines[0]), " " + "x" * 9)
        self.assertTrue(lines[0].endswith("\x1b[0m"))
        self.assertEqual(lines[1], "\x1b[7m " + "x" * 9 + "\x1b[0m")


class Frag(HTMLParser):
    """Every tag and attribute of a page: the shell's fragment must keep inside webjs.FRAG_TAGS / FRAG_ATTRS."""

    def __init__(self):
        HTMLParser.__init__(self)
        self.tags, self.attrs, self.forms = [], set(), []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.update(k for k, _v in attrs)
        if tag == "form":
            self.forms.append(dict(attrs))


def view_of(page_html):
    start = page_html.index('data-card="__view"')
    return page_html[page_html.rindex("<", 0, start):page_html.index("</main>", start)]


class Web(unittest.TestCase):
    def page(self, query, locked=False):
        with golden.FrozenWorld(cfg=golden.LOCKED if locked else None) as world:
            return world.page(query)

    def test_the_shell_draws_the_screen_natively_in_the_view_block(self):
        view = view_of(self.page("app=1&view=ai&sel=qwen3-4b"))
        self.assertNotIn("<pre", view)
        for needle in ('class="scr av"', '<table class="tbl">', '<svg class="bar', 'class="tag ok"', '<dl class="spec-dl">', '<form class="f" method="post" action="/ai/off">',
                       'data-key="e"', 'data-key="x"', 'data-key="X"', "check before acting", 'name="q"'):
            self.assertIn(needle, view)
        self.assertEqual(view.count('data-card="__view"'), 1)
        classic = self.page("app=0&view=ai&sel=qwen3-4b")
        self.assertIn('<pre class="ht">', classic)
        self.assertNotIn('class="scr av"', classic)

    def test_every_form_keeps_the_token_the_back_page_and_the_endpoints_of_the_classic_page(self):
        view = view_of(self.page("app=1&view=ai&sel=qwen3-30b-a3b"))
        classic = self.page("app=0&view=ai&sel=qwen3-30b-a3b")
        p = Frag()
        p.feed(view)
        self.assertTrue(p.forms)
        self.assertTrue(all(f["method"] == "post" and re.match(r"^/ai/[a-z-]+$", f["action"]) for f in p.forms))
        for form in re.findall(r"<form .*?</form>", view):
            self.assertRegex(form, r'<input type="hidden" name="csrf" value="[^"]+">')
            self.assertRegex(form, r'<input type="hidden" name="back" value="[^"]*view=ai')
        actions = {f["action"] for f in p.forms}
        self.assertLessEqual(actions, set(re.findall(r'action="(/ai/[a-z-]+)"', classic)))  # no endpoint the classic page has not
        self.assertIn("/ai/use", actions)
        self.assertEqual(set(re.findall(r'name="csrf" value="([^"]+)"', view)), set(re.findall(r'name="csrf" value="([^"]+)"', classic)))

    def test_a_click_that_asks_a_question_shows_it_with_yes_and_no(self):
        view = view_of(self.page("app=1&view=ai&sel=qwen3-1.7b&confirm=delete"))
        self.assertIn('<div class="ask" role="group"', view)
        self.assertIn("Delete the files of Qwen3 1.7B", view)
        self.assertRegex(view, r'(?s)action="/ai/delete">.*?name="confirm" value="yes">.*?data-key="y"')
        self.assertRegex(view, r'<a class="btn" href="[^"]*view=ai[^"]*" data-key="n">No</a>')
        self.assertNotRegex(re.search(r'<a class="btn" href="([^"]*)" data-key="n"', view).group(1), "confirm=")
        self.assertIn("Delete the runtime and every downloaded model", view_of(self.page("app=1&view=ai&confirm=delete-all")))
        self.assertNotIn('class="ask"', view_of(self.page("app=1&view=ai&sel=qwen3-8b&confirm=delete")))  # not installed: nothing to ask

    def test_locked_shows_the_notice_and_has_no_form_no_button_no_question(self):
        for query in ("app=1&view=ai&sel=qwen3-4b", "app=1&view=ai&sel=qwen3-4b&confirm=delete"):
            view = view_of(self.page(query, locked=True))
            self.assertIn("locked by config.ini ([ai] web_actions = no): this page only shows", view)
            for needle in ("<form", "<button", "<input", 'class="ask"', "use this model", "data-key=\"e\""):
                self.assertNotIn(needle, view)
            self.assertIn("MODELS", view)

    def test_only_what_a_fragment_may_hold(self):
        for locked in (False, True):
            p = Frag()
            p.feed(view_of(self.page("app=1&view=ai&sel=qwen3-4b&confirm=delete", locked)))
            self.assertEqual(sorted(set(p.tags) - set(webjs.FRAG_TAGS)), [])
            self.assertEqual(sorted(p.attrs - {a.lower() for a in webjs.FRAG_ATTRS}), [])

    def nodes(self, cat, locked=False, chat=(), confirm="", sel="", snap_over=None):
        with golden.FrozenWorld() as world:
            render.DEMO, render.DEMO_OS = True, None
            m = Machine(cat=cat)
            snap = dict(m.st["snap"], locked=locked, **(snap_over or {}))
            acts = None if locked else screens.AiActs("TOKEN", "view=ai")
            links = screens.AiLinks(lambda i: "/?view=ai&sel=" + i, "/?view=ai", "/?view=ai")
            return screens.ai_model(m.data, m.st, snap, None if locked else {"model": None, "by": "", "recommended": None, "target": None, "size": None, "installed": False},
                                    m.rows, sel, confirm, acts, links, chat)

    def test_nothing_the_machine_or_the_model_wrote_is_markup(self):
        cat = hand()
        cat["models"][0].update(name=EVIL, notes=EVIL)
        cat["models"][0]["commands"]["install"] = EVIL
        cat["dir"] = EVIL
        cat["hw"] = dict(cat["hw"], notes=[EVIL], gpus=[{"name": EVIL, "backend": "cuda", "vram_mb": 8192, "vram_free_mb": 1.0, "unified": False}])
        chat = [ui.Qa("you", EVIL, ui.Advice(EVIL, [[EVIL]], [("cites", EVIL)], "advice")), ui.Qa("advice", EVIL, None, True)]
        out = "".join(htmlview.html(n) for n in self.nodes(cat, chat=chat, sel="m-gpu"))
        self.assertNotIn("<script>", out)
        self.assertGreaterEqual(out.count(stdhtml.escape(EVIL)), 6)
        self.assertIn("the model is writing the answer", out)

    def test_what_cannot_be_read_is_a_question_mark_on_the_web_too(self):
        cat = hand()
        cat["hw"] = {"os": "linux", "cpu": {}, "ram": {}, "gpus": [{"name": "g"}]}
        cat["space"] = {}
        out = "".join(htmlview.html(n) for n in self.nodes(cat))
        self.assertIn("could not be read", out)
        self.assertIn("free space unknown", out)
        self.assertNotIn("free of", out)

    def test_an_action_posts_only_to_a_path_of_this_server(self):
        self.assertIn('action="/ai/use"', htmlview.html(ui.Action("/ai/use", "use")))
        for bad in ("http://evil.example/x", "//evil.example/x", "/ai/../x", "javascript:x"):
            self.assertEqual(htmlview.html(ui.Action(bad, "use")), "")
        out = htmlview.html(ui.Action("/ai/use", "u", [("model", '"><script>')], "u", "ok", "t", True))
        self.assertNotIn("<script>", out)
        self.assertIn(" disabled", out)

    def test_a_catalog_that_could_not_be_read_is_a_message(self):
        with golden.FrozenWorld():
            nodes = screens.ai_model({"cat": None, "msg": "boom", "err": True, "at": 0}, {}, {}, None, [], "", "", None, None)
        self.assertEqual(nodes[1].level, "err")
        self.assertNotIn("<form", "".join(htmlview.html(n) for n in nodes))


if __name__ == "__main__":
    unittest.main()
