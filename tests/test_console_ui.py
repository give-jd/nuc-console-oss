"""The console's interface (PR 11): the tab bar, the KPI line, the state on a section's title, and what [ui] changes on the console
(theme, NO_COLOR, density, the cards' layout, order and visibility). Whatever the screens draw is locked by the golden files; these are
the rules, at the four sizes (79, 120, 200, 226 columns) and without a machine behind them."""
import os
import re
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import ansi  # noqa: E402
import cards  # noqa: E402
import golden  # noqa: E402
import nuc_config  # noqa: E402
import prefs  # noqa: E402
import render  # noqa: E402
import ui  # noqa: E402

WIDTHS = (79, 120, 200, 226)             # console columns; a frame is one less wide
SIZES = ((79, 24), (120, 33), (200, 50), (226, 50))
SCREENS = ("Overview", "Map", "CPU", "Health", "AI")
PROBLEMS = [(2, "a thing is broken"), (1, "a thing is odd")]
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def plain(s):
    return ansi.ANSI.sub("", s)


def head(w, name="Overview", pb=PROBLEMS, **kw):
    """The header line of a console frame w columns wide, ANSI kept."""
    return render.frame((name, 1, 1, ["x"]), 0, 1, w - 1, 24, pb, **kw).split("\x1b[K\r\n")[0]


class Base(unittest.TestCase):
    def setUp(self):
        cfg = render.CFG
        self.saved = (dict(cfg["ui"]), dict(cfg["features"]), cfg["spacing"], render.KPI_MIN_ROWS, render.time, render.socket,
                      os.environ.get("NO_COLOR"), render.PAUSED)
        os.environ.pop("NO_COLOR", None)
        cfg["ui"].clear()
        cfg["ui"].update({"web": "app", "sections": list(nuc_config.SECTIONS)})
        for f in ("map", "cpu", "health", "ai"):
            cfg["features"][f] = True
        self.now = 1_790_000_000
        self.clock = golden.Clock(self.now)
        render.time = self.clock
        render.KEEP.clear()  # what the screens last read: nothing
        self.host = "demo-host"
        render.socket = types.SimpleNamespace(gethostname=lambda: self.host)

    def tearDown(self):
        cfg = render.CFG
        ui_, features, cfg["spacing"], render.KPI_MIN_ROWS, render.time, render.socket, no_color, render.PAUSED = self.saved
        cfg["ui"].clear()
        cfg["ui"].update(ui_)
        cfg["features"].clear()
        cfg["features"].update(features)
        os.environ.pop("NO_COLOR", None)
        if no_color is not None:
            os.environ["NO_COLOR"] = no_color

    def ui(self, **kw):
        render.CFG["ui"].update(kw)


