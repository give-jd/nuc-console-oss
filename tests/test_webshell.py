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
                    for k in a:
                        unit.assertIn(k, webjs.FRAG_ATTRS, (tag, k))
                    if "href" in a:
                        unit.assertTrue(a["href"].startswith(("/?", "#")), a["href"])
                if tag not in ("polyline", "br", "hr", "input", "line", "circle", "path", "rect"):
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
        self.assertNotIn("http", css.replace("http-equiv", ""))  # nothing loads from anywhere

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
            if start[0] is not None and depth:
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
                    unit.assertIn(k, webjs.FRAG_ATTRS, (tag, k))
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


if __name__ == "__main__":
    unittest.main()
