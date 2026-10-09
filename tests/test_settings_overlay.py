"""The settings page of an INSTALLATION: config.ini is root's and stays untouched; what the page changes goes in the overlay (settings.ini, in a folder
the web view's account owns) and nuc_config.load() lays it over config.ini for the renderer, the web view and the collector. The overlay is
untrusted input: an allowlist of keys, [features] only to switch OFF, a lock in config.ini, no links, a size cap, owner and mode checked."""
import contextlib
import copy
import http.client
import os
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
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import collector  # noqa: E402
import confedit  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
from test_web import get_any, serve  # noqa: E402

TOKEN = "t" * 24


def get(srv, path, method="GET", headers=None):
    return get_any(srv, path, method, dict({"Authorization": "Bearer " + TOKEN}, **(headers or {})))

SHIPPED = os.path.join(ROOT, "config", "config.ini")
POSIX = os.name == "posix"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def put(path, text, mode=0o640):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


class Folder(unittest.TestCase):
    """A config.ini of an installation and a settings folder this account owns, both in a temporary folder."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.ini")
        shutil.copy(SHIPPED, self.path)
        self.folder = os.path.join(self.dir, "ai")
        os.mkdir(self.folder)
        self.ov = os.path.join(self.folder, "settings.ini")
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": self.path, "NUC_CONSOLE_SETTINGS": self.ov}))
        self.addCleanup(stack.close)
        self.ini("[web]\nsettings_actions = yes\ntoken_file = /nonexistent/token")  # the opt-in, and a token configured (the server is given one below)

    def ini(self, text):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n" + text + "\n")
        self.config_before = read(self.path)


class Reading(Folder):
    def test_the_allowlist_is_one_tuple_of_presentation_keys(self):
        for sec, key in (("features", "docker_disk"), ("dashboard", "mode"), ("ui", "theme"), ("display", "zoom")):
            self.assertTrue(nuc_config.overlay_allowed(sec, key), (sec, key))
        for sec, key in (("web", "bind"), ("web", "port"), ("web", "token_file"), ("web", "allowed_hosts"), ("web", "enabled"),
                         ("web", "settings_actions"), ("telegram", "enabled"), ("telegram", "web_actions"), ("ai", "allow_remote"),
                         ("ai", "web_actions"), ("ai", "endpoint"), ("expose", "8080"), ("webapps", "x"), ("console", "font"), ("display", "mode"),
                         ("display", "browser")):
            self.assertFalse(nuc_config.overlay_allowed(sec, key), (sec, key))

    def test_a_key_outside_the_allowlist_is_ignored_and_reported(self):
        put(self.ov, "[web]\nbind = 0.0.0.0\nenabled = yes\nsettings_actions = no\n[telegram]\nenabled = yes\n[ai]\nallow_remote = yes\n"
                     "web_actions = no\nendpoint = http://203.0.113.9/v1\n[expose]\n8080 = lan\n[webapps]\nx = 1\n[console]\nfont = x\n"
                     "[DEFAULT]\ntheme = light\n[display]\nmode = none\nzoom = 150\n[ui]\ntheme = dark\n")
        said = []
        got = nuc_config.read_overlay(self.ov, said.append)
        self.assertEqual(got, {"display.zoom": "150", "ui.theme": "dark"})
        self.assertGreaterEqual(len(said), 12)
        self.assertTrue(any("web.bind" in x for x in said), said)
        cfg = nuc_config.load()
        base = nuc_config.load(overlay=False)
        for k in ("web", "telegram", "ai", "expose", "webapps", "console"):
            self.assertEqual(cfg[k], base[k], k)
        self.assertEqual((cfg["ui"]["theme"], cfg["display"]["zoom"]), ("dark", 150))
        self.assertEqual(cfg["overlaid"], {"display.zoom": "150", "ui.theme": "dark"})

    def test_a_feature_can_only_be_switched_off(self):
        self.ini("[features]\ndocker_disk = no")  # config.ini switches it off (a second [features] section: configparser merges)
        put(self.ov, "[features]\ndocker_disk = yes\ncontainers = no\nthermal = yes\nnot_a_feature = no\nai = maybe\n")
        f = nuc_config.load()["features"]
        self.assertFalse(f["docker_disk"], "the overlay cannot switch on what config.ini switched off")
        self.assertFalse(f["containers"])
        self.assertTrue(f["thermal"] and f["ai"], "yes and nonsense switch nothing")
        self.assertEqual(nuc_config.read_overlay(self.ov), {"features.containers": "no"})

    def test_config_ini_wins_when_it_locks_the_page(self):
        put(self.ov, "[ui]\ntheme = dark\n[features]\nmap = no\n")
        self.assertEqual(nuc_config.load()["ui"]["theme"], "dark")
        self.ini("[web]\nsettings_actions = no")
        cfg = nuc_config.load()
        self.assertNotEqual(cfg["ui"].get("theme"), "dark")
        self.assertTrue(cfg["features"]["map"])
        self.assertEqual(cfg["overlaid"], {})

    def test_the_file_is_untrusted(self):
        put(self.ov, "[ui]\ntheme = dark\n")
        self.assertEqual(nuc_config.read_overlay(self.ov), {"ui.theme": "dark"})
        said = []
        with mock.patch.object(nuc_config, "OVERLAY_MAX", 10):
            self.assertEqual(nuc_config.read_overlay(self.ov, said.append), {}, "too big")
        self.assertTrue(said)
        if POSIX:
            os.chmod(self.ov, 0o666)
            self.assertEqual(nuc_config.read_overlay(self.ov), {}, "writable by others")
            os.chmod(self.ov, 0o660)
            self.assertEqual(nuc_config.read_overlay(self.ov), {}, "writable by the group")
            os.chmod(self.ov, 0o640)
            with mock.patch.object(nuc_config.os, "fstat", lambda fd: type("S", (), {"st_mode": 0o100640, "st_size": 20, "st_uid": 12345})()):
                self.assertEqual(nuc_config.read_overlay(self.ov), {}, "owned by neither root nor the folder's owner")
            os.remove(self.ov)
            os.mkfifo(self.ov)
            self.assertEqual(nuc_config.read_overlay(self.ov), {}, "not a regular file (and a fifo does not block)")
            os.remove(self.ov)
            victim = os.path.join(self.dir, "victim.ini")
            put(victim, "[ui]\ntheme = dark\n")
            os.symlink(victim, self.ov)
            self.assertEqual(nuc_config.read_overlay(self.ov), {}, "a link is not followed")
            self.assertEqual(nuc_config.load()["overlaid"], {})
        for junk in (b"\xff\xfe\x00garbage", b"[ui\ntheme", b"no section = here", b"[ui]\n" + b"x = 1\n" * 10, b""):
            if os.path.lexists(self.ov):
                os.remove(self.ov)
            with open(self.ov, "wb") as f:
                f.write(junk)
            os.chmod(self.ov, 0o640)
            nuc_config.read_overlay(self.ov)  # never raises
            self.assertEqual(nuc_config.load(self.path, overlay=self.ov)["features"], nuc_config.load(self.path, overlay=False)["features"])

    def test_a_value_the_dashboard_would_not_take_costs_that_key_only(self):
        put(self.ov, "[dashboard]\nrotate_seconds = lots\nmode = rotate\n[ui]\nkpis = nope\ntheme = dark\n")
        cfg = nuc_config.load(self.path, warn=lambda line: None)
        self.assertEqual(cfg["mode"], "rotate")
        self.assertEqual(cfg["rotate_seconds"], 15)
        self.assertEqual(cfg["ui"]["theme"], "dark")

    def test_a_portable_run_has_no_overlay(self):
        put(self.ov, "[ui]\ntheme = dark\n")
        with mock.patch.object(nuc_config, "PORTABLE", self.dir):
            self.assertEqual(nuc_config.overlay_path(), "")
            self.assertEqual(nuc_config.load()["overlaid"], {})


class Writing(Folder):
    def test_written_aside_0640_atomic_and_never_through_a_link(self):
        nuc_config.update_overlay({"ui.theme": "dark"}, path=self.ov)
        self.assertEqual(read(self.ov).split("[ui]")[1].strip(), "theme = dark")
        self.assertEqual(os.listdir(self.folder), ["settings.ini"], "no .tmp left")
        if POSIX:
            self.assertEqual(oct(os.stat(self.ov).st_mode & 0o777), oct(0o640))
            victim = os.path.join(self.dir, "victim")
            put(victim, "untouched")
            os.symlink(victim, self.ov + ".tmp")  # a stale name planted as a link
            nuc_config.update_overlay({"ui.density": "wall"}, path=self.ov)
            self.assertEqual(read(victim), "untouched", "the link was removed, not written through")
            self.assertEqual(nuc_config.read_overlay(self.ov), {"ui.theme": "dark", "ui.density": "wall"})
            os.remove(self.ov)
            os.symlink(victim, self.ov)  # the file itself a link: replaced by a regular file, the target stays
            nuc_config.update_overlay({"ui.theme": "light"}, path=self.ov)
            self.assertFalse(os.path.islink(self.ov))
            self.assertEqual(read(victim), "untouched")
            link = os.path.join(self.dir, "linked")
            os.symlink(self.folder, link)
            with self.assertRaises(OSError):
                nuc_config.update_overlay({"ui.theme": "dark"}, path=os.path.join(link, "settings.ini"))

    def test_only_the_allowlist_can_be_written(self):
        for name, val in (("web.bind", "0.0.0.0"), ("telegram.enabled", "yes"), ("ai.allow_remote", "yes"), ("features.docker_disk", "yes"),
                          ("features.nothing", "no"), ("console.font", "x")):
            with self.assertRaises(ValueError, msg=name):
                nuc_config.update_overlay({name: val}, path=self.ov)
        self.assertFalse(os.path.exists(self.ov))

    def test_dropping_everything_removes_the_file_and_a_check_can_refuse(self):
        nuc_config.update_overlay({"ui.theme": "dark", "features.map": "no"}, path=self.ov)
        nuc_config.update_overlay(drop=["ui.theme"], path=self.ov)
        self.assertEqual(nuc_config.read_overlay(self.ov), {"features.map": "no"})
        nuc_config.update_overlay(drop=["*"], path=self.ov)
        self.assertFalse(os.path.exists(self.ov))
        nuc_config.update_overlay({"ui.theme": "dark"}, path=self.ov)
        boom = mock.Mock(side_effect=confedit.Refused(["no"]))
        with self.assertRaises(confedit.Refused):
            nuc_config.update_overlay({"ui.theme": "light"}, path=self.ov, check=boom)
        self.assertEqual(nuc_config.read_overlay(self.ov), {"ui.theme": "dark"}, "the old file stays")
        self.assertEqual(os.listdir(self.folder), ["settings.ini"])

    def test_windows_grants_the_process_own_sid_and_fails_closed(self):
        sid = "S-1-5-21-111-222-333-1001"
        who = mock.Mock(returncode=0, stdout='"HOST\\svc","%s"\n' % sid)
        with mock.patch.object(nuc_config, "WINDOWS", True), mock.patch("subprocess.run") as run:
            run.side_effect = [mock.Mock(returncode=0, stdout=""), mock.Mock(returncode=0, stdout="")]  # whoami finds no SID: nothing is written
            with self.assertRaises(OSError):
                nuc_config.update_overlay({"ui.theme": "dark"}, path=self.ov)
            self.assertEqual(os.listdir(self.folder), [], "no SID, nothing left behind")
            run.side_effect = [who, mock.Mock(returncode=5)]  # icacls fails
            with self.assertRaises(OSError):
                nuc_config.update_overlay({"ui.theme": "dark"}, path=self.ov)
            self.assertEqual(os.listdir(self.folder), [])
            run.side_effect = [who, mock.Mock(returncode=0)]
            nuc_config.update_overlay({"ui.theme": "dark"}, path=self.ov)
            args = run.call_args[0][0]
            self.assertIn("/inheritance:r", args)
            self.assertIn("*%s:F" % sid, args)
            self.assertFalse([a for a in args if "USERNAME" in a or a.startswith("HOST")], "granted by SID, not by a name from the environment")
        self.assertEqual(nuc_config.read_overlay(self.ov), {"ui.theme": "dark"} if POSIX else {})

    def test_save_overlay_checks_like_save_and_keeps_config_ini_as_it_is(self):
        self.assertEqual(confedit.save_overlay(self.path, "ui", {"theme": ["dark"], "density": [""]}, self.ov), ["theme"])
        self.assertEqual(nuc_config.read_overlay(self.ov), {"ui.theme": "dark"})
        self.assertEqual(confedit.save_overlay(self.path, "ui", {"theme": ["dark"]}, self.ov), [], "nothing new")
        self.assertEqual(confedit.save_overlay(self.path, "dashboard", {"rotate_seconds": ["15"]}, self.ov), [], "config.ini's own value is not kept")
        with self.assertRaises(confedit.Refused):
            confedit.save_overlay(self.path, "dashboard", {"rotate_seconds": ["5000"]}, self.ov)
        self.assertEqual(confedit.save_overlay(self.path, "ui", {"theme": [""]}, self.ov), ["theme"], "empty: back to config.ini's")
        self.assertFalse(os.path.exists(self.ov))
        for section, form in (("web", {"bind": ["0.0.0.0"]}), ("telegram", {"enabled": ["yes"]}), ("ai", {"allow_remote": ["yes"]}),
                              ("expose", {"text": ["x = internet"]}), ("webapps", {"text": ["x = 1"]}), ("console", {"font": ["x"]}),
                              ("display", {"mode": ["none"]}), ("features", {"map": ["no"]}), ("nope", {})):
            with self.assertRaises(confedit.Refused, msg=section):
                confedit.save_overlay(self.path, section, form, self.ov)
        self.assertFalse(os.path.exists(self.ov))
        self.assertEqual(read(self.path), self.config_before)


class Page(Folder):
    @classmethod
    def setUpClass(cls):
        cls.saved = copy.deepcopy(render.CFG)
        cls.srv = serve(TOKEN)

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
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Folder.setUp(self)
        self.addCleanup(lambda: (self._restore(), self.srv.cache.clear()))
        render.reload_config()
        self.srv.cache.clear()

    def _restore(self):
        for k, v in self.saved.items():
            old = render.CFG.get(k)
            if isinstance(old, dict) and isinstance(v, dict):
                old.clear()
                old.update(copy.deepcopy(v))
            elif isinstance(old, list) and isinstance(v, list):
                old[:] = v
            else:
                render.CFG[k] = v

    def post(self, path, fields, csrf=True, headers=None):
        data = dict(fields)
        if csrf:
            data["csrf"] = self.srv.csrf if csrf is True else csrf
        port = self.srv.server_address[1]
        h = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:%d" % port, "Authorization": "Bearer " + TOKEN}
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", path, urlencode(data), h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read().decode())
        c.close()
        self.srv.cache.clear()
        return out

    def page(self):
        self.srv.cache.clear()
        st, _h, body = get(self.srv, "/?view=settings")
        self.assertEqual(st, 200)
        return body

    def test_a_save_goes_to_the_overlay_and_the_page_marks_it_and_resets_it(self):
        st, h, text = self.post("/settings/config", {"section": "ui", "theme": "dark", "back": "view=settings"})
        self.assertEqual(st, 303, text)
        self.assertTrue(h["Location"].endswith("#cfg-ui"))
        self.assertEqual(read(self.path), self.config_before, "config.ini is untouched")
        self.assertEqual(nuc_config.read_overlay(self.ov), {"ui.theme": "dark"})
        self.assertEqual(render.CFG["ui"]["theme"], "dark", "this process follows at once")
        body = self.page()
        row = body.split('id="k-ui-theme"')[1].split("</div>")[0]
        self.assertIn("set from this page", row)
        self.assertIn('value="ui.theme"', row)
        self.assertIn("Reset to config.ini", row)
        self.assertNotIn("set from this page", body.split('id="k-ui-density"')[1].split("</div>")[0])
        self.assertIn('action="/settings/config"', body.split('id="cfg-ui"')[1].split("</details>")[0])
        self.assertNotIn("<form", body.split('id="cfg-web"')[1].split("</details>")[0], "the administrator's sections stay read-only")
        self.assertNotIn("<textarea", body)
        self.assertNotIn("Read-only here (the settings folder", body)
        st, h, text = self.post("/settings/reset", {"reset": "ui.theme", "back": "view=settings"})
        self.assertEqual(st, 303, text)
        self.assertFalse(os.path.exists(self.ov))
        self.assertNotEqual(render.CFG["ui"].get("theme"), "dark")
        self.assertNotIn("set from this page", self.page().split('id="k-ui-theme"')[1].split("</div>")[0])

    def test_the_pages_can_switch_a_feature_off_and_reset_it_but_not_on(self):
        self.ini("[features]\nthermal = no")
        body = self.page()
        self.assertIn("off in config.ini", body.split('data-feature="thermal"')[1].split("</li>")[0])
        self.assertEqual(self.post("/settings/feature", {"name": "thermal", "on": "yes"})[0], 400, "config.ini says no: only the administrator")
        st, _h, text = self.post("/settings/feature", {"name": "map", "on": "no", "back": "view=settings"})
        self.assertEqual(st, 303, text)
        self.assertEqual(nuc_config.read_overlay(self.ov), {"features.map": "no"})
        self.assertFalse(render.CFG["features"]["map"])
        self.assertEqual(read(self.path), self.config_before)
        row = self.page().split('data-feature="map"')[1].split("</li>")[0]
        self.assertIn("set from this page", row)
        self.assertIn("Reset to config.ini", row)
        self.assertEqual(self.post("/settings/feature", {"name": "map", "on": "yes"})[0], 303)
        self.assertFalse(os.path.exists(self.ov))
        self.assertTrue(render.CFG["features"]["map"])
        self.assertFalse(render.CFG["features"]["thermal"])

    def test_a_key_outside_the_allowlist_is_refused_on_a_post(self):
        for section, fields in (("web", {"bind": "0.0.0.0"}), ("web", {"settings_actions": "no"}), ("telegram", {"enabled": "yes"}),
                                ("ai", {"allow_remote": "yes"}), ("ai", {"web_actions": "no"}), ("expose", {"text": "x = internet"}),
                                ("webapps", {"text": "x = 1"}), ("console", {"font": "x"}), ("display", {"mode": "none", "zoom": "120"})):
            st, _h, text = self.post("/settings/config", dict(fields, section=section, back="view=settings"))
            self.assertEqual(st, 303, text)
            self.assertFalse(os.path.exists(self.ov), (section, fields))
            self.assertEqual(read(self.path), self.config_before)
            body = self.page()
            self.assertIn("Not saved", body, (section, fields))
        for name, on in (("nope", "no"), ("map", "maybe")):
            self.assertEqual(self.post("/settings/feature", {"name": name, "on": on})[0], 400)
        for what in ("web.bind", "telegram.enabled", "ai.allow_remote", "features", "nope", ""):
            self.assertEqual(self.post("/settings/reset", {"reset": what})[0], 400, what)
        self.assertEqual(read(self.path), self.config_before)

    def test_a_refused_value_writes_nothing(self):
        st, _h, _t = self.post("/settings/config", {"section": "dashboard", "rotate_seconds": "99999", "back": "view=settings"})
        self.assertEqual(st, 303)
        self.assertFalse(os.path.exists(self.ov))
        self.assertIn("Not saved", self.page())

    def test_the_post_guards_are_the_ones_of_every_other_page(self):
        port = self.srv.server_address[1]
        form = {"section": "ui", "theme": "dark"}
        for path in ("/settings/config", "/settings/feature", "/settings/reset"):
            fields = {"section": "ui", "theme": "dark"} if path.endswith("config") else {"name": "map", "on": "no", "reset": "ui.theme"}
            self.assertEqual(self.post(path, fields, csrf=False)[0], 403, "no CSRF token")
            self.assertEqual(self.post(path, fields, csrf="x" * 32)[0], 403, "a wrong CSRF token")
            self.assertEqual(self.post(path, fields, headers={"Origin": "http://203.0.113.7"})[0], 403, "another origin")
            self.assertEqual(self.post(path, fields, headers={"Origin": "http://127.0.0.1:%d" % (port + 1)})[0], 403, "another port")
            self.assertEqual(self.post(path, fields, headers={"Referer": "http://evil.example/"})[0], 403, "another referer")
            self.assertEqual(self.post(path, fields, headers={"Sec-Fetch-Site": "cross-site"})[0], 403, "a cross-site fetch")
            self.assertEqual(self.post(path, fields, headers={"Authorization": "Bearer " + "x" * 24})[0], 401, "a wrong access token")
            self.assertEqual(self.post(path, fields, headers={"Authorization": ""})[0], 401, "no access token")
            self.assertEqual(self.post(path, fields, headers={"Content-Type": "application/json"})[0], 415)
        self.assertFalse(os.path.exists(self.ov))
        self.assertEqual(read(self.path), self.config_before)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/settings/config")
        self.assertEqual(c.getresponse().status, 405)

    def test_the_lock_and_an_unwritable_folder_make_the_page_read_only(self):
        self.ini("[web]\nsettings_actions = no")
        render.reload_config()
        self.assertEqual(nuc_config.settings_mode()[0], "")
        st, _h, text = self.post("/settings/config", {"section": "ui", "theme": "dark"})
        self.assertEqual(st, 403, text)
        self.assertIn("settings_actions", text)
        self.assertIn("Read-only here (off in config.ini", self.page())
        self.assertFalse(os.path.exists(self.ov))
        shutil.rmtree(self.folder)
        self.assertEqual(self.post("/settings/feature", {"name": "map", "on": "no"})[0], 403)

    def test_without_a_token_the_page_stays_read_only_even_when_opted_in(self):
        """A local user could take the CSRF token from a page served without one: loopback alone is not enough."""
        self.assertEqual(nuc_config.settings_mode(token="")[0], "")
        self.assertIn("token_file", nuc_config.settings_mode(token="")[1])
        self.assertEqual(nuc_config.settings_mode(token=TOKEN)[0], "overlay")
        open_srv = serve()
        self.addCleanup(lambda: (open_srv.shutdown(), open_srv.server_close()))
        port = open_srv.server_address[1]
        for path, data in (("/settings/feature", {"name": "exposure", "on": "no"}), ("/settings/config", {"section": "ui", "theme": "dark"})):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", path, urlencode(dict(data, csrf=open_srv.csrf)), {"Content-Type": "application/x-www-form-urlencoded",
                                                                                 "Origin": "http://127.0.0.1:%d" % port})
            r = c.getresponse()
            self.assertEqual(r.status, 403, path)
            self.assertIn("token_file", r.read().decode())
            c.close()
        self.assertFalse(os.path.exists(self.ov))
        self.assertEqual(read(self.path), self.config_before)

    def test_off_by_default_and_a_typo_is_a_lock(self):
        shutil.copy(SHIPPED, self.path)
        put(self.ov, "[ui]\ntheme = dark\n")
        self.assertFalse(nuc_config.load()["web"]["settings_actions"])
        self.assertEqual(nuc_config.load()["overlaid"], {}, "the shipped config.ini opts in nowhere")
        render.reload_config()
        self.assertEqual(nuc_config.settings_mode(token=TOKEN)[0], "")
        for word in ("nope", "ja", "", "1x"):
            self.ini("[web]\nsettings_actions = %s" % word)
            said = []
            cfg = nuc_config.load(self.path, warn=said.append)
            self.assertEqual(cfg["overlaid"], {}, repr(word))
            self.assertFalse(cfg["web"]["settings_actions"])
        self.ini("[web]\nsettings_actions = yes")
        self.assertEqual(nuc_config.load()["overlaid"], {"ui.theme": "dark"})

    def test_hiding_a_security_section_raises_a_problem_that_cannot_be_accepted(self):
        import problems
        render.reload_config()
        self.assertNotIn("feature-hidden", [p[2] for p in problems.problems_raw({}, None)])
        self.assertEqual(self.post("/settings/feature", {"name": "thermal", "on": "no"})[0], 303)
        self.assertNotIn("feature-hidden", [p[2] for p in problems.problems_raw({}, None)], "thermal is no security section")
        self.assertEqual(self.post("/settings/feature", {"name": "exposure", "on": "no"})[0], 303)
        self.assertEqual(self.post("/settings/feature", {"name": "firewall", "on": "no"})[0], 303)
        raw = [p for p in problems.problems_raw({}, None) if p[2] == "feature-hidden"]
        self.assertEqual([(p[0], "exposure, firewall" in p[1]) for p in raw], [(2, True)])
        self.assertIn("feature-hidden", problems.NOT_ACCEPTABLE)
        self.assertIn("feature-hidden", problems.CATALOG)
        self.assertEqual(self.post("/settings/reset", {"reset": "*"})[0], 303)
        self.assertNotIn("feature-hidden", [p[2] for p in problems.problems_raw({}, None)])

    def test_the_notifier_that_cannot_read_the_file_says_so_once_and_calmly(self):
        said = []
        with mock.patch.object(nuc_config.os, "open", side_effect=PermissionError(13, "denied")):
            self.assertEqual(nuc_config.read_overlay(self.ov, said.append), {})
        self.assertEqual(len(said), 1)
        self.assertIn("by design", said[0])
        nuc_config._REPORTED.clear()
        printed = []
        with mock.patch.object(nuc_config.os, "open", side_effect=PermissionError(13, "denied")), mock.patch("builtins.print", lambda *a, **k: printed.append(a)):
            for _ in range(5):
                nuc_config.load(self.path)
        self.assertEqual(len(printed), 1, "once per process")

    def test_the_effective_configuration_is_the_same_for_every_reader(self):
        put(self.ov, "[features]\nmap = no\ndocker_disk = yes\n[ui]\ntheme = dark\n[dashboard]\nrotate_seconds = 30\n[display]\nzoom = 120\n"
                     "[web]\nbind = 0.0.0.0\n")
        want = nuc_config.load(self.path, warn=lambda line: None)
        self.assertFalse(want["features"]["map"])
        self.assertEqual((want["ui"]["theme"], want["rotate_seconds"], want["display"]["zoom"], want["web"]["bind"]), ("dark", 30, 120, "127.0.0.1"))
        render.reload_config()  # the renderer and the web view
        for k in ("features", "ui", "rotate_seconds", "display", "web", "overlaid"):
            self.assertEqual(render.CFG[k], want[k], k)
        with mock.patch.dict(collector.CFG["features"]):  # the collector
            collector.reload_features()
            self.assertEqual(collector.CFG["features"], want["features"])
            self.assertIn("map", collector.OFF)
        collector.OFF.clear()
        self.assertEqual(nuc_config.load(warn=lambda line: None), want)

    def test_the_collector_cannot_be_made_to_run_more(self):
        self.ini("[features]\ndocker_disk = no\ncontainers = no")
        base = nuc_config.load(self.path, overlay=False)["features"]
        for text in ("[features]\ndocker_disk = yes\ncontainers = yes\nfirewall = yes\n[ai]\nenabled = yes\ndaily = yes\n[webapps]\nx = 1\n",
                     "[features]\n" + "\n".join("%s = yes" % f for f in nuc_config.FEATURES) + "\n[web]\nenabled = yes\n"):
            put(self.ov, text)
            cfg = nuc_config.load(self.path, overlay=self.ov, warn=lambda line: None)
            for f, on in cfg["features"].items():
                self.assertFalse(on and not base[f], "%s switched on by the overlay" % f)
            self.assertEqual((cfg["ai"], cfg["webapps"], cfg["web"]["enabled"]), (nuc_config.load(self.path, overlay=False)["ai"],
                                                                                    {}, False))
        put(self.ov, "[features]\nfirewall = no\n")
        with mock.patch.dict(collector.CFG["features"]), mock.patch.object(collector, "OFF", set()):
            saved = dict(collector.CFG["features"])
            collector.CFG["features"].update(base)
            self.assertTrue(collector.reload_features())
            self.assertIn("firewall", collector.OFF)
            collector.CFG["features"].clear()
            collector.CFG["features"].update(saved)
        for junk in (b"\x00" * 50, b"[features]\nfirewall", b"[[[", b"\xff" * 99):
            with open(self.ov, "wb") as f:
                f.write(junk)
            os.chmod(self.ov, 0o640)
            with mock.patch.dict(collector.CFG["features"]), mock.patch.object(collector, "OFF", set()):
                collector.reload_features()  # never raises, never stops the collector

    def test_the_collector_notices_a_change_of_the_overlay(self):
        a = collector.config_stamp()
        put(self.ov, "[features]\nmap = no\n")
        b = collector.config_stamp()
        self.assertNotEqual(a, b)
        put(self.ov, "[features]\nmap = no\ncpu = no\n")
        self.assertNotEqual(collector.config_stamp(), b)
        os.remove(self.ov)
        self.assertEqual(collector.config_stamp(), a)


if __name__ == "__main__":
    unittest.main()
