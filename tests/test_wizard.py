"""The first-run wizard (issue #56): five steps at /setup?step=N, each its own URL and plain links; step 4 writes only through the settings page's own guarded
actions (token, CSRF, Origin, the overlay's allowlist), or gives the lines to put in config.ini; a marker (setup.json) says it was finished or skipped, and
until then the shell offers a banner; the console has the text version of steps 1-3 (`render.py --view guide`, `?` twice, once on the first start)."""
import contextlib
import copy
import http.client
import io
import json
import os
import re
import shutil
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)
import nuc_config  # noqa: E402
import render  # noqa: E402
import webcss  # noqa: E402
import wizard  # noqa: E402
from test_web import get_any, serve  # noqa: E402

TOKEN = "t" * 24
SHIPPED = os.path.join(ROOT, "config", "config.ini")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def text(html_):
    """The words of a page: its tags gone, its entities as they are."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_))


class World(unittest.TestCase):
    """A config.ini, a settings folder and a marker, all in a temporary folder; a server in demo mode that reads them."""
    token = ""
    optin = False  # [web] settings_actions = yes and a token_file: the overlay is available (an installation)
    portable = False
    extra = ""

    @classmethod
    def setUpClass(cls):
        cls.saved = copy.deepcopy(render.CFG)

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        for k, v in cls.saved.items():
            old = render.CFG.get(k)
            if isinstance(old, dict) and isinstance(v, dict):
                old.clear()
                old.update(v)
            elif isinstance(old, list) and isinstance(v, list):
                old[:] = v
            else:
                render.CFG[k] = v

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.ini")
        shutil.copy(SHIPPED, self.path)
        self.folder = os.path.join(self.dir, "ai")
        os.mkdir(self.folder)
        self.ov, self.marker = os.path.join(self.folder, "settings.ini"), os.path.join(self.folder, "setup.json")
        stack = contextlib.ExitStack()
        env = {"NUC_CONSOLE_CONFIG": self.path, "NUC_CONSOLE_SETTINGS": self.ov, "NUC_CONSOLE_SETUP": self.marker}
        stack.enter_context(mock.patch.dict(os.environ, env))
        if self.portable:
            stack.enter_context(mock.patch.object(nuc_config, "PORTABLE", self.dir))
        self.addCleanup(stack.close)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n" + (("[web]\nsettings_actions = yes\ntoken_file = /nonexistent/token\n") if self.optin else "") + self.extra + "\n")
        with open(self.path, encoding="utf-8") as f:
            self.config_before = f.read()
        render.reload_config()
        self.srv = serve(self.token)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def get(self, path, headers=None):
        self.srv.cache.clear()
        h = dict(headers or {})
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return get_any(self.srv, path, "GET", h)

    def post(self, path, fields, csrf=True, origin=None, headers=None):
        data = dict(fields)
        if csrf:
            data["csrf"] = self.srv.csrf if csrf is True else csrf
        port = self.srv.server_address[1]
        h = {"Content-Type": "application/x-www-form-urlencoded", "Origin": origin or "http://127.0.0.1:%d" % port}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", path, urlencode(data), h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read().decode())
        c.close()
        self.srv.cache.clear()
        return out

    def step(self, n):
        st, h, body = self.get("/setup?step=%s" % n)
        self.assertEqual(st, 200, body[:200])
        return h, body


class Installation(World):
    """An installation whose config.ini does not opt in: the overlay is not available."""

    def test_each_step_renders_as_plain_links_and_says_what_is_on_now(self):
        for n in range(1, 6):
            h, body = self.step(n)
            self.assertIn("step %d of 5: %s" % (n, wizard.STEPS[n - 1]), body)
            self.assertIn('<meta name="viewport" content="width=device-width,initial-scale=1">', body)
            main = body.split("<main")[1].split("</main>")[0]
            self.assertNotIn("onclick", main)
            self.assertNotIn("javascript:", main)
            self.assertNotIn(" style=", body)
            for k in (k for k in range(1, 6) if k != n):  # the other steps are links to their own URLs; this one is the current (aria-current)
                self.assertIn('href="/setup?step=%d"' % k, main)
            self.assertIn('<li aria-current="step"><b>%d. %s</b></li>' % (n, wizard.STEPS[n - 1]), main)
            if n < 5:
                self.assertIn('href="/setup?step=%d">Next</a>' % (n + 1), main)
            self.assertIn('action="/setup/skip"', main)
            self.assertEqual(('action="/setup/finish"' in main), n == 5)
            self.assertIn("form-action 'self'", h["Content-Security-Policy"])
            self.assertNotIn("script-src 'unsafe", h["Content-Security-Policy"])

    def test_step_1_uses_the_real_states_and_the_key_figures_in_force(self):
        main = self.step(1)[1].split("<main")[1]
        for state, sym in __import__("ui").SYMBOLS.items():
            self.assertIn('<span class="sym s-%s" aria-hidden="true">%s</span> <b>%s</b>' % (state, sym, state), main)
        self.assertIn("never counted as fine", main)
        self.assertIn("cannot be read shows <b>?</b>", main)
        for label in wizard.kpi_names(render.CFG):
            self.assertIn(label, main)
        self.assertIn("ATTENTION", main)

    def test_step_1_says_when_the_row_is_switched_off(self):
        self.extra = ""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n[ui]\nkpis = none\n")
        render.reload_config()
        self.assertIn("kpis = none", text(self.step(1)[1]))

    def test_step_2_lists_the_screens_with_their_keys_and_links(self):
        main = self.step(2)[1].split("<main")[1]
        for key, name, href in ((1, "Overview", "/"), (2, "Map", "/?view=map"), (3, "CPU", "/?view=cpu"), (4, "Health", "/?view=health"), (5, "AI", "/?view=ai")):
            self.assertIn('<kbd>%d</kbd> <a class="lnk" href="%s">%s</a>' % (key, href, name), main)
        for href in ("/?view=settings", "/?view=telegram", "ui=1.dw&amp;kiosk=1"):
            self.assertIn(href, main)

    def test_a_screen_switched_off_is_said_off(self):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n[features]\nmap = no\n")
        render.reload_config()
        main = self.step(2)[1].split("<main")[1]
        self.assertIn("off: [features] map = no", main)
        self.assertIn("map", text(self.step(3)[1].split("Switched off now")[1]))

    def test_step_3_reads_the_effective_configuration(self):
        main = text(self.step(3)[1].split("<main")[1])
        self.assertIn("Web view ● on", main)  # it answers: it is on, whatever the file says
        self.assertIn("AI model ○ off", main)
        self.assertIn("Telegram ○ off", main)
        self.assertIn("Settings overlay ○ not available", main)
        self.assertIn("[web] settings_actions = yes", main)
        self.assertIn("Nothing is switched off now", main)
        self.assertNotIn("✔", main, "an on switch is not a tick: the wizard shows no green the data does not support")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n[telegram]\nenabled = yes\ndetail = full\n")
        render.reload_config()
        main = text(self.step(3)[1].split("<main")[1])
        self.assertIn("Telegram ● on", main)
        self.assertIn("because detail = full", main)

    def test_step_4_without_the_overlay_gives_the_lines_and_says_why(self):
        main = self.step(4)[1].split("<main")[1]
        self.assertNotIn('action="/settings/', main)
        self.assertIn("Why there is no form here", main)
        self.assertIn("off in config.ini: [web] settings_actions = yes turns it on", main)
        self.assertIn("sudoedit " + self.path, main)
        self.assertIn("[features]", main)
        self.assertIn("kpis = problems, cpu, ram, disk", main)
        self.assertIn("or: none", main)
        self.assertIn("density = desk", main)
        self.assertIn(render.CMD.get("apply") or render.CMD["restart"], main)

    def test_step_5_names_the_files_the_commands_and_who_can_change_what(self):
        main = text(self.step(5)[1].split("<main")[1])
        for needle in (self.path, self.ov, self.marker, "nuc-console-problems", "nuc-console-accept", "nuc-console-config migrate", "The administrator",
                       "the web view", "every key"):
            self.assertIn(needle, main, needle)
        self.assertIn("blob/main/docs/WEB.md", self.step(5)[1])

    def test_a_step_out_of_range_is_the_nearest_one(self):
        for raw, n in (("0", 1), ("-3", 1), ("9", 5), ("999999", 5), ("abc", 1), ("", 1), ("2.5", 1), ("%C2%B2", 1)):
            st, _h, body = self.get("/setup?step=" + raw)
            self.assertEqual(st, 200, raw)
            self.assertIn("step %d of 5" % n, body, raw)
        self.assertIn("step 1 of 5", self.get("/setup")[2])
        self.assertEqual(self.get("/?view=setup&step=3")[0], 200)
        self.assertEqual(self.get("/setup/nope")[0], 404)

    def test_the_marker_is_written_on_skip_and_on_finish_and_the_banner_goes(self):
        for act, how in (("skip", "skipped"), ("finish", "finished")):
            if os.path.exists(self.marker):
                os.remove(self.marker)
            self.assertEqual(wizard.state(), "")
            body = self.get("/")[2]
            self.assertIn("New here?", body)
            self.assertIn("Take the 2-minute tour", body)
            self.assertIn('action="/setup/skip"', body)
            self.assertNotIn("New here?", self.step(1)[1], "the guide itself carries no banner")
            self.assertNotIn("New here?", self.get("/?view=settings")[2])
            st, h, out = self.post("/setup/" + act, {})
            self.assertEqual((st, h["Location"]), (303, "/"), out)
            self.assertEqual(wizard.state(), how)
            self.assertNotIn("New here?", self.get("/")[2])
            self.assertEqual(json.load(open(self.marker))["how"], how)
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.config_before, "it writes no setting")

    def test_the_live_app_has_the_banner_and_the_link_and_the_dismissal_comes_back_to_it(self):
        st, h, body = self.get("/app")
        self.assertEqual(st, 200)
        self.assertIn("New here?", body)
        self.assertIn('href="/setup?step=1">setup guide</a>', body)
        self.assertIn("form-action 'self'", h["Content-Security-Policy"])
        self.assertIn('name="to" value="app"', body)
        st, h, _ = self.post("/setup/skip", {"to": "app"})
        self.assertEqual((st, h["Location"]), (303, "/app"))
        self.assertNotIn("New here?", self.get("/app")[2])

    def test_the_footer_and_the_settings_page_link_to_the_guide(self):
        self.assertIn('<a class="lnk" href="/setup?step=1">setup guide</a>', self.get("/?app=1")[2])
        settings = self.get("/?view=settings")[2]
        self.assertIn("Open the setup guide", settings)
        self.assertIn('href="/setup?step=1"', settings)

    def test_a_marker_that_cannot_be_written_only_brings_the_banner_back(self):
        os.rmdir(self.folder)
        st, h, _ = self.post("/setup/skip", {})
        self.assertEqual((st, h["Location"]), (303, "/"))
        self.assertEqual(wizard.state(), "")
        self.assertIn("New here?", self.get("/")[2])

    def test_the_setup_posts_obey_the_same_guard_as_the_settings_ones(self):
        for act in ("skip", "finish"):
            path = "/setup/" + act
            self.assertEqual(self.post(path, {}, csrf=False)[0], 403, "no CSRF token")
            self.assertEqual(self.post(path, {}, csrf="wrong")[0], 403, "a wrong token")
            self.assertEqual(self.post(path, {}, origin="http://evil.example")[0], 403, "another origin")
            self.assertEqual(self.post(path, {}, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
            self.assertEqual(self.post(path, {}, headers={"Content-Type": "text/plain"})[0], 415)
            self.assertEqual(get_any(self.srv, path)[0], 405, "GET does not write")
        self.assertEqual(self.post("/setup/other", {})[0], 404)
        self.assertEqual(wizard.state(), "", "none of them wrote the marker")

    def test_a_post_of_step_4_in_an_installation_without_the_overlay_is_refused(self):
        st, _h, out = self.post("/settings/config", {"section": "ui", "theme": "dark", "to": "setup"})
        self.assertEqual(st, 403, out)
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.config_before)
        self.assertFalse(os.path.exists(self.ov))


class Overlay(World):
    """An installation that opted in (settings_actions = yes and a token): step 4 has forms, and they are the settings page's own."""
    token = TOKEN
    optin = True

    def test_step_4_has_the_settings_pages_forms_and_returns_to_itself(self):
        main = self.step(4)[1].split("<main")[1]
        self.assertEqual(main.count('action="/settings/feature"'), len(nuc_config.FEATURES))
        self.assertEqual(main.count('action="/settings/config"'), 1)
        self.assertEqual(main.count('name="to" value="setup"'), len(nuc_config.FEATURES) + 1)
        for key in ("preset", "kpis", "theme", "density"):
            self.assertIn('id="k-ui-%s"' % key, main)
        self.assertNotIn("Why there is no form", main)
        self.assertIn("<i>set from this page</i>", main)
        self.assertIn("Settings overlay ● available", text(self.step(3)[1]))

    def test_a_save_goes_to_the_overlay_and_comes_back_to_step_4(self):
        st, h, out = self.post("/settings/config", {"section": "ui", "theme": "dark", "kpis": "none", "to": "setup"})
        self.assertEqual((st, h["Location"]), (303, "/setup?step=4"), out)
        self.assertEqual(nuc_config.read_overlay(self.ov), {"ui.theme": "dark", "ui.kpis": "none"})
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.config_before, "config.ini is untouched")
        main = self.step(4)[1].split("<main")[1]
        self.assertIn("Saved from this page", main)
        self.assertIn("set from this page", main.split('id="k-ui-theme"')[1].split("</div>")[0])
        self.assertEqual(wizard.kpi_names(render.CFG), [], "kpis = none: no row")

    def test_a_feature_can_be_switched_off_from_the_step_but_not_on_when_config_says_no(self):
        st, h, out = self.post("/settings/feature", {"name": "map", "on": "no", "to": "setup"})
        self.assertEqual((st, h["Location"]), (303, "/setup?step=4"), out)
        self.assertEqual(nuc_config.read_overlay(self.ov), {"features.map": "no"})
        self.assertFalse(render.CFG["features"]["map"])
        self.assertIn("off: [features] map = no", self.step(2)[1])
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n[features]\nthermal = no\n")
        render.reload_config()
        self.assertEqual(self.post("/settings/feature", {"name": "thermal", "on": "yes", "to": "setup"})[0], 400)

    def test_the_guard_is_the_settings_guard(self):
        body = {"section": "ui", "theme": "dark", "to": "setup"}
        self.assertEqual(self.post("/settings/config", body, csrf=False)[0], 403, "no CSRF token")
        self.assertEqual(self.post("/settings/config", body, origin="http://evil.example")[0], 403, "another origin")
        self.assertEqual(self.post("/settings/config", body, headers={"Authorization": "Bearer wrong"})[0], 401, "the token")
        self.assertEqual(self.post("/settings/config", body, headers={"Authorization": ""})[0], 401)
        self.assertFalse(os.path.exists(self.ov))

    def test_a_key_outside_the_allowlist_is_refused_whole(self):
        for section, form in (("web", {"bind": "0.0.0.0"}), ("telegram", {"enabled": "yes"}), ("ai", {"allow_remote": "yes"}),
                              ("display", {"mode": "none"}), ("features", {"map": "no"}), ("nope", {"x": "1"})):
            st, h, out = self.post("/settings/config", dict(form, section=section, to="setup"))
            self.assertIn(st, (303, 400), (section, out))
            self.assertEqual(h.get("Location", "/setup?step=4"), "/setup?step=4")
            self.assertFalse(os.path.exists(self.ov), section)
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.config_before)
        st, _h, out = self.post("/settings/feature", {"name": "nonsense", "on": "no", "to": "setup"})
        self.assertEqual(st, 400, out)

    def test_a_to_other_than_setup_does_not_redirect_anywhere_else(self):
        st, h, _ = self.post("/settings/config", {"section": "ui", "theme": "light", "to": "http://evil.example/"})
        self.assertEqual(st, 303)
        self.assertTrue(h["Location"].startswith("/?"), h["Location"])


