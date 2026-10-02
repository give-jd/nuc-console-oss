"""The Health screen as components (src/screens.py): the model, what the console draws from it at several sizes and periods, and what the
web shell draws from the same model (a real page, no <pre>, escaped, inside the __view block).

Hermetic: the demo reports at a clock that stands still (tests/golden.py's FrozenWorld)."""
import html as stdhtml
import os
import re
import sys
import unittest
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golden  # noqa: E402
import ansi  # noqa: E402
import demo  # noqa: E402
import htmlview  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import ui  # noqa: E402
import webjs  # noqa: E402

NOW = golden.NOW
SGR = re.compile(r"\x1b\[[0-9;]*m")
EVIL = "<script>alert(1)</script>"


def report(days=7, os_name=None, variant=""):
    return demo.health_report(os_name, days, NOW, variant=variant)


def data_of(rep):
    return {"report": rep, "msg": "", "err": False, "at": NOW}


def plain(lines):
    return [SGR.sub("", x) for x in lines]


def hostile(rep):
    """The report with `<script>` in every text the machine writes."""
    rep["top_cpu"][0]["app"] = EVIL
    rep["top_mem"][0]["app"] = EVIL
    rep["logs"][0].update(unit=EVIL, template=EVIL)
    rep["disks"][0]["mount"] = EVIL
    rep["notes"] = [EVIL]
    rep["findings"][0].update(title=EVIL, text=EVIL, fix=EVIL, facts={EVIL: EVIL})
    rep["events"]["crash"][0]["subject"] = EVIL
    rep["thermal"]["apps_when_hot"] = [{"app": EVIL, "share": 0.5}]
    return rep


class Frag(HTMLParser):
    """Every tag and attribute a page holds: the shell's fragment must keep inside webjs.FRAG_TAGS / FRAG_ATTRS."""

    def __init__(self):
        HTMLParser.__init__(self)
        self.tags, self.attrs = [], set()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.update(k for k, _v in attrs)


def view_of(page_html):
    start = page_html.index('data-card="__view"')
    return page_html[page_html.rindex("<", 0, start):page_html.index("</main>", start)]


class Model(unittest.TestCase):
    def build(self, rep, sel="", advice=None):
        fl = screens.health_findings(rep)
        hv = screens.HealthView(int(rep["period"]["days"]))
        hv.cur = sel
        return screens.health_model(data_of(rep), hv, fl, advice, lambda d: "/?view=health&period=%d" % d, NOW), fl

    def test_title_has_the_three_periods_with_their_keys(self):
        nodes, _ = self.build(report(7))
        seg = nodes[0].seg
        self.assertEqual([(t, k, c) for t, k, c, _h in seg.options], [("24h", "d", False), ("7d", "w", True), ("30d", "m", False)])
        self.assertEqual(screens.PERIOD_KEYS, ("d", "w", "m"))  # the keymap's own
        self.assertTrue(all(h.startswith("/?view=health") for *_x, h in seg.options))

    def test_findings_carry_state_title_details_and_fix(self):
        rep = report(7)
        nodes, fl = self.build(rep, sel=rep["findings"][1]["id"])
        group = next(n for n in nodes if isinstance(n, ui.Group))
        found = [n for n in group.children if isinstance(n, ui.Finding)]
        self.assertEqual(len(found), len(fl))
        self.assertEqual([f.level for f in found], [screens.health_level(f) for f in fl])
        self.assertEqual([f.open for f in found], [i == 1 for i in range(len(found))])
        self.assertTrue(all(f.detail is not None and f.detail.fix for f in found))
        self.assertEqual(found[0].detail.facts, screens.health_details(fl[0])[3])

    def test_the_tables_are_three_columns_of_all_the_sections(self):
        nodes, _ = self.build(report(7))
        cols = nodes[-1]
        self.assertIsInstance(cols, ui.Cols)
        heads = [h.title for _n, _w in cols.children for h in _n.children if isinstance(h, ui.Head)]
        self.assertEqual(heads, ["TOP CPU", "TOP MEMORY", "EVENTS", "NOISY / NEW LOGS", "DISKS", "THERMAL", "BOOTS"])

    def test_unknown_values_are_question_marks_not_fine(self):
        rep = report(7)
        rep["thermal"] = {"hours_hot": 0, "max": None, "apps_when_hot": []}
        rep["boots"] = [{"total_s": None}]
        text = "\n".join(plain(screens.health_tables_lines(rep, 200, None, NOW)))
        self.assertIn("? no temperature data", text)
        self.assertIn("? boot times unknown", text)
        self.assertNotIn("never hot", text)

    def test_no_history_and_an_unreadable_one(self):
        nodes = screens.health_model({"report": None, "msg": "the history could not be read: x", "err": True, "at": NOW}, screens.HealthView(), [], None, None, NOW)
        self.assertEqual(nodes[1].level, "err")
        rep = report(7, variant="none")
        nodes, _ = self.build(rep)
        self.assertEqual(nodes[-1].level, "info")
        self.assertIn("no history yet", nodes[-1].text)

    def test_the_key_actions_moved_with_the_view(self):
        self.assertIs(render.HealthView, screens.HealthView)
        hv, fl = screens.HealthView(7), [{"id": "a"}, {"id": "b"}]
        self.assertEqual(screens.health_key(hv, "d", fl), "period")
        self.assertEqual(hv.days, 1)
        self.assertEqual(screens.health_key(hv, "j", fl), "")
        self.assertEqual(hv.cur, "b")
        self.assertEqual(screens.health_key(hv, "enter", fl), "")
        self.assertTrue(hv.details)
        self.assertEqual(screens.health_key(hv, "esc", fl), "")
        self.assertEqual(screens.health_key(hv, "q", fl), "back")


