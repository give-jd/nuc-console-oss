"""Windows checks that need no PowerShell: the restart-pending registry signals, the commit charge, the event ids.

Hermetic and portable: `winreg` and the Windows DLL are stand-ins, so these run unchanged on Linux, macOS and Windows."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import collect_windows as cwin  # noqa: E402
import collector  # noqa: E402
import hostdata  # noqa: E402
import hostinfo  # noqa: E402
import nuc_config  # noqa: E402
import problems  # noqa: E402
import render  # noqa: E402
import winapi  # noqa: E402

NOW = 1_800_000_000
WU, CBS, SM = (p for _n, p, _v in cwin.REBOOT_SIGNALS)


class Handle:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeReg:
    """A stand-in `winreg`: `keys` maps a key path to {value: data}; a path in `denied` raises PermissionError."""
    HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_64KEY = "HKLM", 1, 2

    def __init__(self, keys=None, denied=()):
        self.keys, self.denied = keys or {}, set(denied)

    def OpenKey(self, root, path, reserved, access):  # noqa: N802 - the real name
        assert root == "HKLM" and access & 2, "64-bit view of HKLM"
        if path in self.denied:
            raise PermissionError(5, "denied")
        if path not in self.keys:
            raise FileNotFoundError(2, "not found")
        h = Handle()
        h.path = path
        return h

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.keys[key.path]:
            raise FileNotFoundError(2, "no value")
        return self.keys[key.path][name], 7


class RebootPending(unittest.TestCase):
    def test_no_signal_is_a_clear_no(self):
        self.assertEqual(cwin.reboot_pending(FakeReg({SM: {}})), {"flag": False, "sources": []})
        self.assertEqual(cwin.reboot_pending(FakeReg()), {"flag": False, "sources": []})

    def test_each_signal_alone(self):
        self.assertEqual(cwin.reboot_pending(FakeReg({WU: {}}))["sources"], ["Windows Update"])
        self.assertEqual(cwin.reboot_pending(FakeReg({CBS: {}}))["sources"], ["servicing"])
        got = cwin.reboot_pending(FakeReg({SM: {"PendingFileRenameOperations": ["\\??\\C:\\a", "", "\\??\\C:\\b", ""]}}))
        self.assertEqual(got, {"flag": True, "sources": ["files to replace"]})

    def test_an_empty_file_rename_list_is_not_a_signal(self):
        for data in ([], [""], ["", ""], None):
            self.assertFalse(cwin.reboot_pending(FakeReg({SM: {"PendingFileRenameOperations": data}}))["flag"], data)

    def test_all_three_and_no_value_content_is_kept(self):
        got = cwin.reboot_pending(FakeReg({WU: {}, CBS: {}, SM: {"PendingFileRenameOperations": ["C:\\secret\\file"]}}))
        self.assertEqual(got, {"flag": True, "sources": ["Windows Update", "servicing", "files to replace"]})
        self.assertNotIn("secret", repr(got))

    def test_an_unreadable_signal_is_unknown_never_a_no(self):
        self.assertIsNone(cwin.reboot_pending(FakeReg({}, denied=[WU]))["flag"])
        self.assertIs(cwin.reboot_pending(FakeReg({CBS: {}}, denied=[WU]))["flag"], True)  # another one says yes: yes

    def test_the_boot_collector_writes_the_section_on_windows(self):
        self.assertNotIn("reboot", collector.UNSUPPORTED_BOOT_BY_OS["windows"])
        self.assertIn("reboot", collector.UNSUPPORTED_BOOT_BY_OS["darwin"])

    def test_hygiene_problem_has_windows_wording_and_linux_is_unchanged(self):
        got = problems.hygiene_problems({"reboot": {"flag": True, "sources": ["Windows Update", "servicing"]}}, NOW)
        self.assertEqual(got, [(1, "reboot required: Windows asked for one (Windows Update, servicing)", "reboot-required")])
        self.assertEqual(problems.hygiene_problems({"reboot": {"flag": None, "sources": []}}, NOW), [])
        self.assertEqual(problems.hygiene_problems({"reboot": {"flag": False, "sources": []}}, NOW), [])
        self.assertEqual(problems.hygiene_problems({"reboot": {"flag": True, "running": "6", "newest": "6", "kernel_old": False}}, NOW),
                         [(1, "reboot required: the system asked for one (/run/reboot-required)", "reboot-required")])

    def test_the_windows_advice_is_windows(self):
        title, _why, fix = problems.OS_CATALOG["windows"]["reboot-required"]
        self.assertIn("Windows Update", fix)
        self.assertNotIn("sudo", fix)


def fake_dll(total, limit):
    def perf(ref, cb):
        ref._obj.commit_total, ref._obj.commit_limit = total, limit
        return 1
    return mock.Mock(K32GetPerformanceInfo=perf)


class CommitCharge(unittest.TestCase):
    def test_winapi_reads_the_two_figures(self):
        with mock.patch.object(winapi, "_dll", lambda name: fake_dll(900, 1000)):
            self.assertEqual(winapi.commit_charge(), (900, 1000))

    def test_failure_raises_not_zero(self):
        with mock.patch.object(winapi, "_dll", lambda name: mock.Mock(K32GetPerformanceInfo=lambda ref, cb: 0)), \
                mock.patch.object(winapi, "_fail", lambda what: OSError(what), create=True):
            self.assertRaises(OSError, winapi.commit_charge)

    def test_hostinfo_percent_and_fail_soft(self):
        with mock.patch.object(hostinfo, "WINDOWS", True), mock.patch.object(hostinfo, "winapi", create=True) as w:
            w.commit_charge.return_value = (900, 1000)
            self.assertEqual(hostinfo.commit_pct(), 90.0)
            w.commit_charge.return_value = (1, 0)
            self.assertIsNone(hostinfo.commit_pct())
            w.commit_charge.side_effect = OSError("no")
            self.assertIsNone(hostinfo.commit_pct())
            w.commit_charge.side_effect = AttributeError("not windows")
            self.assertIsNone(hostinfo.commit_pct())
        with mock.patch.object(hostinfo, "WINDOWS", False):
            self.assertIsNone(hostinfo.commit_pct())

    def test_read_thermal_carries_it_on_windows_only(self):
        with mock.patch.object(hostdata, "LINUX", False), mock.patch.object(hostdata, "hostinfo", hostinfo, create=True), \
                mock.patch.object(hostinfo, "commit_pct", lambda: 93.5):
            self.assertEqual(hostdata.read_thermal()["commit"], 93.5)

    def raw(self, commit, **cfg):
        th = {"commit": commit}
        with mock.patch.dict(problems.CFG, {"thresholds": dict({"commit_pressure_pct": 90}, **cfg)}):
            return [r for r in problems.problems_raw(None, None, now=NOW, boot=False, thermal=th) if r[2] == "memory-pressure"]

    def test_problem_at_and_above_the_limit(self):
        self.assertEqual(self.raw(89.9), [])
        self.assertEqual(self.raw(None), [])
        for v in (90.0, 99.0):
            got = self.raw(v)
            self.assertEqual([(s, i) for s, _t, i in got], [(1, "memory-pressure")])
            self.assertIn("90 %", got[0][1])
        self.assertEqual(self.raw(95.0, commit_pressure_pct=96), [])
        self.assertEqual(len(self.raw(True)), 0)

    def test_psi_key_is_not_reused(self):
        th = {"psi": None, "commit": 50.0}
        with mock.patch.dict(problems.CFG, {"thresholds": {"memory_pressure_pct": 1, "commit_pressure_pct": 90}}):
            self.assertEqual([r for r in problems.problems_raw(None, None, now=NOW, boot=False, thermal=th) if r[2] == "memory-pressure"], [])

    def test_the_windows_advice(self):
        _t, _w, fix = problems.OS_CATALOG["windows"]["memory-pressure"]
        self.assertIn("commit_pressure_pct", fix)
        self.assertNotIn("/proc", fix)

    def test_config_key(self):
        import tempfile
        said = []
        for text, want in (("[thresholds]\ncommit_pressure_pct = 80\n", 80), ("[thresholds]\ncommit_pressure_pct = 0\n", 90),
                           ("[thresholds]\ncommit_pressure_pct = 101\n", 90), ("[thresholds]\ncommit_pressure_pct = x\n", 90), ("", 90)):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "config.ini")
                with open(path, "w") as f:
                    f.write(text)
                cfg = nuc_config.load(path, warn=said.append)
            self.assertEqual(cfg["thresholds"]["commit_pressure_pct"], want, text)
            self.assertEqual(cfg["thresholds"]["memory_pressure_pct"], 10)
        self.assertEqual(len(said), 3)


def ev(eid, prov, **kw):
    return dict({"Id": eid, "ProviderName": prov, "Arg0": None}, **kw)


class EventIds(unittest.TestCase):
    def test_ids_map_to_existing_kinds(self):
        for e, want in (
                (ev(4101, "Display"), ("hw_error", "display", "Display 4101")),
                (ev(7, "disk"), ("hw_error", "disk", "disk 7")), (ev(11, "disk"), ("hw_error", "disk", "disk 11")),
                (ev(51, "disk"), ("hw_error", "disk", "disk 51")), (ev(153, "disk"), ("hw_error", "disk", "disk 153")),
                (ev(55, "Ntfs"), ("hw_error", "disk", "Ntfs 55")), (ev(140, "Microsoft-Windows-Ntfs"), ("hw_error", "disk", "Microsoft-Windows-Ntfs 140")),
                (ev(6008, "EventLog"), ("unexpected_shutdown", "system", "EventLog 6008")),
                (ev(1001, "Microsoft-Windows-WER-SystemErrorReporting"), ("unexpected_shutdown", "system", "BugCheck 1001")),
                (ev(1001, "BugCheck"), ("unexpected_shutdown", "system", "BugCheck 1001"))):
            self.assertEqual(cwin.classify_event(e), want, e)

    def test_other_ids_and_providers_are_not_events(self):
        for e in (ev(4101, "Something"), ev(7, "Service Control Manager 7"), ev(7, "Ntfs"), ev(55, "disk"), ev(1001, "Windows Error Reporting"),
                  ev(6008, "Other"), ev(98, "Microsoft-Windows-Ntfs"), ev(4100, "Display"), ev("x", "disk"), ev(None, "disk")):
            c = cwin.classify_event(e)
            self.assertIsNone(c, e)

    def test_parse_events_uses_ids_only_never_the_text(self):
        rows = [ev(4101, "Display", Level=3, TimeCreated=NOW, RecordId=5, Message="Le pilote C:\\Users\\alice\\x"),
                ev(7, "disk", Level=2, TimeCreated=NOW, RecordId=6, Message="bad block on \\Device\\Harddisk0")]
        got = cwin.parse_events({"logs": {"System": {"max": 6, "events": rows}}}, NOW)
        self.assertEqual(sorted((e["kind"], e["subject"]) for e in got["events"]), [("hw_error", "disk"), ("hw_error", "display")])
        self.assertNotIn("alice", repr(got))
        self.assertNotIn("Harddisk", repr(got))


class WindowsDemo(unittest.TestCase):
    def test_the_demo_shows_the_restart_and_still_has_no_linux_only_sections(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(render.once(["render.py", "--once", "--demo", "--demo-os", "windows", "--cols", "200", "--rows", "50"]))
        text = out.getvalue()
        self.assertIn("reboot required: Windows asked for one (Windows Update)", text)
        self.assertNotIn("/run/reboot-required", text)
        self.assertNotIn("PSI", text)


if __name__ == "__main__":
    unittest.main()
