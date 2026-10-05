"""The [features] switches of the settings page in a portable run (the desktop app is one): config.ini edited one value at a time
(nuc_config.set_key, set_feature), the collector reading them again (collector.reload_features, start_loops), and the page with its
POST /settings/feature (web.Server.settings_action). An installation's config.ini is the administrator's: there the page only shows."""
import contextlib
import http.client
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never the host's config.ini
import collector  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
from test_web import get_any as get, serve  # noqa: E402

SHIPPED = os.path.join(ROOT, "config", "config.ini")
POSIX = os.name == "posix"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.ini")
        shutil.copy(SHIPPED, self.path)


class Edit(Folder):
    def test_only_the_value_changes_the_key_and_its_comment_stay_where_they_were(self):
        before = read(self.path).splitlines()
        nuc_config.set_key(self.path, "features", "health", "no")
        after = read(self.path).splitlines()
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        self.assertEqual(len(changed), 1)
        old, new = changed[0]
        self.assertTrue(new.startswith("health          = no "), new)
        self.assertEqual(old.index("#"), new.index("#"), "the comment keeps its column")
        self.assertEqual(old[old.index("#"):], new[new.index("#"):])
        self.assertFalse(nuc_config.load(self.path)["features"]["health"])
        nuc_config.set_key(self.path, "features", "health", "yes")
        self.assertEqual(read(self.path), read(SHIPPED), "and back: the very same file")

    def test_a_long_value_pushes_the_comment_and_a_line_without_one_stays_bare(self):
        with open(self.path, "w") as f:
            f.write("[features]\nmap = yes # m\ncpu=no\n")
        nuc_config.set_key(self.path, "features", "map", "no")
        nuc_config.set_key(self.path, "features", "cpu", "yes")
        self.assertEqual(read(self.path), "[features]\nmap = no  # m\ncpu= yes\n")
        self.assertEqual(nuc_config.load(self.path)["features"]["cpu"], True)

    @unittest.skipUnless(POSIX, "POSIX permissions")
    def test_the_file_keeps_its_permissions(self):
        os.chmod(self.path, 0o600)
        nuc_config.set_key(self.path, "features", "map", "no")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_set_feature_writes_the_file_and_the_config_of_this_process(self):
        saved = dict(nuc_config.current()["features"])
        self.addCleanup(lambda: nuc_config.current()["features"].update(saved))
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": self.path}):
            nuc_config.set_feature("thermal", False)
            self.assertIs(nuc_config.current()["features"]["thermal"], False)
            self.assertFalse(nuc_config.load()["features"]["thermal"])
            for bad in ("nope", "", None, "features"):
                with self.assertRaises(ValueError):
                    nuc_config.set_feature(bad, True)

    def test_only_a_portable_run_may_write_its_own_config(self):
        with mock.patch.object(nuc_config, "PORTABLE", ""):
            ok, why = nuc_config.features_writable(self.path)
        self.assertEqual((ok, why), (False, "installed: config.ini is the administrator's"))
        with mock.patch.object(nuc_config, "PORTABLE", self.dir):
            self.assertEqual(nuc_config.features_writable(self.path), (True, ""))
            self.assertEqual(nuc_config.features_writable(os.path.join(self.dir, "missing.ini")), (True, ""), "it is made")
            if POSIX and os.geteuid() != 0:  # root may write anything
                os.chmod(self.path, 0o400)
                self.assertFalse(nuc_config.features_writable(self.path)[0])


