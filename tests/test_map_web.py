"""The web MAP page (web.py ?view=map): links that carry the whole view, escaping, bounded cache, no JavaScript."""
import contextlib
import html
import http.client
import io
import os
import re
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import graph  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
import webmap  # noqa: E402
import weburl  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from webtest import classic_default  # noqa: E402

EVIL = '<script>x</script>"onmouseover=alert(1) \'x'
ROW = re.compile(r'<div class="ro( sel)?" id="r-([0-9a-f]{10})">(.*?)</div>')


def serve():
    cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
    srv = web.Server(("127.0.0.1", 0), cfg, "", demo=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@classic_default()
def get(srv, path="/"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, dict(r.getheaders()), body


def rows_of(page):
    """[(key, selected, toggle href or None, toggle glyph, label href, label text)] of a map page."""
    out = []
    for sel, key, inner in ROW.findall(page):
        tog = re.search(r'<a class="tg"[^>]*href="([^"]*)">([^<]*)</a>', inner)
        lab = re.search(r'<a class="lb[^"]*" href="([^"]*)">([^<]*)</a>', inner)
        out.append((key, bool(sel), html.unescape(tog.group(1)) if tog else None, tog.group(2) if tog else None,
                    html.unescape(lab.group(1)), html.unescape(lab.group(2))))
    return out


def link(page, text):
    m = re.search(r'<a href="([^"]*)">' + re.escape(text) + "</a>", page)
    return html.unescape(m.group(1)) if m else None


def params(url):
    return weburl.view_params(web.parse_qs(web.urlsplit(url).query))


class MapPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = serve()
        cls.expose = render.CFG["expose"]  # the demo declares [expose]: put back after

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        render.CFG["expose"] = cls.expose
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.srv.cache.clear()

    def page(self, path):
        st, h, body = get(self.srv, path)
        self.assertEqual(st, 200, path)
        self.assertNotIn("render error", body, path)
        return body

    def test_map_page_has_rows_legend_header_and_is_locked_down(self):
        st, h, body = get(self.srv, "/?view=map")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual((h["Cache-Control"], h["X-Content-Type-Options"]), ("no-store", "nosniff"))
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("<form", body.lower())
        self.assertIsNone(re.search(r"\son[a-z]+=", body))                          # no inline event handler
        self.assertIn(html.escape(graph.LEGEND.split("  ")[0].split(" ")[1]), body)
        self.assertIn("━━►", body)
        self.assertIn("demo-host │ MAP", body)
        self.assertIn('class="hd w bR B"', body)                                    # the demo has problems: the red banner
        self.assertIn('<meta http-equiv="refresh" content="2">', body)
        rs = rows_of(body)
        labels = [r[5] for r in rs]
        for root in ("INTERNET", "LAN", "LOCAL", "STACKS"):
            self.assertIn(root, labels)
        self.assertEqual(len({r[0] for r in rs}), len(rs))                          # one id per row
        for key, _, tog, _, lab, _ in rs:                                           # the browser stays on the clicked row
            self.assertTrue(lab.endswith("#r-" + key), lab)
            self.assertEqual(params(lab)["sel"], key)
            if tog:
                self.assertTrue(tog.endswith("#r-" + key), tog)
        self.assertRegex(body, r'<a class="lb r" href="[^"]*">✖ worker-1</a>')       # down: red, and a symbol besides the colour
        G, _ = render.map_graph()
        self.assertEqual(labels, [graph.parts(G, r)["label"] if graph.parts(G, r)["state"] not in webmap.STATE_MARK
                                  else webmap.STATE_MARK[graph.parts(G, r)["state"]] + graph.parts(G, r)["label"] for r in graph.rows(G)])

    def test_toggle_links_add_and_remove_keys(self):
        body = self.page("/?view=map")
        closed = next(r for r in rows_of(body) if r[3] == "▸")
        key, href = closed[0], closed[2]
        self.assertEqual(params(href)["open"], (key,))
        opened = self.page(href.split("#")[0])
        again = next(r for r in rows_of(opened) if r[0] == key)
        self.assertEqual(again[3], "▾")
        self.assertGreater(len(rows_of(opened)), len(rows_of(body)))                # its children are shown
        self.assertEqual(params(again[2])["open"], ())                              # closing removes the key
        root = rows_of(body)[0]                                                     # roots are open by default
        self.assertEqual((root[3], params(root[2])["shut"]), ("▾", (root[0],)))
        shut = self.page(root[2].split("#")[0])
        self.assertEqual(next(r for r in rows_of(shut) if r[0] == root[0])[3], "▸")
        self.assertLess(len(rows_of(shut)), len(rows_of(body)))
        # equal views, one URL: keys are sorted and deduplicated
        a, b = sorted(r[0] for r in rows_of(body) if r[3] == "▸")[:2]
        one = self.page(f"/?view=map&open={b}.{a}.{b}")
        self.assertEqual(rows_of(one), rows_of(self.page(f"/?view=map&open={a}.{b}")))
        self.assertIn(f"open={a}.{b}", html.unescape(one))

    def test_footer_expand_collapse_only_pause(self):
        body = self.page("/?view=map&zoom=150&refresh=5")
        everything = self.page(link(body, "expand all"))
        self.assertEqual(params(link(body, "expand all"))["all"], True)
        self.assertNotIn("▸", [r[3] for r in rows_of(everything)])                 # every branch open
        self.assertGreater(len(rows_of(everything)), len(rows_of(body)))
        for k in rows_of(everything)[3:6]:                                          # with everything open, closing = shut
            if k[2]:
                self.assertIn(k[0], params(k[2])["shut"])
        folded = self.page(link(everything, "collapse all"))
        self.assertEqual(rows_of(folded), rows_of(body))
        only = self.page(link(body, "problems only"))
        self.assertTrue(params(link(body, "problems only"))["only"])
        self.assertLessEqual(len(rows_of(only)), len(rows_of(body)))
        self.assertIsNotNone(link(only, "all paths"))
        G, _ = render.map_graph()
        self.assertEqual([r[0] for r in rows_of(only)], [r["key"] for r in graph.rows(G, graph.State(only=True))])
        paused = self.page(link(body, "pause"))
        self.assertNotIn('http-equiv="refresh"', paused)
        self.assertIn("paused", paused)
        self.assertIn('http-equiv="refresh"', self.page(link(paused, "live")))
        for url in (link(body, "expand all"), link(body, "pause"), rows_of(body)[0][4], rows_of(body)[0][2]):  # every link keeps them
            self.assertEqual([params(url)[k] for k in ("view", "zoom", "refresh")], ["map", 150, 5], url)
        self.assertEqual([params(link(body, "A+"))[k] for k in ("view", "zoom", "refresh")], ["map", 175, 5])
        self.assertEqual([params(link(body, "+"))[k] for k in ("view", "zoom", "refresh")], ["map", 150, 6])
        dash = link(body, "dashboard")                                              # back, same size and refresh
        self.assertEqual((params(dash)["view"], params(dash)["zoom"], params(dash)["refresh"]), ("", 150, 5))

    def test_selected_row_shows_the_details_of_its_node(self):
        body = self.page("/?view=map&all=1")
        self.assertNotIn('id="details"', body)
        target = next(r for r in rows_of(body) if r[5] == "shop-db-1")
        page = self.page(target[4].split("#")[0])
        self.assertIn('id="details"', page)
        self.assertIn('class="mp two"', page)
        self.assertTrue([r for r in rows_of(page) if r[0] == target[0]][0][1])     # highlighted
        G, _ = render.map_graph()
        for label, value, _ in graph.details(G, "ct:shop-db-1"):
            self.assertIn(html.escape(str(value)), page, label)
        self.assertIn("postgres:16", page)
        close = re.search(r'<a href="([^"]*)">close ✕</a>', page).group(1)
        self.assertEqual(params(html.unescape(close))["sel"], "")
        self.assertTrue(html.unescape(close).endswith("#r-" + target[0]))
        self.assertIn("expand all", page)
        self.assertTrue(link(page, "pause").endswith("#r-" + target[0]))            # the footer keeps the row in sight
        hidden = self.page(f"/?view=map&sel={target[0]}")                           # its branch closed: still described
        self.assertIn("postgres:16", hidden)
        gone = self.page("/?view=map&sel=0123456789")
        self.assertIn("no longer on the map", gone)

    def test_invalid_parameters_are_dropped(self):
        q = lambda s: weburl.view_params(web.parse_qs(s))  # noqa: E731
        good = "0123456789"
        self.assertEqual(q("open=" + good + ".zz.0123.ABCDEF0123.<x>." + good)["open"], (good,))
        self.assertEqual(q("sel=<script>")["sel"], "")
        self.assertEqual(q("sel=" + good + "0")["sel"], "")
        self.assertEqual((q("view=MAP")["view"], q("view=map")["view"], q("all=yes")["all"], q("only=2")["only"]), ("", "map", False, False))
        many = ".".join("%010x" % i for i in range(1000))
        self.assertEqual(len(q("open=" + many)["open"]), weburl.MAX_KEYS)
        self.assertEqual(q("shut=" + many)["shut"], tuple("%010x" % i for i in range(weburl.MAX_KEYS)))
        plain = rows_of(self.page("/?view=map"))
        for path in ("/?view=map&open=" + many, "/?view=map&shut=" + many + "&all=1", "/?view=map&sel=" + "a" * 5000,
                     "/?view=map&zoom=99999999999999999999&refresh=-1&cols=x&open=%3Cscript%3E", "/?view=map&open=&shut=&sel=",
                     "/?view=map&all=1&open=" + many):
            body = self.page(path)
            self.assertNotIn("<script", body.lower())
            self.assertNotIn("0000000001", body)                                   # keys naming no row are not repeated
        self.assertEqual(rows_of(self.page("/?view=map&open=" + many)), plain)
        self.assertLess(len(self.page("/?view=map&open=" + many)), 3 * len(self.page("/?view=map")))

    def test_big_map_drops_keys_naming_no_row(self):
        """A map too big to walk whole (more than UNIVERSE rows fully open): made-up keys are still dropped, not repeated in
        every link of every row (megabytes per page, times the cache), while the keys of the rows on the page still work."""
        G, _ = render.map_graph()
        every = graph.rows(G, graph.State(all=True))
        saved, webmap.UNIVERSE = webmap.UNIVERSE, len(every) // 2                         # the demo map, as if it were that big
        try:
            far = [r["key"] for r in every[webmap.UNIVERSE:] if r["kids"] and r["depth"]]   # real branches past the limit
            self.assertTrue(far)
            many = ".".join("%010x" % i for i in range(weburl.MAX_KEYS - 1))          # room for one real key
            plain = self.page("/?view=map&all=1")
            bogus = self.page("/?view=map&all=1&shut=" + many)
            self.assertNotIn("0000000001", bogus)
            self.assertEqual(rows_of(bogus), rows_of(plain))                         # the same page, the same links
            self.assertNotIn("0000000001", self.page("/?view=map&open=" + many + "&shut=" + many))
            shut = self.page(f"/?view=map&all=1&shut={far[0]}.{many}")
            row = next(r for r in rows_of(shut) if r[0] == far[0])
            self.assertEqual((row[3], params(row[2])["shut"]), ("▸", ()))            # a real key past the limit still closes it
            self.assertEqual(params(row[4])["shut"], (far[0],))
            self.assertNotIn("0000000001", shut)
        finally:
            webmap.UNIVERSE = saved

    def test_a_new_view_walks_the_tree_again_but_does_not_rebuild_the_graph(self):
        """Every sel/open/shut value is a new page (a cache miss): the graph behind it is built once per r/2, not per page."""
        saved, calls = render.map_graph, []
        render.map_graph = lambda smp=None: calls.append(1) or saved(smp)
        try:
            for i in range(40):
                body = self.page("/?view=map&refresh=10&sel=%010x&open=%010x" % (i, i + 1000))
            self.assertEqual(len(calls), 1)
            self.assertEqual(len([k for k in self.srv.cache if k[0] == "map"]), 40)   # forty pages, one graph
            self.assertIn("no longer on the map", body)
            t, graph_pb = self.srv.cache[("map graph",)]
            self.srv.cache[("map graph",)] = (t - 5, graph_pb)                        # r/2 later: the map follows the state
            self.page("/?view=map&refresh=10&sel=0123456789")
            self.assertEqual(len(calls), 2)
        finally:
            render.map_graph = saved

    def test_hostile_names_stay_inert(self):
        saved = render.map_graph

        def evil(smp=None):
            G, pb = saved(smp)
            for n in G["nodes"].values():
                if n["label"] in ("shop-api-1", "shop-db-1"):
                    n["label"] = n["sub"] = EVIL
                    n["facts"].append((EVIL, EVIL))
                    n["findings"].append(("err", EVIL))
            G["notes"].append(EVIL)
            return G, pb
        render.map_graph = evil
        try:
            body = self.page("/?view=map&all=1")
            key = next(r for r in rows_of(body) if r[5] == EVIL)[0]
            body += self.page(f"/?view=map&all=1&sel={key}")
        finally:
            render.map_graph = saved
        self.assertNotIn("<script", body.lower())
        self.assertNotIn('"onmouseover', body)
        self.assertNotIn("'x", body)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;&quot;onmouseover=alert(1) &#x27;x", body)

    def test_map_link_on_the_dashboard_and_the_feature_switch(self):
        _, _, dash = get(self.srv, "/?zoom=150")
        self.assertEqual(link(dash, "map"), "/?view=map&zoom=150")
        saved = render.CFG["features"].get("map", True)
        render.CFG["features"]["map"] = False
        try:
            self.srv.cache.clear()
            _, _, dash = get(self.srv, "/?zoom=150")
            st, h, off = get(self.srv, "/?view=map&all=1")
        finally:
            render.CFG["features"]["map"] = saved
        self.assertIsNone(link(dash, "map"))
        self.assertNotIn("view=map", dash)
        self.assertEqual(st, 200)
        self.assertIn("map disabled in config.ini", off)
        self.assertNotIn('class="ro', off)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(link(off, "dashboard"), "/?")

    @classic_default()
    def test_cache_is_bounded(self):
        for i in range(web.CACHE_MAX * 2):
            self.srv.page(**weburl.view_params(web.parse_qs("view=map&sel=%010x" % i)))
        self.srv.page(cols=100)
        self.assertEqual(len(self.srv.cache), web.CACHE_MAX)
        self.assertIn(next(reversed(self.srv.cache)), self.srv.cache)
        self.assertEqual(next(reversed(self.srv.cache))[:2], (100, 0))              # the latest stays, the oldest went
        self.assertNotIn("%010x" % 0, "".join(str(k) for k in self.srv.cache))
        first = self.srv.page(**weburl.view_params(web.parse_qs("view=map")))
        self.assertIs(self.srv.page(**weburl.view_params(web.parse_qs("view=map"))), first)  # within r/2: the same render

    def test_cache_is_bounded_in_size_too(self):
        one = len(self.srv.page(**weburl.view_params(web.parse_qs("view=map&all=1"))))
        orig, web.CACHE_CHARS = web.CACHE_CHARS, one * 3                               # room for three pages
        try:
            for i in range(10):
                self.srv.page(**weburl.view_params(web.parse_qs("view=map&all=1&sel=%010x" % i)))
            self.assertLessEqual(self.srv.cache_chars(), web.CACHE_CHARS)
            self.assertIn("%010x" % 9, str(next(reversed(self.srv.cache))))            # the latest stays, the oldest went
            web.CACHE_CHARS = 1                                                        # a page bigger than the limit is still served
            self.assertTrue(self.srv.page(cols=100))
            self.assertEqual(len(self.srv.cache), 1)
        finally:
            web.CACHE_CHARS = orig

    def test_windows_and_macos_demos_render(self):
        for name in ("windows", "darwin"):
            render.DEMO_OS = name
            try:
                self.srv.cache.clear()
                body = self.page("/?view=map&all=1")
            finally:
                render.DEMO_OS = None
            self.assertGreater(len(rows_of(body)), 5, name)
            self.assertIn("Docker Desktop", body, name)                             # the note: container links not seen there

    def test_empty_state_explains_itself(self):
        saved = render.map_graph
        render.map_graph = lambda smp=None: (graph.build(None, None), [])
        try:
            body = self.page("/?view=map&open=0123456789&sel=0123456789")
        finally:
            render.map_graph = saved
        self.assertIn("network collector not running", body)
        self.assertIn("nothing to show yet", body)
        self.assertIn('class="hd rv B"', body)                                      # no problems: the plain pill
        self.assertIn("ALL OK", body)

        def broken(smp=None):
            raise ValueError("secret detail /etc/x")
        render.map_graph = broken
        err = io.StringIO()
        try:
            self.srv.cache.clear()
            with contextlib.redirect_stderr(err):
                st, _, body = get(self.srv, "/?view=map")
        finally:
            render.map_graph = saved
        self.assertEqual(st, 200)
        self.assertIn("render error (see the service log)", body)
        self.assertNotIn("secret detail", body)                                     # the detail goes to the log only
        self.assertNotIn("Traceback", body)
        self.assertIn("secret detail", err.getvalue())
        self.assertIn("dashboard", body)


if __name__ == "__main__":
    unittest.main()
