"""Tests for the web shell (`?app=1`, `[ui] web = app`): its markup, /?set= and the nuc_ui cookie, /s/ assets, the settings page and the CSP.

The classic pages stay the default and unchanged (tests/golden/ locks them byte for byte).
"""
import base64
import hashlib
import os
import re
import sys
import unittest
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import graphjs  # noqa: E402
import nuc_config  # noqa: E402
import prefs  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
import webcss  # noqa: E402
import webjs  # noqa: E402
from test_web import get, serve  # noqa: E402


class Tree(HTMLParser):
    """Every start tag as (tag, attrs dict, open ancestors)."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.stack, self.text = [], [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs), tuple(self.stack)))
        if tag not in ("meta", "link", "input", "br", "hr", "polyline", "line", "circle", "path", "rect"):
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs), tuple(self.stack)))

    def handle_endtag(self, tag):
        while self.stack and self.stack[-1] != tag:
            self.stack.pop()
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)

    def find(self, tag, **attrs):
        return [(t, a, s) for t, a, s in self.tags if t == tag and all(k.replace("_", "-") in a and (v is True or a[k.replace("_", "-")] == v)
                                                                      for k, v in attrs.items())]


def cookie_of(headers):
    return headers.get("Set-Cookie", "")


class Shell(unittest.TestCase):
    maxDiff = 300

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.locked = serve(), serve("t" * 24)
        cls.expose = render.CFG["expose"]
        cls.status, cls.headers, cls.body = get(cls.srv, "/?app=1")
        cls.tree = Tree(cls.body)

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        render.CFG["expose"] = cls.expose
        for s in (cls.srv, cls.locked):
            s.shutdown()
            s.server_close()

    # ---- the markup
    def test_the_html_element_carries_what_the_style_sheet_and_scripts_read(self):
        (_, a, _), = self.tree.find("html")
        self.assertEqual((a["data-theme"], a["data-density"], a["data-prefs-src"]), ("auto", "desk", "config"))
        self.assertTrue(re.fullmatch(r"z100 shift-[012]", a["class"]), a["class"])

    def test_blocks_are_unique_and_in_the_order_top_kpis_cards(self):
        ids = [a["data-card"] for t, a, _ in self.tree.tags if "data-card" in a]
        self.assertEqual(ids[:2], ["__top", "__kpis"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids[2:]), set(prefs.CARDS))
        for t, a, _ in self.tree.tags:
            if "data-card" in a:
                self.assertTrue(re.fullmatch(r"[0-9a-f]{10}", a["data-rev"]))
        for t, a, _ in self.tree.find("article"):
            self.assertRegex(a["class"], r"^card s[1-4] st-(ok|warn|err|down|unknown|info)$")

    def test_main_carries_the_refresh_contract(self):
        (_, a, _), = self.tree.find("main")
        self.assertEqual(a["data-refresh"], "2")
        self.assertTrue(a["data-frag"].startswith("/?") and "frag=1" in a["data-frag"])
        self.assertIn("app=1", a["data-frag"])
        self.assertIn('<noscript><meta http-equiv="refresh" content="2"></noscript>', self.body)  # with the scripts the page polls; without them it reloads
        self.assertEqual(self.body.count('http-equiv="refresh"'), 1)

    def test_tabs_are_links_with_the_keys_and_help_and_settings(self):
        tabs = [a for t, a, s in self.tree.find("a") if "nav" in s]
        self.assertEqual([a["data-key"] for a in tabs], ["1", "2", "3", "4", "5"])
        self.assertEqual(sum(a.get("aria-current") == "page" for a in tabs), 1)
        self.assertTrue(self.tree.find("a", href="#help"))
        self.assertTrue(self.tree.find("a", href="/?app=1&view=settings"))

    def test_every_interactive_control_is_a_link_or_a_form(self):
        for t, a, _ in self.tree.tags:
            self.assertNotIn(t, ("button", "select", "textarea", "iframe", "form"), t)
            self.assertFalse([k for k in a if k.startswith("on") or k == "style"], (t, a))
        for t, a, _ in self.tree.find("a"):
            self.assertTrue(a["href"].startswith(("/?", "#")), a)

    def test_the_footer_has_pause_zoom_theme_and_density_as_links(self):
        self.assertTrue(self.tree.find("a", data_pause=True, data_key="Z"))
        sets = self.tree.find("a", data_set=True)
        themes = {a["data-theme"] for _, a, _ in sets if "data-theme" in a}
        self.assertEqual(themes, {"dark", "light", "high-contrast"})  # the current one (auto) is not a link
        self.assertEqual({a["data-density"] for _, a, _ in sets if "data-density" in a}, {"wall", "compact"})
        for _, a, _ in sets:
            self.assertRegex(a["href"], r"^/\?set=[a-z0-9_]+&back=")
        self.assertIn("A−", self.body)

    def test_the_stale_banner_is_empty_and_hidden_and_help_is_the_key_table(self):
        (_, a, _), = self.tree.find("div", id="stale")
        self.assertIn("hidden", a)
        self.assertEqual(a["role"], "status")
        (_, a, _), = self.tree.find("section", id="help")
        self.assertIn("Keyboard", self.body)
        self.assertTrue(self.tree.find("a", href="#", data_key="? Escape"))

    def test_the_blocks_use_only_the_tags_and_attributes_a_fragment_may_have(self):
        """Everything inside a data-card block (and the block itself) is in webjs.FRAG_TAGS / FRAG_ATTRS: a later fragment poll can take it over."""
        depth, n = [], 0

        class Walk(HTMLParser):
            def handle_starttag(self, tag, attrs):
                nonlocal n
                a = dict(attrs)
                inside = bool(depth) or "data-card" in a
                if inside:
                    n += 1
                    unit.assertIn(tag, webjs.FRAG_TAGS, tag)
                    allowed = {x.lower() for x in webjs.FRAG_ATTRS}  # the parser reports attribute names in lower case
                    for k in a:
                        unit.assertIn(k, allowed, (tag, k))
                    if "href" in a:
                        unit.assertTrue(a["href"].startswith(("/?", "#")), a["href"])
                if tag not in ("polyline", "br", "hr", "input", "line", "circle", "path", "rect", "col"):
                    depth.append(1) if inside else None

            def handle_endtag(self, tag):
                if depth and tag not in ("polyline", "br"):
                    depth.pop()
        unit = self
        Walk().feed(self.body)
        self.assertGreater(n, 100)

    def test_a_kpi_is_a_tile_with_its_state(self):
        tiles = [a for t, a, _ in self.tree.tags if "kpi" in (a.get("class") or "").split()]
        self.assertGreaterEqual(len(tiles), 4)
        self.assertTrue(all(re.search(r"st-(ok|warn|err|down|unknown|info)", a["class"]) for a in tiles))

    def test_the_overview_is_by_severity_unless_the_order_is_fixed(self):
        order = lambda body: re.findall(r'<article class="card s\d st-(\w+)"', body)  # noqa: E731
        rank = [web.STATE_RANK[x] for x in order(self.body)]
        self.assertEqual(rank, sorted(rank))
        _, _, fixed = get(self.srv, "/?app=1&ui=1.of")
        self.assertEqual(re.findall(r'data-card="(\w+)"', fixed)[2:4], ["attention", "exposure"])

    def test_the_other_views_live_inside_the_shell(self):
        for view in ("map", "cpu", "health", "ai"):
            st, h, body = get(self.srv, f"/?view={view}&app=1")
            self.assertEqual(st, 200, view)
            self.assertIn('data-card="__top"', body)
            self.assertIn('stylesheet" href="/s/app.', body)
            self.assertEqual(body.count("<main"), 1, view)  # the old bodies' <main> became <div>
            self.assertTrue(Tree(body).find("a", aria_current="page"), view)
        st, h, body = get(self.srv, "/?view=map&as=graph&app=1")
        self.assertIn("<script>", body)  # the graph's own fixed script, pinned by its hash
        self.assertRegex(h["Content-Security-Policy"], r"script-src 'sha256-[A-Za-z0-9+/=]+'")
        self.assertIn("style-src 'self' 'unsafe-inline'", h["Content-Security-Policy"])

    def test_a_card_in_full(self):
        st, h, body = get(self.srv, "/?card=exposure")
        self.assertEqual(st, 200)
        self.assertEqual([a["data-card"] for t, a, _ in Tree(body).tags if "data-card" in a and not a["data-card"].startswith("__")], ["exposure"])
        self.assertIn("overview", body)
        self.assertEqual(get(self.srv, "/?card=nonsense")[0], 200)  # not a card: the classic page

    def test_pause_stops_the_reload(self):
        _, _, body = get(self.srv, "/?app=1&pause=1")
        self.assertNotIn('http-equiv="refresh"', body)
        self.assertIn('data-paused="1"', body)
        self.assertIn('aria-pressed="true"', body)

    # ---- CSP and headers
    def test_csp_keeps_default_none_no_script_and_loads_only_its_own_style(self):
        csp = self.headers["Content-Security-Policy"]
        scripts = "'sha256-%s' 'sha256-%s' 'sha256-%s'" % tuple(webjs.sha256_b64(js) for js in (webjs.REFRESH_JS, webjs.KEYS_JS, webjs.PREFS_JS))
        self.assertEqual(csp, "default-src 'none'; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
                              f"script-src {scripts}; connect-src 'self'; require-trusted-types-for 'script'; trusted-types nuc-frag")
        self.assertEqual(self.headers["Vary"], "Cookie")
        self.assertEqual(self.headers["Cache-Control"], "no-store")

    def test_the_ai_page_keeps_its_forms_and_csrf(self):
        st, h, body = get(self.srv, "/?view=ai&app=1&sel=qwen3-4b")
        self.assertIn("form-action 'self'", h["Content-Security-Policy"])
        self.assertIn("style-src 'self' 'unsafe-inline'", h["Content-Security-Policy"])
        self.assertEqual(h["Content-Security-Policy"].count("sha256-"), 3)  # the shell's three scripts, and nothing of the form's
        self.assertEqual(h["Referrer-Policy"], "same-origin")
        forms = Tree(body).find("form", method="post")
        self.assertTrue(forms)
        self.assertRegex(body, r'name="csrf" value="[^"]+"')
        self.assertIn("app=1", re.search(r'name="back" value="([^"]*)"', body).group(1))  # a post comes back to the shell
        render_cfg = render.CFG["ai"]
        with mock.patch.dict(render_cfg, {"web_actions": False}):
            _, h, body = get(self.srv, "/?view=ai&app=1")
        self.assertIn("form-action 'none'", h["Content-Security-Policy"])
        self.assertNotIn('method="post"', body)

    # ---- /?set=
    def test_set_stores_the_cookie_and_redirects_to_a_canonical_view(self):
        st, h, _ = get(self.srv, "/?set=tl&back=view%3Dhealth%26period%3D30%26app%3D1%26junk%3D1")
        self.assertEqual(st, 302)
        self.assertEqual(h["Location"], "/?view=health&app=1&period=30")
        ck = cookie_of(h)
        self.assertTrue(ck.startswith("nuc_ui=1.tl;"), ck)
        for part in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=31536000"):
            self.assertIn(part, ck)

    def test_set_adds_to_what_the_cookie_holds_and_reset_clears_it(self):
        st, h, _ = get(self.srv, "/?set=dw", headers={"Cookie": "nuc_ui=1.tl"})
        self.assertEqual(cookie_of(h).split(";")[0], "nuc_ui=1.tl.dw")
        st, h, _ = get(self.srv, "/?set=reset", headers={"Cookie": "nuc_ui=1.tl"})
        self.assertEqual(st, 302)
        self.assertIn("Max-Age=0", cookie_of(h))
        self.assertTrue(cookie_of(h).startswith("nuc_ui=;"))

    def test_set_never_redirects_anywhere_but_to_a_view_of_this_server(self):
        for back in ("//evil.example/", "https://evil.example/x", "/\\evil.example", "view=map&open=" + "a" * 5000, "%00", "\r\nSet-Cookie: x=1",
                     "view=ai&sel=../../etc", "javascript:alert(1)", "http://127.0.0.1:1/?x"):
            from urllib.parse import quote
            st, h, _ = get(self.srv, "/?set=dk&back=" + quote(back, safe=""))
            self.assertEqual(st, 302, back)
            loc = h["Location"]
            self.assertTrue(loc == "/" or loc.startswith("/?"), loc)
            self.assertNotIn("evil", loc)
            self.assertNotIn("\n", loc)

    def test_set_refuses_what_is_not_a_preference(self):
        for field in ("zz", "tx", "t", "dw.tl", "kpb_nope", "lat9", "x" * 300, "../x", "tl;Path=/"):
            self.assertEqual(get(self.srv, "/?set=" + field.replace(";", "%3B"))[0], 400, field)
        self.assertEqual(get(self.srv, "/?set=tl", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(get(self.srv, "/?set=tl", headers={"Sec-Fetch-Site": "same-origin"})[0], 302)

    def test_set_follows_the_same_access_rules_as_the_page(self):
        self.assertEqual(get(self.locked, "/?set=tl")[0], 401)
        self.assertEqual(get(self.srv, "/?set=tl", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(get(self.locked, "/?set=tl", headers={"Authorization": "Bearer " + "t" * 24})[0], 302)

    def test_the_cookie_changes_the_page_and_an_invalid_one_is_ignored(self):
        _, _, body = get(self.srv, "/?app=1", headers={"Cookie": "nuc_ui=1.tl.dw"})
        self.assertRegex(body, r'data-theme="light" data-density="wall" class="[^"]*" data-prefs-src="cookie" data-prefs="1.tl.dw"')
        for bad in ("nuc_ui=1.tz", "nuc_ui=2.tl", "nuc_ui=" + "1." + "t" * 300, "nuc_ui=1.tl.é", "nuc_ui=junk"):
            _, _, body = get(self.srv, "/?app=1", headers={"Cookie": bad.encode("utf-8").decode("latin-1")})
            self.assertIn('data-theme="auto" data-density="desk"', body, bad)
            self.assertIn('data-prefs-src="config"', body, bad)

    def test_ui_in_the_url_wins_over_the_cookie_for_that_url_only(self):
        _, _, body = get(self.srv, "/?app=1&ui=1.td", headers={"Cookie": "nuc_ui=1.tl"})
        self.assertIn('data-theme="dark"', body)
        self.assertIn('data-prefs-src="url"', body)
        self.assertIn("ui=1.td", self.tree and body)  # it travels with the links of that page
        _, _, body = get(self.srv, "/?app=1&ui=1.zz", headers={"Cookie": "nuc_ui=1.tl"})
        self.assertIn('data-theme="light"', body)  # an invalid one is dropped

    def test_the_hostile_cookie_does_not_reach_the_page(self):
        _, _, body = get(self.srv, "/?app=1", headers={"Cookie": 'nuc_ui="><script>alert(1)</script>'})
        self.assertNotIn("alert(1)", body)

    # ---- the style sheet
    def test_the_stylesheet_is_served_immutable_by_its_hash(self):
        path = webcss.asset_path("app")
        self.assertRegex(path, r"^/s/app\.[0-9a-f]{8}\.css$")
        self.assertIn(f'href="{path}"', self.body)
        st, h, body = get(self.srv, path)
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(h["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest()[:8], path.split(".")[1])
        st, h, body = get(self.srv, path, "HEAD")
        self.assertEqual((st, body, h["Content-Length"]), (200, "", str(len(webcss.CSS.encode()))))

    def test_an_unknown_asset_is_404(self):
        sha = webcss.asset_path("app").split(".")[1]
        for p in ("/s/app.00000000.css", f"/s/nope.{sha}.css", f"/s/app.{sha}.js", "/s/", "/s/../web.py", f"/s/app.{sha}.css/x", "/s/app.css"):
            self.assertEqual(get(self.srv, p)[0], 404, p)

    def test_assets_have_the_same_host_and_token_checks(self):
        path = webcss.asset_path("app")
        self.assertEqual(get(self.srv, path, headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(get(self.locked, path)[0], 401)
        self.assertEqual(get(self.locked, path, headers={"Authorization": "Bearer " + "t" * 24})[0], 200)

    def test_the_stylesheet_has_the_themes_densities_zooms_and_no_stray_colours(self):
        css = webcss.CSS
        for name in ("dark", "light", "high-contrast"):
            self.assertIn(f':root[data-theme="{name}"]', css)
        for q in ("prefers-color-scheme", "prefers-contrast", "forced-colors"):
            self.assertIn(q, css)
        for d in ("wall", "desk", "compact"):
            self.assertIn(f'html[data-density="{d}"]', css)
        for z in web.ZOOMS:
            self.assertIn(f"html.z{z}{{", css)
        self.assertEqual(sorted(webcss.ZOOMS), sorted(web.ZOOMS))
        self.assertIn("@container app", css)
        legacy = webcss.themed("".join(webcss.LEGACY_SOURCES))
        self.assertEqual(webcss.HEX.findall(legacy), [], "a colour of htmlview's palette that webcss.REMAP does not map")
        # no colour literal outside the token blocks: everything else says var(--...)
        outside = css
        for table in list(webcss.THEME_TABLES.values()) + [webcss.DARK]:
            outside = outside.replace(webcss.decls(table), "")
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", outside), [], "a colour outside the token blocks")
        self.assertNotIn("http", css.replace("http-equiv", ""))  # nothing loads from anywhere

    def test_a_native_card_is_html_and_a_raw_card_is_a_pre(self):
        import cards
        found = 0
        for cid in prefs.CARDS:
            m = re.search(r'<article class="[^"]*"[^>]*data-card="%s".*?</article>' % cid, self.body, re.S)
            self.assertIsNotNone(m, cid)
            native = cid in cards.NATIVE
            self.assertEqual('class="tty"' in m.group(0), not native, cid)
            if native:
                found += 1
                self.assertNotIn("<pre", m.group(0), cid)
                self.assertTrue(re.search(r'class="(tbl|ln|msg|kv|wrap|grp)', m.group(0)), cid)
        self.assertGreaterEqual(found, 4)

    def test_the_cpu_key_figure_counts_threads(self):
        self.assertIn("16 threads", self.body)
        self.assertNotIn("16 cores", self.body)

    def test_the_wall_cuts_long_lists_and_says_so(self):
        import ui
        t = ui.Table([ui.Col("a", "A")], [ui.Row(["x%d" % i]) for i in range(6)])
        out = web.wall_trim([t, ui.More(2, "more"), ui.Msg("info", "x")])
        self.assertEqual(len(out[0].rows), web.WALL_ROWS)
        self.assertEqual((out[1].n, len(out)), (5, 3))
        short = [ui.Table([ui.Col("a", "A")], [ui.Row(["x"])])]
        self.assertIs(web.wall_trim(short), short)

    def test_the_components_markup_is_inside_the_fragment_allowlist(self):
        import htmlview
        import ui
        node = ui.Card("t", "T", "n", "ok", [
            ui.Line([ui.Span("a", "ok", bold=True), ui.Bar(.5, "5"), ui.Spark([1, 2, 3])]), ui.Msg("warn", "m"),
            ui.KV([("k", "v")]), ui.Table([ui.Col("a", "A", prio=1), ui.Col("b", "B", "r", num=True)],
                                          [ui.Row(["x", "1"], href="/?x=1")], groups=[("g", 0)]),
            ui.Wrap(["a", "b"], max_lines=1), ui.More(2, "more"), ui.Group([ui.Pill("p", "ok")], "t"),
            ui.Tree([(1, "n", "ok")]), ui.Details("s", [ui.Msg("info", "i")]), ui.Kpi("cpu", "CPU", "1", "%", "ok")])
        seen = []
        unit = self

        class W(HTMLParser):
            def handle_starttag(self, tag, attrs):
                seen.append(tag)
                unit.assertIn(tag, webjs.FRAG_TAGS, tag)
                for k, _ in attrs:
                    unit.assertIn(k, {x.lower() for x in webjs.FRAG_ATTRS}, (tag, k))
        W().feed(htmlview.html(node))
        self.assertGreater(len(seen), 30)

    def test_every_class_the_components_emit_is_styled(self):
        import htmlview
        import ui
        node = ui.Card("t", "T", "n", "ok", [ui.Msg("ok", "m"), ui.Notice("warn", "m"), ui.KV([("k", "v")]), ui.Wrap(["a"]), ui.More(1),
                                             ui.Group([], "t"), ui.Pill("p", "warn"), ui.Tree([(0, "a", "ok")]), ui.Details("s"),
                                             ui.Table([ui.Col("a", "A", prio=3)], [ui.Row(["x"], tone="err")], groups=[("g", 0)]),
                                             ui.Line([ui.Span("a", "banner_ok", True, True), ui.Bar(.9, "9"), ui.Spark([1, 2])])])
        used = set(c for cl in re.findall(r'class="([^"]*)"', htmlview.html(node)) for c in cl.split())
        css = webcss.CSS
        for c in used - {"card", "s1", "st-ok", "state", "bg", "fg", "c-a", "tbl"}:  # the article and its header are the shell's own (card_article)
            self.assertIn("." + c, css, c)

    def test_every_class_of_the_thirteen_cards_is_styled(self):
        """The markup of all the cards of the demo (the three demo systems), not of a hand-made sample: a class without a rule is a card that
        looks like the console's text. Matched as whole class names: `.c` is not `.cb`."""
        css, used, saved = webcss.CSS, set(), render.DEMO_OS
        try:
            for os_name in (None, "darwin", "windows"):
                render.DEMO_OS = os_name
                self.srv.cache.clear()
                _, _, body = get(self.srv, "/?app=1")
                for art in re.findall(r"<article.*?</article>", body, re.S):
                    used.update(c for cl in re.findall(r'class="([^"]*)"', art) for c in cl.split())
        finally:
            render.DEMO_OS = saved
            self.srv.cache.clear()
        self.assertGreater(len(used), 40)
        own = {"card", "s1", "s2", "s3", "s4", "st-ok", "st-warn", "st-err", "st-down", "st-unknown", "st-info"}  # the article's: card_article
        bare = [c for c in sorted(used - own) if not c.startswith("c-")  # <col class="c-KEY">: a hook for the column's key, not a style
                and not re.search(r"\.%s(?![\w-])" % re.escape(c), css)]
        self.assertEqual(bare, [], "classes the cards emit that the style sheet does not know")

    # ---- the settings page
    def test_the_settings_page_has_appearance_export_and_about(self):
        st, h, body = get(self.srv, "/?view=settings", headers={"Cookie": "nuc_ui=1.tl.pv"})
        self.assertEqual(st, 200)
        t = Tree(body)
        for text in ("Appearance", "Export", "About this machine", "Theme", "Density", "Preset", "Order", "Start view", "Key figures"):
            self.assertIn(text, body)
        (_, a, _), = t.find("pre", id="export")
        self.assertIn("[ui]", body)
        self.assertRegex(body, r'<pre id="export"[^>]*>\[ui\]\nweb = app\ntheme = light\n')
        self.assertIn("preset = server", body)
        self.assertIn('id="cookie-v">1.tl.pv<', body)
        links = [a["href"] for _, a, _ in t.find("a", data_set=True)]
        self.assertIn("/?set=reset&back=view%3Dsettings", links)
        self.assertTrue(any(l.startswith("/?set=kpb_in_la") for l in links) or any("set=k" in l for l in links))
        self.assertNotIn('data-card="__kpis"', body)
        self.assertIn("form-action 'none'", h["Content-Security-Policy"])
        for tag, a, _ in t.tags:
            self.assertNotIn(tag, ("form", "select"))
        (_, a, _), = t.find("button", data_copy="#export")  # the one button: PREFS_JS copies the snippet (it does nothing without the script)
        self.assertEqual(a["type"], "button")

    def test_the_kpi_links_add_remove_and_keep_one_to_eight(self):
        _, _, body = get(self.srv, "/?view=settings&ui=1.kpb_in")
        self.assertIn("set=kpb_in_la", body)          # adding LAN keeps the order
        self.assertIn("set=kin", body)                # removing Problems
        _, _, body = get(self.srv, "/?view=settings&ui=1.kpb")
        self.assertNotIn("set=k&", body)               # the last one cannot be removed
        eight = "_".join(prefs.KPI_CODES[k] for k in prefs.KPI_IDS[:8])
        _, _, body = get(self.srv, f"/?view=settings&ui=1.k{eight}")
        self.assertNotIn(f"set=k{eight}_", body)       # a ninth cannot be added

    def test_about_install_and_portable(self):
        self.srv.cache.clear()
        _, _, body = get(self.srv, "/?view=settings")
        self.assertIn("nuc-console " + nuc_config.VERSION, body)
        self.assertIn("installed on this machine", body)
        self.assertIn("<code class=\"cmd\">nuc-console-update</code>", body)
        self.assertIn("loopback only (127.0.0.1), no token", body)
        self.assertIn("mode browser · zoom 100%", body)
        self.assertIn("off ([telegram] enabled = no)", body)
        self.assertIn("allowed ([ai] web_actions = yes)", body)
        with mock.patch.object(nuc_config, "PORTABLE", "/tmp/nuc-portable"):
            self.srv.cache.clear()
            _, _, body = get(self.srv, "/?view=settings")
        self.assertIn("portable run", body)
        self.assertIn(os.path.abspath("/tmp/nuc-portable"), body)
        self.assertIn("bin/nuc-console-update", body)
        self.assertNotIn("installed on this machine", body)

    def test_about_token_telegram_lock_and_config_error(self):
        _, _, body = get(self.locked, "/?view=settings", headers={"Authorization": "Bearer " + "t" * 24})
        self.assertIn("a token is required", body)
        cfg = render.CFG
        with mock.patch.dict(cfg["ai"], {"web_actions": False}), mock.patch.dict(cfg["telegram"], {"enabled": True}), \
                mock.patch.dict(cfg, {"config_error": "bad line 3"}), mock.patch.object(render, "telegram_status", lambda path=None: {"paired": False}):
            self.srv.cache.clear()
            _, _, body = get(self.srv, "/?view=settings")
        self.assertIn("locked by the admin", body)
        self.assertIn("not paired", body)
        self.assertIn("unreadable: bad line 3", body)

    # ---- the classic pages stay the default
    def test_the_classic_pages_are_unchanged_and_default(self):
        _, h, body = get(self.srv, "/")
        self.assertIn("<pre>", body)
        self.assertNotIn("/s/app.", body)
        self.assertEqual(h["Content-Security-Policy"], web.CSP)
        self.assertNotIn("script-src", web.CSP)
        self.assertNotIn("<script", body.lower())
        _, _, body = get(self.srv, "/?app=0")
        self.assertNotIn("/s/app.", body)

    def test_ui_web_app_makes_the_shell_the_default_and_app_0_the_way_back(self):
        with mock.patch.dict(render.CFG["ui"], {"web": "app"}):
            self.srv.cache.clear()
            _, _, body = get(self.srv, "/?view=cpu")
            self.assertIn("/s/app.", body)
            _, _, classic = get(self.srv, "/?app=0")
            self.assertNotIn("/s/app.", classic)

    def test_the_token_redirect_keeps_the_shell(self):
        st, h, _ = get(self.locked, "/?token=" + "t" * 24 + "&app=1&view=health")
        self.assertEqual((st, h["Location"]), (302, "/?view=health&app=1"))


