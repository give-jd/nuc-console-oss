"""The host id (written once by the collector, read by everyone else) and [host] name / [hosts] in the configuration."""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import confedit  # noqa: E402
import confmigrate  # noqa: E402
import nuc_config  # noqa: E402
import notify  # noqa: E402

GOOD = "0123456789abcdef"


class HostId(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "lib", "host_id")
        self.said = []

    def put(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as f:
            f.write(data)

    def test_made_once_and_kept(self):
        a = nuc_config.ensure_host_id(self.path, self.said.append)
        self.assertRegex(a, r"^[0-9a-f]{16}$")
        self.assertEqual(nuc_config.ensure_host_id(self.path, self.said.append), a)
        self.assertEqual(nuc_config.read_host_id(self.path), a)
        self.assertEqual(self.said, [])
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        if os.name == "posix":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)

    def test_a_reader_never_invents_one(self):
        self.assertIsNone(nuc_config.read_host_id(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_a_corrupt_file_is_replaced_once_and_said(self):
        for bad in (b"", b"xyz", GOOD.upper().encode(), (GOOD + "0").encode(), b"\xff\xfe" * 8, b"a" * 5000, GOOD.encode() + b"\n" + b"x" * 80):
            self.put(bad)
            self.said.clear()
            self.assertIsNone(nuc_config.read_host_id(self.path), bad[:20])
            new = nuc_config.ensure_host_id(self.path, self.said.append)
            self.assertRegex(new, r"^[0-9a-f]{16}$")
            self.assertEqual(len(self.said), 1)
            self.assertEqual(nuc_config.ensure_host_id(self.path, self.said.append), new)
            self.assertEqual(len(self.said), 1, "said once")

    def test_a_valid_file_with_a_newline_or_spaces_is_read(self):
        self.put(b" " + GOOD.encode() + b"\r\n")
        self.assertEqual(nuc_config.read_host_id(self.path), GOOD)

    def test_a_directory_in_its_place_is_no_id_and_is_not_touched_by_the_reader(self):
        os.makedirs(self.path)
        self.assertIsNone(nuc_config.read_host_id(self.path))

    @unittest.skipIf(os.name != "posix", "symbolic links: POSIX")
    def test_a_link_is_replaced_and_its_target_left_alone(self):
        target = os.path.join(self.dir, "other")
        with open(target, "w") as f:
            f.write(GOOD)
        os.makedirs(os.path.dirname(self.path))
        os.symlink(target, self.path)
        self.assertIsNone(nuc_config.read_host_id(self.path), "a reader never follows it")
        new = nuc_config.ensure_host_id(self.path, self.said.append)
        self.assertRegex(new or "", r"^[0-9a-f]{16}$")
        self.assertNotEqual(new, GOOD)
        self.assertFalse(os.path.islink(self.path))
        self.assertEqual(nuc_config.read_host_id(self.path), new)
        self.assertEqual(len(self.said), 1)
        with open(target) as f:
            self.assertEqual(f.read(), GOOD, "the link's target is left alone")

    def test_a_directory_in_place_is_said_once_and_left(self):
        os.makedirs(self.path)
        self.assertIsNone(nuc_config.ensure_host_id(self.path, self.said.append))
        self.assertTrue(os.path.isdir(self.path))
        self.assertEqual(len(self.said), 1)

    def test_an_empty_environment_variable_means_the_default(self):
        import subprocess
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
        env = dict(os.environ, NUC_CONSOLE_HOST_ID="", NUC_CONSOLE_HOME="")
        out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import nuc_config as n; print(n.HOST_ID_PATH == n.os.path.join(n.LIB_DIR, 'host_id'))", src],
                             env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)

    @unittest.skipIf(not hasattr(os, "mkfifo"), "FIFOs: POSIX")
    def test_a_fifo_does_not_block(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        self.assertIsNone(nuc_config.read_host_id(self.path))

    def test_unwritable_place_gives_none_not_an_error(self):
        blocker = os.path.join(self.dir, "file")
        with open(blocker, "w") as f:
            f.write("x")
        self.assertIsNone(nuc_config.ensure_host_id(os.path.join(blocker, "host_id"), self.said.append))


class HostName(unittest.TestCase):
    def load(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "config.ini")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        said = []
        return nuc_config.load(p, warn=said.append, overlay=False), said

    def test_valid_name_is_used_everywhere(self):
        cfg, said = self.load("[host]\nname = nuc-a.lan_1\n")
        self.assertEqual((cfg["host"]["name"], said), ("nuc-a.lan_1", []))
        self.assertEqual(nuc_config.host_label(cfg), "nuc-a.lan_1")
        self.assertEqual(notify.hostname(cfg), "nuc-a.lan_1")

    def test_default_is_the_host_name_and_a_bad_name_warns_once(self):
        cfg, said = self.load("")
        self.assertEqual(cfg["host"]["name"], "")
        self.assertEqual(nuc_config.host_label(cfg), nuc_config.socket.gethostname())
        for bad in ("-lead", "has space", "x" * 41, "a/b", "café", ".dot"):
            cfg, said = self.load("[host]\nname = %s\n" % bad)
            self.assertEqual(cfg["host"]["name"], "", bad)
            self.assertEqual(len(said), 1, bad)
        self.assertEqual(nuc_config.host_label({"host": {"name": "bad name"}}), nuc_config.socket.gethostname())

    def test_a_message_uses_the_configured_name_and_the_service_reads_it_once(self):
        cfg = {"host": {"name": "nuc-a"}}
        with unittest.mock.patch.object(nuc_config, "load", return_value=cfg) as load:
            svc = notify.Service(os.path.join(tempfile.gettempdir(), "nc-none"), log=lambda _t: None)
            self.assertEqual((svc.label(), svc.label(), svc.label()), ("nuc-a",) * 3)
            self.assertEqual(load.call_count, 1)
        self.assertEqual(notify.format_message("nuc-a", [], started=0).splitlines()[0], "nuc-console \u00b7 nuc-a")

    def test_the_key_is_documented_and_not_for_the_overlay(self):
        key = confedit.KEY[("host", "name")]
        self.assertEqual((key.default, key.applies), ("", confedit.START))
        self.assertFalse(key.editable(overlay=True))
        for sec, k in (("host", "name"), ("hosts", "nuc-b")):
            self.assertFalse(nuc_config.overlay_allowed(sec, k), (sec, k))
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "docs", "CONFIGURATION.md"), encoding="utf-8") as f:
            self.assertIn("`[host]`", f.read())

    def test_migrate_keeps_host_and_hosts(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "config", "config.ini"), encoding="utf-8") as f:
            dist = f.read()
        old = "[host]\nname = nuc-a\n\n[hosts]\nnuc-b = https://nuc-b.example.ts.net token=/etc/nuc-console/peers/nuc-b.token\n"
        new, _lost = confmigrate.build(old, dist)
        self.assertIn("\nname = nuc-a\n", new)
        self.assertIn("\nnuc-b = https://nuc-b.example.ts.net token=/etc/nuc-console/peers/nuc-b.token\n", new)
        self.assertNotIn("legacy", new.split("[hosts]")[1].split("\n")[0] if "[hosts]" in new else "")
        self.assertNotIn("# legacy", new)


if __name__ == "__main__":
    unittest.main()