class TabBar(Base):
    def test_the_line_is_never_wider_than_the_screen_and_the_status_is_always_there(self):
        for cols in WIDTHS:
            for name in SCREENS + ("System", "Boot", "Details"):
                for pb in ([], PROBLEMS, [(3, "port")]):
                    with self.subTest(cols=cols, name=name, pb=len(pb)):
                        line = plain(head(cols, name, pb))
                        self.assertLessEqual(len(line), cols - 1)
                        self.assertRegex(line, r"(ALL OK|PROBLEMS|WARNINGS|EXPOSED PORTS CHANGED)\s*$")

    def test_a_long_host_name_is_cut_before_the_status_is(self):
        self.host = "xyz-" + "very-long-" * 20 + "ABCD.local"
        for cols in WIDTHS:
            line = plain(head(cols))
            self.assertLessEqual(len(line), cols - 1)
            self.assertIn("2 PROBLEMS", line)
            self.assertIn("…", line)

    def test_the_whole_bar_when_it_fits_and_the_short_form_at_79_columns(self):
        for cols in (120, 200, 226):
            self.assertIn("[1 Overview]  2 Map  3 CPU  4 Health  5 AI", plain(head(cols)))
        short = plain(head(79))
        self.assertIn("[1·Ov] 2·Map 3·CPU 4·Hlth 5·AI", short)
        self.assertNotIn("Overview", short)
        self.assertRegex(short, r"^ *demo-host │ ")  # and the host name is whole

    def test_the_current_tab_has_brackets_and_is_in_reverse_video_never_colour_alone(self):
        for i, name in enumerate(SCREENS):
            raw = head(120, name)  # problems: the bar is red, not in reverse: the tab is
            self.assertIn(f"\x1b[7m[{i + 1} {name}]\x1b[27m", raw)
            self.assertEqual(plain(raw).count("["), 1)
        raw = head(120, "CPU", [])  # all ok: the bar is in reverse itself, the tab is the one that is not
        self.assertIn("\x1b[27m[3 CPU]\x1b[7m", raw)

    def test_the_pages_of_the_overview_are_the_first_tab_and_say_which_page(self):
        for name in ("System", "Network & firewall", "Boot", "Details"):
            line = plain(head(120, name))
            self.assertIn("[1 Overview]", line)
            self.assertIn(f"│ {name} │", line)
        self.assertNotRegex(plain(head(120, "Overview")), r"Overview\]\s+│ Overview")  # the overview itself is not repeated
        two = render.frame(("Overview", 2, 2, ["x"]), 1, 2, 119, 24, PROBLEMS).split("\x1b[K\r\n")[0]
        self.assertNotIn("2/2", plain(two))  # the footer counts the screens

    def test_a_screen_that_is_off_is_left_out_and_the_digits_stay(self):
        render.CFG["features"]["map"] = False
        render.CFG["features"]["health"] = False
        line = plain(head(120))
        self.assertIn("[1 Overview]  3 CPU  5 AI", line)
        self.assertNotIn("Map", line)
        self.assertNotIn("Health", line)

    def test_without_a_keyboard_only_the_screen_that_is_shown(self):
        line = plain(render.frame(("Overview", 1, 1, ["x"]), 0, 1, 119, 24, PROBLEMS, keys=False).split("\x1b[K\r\n")[0])
        self.assertIn("[1 Overview]", line)
        self.assertNotIn("2 Map", line)
        cpu = plain(render.frame(("CPU", 1, 1, ["x"]), 0, 1, 119, 24, PROBLEMS, keys=False).split("\x1b[K\r\n")[0])
        self.assertIn("[3 CPU]", cpu)  # a rotation slide is shown whatever the keys

    def test_the_paused_marker_the_clock_and_the_burn_in_shift_stay(self):
        render.PAUSED = True
        self.assertIn("│ paused", plain(head(120)))
        render.PAUSED = False
        self.assertNotIn("paused", plain(head(120)))
        self.assertIn("14:13:20", plain(head(120)))
        lead = []
        for i in range(3):
            self.clock.now = 600 * i
            lead.append(len(plain(head(120))) - len(plain(head(120)).lstrip()))
        self.assertEqual(lead, [1, 2, 3])  # the header moves a column every 10 minutes (and back after 30)

    def test_a_browser_page_keeps_the_plain_header_and_has_no_kpi_line(self):
        page = render.frame(("CPU", 1, 1, ["body"]), 0, 1, 120, 40, PROBLEMS, keys=False, page=True).split("\x1b[K\r\n")
        self.assertRegex(plain(page[0]), r"demo-host │ CPU │ \d\d:\d\d:\d\d")
        self.assertEqual(plain(page[1]), "body")


