"""The system, containers, databases and boot cards, built of components (PR 14), and the components they added to ui.py.

The console's text for them is held byte for byte by the golden files (tests/test_golden.py); here the strings at several detail levels
and widths are held on the demo machine, the components are checked (a Bar carries its value, an unknown figure is '?' with the state
unknown), the HTML is checked for structure and escaping, and a container, a unit or a database named like an attack is neutral.
"""
import copy
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
import ansi  # noqa: E402
import cards  # noqa: E402
import demo  # noqa: E402
import htmlview  # noqa: E402
import render  # noqa: E402
import cardlines  # noqa: E402
import ui  # noqa: E402
from ui import Bar, Col, Flow, Grid, Head, Indent, Line, Msg, NoteTable, Row, Span, Table, Timeline  # noqa: E402

HOSTILE = '<script>alert(1)</script>"\'&\x1b[31m\nx'


def plain(lines):
    return [ansi.ANSI.sub("", x) for x in lines]


def ctx_of(**kw):
    now = time.time()
    cont, net, boot, base = demo.snapshot(now)
    s = demo.sampler_data(None, now)
    args = dict(s=s, cont=cont, net=net, boot=boot, problems=[], now=now, baseline=base)
    args.update(kw)
    return cards.Ctx(**args)


def card_text(id, ctx, k, w=100, **caps):
    card = cards.build(id, ctx, k, cards.Caps(w, **caps))
    return card, plain(ansi.card_lines(card, w)[0])


def walk(node):
    """Every component under a node (cards, groups, tables' cells and the notes of a NoteTable)."""
    yield node
    kids = list(getattr(node, "body", []) or []) + list(getattr(node, "children", []) or []) + list(getattr(node, "spans", []) or [])
    if isinstance(node, (ui.Table, ui.NoteTable)):
        for r in node.rows:
            kids += r.cells
    if isinstance(node, ui.NoteTable):
        for ns in node.notes:
            kids += ns
    if isinstance(node, ui.Wrap) or isinstance(node, ui.Flow):
        kids += node.items
    for kid in kids:
        for x in walk(kid):
            yield x


def kinds(card, cls):
    return [x for x in walk(card) if isinstance(x, cls)]


