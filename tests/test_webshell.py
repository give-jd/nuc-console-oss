"""Tests for the web shell (`?app=1`, `[ui] web = app`): its markup, /?set= and the nuc_ui cookie, /s/ assets, the settings page and the CSP.

The classic pages stay the default and unchanged (tests/golden/ locks them byte for byte).
"""
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
        self.assertIn('<meta http-equiv="refresh" content="2">', self.body)
        self.assertNotIn("<script", self.body.lower())

    def test_tabs_are_links_with_the_keys_and_help_and_settings(self):
        tabs = [a for t, a, s in self.tree.find("a") if "nav" in s]
        self.assertEqual([a["data-key"] for a in tabs], ["1", "2", "3", "4", "5"])
        self.assertEqual(sum(a.get("aria-current") == "page" for a in tabs), 1)
        self.assertTrue(self.tree.find("a", href="#help"))
        self.assertTrue(self.tree.find("a", href="/?app=1&view=settings"))

    def test_every_interactive_control_is_a_link_or_a_form(self):
        for t, a, _ in self.tree.tags:
            self.assertNotIn(t, ("button", "script", "select", "textarea", "iframe", "form"), t)
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
        self.assertEqual(csp, "default-src 'none'; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.assertEqual(self.headers["Vary"], "Cookie")
        self.assertEqual(self.headers["Cache-Control"], "no-store")

    def test_the_ai_page_keeps_its_forms_and_csrf(self):
        st, h, body = get(self.srv, "/?view=ai&app=1&sel=qwen3-4b")
        self.assertIn("form-action 'self'", h["Content-Security-Policy"])
        self.assertIn("style-src 'self' 'unsafe-inline'", h["Content-Security-Policy"])
        self.assertNotIn("script-src", h["Content-Security-Policy"])
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
            self.assertNotIn(tag, ("button", "form", "select", "script"))

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


if __name__ == "__main__":
    unittest.main()