class Reload(Folder):
    def setUp(self):
        Folder.setUp(self)
        for name, value in (("CFG", {"features": {f: True for f in nuc_config.FEATURES}, "ai": {}}), ("OFF", set()), ("_THREADS", {})):
            p = mock.patch.object(collector, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_the_switches_are_read_again_in_place(self):
        feats, off = collector.CFG["features"], collector.OFF
        nuc_config.set_key(self.path, "features", "health", "no")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertTrue(collector.reload_features(self.path))
        self.assertIs(collector.CFG["features"], feats)
        self.assertIs(collector.OFF, off)
        self.assertEqual(off, {"health"})
        self.assertIn("off now: health", err.getvalue())
        self.assertFalse(collector.reload_features(self.path), "nothing changed")

    def test_a_file_that_cannot_be_read_changes_nothing(self):
        collector.OFF.add("map")
        collector.CFG["features"]["map"] = False
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(collector.reload_features(self.dir))  # a folder: read() fails, load() says so, its defaults are not the user's
        self.assertEqual(collector.OFF, {"map"})

    def test_the_stamp_says_when_the_file_changed(self):
        self.assertIsNone(collector.config_stamp(os.path.join(self.dir, "missing.ini")))
        a = collector.config_stamp(self.path)
        with open(self.path, "a") as f:
            f.write("\n")
        self.assertNotEqual(collector.config_stamp(self.path), a)

    def test_a_loop_switched_on_starts_and_one_switched_off_is_told_to_stop(self):
        started = []

        class FakeThread(object):
            alive = True

            def __init__(self, target=None, kwargs=None, daemon=None):
                self.target, self.kwargs = target, kwargs or {}
                started.append(self)

            def start(self):
                pass

            def is_alive(self):
                return FakeThread.alive

        with mock.patch.object(collector, "threading", mock.Mock(Thread=FakeThread)):
            collector.OFF.add("health")
            collector.start_loops()
            self.assertNotIn(collector.history_loop, [t.target for t in started])
            collector.OFF.clear()
            collector.start_loops()
            hist = [t for t in started if t.target is collector.history_loop]
            self.assertEqual(len(hist), 1)
            stop = hist[0].kwargs["stop"]
            self.assertFalse(stop())
            collector.OFF.add("health")
            self.assertTrue(stop(), "switched off: the loop ends at its next pass and closes history.db")
            n = len(started)
            collector.OFF.clear()
            collector.start_loops()
            self.assertEqual(len(started), n, "a loop that still runs is not started twice")
            FakeThread.alive = False
            collector.start_loops()
            self.assertGreater(len(started), n, "one that ended is started again")


class Page(Folder):
    @classmethod
    def setUpClass(cls):
        cls.saved = {k: render.CFG[k] for k in ("expose", "webapps")}  # the demo fills them (render.demo_defaults): the next file's tests must not see it
        cls.srv = serve()

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        render.CFG.update(cls.saved)
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Folder.setUp(self)
        saved = dict(render.CFG["features"])
        self.addCleanup(lambda: (render.CFG["features"].clear(), render.CFG["features"].update(saved)))
        self.srv.cache.clear()

    def portable(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(nuc_config, "PORTABLE", self.dir))
        stack.enter_context(mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": self.path}))
        self.addCleanup(stack.close)

    def post(self, fields, csrf=True, origin=None, path="/settings/feature"):
        data = dict(fields)
        if csrf:
            data["csrf"] = self.srv.csrf if csrf is True else csrf
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("POST", path, urlencode(data), {"Content-Type": "application/x-www-form-urlencoded",
                                                  "Origin": origin or "http://127.0.0.1:%d" % self.srv.server_address[1]})
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read().decode())
        c.close()
        self.srv.cache.clear()
        return out

    def settings(self):
        self.srv.cache.clear()
        st, h, body = get(self.srv, "/?view=settings")
        self.assertEqual(st, 200)
        return h, body

    def test_an_installation_shows_the_switches_and_how_to_change_them_and_takes_no_post(self):
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": self.path}):
            h, body = self.settings()
            self.assertIn('<section class="sec" id="features"', body)
            self.assertIn("Read-only here (installed: config.ini is the administrator&#x27;s)", body)
            self.assertNotIn('action="/settings/feature"', body)
            self.assertIn("form-action 'none'", h["Content-Security-Policy"])
            self.assertEqual(body.count('data-feature="'), len(nuc_config.FEATURES))
            st, _h, text = self.post({"name": "health", "on": "no"})
        self.assertEqual(st, 403, text)
        self.assertIn("administrator", text)
        self.assertEqual(read(self.path), read(SHIPPED), "nothing was written")

    def test_a_portable_run_turns_a_section_off_and_on_from_the_page(self):
        self.portable()
        h, body = self.settings()
        self.assertEqual(body.count('action="/settings/feature"'), len(nuc_config.FEATURES))
        self.assertIn("form-action 'self'", h["Content-Security-Policy"])
        self.assertIn("view=health", body.split('class="tabs"')[1].split("</nav>")[0], "the Health tab is there")
        st, h, text = self.post({"name": "health", "on": "no", "back": "view=settings"})
        self.assertEqual(st, 303, text)
        self.assertTrue(h["Location"].endswith("#features"))
        self.assertIn("view=settings", h["Location"])
        self.assertFalse(render.CFG["features"]["health"], "the screens follow at once")
        self.assertFalse(nuc_config.load(self.path)["features"]["health"])
        _h, body = self.settings()
        row = body.split('data-feature="health"')[1].split("</li>")[0]
        self.assertIn("○ off", row)
        self.assertIn('name="on" value="yes"', row)
        self.assertIn(">Turn on<", row)
        self.assertNotIn("view=health", body.split('class="tabs"')[1].split("</nav>")[0], "no Health tab any more")
        self.assertEqual(self.post({"name": "health", "on": "yes"})[0], 303)
        self.assertEqual(read(self.path), read(SHIPPED))

    def test_what_a_post_must_be(self):
        self.portable()
        self.assertEqual(self.post({"name": "health", "on": "no"}, csrf=False)[0], 403)
        self.assertEqual(self.post({"name": "health", "on": "no"}, csrf="x" * 32)[0], 403)
        self.assertEqual(self.post({"name": "health", "on": "no"}, origin="http://203.0.113.7")[0], 403)
        for fields in ({"name": "nope", "on": "no"}, {"name": "health", "on": "maybe"}, {"name": "health"}, {"on": "no"}):
            self.assertEqual(self.post(fields)[0], 400, fields)
        self.assertEqual(self.post({"name": "health", "on": "no"}, path="/settings/other")[0], 404)
        self.assertEqual(read(self.path), read(SHIPPED), "nothing was written")
        st, h, _b = get(self.srv, "/settings/feature")
        self.assertEqual((st, h.get("Allow")), (405, "POST"))

    def test_a_file_that_cannot_be_written_is_said_not_hidden(self):
        self.portable()
        with mock.patch.object(nuc_config, "set_key", side_effect=OSError("disk full")), contextlib.redirect_stderr(io.StringIO()):
            st, _h, text = self.post({"name": "map", "on": "no"})
        self.assertEqual(st, 500)
        self.assertIn("could not be written", text)
        self.assertTrue(render.CFG["features"]["map"], "nothing changed")

    def test_the_ai_screen_switched_off_stops_the_model_server_this_process_started(self):
        self.portable()
        eng = mock.Mock()
        with mock.patch.object(render, "ai_engine", return_value=eng):
            self.post({"name": "health", "on": "no"})
            eng.stop_server.assert_not_called()
            self.post({"name": "ai", "on": "no"})
            eng.stop_server.assert_called_once_with()
            self.post({"name": "ai", "on": "yes"})
            eng.stop_server.assert_called_once_with()

    def test_every_feature_has_its_words_on_the_page(self):
        self.assertEqual([k for k, _t, _w in web.FEATURE_WORDS if k in nuc_config.FEATURES], [k for k, _t, _w in web.FEATURE_WORDS])
        self.assertEqual(sorted(k for k, _t, _w in web.FEATURE_WORDS), sorted(nuc_config.FEATURES))


if __name__ == "__main__":
    unittest.main()