class NewComponentTests(unittest.TestCase):
    def test_they_have_slots_and_equal_by_fields(self):
        for obj in (Head("T"), Indent([]), Grid([]), Timeline([("a", 1)], 1, 10), NoteTable([Col("a", "A")], []), Flow([])):
            self.assertFalse(hasattr(obj, "__dict__"), obj)
        self.assertEqual(Head("T", "n"), Head("T", "n"))
        self.assertNotEqual(Head("T", "n"), Head("T", "m"))
        self.assertNotEqual(Bar(0.5, "x", tone="accent"), Bar(0.5, "x"))

    def test_text_is_cleaned_in_the_constructors(self):
        self.assertNotIn("\x1b", Head(HOSTILE, HOSTILE).title + Head(HOSTILE, HOSTILE).note)
        self.assertNotIn("\n", Timeline([(HOSTILE, 1)], 1).parts[0][0])
        self.assertEqual(Timeline([("a", None), ("b", "x"), ("c", 2)], 4).parts, [("c", 2.0)])
        with self.assertRaises(ValueError):
            Bar(0.5, tone="nope")
        with self.assertRaises(ValueError):
            NoteTable([Col("a", "A")], [Row(["x", "y"])])

    def test_head_indent_grid_flow_timeline_on_the_console(self):
        self.assertEqual(ansi.render(Head("T", "note"), 40)[0], [ansi.section("T", 40, "note")])
        self.assertEqual(plain(ansi.render(Indent([Line(["a"]), Msg("ok", "b")], 2), 40)[0]), ["  a", "     ✔ b"])
        grid = Grid([Line([f" {i} ", Bar(i / 4, f"{i * 25}%", w=4)]) for i in range(5)], 12, 2)
        lines = plain(ansi.render(grid, 40)[0])
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(len(x) == 24 for x in lines[:2]))  # every cell padded to its width
        self.assertEqual(len(lines[2]), 12)  # and the last row too
        self.assertEqual(lines[0], " 0 ░░░░ 0%   1 █░░░ 25% ")
        flow, hid = ansi.render(Flow(["aaa", "bbb", "ccc"], Span("→", "muted"), 8, "   ", None), 20)
        self.assertEqual(plain(flow), ["      → aaa   bbb", "        ccc"])
        self.assertFalse(hid)
        cut = plain(ansi.render(Flow(["x" * 30], None, 4, "  ", 10), 60)[0])
        self.assertEqual(cut, ["    " + "x" * 10])  # each fact is cut to its width
        tl, _ = ansi.render(Timeline([("kernel", 1.0), ("userspace", 3.0)], 4.0, 20), 60)
        self.assertEqual(plain(tl), ["   " + "█" * 5 + "█" * 15, "   ■ kernel 1.0s  ■ userspace 3.0s"])

    def test_a_bar_with_a_tone_is_not_a_level(self):
        full = ansi.render(Bar(0.95, "", tone="accent", w=4), 40)[0][0]
        self.assertEqual(full, ansi.c(ui.sgr("accent"), "████") + ansi.c(ui.sgr("muted"), ""))
        self.assertIn("\x1b[32m", ansi.render(Bar(0.95, "", 2, 2, 4), 40)[0][0])  # (a plain bar over its limits would be red)

    def test_notetable_puts_the_notes_under_their_row(self):
        t = NoteTable([Col("a", "A", w=4), Col("b", "B")], [Row(["r1", "x"]), Row(["r2", "y"])], [[Line(["  note"])], []])
        self.assertEqual(plain(ansi.render(t, 40)[0]), [" r1   x", "  note", " r2   y"])
        out = htmlview.html(t)
        self.assertEqual(out.count('<tr class="sub">'), 1)
        self.assertLess(out.index("note"), out.index("r2"))
        self.assertIn('<td colspan="2">', out)

    def test_html_of_the_new_components(self):
        self.assertEqual(htmlview.html(Head("T", "n")), '<h3 class="sub">T <span class="note">n</span></h3>')
        self.assertEqual(htmlview.html(Indent([Line(["a"])])), '<div class="ind"><p class="ln">a</p></div>')
        g = htmlview.html(Grid([Line(["a"]), Line(["b"])], 10, 2))
        self.assertEqual(g, '<ul class="grid"><li>a</li><li>b</li></ul>')
        tl = htmlview.html(Timeline([("main path", 1.0), ("kernel", 3.0)], 4.0, 20))
        self.assertIn('<rect class="tp tp-main-path" x="0.0" y="0" width="25.0" height="8"/>', tl)
        self.assertIn('x="25.0"', tl)
        self.assertIn('width="75.0"', tl)
        self.assertIn('<li class="tp-kernel">', tl)
        self.assertNotIn("style=", tl)
        self.assertEqual(htmlview.html(Flow(["a"], Span("→"))), '<p class="flow"><span class="lead">→</span> <span class="fi">a</span></p>')
        self.assertIn('class="bar st-ok t-accent"', htmlview.html(Bar(0.5, "", tone="accent")))

    def test_html_of_the_new_components_is_escaped(self):
        for node in (Head(HOSTILE, HOSTILE), Indent([Line([HOSTILE])]), Grid([Line([HOSTILE])]), Timeline([(HOSTILE, 1.0)], 1.0),
                     NoteTable([Col(HOSTILE, HOSTILE)], [Row([HOSTILE])], [[Line([HOSTILE])]]), Flow([HOSTILE], Span(HOSTILE))):
            out = htmlview.html(node)
            self.assertNotIn("<script", out)
            self.assertNotIn("\x1b", out)
            self.assertNotIn('"\'&\n', out)
            self.assertNotIn("style=", out)


