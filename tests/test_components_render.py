"""The components drawn: ansi.render / ansi.card_lines (the console) and htmlview.html (the web), and the cards built of components.

The console's text for the converted cards is held byte for byte by the golden files (tests/test_golden.py); here the format of each
component, its widths, what it truncates and what it says when it has no value are held on small inputs, the HTML is checked for
escaping and structure, and the five native cards (disks, docker_disk, sessions, tailscale, network_traffic) are built at the detail
levels and in full.
"""
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golden  # noqa: E402
import ansi  # noqa: E402
import cards  # noqa: E402
import demo  # noqa: E402
import htmlview  # noqa: E402
import render  # noqa: E402
import cardlines  # noqa: E402
import ui  # noqa: E402
from ui import Bar, Card, Col, Group, KV, Line, More, Msg, Notice, Raw, Row, Span, Spark, Table, Wrap  # noqa: E402

HOSTILE = '<script>alert(1)</script>"\'&\x1b[31m'


def plain(lines):
    return [ansi.ANSI.sub("", x) for x in lines]


class AnsiRenderTests(unittest.TestCase):
    def test_span_and_line_are_as_they_are(self):
        lines, hid = ansi.render(Line([" a ", Span("b", "err"), Span("c", None, True)]), 40)
        self.assertEqual(lines, [" a \x1b[31mb\x1b[0m\x1b[1mc\x1b[0m"])
        self.assertFalse(hid)
        self.assertEqual(ansi.render(Span("x", "ok"), 10)[0], ["\x1b[32mx\x1b[0m"])

    def test_msg_and_notice_have_the_symbol_and_the_indent(self):
        for level, sym in (("err", "✖"), ("warn", "!"), ("ok", "✔"), ("info", "·")):
            self.assertEqual(plain(ansi.render(Msg(level, "text"), 40)[0]), [f"   {sym} text"])
        self.assertEqual(ansi.render(Notice("warn", "stale"), 40)[0], [ansi.msg("warn", "stale")])

    def test_kv_pads_the_labels(self):
        lines, _ = ansi.render(KV([("a", "1"), ("bb", Span("2", "ok"))], lw=5), 40)
        self.assertEqual(plain(lines), ["   a    1", "   bb   2"])

    def test_bar_is_the_old_bar_and_unknown_is_a_question_mark(self):
        lines, _ = ansi.render(Bar(0.5, "5/10", w=10), 40)
        self.assertEqual(lines, [ansi.bar(0.5, 10) + " 5/10"])
        self.assertEqual(ansi.inline(Bar(0.95, "", w=4)), ansi.bar(0.95, 4))
        self.assertIn("\x1b[31m", ansi.inline(Bar(0.95, "", w=4)))  # over 0.9: err
        unk = ansi.inline(Bar(None, "ignored", w=6))
        self.assertEqual(ansi.ANSI.sub("", unk), "?     ")
        self.assertEqual(ansi.inline(Bar(float("nan"), "x", w=3)), ansi.inline(Bar(None, "x", w=3)))

    def test_spark_is_the_old_sparkline(self):
        self.assertEqual(ansi.inline(Spark([0, 500, 2000, 4000], 6)), ansi.sparkline([0, 500, 2000, 4000], 6))
        self.assertEqual(len(ansi.ANSI.sub("", ansi.inline(Spark([], 5)))), 5)
        self.assertEqual(ansi.inline(Spark([1, 2, 3], 3, floor=1)), ansi.sparkline([1, 2, 3], 3, floor=1))

    def test_table_pads_the_fixed_columns_and_leaves_the_others(self):
        t = Table([Col("a", "A", w=4), Col("n", "N", "r", num=True, w=3), Col("c", "C", gap=2), Col("d", "D", w=3)],
                  [Row(["x", "7", "free", "q"]), Row(["yy", "42", "longer text", "r"])])
        self.assertEqual(plain(ansi.render(t, 80)[0]), [" x      7 free  q", " yy    42 longer text  r"])
        t.head = True
        self.assertEqual(plain(ansi.render(t, 80)[0])[0], " A      N C  D")

    def test_table_drops_the_columns_with_a_priority_when_it_is_too_wide(self):
        t = Table([Col("a", "A", w=5), Col("b", "B", prio=2, w=5), Col("c", "C", prio=1, w=5), Col("d", "D")],
                  [Row(["aaaaa", "bbbbb", "ccccc", "ddddd"])])
        self.assertEqual(plain(ansi.render(t, 40)[0]), [" aaaaa bbbbb ccccc ddddd"])
        self.assertEqual(plain(ansi.render(t, 20)[0]), [" aaaaa ccccc ddddd"])   # b goes first
        self.assertEqual(plain(ansi.render(t, 12)[0]), [" aaaaa ddddd"])         # then c; a and d are never dropped

    def test_table_row_tone_and_groups(self):
        t = Table([Col("a", "A")], [Row(["one"], tone="err"), Row(["two", ]), Row(["three"])], groups=[("first", 0), ("second", 2)])
        lines, _ = ansi.render(t, 40)
        self.assertEqual(plain(lines), [" first", " one", " two", " second", " three"])
        self.assertIn("\x1b[31mone", lines[1])
        self.assertNotIn("\x1b", lines[2])

    def test_wrap_flows_and_ends_with_the_count(self):
        items = [f"item{i}" for i in range(10)]
        lines, hid = ansi.render(Wrap(items, sep=" ", indent=2), 20)
        self.assertEqual(plain(lines)[0], "  item0 item1 item2")
        self.assertFalse(hid)
        self.assertTrue(all(ansi.vlen(x) <= 20 for x in lines))
        lines, hid = ansi.render(Wrap(items, sep=" ", indent=2, max_lines=1), 20)
        self.assertTrue(hid)
        self.assertEqual(len(lines), 1)
        self.assertRegex(plain(lines)[0], r"  … \+\d+$")
        self.assertEqual(lines, ansi_wrap_items(items, 20, 2, " ", 1))

    def test_more_group_and_raw(self):
        self.assertEqual(plain(ansi.render(More(3, "nodes"), 40)[0]), [" … +3 nodes"])
        lines, hid = ansi.render(Group([Msg("ok", "a"), Wrap(["x", "y", "z"], sep=" ", max_lines=1, indent=1)], "T"), 4)
        self.assertEqual(plain(lines)[0], " T")
        self.assertTrue(hid)  # the Wrap cut: the group says so
        self.assertEqual(ansi.render(Raw(["a", "\x1b[31mb"]), 40), (["a", "\x1b[31mb"], False))

    def test_a_node_that_is_no_component_is_a_question_mark(self):
        for node in (None, 3, "text", object()):
            lines, hid = ansi.render(node, 40)
            self.assertEqual(plain(lines), [" ?"])
            self.assertFalse(hid)
        self.assertEqual(ansi.inline("text"), ansi.style("?", "unknown"))

    def test_the_card_has_its_title_and_note_unless_it_is_raw(self):
        card = Card("disks", "DISKS", "a note", "ok", [Msg("info", "hello")])
        lines, hid = ansi.card_lines(card, 40)
        self.assertEqual(lines[0], ansi.section("DISKS", 40, "a note"))
        self.assertEqual(plain(lines)[1], "   · hello")
        self.assertFalse(hid)
        raw = Card("x", "X", "", "ok", [Raw([ansi.section("X", 40), "line"])])
        self.assertEqual(ansi.card_lines(raw, 40)[0], [ansi.section("X", 40), "line"])
        self.assertEqual(ansi.render(card, 40), ansi.card_lines(card, 40))
        card.truncated = True
        self.assertTrue(ansi.card_lines(card, 40)[1])

    def test_pill_kpi_tree_details_have_a_symbol(self):
        self.assertEqual(plain(ansi.render(ui.Pill("ALL OK", "ok"), 40)[0]), [" ✔ ALL OK"])
        self.assertEqual(plain(ansi.render(ui.Kpi("cpu", "CPU", "5", "%", "ok"), 40)[0]), [" ✔ CPU 5%"])
        self.assertEqual(plain(ansi.render(ui.Kpi("cpu", "CPU", "5", "%", "unknown"), 40)[0]), [" ? CPU ?"])
        self.assertEqual(plain(ansi.render(ui.Tree([(0, "a", "ok"), (1, "b", "err")]), 40)[0]), [" ✔ a", "   ✖ b"])
        d = ui.Details("sum", [Msg("ok", "inside")])
        self.assertEqual(plain(ansi.render(d, 40)[0]), [" ▸ sum"])
        d.open = True
        self.assertEqual(plain(ansi.render(d, 40)[0]), [" ▾ sum", "     ✔ inside"])


