"""The MAP's graph view (web.py ?view=map&as=graph): what is drawn, the DOM contract with graphjs.SCRIPT, its CSP, bounds."""
import base64
import contextlib
import hashlib
import html
import http.client
import io
import math
import os
import re
import socket
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import threading
import unittest
from html.parser import HTMLParser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import graph  # noqa: E402
import graphjs  # noqa: E402
import graphlayout  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from webtest import classic_default  # noqa: E402

ZONES = {"root:internet", "root:lan", "root:tailnet", "root:local"}
HEX = re.compile(r"[0-9a-f]{10}")
NUMBER = re.compile(r"[0-9]+(\.[0-9])?")  # a coordinate: a plain decimal, at most one digit after the point
EVIL = 'x\x01\x1b[31m"\'<script>alert(1)</script>]]>&amp;<!--' + "y" * 30


def serve():
    cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
    srv = web.Server(("127.0.0.1", 0), cfg, "", demo=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@classic_default()
def get(srv, path="/"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, dict(r.getheaders()), body


def params(url):
    return web.view_params(web.parse_qs(web.urlsplit(url).query))


def key(nid):
    return graph.path_key((nid,))


def link(page, text):
    m = re.search(r'<a href="([^"]*)">' + re.escape(text) + "</a>", page)
    return html.unescape(m.group(1)) if m else None


class Drawing(HTMLParser):
    """The graph page, parsed: nodes {key: attrs + circle/text/title}, edges [attrs], the svg, #gv, the markers, every id."""

    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.nodes, self.edges, self.ids, self.markers, self.scripts = {}, [], [], [], []
        self.svg = self.gv = self.cur = self.field = None
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "svg" and a.get("id") == "gsvg":
            self.svg = a
        elif tag == "div" and a.get("id") == "gv":
            self.gv = a
        elif tag == "marker":
            self.markers.append(a)
        elif tag == "line" and "e" in cls:
            self.edges.append(dict(a, cls=cls))
        elif tag == "a" and "n" in cls:
            self.cur = dict(a, cls=cls, text="", title="")
            self.nodes[a.get("data-k")] = self.cur
        elif tag == "circle" and self.cur is not None:
            self.cur["circle"] = a
        elif tag in ("text", "title") and self.cur is not None:
            self.field = tag
        elif tag == "script":
            self.field = "script"
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "a":
            self.cur = None
        if tag in ("text", "title", "script"):
            self.field = None

    def handle_data(self, data):
        if self.field == "script":
            self.scripts[-1] += data
        elif self.field and self.cur is not None:
            self.cur[self.field] += data


def drawn(G, stacks=False, ext=True):
    """The nodes the graph view must draw (no filter but stacks/ext), worked out independently of web.py."""
    return {nid for nid, n in G["nodes"].items() if (n["kind"] != "root" or nid in ZONES)
            and (stacks or n["kind"] != "stack") and (ext or n["kind"] != "ext")}


class GraphPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # the demo (render.demo_defaults) declares [webapps] and [expose] and renames the host for the whole process: put back after
        cls.saved = (render.CFG["webapps"], socket.gethostname)
        cls.saved_expose = render.CFG["expose"]
        cls.srv = serve()

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        render.CFG["webapps"], socket.gethostname = cls.saved
        render.CFG["expose"] = cls.saved_expose
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.srv.cache.clear()
        self.srv.layouts.clear()

    def page(self, path):
        st, h, body = get(self.srv, path)
        self.assertEqual(st, 200, path)
        self.assertNotIn("render error", body, path)
        return body

    def draw(self, path):
        return Drawing(self.page(path))

    def graph(self):
        return render.map_graph()[0]

    # ---- the toggle -------------------------------------------------------------------------------------------------

    def test_the_toggle_in_both_modes_keeps_the_view(self):
        tree = self.page("/?view=map&only=1&pause=1&zoom=150&refresh=5")
        m = re.search(r'<span class="mv"><b>tree</b><a href="([^"]*)">graph</a></span>', tree)
        self.assertIsNotNone(m)                                                     # tree highlighted, graph a link
        to_graph = html.unescape(m.group(1))
        self.assertEqual([params(to_graph)[k] for k in ("view", "as", "only", "pause", "zoom", "refresh")],
                         ["map", "graph", True, True, 150, 5])
        gpage = self.page(to_graph)
        m = re.search(r'<span class="mv"><a href="([^"]*)">tree</a><b>graph</b></span>', gpage)
        self.assertIsNotNone(m)                                                     # graph highlighted, tree a link
        back = html.unescape(m.group(1))
        self.assertEqual([params(back)[k] for k in ("view", "as", "only", "pause", "zoom", "refresh")], ["map", "", True, True, 150, 5])
        self.assertIn('class="ro', self.page(back))
        self.assertNotIn("<svg", tree)                                               # the tree has no drawing, no script
        self.assertNotIn("<script", tree)
        self.assertNotIn("data-refresh", tree)
        self.assertNotIn("<noscript>", tree)

    @classic_default()
    def test_tree_mode_ignores_the_graph_parameters(self):
        """The graph's filters mean nothing to the tree: not in its links, not in its cache key (the very same page)."""
        plain = self.srv.page(**web.view_params(web.parse_qs("view=map&all=1")))
        same = self.srv.page(**web.view_params(web.parse_qs("view=map&all=1&as=tree&stacks=1&ext=0&local=2&z=200&sel=")))
        self.assertIs(same, plain)
        for word in ("stacks=", "ext=", "local=", "z=", "as="):
            self.assertNotIn(word, re.sub(r'<span class="mv">.*?</span>', "", plain))
        self.assertEqual(getattr(plain, "csp", web.CSP), web.CSP)

    def test_the_selection_follows_the_toggle(self):
        G = self.graph()
        every = graph.rows(G, graph.State(all=True))
        row = next(r for r in every if r["node"] == "ct:shop-db-1")
        tree = self.page(f"/?view=map&sel={row['key']}")
        to_graph = link(tree, "graph")
        self.assertEqual(params(to_graph)["sel"], key("ct:shop-db-1"))             # a row key becomes the node's key
        d = Drawing(self.page(to_graph))
        self.assertIn("sel", d.nodes[key("ct:shop-db-1")]["cls"])
        back = link(self.page(to_graph), "tree")
        p = params(back)
        self.assertEqual(G["nodes"][next(r for r in every if r["key"] == p["sel"])["node"]]["id"], "ct:shop-db-1")
        self.assertTrue(back.endswith("#r-" + p["sel"]))
        shown = self.page(back.split("#")[0])
        self.assertRegex(shown, r'<div class="ro sel" id="r-%s">' % p["sel"])        # its branch is open: the row is on the page
        self.assertIn('id="details"', shown)
        stack = next(r for r in every if r["node"] == "stack:shop")                 # a compose project: the graph shows the stacks
        p = params(link(self.page(f"/?view=map&sel={stack['key']}"), "graph"))
        self.assertEqual((p["sel"], p["stacks"]), (key("stack:shop"), True))
        impact = next(r for r in every if r["node"] == "root:impact")               # a view, not a node of the graph: no selection
        self.assertEqual(params(link(self.page(f"/?view=map&sel={impact['key']}"), "graph"))["sel"], "")

    # ---- what is drawn ------------------------------------------------------------------------------------------------

    def test_nodes_and_edges_of_the_linux_demo(self):
        G = self.graph()
        d = self.draw("/?view=map&as=graph")
        want = drawn(G)
        self.assertEqual(set(d.nodes), {key(x) for x in want})
        of = {key(x): x for x in G["nodes"]}
        self.assertEqual({of[k] for k in d.nodes if "k-root" in d.nodes[k]["cls"]}, {"root:internet", "root:lan", "root:local"})
        for rid in ("root:impact", "root:stacks", "root:outbound"):                 # views of the tree, not nodes
            self.assertIn(rid, G["nodes"])
            self.assertNotIn(key(rid), d.nodes)
        self.assertFalse([k for k in d.nodes if "k-stack" in d.nodes[k]["cls"]])    # stacks: off by default
        self.assertTrue([k for k in d.nodes if "k-ext" in d.nodes[k]["cls"]])       # remote addresses: on by default
        bad = {x for x in want if G["nodes"][x]["state"] in ("err", "down", "warn", "unknown")}
        expect = {}
        for e in G["edges"]:
            if e["src"] in want and e["dst"] in want:
                ev = "reach" if e["ev"] == "bind" and e["src"] in ZONES else e["ev"]
                b = (e["src"] in bad and e["dst"] in bad) or (e["ev"] in ("seen", "declared") and e["dst"] in bad)
                expect[(key(e["src"]), key(e["dst"]))] = (ev, b)
        got = {(e["data-a"], e["data-b"]): (e["cls"][1], "bad" in e["cls"]) for e in d.edges}
        self.assertEqual(got, expect)
        self.assertEqual(len(d.edges), len(expect))                                  # one line per edge
        evs = {v[0] for v in got.values()}
        self.assertTrue({"seen", "declared", "possible", "bind", "reach"} <= evs, evs)
        for k, n in d.nodes.items():                                                # state and kind as classes
            node = G["nodes"][of[k]]
            self.assertEqual(n["cls"][:3], ["n", node["state"], "k-" + node["kind"]])
            self.assertTrue(n["title"].startswith(node["label"]))
        self.assertTrue(any(n["text"] == "✖ worker-1" for n in d.nodes.values()))  # down: a symbol besides the colour

    def test_remote_addresses_and_stacks_switch(self):
        G = self.graph()
        no_ext = self.draw("/?view=map&as=graph&ext=0")
        self.assertEqual(set(no_ext.nodes), {key(x) for x in drawn(G, ext=False)})
        self.assertFalse([n for n in no_ext.nodes.values() if "k-ext" in n["cls"]])
        stacks = self.draw("/?view=map&as=graph&stacks=1")
        self.assertEqual(set(stacks.nodes), {key(x) for x in drawn(G, stacks=True)})
        members = {(e["data-a"], e["data-b"]) for e in stacks.edges if e["cls"][1] == "bind" and e["data-a"] == key("stack:shop")}
        self.assertIn((key("stack:shop"), key("ct:shop-db-1")), members)          # a project, linked to its containers
        both = self.draw("/?view=map&as=graph&stacks=1&ext=0")
        self.assertEqual(set(both.nodes), {key(x) for x in drawn(G, stacks=True, ext=False)})

    def test_problems_only(self):
        G = self.graph()
        ids = drawn(G)
        bad = {x for x in ids if G["nodes"][x]["state"] in ("err", "down", "warn", "unknown")}

        def closure(start, fwd):
            out, grew = set(start), True
            while grew:
                more = {e["dst"] if fwd else e["src"] for e in G["edges"] if (e["src"] if fwd else e["dst"]) in out
                        and e["src"] in ids and e["dst"] in ids} - out
                out |= more
                grew = bool(more)
            return out
        on_path = closure(ZONES & ids, True) & closure(bad, False)                  # from a zone to a problem
        d = self.draw("/?view=map&as=graph&only=1")
        self.assertEqual(set(d.nodes), {key(x) for x in bad | on_path})
        self.assertLess(len(d.nodes), len(ids))
        for nid in ("ct:worker-1", "webapp:admin-console", "ct:shop-db-1", "ct:shop-api-1", "ct:shop-web-1", "root:lan", "port:8080/tcp@lan"):
            self.assertTrue(key(nid) in d.nodes, nid)
        for nid in ("proc:sshd", "root:local", "ext:192.168.0.50"):                 # healthy, or a client: not on a path to it
            self.assertNotIn(key(nid), d.nodes, nid)
        for e in d.edges:
            self.assertIn(e["data-a"], d.nodes)
            self.assertIn(e["data-b"], d.nodes)
        page = self.page("/?view=map&as=graph&only=1")
        self.assertEqual(params(link(page, "all nodes"))["only"], False)
        self.assertEqual(params(link(self.page("/?view=map&as=graph"), "problems only"))["only"], True)

    def test_local_graph_one_and_two_hops(self):
        G = self.graph()
        ids, sel = drawn(G), "ct:shop-api-1"
        near = {}
        for e in G["edges"]:
            if e["src"] in ids and e["dst"] in ids:
                near.setdefault(e["src"], set()).add(e["dst"])
                near.setdefault(e["dst"], set()).add(e["src"])
        one = {sel} | near[sel]
        two = one | {y for x in one for y in near.get(x, ())}
        d1 = self.draw(f"/?view=map&as=graph&local=1&sel={key(sel)}")
        d2 = self.draw(f"/?view=map&as=graph&local=2&sel={key(sel)}")
        self.assertEqual(set(d1.nodes), {key(x) for x in one})
        self.assertEqual(set(d2.nodes), {key(x) for x in two})
        self.assertLess(len(d1.nodes), len(d2.nodes))
        self.assertLess(len(d2.nodes), len(ids))
        for d in (d1, d2):
            self.assertEqual([k for k, n in d.nodes.items() if "sel" in n["cls"]], [key(sel)])
        for k, n in d1.nodes.items():                                               # a neighbour selects itself, still local
            p = params(n["href"])
            self.assertEqual((p["sel"], p["local"], p["as"]), (k, 1, "graph"))
        self.assertEqual(set(self.draw("/?view=map&as=graph&local=1").nodes), {key(x) for x in ids})  # no selection: all

    def test_details_pane_and_its_links(self):
        G = self.graph()
        k = key("ct:shop-db-1")
        page = self.page(f"/?view=map&as=graph&zoom=150&refresh=5&sel={k}")
        self.assertIn('id="details"', page)
        self.assertIn('<main class="mp two">', page)                                 # beside the drawing (below it when narrow)
        for label, value, _ in graph.details(G, "ct:shop-db-1"):
            self.assertIn(html.escape(str(value)), page, label)
        close = html.unescape(re.search(r'<a href="([^"]*)">close ✕</a>', page).group(1))
        self.assertEqual((params(close)["sel"], params(close)["local"], params(close)["as"]), ("", 0, "graph"))
        tools = re.search(r'<span>DETAILS</span><span class="mv">(.*?)</span>', page).group(1)
        self.assertIn("<b>whole graph</b>", tools)
        for n in (1, 2):
            p = params(html.unescape(re.search(r'<a href="([^"]*)">local %d</a>' % n, tools).group(1)))
            self.assertEqual((p["local"], p["sel"], p["zoom"], p["refresh"]), (n, k, 150, 5))
        local = self.page(f"/?view=map&as=graph&local=2&sel={k}")
        self.assertIn("<b>local 2</b>", local)
        self.assertEqual(params(link(local, "whole graph"))["local"], 0)
        self.assertNotIn('id="details"', self.page("/?view=map&as=graph"))

    def test_toolbar_links_keep_the_view(self):
        base = "/?view=map&as=graph&zoom=150&refresh=5&z=200&sel=" + key("ct:shop-db-1")
        page = self.page(base)
        d = Drawing(page)
        w, h = int(d.svg["data-w"]), int(d.svg["data-h"])
        self.assertEqual((d.svg["width"], d.svg["height"]), (str(w * 2), str(h * 2)))    # z=200: twice the size, the box scrolls
        self.assertIn("#gsvg{width:200%}", page)
        bar = page[page.index("<footer>"):]
        urls = dict((t, html.unescape(u)) for u, t in re.findall(r'<a href="([^"]*)">([^<]*)</a>', bar))
        self.assertEqual(params(urls["off"])["ext"], "0")                            # remote addresses: on, off is the link
        self.assertTrue(params(urls["on"])["stacks"])                                # stacks: off, on is the link
        self.assertTrue(params(urls["pause"])["pause"])
        self.assertTrue(params(urls["problems only"])["only"])
        zoom = re.search(r'zoom <a href="([^"]*)">−</a> 200% <a href="([^"]*)">\+</a>', bar)
        self.assertEqual((params(html.unescape(zoom.group(1)))["z"], params(html.unescape(zoom.group(2)))["z"]), (150, 250))
        for text, u in urls.items():                                                # every link keeps the rest of the view
            p = params(u)
            if text not in ("−", "+", "A−", "A+", "dashboard"):
                self.assertEqual((p["view"], p["zoom"], p["refresh"]), ("map", 150, 5), text)
                self.assertEqual(p["z"], 200 if text != "tree" else 0, text)
        self.assertEqual(params(urls["pause"])["sel"], key("ct:shop-db-1"))
        top = self.page("/?view=map&as=graph&z=300")
        self.assertRegex(top, r"zoom <a [^>]*>−</a> 300% \+ ")                        # no step beyond 300
        plain = self.page("/?view=map&as=graph")
        self.assertIn("#gsvg{width:100%;max-height:", plain)                          # z=100: the drawing fits the window
        self.assertRegex(plain, r'zoom <a href="[^"]*z=80[^"]*">−</a> 100% <a href="[^"]*z=125[^"]*">\+</a>')
        paused = self.page("/?view=map&as=graph&pause=1")
        self.assertNotIn('http-equiv="refresh"', paused)
        self.assertEqual(Drawing(paused).gv["data-paused"], "1")
        self.assertEqual(params(link(paused, "live"))["pause"], False)

    # ---- the DOM contract with graphjs.SCRIPT --------------------------------------------------------------------------

    def check_contract(self, d):
        self.assertIsNotNone(d.svg)
        self.assertIsNotNone(d.gv)
        self.assertEqual(d.gv["class"], "gv")
        self.assertEqual(d.svg["xmlns"], "http://www.w3.org/2000/svg")
        w, h = int(d.svg["data-w"]), int(d.svg["data-h"])
        self.assertEqual(d.svg["viewbox"], f"0 0 {w} {h}")
        self.assertEqual(sorted(m["id"] for m in d.markers), ["ah-declared", "ah-possible", "ah-seen"])
        ours = {"gv", "gsvg", "gvp"} | {"n-" + k for k in d.nodes}
        self.assertEqual(sorted(i for i in d.ids if i.startswith(("n-", "gsvg", "gvp", "gv"))), sorted(ours))
        self.assertEqual(len(d.ids), len(set(d.ids)))                                 # every id once
        for k, n in d.nodes.items():
            self.assertRegex(k, HEX)
            self.assertEqual(n["id"], "n-" + k)
            self.assertIn(n["cls"][1], web.STATES)
            self.assertIn(n["cls"][2][2:], web.KINDS)
            c = n["circle"]
            for a in ("cx", "cy", "r"):
                self.assertRegex(c[a], NUMBER)
            x, y, r = float(c["cx"]), float(c["cy"]), float(c["r"])
            self.assertTrue(0 <= x <= w and 0 <= y <= h, (k, x, y))
            self.assertEqual(r, 18 if "k-root" in n["cls"] else r)
            self.assertTrue(6 <= r <= 18)
            self.assertEqual(params(n["href"])["sel"], k)
            self.assertEqual(params(n["href"])["as"], "graph")
            self.assertTrue(n["title"])
            self.assertLessEqual(len(n["text"]), web.LABEL_MAX + 2)
        for e in d.edges:
            self.assertRegex(e["data-a"], HEX)
            self.assertIn(e["data-a"], d.nodes)                                     # every endpoint is a drawn node
            self.assertIn(e["data-b"], d.nodes)
            ev = e["cls"][1]
            self.assertIn(ev, ("seen", "declared", "possible", "bind", "reach"))
            self.assertTrue(set(e["cls"][2:]) <= {"bad", "nb"}, e["cls"])
            self.assertEqual(e.get("marker-end"), f"url(#ah-{ev})" if ev in web.ARROWS else None)
            pts = [float(e[a]) for a in ("x1", "y1", "x2", "y2")]
            for a in ("x1", "y1", "x2", "y2"):
                self.assertRegex(e[a], NUMBER)
            self.assertTrue(0 <= pts[0] <= w and 0 <= pts[2] <= w and 0 <= pts[1] <= h and 0 <= pts[3] <= h)
            a, b = d.nodes[e["data-a"]]["circle"], d.nodes[e["data-b"]]["circle"]
            ax, ay, ar, bx, by, br = (float(v) for v in (a["cx"], a["cy"], a["r"], b["cx"], b["cy"], b["r"]))
            gap = web.ARROW_GAP if ev in web.ARROWS else 0
            if math.hypot(bx - ax, by - ay) > ar + br + gap + 1:                    # shortened to the circles' edges
                self.assertAlmostEqual(math.hypot(pts[0] - ax, pts[1] - ay), ar, delta=0.15)
                self.assertAlmostEqual(math.hypot(pts[2] - bx, pts[3] - by), br + gap, delta=0.15)
        return w, h

    def test_dom_contract(self):
        page = self.page("/?view=map&as=graph")
        d = Drawing(page)
        w, h = self.check_contract(d)
        self.assertEqual((d.gv["data-refresh"], d.gv["data-paused"]), ("2", "0"))
        self.assertEqual(params(d.gv["data-state"])["as"], "graph")
        self.assertRegex(page, r'<svg id="gsvg" [^>]*><defs>(<marker [^>]*><path [^>]*/></marker>){3}</defs><g id="gvp"><g class="ge">'
                               r'(<line [^>]*/>)+</g><g class="gn">(<a [^>]*><circle [^>]*/><text class="lb(?: ls| le)?" [^>]*>[^<]*</text>'
                               r'<title>[^<]*</title></a>)+</g></g></svg></div>')
        for n in d.nodes.values():                                    # the label under, above, right or left of its circle
            c = n["circle"]
            m = re.search(r'id="n-%s"[^>]*><circle [^>]*/><text class="lb( ls| le)?" x="([^"]*)" y="([^"]*)"' % n["data-k"], page)
            cx, cy, r, side, lx, ly = float(c["cx"]), float(c["cy"]), float(c["r"]), m.group(1) or "", float(m.group(2)), float(m.group(3))
            spot = {"": [(cx, cy + r + 12), (cx, cy - r - 5)], " ls": [(cx + r + 4, cy + 4)], " le": [(cx - r - 4, cy + 4)]}[side]
            self.assertTrue(any(abs(lx - x) <= 0.11 and abs(ly - y) <= 0.11 for x, y in spot), (n["data-k"], side, lx, ly, cx, cy, r))
            self.assertLessEqual(ly, h)
            self.assertNotIn("sel", n["cls"][3:])
            self.assertNotIn("dim", n["cls"][3:])
        self.assertFalse([e for e in d.edges if "nb" in e["cls"]])

    def test_labels_avoid_each_other_and_stay_put(self):
        xy = {"a": (100.0, 100.0), "b": (150.0, 100.0), "c": (100.0, 300.0)}           # a and b too close for two labels below
        rad = {"a": 8.0, "b": 8.0, "c": 8.0}
        texts = {"a": "shop-web-1", "b": "shop-db-1", "c": "alone"}
        spots = web.place_labels(["a", "b", "c"], xy, rad, texts)
        self.assertEqual(spots["a"], (100.0, 120.0, ""))                               # the first one keeps the default
        self.assertEqual(spots["c"], (100.0, 320.0, ""))
        self.assertNotEqual(spots["b"], (150.0, 120.0, ""))                            # the second one moves away
        w = len(texts["a"]) * web.LABEL_CHAR_W
        bx, by, side = spots["b"]
        if side == "":
            self.assertTrue(by < 100 or abs(bx - 100) >= w)
        self.assertEqual(web.place_labels(["a", "b", "c"], xy, rad, texts), spots)      # deterministic

    def test_selection_marks_the_neighbours_and_dims_the_rest(self):
        G = self.graph()
        sel = "ct:shop-api-1"
        path = f"/?view=map&as=graph&refresh=3&pause=1&sel={key(sel)}"
        d = self.draw(path)
        self.check_contract(d)
        near = {key(e["dst"] if e["src"] == sel else e["src"]) for e in G["edges"] if sel in (e["src"], e["dst"])
                and e["src"] in drawn(G) and e["dst"] in drawn(G)}
        for k, n in d.nodes.items():
            want = "sel" if k == key(sel) else "nb" if k in near else "dim"
            self.assertEqual(n["cls"][3:], [want], k)
        for e in d.edges:
            self.assertEqual("nb" in e["cls"], key(sel) in (e["data-a"], e["data-b"]))
        state = params(d.gv["data-state"])                                          # the storage key: the view without sel
        self.assertEqual((state["sel"], state["refresh"], state["pause"], state["as"]), ("", 3, True, "graph"))
        self.assertEqual((d.gv["data-refresh"], d.gv["data-paused"]), ("3", "1"))

    def test_other_os_demos_render(self):
        for name in ("windows", "darwin"):
            render.DEMO_OS = name
            try:
                self.srv.cache.clear()
                page = self.page("/?view=map&as=graph")
                d = Drawing(page)
                G = self.graph()
            finally:
                render.DEMO_OS = None
            self.check_contract(d)
            self.assertEqual(set(d.nodes), {key(x) for x in drawn(G)}, name)
            self.assertGreater(len(d.nodes), 5, name)
            self.assertIn("Docker Desktop", page, name)

    # ---- the script and its CSP ----------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def script(self, text):
        saved = graphjs.SCRIPT, web.GRAPH_SCRIPT
        graphjs.SCRIPT = web.GRAPH_SCRIPT = text
        self.srv.cache.clear()
        try:
            yield
        finally:
            graphjs.SCRIPT, web.GRAPH_SCRIPT = saved
            self.srv.cache.clear()

    def test_the_csp_pins_the_one_script_of_the_graph_page(self):
        self.assertEqual(web.page_csp([graphjs.SCRIPT]), web.CSP + f"; script-src {graphjs.csp_source(graphjs.SCRIPT)}")  # the graph page's policy: its one hash
        fake = "/* nuc-console ✓ */ document.documentElement.dataset.ok = '1';"
        with self.script(fake):
            st, h, page = get(self.srv, "/?view=map&as=graph&sel=" + key("ct:shop-db-1"))
            others = [get(self.srv, p) for p in ("/", "/?view=map", "/?view=map&all=1", "/?cols=100&full=1")]
        self.assertEqual(st, 200)
        d = Drawing(page)
        self.assertEqual(d.scripts, [fake])                                          # once, exactly the script
        self.assertTrue(page.endswith("</footer><script>" + fake + "</script></html>"))
        self.assertEqual(page.count("<script"), 1)
        digest = base64.b64encode(hashlib.sha256(d.scripts[0].encode("utf-8")).digest()).decode()
        self.assertEqual(h["Content-Security-Policy"], web.CSP + f"; script-src 'sha256-{digest}'")
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual((h["Cache-Control"], h["X-Content-Type-Options"], h["Referrer-Policy"], h["X-Frame-Options"]),
                         ("no-store", "nosniff", "no-referrer", "DENY"))
        self.assertIn('<noscript><meta http-equiv="refresh" content="2"></noscript>', page)  # the script reloads the page itself
        self.assertEqual(page.count('http-equiv="refresh"'), 1)
        self.assertIsNone(re.search(r"\son[a-z]+=", page))                          # no inline event handler
        for st, h, body in others:                                                  # no other page has a script
            self.assertEqual(st, 200)
            self.assertEqual(h["Content-Security-Policy"], web.CSP)
            self.assertNotIn("script-src", h["Content-Security-Policy"])
            self.assertNotIn("<script", body.lower())
        with self.script(fake):                                                     # the script never comes from the request
            for path in ("/?view=map&as=graph&sel=%3Cscript%3E", "/?view=map&as=graph&ext=0&only=1&z=50&x=</script>"):
                st, h, body = get(self.srv, path)
                self.assertEqual(Drawing(body).scripts, [fake], path)
                self.assertEqual(h["Content-Security-Policy"], web.page_csp([fake]))

    def test_the_real_script_can_be_inlined(self):
        """Inline, the script ends at the first '</script' and '<!--' changes how the browser reads it: neither may be in it."""
        for bad in ("</script", "<!--", "<script"):
            self.assertNotIn(bad, graphjs.SCRIPT.lower())
        self.assertEqual(web.GRAPH_SCRIPT, graphjs.SCRIPT)
        self.assertEqual(self.srv.page(view="map", **{"as": "graph"}, z=137, local=7)[:15], "<!doctype html>")  # never a 500

    def test_no_script_no_script_src(self):
        with self.script(""):
            st, h, page = get(self.srv, "/?view=map&as=graph")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Security-Policy"], web.CSP)
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("<noscript>", page)                                         # no script to reload it: the meta refresh does
        self.assertIn('<meta http-equiv="refresh" content="2">', page)

    # ---- bounds ------------------------------------------------------------------------------------------------------

    def test_parameters_are_validated(self):
        q = lambda s: web.view_params(web.parse_qs(s))  # noqa: E731
        self.assertEqual([q(s)["as"] for s in ("as=graph", "as=GRAPH", "as=tree", "as=graph2", "as=", "")], ["graph", "", "", "", "", ""])
        self.assertEqual([q(s)["stacks"] for s in ("stacks=1", "stacks=yes", "stacks=0", "stacks=2")], [True, False, False, False])
        self.assertEqual([q(s)["ext"] for s in ("ext=0", "ext=1", "ext=off", "ext=", "ext=00")], ["0", "", "", "", ""])
        self.assertEqual([q(s)["local"] for s in ("local=1", "local=2", "local=3", "local=0", "local=01", "local=-1", "local=x")],
                         [1, 2, 0, 0, 0, 0, 0])
        self.assertEqual([q(s)["z"] for s in ("z=100", "z=50", "z=1", "z=999999", "z=99999999", "z=130", "z=x", "z=²", "z=-50", "")],
                         [0, 50, 50, 300, 0, 125, 0, 0, 0, 0])
        self.assertTrue(all(z in web.GZOOMS for z in web.GZOOMS))
        for s in ("sel=<script>", "sel=0123456789a", "sel=ABCDEF0123"):
            self.assertEqual(q(s)["sel"], "")
        G = self.graph()
        for sel in (key("root:impact"), key("stack:shop"), "0123456789"):             # names nothing drawn: dropped
            page = self.page(f"/?view=map&as=graph&local=1&sel={sel}")
            d = Drawing(page)
            self.assertNotIn('id="details"', page)
            self.assertEqual(set(d.nodes), {key(x) for x in drawn(G)})              # and local with it
            self.assertFalse([n for n in d.nodes.values() if len(n["cls"]) > 3])
            self.assertNotIn(sel, page)
        ext = next(x for x in drawn(G) if x.startswith("ext:"))
        self.assertNotIn('id="details"', self.page(f"/?view=map&as=graph&ext=0&sel={key(ext)}"))  # not drawn with ext=0
        self.assertIn('id="details"', self.page(f"/?view=map&as=graph&sel={key(ext)}"))
        one = self.srv.page(**q("view=map&as=graph&local=2"))                        # local means nothing without sel
        self.assertIs(self.srv.page(**q("view=map&as=graph")), one)
        self.assertIs(self.srv.page(**q("view=map&as=graph&open=0123456789&all=1")), one)  # nor the tree's branches
        self.assertIsNot(self.srv.page(**q("view=map&as=graph&ext=0")), one)           # the filters are in the cache key
        self.assertIsNot(self.srv.page(**q("view=map")), one)
        for path in ("/?view=map&as=graph&z=99999999999999999999&local=9&stacks=%3Cx%3E&ext=%22&refresh=-1",
                     "/?view=map&as=graph&sel=" + "a" * 5000, "/?view=map&as=graph&as=tree&ext=0&ext=1"):
            page = self.page(path)
            self.check_contract(Drawing(page))
            self.assertNotIn("<x>", page)

    def test_the_layout_is_computed_once_per_shape(self):
        """A new selection, or the graph rebuilt with the same shape, does not lay it out again; a new filter does."""
        saved, calls = graphlayout.layout, []
        graphlayout.layout = lambda *a, **kw: calls.append(a[2:4]) or saved(*a, **kw)
        try:
            for nid in ("ct:shop-db-1", "ct:shop-api-1", "root:lan", "proc:sshd"):
                self.page(f"/?view=map&as=graph&refresh=10&sel={key(nid)}")
            self.assertEqual(len(calls), 1)
            t, g = self.srv.cache[("map graph",)]
            self.srv.cache[("map graph",)] = (t - 60, g)                              # a new graph, the same shape
            self.page("/?view=map&as=graph&refresh=10&pause=1")
            self.assertEqual(len(calls), 1)
            self.assertIsNot(self.srv.cache[("map graph",)][1], g)
            self.page("/?view=map&as=graph&refresh=10&ext=0")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], (1000, 700))                                   # a small graph: 1000 x 700
            G = self.graph()
            for nid in sorted(drawn(G))[:web.LAYOUTS_MAX + 4]:                          # every local view is a shape: bounded
                self.page(f"/?view=map&as=graph&local=2&sel={key(nid)}")
            self.assertLessEqual(len(self.srv.layouts), web.LAYOUTS_MAX)
        finally:
            graphlayout.layout = saved

    def test_at_most_graph_nodes_the_most_relevant_first(self):
        saved_graph, saved_layout, sizes = render.map_graph, graphlayout.layout, []
        addrs = [f"{net}.{i}" for net in ("198.51.100", "203.0.113", "192.0.2") for i in range(1, 201)]

        def big(smp=None):
            G, pb = saved_graph(smp)
            for i, ip in enumerate(addrs):                                           # 600 remote addresses, the first seen last
                n = graph._ensure(G, "ext:" + ip, {})
                graph._edge(G, n["id"], "ct:shop-web-1", "seen", 8080, 0, G["now"] - 60 - i, "test")
            return G, pb

        def fast(nodes, edges, width=1000.0, height=700.0, margin=40.0):
            sizes.append((len(nodes), width, height))
            ids = sorted(nodes)
            cols = max(1, int(math.sqrt(len(ids))))
            return {x: (margin + (width - 2 * margin) * (i % cols) / cols, margin + (height - 2 * margin) * (i // cols) / cols)
                    for i, x in enumerate(ids)}
        render.map_graph, graphlayout.layout = big, fast
        try:
            G = big()[0]
            ids = drawn(G)
            page = self.page("/?view=map&as=graph")
            d = Drawing(page)
            oldest = key("ext:" + addrs[-1])
            sel = self.draw(f"/?view=map&as=graph&sel={oldest}")
            only = self.draw("/?view=map&as=graph&only=1")
        finally:
            render.map_graph, graphlayout.layout = saved_graph, saved_layout
        self.assertEqual(len(d.nodes), web.GRAPH_NODES)
        self.assertIn(f"+{len(ids) - web.GRAPH_NODES} more not drawn: show problems only, or select a node and its local graph", page)
        self.assertEqual(sizes[0], (web.GRAPH_NODES, 2000, 1400))                     # the drawing grows with the nodes
        self.check_contract(d)
        for nid in ids - {x for x in ids if x.startswith("ext:")}:                    # problems, zones, ports, containers first
            self.assertTrue(key(nid) in d.nodes, nid)
        kept = [ip for ip in addrs if key("ext:" + ip) in d.nodes]
        self.assertEqual(kept, addrs[:len(kept)])                                     # then the remote addresses seen last
        self.assertNotIn(oldest, d.nodes)
        self.assertIn("sel", sel.nodes[oldest]["cls"])                                # a selected node is always drawn
        self.assertEqual(len(sel.nodes), web.GRAPH_NODES)
        self.assertLess(len(only.nodes), web.GRAPH_NODES)                             # problems only: everything fits again

    def test_hostile_names_stay_inert(self):
        saved = render.map_graph

        def evil(smp=None):
            G, pb = saved(smp)
            for n in G["nodes"].values():
                if n["id"] in ("ct:shop-api-1", "ct:shop-db-1", "ext:192.168.0.50"):
                    n["label"] = n["sub"] = EVIL
                    n["facts"].append((EVIL, EVIL))
                    n["findings"].append(("err", EVIL))
            G["notes"].append(EVIL)
            return G, pb
        render.map_graph = evil
        try:
            with self.script("void 0;"):
                body = self.page("/?view=map&as=graph&sel=" + key("ct:shop-db-1"))
                body += self.page("/?view=map&as=graph&local=2&sel=" + key("ct:shop-api-1"))
        finally:
            render.map_graph = saved
        self.assertEqual(body.count("<script"), 2)                                    # ours, once per page
        self.assertEqual(body.count("</script>"), 2)
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("]]>", body)
        self.assertNotIn("<!--", body)
        self.assertNotIn('"\'', body)
        drawing = "".join(re.findall(r'<svg id="gsvg".*?</svg>', body)) + "".join(re.findall(r'<div class="nt">.*?</div>', body))
        self.assertIn("&lt;script&gt;", drawing)
        self.assertNotIn("\x01", drawing)                                             # control characters: replaced in the drawing
        self.assertNotIn("\x1b", drawing)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;]]&gt;&amp;amp;&lt;!--", body)
        d = Drawing(body[:body.index("</html>") + 7])
        self.check_contract(d)
        n = d.nodes[key("ct:shop-db-1")]
        self.assertEqual(n["title"], graph.safe(EVIL) + " · " + graph.safe(EVIL))  # the full name in the tooltip, made safe
        self.assertEqual(n["text"], graph.safe(EVIL)[:web.LABEL_MAX - 1] + "…")       # cut under the circle

    # ---- failure -----------------------------------------------------------------------------------------------------

    def test_render_error_is_generic_and_carries_no_script(self):
        saved = render.map_graph

        def broken(smp=None):
            raise ValueError("secret detail /etc/x")
        render.map_graph = broken
        err = io.StringIO()
        try:
            with self.script("void 0;"), contextlib.redirect_stderr(err):
                st, h, body = get(self.srv, "/?view=map&as=graph&sel=" + key("ct:shop-db-1"))
        finally:
            render.map_graph = saved
        self.assertEqual(st, 200)
        self.assertIn("render error (see the service log)", body)
        self.assertNotIn("secret detail", body)
        self.assertNotIn("Traceback", body)
        self.assertIn("secret detail", err.getvalue())
        self.assertNotIn("<script", body)
        self.assertEqual(h["Content-Security-Policy"], web.CSP)
        self.assertIn('<meta http-equiv="refresh" content="2">', body)               # no script: the plain refresh
        self.assertIn("dashboard", body)
        self.assertEqual(params(link(body, "tree"))["as"], "")

    def test_empty_graph_and_the_feature_switch(self):
        saved = render.map_graph
        render.map_graph = lambda smp=None: (graph.build(None, None), [])
        try:
            page = self.page("/?view=map&as=graph&sel=0123456789&local=1")
            only = self.page("/?view=map&as=graph&only=1")
        finally:
            render.map_graph = saved
        self.assertIn("network collector not running", page)
        self.assertIn("nothing to show yet", page)
        self.assertIn("nothing needs attention", only)
        d = Drawing(page)
        self.assertEqual(d.nodes, {})
        self.assertIsNotNone(d.gv)                                                   # the script still finds its box (and reloads)
        flag = render.CFG["features"].get("map", True)
        render.CFG["features"]["map"] = False
        try:
            self.srv.cache.clear()
            st, h, off = get(self.srv, "/?view=map&as=graph")
        finally:
            render.CFG["features"]["map"] = flag
        self.assertIn("map disabled in config.ini", off)
        self.assertNotIn("<svg", off)
        self.assertEqual(h["Content-Security-Policy"], web.CSP)


class Pure(unittest.TestCase):
    def test_canvas_grows_with_the_nodes(self):
        self.assertEqual(web.canvas(0), (1000, 700))
        self.assertEqual(web.canvas(60), (1000, 700))
        self.assertEqual(web.canvas(web.GRAPH_NODES), (2000, 1400))
        sizes = [web.canvas(n)[0] for n in range(0, web.GRAPH_NODES + 50, 10)]
        self.assertEqual(sizes, sorted(sizes))
        self.assertLessEqual(max(sizes), 2000)

    def test_spot_keeps_positions_inside(self):
        self.assertEqual(web.spot((float("nan"), 3), 1000, 700), (500, 350))
        self.assertEqual(web.spot(None, 1000, 700), (500, 350))
        self.assertEqual(web.spot((-5, 9999), 1000, 700), (20, 666))
        self.assertEqual([web.f1(v) for v in (1.0, 1.04, 1.05, 12.345, 0)], ["1", "1", "1.1", "12.3", "0"])


if __name__ == "__main__":
    unittest.main()