class SystemCardTests(unittest.TestCase):
    def test_ram_disk_and_cpu_are_bars_with_their_values(self):
        card = cards.build("system", ctx_of(), 0, cards.Caps(120))
        bars = kinds(card, Bar)
        self.assertTrue(all(b.state in ui.STATES and b.value_text for b in bars))
        text = plain(ansi.card_lines(card, 120)[0])
        self.assertTrue(text[0].startswith("── SYSTEM"))
        self.assertIn("up 5d", text[0])
        self.assertRegex(text[1], r"^ RAM   [█░]+ \d+\.\dG/\d+\.\dG   cache ")
        self.assertRegex(text[2], r"^ DISK  [█░]+ \d+\.\dG/\d+\.\dG$")
        self.assertTrue(any(x.startswith(" TEMP ") and "limits" in x and "°C/" in x for x in text))
        self.assertTrue(any(x.startswith(" CPU   ") and "threads" in x for x in text))

    def test_the_cores_are_a_grid_of_bars_and_a_spark_in_the_tiny_levels(self):
        ctx = ctx_of()
        card = cards.build("system", ctx, 0, cards.Caps(120))
        grid = kinds(card, Grid)[0]
        self.assertEqual(len(grid.items), len(ctx.s["cpu"]))
        self.assertEqual(grid.per, 5)  # a cell is 20 columns wide
        self.assertEqual(kinds(cards.build("system", ctx, 0, cards.Caps(60)), Grid)[0].per, 2)
        tiny = cards.build("system", ctx, 3, cards.Caps(80))
        self.assertEqual(kinds(tiny, Grid), [])
        cpu = plain(ansi.card_lines(tiny, 80)[0])[-1]
        self.assertRegex(cpu, r"^ CPU   [█░]+ \d+%   core [▁▂▃▄▅▆▇█]{" + str(len(ctx.s["cpu"])) + "}$")

    def test_the_heaviest_containers_only_when_there_is_room(self):
        ctx = ctx_of()
        _, rich = card_text("system", ctx, -2)
        _, normal = card_text("system", ctx, 0)
        self.assertIn(" HEAVIEST CONTAINERS (RAM)", rich)
        self.assertNotIn(" HEAVIEST CONTAINERS (RAM)", normal)
        row = rich[rich.index(" HEAVIEST CONTAINERS (RAM)") + 1]
        self.assertTrue(row.startswith(" shop-api-1"))
        self.assertEqual(row[36:43], "520M   ")
        self.assertEqual(row[43:], "█" * 20)

    def test_the_heaviest_containers_are_capped_and_the_card_says_so(self):
        cont = copy.deepcopy(ctx_of().cont)
        card = cards.build("system", ctx_of(cont=cont), -2, cards.Caps(120))
        self.assertTrue(card.truncated)  # six containers have a memory: five are shown
        self.assertFalse(cards.build("system", ctx_of(cont=cont), -2, cards.Caps(120, full=True)).truncated)

    def test_what_could_not_be_read_is_a_question_mark_with_the_state_unknown(self):
        s = dict(ctx_of().s, mem=None, disk_root=None)
        card = cards.build("system", ctx_of(s=s), 0, cards.Caps(100))
        text = plain(ansi.card_lines(card, 100)[0])
        self.assertEqual(text[1:3], [" RAM   ?", " DISK  ?"])
        self.assertEqual([x.tone for x in kinds(card, Span) if x.text == "?"], ["unknown", "unknown"])
        self.assertEqual(cards.build("system", ctx_of(s={}), 0, cards.Caps(100)).state, "unknown")
        unreadable = dict(ctx_of().s, thermal={"cpu": (None, 90.0)})
        card = cards.build("system", ctx_of(s=unreadable), 0, cards.Caps(100))
        self.assertIn(" TEMP  ?", plain(ansi.card_lines(card, 100)[0]))
        self.assertEqual(Bar(None, "x").state, "unknown")  # (and a bar that is given no fraction says '?' whatever it is told to say)
        self.assertEqual(Bar(None, "x").value_text, "?")

    def test_the_thermal_line_drops_the_clock_then_the_limits_when_narrow(self):
        th = {"cpu": (50.0, 100.0), "nvme": (40.0, 70.0), "clk": (3.1, 4.2)}
        lens = [[ansi.vlen(ansi.render(p, 0)[0][0]) for p in cards.thermal_parts(th, 10, mw)] for mw in (None, 60, 30)]
        self.assertEqual(lens, [[65, 44], [45, 44], [28, 27]])  # clock, limits, value: what is left of the line as the room shrinks
        texts = [plain(ansi.render(p, 0)[0])[0] for p in cards.thermal_parts(th, 10, 60)]
        self.assertIn("limits", texts[0])
        self.assertNotIn("clock", texts[0])

    def test_html(self):
        out = htmlview.html(cards.build("system", ctx_of(), -2, cards.Caps(120)))
        self.assertIn('data-card="system"', out)
        self.assertIn('<ul class="grid">', out)
        self.assertGreaterEqual(out.count("<svg"), 5)
        self.assertIn("HEAVIEST CONTAINERS", out)
        self.assertNotIn("<pre", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("style=", out)


class ContainersCardTests(unittest.TestCase):
    def test_the_summary_and_the_stacks_at_every_level(self):
        ctx = ctx_of()
        card, text = card_text("containers", ctx, 0)
        self.assertEqual(card.state, "ok")
        self.assertEqual(text[1], " 7 containers   RAM 1.6G   ● 6 ok   ✖ 1 stopped")
        self.assertEqual(text[2], " ▸ blog                       2/2 running   RAM 410M")
        self.assertEqual(text[3], "     ● app   ● db")
        self.assertIn(" ▸ shop                       3/4 running   RAM 1.2G   ✖ 1", text)
        self.assertIn("     ● api   ● db   ● web   ✖ worker", text)
        groups = kinds(card, ui.Group)
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(isinstance(g.children[0], Line) and isinstance(g.children[1], ui.Wrap) for g in groups))
        _, k3 = card_text("containers", ctx, 3)
        self.assertEqual(k3[2], " (standalone) 1  ·  blog 2  ·  shop 4")
        self.assertEqual(k3[3], " ✖ " + "worker-1".ljust(37) + "Exited (137) 2 hours ago")
        _, k4 = card_text("containers", ctx, 4)
        self.assertEqual(len(k4), 2)

    def test_a_stack_that_is_too_long_is_cut_and_full_shows_everything(self):
        cont = copy.deepcopy(ctx_of().cont)
        cont["containers"] += [dict(cont["containers"][0], name=f"shop-svc{i}-1") for i in range(30)]
        card = cards.build("containers", ctx_of(cont=cont), 2, cards.Caps(60))
        lines, cut = ansi.card_lines(card, 60)  # a Wrap that the width cuts says so when it is drawn
        self.assertTrue(cut)
        self.assertIn("… +", "\n".join(plain(lines)))
        full = cards.build("containers", ctx_of(cont=cont), 2, cards.Caps(60, full=True))
        lines, cut = ansi.card_lines(full, 60)
        self.assertFalse(cut)
        self.assertNotIn("… +", "\n".join(plain(lines)))

    def test_sick_and_stopped_are_counted_and_the_state_follows_the_problems(self):
        cont = copy.deepcopy(ctx_of().cont)
        cont["containers"][1]["status"] = "Up 1 hour (unhealthy)"
        _, text = card_text("containers", ctx_of(cont=cont), 0)
        self.assertEqual(text[1], " 7 containers   RAM 1.6G   ● 5 ok   ✖ 1 stopped   ✖ 1 unhealthy")
        pb = render.ProblemList([(2, "x")])
        pb.pids = ["unhealthy-container"]
        self.assertEqual(cards.build("containers", ctx_of(cont=cont, problems=pb), 0, cards.Caps(100)).state, "err")

    def test_without_data_it_says_why(self):
        for cont, level, word in ((None, "err", "not running"), ({"absent": ["docker"], "containers": []}, "info", "not installed"),
                                  ({"ts": 1}, "warn", "unavailable")):
            card = cards.build("containers", ctx_of(cont=cont), 0, cards.Caps(100))
            self.assertEqual((card.body[0].level, word in card.body[0].text), (level, True))
        self.assertEqual(cards.build("containers", ctx_of(cont=None), 0, cards.Caps(100)).state, "unknown")
        self.assertEqual(cards.build("containers", ctx_of(cont={"absent": ["docker"], "containers": []}), 0, cards.Caps(100)).state, "info")

    def test_hostile_names_are_neutral_on_both_surfaces(self):
        cont = copy.deepcopy(ctx_of().cont)
        cont["containers"][0].update(name=HOSTILE, project=HOSTILE)
        cont["containers"][6].update(name=HOSTILE + "-2", status=HOSTILE)
        for k in (0, 3):
            card = cards.build("containers", ctx_of(cont=cont), k, cards.Caps(100))
            text = "\n".join(ansi.card_lines(card, 100)[0])
            self.assertEqual(ansi.ANSI.sub("", text).count("\x1b"), 0)
            self.assertEqual(len(ansi.card_lines(card, 100)[0]), len(text.split("\n")))  # no raw line break got in
            out = htmlview.html(card)
            self.assertNotIn("<script", out)
            self.assertIn("&lt;script&gt;", out)
            self.assertNotIn("\x1b", out)
            self.assertNotIn("style=", out)

    def test_html_has_a_group_a_stack_with_chips(self):
        out = htmlview.html(cards.build("containers", ctx_of(), 0, cards.Caps(100)))
        self.assertEqual(out.count('<section class="grp">'), 3)
        self.assertIn('<ul class="wrap" data-max-lines="3">', out)
        self.assertIn('<span class="t-err">✖</span> worker', out)
        self.assertEqual(len(re.findall("<section", out)), len(re.findall("</section>", out)))


