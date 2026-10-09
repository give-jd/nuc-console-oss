"""`nuc-console-config migrate` (src/confmigrate.py): an old, long config.ini (tests/fixtures/config-old-layout.ini: the file the installers
shipped before the one-line-per-key layout) is brought to config/config.ini without losing a setting, and the configuration in force stays
the same in every section."""
import contextlib
import io
import os
import re
import stat
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import confmigrate  # noqa: E402
import nuc_config  # noqa: E402

with open(os.path.join(HERE, "fixtures", "config-old-layout.ini"), encoding="utf-8") as _f:
    OLD = _f.read()
with open(os.path.join(ROOT, "config", "config.ini"), encoding="utf-8") as _f:
    DIST = _f.read()


def set_old(text, section, key, value):
    """The old layout's line of a key with another value (its comment stays)."""
    sec, out = None, []
    for ln in text.split("\n"):
        if ln.startswith("["):
            sec = ln.strip()
        elif sec == "[%s]" % section and re.match(r"%s\s*=" % re.escape(key), ln):
            ln = re.sub(r"=\s*[^#]*?(\s*(#.*)?)$", lambda m: "= %s%s" % (value, m.group(1) if m.group(2) else ""), ln, count=1)
        out.append(ln)
    return "\n".join(out)


def add_to(text, section, line):
    return text.replace("\n[%s]\n" % section, "\n[%s]\n%s\n" % (section, line), 1)