def ansi_wrap_items(items, w, indent, sep, max_lines):
    """The legacy render.wrap_items, called with its globals as they are in a normal frame."""
    return render.wrap_items(items, w, indent=indent, sep=sep, max_lines=max_lines)


class HtmlRenderTests(unittest.TestCase):
    def test_hostile_text_is_escaped_everywhere(self):
        nodes = [Span(HOSTILE, "err"), Line([HOSTILE]), Msg("warn", HOSTILE), Notice("err", HOSTILE), KV([(HOSTILE, HOSTILE)]),
                 Table([Col(HOSTILE, HOSTILE)], [Row([HOSTILE], key=HOSTILE, href="javascript:alert(1)")]),
                 Wrap([HOSTILE]), More(2, HOSTILE, 'javascript:alert(1)'), Group([Msg("ok", HOSTILE)], HOSTILE),
                 ui.Pill(HOSTILE, "ok"), ui.Kpi("k", HOSTILE, HOSTILE, HOSTILE, "ok", hint=HOSTILE),
                 ui.Tree([(0, HOSTILE, "ok")]), ui.Details(HOSTILE, [Msg("ok", HOSTILE)]), Bar(0.5, HOSTILE),
                 Card(HOSTILE, HOSTILE, HOSTILE, "ok", [Msg("ok", HOSTILE)]), Raw([HOSTILE])]
        for node in nodes:
            out = htmlview.html(node)
            self.assertNotIn("<script", out, node)
            self.assertNotIn("alert(1)</", out, node)
            self.assertNotIn("javascript:", out, node)
            self.assertNotIn("\x1b", out, node)
            self.assertNotIn(' style=', out, node)
            self.assertEqual(out.count('"') % 2, 0, node)

    def test_span_tones_and_levels_are_classes(self):
        self.assertEqual(htmlview.html(Span("x", "err")), '<p class="ln"><span class="t-err">x</span></p>')
        self.assertEqual(htmlview.html(Span("x", "accent_strong", True, True)),
                         '<p class="ln"><span class="t-accent-strong b mono">x</span></p>')
        self.assertIn('class="msg lv-warn"', htmlview.html(Msg("warn", "w")))
        self.assertIn('role="status"', htmlview.html(Notice("warn", "w")))
        self.assertIn('<span class="sym">✖</span>', htmlview.html(Msg("err", "e")))  # the symbol goes with the colour

    def test_table_has_a_head_numbers_in_n_cells_and_priorities(self):
        t = Table([Col("name", "Name"), Col("size", "Size", "r", num=True), Col("os", "OS", prio=2)],
                  [Row(["a", "1", "linux"], tone="err", key="k1"), Row(["b", "2", "mac"], href="/x?y=1")], groups=[("G", 1)])
        out = htmlview.html(t)
        self.assertIn('<thead><tr><th scope="col">Name</th><th class="r n" scope="col">Size</th><th class="p2" scope="col">OS</th></tr></thead>', out)
        self.assertIn('<td class="r n">1</td>', out)
        self.assertIn('<tr class="t-err" data-key="k1">', out)
        self.assertIn('<td><a href="/x?y=1">b</a></td>', out)
        self.assertIn('<tr class="grp"><th colspan="3" scope="rowgroup">G</th></tr>', out)
        self.assertIn('<col class="c-name">', out)

    def test_bar_and_spark_are_svg_with_attributes(self):
        out = htmlview.html(Bar(0.5, "3/4"))
        self.assertIn('<svg class="bar st-ok" viewBox="0 0 100 8"', out)
        self.assertIn('width="50"', out)
        self.assertIn('<span class="n">3/4</span>', out)
        self.assertIn("st-err", htmlview.html(Bar(0.95)))
        self.assertIn("st-warn", htmlview.html(Bar(0.8)))
        unk = htmlview.html(Bar(None, "never"))
        self.assertIn(">?</span>", unk)
        self.assertNotIn("never", unk)
        self.assertNotIn("<svg", unk)
        sp = htmlview.html(Spark([1, 5000, 2500]))
        self.assertIn('<svg class="spark"', sp)
        self.assertRegex(sp, r'points="0\.0,[\d.]+ 40\.0,[\d.]+ 80\.0,[\d.]+"')
        self.assertIn("<svg", htmlview.html(Spark([])))

    def test_more_is_a_details_and_a_link_only_when_safe(self):
        self.assertEqual(htmlview.html(More(2, "nodes")), '<details class="more"><summary>… +2 nodes</summary></details>')
        self.assertIn('<a href="?card=disks">show all</a>', htmlview.html(More(2, "more", "?card=disks")))
        self.assertIn('href="https://x.example/a"', htmlview.html(More(2, "more", "https://x.example/a")))
        self.assertNotIn("<a ", htmlview.html(More(2, "more", "//evil.example/")))

    def test_card_structure_and_the_unknown_node(self):
        card = Card("disks", "DISKS", "2 of 2", "warn", [Msg("warn", "hm"), Wrap(["a", "b"], max_lines=1)], size=2, truncated=True)
        out = htmlview.html(card)
        self.assertTrue(out.startswith('<article class="card st-warn s2" id="card-disks" data-card="disks" data-truncated>'))
        self.assertIn("<header><h2>DISKS</h2>", out)
        self.assertIn('<ul class="wrap" data-max-lines="1"><li>a</li><li>b</li></ul>', out)
        self.assertTrue(out.endswith("</article>"))
        self.assertEqual(htmlview.html(object()), '<span class="st-unknown">?</span>')
        self.assertEqual(htmlview.html(None), '<span class="st-unknown">?</span>')

    def test_kv_group_kpi_tree_details(self):
        self.assertEqual(htmlview.html(KV([("a", "b")])), '<dl class="kv"><dt>a</dt><dd>b</dd></dl>')
        self.assertIn("<h3>T</h3>", htmlview.html(Group([], "T")))
        k = htmlview.html(ui.Kpi("cpu", "CPU", "5", "%", "ok", spark=[1, 2, 3]))
        self.assertIn('class="kpi st-ok"', k)
        self.assertIn("<svg", k)
        self.assertIn("?", htmlview.html(ui.Kpi("cpu", "CPU", "5", "%", "unknown")))
        self.assertIn('data-depth="1"', htmlview.html(ui.Tree([(1, "x", "err")])))
        self.assertIn("<details class=\"dt\" open>", htmlview.html(ui.Details("s", [], True)))

    def test_the_module_keeps_the_escape_of_the_standard_library(self):
        self.assertEqual(htmlview.html.escape("<&>"), "&lt;&amp;&gt;")
        self.assertIn("&lt;", htmlview.to_html("<b>"))