INLINE = re.compile(r"<script>(.*?)</script>", re.S)
VOID = ("meta", "link", "input", "br", "hr", "polyline", "line", "circle", "path", "rect")


def inline_scripts(body):
    return INLINE.findall(body)


def hashes(scripts):
    return ["'sha256-%s'" % base64.b64encode(hashlib.sha256(x.encode("utf-8")).digest()).decode() for x in scripts]


def script_src(csp):
    m = re.search(r"script-src ([^;]*)", csp)
    return m.group(1).split() if m else []


def split_blocks(text):
    """The top-level data-card elements of a fragment (or of a page) as (data-card, raw html)."""
    lines, pos = [0], 0
    for ln in text.split("\n"):
        pos += len(ln) + 1
        lines.append(pos)
    out, depth, start = [], [], [None]

    class P(HTMLParser):
        def at(self):
            ln, col = self.getpos()
            return lines[ln - 1] + col

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if not depth and start[0] is None and "data-card" in a:
                start[0] = (self.at(), a["data-card"])
            if start[0] is not None and tag not in VOID:
                depth.append(tag)

        def handle_endtag(self, tag):
            if start[0] is not None and tag in depth:  # a void or self-closed element (<polyline/>) has no end of its own
                while depth and depth.pop() != tag:
                    pass
                if not depth:
                    out.append((start[0][1], text[start[0][0]:self.at() + len("</%s>" % tag)]))
                    start[0] = None
    P(convert_charrefs=False).feed(text)
    return out