class Console(unittest.TestCase):
    def test_sizes_and_periods_fit_and_keep_their_parts(self):
        for days in (1, 7, 30):
            rep = report(days)
            for w, h in ((79, 24), (120, 33), (200, 50), (226, 60), (60, 14)):
                hv = screens.HealthView(days)
                fl = screens.health_findings(rep)
                lines = plain(screens.health_lines(data_of(rep), hv, fl, w, h, None, NOW))
                self.assertLessEqual(len(lines), h)
                self.assertTrue(all(ansi.vlen(x) <= w for x in lines), (days, w, h))
                self.assertIn("HEALTH", lines[0])
                self.assertIn(" w:7d " if days == 7 else " d:24h " if days == 1 else " m:30d ", lines[0] + " " if w >= 79 else lines[0] + " ")
                self.assertTrue(any("FINDINGS" in x for x in lines))

    def test_the_chosen_period_is_in_reverse_video(self):
        rep = report(30)
        line = screens.health_lines(data_of(rep), screens.HealthView(30), screens.health_findings(rep), 120, 33, None, NOW)[0]
        self.assertIn("\x1b[7m m:30d \x1b[0m", line)
        self.assertIn("\x1b[90m d:24h \x1b[0m", line)

    def test_the_advice_block_sits_under_the_findings(self):
        rep = report(7)
        adv = lambda R, w: ui.Advice(lines=[ansi.clip(ansi.c(90, " advice head"), w), " second line"])  # noqa: E731
        lines = plain(screens.health_lines(data_of(rep), screens.HealthView(7), screens.health_findings(rep), 120, 40, adv, NOW))
        i = next(j for j, x in enumerate(lines) if "ADVICE" in x)
        self.assertEqual(lines[i + 1].strip(), "advice head")
        self.assertTrue(any("FINDINGS" in x for x in lines[:i]))

    def test_hostile_text_stays_text(self):
        rep = hostile(report(7))
        for w in (79, 200):
            for details in (False, True):
                hv = screens.HealthView(7)
                hv.details = details
                lines = screens.health_lines(data_of(rep), hv, screens.health_findings(rep), w, 50, None, NOW)
                self.assertTrue(all(not re.search(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]", SGR.sub("", x)) for x in lines))

    def test_a_bare_escape_in_a_name_is_a_question_mark(self):
        rep = report(7)
        rep["top_cpu"][0]["app"] = "a\x1b[31mb"
        text = "\n".join(plain(screens.health_tables_lines(rep, 200, None, NOW)))
        self.assertNotIn("\x1b", text)
        self.assertIn("a?[31mb", text)


class Web(unittest.TestCase):
    def page(self, query, rep_hook=None):
        with golden.FrozenWorld() as world:
            if rep_hook:
                orig = render.health_build
                world.set(render, "health_build", lambda days, now: dict(orig(days, now), report=rep_hook(orig(days, now)["report"])))
            return world.page(query)

    def test_the_shell_draws_the_screen_natively_in_the_view_block(self):
        page = self.page("app=1&view=health")
        view = view_of(page)
        self.assertNotIn("<pre", view)
        for needle in ('class="hv"', '<details data-k="f-', '<table class="tbl">', '<svg class="series', '<svg class="bar',
                       'data-key="d"', 'data-key="m"', 'aria-current="true">7d<'):
            self.assertIn(needle, view)
        self.assertEqual(view.count('data-card="__view"'), 1)
        self.assertEqual(view.count("<details"), len(re.findall(r'class="fd lv-', view)))

    def test_the_selected_finding_is_open_and_the_classic_page_is_unchanged(self):
        sel = golden.SELECTED["finding"]
        view = view_of(self.page("app=1&view=health&sel=" + sel))
        self.assertEqual(len(re.findall(r"<details [^>]*open", view)), 1)
        classic = self.page("app=0&view=health")
        self.assertIn('<pre class="ht">', classic)
        self.assertNotIn('class="hv"', classic)

    def test_the_period_links_keep_the_rest_of_the_url(self):
        view = view_of(self.page("app=1&view=health&sel=" + golden.SELECTED["finding"]))
        links = re.findall(r'<a href="([^"]*)" data-key="([dwm])"', view)
        self.assertEqual([k for _h, k in links], ["d", "m"])
        self.assertTrue(all("view=health" in stdhtml.unescape(h) and "sel=" in stdhtml.unescape(h) for h, _k in links))
        self.assertIn("period=1", stdhtml.unescape(links[0][0]))

    def test_nothing_the_machine_wrote_is_markup(self):
        page = self.page("app=1&view=health", lambda rep: hostile(rep))
        view = view_of(page)
        self.assertNotIn(EVIL, view)
        self.assertIn(stdhtml.escape(EVIL), view)

    def test_only_what_a_fragment_may_hold(self):
        view = view_of(self.page("app=1&view=health&sel=" + golden.SELECTED["finding"]))
        p = Frag()
        p.feed(view)
        self.assertEqual(sorted(set(p.tags) - set(webjs.FRAG_TAGS)), [])
        self.assertEqual(sorted(p.attrs - {a.lower() for a in webjs.FRAG_ATTRS}), [])  # the parser lowercases (viewBox)

    def test_advice_is_a_highlighted_block_with_its_parts(self):
        node = ui.Advice("ADVICE (AI, m) — check", [["one", "two"], ["three"]], [("cites", "cites: [a]")], "shared")
        out = htmlview.html(node)
        self.assertIn('class="advice advice-shared"', out)
        self.assertIn("<p>one<br>two</p><p>three</p>", out)
        self.assertIn('<p class="advice-cites">cites: [a]</p>', out)
        self.assertEqual(htmlview.html(ui.Advice()), "")

    def test_series_leaves_a_gap_where_nothing_was_recorded(self):
        svg = htmlview.html(ui.Series([1, None, 3], 3))
        self.assertEqual(svg.count("<rect"), 2)
        self.assertIn('aria-label="no data"', htmlview.html(ui.Series([None], 1)))


if __name__ == "__main__":
    unittest.main()
