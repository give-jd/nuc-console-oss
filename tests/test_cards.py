"""Tests for the components of ui.py and for cards.py: the card registry, the KPI model, Caps and the per-frame memo.

Nothing here draws a screen: the components are data, the registry builds Cards, the KPIs are read off a Ctx. That the console and the
web pages are byte-for-byte what they were is held by the golden files (tests/test_golden.py, where there are any) and the proof in
the commit message of the change that introduced the registry.
"""
import os
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ansi  # noqa: E402
import cards  # noqa: E402
import demo  # noqa: E402
import nuc_config  # noqa: E402
import prefs  # noqa: E402
import render  # noqa: E402
import cardlines  # noqa: E402
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


_SAVED = {}


def setUpModule():
    # render.demo_defaults() renames the host and declares [webapps] and [expose] for good: put them back for the tests that follow
    _SAVED.update(host=socket.gethostname, webapps=render.CFG["webapps"], expose=render.CFG["expose"])


def tearDownModule():
    socket.gethostname = _SAVED["host"]
    render.CFG["webapps"], render.CFG["expose"] = _SAVED["webapps"], _SAVED["expose"]


def plist(*items):
    """A render.ProblemList of (severity, text, problem id)."""
    out = render.ProblemList((sev, text) for sev, text, _ in items)
    out.pids = [pid for _, _, pid in items]
    return out


def demo_ctx(**kw):
    """A Ctx on the demo machine (the problems are the demo's: render.problems of its state)."""
    now = time.time()
    cont, net, boot, base = demo.snapshot(now)
    s = demo.sampler_data(None, now)
    with mock.patch.object(render, "DEMO", True):
        render.demo_defaults()
        pb = render.problems(net, cont, now, boot=boot, thermal=s["thermal"], baseline=base)
    args = dict(s=s, cont=cont, net=net, boot=boot, problems=pb, now=now, baseline=base,
                health={"report": {"findings": [{"level": "err"}, {"level": "warn"}, {"level": "info"}]}},
                ai={"enabled": True, "probe": {"state": "answering"}})
    args.update(kw)
    return cards.Ctx(**args)


class CapsTests(unittest.TestCase):
    def test_lim_caps_and_remembers(self):
        caps = cards.Caps(40)
        self.assertEqual(caps.lim([1, 2, 3, 4], 2, "disks"), [1, 2])
        self.assertEqual(caps.trunc, {"disks"})
        self.assertEqual(caps.lim([1, 2], 2, "sessions"), [1, 2])
        self.assertEqual(caps.trunc, {"disks"})

    def test_full_and_expanded_show_everything(self):
        self.assertEqual(cards.Caps(40, full=True).lim([1, 2, 3], 1, "disks"), [1, 2, 3])
        caps = cards.Caps(40, expand={"disks"})
        self.assertEqual(caps.lim([1, 2, 3], 1, "disks"), [1, 2, 3])
        self.assertEqual(caps.lim([1, 2, 3], 1, "sessions"), [1])
        self.assertTrue(caps.opened("disks") and not caps.opened("sessions"))
        self.assertEqual(caps.trunc, {"sessions"})

    def test_it_shares_the_sets_it_is_given(self):
        seq, expand, trunc = list(range(9)), {"boot"}, set()
        caps = cards.Caps(30, False, expand, trunc)
        self.assertEqual(caps.lim(seq, 3, "boot"), seq)
        self.assertEqual(caps.lim(seq, 3, "disks"), [0, 1, 2])
        self.assertIs(caps.expand, expand)
        self.assertIs(caps.trunc, trunc)
        self.assertEqual(trunc, {"disks"})

    def test_key_tells_what_changes_the_lines(self):
        a = cards.Caps(40, expand={"disks"})
        self.assertNotEqual(a.key("disks"), a.key("boot"))
        self.assertNotEqual(a.key("boot"), cards.Caps(41, expand={"disks"}).key("boot"))
        self.assertNotEqual(a.key("boot"), cards.Caps(40, full=True, expand={"disks"}).key("boot"))