class KpiLine(Base):
    def frame(self, w, h, **kw):
        return plain(render.frame(("Overview", 1, 1, ["body"]), 0, 1, w - 1, h, PROBLEMS, **kw)).split("\r\n")

    def test_it_shows_from_30_rows_or_when_kpis_are_set(self):
        self.assertEqual([render.kpi_on(h) for h in (24, 29, 30, 33, 50)], [False, False, True, True, True])
        self.assertFalse(render.kpi_on(50, page=True))
        self.ui(kpis=["cpu", "ram"])
        self.assertTrue(render.kpi_on(24))
        self.assertEqual([render.body_rows(h) for h in (24, 33)], [21, 30])  # header, KPI line and footer
        del render.CFG["ui"]["kpis"]
        self.assertEqual([render.body_rows(h) for h in (24, 29, 30, 33)], [22, 27, 27, 30])

    def test_a_frame_is_always_exactly_h_lines_and_never_wider_than_the_screen(self):
        for cols, rows in SIZES:
            for body in (["x"], ["y"] * 200):
                lines = self.frame(cols, rows)
                lines = plain(render.frame(("Overview", 1, 1, body), 0, 1, cols - 1, rows, PROBLEMS)).split("\r\n")
                self.assertEqual(len(lines), rows, (cols, rows))
                self.assertTrue(all(len(x) <= cols - 1 for x in lines), (cols, rows))
                self.assertIn("console", lines[-1])  # the footer survives a body that is too tall
                self.assertEqual("Problems" in lines[1], rows >= 30, (cols, rows))

    def test_symbol_label_and_value_and_the_last_ones_go_first(self):
        ctx = cards.Ctx(problems=PROBLEMS, cfg=render.CFG)
        full = render.kpi_line(ctx, 300)
        self.assertRegex(plain(full), r"^ ✖ Problems 2   \? Internet \?   \? LAN \?   \? Beyond \?   \? CPU \?   \? RAM \?   \? Disk \?   \? Temp \?$")
        ids = prefs.effective(render.CFG["ui"])[0]["kpis"]
        seen = []
        for w in range(len(plain(full)), 14, -1):
            line = plain(render.kpi_line(ctx, w))
            self.assertLessEqual(len(line), w, w)
            kept = [k for k in ids if cards.KPI_LABELS[k] in line]
            self.assertEqual(kept, ids[:len(kept)], w)  # a prefix: what goes is the end of the list
            seen.append(len(kept))
        self.assertEqual(seen, sorted(seen, reverse=True))
        self.assertGreaterEqual(min(seen), 1)  # one is always kept

    def test_what_cannot_be_read_is_a_question_mark_and_never_fine(self):
        line = plain(render.kpi_line(cards.Ctx(problems=[], cfg=render.CFG), 300))
        for label in ("Internet", "LAN", "Beyond", "CPU", "RAM", "Disk", "Temp"):
            self.assertIn(f"? {label} ?", line)
        self.assertIn("✔ Problems 0", line)
        for cols, rows in SIZES:
            self.assertNotIn("✔ CPU", self.frame(cols, 50)[1])  # a frame that read nothing: the CPU is not known

    def test_the_kpis_of_ui_in_the_order_given(self):
        self.ui(kpis=["temp", "cpu"])
        line = plain(render.kpi_line(cards.Ctx(problems=[], cfg=render.CFG), 300))
        self.assertLess(line.index("Temp"), line.index("CPU"))
        self.assertNotIn("RAM", line)
        self.ui(preset="server")
        del render.CFG["ui"]["kpis"]
        self.assertIn("Load", plain(render.kpi_line(cards.Ctx(problems=[], cfg=render.CFG), 300)))  # the preset's list

    def test_every_screen_has_the_same_header_and_kpi_line(self):
        with golden.FrozenWorld() as world:
            lines = {}
            for view in ("", "map", "cpu", "health", "ai"):
                for cols, rows in SIZES:
                    out = world.once(("--view", view, "--cols", str(cols), "--rows", str(rows)) if view else ("--cols", str(cols), "--rows", str(rows)))
                    rs = plain(out.rstrip("\n")).split("\r\n")
                    self.assertEqual(len(rs), rows, (view, cols))
                    self.assertTrue(all(len(x) <= cols - 1 for x in rs), (view, cols))
                    lines[view, cols] = rs
                    self.assertRegex(rs[0], r"\[\d[ ·](Overview|Ov|Map|CPU|Hlth|Health|AI)\]")
                    self.assertRegex(rs[0], r"PROBLEMS")
                    self.assertEqual("✖ Problems" in rs[1], rows >= 30, (view, cols, rows))
            for cols, rows in SIZES[1:]:
                self.assertEqual({lines[v, cols][1] for v in ("", "map", "cpu", "health", "ai")}.__len__(), 1)  # one and the same line