class DatabasesCardTests(unittest.TestCase):
    def test_a_row_each_and_the_facts_under_it(self):
        card, text = card_text("databases", ctx_of(), 0, 100)
        self.assertEqual(text[0], "── DATABASE " + "─" * 63 + "  3 running · 1 exposed")
        self.assertEqual(text[1], " ● postgres shop-db-1                      *5432    exposed")
        self.assertEqual(text[2], "      → in use now: api   external clients now: 192.168.0.50")
        self.assertEqual(text[3], " ● postgres blog-db-1                      lo:5433  local only")
        self.assertEqual(text[4], "      → in use now: app   no external client seen in 60 min")
        self.assertEqual(text[6], "      → no known service (bridge: cannot tell)   no external client seen in 60 min")
        self.assertEqual(card.state, "ok")  # (the state comes from the problems of the frame: this one has none)
        table = kinds(card, NoteTable)[0]
        self.assertEqual(len(table.rows), 3)
        self.assertEqual([len(x) for x in table.notes], [1, 1, 1])
        self.assertTrue(isinstance(table.notes[0][0], Flow))

    def test_the_facts_are_only_at_the_richest_levels(self):
        for k, notes in ((-2, 3), (1, 3), (2, 0), (4, 0)):
            _, text = card_text("databases", ctx_of(), k, 100)
            self.assertEqual(sum("→" in x for x in text), notes, k)
            self.assertEqual(len(text), 1 + 3 + notes + (0 if notes == 3 else 0), k)

    def test_unknown_external_clients_say_so(self):
        net = copy.deepcopy(ctx_of().net)
        item = net["dbs"]["items"][2]
        item.update(ext_source="none", external=[], active=[], usano=[], stessa_rete=[], host_clients=[])
        _, text = card_text("databases", ctx_of(net=net), 0, 100)
        last = "\n".join(text[-2:])
        self.assertIn("no known service (bridge: cannot tell)", last)
        self.assertIn("external clients: not detectable", last)

    def test_a_narrow_console_wraps_the_facts_and_never_cuts_them_silently(self):
        _, text = card_text("databases", ctx_of(), 0, 60)
        self.assertTrue(all(len(x) <= 60 for x in text if "→" in x or x.startswith("        ")))  # the facts: wrapped, not cut
        self.assertIn("192.168.0.50", "\n".join(text))
        self.assertTrue(any(x.startswith("        ") for x in text))  # a continuation line, under the first

    def test_without_data_it_says_why(self):
        for net, level, word in ((None, "err", "not running"), (dict(ctx_of().net, dbs=None), "warn", "unavailable"),
                                 (dict(ctx_of().net, dbs={"items": [], "since": 0}), "info", "no databases")):
            card = cards.build("databases", ctx_of(net=net), 0, cards.Caps(100))
            self.assertEqual((card.body[0].level, word in card.body[0].text), (level, True))
        self.assertEqual(cards.build("databases", ctx_of(net=None), 0, cards.Caps(100)).state, "unknown")

    def test_hostile_database_and_client_names(self):
        net = copy.deepcopy(ctx_of().net)
        item = net["dbs"]["items"][0]
        item.update(kind=HOSTILE, name=HOSTILE, host_clients=[HOSTILE], external=[{"ip": HOSTILE, "last": time.time()}])
        card = cards.build("databases", ctx_of(net=net), 0, cards.Caps(100))
        text = "\n".join(ansi.card_lines(card, 100)[0])
        self.assertEqual(ansi.ANSI.sub("", text).count("\x1b"), 0)
        out = htmlview.html(card)
        self.assertNotIn("<script", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("\x1b", out)

    def test_html_has_a_table_and_a_row_of_facts_under_each_database(self):
        out = htmlview.html(cards.build("databases", ctx_of(), 0, cards.Caps(100)))
        self.assertEqual(out.count('<tr class="sub">'), 3)
        self.assertIn('<p class="flow"><span class="lead"><span class="t-muted">→</span></span>', out)
        self.assertIn('<span class="t-ok">in use now: </span>api', out)
        self.assertIn("external clients now:", out)
        self.assertEqual(out.count("<table"), 1)
        self.assertNotIn("<pre", out)


class BootCardTests(unittest.TestCase):
    def test_the_summary_at_the_levels_that_fit_the_sections_side_by_side(self):
        for k, slowest in ((-1, 4), (0, 3), (1, 2), (2, 1)):
            card, text = card_text("boot", ctx_of(), k)
            self.assertEqual(text[1], " finished in 58.4s   ✔ 0 failed units   journal 2 err · 9 warn", k)
            self.assertEqual(text[2].count("  ·  "), slowest - 1, k)
            self.assertTrue(text[2].startswith(" slowest: systemd-networkd-wait-online 33.5s"), k)
            self.assertEqual(card.state, "ok")
        _, k3 = card_text("boot", ctx_of(), 3)
        self.assertEqual(len(k3), 2)

    def test_the_summary_drops_what_does_not_fit(self):
        _, text = card_text("boot", ctx_of(), 0, 40)
        self.assertEqual(text[1], " finished in 58.4s   ✔ 0 failed units")
        self.assertLessEqual(len(text[1]), 40)

    def test_the_rich_level_is_a_timeline_the_slowest_units_and_the_journal(self):
        card, text = card_text("boot", ctx_of(), -2, 100)
        tl = kinds(card, Timeline)[0]
        self.assertEqual([n for n, _ in tl.parts], ["firmware", "loader", "kernel", "initrd", "userspace"])
        self.assertEqual(tl.w, 80)
        self.assertEqual(text[2], "   boot finished in 58.4s   kernel 6.8.0-demo   up 5d 0h")
        self.assertEqual(len(text[3]), 3 + 80 + 1)  # each segment is at least a column: the rounding adds one here
        self.assertIn("   ■ firmware 5.1s  ■ loader 2.0s  ■ kernel 1.2s  ■ initrd 1.1s  ■ userspace 49.0s", text)
        i = next(n for n, x in enumerate(text) if "SLOWEST UNITS" in x)
        self.assertIn("activation time: not all of them block boot", text[i])
        self.assertEqual(text[i + 1], "")
        self.assertRegex(text[i + 2], r"^   systemd-networkd-wait-online\.service +█+░* +33\.5s$")
        self.assertEqual(sum(1 for x in text[i + 2:] if re.match(r"^   \S+ +█", x)), 4)
        j = next(n for n, x in enumerate(text) if "BOOT JOURNAL" in x)
        self.assertEqual(text[j + 2], "   2 errors   9 warning")
        self.assertRegex(text[j + 3], r"^   ● \S+ +\d+×  \S")
        bars = kinds(card, Bar)
        self.assertTrue(all(b.tone == "accent" and b.w == 20 for b in bars))
        self.assertEqual(max(b.frac for b in bars), 1.0)

    def test_the_slowest_units_are_capped_by_the_level_and_say_so(self):
        boot = copy.deepcopy(ctx_of().boot)
        boot["blame"] = [{"unit": f"u{i}.service", "s": 20.0 - i} for i in range(12)]
        card = cards.build("boot", ctx_of(boot=boot), -2, cards.Caps(100))
        self.assertTrue(card.truncated)
        self.assertEqual(len(kinds(card, Bar)), 5)
        full = cards.build("boot", ctx_of(boot=boot), -2, cards.Caps(100, full=True))
        self.assertEqual(len(kinds(full, Bar)), 12)
        self.assertFalse(full.truncated)

    def test_the_failed_units_and_the_journal_colour_the_summary(self):
        boot = copy.deepcopy(ctx_of().boot)
        boot["failed"] = ["a.service", "b.service"]
        _, text = card_text("boot", ctx_of(boot=boot), 0)
        self.assertIn("✖ 2 failed units", text[1])
        card = cards.build("boot", ctx_of(boot=boot), 0, cards.Caps(100))
        self.assertIn("err", [x.tone for x in kinds(card, Span)])

    def test_without_data_it_says_why_and_the_card_is_unknown(self):
        card = cards.build("boot", ctx_of(boot=None), 0, cards.Caps(100))
        self.assertEqual((card.state, card.body[0].level), ("unknown", "warn"))
        self.assertEqual(plain(ansi.card_lines(card, 100)[0])[1], "   ! boot collector not running")
        none = cards.build("boot", ctx_of(boot=None), -2, cards.Caps(100))
        self.assertEqual(none.body[0].level, "warn")  # (the richest level has no detail to draw either)
        boot = dict(ctx_of().boot, analyze=None, blame=None, journal=None, failed=None, notes={})
        _, text = card_text("boot", ctx_of(boot=boot), -2, 100)
        self.assertEqual(sum("!" in x for x in text), 3)  # boot times, slowest units and journal: each says it is unavailable
        _, summary = card_text("boot", ctx_of(boot=boot), 0, 100)
        self.assertEqual(summary[1], " ")  # nothing is claimed

    def test_the_other_oses_have_their_own_words_and_no_empty_blocks(self):
        cont, net, boot, base = demo.snapshot(time.time(), "windows")
        _, text = card_text("boot", ctx_of(boot=boot), -2, 100)
        joined = "\n".join(text)
        self.assertNotIn("SLOWEST UNITS", joined)  # this OS has no such list: no empty block
        mac = demo.snapshot(time.time(), "darwin")[2]
        _, mtext = card_text("boot", ctx_of(boot=mac), -2, 100)
        self.assertEqual("\n".join(mtext).count("SLOWEST") + "\n".join(mtext).count("JOURNAL"), 0)
        self.assertIn("not available on this OS", "\n".join(mtext))
        self.assertIn("SYSTEM EVENT LOG", joined)
        _, summary = card_text("boot", ctx_of(boot=boot), 0, 100)
        self.assertIn("failed service", summary[1])

    def test_hostile_unit_and_kernel_names(self):
        boot = copy.deepcopy(ctx_of().boot)
        boot["kernel"] = HOSTILE
        boot["blame"][0]["unit"] = HOSTILE
        boot["journal"]["top"][0].update(id=HOSTILE, last=HOSTILE)
        boot["analyze"]["parts"][HOSTILE] = 1.0
        for k in (-2, 0):
            card = cards.build("boot", ctx_of(boot=boot), k, cards.Caps(100))
            text = "\n".join(ansi.card_lines(card, 100)[0])
            self.assertEqual(ansi.ANSI.sub("", text).count("\x1b"), 0)
            out = htmlview.html(card)
            self.assertNotIn("<script", out)
            self.assertNotIn("\x1b", out)
            self.assertNotIn("style=", out)
            self.assertEqual(len(re.findall("<svg", out)), len(re.findall("</svg>", out)))

    def test_html_has_a_timeline_a_table_and_the_journal(self):
        out = htmlview.html(cards.build("boot", ctx_of(), -2, cards.Caps(100)))
        self.assertIn('<figure class="timeline">', out)
        self.assertEqual(out.count('<rect class="tp tp-'), 5)
        self.assertIn('<li class="tp-userspace">', out)
        self.assertIn('<h3 class="sub">SLOWEST UNITS <span class="note">', out)
        self.assertIn('<h3 class="sub">BOOT JOURNAL', out)
        self.assertIn('class="bar st-ok t-accent"', out)
        self.assertEqual(out.count("<table"), 2)
        self.assertNotIn("<pre", out)
        summary = htmlview.html(cards.build("boot", ctx_of(), 0, cards.Caps(100)))
        self.assertIn('<span class="t-ok">✔ 0 failed units</span>', summary)


class SharedPiecesTests(unittest.TestCase):
    """The detail pages and the rotation pages use the same pieces as the cards: they keep working, and say the same."""

    def test_page_boot_and_page_sistema_are_the_card_pieces_with_their_own_titles(self):
        ctx = ctx_of()
        up = ctx.now - ctx.boot["btime"]
        lines = render.boot_block_avvio(ctx.boot, up, 100)
        self.assertEqual(lines[0], ansi.section("BOOT", 100))
        self.assertEqual(plain(lines)[1], "")
        card_lines = ansi.card_lines(cards.build("boot", ctx, -2, cards.Caps(100)), 100)[0]
        self.assertEqual(card_lines[:len(lines)], lines)
        self.assertEqual(render.boot_block_lente(ctx.boot, 100, 5)[:2], [ansi.section("SLOWEST UNITS", 100, "activation time: not all of them block boot"), ""])
        page = render.page_boot(ctx.boot, 100, 60, ctx.now)
        self.assertIn("   ■ firmware 5.1s", "\n".join(plain(page)))
        self.assertIn(" RAM ", "\n".join(plain(render.page_sistema(ctx.s, 100, ctx.cont))))

    def test_the_old_function_names_still_answer(self):
        ctx = ctx_of()
        self.assertEqual(cardlines.ov_boot(ctx.boot, 100, 0), ansi.card_lines(cards.build("boot", ctx, 0, cards.Caps(100)), 100)[0])
        self.assertEqual(cards.short_name("shop-api-1", "shop"), "api")
        self.assertEqual(ui.fmt_s(1.25), "1.2s")
        self.assertEqual(cards.up_load_note(90000, ["0.1", "0.2", "0.3"]), "up 1d 1h · load 0.1 0.2 0.3")
        self.assertEqual(cardlines.stack_lines(ctx.cont, 100, 3)[1], "     " + ansi.c(32, "●") + " app   " + ansi.c(32, "●") + " db")


if __name__ == "__main__":
    unittest.main()
