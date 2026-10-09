"""The guard of tests/hermetic.py: the suite does not read the state of the machine it runs on.

Named test_zz_* so that discovery runs it after every other module: the last test fails if a read of the host's state was swallowed
somewhere (a thread, a bare `except BaseException`) instead of failing the test that made it.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402

HOST = os.path.join(hermetic.HOST_DIRS[0], "anything")


class Guard(unittest.TestCase):
    def tripped(self, fn, *a):
        before = len(hermetic.VIOLATIONS)
        try:
            with self.assertRaises(hermetic.HostStateRead):
                fn(*a)
        finally:
            del hermetic.VIOLATIONS[before:]  # on purpose: not a finding of the last test

    def test_the_host_folders_of_the_console_are_trapped_whatever_the_call(self):
        for d in hermetic.HOST_DIRS:
            for fn in (open, os.stat, os.listdir, os.scandir, sqlite3.connect, os.path.exists, os.path.isfile):
                self.tripped(fn, os.path.join(d, "x"))
        self.tripped(os.listdir, hermetic.HOST_DIRS[0])

    def test_the_commands_that_ask_the_machine_are_trapped(self):
        import subprocess
        for cmd in (["loginctl", "list-sessions"], ["/usr/bin/ss", "-tn"], ["docker", "ps"]):
            self.tripped(subprocess.run, cmd)

    def test_a_read_in_a_try_except_is_not_turned_into_no_file(self):
        def swallowing():
            try:
                open(HOST)
            except Exception:  # noqa: BLE001
                return "swallowed"
            except OSError:
                return "swallowed"
        self.tripped(swallowing)

    def test_the_other_paths_are_untouched(self):
        self.assertTrue(os.path.isdir(os.path.dirname(os.path.abspath(__file__))))
        self.assertFalse(os.path.exists(os.path.join(hermetic.ABSENT, "x")))
        self.assertEqual(hermetic.HOST_DIRS[0].startswith(hermetic.SCRATCH), False)

    def test_no_state_variable_of_the_environment_points_into_the_host(self):
        for k, v in os.environ.items():
            if k.startswith("NUC_CONSOLE_"):
                self.assertFalse(any(os.path.abspath(v).startswith(d) for d in hermetic.HOST_DIRS), "%s=%s" % (k, v))
        for k in ("COLUMNS", "LINES", "NO_COLOR", "NUC_CONSOLE_HOME"):
            self.assertNotIn(k, os.environ)
        self.assertTrue(os.environ["HOME"].startswith(hermetic.SCRATCH))

    def test_aisetup_and_history_do_not_look_in_the_hosts_folders(self):
        import aisetup
        import history
        self.assertTrue(aisetup.find_dir().startswith(hermetic.SCRATCH) or aisetup.find_dir().startswith(os.environ["HOME"]))
        self.assertIsNone(history.open_ro())


    def test_a_screen_built_by_hand_sees_no_accepted_problem_baseline_or_config_of_the_host(self):
        import render
        self.assertEqual(render.load_accepted(), {})          # accepted.json hid the boot errors and added "1 accepted as known"
        self.assertIsNone(render.load_baseline())
        self.assertEqual(render.load_json(render.NET_STATE), None)
        self.assertTrue(render.ACCEPTED_PATH.startswith(hermetic.SCRATCH) and render.BASELINE.startswith(hermetic.SCRATCH))


class ZLast(unittest.TestCase):
    def test_zz_no_test_of_the_run_read_the_hosts_state(self):
        self.assertEqual(hermetic.VIOLATIONS, [], "read the state of the host (a read swallowed instead of failing its test)")


if __name__ == "__main__":
    unittest.main()