class RegistryTests(unittest.TestCase):
    def test_every_section_is_registered_in_order(self):
        self.assertEqual(list(cards.CARDS), list(nuc_config.SECTIONS))
        self.assertEqual(list(cards.CARDS), list(prefs.CARDS))
        for id, entry in cards.CARDS.items():
            self.assertEqual(entry.id, id)
            self.assertTrue(entry.title)

    def test_feature_switched_off_is_absent(self):
        cfg = {"features": {f: True for f in nuc_config.FEATURES}}
        self.assertEqual(cards.ids(cfg), list(nuc_config.SECTIONS))
        cfg["features"].update(containers=False, tailscale=False)
        self.assertNotIn("containers", cards.ids(cfg))
        self.assertNotIn("tailscale", cards.ids(cfg))
        self.assertIn("system", cards.ids(cfg))  # always there
        self.assertIn("attention", cards.ids(cfg))
        self.assertFalse(cards.enabled("nope", cfg))

    def test_the_overview_leaves_out_a_card_whose_feature_is_off(self):
        cont, net, boot, base = demo.snapshot(time.time())
        s = demo.sampler_data(None)
        with mock.patch.object(render, "DEMO", True):
            full = "\n".join(render.page_overview(s, cont, net, boot, 200, 60, baseline=base))
            with mock.patch.dict(render.CFG["features"], {"firewall": False}):
                part = "\n".join(render.page_overview(s, cont, net, boot, 200, 60, baseline=base))
        self.assertIn("FIREWALL", full)
        self.assertNotIn("FIREWALL", part)
        self.assertIn("EXPOSURE", part)

    def test_build_gives_a_card_with_the_lines_and_a_state(self):
        ctx = demo_ctx()
        for id in cards.CARDS:
            card = cards.build(id, ctx, 0, cards.Caps(80))
            self.assertIsInstance(card, ui.Card)
            self.assertEqual(card.id, id)
            self.assertIn(card.state, ui.STATES)
            lines, _hid = ansi.card_lines(card, 80)
            self.assertTrue(lines and all(isinstance(x, str) for x in lines))
            # the cards built of components are not Raw; the others still draw the lines render.py has always drawn
            self.assertEqual(isinstance(card.body[0], ui.Raw), id not in ("disks", "docker_disk", "sessions", "tailscale", "network_traffic", "system", "containers", "databases", "boot",
                                                                         "attention", "exposure", "firewall", "webapps"))

    def test_the_card_is_what_the_section_draws(self):
        ctx = demo_ctx()
        caps = cards.Caps(100)
        self.assertEqual(ansi.card_lines(cards.build("firewall", ctx, 0, caps), 100)[0], cardlines.ov_firewall(ctx.net, 100, 0))
        self.assertEqual(ansi.card_lines(cards.build("disks", ctx, 0, caps), 100)[0], cardlines.ov_dischi(ctx.s, 100, 0))
        self.assertEqual(ansi.card_lines(cards.build("attention", ctx, 0, caps), 100)[0], cardlines.ov_attention(ctx.problems, 100, 0))

    def test_a_broken_section_becomes_a_message_not_an_exception(self):
        ctx = demo_ctx(s={"cpu": "nonsense"})
        card = cards.build("system", ctx, 0, cards.Caps(80))
        self.assertEqual(ansi.card_lines(card, 80)[0][0], ansi.section("SYSTEM", 80))
        self.assertEqual(card.body[0].level, "err")
        self.assertEqual(len(ansi.card_lines(card, 80)[0]), 2)

    # ---- the problems of a card
    def test_every_problem_has_its_cards_and_they_exist(self):
        for pid in render.CATALOG:
            self.assertIn(pid, cards.PROBLEM_CARDS, pid)
        for pid, ids in cards.PROBLEM_CARDS.items():
            self.assertTrue(set(ids) <= set(nuc_config.SECTIONS), pid)

    def test_state_follows_the_problems_of_the_card(self):
        ctx = demo_ctx(problems=plist((2, "1 DB/broker open on LAN", "db-open-lan"), (1, "x", "docker-bypass"), (1, "t", "telegram-failing")))
        st = {i: cards.card_state(i, ctx) for i in cards.CARDS}
        self.assertEqual((st["databases"], st["exposure"], st["firewall"]), ("err", "err", "warn"))
        self.assertEqual(st["attention"], "err")
        self.assertEqual((st["system"], st["boot"], st["disks"], st["sessions"]), ("ok", "ok", "ok", "ok"))

    def test_port_changes_are_errors_and_warnings_are_warnings(self):
        err = demo_ctx(problems=plist((3, "NEW exposed port", "port-new")))
        self.assertEqual((cards.card_state("exposure", err), cards.card_state("attention", err)), ("err", "err"))
        warn = demo_ctx(problems=plist((1, "2 errors", "journal-errors")))
        self.assertEqual((cards.card_state("boot", warn), cards.card_state("attention", warn), cards.card_state("exposure", warn)),
                         ("warn", "warn", "ok"))
        self.assertEqual(cards.card_state("attention", demo_ctx(problems=plist())), "ok")

    def test_a_missing_source_is_unknown_whatever_the_problems_say(self):
        ctx = demo_ctx(net=None, problems=plist((2, "network collector not running", "collector-net")))
        for id in ("exposure", "firewall", "webapps", "databases", "tailscale"):
            self.assertEqual(cards.card_state(id, ctx), "unknown", id)
        self.assertEqual(cards.card_state("attention", ctx), "err")
        self.assertEqual(cards.card_state("containers", ctx), "ok")
        ctx = demo_ctx(cont=None, boot=None, s={"cpu": {}, "net": None, "sessions": None, "fs": None, "mem": None, "disk_root": None})
        for id in ("containers", "boot", "docker_disk", "system", "network_traffic", "sessions", "disks"):
            self.assertEqual(cards.card_state(id, ctx), "unknown", id)
            self.assertEqual(cards.build(id, ctx, 0, cards.Caps(80)).state, "unknown", id)

    def test_nothing_to_show_on_purpose_is_info(self):
        ctx = demo_ctx(cont={"ts": 0, "containers": [], "absent": True}, problems=plist())
        self.assertEqual(cards.card_state("containers", ctx), "info")
        ctx = demo_ctx(s=dict(demo_ctx().s, fs=[], net={}), problems=plist())
        self.assertEqual((cards.card_state("disks", ctx), cards.card_state("network_traffic", ctx)), ("info", "info"))

    def test_a_list_that_does_not_say_which_problem_is_which(self):
        ctx = demo_ctx(problems=[(1, "something")])
        self.assertEqual(cards.card_state("attention", ctx), "warn")
        self.assertEqual(cards.card_state("exposure", ctx), "unknown")
        self.assertEqual(cards.card_state("exposure", demo_ctx(problems=[])), "ok")

    def test_problems_carry_their_ids(self):
        pb = demo_ctx().problems
        self.assertEqual(len(pb), len(pb.pids))
        self.assertIn("db-open-lan", pb.pids)


class MemoTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.saved = dict(cards.CARDS)

        def builder(ctx, k, caps):
            self.calls.append((k, caps.width, caps.full, "memo-x" in caps.expand))
            caps.trunc.add("memo-x")
            return ui.Card("memo-x", "X", "", "ok", [ui.Raw([f"k={k}"])])
        cards.register("memo-x", "X", True, builder)

    def tearDown(self):
        cards.CARDS.clear()
        cards.CARDS.update(self.saved)

    def test_a_builder_runs_once_per_frame_per_key(self):
        ctx = cards.Ctx()
        caps = cards.Caps(40)
        first = cards.build("memo-x", ctx, 1, caps)
        self.assertIs(cards.build("memo-x", ctx, 1, caps), first)
        self.assertEqual(len(self.calls), 1)
        cards.build("memo-x", ctx, 2, caps)  # another level
        cards.build("memo-x", ctx, 1, cards.Caps(41))  # another width
        cards.build("memo-x", ctx, 1, cards.Caps(40, expand={"memo-x"}))  # its caps lifted
        cards.build("memo-x", ctx, 1, cards.Caps(40, full=True))
        self.assertEqual(len(self.calls), 5)
        cards.build("memo-x", cards.Ctx(), 1, caps)  # another frame
        self.assertEqual(len(self.calls), 6)

    def test_what_another_section_lifted_does_not_matter(self):
        ctx = cards.Ctx()
        cards.build("memo-x", ctx, 0, cards.Caps(40))
        cards.build("memo-x", ctx, 0, cards.Caps(40, expand={"disks"}))
        self.assertEqual(len(self.calls), 1)

    def test_truncation_is_told_again_when_the_card_is_remembered(self):
        ctx = cards.Ctx()
        caps = cards.Caps(40, trunc={"old"})
        card = cards.build("memo-x", ctx, 0, caps)
        self.assertTrue(card.truncated)
        self.assertEqual(caps.trunc, {"old", "memo-x"})
        caps.trunc.clear()
        self.assertIs(cards.build("memo-x", ctx, 0, caps), card)
        self.assertEqual(caps.trunc, {"memo-x"})
        self.assertEqual(len(self.calls), 1)

    def test_a_card_that_hid_nothing_is_not_truncated_and_keeps_the_old_marks(self):
        cards.register("memo-y", "Y", True, lambda ctx, k, caps: ui.Card("memo-y", "Y", "", "ok", []))
        caps = cards.Caps(40, trunc={"disks"})
        self.assertFalse(cards.build("memo-y", cards.Ctx(), 0, caps).truncated)
        self.assertEqual(caps.trunc, {"disks"})

    def test_a_failing_builder_leaves_the_marks_alone(self):
        def boom(ctx, k, caps):
            caps.trunc.add("x")
            raise RuntimeError("no")
        cards.register("memo-z", "Z", True, boom)
        caps = cards.Caps(40, trunc={"disks"})
        with self.assertRaises(RuntimeError):
            cards.build("memo-z", cards.Ctx(), 0, caps)
        self.assertEqual(caps.trunc, {"disks", "x"})

    def test_ctx_once(self):
        ctx, n = cards.Ctx(), []
        self.assertEqual(ctx.once("a", lambda: n.append(1) or 7), 7)
        self.assertEqual(ctx.once("a", lambda: n.append(1) or 8), 7)
        self.assertEqual(n, [1])

    def test_the_overview_builds_each_section_once_per_level_and_caps(self):
        """page_overview asks for every card again at each level and while it lifts the caps: the registry answers from the frame."""
        seen = []
        real = cards.CARDS["firewall"].builder

        def spy(ctx, k, caps):
            seen.append((k, caps.width, caps.full, "firewall" in caps.expand))
            return real(ctx, k, caps)
        cont, net, boot, base = demo.snapshot(time.time())
        s = demo.sampler_data(None)
        with mock.patch.object(cards.CARDS["firewall"], "builder", spy), mock.patch.object(render, "DEMO", True):
            for w, h in ((79, 24), (120, 33), (200, 50)):
                del seen[:]
                render.page_overview(s, cont, net, boot, w, h - 2, baseline=base)
                self.assertTrue(seen)
                self.assertEqual(len(seen), len(set(seen)), (w, h))


