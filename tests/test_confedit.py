"""The config.ini block of the settings page: the schema of every key (src/confedit.py) against config/config.ini and nuc_config.load(),
the checks a value passes before it is written, the write itself (nuc_config.set_keys / edit_lines: one section, every other line as it
was), and the page with its POST /settings/config in a portable run (the desktop app is one); an installation's page only shows."""
import configparser
import contextlib
import copy
import http.client
import io
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
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never the host's config.ini
import confedit  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
from test_web import get_any as get, serve  # noqa: E402

SHIPPED = os.path.join(ROOT, "config", "config.ini")
DEFAULTS = nuc_config.load("/nonexistent")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def samples(key):
    """Values the page can send for a key, each with what config.ini then holds."""
    if key.kind == confedit.BOOL:
        return [("yes", "yes"), ("no", "no"), ("YES", "yes")]
    if key.kind == confedit.CHOICE:
        return [(c, c) for c in key.choices] + ([("", "")] if key.unset else [])
    if key.kind == confedit.INT:
        out = [(str(key.lo), str(key.lo)), (str(key.hi), str(key.hi)), (" %d " % key.hi, str(key.hi))]
        return out + ([("0", "0")] if key.zero else [])
    return {
        ("dashboard", "sections"): [("disks, attention", "disks, attention, exposure, webapps, firewall, system, containers, databases, boot, "
                                                         "network_traffic, sessions, tailscale, docker_disk")],
        ("ui", "kpis"): [("problems, cpu", "problems, cpu"), ("", "")],
        ("ui", "layout"): [("attention:2, disks", "attention:2, disks"), ("", "")],
        ("ui", "hidden"): [("sessions", "sessions"), (confedit.NOTHING, confedit.NOTHING), ("", "")],
        ("display", "browser"): [("auto", "auto"), ("/opt/Example Browser/browser", "/opt/Example Browser/browser")],
        ("web", "bind"): [("192.0.2.10", "192.0.2.10")],
        ("web", "token_file"): [("/etc/nuc-console/token", "/etc/nuc-console/token"), ("", "")],
        ("web", "allowed_hosts"): [("Nuc.Example.lan,b.example.ts.net", "nuc.example.lan, b.example.ts.net"), ("", "")],
        ("telegram", "username"): [("@Some_User", "some_user"), ("", "")],
        ("ai", "endpoint"): [("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1")],
        ("ai", "model"): [("qwen3-8b", "qwen3-8b"), ("hf.co/example/repo:Q4_K_M", "hf.co/example/repo:Q4_K_M"), ("", "")],
    }[(key.section, key.name)]


class Schema(unittest.TestCase):
    def test_the_schema_names_every_key_of_the_shipped_file_and_its_sections(self):
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"), strict=False)
        cp.read(SHIPPED, encoding="utf-8")
        shipped = {(s, k) for s in cp.sections() for k in cp.options(s)}
        self.assertEqual(shipped - set(confedit.KEY), set(), "a key of config.ini the page does not know")
        text = read(SHIPPED)
        for key in confedit.KEYS:
            if (key.section, key.name) not in shipped:  # [ui] and [dashboard] sections: there, commented out, with their default
                self.assertIn((key.section, key.name), [(k.section, k.name) for k in confedit.BY_SECTION["ui"]] + [("dashboard", "sections")])
                self.assertRegex(text, r"(?m)^# %s = " % key.name)
        self.assertEqual(set(cp.sections()), set(confedit.TITLES) | {"features"})
        self.assertEqual([s for s, _t, _w in confedit.SECTIONS][-2:], list(confedit.MAPS))

    def test_the_shipped_file_stays_light_and_every_key_is_documented(self):
        lines = read(SHIPPED).splitlines()
        self.assertLessEqual(len(lines), 115, "config.ini fits a screen or two: long text goes in docs/CONFIGURATION.md and confedit.KEYS")
        self.assertEqual([n + 1 for n, ln in enumerate(lines) if len(ln) > 120], [], "lines over 120 characters")
        doc = read(os.path.join(ROOT, "docs", "CONFIGURATION.md"))
        self.assertEqual([k.name for k in confedit.KEYS if "`%s`" % k.name not in doc], [], "a key CONFIGURATION.md does not name")

    def test_the_defaults_the_page_shows_are_the_ones_load_uses(self):
        for key in confedit.KEYS:
            if key.section != "ui":
                self.assertEqual(confedit.value(DEFAULTS, key), key.default, (key.section, key.name))
            else:
                self.assertEqual(confedit.value(DEFAULTS, key), "" if key.unset else key.default, key.name)
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"), strict=False)
        cp.read(SHIPPED, encoding="utf-8")
        for (sec, name), key in confedit.KEY.items():
            if cp.has_option(sec, name):
                self.assertEqual(cp.get(sec, name).strip(), key.default, "config.ini writes the default (%s %s)" % (sec, name))

    def test_every_key_says_what_it_does_and_when_it_applies(self):
        for key in confedit.KEYS:
            self.assertIn(key.applies, confedit.APPLIES)
            self.assertGreater(len(key.what), 20, key.name)
            self.assertTrue(key.what.isascii() or "…" in key.what, key.name)
        self.assertEqual(sorted(k for k, _t, _w in confedit.FEATURE_WORDS), sorted(nuc_config.FEATURES))
        locks = {(k.section, k.name) for k in confedit.KEYS if k.applies == confedit.LOCK}
        self.assertEqual(locks, {("ai", "web_actions"), ("telegram", "web_actions"), ("ai", "allow_remote"), ("web", "settings_actions")})


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.ini")
        shutil.copy(SHIPPED, self.path)

    def save(self, section, **fields):
        return confedit.save(self.path, section, {k: [v] for k, v in fields.items()})