class ConsoleOnlyFieldsTests(unittest.TestCase):
    """What a component carries for the console alone (the layout of the old blocks, byte for byte); the web ignores it."""

    def test_a_blank_line_and_a_clipped_line(self):
        self.assertEqual(ansi.render(Line(), 40), ([""], False))
        self.assertEqual(ansi.render(Line([Span("x" * 30, "muted")], clip=10), 40)[0], [ansi.clip(ansi.c(90, "x" * 30), 10)])
        self.assertEqual(ansi.render(Line([Span("x" * 5, "muted")], clip=10), 40)[0], [ansi.clip(ansi.c(90, "x" * 5), 10)])  # clip() ends with a reset

    def test_an_empty_text_with_a_tone_is_still_coloured(self):
        self.assertEqual(ansi.style("", "muted"), ansi.c(90, ""))
        self.assertEqual(ansi.style("", None), "")
        self.assertEqual(ansi.render(Span(""), 10)[0], [""])

    def test_more_indent_is_inside_the_colour_and_none_is_the_old_one(self):
        self.assertEqual(ansi.render(More(2, "rules", indent=3), 40)[0], [ansi.c(90, "   … +2 rules")])
        self.assertEqual(ansi.render(More(2, "rules"), 40)[0], [" " + ansi.c(90, "… +2 rules")])

    def test_table_indent_header_line_titled_groups_and_centred_cells(self):
        t = Table([Col("a", "A", w=4, gap=0), Col("b", "B", "c", w=5, gap=1), Col("c", "C")],
                  [Row(["x", Span("o", "ok"), "n1"]), Row(["yy", "pp", "n2"]), Row(["z", "ab", "n3"])],
                  groups=[("one", 0), ("two", 2)], indent=3, head_line="   AAAA BBBB C", titled=True)
        lines, _ = ansi.render(t, 80)
        self.assertEqual(lines[0], ansi.c(1, "   AAAA BBBB C"))
        self.assertEqual(plain(lines[1:]), ["", "   one  (2)", "   x     o   n1", "   yy    pp  n2", "", "   two  (1)", "   z     ab  n3"])
        self.assertEqual(lines[2], ansi.c(1, "   one") + ansi.c(90, "  (2)"))
        for word in ("a", "ab", "abc", "abcd", "abcde"):  # the arithmetic of str.center, which the old matrix used
            self.assertEqual(plain(ansi.render(Table([Col("b", "B", "c", w=6)], [Row([word])]), 40)[0]), [" " + word.center(6)])
        self.assertTrue(t.head)  # a header line is a header

    def test_table_fill_colours_the_row_from_end_to_end_and_clip_cuts_a_cell(self):
        t = Table([Col("a", "A", w=4, gap=0), Col("b", "B", clip=5)], [Row(["x", "abcdefgh"], tone="warn"), Row(["y", "abc"])], indent=3, fill=True)
        lines, _ = ansi.render(t, 40)
        self.assertEqual(lines[0], ansi.c(33, "   x   abcde" + "\x1b[0m"))  # the clip's own reset sits inside the colour
        self.assertEqual(lines[1], "   y   abc\x1b[0m")
        loose = Table([Col("a", "A", w=4, gap=0), Col("b", "B")], [Row(["x", "ab"], tone="warn")], indent=3)
        self.assertEqual(ansi.render(loose, 40)[0], ["   " + ansi.c(33, "x") + "   " + ansi.c(33, "ab")])  # not filled: per cell, as before

    def test_details_brief_replaces_the_fold(self):
        d = ui.Details("2 accepted", [Msg("info", "inside")], brief=Line([Span("   · 2 accepted as known (cmd)", "muted")]))
        self.assertEqual(ansi.render(d, 60)[0], [ansi.c(90, "   · 2 accepted as known (cmd)")])
        d.open = True
        self.assertEqual(len(ansi.render(d, 60)[0]), 1)  # open or not: the console has the one line

    def test_a_problem_wraps_at_its_commas_and_the_rest_is_not_drawn(self):
        text = "a, b, " * 12 + "end"
        p = ui.Problem("err", text, "over-exposed", "Title", "why", "fix it", "accept it")
        self.assertEqual(ansi.render(p, 40)[0], ansi.msg_wrap("err", text, 40))
        self.assertEqual(ansi.render(ui.Problem("warn", "short"), 40)[0], [ansi.msg("warn", "short")])
        for node in (ui.Hint("label", "cmd"), ):
            self.assertEqual(ansi.render(node, 40), ([], False))
        acc = ui.Accepted("text", "id", "reason", "3 h ago", "undo")
        self.assertEqual(ansi.render(acc, 40)[0], [ansi.msg("info", "text")])

    def test_a_rich_msg_is_a_msg_with_its_own_emphasis(self):
        m = ui.RichMsg("err", [Span("ufw OFF", "err_strong"), ": no filtering"])
        self.assertEqual(m.text, "ufw OFF: no filtering")
        self.assertEqual(ansi.render(m, 40)[0], [ansi.msg("err", ansi.c("1;31", "ufw OFF") + ": no filtering")])

    def test_the_new_components_are_equal_by_their_fields(self):
        self.assertEqual(ui.Problem("err", "t", "a", "b", "c", "d", "e"), ui.Problem("err", "t", "a", "b", "c", "d", "e"))
        self.assertNotEqual(ui.Problem("err", "t", "a"), ui.Problem("err", "t", "b"))
        self.assertNotEqual(ui.RichMsg("ok", ["a"]), ui.RichMsg("ok", [Span("a", "ok")]))
        self.assertNotEqual(ui.Accepted("t", "a"), ui.Accepted("t", "b"))
        self.assertEqual(ui.Span("a", full="b"), ui.Span("a", full="b"))
        self.assertNotEqual(ui.Span("a", full="b"), ui.Span("a"))


