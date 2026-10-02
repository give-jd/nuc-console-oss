"""The Map screen as components (src/screens.py): the model, what the console draws from it at several sizes, and what the web shell draws from
the same model (a real page, no <pre>, links, escaped, inside the __view block).

Hermetic: the demo at a clock that stands still (tests/golden.py's FrozenWorld)."""
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
import graph  # noqa: E402
import htmlview  # noqa: E402
import render  # noqa: E402
import cardlines  # noqa: E402
import screens  # noqa: E402
import ui  # noqa: E402
import webjs  # noqa: E402

NOW = golden.NOW
SGR = re.compile(r"\x1b\[[0-9;]*m")
EVIL = "<script>alert(1)</script>"
WEBAPPS = {"shop-web": [8080], "admin-console": [9443]}
EXPOSE = {"shop-web": "LAN", "shop-db": "LOCALE", "n8n": "TAILNET"}
SIZES = ((40, 12), (79, 24), (120, 33), (139, 40), (140, 40), (200, 50), (226, 50))


def demo_graph(os_name=None, hostile=False):
    cont, net, boot, base = demo.snapshot(now=NOW, os_name=os_name)
    G = graph.build(cont, net, boot, WEBAPPS, now=NOW, baseline=base, expose=EXPOSE)
    if hostile:
        for n in G["nodes"].values():
            if n["kind"] != "root":
                n["label"] = EVIL + n["label"]
                n["facts"] = [(EVIL, EVIL)] + list(n["facts"])
                n["findings"] = list(n["findings"]) + [("warn", EVIL)]
        G["notes"] = list(G["notes"]) + [EVIL]
    return G


def plain(lines):
    return [SGR.sub("", x) for x in lines]