class Portable(World):
    """A portable run (or the desktop app): config.ini is the account's own, and step 4 writes it like the settings page."""
    portable = True

    def test_step_4_writes_config_ini_through_the_settings_actions(self):
        main = self.step(4)[1].split("<main")[1]
        self.assertEqual(main.count('action="/settings/feature"'), len(nuc_config.FEATURES))
        self.assertIn("This is a portable run", main)
        self.assertIn("Settings overlay ● writes config.ini", text(self.step(3)[1]))
        st, h, out = self.post("/settings/config", {"section": "ui", "preset": "server", "to": "setup"})
        self.assertEqual((st, h["Location"]), (303, "/setup?step=4"), out)
        self.assertRegex(open(self.path, encoding="utf-8").read(), r"(?m)^preset\s*=\s*server\b")
        st, h, out = self.post("/settings/feature", {"name": "health", "on": "no", "to": "setup"})
        self.assertEqual((st, h["Location"]), (303, "/setup?step=4"), out)
        self.assertRegex(open(self.path, encoding="utf-8").read(), r"(?m)^health\s*=\s*no\b")

    def test_the_marker_goes_in_the_portable_folder(self):
        os.environ.pop("NUC_CONSOLE_SETUP")
        self.assertEqual(wizard.marker_path(), os.path.join(self.dir, "ai", "setup.json"))
        shutil.rmtree(self.folder)
        self.assertTrue(wizard.mark("console"), "a portable run makes its own folder")
        self.assertEqual(wizard.state(), "console")