class KpiTests(unittest.TestCase):
    def test_every_kpi_id_has_a_builder_and_a_label(self):
        self.assertEqual(list(cards.KPIS), list(prefs.KPI_IDS))
        self.assertEqual(set(cards.KPI_LABELS), set(prefs.KPI_IDS))

    def test_each_has_a_normal_value_on_the_demo_machine(self):
        ctx = demo_ctx()
        got = {k.id: k for k in cards.kpis(ctx, prefs.KPI_IDS)}
        self.assertEqual(list(got), list(prefs.KPI_IDS))
        for id, k in got.items():
            self.assertIsInstance(k, ui.Kpi)
            self.assertEqual(k.label, cards.KPI_LABELS[id])
            self.assertNotEqual(k.state, "unknown", id)
            self.assertNotEqual(k.value, "?", id)
        self.assertEqual((got["problems"].value, got["problems"].state), (str(len(ctx.problems)), "err"))
        self.assertEqual((got["internet"].value, got["internet"].state), ("1", "err"))
        self.assertEqual(got["db_lan"].state, "err")
        self.assertEqual((got["firewall"].value, got["firewall"].state), ("on", "ok"))
        busy = demo.machine("linux")["busy"]  # the one demo machine: its CPU figure, whatever the table says
        self.assertEqual((got["cpu"].value, got["cpu"].unit, got["cpu"].state), (str(round(sum(busy) / len(busy))), "%", "ok"))
        self.assertEqual((got["ram"].unit, got["ram"].state), ("%", "ok"))
        self.assertEqual((got["temp"].unit, got["load"].value), ("°C", "0.82"))
        self.assertEqual((got["containers"].value, got["containers"].unit, got["containers"].state), ("6", "/7", "warn"))
        self.assertEqual((got["unhealthy"].value, got["failed_units"].value, got["ssh"].value), ("0", "0", "1"))
        self.assertEqual((got["tailnet"].value, got["tailnet"].unit), ("1", "/2"))
        self.assertTrue(got["rx"].spark.values and got["tx"].spark.values)
        self.assertEqual(got["uptime"].value, "5d 0h")
        self.assertEqual((got["health"].value, got["health"].state), ("2", "err"))
        self.assertEqual((got["ai"].value, got["ai"].state), ("on", "ok"))

    def test_thresholds(self):
        hot = dict(demo_ctx().s, cpu={"cpu0": 0.95, "cpu1": 0.95}, mem={"MemTotal": 100, "MemAvailable": 20}, disk_root=(95, 100, "/"),
                   thermal={"cpu": (85.0, 100.0)}, load=["5.0", "1", "1"])
        got = {k.id: k for k in cards.kpis(demo_ctx(s=hot), ("cpu", "ram", "disk", "temp", "load"))}
        self.assertEqual({i: k.state for i, k in got.items()}, {"cpu": "err", "ram": "warn", "disk": "err", "temp": "warn", "load": "err"})
        t = cards.kpis(demo_ctx(s=dict(hot, thermal={"cpu": (95.0, 100.0)})), ("temp",))[0]
        self.assertEqual(t.state, "err")

    def test_a_missing_source_is_unknown_and_never_ok(self):
        for ctx in (cards.Ctx(), demo_ctx(s=None, cont=None, net=None, boot=None, problems=None, health=None, ai=None),
                    demo_ctx(s={}, cont={}, net={}, boot={}, problems=None, health={}, ai={}),
                    demo_ctx(s={"cpu": {}, "mem": {}, "disk_root": None, "thermal": {}, "load": None, "uptime": None, "net": {},
                                "sessions": None}, problems=None, health=None, ai=None, net={"listeners": None}, cont={"containers": None},
                              boot={"failed": None})):
            for k in cards.kpis(ctx, prefs.KPI_IDS):
                self.assertEqual((k.state, k.value, k.symbol), ("unknown", "?", "?"), (k.id, k))
        # a source that is there but says nothing is not a number either
        junk = demo_ctx(s={"cpu": {"cpu0": "x", "cpu1": None}, "mem": {"MemTotal": 0, "MemAvailable": 0}, "disk_root": ("a", 0, "/"),
                           "thermal": {"cpu": ("hot", 100)}, "load": ["a", "b", "c"], "uptime": "n/a",
                           "net": {"eth0": {"rx": "x"}}, "sessions": {"ssh": None}})
        for k in cards.kpis(junk, ("cpu", "ram", "disk", "temp", "load", "uptime", "rx", "tx", "ssh")):
            self.assertEqual((k.state, k.value), ("unknown", "?"), k.id)

    def test_the_os_has_no_load_average(self):
        k = cards.kpis(demo_ctx(s=dict(demo_ctx().s, load=[])), ("load",))[0]
        self.assertEqual((k.state, k.value), ("unknown", "?"))
        self.assertIn("no load average", k.hint)

    def test_a_missing_collector_makes_its_kpis_unknown(self):
        k = {x.id: x for x in cards.kpis(demo_ctx(net=None), ("internet", "lan", "beyond", "db_lan", "firewall", "tailnet", "cpu"))}
        self.assertEqual([k[i].state for i in ("internet", "lan", "beyond", "db_lan", "firewall", "tailnet")], ["unknown"] * 6)
        self.assertNotEqual(k["cpu"].state, "unknown")
        k = {x.id: x for x in cards.kpis(demo_ctx(cont=None, boot=None), ("containers", "unhealthy", "failed_units"))}
        self.assertEqual({x.state for x in k.values()}, {"unknown"})

    def test_what_is_not_there_on_purpose_is_info(self):
        k = cards.kpis(demo_ctx(cont={"ts": 0, "containers": [], "absent": True}), ("containers", "unhealthy"))
        self.assertEqual([(x.state, x.value) for x in k], [("info", "-")] * 2)
        ctx = demo_ctx()
        with mock.patch.dict(render.CFG, {"expose": {}}):
            self.assertEqual(cards.kpis(ctx, ("beyond",))[0].state, "info")
        net = dict(demo_ctx().net, ts_peers=None, absent=["ts_peers"])
        self.assertEqual(cards.kpis(demo_ctx(net=net), ("tailnet",))[0].state, "info")

    def test_firewall_states(self):
        net = demo_ctx().net
        off = dict(net, ufw=dict(net["ufw"], active=False))
        state = lambda n: cards.kpis(demo_ctx(net=n), ("firewall",))[0].state  # noqa: E731
        self.assertEqual([state(off), state(dict(net, ufw=None, absent=["ufw"])), state(dict(net, ufw=None, disabled=["ufw"])),
                          state(dict(net, ufw=None))], ["err", "warn", "info", "unknown"])
        win = dict(net, os="windows", firewall={"off": ["public"]})
        self.assertEqual(state(win), "err")
        self.assertEqual(state(dict(win, firewall={"off": []})), "ok")
        self.assertEqual(state(dict(win, firewall=None)), "unknown")

    def test_problems_ai_health(self):
        self.assertEqual(cards.kpis(demo_ctx(problems=plist()), ("problems",))[0].state, "ok")
        acc = plist()
        acc.accepted = 2
        self.assertIn("2 accepted", cards.kpis(demo_ctx(problems=acc), ("problems",))[0].hint)
        self.assertEqual(cards.kpis(demo_ctx(problems=plist((1, "w", "stale-net"))), ("problems",))[0].state, "warn")
        ai = lambda **kw: cards.kpis(demo_ctx(ai=dict(enabled=True, **kw)), ("ai",))[0]  # noqa: E731
        self.assertEqual(ai(probe={"state": "down"}).state, "down")
        self.assertEqual(ai(probe=None).state, "unknown")
        self.assertEqual(cards.kpis(demo_ctx(ai={"enabled": False, "probe": {"state": "off"}}), ("ai",))[0].state, "info")
        h = lambda f: cards.kpis(demo_ctx(health={"report": {"findings": f}}), ("health",))[0].state  # noqa: E731
        self.assertEqual((h([]), h([{"level": "warn"}]), h([{"level": "info"}])), ("ok", "warn", "ok"))
        self.assertEqual(cards.kpis(demo_ctx(health={"report": None, "msg": "no history"}), ("health",))[0].state, "unknown")

    def test_ssh_from_outside(self):
        s = dict(demo_ctx().s, sessions={"local": [], "ssh": ["203.0.113.9"]})
        k = cards.kpis(demo_ctx(s=s), ("ssh",))[0]
        self.assertEqual((k.state, k.value), ("err", "1"))

    def test_ids_default_and_unknown_ids(self):
        ctx = demo_ctx()
        self.assertEqual([k.id for k in cards.kpis(ctx)], prefs.preset_prefs("default")["kpis"])
        self.assertEqual([k.id for k in cards.kpis(ctx, ["cpu", "nope", "ram"])], ["cpu", "ram"])

    def test_a_builder_that_raises_is_unknown(self):
        with mock.patch.dict(cards.KPIS, {"cpu": lambda ctx: 1 / 0}):
            k = cards.kpis(demo_ctx(), ("cpu", "ram"))
        self.assertEqual((k[0].state, k[0].value, k[1].state != "unknown"), ("unknown", "?", True))

    def test_hostile_text_is_cleaned(self):
        s = dict(demo_ctx().s, disk_root=(1, 4, "C:\x1b[31m\nx"))
        self.assertNotIn("\x1b", cards.kpis(demo_ctx(s=s), ("disk",))[0].hint)
        boot = dict(demo_ctx().boot, failed=["evil\x1b[1m.service"])
        self.assertNotIn("\x1b", cards.kpis(demo_ctx(boot=boot), ("failed_units",))[0].hint)


if __name__ == "__main__":
    unittest.main()
