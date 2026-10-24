import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import nuc_config  # noqa: E402
import problems  # noqa: E402
import wake  # noqa: E402

VT = {"TERM": "linux"}


def pl(*pairs):
    """A ProblemList of (id, severity)."""
    out = problems.ProblemList()
    out.pids = []
    for pid, sev in pairs:
        out.append((sev, pid))
        out.pids.append(pid)
    return out


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class Transitions(unittest.TestCase):
    def make(self, enabled=True):
        self.clock = Clock()
        self.calls = []
        w = wake.Waker(enabled, 5, self.clock, VT)
        w.enabled = enabled  # the VT check is tested apart: here only the transitions
        patcher = mock.patch.object(wake, "unblank", lambda fd: self.calls.append(fd) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return w

    def test_first_frame_never_wakes(self):
        w = self.make()
        self.assertFalse(w.feed(pl(("a", 3))))
        self.assertEqual(self.calls, [])

    def test_new_id_wakes_same_does_not(self):
        w = self.make()
        w.feed(pl(("a", 2)))
        self.assertFalse(w.feed(pl(("a", 2))))
        self.clock.t += 31
        self.assertTrue(w.feed(pl(("a", 2), ("b", 1))))
        self.assertEqual(self.calls, [5])

    def test_severity_rise_wakes_fall_does_not(self):
        w = self.make()
        w.feed(pl(("a", 1)))
        self.assertTrue(w.feed(pl(("a", 3))))
        self.clock.t += 31
        self.assertFalse(w.feed(pl(("a", 1))))

    def test_gone_and_back_is_new(self):
        w = self.make()
        w.feed(pl(("a", 2)))
        w.feed(pl())
        self.assertTrue(w.feed(pl(("a", 2))))

    def test_severity_zero_ignored(self):
        w = self.make()
        w.feed(pl())
        self.assertFalse(w.feed(pl(("a", 0))))

    def test_rate_limit_and_state_still_tracked(self):
        w = self.make()
        w.feed(pl())
        self.assertTrue(w.feed(pl(("a", 2))))
        self.clock.t += 10
        self.assertFalse(w.feed(pl(("a", 2), ("b", 2))))  # inside 30 s: dropped
        self.clock.t += 30
        self.assertFalse(w.feed(pl(("a", 2), ("b", 2))))  # b is no longer new
        self.assertTrue(w.feed(pl(("a", 2), ("b", 2), ("c", 1))))
        self.assertEqual(len(self.calls), 2)

    def test_list_without_ids_changes_nothing(self):
        w = self.make()
        w.feed(pl(("a", 2)))
        self.assertFalse(w.feed([(2, "map could not be built")]))
        self.assertFalse(w.feed(pl(("a", 2))))

    def test_disabled_never_calls(self):
        w = self.make(enabled=False)
        w.feed(pl())
        self.assertFalse(w.feed(pl(("a", 3))))
        self.assertEqual(self.calls, [])

    def test_accepted_and_muted_are_not_in_the_list(self):
        # problems.problems() leaves them out of the list (and of .pids): nothing new, nothing woken
        w = self.make()
        w.feed(pl())
        with mock.patch.object(problems, "problems_raw", return_value=[(3, "x", "thermal")]), \
                mock.patch.object(problems, "muted_ids", return_value={"thermal"}):
            self.assertFalse(w.feed(problems.problems()))


class Ioctl(unittest.TestCase):
    def test_exact_request_and_oserror_swallowed(self):
        fake = mock.Mock()  # a stand-in module: fcntl does not exist on Windows and macOS has no TIOCLINUX, the logic is the same everywhere
        with mock.patch.object(wake, "fcntl", fake):
            self.assertTrue(wake.unblank(7))
            fake.ioctl.assert_called_once_with(7, wake.TIOCLINUX, bytes([4]))
            self.assertEqual(wake.TIOCLINUX, 0x541C)
        fake = mock.Mock()
        fake.ioctl.side_effect = PermissionError
        with mock.patch.object(wake, "fcntl", fake):
            self.assertFalse(wake.unblank(7))
        with mock.patch.object(wake, "fcntl", None):  # Windows: no module at all
            self.assertFalse(wake.unblank(7))

    def test_vt_only(self):
        with mock.patch.object(wake, "fcntl", mock.Mock()):  # Windows has no fcntl; the check is about the terminal, not the module
            self._vt_only()

    def _vt_only(self):
        with mock.patch.object(nuc_config, "LINUX", True), mock.patch.object(os, "isatty", return_value=True):
            self.assertTrue(wake.is_vt(3, {"TERM": "linux"}))
            self.assertFalse(wake.is_vt(3, {"TERM": "xterm-256color"}))
            self.assertFalse(wake.is_vt(-1, {"TERM": "linux"}))
        with mock.patch.object(nuc_config, "LINUX", True), mock.patch.object(os, "isatty", return_value=False):
            self.assertFalse(wake.is_vt(3, {"TERM": "linux"}))
        with mock.patch.object(nuc_config, "LINUX", False), mock.patch.object(os, "isatty", return_value=True):
            self.assertFalse(wake.is_vt(3, {"TERM": "linux"}))

    def test_not_a_vt_no_ioctl(self):
        with mock.patch.object(wake, "is_vt", return_value=False), mock.patch.object(wake, "fcntl", mock.Mock()) as fake:
            w = wake.Waker(True, 3)
            w.feed(pl())
            self.assertFalse(w.feed(pl(("a", 3))))
            fake.ioctl.assert_not_called()


class Config(unittest.TestCase):
    def test_default_and_off(self):
        import tempfile
        self.assertTrue(nuc_config.load(path="/nonexistent/x.ini", overlay=False)["console"]["wake_on_problem"])
        with tempfile.TemporaryDirectory() as d:  # closed before it is read: Windows refuses a second open of a file held for writing
            path = os.path.join(d, "x.ini")
            with open(path, "w") as f:
                f.write("[console]\nwake_on_problem = no\n")
            self.assertFalse(nuc_config.load(path=path, overlay=False)["console"]["wake_on_problem"])


if __name__ == "__main__":
    unittest.main()