class Scripts(unittest.TestCase):
    """What the shell's pages carry as scripts, the CSP that lists exactly them, the fragment endpoint and /?set=...&frag=1."""
    maxDiff = None
    PAGES = ("/?app=1", "/?app=1&pause=1", "/?app=1&view=settings", "/?card=exposure", "/?app=1&view=map", "/?app=1&view=cpu", "/?app=1&view=cpu&sel=1",
             "/?app=1&view=health", "/?app=1&view=ai", "/?app=1&view=ai&sel=qwen3-4b", "/?app=1&view=ai&sel=qwen3-4b&confirm=delete",
             "/?app=1&view=map&as=graph", "/?app=1&view=map&all=1")

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.locked = serve(), serve("t" * 24)
        cls.expose = render.CFG["expose"]

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        render.CFG["expose"] = cls.expose
        for s in (cls.srv, cls.locked):
            s.shutdown()
            s.server_close()

    # ---- the CSP lists exactly the scripts of the page
    def test_a_shell_page_carries_its_scripts_and_the_csp_lists_exactly_their_hashes(self):
        for path in self.PAGES:
            st, h, body = get(self.srv, path)
            self.assertEqual(st, 200, path)
            scripts = inline_scripts(body)
            want = [webjs.REFRESH_JS, webjs.KEYS_JS, webjs.PREFS_JS] + ([graphjs.SCRIPT] if "as=graph" in path else [])
            self.assertEqual(scripts, want, path)
            self.assertEqual(script_src(h["Content-Security-Policy"]), hashes(want), path)
            self.assertEqual(body.lower().count("<script"), len(want), path)  # no other script: none from outside, none in an attribute
            self.assertIsNone(re.search(r"\son[a-z]+=", body), path)
            for text in scripts:
                self.assertNotIn("</script", text.lower())
                self.assertNotIn("<!--", text)

    def test_the_policy_of_each_page(self):
        for path in self.PAGES:
            st, h, _ = get(self.srv, path)
            csp = h["Content-Security-Policy"]
            parts = [p.strip() for p in csp.split(";")]
            self.assertEqual(parts[0], "default-src 'none'", path)
            for want in ("style-src 'self' 'unsafe-inline'", "base-uri 'none'", "frame-ancestors 'none'", "connect-src 'self'",
                         "require-trusted-types-for 'script'", "trusted-types nuc-frag"):
                self.assertIn(want, parts, path)  # refresh and preferences are on every shell page
            self.assertEqual(csp.count("connect-src"), 1)
            forms = "view=ai" in path
            self.assertIn("form-action 'self'" if forms else "form-action 'none'", parts, path)
            self.assertEqual(h["Referrer-Policy"], "same-origin" if forms else "no-referrer", path)

    def test_the_policy_builder(self):
        self.assertEqual(web.page_csp(), web.CSP)
        self.assertEqual(web.CSP, "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.assertEqual(web.page_csp(forms=True), web.CSP.replace("form-action 'none'", "form-action 'self'"))
        one = web.page_csp(["x = 1;"])
        self.assertEqual(one, web.CSP + "; script-src " + hashes(["x = 1;"])[0])  # an unknown script: its hash only, no connection, no Trusted Types
        self.assertEqual(web.page_csp(["x = 1;", "x = 1;"]), one)
        for js, connect in ((webjs.REFRESH_JS, True), (webjs.KEYS_JS, False), (webjs.PREFS_JS, True), (webjs.BUILDER_JS, True)):
            csp = web.page_csp([js])
            self.assertEqual("connect-src 'self'" in csp, connect)
            self.assertEqual("trusted-types nuc-frag" in csp, js is webjs.REFRESH_JS)
        self.assertNotIn("script-src", web.page_csp([""]))
        self.assertIn("style-src 'self' 'unsafe-inline'", web.page_csp(shell=True))

    def test_the_classic_pages_have_no_script_src_except_the_graph(self):
        for path in ("/", "/?view=cpu", "/?view=map", "/?view=health", "/?view=ai", "/?app=0", "/?cols=100&full=1"):
            st, h, body = get(self.srv, path)
            csp = h["Content-Security-Policy"]
            self.assertTrue(csp.startswith("default-src 'none'"))
            for word in ("script-src", "connect-src", "trusted-types"):
                self.assertNotIn(word, csp, path)
            self.assertNotIn("<script", body.lower(), path)
        st, h, body = get(self.srv, "/?view=map&as=graph")
        self.assertEqual(script_src(h["Content-Security-Policy"]), hashes(inline_scripts(body)))
        self.assertEqual(len(inline_scripts(body)), 1)
        self.assertNotIn("connect-src", h["Content-Security-Policy"])
        self.assertNotIn("trusted-types", h["Content-Security-Policy"])

    def test_the_meta_refresh_is_for_browsers_without_the_scripts(self):
        for path in self.PAGES:
            body = get(self.srv, path)[2]
            metas = re.findall(r"(<noscript>)?<meta http-equiv=\"refresh\" content=\"(\d+)\">", body)
            self.assertEqual(len(metas), body.count('http-equiv="refresh"'), path)
            for wrap, _secs in metas:
                self.assertEqual(wrap, "<noscript>", path)
            if "pause=1" in path or "settings" in path:
                self.assertEqual(metas, [], path)
        for path in ("/?app=1&view=cpu", "/?app=1&view=map&as=graph", "/?app=1"):
            self.assertIn('<noscript><meta http-equiv="refresh" content="2"></noscript>', get(self.srv, path)[2], path)

    def test_a_paused_page_can_still_be_resumed_by_the_script(self):
        body = get(self.srv, "/?app=1&pause=1")[2]
        (_, a, _), = Tree(body).find("main")
        self.assertEqual((a["data-paused"], a["data-refresh"]), ("1", "2"))
        self.assertIn("pause=1", a["data-frag"])
        for path in ("/?app=1&view=map&as=graph", "/?app=1&view=settings"):  # a graph reloads itself, the settings never poll: the pause link is the server's
            self.assertFalse(Tree(get(self.srv, path)[2]).find("main", data_refresh=True), path)

    # ---- the fragment
    def test_the_fragment_is_the_blocks_of_the_page(self):
        for path in self.PAGES:
            _, _, page = get(self.srv, path)
            st, h, frag = get(self.srv, path + "&frag=1")
            self.assertEqual(st, 200, path)
            self.assertEqual(h["X-Nuc-Fragment"], "1")
            self.assertEqual(h["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(h["Content-Security-Policy"], web.CSP)
            self.assertEqual((h["Cache-Control"], h["Vary"], h["X-Content-Type-Options"]), ("no-store", "Cookie", "nosniff"))
            self.assertRegex(h["ETag"], r'^"[0-9a-f]{20}"$')
            theirs, mine = split_blocks(frag), split_blocks(page)
            self.assertEqual("".join(raw for _, raw in theirs), frag, path)  # nothing between the blocks, nothing around them
            self.assertEqual([i for i, _ in theirs], [i for i, _ in mine if i != "__view" or "as=graph" not in path][:len(theirs)], path)
            self.assertEqual(len(theirs), len(mine), path)
            by_id = dict(mine)  # the clock of the top bar may tick between the two requests: every other block is the page's, byte for byte
            for cid, raw in theirs:
                if cid != "__top":
                    self.assertEqual(raw, by_id[cid], (path, cid))

    def test_the_overview_fragment_has_every_card_and_a_view_has_one_block(self):
        ids = [i for i, _ in split_blocks(get(self.srv, "/?app=1&frag=1")[2])]
        self.assertEqual(ids[:2], ["__top", "__kpis"])
        self.assertEqual(set(ids[2:]), set(prefs.CARDS))
        for view in ("map", "cpu", "health", "ai"):
            ids = [i for i, _ in split_blocks(get(self.srv, f"/?app=1&view={view}&frag=1")[2])]
            self.assertEqual(ids, ["__top", "__kpis", "__view"], view)

    def test_a_fragment_has_only_the_tags_and_attributes_the_refresh_script_takes(self):
        unit = self

        class Walk(HTMLParser):
            seen = 0

            def handle_starttag(self, tag, attrs):
                self.seen += 1
                unit.assertIn(tag, webjs.FRAG_TAGS)
                for k, v in attrs:
                    unit.assertIn(k, {x.lower() for x in webjs.FRAG_ATTRS}, (tag, k))  # the parser reports attribute names in lower case
                    if k == "href":
                        unit.assertTrue(v.startswith(("/?", "#")), v)
                    if k == "action":
                        unit.assertTrue(v.startswith("/") and not v.startswith("//"), v)
                    if k == "method":
                        unit.assertIn(v, ("get", "post"))
                    if k == "name":
                        unit.assertIn(tag, ("input", "select", "textarea", "button"))
                    if k == "id":
                        unit.assertNotIn(v, ("stale", "live", "help", "export"))
            handle_startendtag = handle_starttag
        for path in self.PAGES + ("/?app=1&view=cpu&sort=mem&sel=1", "/?app=1&view=ai&sel=qwen3-30b-a3b", "/?app=1&view=ai&confirm=delete-all",
                                  "/?app=1&full=1", "/?app=1&ui=1.dw.ls", "/?app=1&ui=1.tl.pm"):
            w = Walk()
            w.feed(get(self.srv, path + "&frag=1")[2])
            self.assertGreater(w.seen, 10, path)

    def test_the_ai_fragment_keeps_its_forms_and_csrf(self):
        frag = get(self.srv, "/?app=1&view=ai&sel=qwen3-4b&frag=1")[2]
        self.assertTrue(Tree(frag).find("form", method="post"))
        self.assertRegex(frag, r'name="csrf" value="[^"]+"')

    def test_etag_and_304(self):
        path = "/?app=1&view=cpu&frag=1"
        for _ in range(3):  # the clock of the top bar may tick between two requests: ask again
            _, h, body = get(self.srv, path)
            st, h2, b2 = get(self.srv, path, headers={"If-None-Match": h["ETag"]})
            if st == 304:
                break
        self.assertEqual((st, b2), (304, ""))
        self.assertEqual((h2["ETag"], h2["X-Nuc-Fragment"]), (h["ETag"], "1"))
        self.assertNotIn("Content-Length", h2)
        self.assertEqual(h["ETag"], '"%s"' % hashlib.sha256(body.encode()).hexdigest()[:20])  # the hash of the blocks
        self.assertEqual(get(self.srv, path, headers={"If-None-Match": "W/" + h["ETag"]})[0] in (200, 304), True)
        st, _, b3 = get(self.srv, path, headers={"If-None-Match": '"0000"'})
        self.assertEqual(st, 200)
        self.assertTrue(b3)
        self.assertNotEqual(get(self.srv, "/?app=1&view=health&frag=1")[1]["ETag"], h["ETag"])  # other blocks, another tag

    def test_head_is_the_headers_of_the_get(self):
        st, h, body = get(self.srv, "/?app=1&view=health&frag=1", "HEAD")
        self.assertEqual((st, body), (200, ""))
        self.assertEqual((h["X-Nuc-Fragment"], h["Content-Type"]), ("1", "text/html; charset=utf-8"))
        self.assertGreater(int(h["Content-Length"]), 500)

    def test_only_shell_pages_have_fragments(self):
        for path in ("/?frag=1", "/?view=cpu&frag=1", "/?app=0&frag=1"):
            st, h, body = get(self.srv, path)
            self.assertEqual(st, 200)
            self.assertNotIn("X-Nuc-Fragment", h, path)
            self.assertNotIn("ETag", h, path)
            self.assertIn("<pre>", body)  # the classic page, as ever

    def test_a_fragment_has_the_same_access_rules_as_the_page(self):
        self.assertEqual(get(self.locked, "/?app=1&frag=1")[0], 401)
        self.assertEqual(get(self.locked, "/?app=1&frag=1", headers={"Authorization": "Bearer " + "t" * 24})[0], 200)
        self.assertEqual(get(self.locked, "/?app=1&frag=1", headers={"Cookie": "nuc_token=" + "t" * 24})[0], 200)
        self.assertEqual(get(self.locked, "/?app=1&frag=1&token=" + "t" * 24)[0], 302)  # the token moves into a cookie first, like for any page
        self.assertEqual(get(self.srv, "/?app=1&frag=1", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(get(self.srv, "/?app=1&frag=1", "HEAD", headers={"Host": "evil.example"})[0], 421)

    def test_the_cookie_shapes_the_fragment_like_the_page(self):
        _, _, fixed = get(self.srv, "/?app=1&frag=1", headers={"Cookie": "nuc_ui=1.of"})
        self.assertEqual([i for i, _ in split_blocks(fixed)][2:4], ["attention", "exposure"])
        self.assertEqual(get(self.srv, "/?app=1&frag=1", headers={"Cookie": "nuc_ui=<script>"})[0], 200)

    # ---- /?set=...&frag=1
    def test_set_with_frag_answers_204_and_the_cookie(self):
        st, h, body = get(self.srv, "/?set=tl&frag=1")
        self.assertEqual((st, body), (204, ""))
        self.assertIn("nuc_ui=1.tl;", h["Set-Cookie"])
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertEqual(h["X-Nuc-Prefs"], "1.tl")
        self.assertNotIn("Location", h)
        self.assertNotIn("Content-Length", h)
        st, h, _ = get(self.srv, "/?set=dw&frag=1", headers={"Cookie": "nuc_ui=1.tl"})
        self.assertEqual((st, h["X-Nuc-Prefs"]), (204, "1.tl.dw"))
        self.assertRegex(h["X-Nuc-Prefs"], r"^[A-Za-z0-9_.~:,-]{1,256}$")  # what PREFS_JS accepts to keep
        st, h, _ = get(self.srv, "/?set=reset&frag=1", headers={"Cookie": "nuc_ui=1.tl"})
        self.assertEqual((st, h["X-Nuc-Prefs"]), (204, "1"))
        self.assertIn("Max-Age=0", h["Set-Cookie"])

    def test_set_takes_back_the_whole_string_it_gave(self):
        """PREFS_JS keeps X-Nuc-Prefs and sends it again as ?set= when the browser sent no cookie."""
        st, h, _ = get(self.srv, "/?set=1.tl.dw.kpb_in&frag=1")
        self.assertEqual((st, h["X-Nuc-Prefs"]), (204, "1.tl.dw.kpb_in"))
        self.assertIn("nuc_ui=1.tl.dw.kpb_in;", h["Set-Cookie"])
        for bad in ("1.zz", "1.", "1.TL", "2.tl", "1..tl"):
            self.assertEqual(get(self.srv, "/?set=%s&frag=1" % bad)[0], 400, bad)
        self.assertEqual(get(self.srv, "/?set=1.tl&back=view%3Dcpu")[0], 302)

    def test_set_with_frag_keeps_every_check(self):
        self.assertEqual(get(self.srv, "/?set=tl&frag=1", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(get(self.srv, "/?set=junk&frag=1")[0], 400)
        self.assertEqual(get(self.locked, "/?set=tl&frag=1")[0], 401)
        self.assertEqual(get(self.srv, "/?set=tl&frag=1", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(get(self.srv, "/?set=tl&frag=1", "HEAD")[0], 204)

    def test_the_settings_page_has_the_copy_button(self):
        _, _, body = get(self.srv, "/?app=1&view=settings")
        (_, a, _), = Tree(body).find("button", data_copy="#export")
        self.assertEqual(a["type"], "button")
        self.assertTrue(Tree(body).find("pre", id="export"))


class Builder(unittest.TestCase):
    """The layout editor (`?edit=1`): its page, its links (`?set=e...`, one step on the server, no script needed), its script and its policy."""
    maxDiff = None
    EDIT = "/?app=1&edit=1"
    STEPS = ("data-earlier", "data-later", "data-shrink", "data-grow", "data-hide")

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.locked = serve(), serve("t" * 24)
        cls.expose = render.CFG["expose"]
        cls.avail = [c for c in prefs.CARDS if web.cards.enabled(c, render.CFG)]

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        render.CFG["expose"] = cls.expose
        for s in (cls.srv, cls.locked):
            s.shutdown()
            s.server_close()

    def page(self, path=None, cookie=""):
        st, h, body = get(self.srv, path or self.EDIT, headers={"Cookie": "nuc_ui=" + cookie} if cookie else None)
        self.assertEqual(st, 200)
        return h, body, Tree(body)

    @staticmethod
    def cards_of(tree):
        """[(id, classes)] of the articles of the grid, in order."""
        return [(a["data-card"], a.get("class", "").split()) for t, a, s in tree.tags if t == "article" and "data-card" in a]

    def follow(self, href, cookie=""):
        """Click a link of the page: the status, the Location and the cookie it sets (as the string the browser keeps)."""
        st, h, _ = get(self.srv, href, headers={"Cookie": "nuc_ui=" + cookie} if cookie else None)
        ck = h.get("Set-Cookie", "")
        return st, h.get("Location", ""), ck.split(";")[0][len("nuc_ui="):] if ck.startswith("nuc_ui=") else None

    # ---- the page
    def test_the_edit_page_is_the_overview_in_edit_mode(self):
        h, body, tree = self.page()
        (_, main, _), = tree.find("main", data_edit=True)
        self.assertEqual(main["class"], "grid")
        for attr in ("data-refresh", "data-frag", "data-paused"):
            self.assertNotIn(attr, main)  # it does not reload by itself: a page that moves under your hand is no editor
        self.assertNotIn("http-equiv", body)
        cards = self.cards_of(tree)
        self.assertEqual([c for c, _ in cards], [c for c, _ in prefs.visible_cards(prefs.effective(None, "")[0], self.avail)])  # the layout, never by severity
        for _, cls in cards:
            self.assertEqual(len([c for c in cls if re.fullmatch(r"s[1-4]", c)]), 1, cls)
        self.assertEqual(len(tree.find("article", tabindex=True)), 0)  # BUILDER_JS gives them a tab stop
        self.assertEqual(len(tree.find("section", id="help")), 1)
        self.assertEqual(len(tree.find("p", id="live", role="status")), 1)
        self.assertIn("aria-live", tree.find("p", id="live")[0][1])

    def test_every_card_has_its_buttons_as_links_to_the_server(self):
        _, body, tree = self.page()
        back = "app%3D1%26edit%3D1"
        for cid, _ in self.cards_of(tree):
            art = body[body.index('data-card="%s"' % cid):]
            art = art[:art.index("</article>")]
            ops = []
            for attr in self.STEPS:
                (_, a, _), = Tree(art).find("a", **{attr: True})
                self.assertRegex(a["href"], r"^/\?set=e[udsghw][a-z]{2}&back=%s$" % back, (cid, attr))
                self.assertNotIn("data-set", a)  # PREFS_JS must not take these over: BUILDER_JS does, or the server's own step
                self.assertTrue(a["aria-label"].endswith(": " + web.cards.CARDS[cid].title) or attr == "data-hide", a)
                self.assertEqual(prefs.edit_parts(a["href"][len("/?set="):].split("&")[0])[1], cid)
                ops.append(prefs.edit_parts(a["href"][len("/?set="):].split("&")[0])[0])
            self.assertEqual(ops, ["u", "d", "s", "g", "h"], cid)
            self.assertEqual(len(Tree(art).find("span", data_drag=True)), 1)
            self.assertEqual(len(Tree(art).find("span", data_size=True)), 1)
            self.assertIn("inert", Tree(art).find("div", **{"class": "cb"})[0][1])  # the body is a preview

    def test_the_bar_has_done_reset_and_says_the_order_is_fixed(self):
        _, body, tree = self.page()
        hrefs = {a.get("href") for t, a, s in tree.tags if t == "a"}
        self.assertIn("/?app=1", hrefs)  # done
        (_, reset, _), = [x for x in tree.find("a", data_set=True) if "ereset" in x[1]["href"]]
        self.assertEqual(reset["href"], "/?set=ereset&back=app%3D1%26edit%3D1")
        text = " ".join(tree.text)
        self.assertIn("the order is fixed", text)
        self.assertIn("instead of moving by severity", text)
        self.assertIn("Reset layout", text)
        self.assertIn("Every change is saved at once", text)
        self.assertIn('<a class="lnk" href="/?app=1">Done</a>', body)

    def test_hidden_cards_are_listed_dimmed_with_a_show_button(self):
        _, body, tree = self.page(cookie="1.lat2_ex2_dbx_wax_bo1_se1")
        cards = self.cards_of(tree)
        self.assertEqual([c for c, cls in cards if "off" in cls], ["webapps", "databases"])  # hidden: in the order of the sections, after the shown
        self.assertEqual([c for c, _ in cards][:4], ["attention", "exposure", "boot", "sessions"])
        self.assertEqual([c for c, _ in cards][-2:], ["webapps", "databases"])
        self.assertEqual(len(cards), len(self.avail))
        for cid in ("webapps", "databases"):
            art = body[body.index('data-card="%s"' % cid):]
            art = art[:art.index("</article>")]
            (_, a, _), = Tree(art).find("a", data_hide=True)
            self.assertEqual(prefs.edit_parts(a["href"][len("/?set="):].split("&")[0]), ("w", cid))  # a show button
            self.assertIn(">show<", art)
        shown = body[body.index('data-card="attention"'):]
        (_, a, _), = Tree(shown[:shown.index("</article>")]).find("a", data_hide=True)
        self.assertEqual(prefs.edit_parts(a["href"][len("/?set="):].split("&")[0]), ("h", "attention"))

    def test_the_page_shows_the_cookies_layout_and_widths(self):
        _, _, tree = self.page(cookie="1.lsy3_at1_ex4")
        self.assertEqual(self.cards_of(tree)[:3], [("system", ["card", "s3", "st-ok"]), ("attention", ["card", "s1", "st-err"]),
                                                    ("exposure", ["card", "s4", "st-err"])])

    def test_a_ui_parameter_does_not_change_what_is_edited(self):
        _, _, plain = self.page()
        _, _, other = self.page(self.EDIT + "&ui=1.lasy1_at1")
        self.assertEqual(self.cards_of(plain), self.cards_of(other))

    # ---- the steps
    def test_a_step_is_a_get_that_comes_back_to_the_editor(self):
        _, body, tree = self.page()
        href = [a["href"] for t, a, s in tree.tags if t == "a" and "data-later" in a and "ex" in a["href"]][0]
        self.assertEqual(href, "/?set=edex&back=app%3D1%26edit%3D1")
        st, loc, ck = self.follow(href)
        self.assertEqual((st, loc), (302, "/?app=1&edit=1"))
        got = prefs.parse_cookie(ck)
        self.assertEqual([c for c, _ in got["layout"]][:3], ["attention", "webapps", "exposure"])
        self.assertEqual(got["hidden"], [])
        h, _, tree = self.page(cookie=ck)
        self.assertEqual([c for c, _ in self.cards_of(tree)][:3], ["attention", "webapps", "exposure"])
        # and with the script's request: the same step, no redirect
        st, h, body = get(self.srv, href + "&frag=1")
        self.assertEqual((st, body), (204, ""))
        self.assertEqual(h["X-Nuc-Prefs"], ck)
        self.assertNotIn("Location", h)

    def test_a_run_of_steps_through_the_server_ends_where_the_pure_functions_say(self):
        ck, want = "", prefs.layout_of(prefs.effective(None, "")[0], self.avail)
        for op, card in (("d", "attention"), ("g", "webapps"), ("g", "webapps"), ("h", "boot"), ("u", "sessions"), ("s", "exposure"), ("w", "boot"), ("h", "disks")):
            st, loc, new = self.follow("/?set=%s&back=app%%3D1%%26edit%%3D1" % prefs.edit_field(op, card), ck)
            self.assertEqual(st, 302)
            ck = new
            f = {"u": lambda: prefs.move(want, card, -1), "d": lambda: prefs.move(want, card, 1), "s": lambda: prefs.resize(want, card, -1),
                 "g": lambda: prefs.resize(want, card, 1), "h": lambda: prefs.hide(want, card), "w": lambda: prefs.show(want, card)}[op]
            want = f()
            self.assertLessEqual(len(ck), prefs.COOKIE_MAX)
        got = prefs.parse_cookie(ck)
        self.assertEqual((got["layout"], got["hidden"]), (want["layout"], want["hidden"]))

    def test_steps_keep_the_other_preferences_and_the_script_may_send_a_whole_layout(self):
        st, loc, ck = self.follow("/?set=ehbo&back=app%3D1%26edit%3D1", "1.td.dw.of.kpb_in")
        got = prefs.parse_cookie(ck)
        self.assertEqual((got["theme"], got["density"], got["order"], got["kpis"]), ("dark", "wall", "fixed", ["problems", "internet"]))
        self.assertEqual(got["hidden"], ["boot"])
        layout = "l" + "_".join(prefs.CARD_CODES[c] + "2" for c in self.avail[:-1]) + "_" + prefs.CARD_CODES[self.avail[-1]] + "x"  # what BUILDER_JS writes
        st, h, _ = get(self.srv, "/?set=%s&frag=1" % layout, headers={"Cookie": "nuc_ui=1.td"})
        self.assertEqual(st, 204)
        got = prefs.parse_cookie(h["X-Nuc-Prefs"])
        self.assertEqual(([c for c, _ in got["layout"]], got["hidden"], got["theme"]), (self.avail[:-1], [self.avail[-1]], "dark"))
        self.assertLessEqual(len(h["X-Nuc-Prefs"]), prefs.COOKIE_MAX)

    def test_a_step_that_moves_nothing_does_not_fix_the_layout(self):
        for field in ("euat", "esfw", "ewat"):
            st, loc, ck = self.follow("/?set=%s&back=app%%3D1%%26edit%%3D1" % field)
            self.assertEqual((st, loc), (302, "/?app=1&edit=1"), field)
            self.assertTrue(ck is None or "l" not in ck, (field, ck))

    def test_reset_layout_drops_only_the_layout(self):
        st, loc, ck = self.follow("/?set=ereset&back=app%3D1%26edit%3D1", "1.td.pv.lat2_ex2_wax")
        self.assertEqual((st, loc, ck), (302, "/?app=1&edit=1", "1.td.pv"))
        st, h, _ = get(self.srv, "/?set=ereset&frag=1", headers={"Cookie": "nuc_ui=1.lat2"})
        self.assertEqual((st, h["X-Nuc-Prefs"]), (204, "1"))
        self.assertIn("Max-Age=0", h["Set-Cookie"])

    def test_bad_steps_are_refused_and_never_stored(self):
        for field in ("euzz", "exat", "euat1", "Euat", "ereset1", "eu", "eu.at", "e", "euat;x", "eeat", "ewat%00", "lzz1", "l", "lat5", "lat2_at2x"):
            st, h, _ = get(self.srv, "/?set=%s&back=app%%3D1" % field)
            self.assertIn(st, (302, 400), field)
            if st == 302:  # a legal field that changes nothing (lat2_at2x: the card once, hidden wins)
                self.assertNotIn("euat", field)
        for field in ("euzz", "exat", "euat1", "Euat", "ereset1", "eu", "eu.at", "e", "eeat", "lzz1", "lat5"):
            self.assertEqual(get(self.srv, "/?set=%s" % field)[0], 400, field)
        self.assertEqual(get(self.srv, "/?set=" + "e" * 300)[0], 400)
        self.assertEqual(get(self.srv, "/?set=euat", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)  # another site cannot move your cards
        self.assertEqual(get(self.locked, "/?set=euat")[0], 401)

    def test_a_hostile_cookie_does_not_break_a_step(self):
        for ck in ("<script>", "1.lzz", "1." + "l" * 300, "1.lat9", "é"):
            st, loc, new = self.follow("/?set=ehat&back=app%3D1%26edit%3D1", ck.encode("ascii", "ignore").decode())
            self.assertEqual(st, 302, ck)
            self.assertEqual(prefs.parse_cookie(new)["hidden"], ["attention"], ck)

    def test_the_cookie_never_passes_the_limit_through_the_server(self):
        ck = "1.tl.dw.vo.pv.os.kpb_in_la_be_dl_fw_cp_rm"
        rnd = __import__("random").Random(5)
        for _ in range(60):
            op, card = rnd.choice(list(prefs.EDIT_OPS)), rnd.choice(self.avail)
            st, loc, new = self.follow("/?set=%s&back=app%%3D1%%26edit%%3D1" % prefs.edit_field(op, card), ck)
            self.assertEqual(st, 302)
            ck = new or ck
            self.assertLessEqual(len(ck), prefs.COOKIE_MAX)
            self.assertTrue(prefs.parse_cookie(ck))

    # ---- the overview that follows
    def test_a_layout_of_your_own_keeps_its_order_instead_of_moving_by_severity(self):
        _, _, sev = self.page("/?app=1")
        _, _, mine = self.page("/?app=1", cookie="1.lsy2_ct1_at2")
        first = lambda t: [a["data-card"] for tag, a, s in t.tags if tag == "article"][:3]  # noqa: E731
        self.assertEqual(first(sev)[0], "attention")  # by severity: what needs you first
        self.assertEqual(first(mine)[:2], ["system", "containers"])  # fixed: as the reader put them
        self.assertEqual(first(self.page("/?app=1", cookie="1.tl")[2])[0], "attention")  # a cookie of another field: still by severity

    def test_the_footer_and_the_settings_lead_to_the_editor(self):
        _, _, tree = self.page("/?app=1")
        links = [a for t, a, s in tree.tags if t == "a" and a.get("href") == "/?app=1&edit=1"]
        self.assertEqual(len(links), 1)
        self.assertIn("Edit layout", " ".join(tree.text))
        _, _, tree = self.page("/?app=1&view=settings")
        self.assertEqual(len([a for t, a, s in tree.tags if t == "a" and a.get("href") == "/?app=1&edit=1"]), 1)
        _, body, _ = self.page("/?app=1&view=settings", cookie="1.lat2_ex1")
        self.assertIn("so for now the cards stay in its order", body)
        self.assertNotIn("so for now the cards stay in its order", self.page("/?app=1&view=settings")[1])
        _, _, tree = self.page("/?app=1&card=exposure")
        self.assertNotIn("Edit layout", " ".join(tree.text))  # one card in full: not an overview

    def test_the_export_shows_the_cookies_layout_and_hidden(self):
        _, body, tree = self.page("/?app=1&view=settings", cookie="1.lwa2_at1_dbx_bo1x")
        (_, _, _), = tree.find("pre", id="export")
        text = re.search(r'<pre id="export"[^>]*>(.*?)</pre>', body, re.S).group(1)
        self.assertIn("layout = webapps:2, attention\n", text)
        self.assertIn("hidden = databases, boot\n", text)

    def test_the_editor_is_only_where_it_makes_sense(self):
        for path in ("/?edit=1&app=0", "/?app=1&view=settings&edit=1", "/?app=1&view=cpu&edit=1", "/?view=map&edit=1"):
            self.assertFalse(self.page(path)[2].find("main", data_edit=True), path)
        self.assertTrue(self.page("/?app=1&edit=1&card=exposure")[2].find("main", data_edit=True))  # the editor wins over a single card
        _, body, tree = self.page("/?edit=1")  # [ui] web = classic: the editor is a shell page all the same
        self.assertTrue(tree.find("main", data_edit=True))
        self.assertIn('href="/s/app.', body)
        _, body, _ = self.page(self.EDIT + "&pause=1")
        self.assertNotIn('data-paused="1"', body)
        self.assertNotIn('data-key="Z"', body)  # nothing to pause
        self.assertEqual(get(self.locked, self.EDIT)[0], 401)

    def test_the_edit_page_has_its_own_help_and_footer(self):
        _, body, tree = self.page()
        for key in ("Space", "Esc"):
            self.assertIn(key, body[body.index('id="help"'):])
        self.assertIn("grab or drop a card", body)
        foot = body[body.index('<footer class="foot">'):]
        self.assertIn("Done", foot)
        self.assertNotIn("refresh every", foot)
        self.assertIn("A+", foot)

    # ---- the scripts and the policy
    def test_the_edit_page_carries_exactly_its_scripts_and_the_csp_lists_exactly_their_hashes(self):
        for path in (self.EDIT, "/?edit=1", self.EDIT + "&zoom=125", "/?app=1&edit=1&ui=1.tl"):
            st, h, body = get(self.srv, path)
            self.assertEqual(st, 200, path)
            want = [webjs.KEYS_JS, webjs.PREFS_JS, webjs.BUILDER_JS]
            self.assertEqual(inline_scripts(body), want, path)
            self.assertEqual(script_src(h["Content-Security-Policy"]), hashes(want), path)
            self.assertEqual(body.lower().count("<script"), 3, path)
            self.assertIsNone(re.search(r"\son[a-z]+=", body), path)
            parts = [p.strip() for p in h["Content-Security-Policy"].split(";")]
            for need in ("default-src 'none'", "connect-src 'self'", "form-action 'none'", "frame-ancestors 'none'", "base-uri 'none'"):
                self.assertIn(need, parts, path)
            self.assertNotIn("require-trusted-types-for 'script'", parts)  # no REFRESH_JS here: nothing is parsed from a string
            self.assertNotIn("trusted-types nuc-frag", parts)
            self.assertNotIn(webjs.csp_source(webjs.REFRESH_JS), h["Content-Security-Policy"])
            self.assertEqual(h["Content-Security-Policy"], web.page_csp(want, shell=True))

    def test_the_builder_script_is_on_the_edit_page_only(self):
        for path in ("/?app=1", "/?app=1&view=settings", "/?card=exposure", "/?app=1&view=map", "/?app=1&view=ai", "/?app=1&view=cpu", "/?app=1&pause=1",
                     "/?app=1&view=health", "/", "/?view=map&as=graph"):
            st, h, body = get(self.srv, path)
            self.assertNotIn(webjs.csp_source(webjs.BUILDER_JS), h["Content-Security-Policy"], path)
            self.assertNotIn(webjs.BUILDER_JS, body, path)
        for path in ("/?app=1", "/?app=1&view=settings"):
            self.assertIn(webjs.csp_source(webjs.REFRESH_JS), get(self.srv, path)[1]["Content-Security-Policy"])  # they keep refresh and preferences

    def test_the_edit_page_is_served_whole_even_for_a_fragment_request(self):
        st, h, body = get(self.srv, self.EDIT + "&frag=1")
        self.assertEqual(st, 200)
        self.assertNotIn("X-Nuc-Fragment", h)  # a fragment is for REFRESH_JS, which this page does not have
        self.assertTrue(Tree(body).find("main", data_edit=True))

    def test_the_markup_the_script_needs_is_what_the_server_renders(self):
        _, body, tree = self.page()
        js = webjs.BUILDER_JS
        for needle in ("main.grid[data-edit]", "article.card[data-card]", "[data-drag]", "[data-size]", "data-earlier", "data-later", "data-grow",
                       "data-shrink", "data-hide", "data-title", "data-grab"):
            self.assertIn(needle, js)
        self.assertTrue(tree.find("main", data_edit=True))
        self.assertEqual(len(tree.find("p", id="live")), 1)
        for t, a, s in tree.tags:
            if t == "article":
                self.assertIn("data-title", a)
                self.assertEqual(s[-1], "main")  # nothing else in the grid
        css = webcss.CSS
        for needle in (".grip{display:none", ".card[tabindex] .grip", ".ectl", ".card.off", ".card.drag", ".card[data-grab]", "touch-action:none", ".ebar"):
            self.assertIn(needle, css)


if __name__ == "__main__":
    unittest.main()