class WebOnlyFieldsTests(unittest.TestCase):
    """What the web draws that the console does not: the problem's id and fix, the accepted ones, the whole name, the commands."""

    def test_the_exposure_counters_and_legend_are_lists_on_the_web_and_text_on_the_console(self):
        card = cards.build("exposure", ctx_of(), -2, cards.Caps(200))
        web = htmlview.html(card)
        self.assertEqual(web.count('<ul class="wrap flat">'), 2)
        self.assertIn("filtered by source", web)
        self.assertNotIn("   ● open", web)  # no padded text for the web to collapse
        text = plain(ansi.card_lines(card, 200)[0])
        self.assertTrue(any("● open   ◐ filtered by source" in x for x in text))
        self.assertTrue(any(x.lstrip().startswith("Internet ") and "    LAN " in x for x in text))

    def test_a_flat_wrap_is_marked_for_the_web(self):
        self.assertIn('<ul class="wrap flat">', htmlview.html(Wrap(["a"], flat=True)))
        self.assertIn('<ul class="wrap">', htmlview.html(Wrap(["a"])))

    def test_a_problem_has_its_id_why_fix_and_accept_command(self):
        p = ui.Problem("err", "1 DB open", "db-open-lan", "Title <b>", "data services stay home", "publish on 127.0.0.1", 'sudo x --problem db-open-lan --reason "..."')
        out = htmlview.html(p)
        self.assertIn('class="msg prob lv-err"', out)
        self.assertIn('data-problem="db-open-lan"', out)
        self.assertIn('<code class="pid" title="Title &lt;b&gt;">db-open-lan</code></span><span class="d why">data services stay home</span>', out)
        self.assertIn('<details class="fix" data-k="fix-db-open-lan"><summary>fix</summary>', out)
        self.assertIn('fix: <code class="cmd">publish on 127.0.0.1</code>', out)
        self.assertIn('accept if known: <code class="cmd">sudo x --problem db-open-lan --reason &quot;...&quot;</code>', out)
        bare = htmlview.html(ui.Problem("warn", "no id"))
        self.assertNotIn("<code", bare)
        self.assertNotIn("fix:", bare)

    def test_accepted_hint_rich_msg_and_full_names(self):
        out = htmlview.html(ui.Accepted("text", "docker-bypass", "known <i>", "3 h ago", "sudo x --forget docker-bypass"))
        self.assertIn('<span class="pr">text <code class="pid">docker-bypass</code></span>', out)
        self.assertIn("reason: “known &lt;i&gt;” · accepted 3 h ago · undo: <code class=\"cmd\">sudo x --forget docker-bypass</code>", out)
        self.assertEqual(htmlview.html(ui.Hint("list", "nuc-console-problems")), '<p class="hint d">list: <code class="cmd">nuc-console-problems</code></p>')
        rich = htmlview.html(ui.RichMsg("err", [Span("ufw OFF", "err_strong"), ": no"]))
        self.assertIn('<span class="t-err-strong">ufw OFF</span>: no', rich)
        self.assertIn("a-very-long-name", htmlview.html(Span("a-very-lo…", full="a-very-long-name")))
        self.assertNotIn("…", htmlview.html(Span("a-very-lo…", full="a-very-long-name")))

    def test_a_line_with_a_bar_is_boxed_and_a_column_has_its_web_priority(self):
        out = htmlview.html(Line([" RAM  ", ui.Bar(.33, "10.2G/31.2G   cache 4.8G")]))
        self.assertTrue(out.startswith('<p class="ln bl"><span class="lb">RAM</span><svg'), out)
        self.assertIn('<span class="n">10.2G/31.2G</span><span class="bx">cache 4.8G</span></p>', out)
        t = htmlview.html(Table([Col("a", "A", prio=1, wprio=3), Col("b", "B", prio=2), Col("c", "C")], [Row(["x", "y", "z"])]))
        self.assertIn('<th class="p3" scope="col">A</th><th class="p2" scope="col">B</th><th scope="col">C</th>', t)
        self.assertEqual(Col("a", "A", prio=1), Col("a", "A", prio=1))

    def test_blank_lines_are_for_the_console_and_titled_groups_count_their_rows(self):
        self.assertEqual(htmlview.html(Line()), "")
        t = Table([Col("a", "A", "c")], [Row(["1"]), Row(["2"]), Row(["3"])], groups=[("g1", 0), ("g2", 2)], titled=True, indent=3, head_line="   A")
        out = htmlview.html(t)
        self.assertIn('scope="rowgroup">g1 <span class="n">(2)</span></th>', out)
        self.assertIn('scope="rowgroup">g2 <span class="n">(1)</span></th>', out)
        self.assertIn('<th class="c" scope="col">A</th>', out)
        self.assertIn('<td class="c">1</td>', out)
        self.assertNotIn("   A", out)  # the console's header line is not the web's

    def test_hostile_text_in_the_new_nodes(self):
        for node in (ui.Problem("err", HOSTILE, HOSTILE, HOSTILE, HOSTILE, HOSTILE, HOSTILE), ui.Accepted(HOSTILE, HOSTILE, HOSTILE, HOSTILE, HOSTILE),
                     ui.Hint(HOSTILE, HOSTILE), ui.RichMsg("warn", [Span(HOSTILE, "err")]), Span(HOSTILE, full=HOSTILE),
                     ui.Details(HOSTILE, [ui.Accepted(HOSTILE)], brief=Line([HOSTILE]))):
            out = htmlview.html(node)
            self.assertNotIn("<script", out, node)
            self.assertNotIn("\x1b", out, node)
            self.assertEqual(out.count('"') % 2, 0, node)
            self.assertNotIn(" onerror", out)