class Values(Folder):
    def test_every_value_the_page_offers_reads_back_as_itself_and_touches_one_line(self):
        for key in confedit.KEYS:
            if not key.editable() or key.section == "features":
                continue
            for sent, kept in samples(key):
                with self.subTest(key=(key.section, key.name), sent=sent):
                    shutil.copy(SHIPPED, self.path)
                    before = read(self.path).splitlines()
                    names = self.save(key.section, **{key.name: sent})
                    self.assertEqual(confedit.value(nuc_config.load(self.path), key), kept)
                    after = read(self.path).splitlines()
                    if names:
                        self.assertEqual(names, [key.name])
                        self.assertLessEqual(abs(len(after) - len(before)), 1)
                        self.assertLessEqual(len([x for x in after if x not in before]), 1)
                    else:
                        self.assertEqual(after, before, "the value it had: nothing written")
                    self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_bad_values_are_refused_with_their_reason_and_nothing_is_written(self):
        cases = [
            ("dashboard", {"rotate_seconds": "2"}, "rotate_seconds: a whole number, 3-600"),
            ("dashboard", {"columns": "39"}, "0 (automatic) or 40-500"),
            ("dashboard", {"refresh_seconds": "1.5"}, "refresh_seconds"),
            ("dashboard", {"mode": "carousel"}, "mode: one of overview, rotate"),
            ("dashboard", {"details": "maybe"}, "details: yes or no"),
            ("dashboard", {"sections": "attention, nowhere"}, "unknown name nowhere"),
            ("ai", {"model": "a#b"}, "without # or ;"),
            ("ai", {"model": "two words"}, "model: not a value it takes"),
            ("ai", {"endpoint": "ftp://192.0.2.1/"}, "endpoint"),
            ("ai", {"endpoint": ""}, "it cannot be empty"),
            ("telegram", {"username": "abc"}, "username"),
            ("telegram", {"detail": "everything"}, "titles, full"),
            ("display", {"zoom": "300"}, "50-200"),
            ("web", {"allowed_hosts": "bad host!"}, "host names"),
            ("ui", {"kpis": "problems, bogus"}, "kpis: unknown name 'bogus'"),
            ("ui", {"kpis": "problems, internet, lan, beyond, cpu, ram, disk, temp, load"}, "at most 8"),
            ("ui", {"layout": "attention:9"}, "not a width"),
            ("ui", {"theme": "neon"}, "theme: one of"),
            ("webapps", {"text": "shop = 80, 99999"}, "1 to 65535"),
            ("webapps", {"text": "shop 80"}, "write name = value"),
            ("webapps", {"text": "a = 80\nA = 81"}, "twice"),
            ("expose", {"text": ":8080 = lan"}, "a name is"),
            ("expose", {"text": "70000 = lan"}, "a port is 1-65535"),
            ("expose", {"text": "shop-db = everywhere"}, "local, tailnet, lan or internet"),
            ("expose", {"text": "[ai] = lan"}, "a name is"),
        ]
        for section, fields, why in cases:
            with self.subTest(section=section, fields=fields):
                with self.assertRaises(confedit.Refused) as e:
                    self.save(section, **fields)
                self.assertIn(why, "; ".join(e.exception.reasons))
                self.assertEqual(read(self.path), read(SHIPPED))
                self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_every_reason_at_once(self):
        with self.assertRaises(confedit.Refused) as e:
            self.save("dashboard", rotate_seconds="1", overview_seconds="9", mode="overview")
        self.assertEqual(len(e.exception.reasons), 2)

    def test_the_locks_and_an_installations_keys_are_not_the_pages_to_write(self):
        self.assertEqual(self.save("ai", allow_remote="yes", web_actions="no", timeout_s="120"), [])
        self.assertEqual(self.save("telegram", web_actions="no"), [])
        self.assertEqual(self.save("web", bind="0.0.0.0", enabled="yes", port="80", token_file="/x"), [])
        self.assertEqual(self.save("display", mode="none", browser="/x"), [])
        self.assertEqual(read(self.path), read(SHIPPED))
        with self.assertRaises(confedit.Refused):
            self.save("features", health="no")
        with self.assertRaises(confedit.Refused):
            self.save("nowhere", x="1")

    def test_a_section_keeps_its_comments_and_the_rest_of_the_file(self):
        self.assertEqual(self.save("ai", enabled="yes", timeout_s="300", model="qwen3-8b", gpu="auto"), ["enabled", "model", "timeout_s"])
        cfg = nuc_config.load(self.path)
        self.assertEqual((cfg["ai"]["enabled"], cfg["ai"]["timeout_s"], cfg["ai"]["model"]), (True, 300, "qwen3-8b"))
        before, after = read(SHIPPED).splitlines(), read(self.path).splitlines()
        self.assertEqual(len(before), len(after))
        self.assertEqual(sum(a != b for a, b in zip(before, after)), 3)
        self.assertEqual([ln for ln in after if ln.startswith("#")], [ln for ln in before if ln.startswith("#")])

    def test_ui_keys_are_added_and_removed_and_none_means_nothing_hidden(self):
        self.assertEqual(self.save("ui", theme="dark", hidden=confedit.NOTHING, kpis="problems, ai"), ["theme", "kpis", "hidden"])
        ui = nuc_config.load(self.path)["ui"]
        self.assertEqual((ui["theme"], ui["hidden"], ui["kpis"]), ("dark", [], ["problems", "ai"]))
        self.assertIn("\nhidden =\n", read(self.path))
        self.assertEqual(self.save("ui", theme="", hidden="", kpis="problems, ai"), ["theme", "hidden"])
        ui = nuc_config.load(self.path)["ui"]
        self.assertNotIn("theme", ui)
        self.assertNotIn("hidden", ui)
        self.assertNotIn("\ntheme =", read(self.path))
        self.assertIn("# theme = auto", read(self.path), "the comment stays")

    def test_the_lists_of_webapps_and_expose(self):
        self.assertEqual(self.save("webapps", text="shop-web = 8080; 8443\n\napi = 3000\n"), ["api", "shop-web"])
        self.assertEqual(nuc_config.load(self.path)["webapps"], {"shop-web": [8080, 8443], "api": [3000]})
        self.assertEqual(self.save("webapps", text="api = 3000"), ["shop-web"], "a line taken away is a key removed")
        self.assertEqual(nuc_config.load(self.path)["webapps"], {"api": [3000]})
        self.assertIn("# ethibid = 8180, 8543", read(self.path))
        self.assertEqual(self.save("expose", text="shop-db = localhost\n8080/udp = public\nN8N = tailnet"), ["8080/udp", "n8n", "shop-db"])
        self.assertEqual(nuc_config.load(self.path)["expose"], {"shop-db": "LOCALE", "8080/udp": "INTERNET", "n8n": "TAILNET"})
        self.assertIn("shop-db = local\n", read(self.path))
        self.assertEqual(confedit.map_text(nuc_config.load(self.path), "expose"), "8080/udp = internet\nn8n = tailnet\nshop-db = local")
        self.assertEqual(self.save("expose", text=""), ["8080/udp", "n8n", "shop-db"])
        self.assertEqual(nuc_config.load(self.path)["expose"], {})

    def test_a_file_that_cannot_be_read_is_not_edited(self):
        with open(self.path, "a") as f:
            f.write("\n[broken\n")
        with self.assertRaises(confedit.Refused) as e, contextlib.redirect_stderr(io.StringIO()):
            self.save("ai", timeout_s="300")
        self.assertIn("fix it by hand first", e.exception.reasons[0])

    def test_a_complaint_of_load_about_the_section_refuses_it_and_an_old_one_does_not(self):
        with open(self.path, "a") as f:
            f.write("\n[extra]\n")
        nuc_config.set_key(self.path, "ai", "colour", "blue")  # load() says nothing of unknown [ai] keys: it is no complaint
        nuc_config.set_key(self.path, "ui", "colour", "blue")  # [ui] does: an old complaint, it does not stop a save
        self.assertEqual(self.save("ui", theme="light"), ["theme"])
        checker = mock.Mock(side_effect=lambda path, warn=None: nuc_config.load(path, warn=warn))
        self.assertEqual(confedit.save(self.path, "ai", {"timeout_s": ["200"]}, load=checker), ["timeout_s"])
        self.assertEqual(checker.call_count, 2, "the old file, then the new one before it replaces it")


