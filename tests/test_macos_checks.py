"""macOS checks tested on any OS through fakes: kernel panics in the event history, memory pressure (sysctl level) and software
updates (com.apple.SoftwareUpdate.plist). Nothing here reads the machine running the tests."""
import datetime
import os
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import collect_darwin as cmac  # noqa: E402
import collector  # noqa: E402
import hostdata  # noqa: E402
import problems  # noqa: E402

NOW = 1_800_000_000
DAY = 86400


class Panics(unittest.TestCase):
    P = staticmethod(cmac.parse_report_head)
    PANIC = ("unexpected_shutdown", "kernel panic", "panic report")

    def test_names_and_headers(self):
        self.assertEqual(self.P(b"panic(cpu 0 caller 0xfff): secret string\nBacktrace", "Kernel_2026-10-01-1200_host.panic"), self.PANIC)
        self.assertEqual(self.P(b'{"bug_type":"210","os_version":"macOS 15"}\n{"panicString":"x"}', "Kernel-2026-10-01-120000.ips"), self.PANIC)
        self.assertEqual(self.P(b'{"bug_type":"210"}\n', "whatever.ips"), self.PANIC)
        self.assertEqual(self.P(b"{broken", "Kernel_2026-10-01.ips"), self.PANIC)
        self.assertEqual(self.P(b"", "panic-full-2026-10-01.ips"), self.PANIC)
        self.assertIsNone(self.P(b'{"app_name":"x","bug_type":"385"}\n', "x.ips"))  # stackshots stay out

    def test_body_never_leaves(self):
        got = self.P(b'{"bug_type":"210"}\n{"panicString":"user alice at 10.1.2.3"}', "Kernel.ips")
        self.assertNotIn("alice", repr(got))
        self.assertNotIn("10.1.2.3", repr(got))

    def test_scan_reads_panic_files(self):
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("Kernel_2026-10-01.panic", b"panic(cpu 0): x\n"), ("Kernel-2026.ips", b'{"bug_type":"210"}\n'),
                               ("Safari.ips", b'{"app_name":"Safari","bug_type":"309"}\n')):
                p = os.path.join(d, name)
                with open(p, "wb") as f:
                    f.write(body)
                os.utime(p, (NOW - 100, NOW - 100))
            evs, _ = cmac.diag_events(NOW - 1000, dirs=(d,), now=NOW)
        self.assertEqual(sorted((e["kind"], e["subject"], e["n"]) for e in evs),  # same kind and subject: merged into one row with n=2
                         [("crash", "Safari", 1), ("unexpected_shutdown", "kernel panic", 2)])


class Pressure(unittest.TestCase):
    def test_levels(self):
        for level, want in ((1, None), (2, 1), (4, 2), (3, None), (0, None)):
            th = {"cpu": (40.0, 100.0), "mac_pressure": hostdata.mac_pressure(lambda n, v=level: v)}
            got = problems.problems_raw(None, None, now=NOW, thermal=th)
            ids = [(s, i) for s, _, i in got if i == "memory-pressure"]
            self.assertEqual(ids, [(want, "memory-pressure")] if want else [], level)

    def test_unreadable_is_unknown(self):
        def boom(name):
            raise OSError(2, "no such sysctl")
        self.assertIsNone(hostdata.mac_pressure(boom))
        self.assertIsNone(hostdata.mac_pressure(lambda n: None))
        th = {"cpu": (40.0, 100.0), "mac_pressure": None}
        self.assertNotIn("memory-pressure", [i for _, _, i in problems.problems_raw(None, None, now=NOW, thermal=th)])
        self.assertEqual(hostdata.mac_pressure(lambda n: self.assertEqual(n, "kern.memorystatus_vm_pressure_level") or 2), 2)

    def test_not_on_other_os(self):
        with mock.patch.object(hostdata.nuc_config, "MACOS", False):
            self.assertIsNone(hostdata.mac_pressure())

    def test_wording(self):
        self.assertIn("Activity Monitor", problems.OS_CATALOG["darwin"]["memory-pressure"][2])


class Updates(unittest.TestCase):
    def read(self, data, raw=None):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "su.plist")
            with open(p, "wb") as f:
                f.write(raw if raw is not None else plistlib.dumps(data))
            return collector.collect_updates_darwin(p)

    def test_count_and_date(self):
        when = datetime.datetime(2026, 9, 1, 12, 0, 0)
        got = self.read({"LastRecommendedUpdatesAvailable": 3, "LastSuccessfulDate": when})
        self.assertEqual((got["pending"], got["os"]), (3, "darwin"))
        self.assertEqual(got["checked"], int(when.replace(tzinfo=datetime.timezone.utc).timestamp()))
        self.assertEqual(self.read({"LastRecommendedUpdatesAvailable": 2}, plistlib.dumps({"LastRecommendedUpdatesAvailable": 2}, fmt=plistlib.FMT_BINARY))["pending"], 2)

    def test_tolerance(self):
        self.assertEqual(self.read({})["pending"], None)
        for junk in ("3", True, -1, 1.5, [1]):
            self.assertIsNone(self.read({"LastRecommendedUpdatesAvailable": junk})["pending"], repr(junk))
        self.assertIsNone(self.read({"LastSuccessfulDate": "yesterday"})["checked"])
        with self.assertRaises(RuntimeError):
            self.read(None, raw=b"not a plist")
        with self.assertRaises(RuntimeError):
            self.read(["a list"])

    def test_missing_and_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(collector.Absent):
                collector.collect_updates_darwin(os.path.join(d, "none.plist"))
            with mock.patch("builtins.open", side_effect=PermissionError(1, "Operation not permitted")):
                with self.assertRaises(RuntimeError):  # TCC: unreadable, never ok
                    collector.collect_updates_darwin(os.path.join(d, "x"))

    def test_problem(self):
        hp = lambda up: problems.hygiene_problems({"updates": up}, NOW)  # noqa: E731
        got = hp({"os": "darwin", "pending": 2, "checked": NOW - DAY})
        self.assertEqual([(s, i) for s, _, i in got], [(1, "updates-pending")])
        self.assertIn("2 software updates pending", got[0][1])
        self.assertNotIn("has not run", got[0][1])
        self.assertIn("has not run for 20 days", hp({"os": "darwin", "pending": 1, "checked": NOW - 20 * DAY})[0][1])
        for up in ({"os": "darwin", "pending": 0, "checked": NOW}, {"os": "darwin", "pending": None, "checked": None},
                   {"os": "darwin", "pending": "2"}, {"os": "darwin", "pending": True}, {"os": "darwin"}):
            self.assertEqual(hp(up), [], up)
        self.assertEqual([i for _, _, i in hp({"security": 3, "total": 14})], ["updates-pending"])  # Linux shape unchanged

    def test_boot_section_on_macos(self):
        self.assertNotIn("updates", collector.UNSUPPORTED_BOOT_BY_OS["darwin"])
        self.assertIn("updates", collector.UNSUPPORTED_BOOT_BY_OS["windows"])
        with mock.patch.object(collector, "OS_NAME", "darwin"), \
                mock.patch.object(collector, "collect_updates_darwin", return_value={"os": "darwin", "pending": 1, "checked": None}):
            self.assertEqual(collector.collect_updates()["pending"], 1)
        self.assertIn("Software updates", problems.OS_CATALOG["darwin"]["updates-pending"][0])


if __name__ == "__main__":
    unittest.main()