class TitleSymbol(Base):
    def test_a_title_says_the_state_of_its_card_besides_the_colour(self):
        for state, sym in (("err", "✖"), ("warn", "!"), ("unknown", "?"), ("down", "✖")):
            for w in (39, 79, 119):
                line = ansi.section("EXPOSURE", w)
                marked = render.mark_title([line, "rest"], state)
                self.assertRegex(plain(marked[0]), r"^── %s EXPOSURE ─+$" % re.escape(sym))
                self.assertEqual(len(plain(marked[0])), len(plain(line)), (state, w))  # the rule gets shorter: the width does not change
                self.assertEqual(marked[1], "rest")

    def test_the_symbol_is_in_the_colour_of_the_state(self):
        marked = render.mark_title([ansi.section("FIREWALL", 60)], "warn")[0]
        self.assertIn(ansi.c(ui.sgr("warn"), "!"), marked)
        marked = render.mark_title([ansi.section("FIREWALL", 60)], "err")[0]
        self.assertIn(ansi.c(ui.sgr("err"), "✖"), marked)

    def test_a_fine_card_and_a_note_are_left_as_they_are(self):
        line = ansi.section("SYSTEM", 60)
        for state in ("ok", "info"):
            self.assertEqual(render.mark_title([line], state), [line])
        self.assertEqual(render.mark_title(["no title here"], "err"), ["no title here"])
        self.assertEqual(render.mark_title([], "err"), [])
        with_note = ansi.section("ATTENTION", 70, "3 open")
        self.assertEqual(len(plain(render.mark_title([with_note], "err")[0])), len(plain(with_note)))

    def test_the_overview_marks_the_cards_that_are_not_fine_at_every_size(self):
        with golden.FrozenWorld() as world:
            for cols, rows in SIZES:
                out = plain(world.once(("--cols", str(cols), "--rows", str(rows))))
                self.assertRegex(out, r"── ✖ ATTENTION ─")
                self.assertRegex(out, r"── ✖ EXPOSURE ─")
                self.assertRegex(out, r"── ! FIREWALL ─")
                self.assertNotRegex(out, r"── [✖!?] SYSTEM")  # a card that is fine says nothing