class Layout(unittest.TestCase):
    def test_the_page_is_made_for_a_phone(self):
        css = webcss.WIZARD
        self.assertNotRegex(css, r"(?<![a-z-])(min-|max-)?width:\s*\d+(\.\d+)?(px|em|rem)\b(?<!max-width)", "no fixed width")
        for rule in (".wz-list li{display:flex;flex-wrap:wrap", ".wz-nav,.wz-banner{display:flex;flex-wrap:wrap", "overflow-x:auto", "min-height:2.6em"):
            self.assertIn(rule, css)

    def test_the_html_has_no_fixed_width_and_no_inline_style(self):
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_SETUP": os.path.join(hermetic.SCRATCH, "none.json")}):
            for n in range(1, 6):
                body = wizard.step_html(n, render.CFG, wizard.facts(render.CFG), {"path": "/etc/nuc-console/config.ini", "overlay": "", "marker": "m",
                                                                                   "apply": "restart", "note": None}, "tok")
                self.assertNotRegex(body, r"\b(style|width|height)=", n)
                self.assertNotRegex(body, r"\d+px")
            self.assertNotRegex(wizard.banner_html("tok"), r"\b(style|width)=")


class Marker(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "setup.json")

    def test_state_and_mark(self):
        self.assertEqual(wizard.state(self.path), "")
        for how in wizard.MARKS:
            self.assertTrue(wizard.mark(how, self.path))
            self.assertEqual(wizard.state(self.path), how)
        with self.assertRaises(ValueError):
            wizard.mark("done", self.path)

    def test_a_marker_that_is_not_one_counts_for_nothing(self):
        for body in ("", "not json", "[]", '{"how": "other"}', '{"how": 3}', "x" * (wizard.MARKER_MAX + 1)):
            with open(self.path, "w") as f:
                f.write(body)
            self.assertEqual(wizard.state(self.path), "", body[:20])

    @unittest.skipIf(not hasattr(os, "mkfifo"), "fifo")
    def test_a_fifo_in_the_place_of_the_marker_does_not_block(self):
        os.mkfifo(self.path)
        import signal
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(AssertionError("state() blocked on a FIFO")))
        signal.alarm(5)
        try:
            self.assertEqual(wizard.state(self.path), "")
        finally:
            signal.alarm(0)

    @unittest.skipIf(os.name != "posix", "links")
    def test_it_is_never_written_or_read_through_a_link(self):
        target = os.path.join(self.dir, "target")
        os.symlink(target, self.path)
        self.assertEqual(wizard.state(self.path), "")
        self.assertTrue(wizard.mark("skipped", self.path), "a link in the file's place is replaced, not followed")
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.islink(self.path))
        other = os.path.join(self.dir, "elsewhere")
        os.mkdir(other)
        link = os.path.join(self.dir, "folder")
        os.symlink(other, link)
        self.assertFalse(wizard.mark("skipped", os.path.join(link, "setup.json")), "a link as the folder")
        self.assertFalse(wizard.mark("skipped", os.path.join(self.dir, "missing", "setup.json")), "no folder")

    def test_clamp(self):
        self.assertEqual([wizard.clamp(x) for x in (None, "", "x", 0, 1, 3, 5, 6, -1, "4")], [1, 1, 1, 1, 1, 3, 5, 5, 1, 4])