class Lines(unittest.TestCase):
    def test_edit_lines(self):
        lines = ["# head", "[a]", "x = 1  # one", "; y = 2", "Y=3", "", "[b]", "z = 4", "z = 5", ""]
        out = nuc_config.edit_lines(lines, "a", [("x", "10"), ("y", ""), ("new", "n")])
        self.assertEqual(out, ["# head", "[a]", "x = 10 # one", "; y = 2", "Y=", "new = n", "", "[b]", "z = 4", "z = 5", ""])
        out = nuc_config.edit_lines(lines, "B", [("z", "6")], drop=["x"])
        self.assertEqual(out[7:9], ["z = 6", "z = 6"], "every line of a key: configparser keeps the last")
        self.assertEqual(nuc_config.edit_lines(lines, "a", [], drop=["x", "y"]), ["# head", "[a]", "; y = 2", "", "[b]", "z = 4", "z = 5", ""])
        self.assertEqual(nuc_config.edit_lines(lines, "c", [("k", "v")])[-3:], ["", "[c]", "k = v"])
        self.assertEqual(nuc_config.edit_lines(lines, "c", [], drop=["k"]), lines, "nothing to add: no empty section")

    def test_load_gives_its_complaints_to_whoever_asks(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.ini")
            with open(path, "w") as f:
                f.write("[dashboard]\nmode = carousel\n[ui]\ntheme = neon\n")
            said = []
            with contextlib.redirect_stderr(io.StringIO()) as err:
                nuc_config.load(path, warn=said.append)
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(len(said), 2)
            self.assertTrue(all(x.startswith("nuc-console: %s: [" % path) for x in said), said)


class Page(Folder):
    @classmethod
    def setUpClass(cls):
        cls.srv = serve()

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Folder.setUp(self)
        saved = copy.deepcopy(render.CFG)
        consts = (render.MODE, render.ROTATE_S, render.REFRESH_S)
        cfg, zoom = dict(self.srv.cfg), self.srv.zoom

        def restore():
            for k in list(render.CFG):
                if k not in saved:
                    del render.CFG[k]
            for k, v in saved.items():
                old = render.CFG.get(k)
                if isinstance(old, dict):
                    old.clear()
                    old.update(v)
                elif isinstance(old, list):
                    old[:] = v
                else:
                    render.CFG[k] = v
            render.MODE, render.ROTATE_S, render.REFRESH_S = consts
            self.srv.cfg.clear()
            self.srv.cfg.update(cfg)
            self.srv.zoom, self.srv.config_note = zoom, None
        self.addCleanup(restore)
        self.srv.cache.clear()

    def portable(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(nuc_config, "PORTABLE", self.dir))
        stack.enter_context(mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": self.path}))
        self.addCleanup(stack.close)

    def post(self, fields, csrf=True):
        data = dict(fields)
        if csrf:
            data["csrf"] = self.srv.csrf
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("POST", "/settings/config", urlencode(data), {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read().decode())
        c.close()
        return out

    def settings(self):
        self.srv.cache.clear()
        st, _h, body = get(self.srv, "/?view=settings")
        self.assertEqual(st, 200)
        return body.split('id="config"')[1].split("</section>")[0]

    def test_an_installation_shows_every_key_and_what_it_does_and_takes_no_post(self):
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": self.path}):
            body = self.settings()
            self.assertNotIn("<form", body)
            self.assertIn("Read-only here (the settings folder is not writable", body)  # no settings folder this account can write: the overlay is unavailable
            for key in confedit.KEYS:
                if key.section != "features":
                    self.assertIn('id="k-%s-%s"' % (key.section, key.name), body)
            for sec, title, _w in confedit.SECTIONS:
                self.assertIn('<details class="cfgs" id="cfg-%s"><summary><code>[%s]</code>' % (sec, sec), body)
            self.assertNotIn("used by an installation only", body, "there every key is applied by the restart the header names")
            self.assertIn("a lock: only config.ini changes it", body)
            self.assertNotIn("<textarea", body)
            self.assertNotIn('<span class="sm"></span>', body)
            st, _h, text = self.post({"section": "ai", "timeout_s": "300"})
        self.assertEqual(st, 403, text)
        self.assertEqual(read(self.path), read(SHIPPED))

    def test_a_portable_run_saves_a_section_and_the_pages_follow_at_once(self):
        self.portable()
        body = self.settings()
        self.assertEqual(body.count('action="/settings/config"'), len(confedit.SECTIONS))
        self.assertIn('<select id="f-dashboard-mode" name="mode">', body)
        self.assertIn('<input id="f-dashboard-refresh_seconds" name="refresh_seconds" type="number" min="1" max="10"', body)
        self.assertIn('<input id="f-dashboard-columns" name="columns" type="number" min="0" max="500"', body)
        self.assertIn('<textarea class="cmap" id="f-expose" name="text"', body)
        lock = body.split('id="k-ai-allow_remote"')[1].split("</div>")[0]
        self.assertNotIn("<select", lock)
        self.assertIn("only config.ini changes it", lock)
        self.assertNotIn('name="bind"', body, "an installation's key: shown, not offered")
        st, h, text = self.post({"section": "dashboard", "refresh_seconds": "5", "rotate_seconds": "30", "back": "view=settings"})
        self.assertEqual(st, 303, text)
        self.assertTrue(h["Location"].endswith("view=settings#cfg-dashboard"), h["Location"])
        self.assertEqual(nuc_config.load(self.path)["refresh_seconds"], 5)
        self.assertEqual((render.CFG["refresh_seconds"], render.ROTATE_S, self.srv.cfg["refresh_seconds"]), (5, 30, 5))
        body = self.settings()
        self.assertIn('<details class="cfgs" id="cfg-dashboard" open>', body)
        self.assertIn("Saved in config.ini: rotate_seconds, refresh_seconds.", body)
        self.assertIn("rotate_seconds, refresh_seconds: applies at once.", body)
        self.assertIn('name="refresh_seconds" type="number" min="1" max="10" step="1" value="5"', body)

    def test_a_refused_post_says_why_and_keeps_what_was_typed(self):
        self.portable()
        st, h, _t = self.post({"section": "ai", "timeout_s": "5", "model": "qwen3-8b", "back": ""})
        self.assertEqual(st, 303)
        self.assertTrue(h["Location"].endswith("#cfg-ai"))
        body = self.settings()
        self.assertIn('<details class="cfgs" id="cfg-ai" open>', body)
        self.assertIn("Not saved, nothing changed:<br>[ai] timeout_s: a whole number, 10-600", body)
        self.assertIn('name="timeout_s" type="number" min="10" max="600" step="1" value="5"', body)
        self.assertIn('name="model" type="text"', body)
        self.assertRegex(body, r'name="model" type="text"[^>]*value="qwen3-8b"')
        self.assertEqual(read(self.path), read(SHIPPED))
        self.assertEqual(render.CFG["ai"]["timeout_s"], 120)

    def test_the_web_views_own_figures_follow_and_a_start_key_says_how_to_restart(self):
        self.portable()
        self.post({"section": "web", "columns": "120", "allowed_hosts": "nuc.example.lan"})
        self.assertEqual(self.srv.cfg["columns"], 120)
        self.assertIn("allowed_hosts: applies at the next start.", self.settings())
        self.post({"section": "display", "zoom": "150"})
        self.assertEqual(self.srv.zoom, 150)

    def test_what_a_post_must_be(self):
        self.portable()
        self.assertEqual(self.post({"section": "ai", "timeout_s": "300"}, csrf=False)[0], 403)
        self.assertEqual(self.post({"section": "nowhere"})[0], 400)
        self.assertEqual(self.post({"timeout_s": "300"})[0], 400)
        self.assertEqual(read(self.path), read(SHIPPED))
        st, h, _b = get(self.srv, "/settings/config")
        self.assertEqual((st, h.get("Allow")), (405, "POST"))

    def test_a_file_that_cannot_be_written_is_said_not_hidden(self):
        self.portable()
        with mock.patch.object(nuc_config, "set_keys", side_effect=OSError("disk full")), contextlib.redirect_stderr(io.StringIO()):
            st, _h, text = self.post({"section": "ai", "timeout_s": "300"})
        self.assertEqual(st, 500)
        self.assertIn("could not be written", text)

    def test_nothing_on_the_page_is_unescaped(self):
        self.portable()
        nuc_config.set_key(self.path, "display", "browser", '/opt/<b>x"</b>')
        render.reload_config()
        body = self.settings()
        self.assertIn("/opt/&lt;b&gt;x&quot;&lt;/b&gt;", body)
        self.assertIsNone(re.search(r"<b>x", body))


if __name__ == "__main__":
    unittest.main()