class Theme(Base):
    def codes(self, text):
        return {m.group(1) for m in SGR.finditer(text)}

    def once(self, **ui_):
        with golden.FrozenWorld(cfg={"ui": ui_}) as world:
            return world.once(("--cols", "120", "--rows", "33"))

    def test_auto_and_dark_are_the_default_theme_and_the_default_is_what_it_was(self):
        base = self.once()
        for theme in ("auto", "dark"):
            self.assertEqual(self.once(theme=theme), base)
        self.assertEqual(ansi.retheme(base, "default"), base)

    def test_light_and_high_contrast_write_their_own_codes(self):
        base = self.once()
        self.assertIn("36", self.codes(base))
        light = self.once(theme="light")
        self.assertNotIn("36", self.codes(light))
        self.assertIn("34", self.codes(light))
        self.assertEqual(plain(light), plain(base))  # only the colours change
        hc = self.once(theme="high-contrast")
        self.assertIn("1;91", self.codes(hc))
        self.assertTrue(self.codes(hc).isdisjoint({"31", "32", "33", "90"}))
        self.assertEqual(plain(hc), plain(base))

    def test_no_color_is_mono_whatever_the_theme_and_keeps_every_symbol(self):
        base = self.once()
        for theme in (None, "light", "high-contrast"):
            with golden.FrozenWorld(cfg={"ui": {"theme": theme} if theme else {}}, env={"NO_COLOR": "1"}) as world:
                mono = world.once(("--cols", "120", "--rows", "33"))
            self.assertLessEqual(self.codes(mono), {"0", "1", "7", "27", "1;7", ""}, theme)
            self.assertEqual(plain(mono), plain(base))
            self.assertIn("[1 Overview]", plain(mono))
        self.assertEqual(render.theme_name(), "default")

    def test_the_variable_counts_when_it_is_set_and_not_empty(self):
        for value, want in (("1", "mono"), ("yes", "mono"), ("", "default")):
            os.environ["NO_COLOR"] = value
            self.assertEqual(render.theme_name(), want, repr(value))
        del os.environ["NO_COLOR"]
        self.ui(theme="light")
        self.assertEqual(render.theme_name(), "light")
        self.ui(theme="high-contrast")
        self.assertEqual(render.theme_name(), "hc")
        os.environ["NO_COLOR"] = "1"
        self.assertEqual(render.theme_name(), "mono")

    def test_mono_keeps_bold_and_reverse_and_drops_every_colour(self):
        text = ansi.c("1;41;37", "a") + ansi.c("31", "b") + ansi.c("1;33", "c") + ansi.c("90", "d") + "\x1b[38;5;12me\x1b[0m" + ansi.c("7", "f")
        out = ansi.retheme(text, "mono")
        self.assertEqual(plain(out), "abcdef")
        self.assertLessEqual(self.codes(out), {"0", "1", "7", "1;7"})
        self.assertIn("\x1b[1;7ma", out)  # the pill is still a pill
        self.assertIn("\x1b[7mf", out)    # and the cursor row is still the cursor row
        for theme in ("light", "hc"):
            self.assertEqual(plain(ansi.retheme(text, theme)), "abcdef")
        self.assertEqual(ansi.retheme(text, "no-such-theme"), text)

    def test_the_frame_is_themed_when_it_is_written_and_a_browser_page_never_is(self):
        self.ui(theme="light")
        screen = render.frame(("Overview", 1, 1, [ansi.c("36", "x")]), 0, 1, 119, 24, [])
        self.assertIn("\x1b[36m", screen)  # drawn in the default theme
        self.assertIn("\x1b[34m", render.themed(screen))
        self.assertNotIn("\x1b[36m", render.themed(screen))


class Density(Base):
    def once(self, cols, rows, spacing=1, **ui_):
        with golden.FrozenWorld(cfg={"ui": ui_, "spacing": spacing}) as world:
            return plain(world.once(("--cols", str(cols), "--rows", str(rows)))).split("\r\n")

    def air(self, lines):
        """How many titles have an empty line under them."""
        return sum(1 for a, b in zip(lines, lines[1:]) if a.startswith("──") and b == "")

    def test_desk_is_what_it_was_and_compact_has_no_air_under_the_titles(self):
        desk = self.once(200, 90)  # a screen with room for the air
        self.assertGreater(self.air(desk), 0)
        self.assertEqual(self.once(200, 90, density="desk"), desk)
        compact = self.once(200, 90, density="compact")
        self.assertLess(self.air(compact), self.air(desk))
        self.assertEqual(compact, self.once(200, 90, spacing=0))  # compact is [dashboard] spacing = 0, whatever that says

    def test_spacing_on_follows_the_density_and_the_dashboard_setting(self):
        render.CFG["spacing"] = 1
        self.assertTrue(render.spacing_on())
        self.ui(density="compact")
        self.assertFalse(render.spacing_on())
        self.ui(density="wall")
        self.assertTrue(render.spacing_on())
        render.CFG["spacing"] = 0
        self.assertFalse(render.spacing_on())

    def levels(self, cols, rows, **ui_):
        """The detail levels the overview tried to build its cards at."""
        seen, real = set(), cards.build

        def spy(id, ctx, k, caps):
            seen.add(k)
            return real(id, ctx, k, caps)
        cards.build = spy
        try:
            self.once(cols, rows, **ui_)
        finally:
            cards.build = real
        return seen

    def test_wall_starts_at_level_0_without_the_two_richest_levels(self):
        desk, wall = self.levels(226, 50), self.levels(226, 50, density="wall")
        self.assertIn(-2, desk)
        self.assertTrue(all(k >= 0 for k in wall), wall)
        self.assertNotEqual(self.once(226, 50), self.once(226, 50, density="wall"))  # and the picture is the coarser one
        self.assertEqual(self.once(79, 24), self.once(79, 24, density="wall"))      # where level 0 was the first to fit, nothing changes


