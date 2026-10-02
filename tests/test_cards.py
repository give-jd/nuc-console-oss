"""Tests for the components of ui.py and for cards.py: the card registry, the KPI model, Caps and the per-frame memo.

Nothing here draws a screen: the components are data, the registry builds Cards, the KPIs are read off a Ctx. That the console and the
web pages are byte-for-byte what they were is held by the golden files (tests/test_golden.py, where there are any) and the proof in
the commit message of the change that introduced the registry.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import ui  # noqa: E402


class ComponentTests(unittest.TestCase):
    def test_span_cleans_hostile_text_in_the_constructor(self):
        s = ui.Span("evil\x1b[31m\nname\x07\x9b")
        self.assertNotIn("\x1b", s.text)
        self.assertNotIn("\n", s.text)
        self.assertEqual(s.text, "evil?[31m?name??")
        self.assertEqual(ui.Span(None).text, "")
        self.assertEqual(ui.Span(42).text, "42")

    def test_span_tone_is_a_token(self):
        self.assertEqual(ui.Span("x", "err").tone, "err")
        with self.assertRaises(ValueError):
            ui.Span("x", "red")

    def test_line_makes_spans_and_cleans(self):
        ln = ui.Line(["a", ui.Span("b", "ok"), "c\x1b"])
        self.assertEqual([type(x) for x in ln.spans], [ui.Span] * 3)
        self.assertEqual(ln.text, "abc?")
        self.assertEqual(ui.Line("one").text, "one")

    def test_a_card_has_a_state(self):
        for st in ui.STATES:
            self.assertEqual(ui.Card("system", "SYSTEM", "", st, []).state, st)
        with self.assertRaises(ValueError):
            ui.Card("system", "SYSTEM", "", "fine", [])
        with self.assertRaises(TypeError):
            ui.Card("system", "SYSTEM", "", body=[])  # no state: it does not exist

    def test_card_cleans_title_and_note_and_holds_the_size(self):
        c = ui.Card("a", "T\x1b[1m", "n\n", "ok", [ui.Raw(["x"]), ui.Msg("ok", "y"), ui.Raw(["z"])], size=9)
        self.assertEqual((c.title, c.note, c.size), ("T?[1m", "n?", 1))
        self.assertEqual(c.lines, ["x", "z"])
        self.assertEqual(ui.Card("a", "T", "", "ok", [], size=3).size, 3)

    def test_kpi_requires_a_state_and_unknown_is_a_question_mark(self):
        with self.assertRaises(ValueError):
            ui.Kpi("cpu", "CPU", "5", "%", "fine")
        k = ui.Kpi("cpu", "CPU", "93", "%", "unknown")
        self.assertEqual((k.value, k.unit, k.symbol, k.state), ("?", "", "?", "unknown"))
        k = ui.Kpi("cpu", "CPU", "93", "%", "err")
        self.assertEqual((k.value, k.unit, k.symbol), ("93", "%", "✖"))
        self.assertEqual(ui.Kpi("x", "X", "1", "", "ok", spark=[1, None, "a", float("nan"), 2]).spark.values, [1.0, 2.0])

    def test_bar_none_is_unknown(self):
        b = ui.Bar(None, "12 of 40")
        self.assertEqual((b.state, b.value_text, b.frac), ("unknown", "?", None))
        self.assertEqual(ui.Bar(float("nan"), "x").state, "unknown")
        self.assertEqual(ui.Bar("0.5", "x").state, "unknown")  # a string is not a number

    def test_bar_states_follow_the_thresholds(self):
        self.assertEqual([ui.Bar(f, "").state for f in (0.0, 0.69, 0.7, 0.89, 0.9, 1.5)], ["ok", "ok", "warn", "warn", "err", "err"])
        self.assertEqual(ui.Bar(1.5, "").frac, 1.0)
        self.assertEqual(ui.Bar(-1, "").frac, 0.0)
        self.assertEqual(ui.Bar(0.5, "", warn=0.4, err=0.6).state, "warn")

    def test_spark_keeps_only_numbers(self):
        self.assertEqual(ui.Spark([1, 2.5, None, True, "3", float("inf")]).values, [1.0, 2.5])

    def test_pill_msg_notice_need_a_known_state_or_level(self):
        self.assertEqual(ui.Pill("ALL OK", "ok").state, "ok")
        with self.assertRaises(ValueError):
            ui.Pill("x", "green")
        for lv in ui.LEVELS:
            self.assertEqual(ui.Msg(lv, "t").level, lv)
            self.assertEqual(ui.Notice(lv, "t").level, lv)
        with self.assertRaises(ValueError):
            ui.Msg("unknown", "t")  # a message says ok, warn, err or info: an unread value is a Kpi / a Bar
        self.assertEqual(ui.Msg("err", "a\x1bb").text, "a?b")
        self.assertIsInstance(ui.Notice("warn", "t"), ui.Msg)

    def test_table_checks_its_rows_and_cleans_cells(self):
        cols = [ui.Col("a", "A"), ui.Col("b", "B", "r", prio=2, num=True)]
        t = ui.Table(cols, [ui.Row(["x\x1b", ui.Span("1", mono=True)], tone="warn", key="k", href="/x")], groups=[("G", 0)])
        self.assertEqual(t.rows[0].cells[0].text, "x?")
        self.assertEqual((t.cols[1].align, t.cols[1].prio, t.cols[1].num), ("r", 2, True))
        self.assertEqual(t.groups, [("G", 0)])
        with self.assertRaises(ValueError):
            ui.Table(cols, [ui.Row(["only one"])])
        with self.assertRaises(ValueError):
            ui.Row(["x"], tone="purple")

    def test_kv_wrap_group_tree_details_more(self):
        self.assertEqual(ui.KV([("a\x1b", "b")]).pairs[0][0], "a?")
        self.assertEqual([x.text for x in ui.Wrap(["a", ui.Span("b")]).items], ["a", "b"])
        self.assertEqual(ui.Group([ui.Raw([])], "t\x1b").title, "t?")
        tr = ui.Tree([(0, "root", "ok"), (1, "leaf", "unknown")])
        self.assertEqual([(d, s) for d, _, s in tr.rows], [(0, "ok"), (1, "unknown")])
        with self.assertRaises(ValueError):
            ui.Tree([(0, "root", "good")])
        self.assertEqual(ui.Details("sum", [ui.Raw([])]).summary.text, "sum")
        m = ui.More(3, "ports", "/?card=exposure")
        self.assertEqual((m.text, m.href), ("… +3 ports", "/?card=exposure"))
        self.assertEqual(ui.More(-2).n, 0)

    def test_equality_and_repr(self):
        self.assertEqual(ui.Span("a", "ok"), ui.Span("a", "ok"))
        self.assertNotEqual(ui.Span("a", "ok"), ui.Span("a", "err"))
        self.assertNotEqual(ui.Msg("ok", "a"), ui.Notice("ok", "a"))
        self.assertIn("Span(text='a'", repr(ui.Span("a")))

    def test_every_component_has_slots_and_no_dict(self):
        for cls in (ui.Span, ui.Line, ui.Raw, ui.Card, ui.Kpi, ui.Col, ui.Row, ui.Table, ui.KV, ui.Bar, ui.Spark, ui.Pill, ui.Msg,
                    ui.Notice, ui.Wrap, ui.Group, ui.Tree, ui.Details, ui.More):
            self.assertIn("__slots__", vars(cls), cls)
        for obj in (ui.Span("x"), ui.Line("x"), ui.Card("a", "T", "", "ok", []), ui.Kpi("a", "A", "1", "", "ok"), ui.Bar(0.5)):
            self.assertFalse(hasattr(obj, "__dict__"), obj)

    def test_worst_state(self):
        self.assertEqual(ui.worst([]), "ok")
        self.assertEqual(ui.worst(["ok", "warn", "unknown"]), "warn")
        self.assertEqual(ui.worst(["unknown", "ok"]), "unknown")
        self.assertEqual(ui.worst(["warn", "down", "err"]), "down")
        with self.assertRaises(ValueError):
            ui.worst(["nope"])


if __name__ == "__main__":
    unittest.main()
