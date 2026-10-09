import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(ROOT, "src"))
import nuc_config  # noqa: E402
import ttyprep  # noqa: E402


class Fake:
    """Stands for the tools: no real setfont or setterm runs. have: the names that 'exist'; rc: their exit status."""

    def __init__(self, have=("setfont", "setterm"), rc=0):
        self.have, self.rc, self.calls = have, rc, []

    def which(self, name):
        return "/usr/bin/" + name if name in self.have else None

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.rc, b"", b"boom")


def go(ini, fake, env=None):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        if ini is not None:
            with open(path, "w") as fh:
                fh.write(ini)
        say = []
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": path}), mock.patch("shutil.which", fake.which), \
                mock.patch("subprocess.run", fake.run), mock.patch.object(nuc_config, "LINUX", True):
            rc = ttyprep.main(env={"NUC_CONSOLE_VT": "3"} if env is None else env, say=say.append)
    return rc, say


class Console(unittest.TestCase):
    def test_off_by_default_nothing_runs(self):
        for ini in (None, "[console]\n", "[console]\nfont =\nblank_minutes = 0\n"):
            f = Fake()
            self.assertEqual(go(ini, f), (0, []))
            self.assertEqual(f.calls, [])

    def test_font_and_blanking_on_the_chosen_tty_and_twice_the_same(self):
        ini = "[console]\nfont = Lat15-TerminusBold32x16\nblank_minutes = 10\n"
        f = Fake()
        go(ini, f)
        go(ini, f)
        once = [["/usr/bin/setfont", "-C", "/dev/tty3", "Lat15-TerminusBold32x16"],
                ["/usr/bin/setterm", "--term", "linux", "--blank", "10", "--powerdown", "10", "--file", "/dev/tty3"]]
        self.assertEqual(f.calls, once + once)  # idempotent: the same commands, nothing accumulates

    def test_missing_tools_are_skipped_not_errors(self):
        f = Fake(have=())
        rc, say = go("[console]\nfont = x\nblank_minutes = 5\n", f)
        self.assertEqual((rc, f.calls, len(say)), (0, [], 2))

    def test_a_failing_tool_or_a_missing_binary_at_run_time_is_reported_and_the_exit_is_0(self):
        rc, say = go("[console]\nfont = nosuchfont\n", Fake(rc=1))
        self.assertEqual(rc, 0)
        self.assertIn("setfont failed: boom", say[0])
        with mock.patch("shutil.which", return_value="/usr/bin/setfont"), mock.patch("subprocess.run", side_effect=OSError("gone")):
            self.assertFalse(ttyprep.run(["setfont", "x"], lambda _l: None))

    def test_bad_values_keep_it_off_with_a_warning(self):
        for ini in ("blank_minutes = 61", "blank_minutes = soon", "blank_minutes = -1", "font = ../../etc/x", "font = a b"):
            f = Fake()
            rc, say = go("[console]\n" + ini + "\n", f)
            self.assertEqual((rc, f.calls), (0, []), ini)
            self.assertTrue(say and "[console]" in say[0], ini)

    def test_a_bad_vt_sets_nothing(self):
        f = Fake()
        rc, say = go("[console]\nfont = x\n", f, env={"NUC_CONSOLE_VT": "../tty"})
        self.assertEqual((rc, f.calls), (0, []))

    def test_other_systems_do_nothing(self):
        f = Fake()
        with mock.patch.object(nuc_config, "LINUX", False), mock.patch("subprocess.run", f.run):
            self.assertEqual(ttyprep.main(env={}, say=print), 0)
        self.assertEqual(f.calls, [])


class Wiring(unittest.TestCase):
    def test_unit_runs_it_as_root_and_failure_is_not_fatal_and_installer_gives_the_vt(self):
        with open(os.path.join(ROOT, "systemd", "nuc-console.service")) as fh:
            unit = fh.read()
        self.assertRegex(unit, r"(?m)^ExecStartPre=-\+/usr/bin/python3 /opt/nuc-console/ttyprep\.py$")
        self.assertLess(unit.index("ExecStartPre"), unit.index("ExecStart=/usr"))
        with open(os.path.join(ROOT, "install.sh")) as fh:
            inst = fh.read()
        self.assertIn('echo "Environment=NUC_CONSOLE_VT=$VT"', inst)
        self.assertNotRegex(inst, r"setfont|setterm|consoleblank|grub")  # the installer never touches fonts or the bootloader itself

    def test_the_shipped_config_leaves_both_off(self):
        with mock.patch.dict(os.environ, {}):
            cfg = nuc_config.load(os.path.join(ROOT, "config", "config.ini"), warn=lambda l: self.fail(l))
        self.assertEqual(cfg["console"], {"font": "", "blank_minutes": 0})


if __name__ == "__main__":
    unittest.main()