class CardOrder(Base):
    BASE = list(nuc_config.SECTIONS)

    def ctx(self, states=None):
        ctx = cards.Ctx(problems=[], cfg=render.CFG)
        self.states = dict.fromkeys(self.BASE, "ok") if states is None else states
        return ctx

    def setUp(self):
        super().setUp()
        self.real_state = cards.card_state
        self.states = dict.fromkeys(self.BASE, "ok")
        cards.card_state = lambda id, ctx: self.states[id]

    def tearDown(self):
        cards.card_state = self.real_state
        super().tearDown()

    def order(self, **ui_):
        render.CFG["ui"].update(ui_)
        return render.card_order(self.ctx(self.states), self.BASE)

    def test_without_ui_the_order_is_the_dashboard_sections_exactly(self):
        sections = ["firewall", "attention", "system"] + [s for s in self.BASE if s not in ("firewall", "attention", "system")]
        self.assertEqual(render.card_order(self.ctx(), sections), sections)
        self.states["exposure"] = "err"
        self.assertEqual(render.card_order(self.ctx(self.states), sections), sections)  # states never move a card here
        for key, value in (("theme", "light"), ("density", "wall"), ("start_view", "cpu"), ("kpis", ["cpu"])):
            self.ui(**{key: value})
            self.assertEqual(render.card_order(self.ctx(self.states), sections), sections, key)

    def test_layout_puts_its_cards_first_and_the_ones_it_leaves_out_after_them(self):
        got = self.order(layout=[("system", 2), ("exposure", 1)])
        self.assertEqual(got[:2], ["system", "exposure"])
        self.assertEqual(got[2:], [s for s in self.BASE if s not in ("system", "exposure")])
        self.assertEqual(sorted(got), sorted(self.BASE))

    def test_a_layout_alone_fixes_the_order_and_severity_has_to_be_written(self):
        self.states.update(exposure="err", firewall="warn")
        layout = [("system", 1), ("firewall", 1), ("exposure", 1)]
        self.assertEqual(self.order(layout=layout)[:3], ["system", "firewall", "exposure"], "the states do not move a card")
        self.assertEqual(self.order(layout=layout, order="severity")[:3], ["exposure", "firewall", "system"])

    def test_hidden_cards_are_not_there_and_a_hidden_card_is_hidden_in_a_layout_too(self):
        got = self.order(hidden=["sessions", "docker_disk"])
        self.assertEqual(got, [s for s in self.BASE if s not in ("sessions", "docker_disk")])
        got = self.order(layout=[("system", 1), ("sessions", 1)], hidden=["sessions"])
        self.assertNotIn("sessions", got)

    def test_a_preset_brings_its_order_and_ui_beats_the_preset(self):
        got = self.order(preset="security")
        self.assertEqual(got[:3], ["attention", "exposure", "firewall"])
        got = self.order(preset="desktop")
        self.assertNotIn("containers", got)  # the desktop preset hides it
        got = self.order(preset="desktop", layout=[("containers", 1), ("system", 1)])
        self.assertEqual(got[:2], ["containers", "system"])  # [ui] layout names it: it shows
        self.assertNotIn("databases", got)

    def test_fixed_keeps_the_layout_and_never_looks_at_the_states(self):
        self.states.update(exposure="err", firewall="warn")
        want = self.order(order="fixed")
        self.assertEqual(want, self.BASE)
        self.assertEqual(self.order(order="fixed", layout=[("firewall", 1), ("exposure", 1)])[:2], ["firewall", "exposure"])

    def test_severity_puts_the_worst_first_the_attention_card_stays_on_top(self):
        self.states.update(exposure="warn", firewall="err", system="unknown", databases="down")
        got = self.order(order="severity")
        self.assertEqual(got[0], "attention")
        rest = got[1:]
        rank = [ui._SEVERITY[self.states[n]] for n in rest]
        self.assertEqual(rank, sorted(rank, reverse=True))
        self.assertEqual(set(rest[:2]), {"firewall", "databases"})  # err and down first
        self.assertEqual(rest[2], "exposure")                        # then warn, then unknown
        self.assertEqual(rest[3], "system")
        self.assertEqual([n for n in rest[4:]], [s for s in self.BASE if s != "attention" and self.states[s] == "ok"])  # the fine ones keep their order

    def test_a_card_moves_only_when_a_state_changes(self):
        self.states.update(exposure="err", firewall="warn")
        first = self.order(order="severity")
        for _ in range(5):  # frame after frame, with new data and the same states: the same order
            self.assertEqual(render.card_order(self.ctx(self.states), self.BASE), first)
        self.states["system"] = "err"  # one state changes: the system card moves up, the others keep their places
        second = render.card_order(self.ctx(self.states), self.BASE)
        self.assertNotEqual(second, first)
        self.assertLess(second.index("system"), first.index("system"))
        others = [n for n in first if n != "system"]
        self.assertEqual([n for n in second if n != "system"], others)
        self.states["system"] = "ok"  # and back
        self.assertEqual(render.card_order(self.ctx(self.states), self.BASE), first)

    def test_severity_with_a_layout_sorts_within_the_layout_and_hides_the_hidden(self):
        self.states.update(webapps="err")
        got = self.order(order="severity", layout=[("attention", 1), ("system", 1), ("webapps", 1)], hidden=["boot"])
        self.assertEqual(got[:3], ["attention", "webapps", "system"])
        self.assertNotIn("boot", got)

    def test_the_order_is_worked_out_once_for_a_frame(self):
        self.states.update(exposure="err")
        ctx = self.ctx(self.states)
        render.CFG["ui"]["order"] = "severity"
        calls = []
        cards.card_state = lambda id, c: calls.append(id) or self.states[id]
        render.card_order(ctx, self.BASE)
        render.card_order(ctx, self.BASE)
        self.assertEqual(len(calls), len(set(calls)))  # the levels loop asks again and again: the second time costs nothing