# ---- the cards built of components -------------------------------------------------------------------------------------------------

NATIVE = ("disks", "docker_disk", "sessions", "tailscale", "network_traffic")


def ctx_of(**kw):
    now = time.time()
    cont, net, boot, base = demo.snapshot(now)
    s = demo.sampler_data(None, now)
    args = dict(s=s, cont=cont, net=net, boot=boot, problems=[], now=now, baseline=base)
    args.update(kw)
    return cards.Ctx(**args)


class NativeCardTests(unittest.TestCase):
    def test_they_are_registered_in_the_order_of_the_sections_and_not_raw(self):
        self.assertEqual(list(cards.CARDS), list(render.nuc_config.SECTIONS))
        ctx = ctx_of()
        for id in NATIVE:
            card = cards.build(id, ctx, 0, cards.Caps(100))
            self.assertFalse(any(isinstance(p, Raw) for p in card.body), id)
            self.assertEqual(card.title, cards.CARDS[id].title)
            lines, _ = ansi.card_lines(card, 100)
            self.assertEqual(plain(lines[:1])[0].split()[1], card.title.split()[0], id)
            self.assertTrue(all(ansi.vlen(x) <= 100 for x in lines), id)

    def test_the_demo_cards_are_the_overview_blocks(self):
        ctx = ctx_of()
        for id, fn, arg in (("disks", cardlines.ov_dischi, ctx.s), ("sessions", cardlines.ov_sessioni, ctx.s),
                            ("tailscale", cardlines.ov_tailscale, ctx.net), ("docker_disk", cardlines.ov_docker, ctx.boot),
                            ("network_traffic", cardlines.ov_traffico, ctx.s)):
            for k in (-2, 0, 3):
                card = cards.build(id, ctx, k, cards.Caps(100))
                self.assertEqual(ansi.card_lines(card, 100)[0], fn(arg, 100, k), (id, k))

    def fs(self, n):
        return [{"mount": f"/m{i}", "used": 10 * 2 ** 30, "total": 100 * 2 ** 30} for i in range(n)]

    def test_disks_cap_at_five_with_a_more_and_say_so(self):
        ctx = ctx_of(s={"fs": self.fs(8)})
        caps = cards.Caps(100)
        card = cards.build("disks", ctx, 0, caps)
        self.assertTrue(card.truncated)
        self.assertEqual(caps.trunc, {"disks"})
        table = card.body[0]
        self.assertEqual(len(table.rows), 5)
        self.assertEqual(card.body[-1], More(3, "more"))
        self.assertEqual(card.more, More(3, "more"))
        lines, _ = ansi.card_lines(card, 100)
        self.assertEqual(plain(lines)[-1], " … +3 more")
        self.assertEqual(plain(lines)[1], " /m0            ███░░░░░░░░░░░░░░░░░░░░░░░░░░░ 10.0G/100.0G")

    def test_disks_in_full_or_expanded_show_all_and_no_more(self):
        ctx = ctx_of(s={"fs": self.fs(8)})
        for caps in (cards.Caps(100, full=True), cards.Caps(100, expand={"disks"})):
            card = cards.build("disks", ctx, 0, caps)
            self.assertFalse(card.truncated)
            self.assertEqual(len(card.body[0].rows), 8)
            self.assertFalse(any(isinstance(p, More) for p in card.body))
            self.assertEqual(len(ansi.card_lines(card, 100)[0]), 9)

    def test_disks_without_data_say_why(self):
        for fs, level, text in (([], "info", "no filesystems"), (None, "warn", "unavailable")):
            card = cards.build("disks", ctx_of(s={"fs": fs}), 0, cards.Caps(100))
            self.assertEqual(card.body, [Msg(level, text)])
        card = cards.build("disks", ctx_of(s={"fs": [{"mount": "/", "used": 1, "total": 0}]}), 0, cards.Caps(100))
        self.assertEqual(card.body[0].rows[0].cells[1].state, "unknown")  # a disk of no size is '?', not an empty bar that looks fine

    def test_sessions_levels_the_cap_and_the_tty_line(self):
        sess = {"local": [{"tty": "tty1"}, {"tty": "pts/0"}, {"tty": "pts/0"}], "ssh": [f"10.0.0.{i}" for i in range(6)] + ["8.8.8.8"]}
        ctx = ctx_of(s={"sessions": sess})
        caps = cards.Caps(60)
        card = cards.build("sessions", ctx, 0, caps)
        self.assertTrue(card.truncated)
        lines = plain(ansi.card_lines(card, 60)[0])
        self.assertEqual(lines[1], " 3 user sessions   ssh: 7 connected")
        self.assertEqual(len([x for x in lines if "ssh from" in x]), 4)
        self.assertIn(" … +3 more ssh clients", lines)
        self.assertEqual(lines[-1], " pts/0 tty1")
        full = plain(ansi.card_lines(cards.build("sessions", ctx, 0, cards.Caps(60, full=True)), 60)[0])
        self.assertEqual(len([x for x in full if "ssh from" in x]), 7)
        self.assertIn("✖ ssh from 8.8.8.8  address NOT local or Tailscale", " ".join(full).replace("  ", "  "))
        self.assertFalse(any("more ssh" in x for x in full))

    def test_sessions_tty_wrap_that_is_cut_marks_the_card(self):
        sess = {"local": [{"tty": f"pts/{i}"} for i in range(30)], "ssh": []}
        card = cards.build("sessions", ctx_of(s={"sessions": sess}), 0, cards.Caps(30))
        self.assertFalse(card.truncated)  # the cut happens when it is drawn, at its width
        lines, hid = ansi.card_lines(card, 30)
        self.assertTrue(hid)
        self.assertRegex(plain(lines)[-1], r"… \+\d+$")
        self.assertFalse(ansi.card_lines(cards.build("sessions", ctx_of(s={"sessions": sess}), 0, cards.Caps(30, full=True)), 30)[1])
        self.assertEqual(plain(ansi.card_lines(cards.build("sessions", ctx_of(s={"sessions": None}), 0, cards.Caps(30)), 30)[0])[1], "   ! unavailable")

    def test_tailscale_note_stale_exit_node_and_the_cap(self):
        now = time.time()
        peers = [{"name": f"n{i}", "os": "linux", "online": i % 2 == 0, "direct": i % 4 == 0, "relay": "fra", "last_seen": now - 3600 * i,
                  "exit": i == 2} for i in range(11)]
        net = {"ts": now - 600, "ts_peers": {"self": {"name": "me", "exit_option": True}, "peers": peers}}
        ctx = ctx_of(net=net, now=now)
        caps = cards.Caps(100)
        card = cards.build("tailscale", ctx, 0, caps)
        self.assertEqual(card.note, "me · 6/11 nodes online · exit node · stale data (10 min old)")
        self.assertTrue(card.truncated)
        lines = plain(ansi.card_lines(card, 100)[0])
        self.assertEqual(lines[-1], " … +3 nodes")
        self.assertEqual(lines[1], " ● n0                 linux    online direct")
        self.assertEqual(lines[2], " ○ n1                 linux    offline · seen 60 min ago")
        self.assertIn("via relay fra  exit node in use", lines[3])
        self.assertEqual(len(lines), 10)
        self.assertEqual(len(ansi.card_lines(cards.build("tailscale", ctx, 0, cards.Caps(100, full=True)), 100)[0]), 12)

    def test_tailscale_narrow_drops_the_os_column_not_the_state(self):
        now = time.time()
        peers = [{"name": "a-long-node-name", "os": "linux", "online": False, "direct": False, "relay": "", "last_seen": 0, "exit": True}]
        net = {"ts": now, "ts_peers": {"self": {"name": "me", "exit_option": False}, "peers": peers}}
        card = cards.build("tailscale", ctx_of(net=net, now=now), 0, cards.Caps(50))
        wide = plain(ansi.card_lines(card, 100)[0])[1]
        narrow = plain(ansi.card_lines(card, 50)[0])[1]
        self.assertIn("linux", wide)
        self.assertNotIn("linux", narrow)
        self.assertIn("never seen  exit node in use", narrow)

    def test_tailscale_and_docker_without_data_say_why(self):
        for id, net_key, kw in (("tailscale", "net", {"net": {"absent": ["ts_peers"], "ts": time.time()}}),
                                ("docker_disk", "boot", {"boot": {"absent": ["docker_df"], "ts": time.time()}})):
            card = cards.build(id, ctx_of(**kw), 0, cards.Caps(80))
            self.assertEqual(card.body, [Msg("info", "not installed on this machine")])
        card = cards.build("tailscale", ctx_of(net={"errors": {"ts_peers": "boom"}}), 0, cards.Caps(80))
        self.assertEqual(card.body, [Msg("warn", "unavailable: boom")])

    def test_docker_disk_rows_notes_and_the_stale_title(self):
        boot = {"ts": time.time() - 2000, "docker_df": {"rows": [{"type": "Images", "count": 12, "active": 3, "size": "4.1GB", "reclaimable": "1GB (24%)"},
                                                                  {"type": "Volumes", "count": 2, "active": 2, "size": "1GB", "reclaimable": "0B"}],
                                                         "dangling_images": {"count": 2, "bytes": 2 ** 30}, "volumes_unused": 1,
                                                         "volumes_unused_anonymous": 1}}
        card = cards.build("docker_disk", ctx_of(boot=boot), 0, cards.Caps(100))
        self.assertTrue(card.note.startswith("stale data ("))
        lines = plain(ansi.card_lines(card, 100)[0])
        self.assertEqual(lines[1], " Images          12 (3 in use)  4.1GB     unused 1GB (24%)")
        self.assertEqual(lines[2], " Volumes          2 (2 in use)  1GB       unused 0B")
        self.assertEqual(lines[3], " dangling images: 2 (1.0G): safe to prune")
        self.assertEqual(lines[4], " 1 unused volumes (1 anonymous): may hold data, check before pruning")
        self.assertEqual(lines[5], " unused = no container uses it; tagged images can be re-pulled")
        self.assertIn("\x1b[33m dangling images", ansi.card_lines(card, 100)[0][3])
        self.assertFalse(card.truncated)

    def test_docker_disk_cleans_what_docker_wrote(self):
        boot = {"ts": time.time(), "docker_df": {"rows": [{"type": "Im\x1b[2Jages", "count": "1\x1b[2J", "active": "2", "size": "3", "reclaimable": "\x1bcx"}]}}
        out = "\n".join(ansi.card_lines(cards.build("docker_disk", ctx_of(boot=boot), 0, cards.Caps(100)), 100)[0])
        self.assertNotIn("\x1b[2J", out)
        self.assertNotIn("\x1bc", out)

    def test_network_traffic_the_cap_the_columns_and_the_sparkline_width(self):
        hist = [100.0, 2000.0, 300.0]
        nets = {f"eth{i}": {"rx": 1500.0 * i, "tx": 20.0, "rx_tot": 10 * 2 ** 20 * (9 - i), "tx_tot": 2 ** 20, "hist_rx": hist, "hist_tx": hist}
                for i in range(8)}
        ctx = ctx_of(s={"net": nets})
        card = cards.build("network_traffic", ctx, 0, cards.Caps(120))
        self.assertEqual(card.note, "↓ received · ↑ sent")
        self.assertTrue(card.truncated)
        lines = plain(ansi.card_lines(card, 120)[0])
        self.assertEqual(len(lines), 1 + 5 + 1)
        self.assertEqual(lines[-1], " … +3 more")
        narrow = ansi.card_lines(cards.build("network_traffic", ctx, 0, cards.Caps(99)), 99)[0]
        wide = ansi.card_lines(card, 120)[0]
        self.assertGreater(ansi.vlen(wide[1]), ansi.vlen(narrow[1]))  # a shorter sparkline in a narrow column
        card = cards.build("network_traffic", ctx_of(s={"net": {}}), 0, cards.Caps(120))
        self.assertEqual(card.body, [Msg("info", "no interfaces")])

    def test_a_broken_native_card_is_its_title_and_a_message(self):
        card = cards.build("disks", ctx_of(s={"fs": [{"mount": "/"}]}), 0, cards.Caps(80))
        self.assertIsInstance(card.body[0], Msg)
        self.assertEqual(card.body[0].level, "err")
        lines = ansi.card_lines(card, 80)[0]
        self.assertEqual(lines[0], ansi.section("DISKS", 80))
        self.assertEqual(plain(lines)[1], "   ✖ KeyError('used')")

    def test_the_overview_marks_what_it_cut_for_the_details_pages(self):
        with golden.FrozenWorld():  # the configuration and the clock of the golden files, whatever the other tests left
            cont, net, boot, base = demo.snapshot(golden.NOW)
            s = dict(demo.sampler_data(None, golden.NOW), fs=[{"mount": f"/m{i}", "used": 1, "total": 2} for i in range(9)])
            det = []
            shown = render.page_overview(s, cont, net, boot, 200, 30, baseline=base, details=det)
        self.assertFalse(any("/m8" in x for x in plain(shown)), "the overview cut the disks")
        self.assertTrue(any("/m8" in "\n".join(plain(pg)) for pg in det), "the Details pages hold every disk")

    def test_every_native_card_is_html_without_leftovers(self):
        ctx = ctx_of()
        for id in NATIVE:
            out = htmlview.html(cards.build(id, ctx, 0, cards.Caps(100)))
            self.assertIn(f'data-card="{id}"', out)
            self.assertNotIn("\x1b", out)
            self.assertNotIn("<pre", out)
            self.assertEqual(len(re.findall("<article", out)), len(re.findall("</article>", out)))


if __name__ == "__main__":
    unittest.main()