class Frag(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.tags, self.attrs = [], set()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.update(k for k, _v in attrs)


def view_of(page_html):
    start = page_html.index('data-card="__view"')
    return page_html[page_html.rindex("<", 0, start):page_html.index("</main>", start)]


def links(row_url=None):
    return screens.MapLinks(lambda r: "/?view=map&sel=" + r["key"], lambda r: "/?view=map&open=" + r["key"], "/?view=map", "/?view=map&all=1",
                            "/?view=map", lambda only: "/?view=map&only=%d" % only, "/?view=map&as=graph")


class Model(unittest.TestCase):
    def test_a_branch_carries_state_depth_and_the_links(self):
        G = demo_graph()
        rs = graph.rows(G, graph.State(all=True))
        for r in rs:
            b = screens.map_branch(G, r, False, links(), True)
            self.assertEqual((b.key, b.depth), (r["key"], r["depth"]))
            self.assertIn(b.state, ui.STATES)
            self.assertEqual(b.href, "/?view=map&sel=" + r["key"])
            self.assertEqual(bool(b.mark_href), bool(r["kids"] and not r["cycle"]))
        self.assertTrue(any(b.state == "down" for b in (screens.map_branch(G, r) for r in rs)))

    def test_the_console_row_is_what_it_always_was(self):
        G = demo_graph()
        rs = graph.rows(G, graph.State(all=True))
        down = next(r for r in rs if G["nodes"][r["node"]]["state"] == "down")
        text = SGR.sub("", cardlines.map_row(G, down))
        self.assertIn("\u2716 " + G["nodes"][down["node"]]["label"], text)  # a symbol besides the colour
        self.assertRegex(text, r"^(\u2502  |   )*(\u251c|\u2514)\u2500 ")  # the tree glyphs come first

    def test_the_web_model_has_the_choices_with_their_keys_from_the_keymap(self):
        G = demo_graph()
        st = graph.State()
        nodes = screens.map_web(G, graph.rows(G, st), st, "", None, links())
        title = nodes[0]
        keys = {k: lab for sg in title.segs for lab, k, _c, _h in sg.options if k}
        self.assertEqual(sorted(keys), ["c", "e", "p"])
        for key, act in (("e", "expand"), ("c", "collapse"), ("p", "problems")):
            self.assertEqual(ui.action("map", key), act)
        self.assertEqual([lab for lab, _k, chosen, _h in title.segs[2].options if chosen], ["tree"])

    def test_unknown_values_are_question_marks_not_fine(self):
        G = demo_graph()
        nid = next(i for i, n in G["nodes"].items() if n["kind"] == "port")
        G["nodes"][nid]["owners"] = []
        rs = graph.rows(G, graph.State(all=True))
        row = next(r for r in rs if r["node"] == nid)
        b = screens.map_branch(G, row)
        self.assertIn("?", [s.text for s in b.body.spans if getattr(s, "tone", None) == "warn"])
        G["nodes"][nid]["state"] = "mystery"
        self.assertEqual(screens.map_branch(G, row).state, "unknown")

    def test_empty_graph_says_so(self):
        G = graph.build(None, None)
        lines, top = screens.map_view(G, [], 100, 10)
        self.assertEqual(top, 0)
        self.assertIn("nothing to draw", "\n".join(plain(lines)))
        st = graph.State()
        web = screens.map_web(G, [], st, "", None, links())
        self.assertIn("nothing to show yet", htmlview.html(web[-1]))

    def test_the_keys_moved_with_the_view(self):
        self.assertIs(screens.MapView, screens.MapView)
        self.assertIs(screens.map_key, screens.map_key)
        self.assertEqual((screens.MAP_PANE_W, screens.MAP_IDLE_S), (screens.MAP_PANE_W, screens.MAP_IDLE_S))
        G = demo_graph()
        mv = screens.MapView(NOW)
        rs = graph.rows(G, mv.st)
        self.assertEqual(screens.map_key(mv, "down", rs), "")
        self.assertEqual(mv.idx, 1)
        self.assertEqual(screens.map_key(mv, "e", rs), "rows")
        self.assertEqual(screens.map_key(mv, "q", rs), "back")


class Console(unittest.TestCase):
    def test_every_size_fits_and_keeps_the_title(self):
        for os_name in (None, "windows", "darwin"):
            G = demo_graph(os_name)
            rs = graph.rows(G, graph.State(all=True))
            for w, h in SIZES:
                for cur, det in ((None, None), (rs[3]["key"], True), (rs[-1]["key"], False)):
                    lines, top = screens.map_view(G, rs, w, h, cur, det, 0)
                    txt = plain(lines)
                    self.assertLessEqual(len(lines), h)
                    self.assertTrue(all(len(x) <= w for x in txt), (w, h))
                    self.assertIn("── MAP ", txt[0])
                    if cur and det:
                        self.assertIn("DETAILS", "\n".join(txt) if h > 10 else "DETAILS")

    def test_the_cursor_row_is_in_reverse_video_and_in_sight(self):
        G = demo_graph()
        rs = graph.rows(G, graph.State(all=True))
        lines, top = screens.map_view(G, rs, 120, 20, rs[-1]["key"], None, 0)
        hit = [x for x in lines if "\x1b[7m" in x]
        self.assertEqual(len(hit), 1)
        self.assertGreater(top, 0)

    def test_the_legend_loses_its_last_items_first(self):
        G = demo_graph()
        wide = plain(screens.map_view(G, [], 200, 3)[0])[0]
        self.assertIn("connected to it", wide)
        narrow = plain(screens.map_view(G, [], 90, 3)[0])[0]
        self.assertIn("seen", narrow)
        self.assertNotIn("connected to it", narrow)

    def test_hostile_text_stays_text(self):
        G = demo_graph(hostile=True)
        rs = graph.rows(G, graph.State(all=True))
        lines, _top = screens.map_view(G, rs, 200, 50, rs[2]["key"], True, 0)
        self.assertIn(EVIL, "\n".join(plain(lines)))  # as text, on a console it is harmless
        for x in lines:
            self.assertIsNone(re.search(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f-\x9f]", SGR.sub("", x)))

    def test_a_control_character_in_a_name_is_a_question_mark(self):
        G = demo_graph()
        for n in G["nodes"].values():
            if n["kind"] == "ct":
                n["label"] = "x\x1b[2Jy"
        rs = graph.rows(G, graph.State(all=True))
        txt = "\n".join(plain(screens.map_view(G, rs, 200, 50)[0]))
        self.assertIn("x?[2Jy", txt)
        self.assertNotIn("\x1b[2J", "\n".join(screens.map_view(G, rs, 200, 50)[0]))

    def test_the_rotation_slide_counts_what_does_not_fit(self):
        G = demo_graph()
        rs = graph.rows(G, graph.State(all=True))
        lines = screens.map_view(G, rs, 100, 12)[0]
        self.assertIn("more rows", plain(lines)[-1])

    def test_props_cut_to_h_say_how_many_lines_were_left_out(self):
        G = demo_graph()
        nid = next(i for i, n in G["nodes"].items() if n["kind"] == "ct")
        full = ansi.render(screens.map_props(G, nid, None), 80)[0]
        cut = plain(ansi.render(screens.map_props(G, nid, 5), 80)[0])
        self.assertEqual(len(cut), 5)
        self.assertIn("more lines", cut[-1])
        self.assertGreater(len(full), 5)


class Web(unittest.TestCase):
    def page(self, query, graph_hook=None):
        with golden.FrozenWorld() as world:
            if graph_hook:
                orig = render.map_graph
                world.set(render, "map_graph", lambda smp=None: (graph_hook(orig(smp)[0]), orig(smp)[1]))
            return world.page(query)

    def test_the_shell_draws_the_tree_natively_in_the_view_block(self):
        view = view_of(self.page("app=1&view=map"))
        self.assertNotIn("<pre", view)
        for needle in ('class="scr mapv"', '<ul class="ol">', 'data-row', 'data-key="e"', 'data-key="p"', 'aria-label="view"', ">graph<"):
            self.assertIn(needle, view)
        self.assertEqual(view.count('data-card="__view"'), 1)
        rows = re.findall(r'<li class="ob st-(\w+)" data-depth="(\d+)">', view)
        self.assertTrue(rows and all(st in ui.STATES for st, _d in rows))
        self.assertEqual(rows[0][1], "0")

    def test_a_selection_shows_its_details_beside_the_tree_and_a_way_to_close_them(self):
        view = view_of(self.page("app=1&view=map&all=1&sel=" + golden.SELECTED["row"]))
        self.assertEqual(len(re.findall(r'aria-current="true" data-row|data-row data-k="o-[0-9a-f]+" aria-current="true"', view)), 1)
        self.assertIn('<aside class="props">', view)
        self.assertIn("close ✕", view)
        self.assertNotIn("<aside", view_of(self.page("app=1&view=map")))

    def test_the_details_are_also_right_under_the_selected_row_for_a_narrow_window(self):
        view = view_of(self.page("app=1&view=map&all=1&sel=" + golden.SELECTED["row"]))
        self.assertEqual(len(re.findall(r'<li class="ob-d">\s*<aside class="props">', view)), 1)
        self.assertEqual(view.count('<aside class="props">'), 2)  # the one beside the tree, and the one under the row
        at = view.index('<li class="ob-d">')
        self.assertIn('aria-current="true"', view[view.rindex('<li class="ob', 0, at):at])  # the row just before it is the selected one
        self.assertNotIn("ob-d", view_of(self.page("app=1&view=map")))

    def test_the_style_sheet_keeps_the_details_in_sight(self):
        import webcss
        css = webcss.MAP_VIEW
        self.assertIn(".mapv .sp-r{position:sticky", css)  # wide: the pane stays while the tree scrolls
        self.assertIn(".mapv .split:has(li.ob-d) .sp-r{display:none}", css)  # narrow: the copy under the row replaces it
        self.assertIn(".mapv li.ob-d{display:none}", css)

    def test_a_branch_after_is_not_drawn_by_the_console(self):
        a =ui.Branch("k", 1, ui.Span("·"), ui.Line([ui.Span("name")]))
        b = ui.Branch("k", 1, ui.Span("·"), ui.Line([ui.Span("name")]), after=ui.Props("X", [("a", "b", "")]))
        self.assertEqual(ansi.render(ui.Outline([a]), 80), ansi.render(ui.Outline([b]), 80))

    def test_a_row_link_selects_and_the_selected_one_deselects(self):
        view = view_of(self.page("app=1&view=map&sel=" + golden.SELECTED["row"] + "&all=1"))
        for href, key in re.findall(r'<a class="oa" href="([^"]*)" data-row data-k="o-([0-9a-f]+)"', view):
            sel = re.search(r"sel=([0-9a-f]*)", stdhtml.unescape(href))
            self.assertEqual(sel.group(1) if sel else "", "" if key == golden.SELECTED["row"] else key)

    def test_the_classic_pages_are_unchanged(self):
        classic = self.page("app=0&view=map")
        self.assertIn('class="ro', classic)
        self.assertNotIn("mapv", classic)
        self.assertNotIn("mapv", self.page("app=1&view=map&as=graph"))  # the graph view is the old drawing

    def test_nothing_the_machine_wrote_is_markup(self):
        def evil(G):
            for n in G["nodes"].values():
                if n["kind"] != "root":
                    n["label"] = EVIL + n["label"]
                    n["facts"] = [(EVIL, EVIL)] + list(n["facts"])
            G["notes"] = list(G["notes"]) + [EVIL]
            return G
        view = view_of(self.page("app=1&view=map&all=1&sel=" + golden.SELECTED["row"], evil))
        self.assertNotIn(EVIL, view)
        self.assertIn(stdhtml.escape(EVIL), view)

    def test_only_what_a_fragment_may_hold(self):
        view = view_of(self.page("app=1&view=map&all=1&sel=" + golden.SELECTED["row"]))
        p = Frag()
        p.feed(view)
        self.assertEqual(sorted(set(p.tags) - set(webjs.FRAG_TAGS)), [])
        self.assertEqual(sorted(p.attrs - {a.lower() for a in webjs.FRAG_ATTRS}), [])

    def test_depth_is_a_number_the_style_sheet_knows(self):
        import webcss
        view = view_of(self.page("app=1&view=map&all=1"))
        for d in {int(x) for x in re.findall(r'data-depth="(\d+)"', view)}:
            if d:
                self.assertIn('.ob[data-depth="%d"]' % d, webcss.CSS)


if __name__ == "__main__":
    unittest.main()