class Console(unittest.TestCase):
    def lines(self, box):
        return [ANSI.sub("", x) for x in box]

    def test_the_guide_box_shows_the_whole_guide_at_80x24_and_120x33(self):
        for cols, rows in ((80, 24), (120, 33)):
            box = self.lines(render.guide_box(cols - 1, rows))  # the loop draws one column less
            self.assertLessEqual(len(box), rows - 3, (cols, rows))
            self.assertEqual({len(x) for x in box}, {len(box[0])}, "lines of equal width")
            self.assertLessEqual(len(box[0]), cols - 1)
            inside = [x[2:-2].rstrip() for x in box[1:-1]]
            self.assertEqual(inside, wizard.guide_lines(render.CFG, wizard.GUIDE_W), "nothing is cut or left out")
            self.assertFalse(any("more" in x for x in inside))

    def test_a_console_too_small_for_it_says_how_much_is_left(self):
        box = self.lines(render.guide_box(39, 10))
        self.assertLessEqual(len(box), 10 - 2)
        self.assertTrue(any("more: --view guide" in x for x in box))
        self.assertEqual(len({len(x) for x in box}), 1)

    def test_view_guide_prints_the_text_version(self):
        for cols, rows in ((80, 24), (120, 33)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                render.once(["--once", "--view", "guide", "--cols", str(cols), "--rows", str(rows)])
            lines = out.getvalue().split("\n")
            self.assertTrue(lines[0].startswith("nuc-console: setup guide"))
            self.assertLessEqual(max(len(x) for x in lines), cols)
            self.assertLessEqual(len(lines), rows)
            body = "\n".join(lines)
            for needle in ("HOW TO READ IT", "SCREENS  1 Overview  2 Map  3 CPU  4 Health  5 AI", "ON AND OFF NOW", "/setup"):
                self.assertIn(needle, body)

    def test_the_guide_tells_the_configuration_in_force(self):
        cfg = copy.deepcopy(render.CFG)
        cfg["telegram"].update(enabled=True, detail="full")
        cfg["features"]["map"] = cfg["features"]["ai"] = False
        cfg["web"]["enabled"], cfg["web"]["port"] = True, 8787
        body = "\n".join(wizard.guide_lines(cfg))
        self.assertIn("web view on (port 8787)", body)
        self.assertIn("Telegram on (full detail)", body)
        self.assertIn("Sections switched off: 2 (map, ai)", body)
        self.assertIn("SCREENS  1 Overview  3 CPU  4 Health", body)
        self.assertNotIn("✔ on", body)
        self.assertEqual(max(len(x) for x in wizard.guide_lines(cfg)), max(len(x) for x in wizard.guide_lines(cfg, wizard.GUIDE_W)))

    def test_the_help_points_at_the_guide(self):
        box = "\n".join(self.lines(render.help_box("overview", 100, 40, lambda f: True)))
        self.assertIn("any key closes this help \u00b7 ? again: the guide", box)

    def test_the_key_handling_stays_inside_the_existing_loop(self):
        src = open(os.path.join(ROOT, "src", "render.py"), encoding="utf-8").read()
        self.assertIn('help_open, guide_open = False, k == "?"', src)
        self.assertIn("guide_open = bool(old or win_keys) and not wizard.state()", src, "the first start only, and only with a keyboard")


class Docs(unittest.TestCase):
    def test_the_docs_know_the_wizard(self):
        read = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        self.assertIn("/setup", read("docs", "WEB.md"))
        self.assertIn("WIZARD.md", read("README.md"))
        self.assertIn("setup guide", read("CHANGELOG.md").split("## [", 2)[1].lower())
        guide = read("docs", "WIZARD.md")
        for link in ("WEB.md", "CONFIGURATION.md", "SECURITY.md"):
            self.assertIn(link, guide)


if __name__ == "__main__":
    unittest.main()
