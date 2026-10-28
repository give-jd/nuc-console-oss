import os
import re
import subprocess
import sys
import tempfile
import threading
import time
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
        self.have, self.rc, self.calls, self.stdin = have, rc, [], []

    def which(self, name):
        return "/usr/bin/" + name if name in self.have else None

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        self.stdin.append(kw.get("stdin"))
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
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(nuc_config, "RUN_DIR", tmp.name)  # the marker of an earlier start lives here, not in the host's /run
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mark = os.path.join(tmp.name, "ttyprep-blank")
        self.fds = []
        self.fb = os.path.join(tmp.name, "graphics")
        os.makedirs(os.path.join(self.fb, "fb0"))
        with open(os.path.join(self.fb, "fb0", "blank"), "w") as fh:
            fh.write("4\n")
        patcher = mock.patch.object(ttyprep, "FB_DIR", self.fb)  # not the host's sysfs
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ttyprep, "open_tty", lambda tty: self.fds.append(tty) or os.open(os.devnull, os.O_RDONLY))  # the tty itself is root's
        patcher.start()
        self.addCleanup(patcher.stop)

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
                ["/usr/bin/setterm", "--term", "linux", "--blank", "10", "--powerdown", "10", "--file", "/dev/tty3"],
                ["/usr/bin/setterm", "--term", "linux", "--blank", "poke"]]
        self.assertEqual(f.calls, once + once)  # idempotent: the same commands, nothing accumulates

    def test_zero_after_a_number_takes_the_timer_back_and_wakes_the_screen_once(self):
        f = Fake()
        go("[console]\nblank_minutes = 1\n", f)
        self.assertTrue(os.path.exists(self.mark))
        f.calls.clear()
        go("[console]\nblank_minutes = 0\n", f)
        self.assertEqual(f.calls, [["/usr/bin/setterm", "--term", "linux", "--blank", "0", "--powerdown", "0", "--file", "/dev/tty3"],
                                   ["/usr/bin/setterm", "--term", "linux", "--blank", "poke"]])
        self.assertFalse(os.path.exists(self.mark))
        self.assertNotEqual(f.stdin[-1], subprocess.DEVNULL, "poke acts on its standard input: the tty, not /dev/null")
        f.calls.clear()
        go("[console]\nblank_minutes = 0\n", f)
        self.assertEqual(f.calls, [], "and then it leaves the tty alone again")

    def test_the_framebuffer_left_in_powerdown_is_brought_back(self):
        blank = os.path.join(self.fb, "fb0", "blank")
        go("[console]\nblank_minutes = 1\n", Fake())
        with open(blank) as fh:
            self.assertEqual(fh.read(), "0", "fb0/blank was 4 (powerdown): a start that sets blanking leaves the screen on")

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



class StuckDisplay(unittest.TestCase):
    """A wedged display driver (i915 flip_done timeouts): a tty ioctl does not return, and the child survives SIGKILL, so subprocess.run does not
    return either. The dashboard must still start: one deadline for all the steps, and no step after one that did not answer."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for obj, name, value in ((nuc_config, "RUN_DIR", tmp.name), (ttyprep, "DEADLINE_S", 0.3), (ttyprep, "JOIN_SLACK_S", 0.2)):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.release = threading.Event()
        self.addCleanup(self.release.set)  # the abandoned thread ends with the test
        self.calls = []

    def hang(self, cmd, **kw):
        self.calls.append(cmd)
        self.release.wait(30)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    def go(self, ini, fn):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.ini")
            with open(path, "w") as fh:
                fh.write(ini)
            say = []
            with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": path}), mock.patch("shutil.which", lambda n: "/usr/bin/" + n), \
                    mock.patch("subprocess.run", fn), mock.patch.object(nuc_config, "LINUX", True):
                t0 = time.monotonic()
                rc = ttyprep.main(env={"NUC_CONSOLE_VT": "3"}, say=say.append)
                return rc, say, time.monotonic() - t0

    def test_a_step_that_never_returns_ends_the_run_at_the_deadline_and_skips_the_rest(self):
        rc, say, took = self.go("[console]\nfont = x\nblank_minutes = 5\n", self.hang)
        self.assertEqual(rc, 0)
        self.assertLess(took, 3.0, "the old behaviour waited as long as the driver did")
        self.assertEqual([c[0] for c in self.calls], ["/usr/bin/setfont"], "setterm was never started")
        self.assertTrue(any("setfont did not come back" in s and "other steps are skipped" in s for s in say), say)

    def test_a_timeout_that_does_return_also_skips_the_following_steps(self):
        calls = []

        def timing_out(cmd, **kw):
            calls.append(cmd)
            raise subprocess.TimeoutExpired(cmd, 10)
        rc, say, took = self.go("[console]\nfont = x\nblank_minutes = 5\n", timing_out)
        self.assertEqual((rc, [c[0] for c in calls]), (0, ["/usr/bin/setfont"]))
        self.assertTrue(any("setfont" in s and "timed out" in s for s in say), say)
        self.assertTrue(any("setterm" in s and "skipped" in s for s in say), say)

    def test_the_framebuffer_is_not_touched_once_the_tty_is_stuck(self):
        ttyprep._state.update(stuck=True)
        self.addCleanup(ttyprep._state.update, stuck=False, end=None)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "fb0"))
            blank = os.path.join(d, "fb0", "blank")
            with open(blank, "w") as fh:
                fh.write("4")
            ttyprep.unblank_fb(lambda _l: None, base=d)
            with open(blank) as fh:
                self.assertEqual(fh.read(), "4")

    def test_a_stuck_run_does_not_leak_into_the_next_start(self):
        self.go("[console]\nfont = x\n", self.hang)
        self.release.set()
        ok = Fake()
        calls_before = len(ok.calls)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.ini")
            with open(path, "w") as fh:
                fh.write("[console]\nfont = x\n")
            with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": path}), mock.patch("shutil.which", ok.which), \
                    mock.patch("subprocess.run", ok.run), mock.patch.object(nuc_config, "LINUX", True):
                self.assertEqual(ttyprep.main(env={"NUC_CONSOLE_VT": "3"}, say=lambda _l: None), 0)
        self.assertEqual(len(ok.calls), calls_before + 1, "the font was set: nothing carried over from the stuck run")

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
        self.assertEqual(cfg["console"], {"font": "", "blank_minutes": 0, "wake_on_problem": True, "remote_idle_seconds": 60})


if __name__ == "__main__":
    unittest.main()