class Migrate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.ini")
        with open(self.path + ".dist", "w", encoding="utf-8") as f:
            f.write(DIST)

    def put(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(self.path, 0o644)
        return text

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def run_cmd(self, **kw):
        lines = []
        rc = confmigrate.migrate(self.path, out=lines.append, **kw)
        return rc, "\n".join(lines)

    def cfg(self, path=None):
        return nuc_config.load(path or self.path, warn=lambda _: None)

    def backups(self):
        return sorted(n for n in os.listdir(self.dir) if ".bak-" in n)

    def migrated(self, text, **kw):
        """Migrates `text`; checks the configuration in force is unchanged; returns the new file."""
        self.put(text)
        before = self.cfg()
        rc, out = self.run_cmd(yes=True, **kw)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.cfg(), before)
        return self.read()

    def test_fixture_is_the_old_long_layout(self):
        self.assertGreater(len(OLD.splitlines()), 150)
        self.assertIsNone(confmigrate.layout(OLD))
        self.assertEqual(confmigrate.layout(DIST), "2")
        self.assertTrue(confmigrate.notice(self.put(OLD) and self.path).startswith("config.ini is in the old layout: run `"))

    def test_defaults_only_becomes_the_shipped_file(self):
        self.assertEqual(self.migrated(OLD), DIST)

    def test_changed_values_are_carried_and_defaults_dropped(self):
        old = set_old(set_old(set_old(OLD, "dashboard", "rotate_seconds", "30"), "features", "sessions", "no"), "web", "port", "9000")
        new = self.migrated(old)
        self.assertRegex(new, r"(?m)^rotate_seconds = 30$")
        self.assertRegex(new, r"(?m)^sessions\s+= no")
        self.assertRegex(new, r"(?m)^port = 9000$")
        self.assertEqual(len(new.splitlines()), len(DIST.splitlines()))
        self.assertNotIn("Docker containers list", new)

    def test_comment_on_a_changed_line_stays(self):
        new = self.migrated(set_old(OLD, "web", "port", "9000  # moved: 8787 is taken by grafana"))
        self.assertRegex(new, r"(?m)^port = 9000\s+# moved: 8787 is taken by grafana$")

    def test_ui_lines_and_the_webapps_and_expose_lines_stay_as_written(self):
        old = add_to(add_to(add_to(OLD, "ui", "theme = dark"), "webapps", "ethibid = 8180, 8543   # the shop"), "expose", "n8n     = tailnet")
        old = add_to(old, "ui", "density = auto   # same as the default, still a set key")
        new = self.migrated(old)
        for line in ("theme = dark", "ethibid = 8180, 8543   # the shop", "n8n     = tailnet", "density = auto   # same as the default, still a set key"):
            self.assertIn("\n" + line + "\n", new)

    def test_legacy_and_unknown_keys_are_kept_with_a_marker(self):
        old = add_to(OLD, "ui", "web = classic")
        old = add_to(old, "web", "refresh_seconds = 5   # slow link")
        old = add_to(old, "web", "tls_cert = /etc/x.pem")
        old += "\n[experimental]\nflag = on\n"
        new = self.migrated(old)
        marker = confmigrate.LEGACY + "\n"
        for line in ("refresh_seconds = 5   # slow link", "tls_cert = /etc/x.pem", "flag = on"):
            self.assertIn(marker + line + "\n", new)
        self.assertIn("[experimental]", new)
        self.assertIn("\nweb = classic\n", new)  # a key of [ui] the page still honours: carried, not legacy
        self.assertEqual(self.cfg()["ui"]["web"], "classic")

    def test_second_run_is_already_current_and_changes_nothing(self):
        new = self.migrated(add_to(set_old(OLD, "web", "port", "9000"), "web", "tls_cert = /etc/x.pem"))
        baks = self.backups()
        rc, out = self.run_cmd(yes=True)
        self.assertEqual((rc, self.read(), self.backups()), (0, new, baks))
        self.assertIn("already current", out)
        # and the layout line is what says so: the rebuild of a migrated file is the same file
        self.assertEqual(confmigrate.build(new, DIST)[0], new)

    def test_backup_is_a_copy_of_the_old_file_mode_0600(self):
        self.put(OLD)
        self.run_cmd(yes=True)
        (bak,) = self.backups()
        self.assertRegex(bak, r"^config\.ini\.bak-\d{8}-\d{6}$")
        with open(os.path.join(self.dir, bak), encoding="utf-8") as f:
            self.assertEqual(f.read(), OLD)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, bak)).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)

    def test_dry_run_prints_a_diff_and_changes_nothing(self):
        self.put(OLD)
        rc, out = self.run_cmd(dry_run=True)
        self.assertEqual((rc, self.read(), self.backups()), (0, OLD, []))
        self.assertIn("--- config.ini", out)
        self.assertIn("+++ config.ini (migrated)", out)
        self.assertIn("-containers      = yes   # Docker containers list", out)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".config.ini.")], [])

    def test_without_yes_it_asks_and_no_answer_changes_nothing(self):
        self.put(OLD)
        for answer in ("n", ""):
            lines = []
            rc = confmigrate.migrate(self.path, out=lines.append, ask=lambda _: answer)
            self.assertEqual((rc, self.read(), self.backups()), (1, OLD, []))

        def eof(_):
            raise EOFError
        self.assertEqual(confmigrate.migrate(self.path, out=lambda _: None, ask=eof), 1)
        self.assertEqual(self.read(), OLD)
        self.assertEqual(confmigrate.migrate(self.path, out=lambda _: None, ask=lambda _: "y"), 0)
        self.assertEqual(self.read(), DIST)

    def test_a_broken_file_is_refused_and_left_alone(self):
        broken = OLD + "\n[ui\nkey\n"
        self.put(broken)
        rc, out = self.run_cmd(yes=True)
        self.assertEqual((rc, self.read(), self.backups()), (1, broken, []))
        self.assertIn("fix it by hand", out)

    def test_a_change_of_the_configuration_in_force_keeps_the_old_file(self):
        self.put(OLD)
        real = confmigrate.build
        # a layout that drops a setting: the check must catch it whatever the cause
        confmigrate.build = lambda old, dist: (real(set_old(old, "web", "port", "1234"), dist)[0], 0)
        self.addCleanup(setattr, confmigrate, "build", real)
        rc, out = self.run_cmd(yes=True)
        self.assertEqual((rc, self.read(), self.backups()), (1, OLD, []))
        self.assertIn("web", out)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".config.ini.")], [])

    def test_effective_configuration_equal_for_many_shapes(self):
        shapes = [set_old(OLD, "features", k, "no") for k in ("containers", "ai", "fail2ban", "thermal")]
        shapes.append(add_to(add_to(OLD, "webapps", "app = 8080, 8081"), "expose", "8080/udp = lan"))
        shapes.append(set_old(set_old(OLD, "ai", "enabled", "yes"), "ai", "model", "qwen3-8b"))
        for text in shapes:
            self.migrated(text)
            for b in self.backups():
                os.remove(os.path.join(self.dir, b))

    def test_symlink_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        real = os.path.join(self.dir, "real.ini")
        with open(real, "w", encoding="utf-8") as f:
            f.write(OLD)
        os.symlink(real, self.path)
        with open(real + ".dist", "w", encoding="utf-8") as f:
            f.write(DIST)
        rc, out = self.run_cmd(yes=True)
        self.assertEqual(rc, 1)
        self.assertIn("symbolic link", out)
        self.assertTrue(os.path.islink(self.path))
        with open(real, encoding="utf-8") as f:
            self.assertEqual(f.read(), OLD)

    def test_a_planted_link_under_a_guessable_name_is_not_written_through(self):
        self.put(OLD)
        victim = os.path.join(self.dir, "victim")
        with open(victim, "w") as f:
            f.write("keep")
        for name in ("config.ini.migrating", "config.ini.tmp"):
            os.symlink(victim, os.path.join(self.dir, name))
        rc, _ = self.run_cmd(yes=True)
        self.assertEqual(rc, 0)
        with open(victim) as f:
            self.assertEqual(f.read(), "keep")
        self.assertFalse(os.path.islink(self.path))

    def test_two_runs_in_the_same_second_keep_both_backups(self):
        with mock.patch("confmigrate.time.strftime", return_value="20260101-000000"):
            self.put(OLD)
            self.assertEqual(self.run_cmd(yes=True)[0], 0)
            self.put(OLD)
            self.assertEqual(self.run_cmd(yes=True)[0], 0)
        self.assertEqual(sorted(self.backups()), ["config.ini.bak-20260101-000000", "config.ini.bak-20260101-000000-2"])

    def test_missing_file_or_dist(self):
        rc, out = self.run_cmd(yes=True)
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", out)
        self.put(OLD)
        os.remove(self.path + ".dist")
        self.assertEqual(self.run_cmd(yes=True)[0], 1)
        self.assertEqual(self.read(), OLD)

    def test_notice_only_for_the_old_layout(self):
        self.put(OLD)
        self.assertIn("nuc-console-config migrate", confmigrate.notice(self.path))
        self.run_cmd(yes=True)
        self.assertEqual(confmigrate.notice(self.path), "")
        self.assertEqual(confmigrate.notice(os.path.join(self.dir, "nope.ini")), "")

    def test_main_uses_config_and_dry_run(self):
        self.put(OLD)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = confmigrate.main(["migrate", "--dry-run", "--config", self.path])
        self.assertEqual((rc, self.read()), (0, OLD))
        self.assertIn("dry run", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            confmigrate.main(["notice", "--config", self.path])
        self.assertIn("old layout", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