class Overview(Base):
    """What [ui] does to the overview that is drawn (the demo, in a frozen world)."""

    def titles(self, out):
        return re.findall(r"^── [✖!?]? ?([A-Z ]+?) ─", out, re.M)

    def draw(self, cols=200, rows=50, **ui_):
        with golden.FrozenWorld(cfg={"ui": ui_}) as world:
            return plain(world.once(("--cols", str(cols), "--rows", str(rows))))

    def test_without_ui_the_sections_come_in_the_dashboard_order(self):
        order = self.titles(self.draw())
        sections = [s.upper().replace("_", " ") for s in nuc_config.SECTIONS]
        names = {"CONTAINERS": "CONTAINER", "DATABASES": "DATABASE", "BOOT": "BOOT"}
        want = [names.get(s, s) for s in sections]
        pos = [want.index(t) for t in order if t in want]
        self.assertEqual(pos, sorted(pos))

    def test_hidden_cards_do_not_show_and_the_layout_order_is_the_screens(self):
        out = self.draw(layout=[("system", 1), ("attention", 1)], hidden=["firewall", "databases"])
        titles = self.titles(out)
        self.assertEqual(titles[:2], ["SYSTEM", "ATTENTION"])
        self.assertNotIn("FIREWALL", titles)
        self.assertNotIn("DATABASE", titles)

    def test_severity_on_the_screen_the_worst_cards_first(self):
        out = self.draw(order="severity")
        titles = self.titles(out)
        self.assertEqual(titles[0], "ATTENTION")
        self.assertLess(titles.index("EXPOSURE"), titles.index("SYSTEM"))
        self.assertLess(titles.index("FIREWALL"), titles.index("SYSTEM"))


if __name__ == "__main__":
    unittest.main()
