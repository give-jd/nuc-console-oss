"""The web MAP page (web.py ?view=map): links that carry the whole view, escaping, bounded cache, no JavaScript."""
import contextlib
import html
import http.client
import io
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
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

EVIL = '<script>x</script>"onmouseover=alert(1) \'x'
ROW = re.compile(r'<li class="ob( [^"]*)?" data-depth="\d+">(.*?)</li>', re.S)


def serve():
    cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
    srv = web.Server(("127.0.0.1", 0), cfg, "", demo=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def get(srv, path="/"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, dict(r.getheaders()), body


def view_of(page):
    start = page.index('data-card="__view"')
    return page[page.rindex("<", 0, start):page.index("</main>", start)]


def rows_of(page):
    """[(key, selected, toggle href or None, toggle glyph, label href, label text)] of the tree of a map page."""
    out = []
    for sel, inner in ROW.findall(view_of(page)):
        tog = re.search(r'<(?:a|span) class="tg[^"]*"(?: href="([^"]*)")?[^>]*>(?:<span[^>]*>)?([^<]*)', inner)
        lab = re.search(r'<a class="oa"[^>]* href="([^"]*)"[^>]* data-k="o-([0-9a-f]{10})"[^>]*>(.*?)</a>', inner, re.S)
        out.append((lab.group(2), "sel" in sel.split(), html.unescape(tog.group(1)) if tog.group(1) else None, tog.group(2),
                    html.unescape(lab.group(1)), html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", lab.group(3)))).strip()))
    return out


def link(page, text):
    m = re.search(r'<a [^>]*href="([^"]*)"[^>]*>' + re.escape(text) + "</a>", page)
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
        self.assertNotIn("<script", view_of(body).lower())
        self.assertNotIn("<form", view_of(body).lower())
        self.assertIsNone(re.search(r"\son[a-z]+=", body))                          # no inline event handler
        self.assertIn(html.escape(graph.LEGEND.split("  ")[0].split(" ")[1]), body)
        self.assertIn("━━►", body)
        self.assertIn("<title>demo-host · map · nuc-console</title>", body)
        self.assertIn('<meta http-equiv="refresh" content="2">', body)
        rs = rows_of(body)
        labels = [r[5] for r in rs]
        for root in ("INTERNET", "LAN", "LOCAL", "STACKS"):
            self.assertTrue(any(root in x for x in labels), root)
        self.assertEqual(len({r[0] for r in rs}), len(rs))                          # one id per row
        for key, _, tog, _, lab, _ in rs:
            self.assertEqual(params(lab)["sel"], key)
        self.assertRegex(body, r'<span class="sym">✖</span> <span class="t-err">worker-1</span>')   # down: red, and a symbol besides the colour
        G, _ = render.map_graph()
        self.assertEqual(len(rs), len(graph.rows(G)))
        for r, row in zip(rs, graph.rows(G)):
            self.assertIn(graph.parts(G, row)["label"], r[5])

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
        view, foot = view_of(body), body[body.index("<footer"):]
        everything = self.page(link(view, "expand all"))
        self.assertEqual(params(link(view, "expand all"))["all"], True)
        self.assertNotIn("▸", [r[3] for r in rows_of(everything)])                 # every branch open
        self.assertGreater(len(rows_of(everything)), len(rows_of(body)))
        for k in rows_of(everything)[3:6]:                                          # with everything open, closing = shut
            if k[2]:
                self.assertIn(k[0], params(k[2])["shut"])
        folded = self.page(link(view_of(everything), "collapse all"))
        self.assertEqual(rows_of(folded), rows_of(body))
        only = self.page(link(view, "problems only"))
        self.assertTrue(params(link(view, "problems only"))["only"])
        self.assertLessEqual(len(rows_of(only)), len(rows_of(body)))
        self.assertIsNotNone(link(view_of(only), "all paths"))
        G, _ = render.map_graph()
        self.assertEqual([r[0] for r in rows_of(only)], [r["key"] for r in graph.rows(G, graph.State(only=True))])
        paused = self.page(link(foot, "pause"))
        self.assertNotIn('http-equiv="refresh"', paused)
        self.assertIn("paused", paused)
        self.assertIn('http-equiv="refresh"', self.page(link(paused[paused.index("<footer"):], "resume")))
        for url in (link(view, "expand all"), link(foot, "pause"), rows_of(body)[0][4], rows_of(body)[0][2]):  # every link keeps them
            self.assertEqual([params(url)[k] for k in ("view", "zoom", "refresh")], ["map", 150, 5], url)
        self.assertEqual([params(link(foot, "A+"))[k] for k in ("view", "zoom", "refresh")], ["map", 175, 5])
        self.assertEqual([params(link(foot, "+"))[k] for k in ("view", "zoom", "refresh")], ["map", 150, 6])

    def test_selected_row_shows_the_details_of_its_node(self):
        body = self.page("/?view=map&all=1")
        self.assertNotIn('class="props"', body)
        target = next(r for r in rows_of(body) if "shop-db-1" in r[5])
        page = self.page(target[4])
        self.assertIn('class="props"', page)
        self.assertTrue([r for r in rows_of(page) if r[0] == target[0]][0][1])     # highlighted
        G, _ = render.map_graph()
        shown = re.search(r'<aside class="props">.*?</aside>', page, re.S).group(0)
        self.assertGreaterEqual(shown.count("<dt"), len(graph.details(G, "ct:shop-db-1")))   # every fact of the node is a line
        self.assertIn("postgres:16", page)
        close = re.search(r'<a href="([^"]*)">close ✕</a>', page).group(1)
        self.assertEqual(params(html.unescape(close))["sel"], "")
        self.assertIn("expand all", page)
        hidden = self.page(f"/?view=map&sel={target[0]}")                           # its branch closed: still described
        self.assertIn("postgres:16", hidden)
        gone = self.page("/?view=map&sel=0123456789")                               # no such row: dropped, nothing described
        self.assertNotIn('class="props"', gone)

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
                     "/?view=map&zoom=99999999999999999999&refresh=-1&open=%3Cscript%3E", "/?view=map&open=&shut=&sel=",
                     "/?view=map&all=1&open=" + many):
            body = self.page(path)
            self.assertNotIn("<script", view_of(body).lower())
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
            self.assertEqual(len([k for k in self.srv.cache if k[:2] == ("shell", "map")]), 40)   # forty pages, one graph
            self.assertNotIn('class="props"', body)
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
            key = next(r for r in rows_of(body) if EVIL in r[5])[0]
            body = view_of(body) + view_of(self.page(f"/?view=map&all=1&sel={key}"))
        finally:
            render.map_graph = saved
        self.assertNotIn("<script", body.lower())
        self.assertNotIn('"onmouseover', body)
        self.assertNotIn("'x", body)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;&quot;onmouseover=alert(1) &#x27;x", body)

    def test_map_tab_and_the_feature_switch(self):
        tabs = lambda b: re.search(r'<nav class="tabs".*?</nav>', b).group(0)  # noqa: E731
        _, _, dash = get(self.srv, "/?zoom=150")
        self.assertIn('href="/?zoom=150&amp;view=map"', tabs(dash))
        saved = render.CFG["features"].get("map", True)
        render.CFG["features"]["map"] = False
        try:
            self.srv.cache.clear()
            _, _, dash = get(self.srv, "/?zoom=150")
            st, h, off = get(self.srv, "/?view=map&all=1")
        finally:
            render.CFG["features"]["map"] = saved
        self.assertNotIn("view=map", tabs(dash))
        self.assertEqual(st, 200)
        self.assertIn("map disabled in config.ini", off)
        self.assertNotIn('class="ob', off)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertIn('href="/?" data-key="1"', off)                                # the way out is still there

    def test_cache_is_bounded(self):
        for i in range(web.CACHE_MAX * 2):
            self.srv.page(**weburl.view_params(web.parse_qs("view=map&sel=%010x" % i)))
        self.srv.page(zoom=150)
        self.assertEqual(len(self.srv.cache), web.CACHE_MAX)
        self.assertIn(next(reversed(self.srv.cache)), self.srv.cache)
        self.assertEqual(next(reversed(self.srv.cache))[3], 150)              # the latest stays, the oldest went
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
            self.assertTrue(self.srv.page(zoom=150))
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
        self.assertIn('href="/?" data-key="1"', body)                                # the way out is still there


if __name__ == "__main__":
    unittest.main()
