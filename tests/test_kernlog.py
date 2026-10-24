"""Kernel-log and memory alarms: display-stuck, disk-io-errors, memory-errors, nic-resets, oom-killed (boot['kernlog'], read by the root
collector from `journalctl -k`) and memory-pressure (PSI). Sample lines only: nothing reads the machine running the tests."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import collector  # noqa: E402
import demo  # noqa: E402
import history  # noqa: E402
import hostdata  # noqa: E402
import nuc_config  # noqa: E402
import problems  # noqa: E402

NOW = 1_800_000_000
I915 = "i915 0000:00:02.0: [drm] "
# the lines of the real incident (Intel Arrow Lake-P, i915), the first four are the fault
REAL = [
    I915 + "*ERROR* [CRTC:150:pipe A] flip_done timed out",
    I915 + "*ERROR* flip_done timed out",
    I915 + "vblank wait timed out on crtc 0",
    I915 + "*ERROR* Timeout waiting for DDI BUF A to get active",
    I915 + "PHY A failed to change powerdown state",  # follows the others; not a fault line on its own
]
BENIGN = [  # boot-time lines of the same machine: none is a fault
    "EDAC MC0: Giving out device to module igen6_edac controller Intel_client_SoC MC#0: DEV 0000:00:00.0 (INTERRUPT)",
    "EDAC igen6: Expected 2 mcs, but only 1 detected.",
    "nvme nvme0: using unchecked data buffer",
    "block nvme0n1: No UUID available providing old NGUID",
    "EXT4-fs (nvme0n1p2): mounted filesystem d30cd60a-d5bc-4454-b848-7dc13834edfd ro with ordered data mode. Quota mode: none.",
    "EXT4-fs (nvme0n1p2): re-mounted d30cd60a-d5bc-4454-b848-7dc13834edfd r/w.",
    "igc 0000:56:00.0 enp86s0: NIC Link is Up 1000 Mbps Full Duplex, Flow Control: RX/TX",
    "[UFW BLOCK] IN=enp86s0 OUT= SRC=192.0.2.9 DST=192.0.2.10 PROTO=UDP",
    "ata1: SATA link up 6.0 Gbps (SStatus 133 SControl 300)",
]


def classes(msg):
    return sorted(c for c, _ in history.classify_kernel(msg))


def journal(*rows):
    """(age_s, message) -> `journalctl -o short-unix` text."""
    return "\n".join("%d.%06d nuc kernel: %s" % (NOW - age, 123456, msg) for age, msg in rows)


class Classifier(unittest.TestCase):
    def test_the_real_i915_lines(self):
        for msg in REAL[:4]:
            self.assertEqual(history.classify_kernel(msg), [("display", "i915")], msg)
        self.assertEqual(classes(REAL[4]), [])

    def test_each_graphics_symptom_and_driver(self):
        for msg, drv in (("xe 0000:00:02.0: [drm] *ERROR* pipe_off wait timed out", "xe"),
                         ("amdgpu 0000:03:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=1", "amdgpu"),
                         ("i915 0000:00:02.0: [drm] GPU HANG: ecode 12:1:85dffffb, in Xorg [1234]", "i915"),
                         ("nouveau 0000:01:00.0: gpu hang", "nouveau"),
                         ("NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus. flip_done timed out", "NVRM"),
                         ("[drm] *ERROR* Timeout waiting for something", "gpu")):
            self.assertEqual(history.classify_kernel(msg), [("display", drv)], msg)
        self.assertEqual(classes("ring buffer timeout in some other subsystem"), [])  # "ring .* timeout" needs a driver on the line

    def test_disk_memory_and_nic(self):
        for msg in ("blk_update_request: I/O error, dev nvme0n1, sector 1234 op 0x0:(READ)", "Buffer I/O error on dev sda1, logical block 3",
                    "EXT4-fs error (device sda2): ext4_find_entry:1455: inode #2: comm ls: reading directory lblock 0",
                    "EXT4-fs (sda2): Remounting filesystem read-only", "XFS (sdb1): Metadata corruption detected at xfs_buf_ioend",
                    "BTRFS error (device sdc): bdev /dev/sdc errs: wr 1, rd 0", "ata3.00: exception Emask 0x0 SAct 0x0 SErr 0x0 action 0x6",
                    "ata3.00: failed command: READ DMA", "nvme nvme0: I/O 12 QID 3 timeout, aborting"):
            self.assertEqual(classes(msg), ["disk"], msg)
        for msg in ("EDAC MC0: 1 CE memory read error on CPU_SrcID#0_Ha#0_Chan#0_DIMM#0", "mce: [Hardware Error]: Machine check events logged",
                    "mce: [Hardware Error]: CPU 0: Machine Check: 0 Bank 5", "BUG: memory corruption detected"):
            self.assertIn("memory", classes(msg), msg)
        for msg in ("igc 0000:56:00.0 enp86s0: Reset adapter", "e1000e 0000:00:1f.6 eno1: NIC Link is Down",
                    "r8169 0000:03:00.0 enp3s0: Link is Down"):
            self.assertEqual(classes(msg), ["nic"], msg)

    def test_oom_names_the_process_only(self):
        self.assertEqual(history.classify_kernel("Out of memory: Killed process 4242 (node) total-vm:9000kB, anon-rss:1kB"), [("oom", "node")])
        self.assertEqual(history.classify_kernel("Memory cgroup out of memory: Killed process 77 (java) total-vm:1kB"), [("oom", "java")])
        self.assertEqual(classes("oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),task=node,pid=4242,uid=1000"), [])  # same kill, counted once

    def test_boot_time_lines_are_not_faults(self):
        for msg in BENIGN:
            self.assertEqual(classes(msg), [], msg)

    def test_the_health_screen_gets_hardware_errors(self):
        d = {"MESSAGE": "blk_update_request: I/O error, dev sda, sector 1", "_TRANSPORT": "kernel", "SYSLOG_IDENTIFIER": "kernel"}
        self.assertEqual(history.classify_journal(d, "journal"), [("hw_error", "disk", "kernel reported disk I/O errors")])
        d["MESSAGE"] = "EDAC MC0: 1 UE memory read error"
        self.assertEqual(history.classify_journal(d, "kernel")[0][:2], ("hw_error", "memory"))
        d["MESSAGE"] = REAL[0]
        self.assertEqual(history.classify_journal(d, "journal"), [])  # the display has no event kind: it is an ATTENTION alarm only


class Parser(unittest.TestCase):
    def test_counts_last_and_driver(self):
        text = journal((600, REAL[0]), (500, REAL[1]), (60, REAL[2]), (30, BENIGN[0]), (0, "this line is not a kernel line".replace("this", "x")))
        k = collector.parse_kernlog(text, NOW)
        self.assertEqual(k, {"display": {"n": 3, "last": NOW - 60, "driver": "i915"}})

    def test_windows(self):
        k = collector.parse_kernlog(journal((1801, REAL[0]), (86401, "blk_update_request: I/O error, dev sda, sector 1"),
                                            (3 * 86400, "Out of memory: Killed process 9 (node) total-vm:1kB")), NOW)
        self.assertEqual(sorted(k), ["oom"])  # display 30 min, disk 24 h; the OOM killer is the whole boot
        self.assertEqual(k["oom"], {"n": 1, "last": NOW - 3 * 86400, "procs": ["node"]})

    def test_no_raw_line_is_kept(self):
        k = collector.parse_kernlog(journal((10, "Out of memory: Killed process 9 (node) total-vm:1kB, anon-rss:2kB, file-rss:0kB")), NOW)
        self.assertNotIn("anon-rss", repr(k))
        self.assertEqual(collector.parse_kernlog("garbage\n\n", NOW), {})

    def test_a_flood_stays_small(self):
        k = collector.parse_kernlog(journal(*[(5, "Out of memory: Killed process %d (p%d) total-vm:1kB" % (i, i)) for i in range(50)]), NOW)
        self.assertEqual((k["oom"]["n"], len(k["oom"]["procs"])), (50, 5))


def raw(kernlog, now=NOW, thermal=None):
    boot = {"ts": now, "errors": {}, "kernel": "7.0", "btime": now - 9, "failed": [], "journal": {"err": 0, "warn": 0, "capped": False, "top": []}}
    if kernlog is not None:
        boot["kernlog"] = kernlog
    return [(s, t, i) for s, t, i in problems.problems_raw({}, None, now=now, boot=boot, thermal=thermal) if i in KERNEL_IDS]


KERNEL_IDS = {"display-stuck", "disk-io-errors", "memory-errors", "nic-resets", "oom-killed", "memory-pressure"}


class Problems(unittest.TestCase):
    def test_display_stuck_names_driver_and_count_and_clears(self):
        k = {"display": {"n": 12, "last": NOW - 60, "driver": "i915"}}
        self.assertEqual(raw(k), [(2, "i915 graphics driver stuck: 12 errors in the kernel log", "display-stuck")])
        self.assertEqual(raw(k, now=NOW + 1700), raw(k))     # still within 30 min of the newest line
        self.assertEqual(raw(k, now=NOW + 1800 + 1), [])           # cleared: nothing newer than 30 min
        title, why, fix = problems.CATALOG["display-stuck"]
        self.assertIn("do not replug", fix)
        self.assertIn("vendor bug tracker", fix)

    def test_the_other_classes(self):
        k = {"disk": {"n": 4, "last": NOW - 5}, "memory": {"n": 1, "last": NOW - 5}, "oom": {"n": 2, "last": NOW - 5, "procs": ["node", "java"]},
             "nic": {"n": 3, "last": NOW - 5}}
        self.assertEqual({i: (s, t) for s, t, i in raw(k)}, {
            "disk-io-errors": (2, "disk I/O errors in the kernel log (last 24 h)"),
            "memory-errors": (2, "memory (ECC/MCE) errors in the kernel log (last 24 h)"),
            "oom-killed": (1, "kernel killed 2 processes for memory this boot (node, java)"),
            "nic-resets": (1, "network adapter reset or link down 3 times in 24 h")})
        self.assertEqual([i for _, _, i in raw({"nic": {"n": 2, "last": NOW}})], [], "a single flap (under 3) does not alarm")
        self.assertEqual(raw({"disk": {"n": 1, "last": NOW - 86401}, "oom": {"n": 1, "last": NOW - 9 * 86400, "procs": ["x"]}})[0][2], "oom-killed")

    def test_absent_unsupported_or_broken_data_is_no_problem(self):
        for k in (None, {}, "x", {"disk": "x"}, {"disk": {"n": 0, "last": NOW}}, {"disk": {"n": True, "last": NOW}}, {"disk": {"n": 2}},
                  {"oom": {"n": 2, "last": "yesterday"}}):
            self.assertEqual(raw(k), [], k)

    def test_hardware_ones_are_neither_muteable_nor_acceptable(self):
        for pid in ("disk-io-errors", "memory-errors"):
            self.assertIn(pid, problems.NOT_ACCEPTABLE)
            self.assertNotIn(pid, nuc_config.MUTABLE_ALERTS)
        for pid in ("display-stuck", "oom-killed", "memory-pressure", "nic-resets"):
            self.assertIn(pid, nuc_config.MUTABLE_ALERTS)
            self.assertNotIn(pid, problems.NOT_ACCEPTABLE)
        for pid in KERNEL_IDS:
            self.assertIn(pid, problems.CATALOG)

    def test_text_is_stable_for_the_fingerprint(self):
        a = raw({"display": {"n": 5, "last": NOW - 10, "driver": "i915"}})[0]
        b = raw({"display": {"n": 80, "last": NOW - 1500, "driver": "i915"}})[0]
        self.assertEqual(problems.fingerprint(a[0], a[1], a[2]), problems.fingerprint(b[0], b[1], b[2]))


class Pressure(unittest.TestCase):
    def test_parse(self):
        text = "some avg10=0.00 avg60=12.34 avg300=1.00 total=114371\nfull avg10=0.00 avg60=3.00 avg300=0.00 total=114202\n"
        self.assertEqual(hostdata.parse_psi(text), 12.34)
        for bad in ("", None, "full avg60=1.0", "some avg10=1.0", "some avg60=abc", "garbage"):
            self.assertIsNone(hostdata.parse_psi(bad), bad)

    def test_threshold_default_and_config(self):
        th = lambda psi: {"cpu": (40.0, 100.0), "psi": psi}  # noqa: E731
        self.assertEqual([i for _, _, i in raw(None, thermal=th(10.0))], [])      # at the limit is not above it
        got = raw(None, thermal=th(14.25))
        self.assertEqual([(s, i) for s, _, i in got], [(1, "memory-pressure")])
        self.assertIn("14.2", got[0][1])
        self.assertEqual(raw(None, thermal=th(None)), [])                         # no PSI: no problem
        self.assertEqual(raw(None, thermal={"cpu": (40.0, 100.0)}), [])
        with mock.patch.dict(problems.CFG, {"thresholds": {"memory_pressure_pct": 30}}):
            self.assertEqual(raw(None, thermal=th(14.25)), [])
            self.assertEqual(len(raw(None, thermal=th(31.0))), 1)

    def test_absent_psi_file_is_none(self):
        self.assertIsNone(hostdata.read_psi("/nonexistent/pressure/memory"))
        with mock.patch.object(hostdata, "LINUX", False):
            self.assertIsNone(hostdata.read_psi())

    def test_config_key(self):
        said = []
        for text, want in (("[thresholds]\nmemory_pressure_pct = 25\n", 25), ("[thresholds]\nmemory_pressure_pct = 0\n", 10),
                           ("[thresholds]\nmemory_pressure_pct = x\n", 10), ("", 10)):
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "config.ini")
                with open(path, "w") as f:
                    f.write(text)
                self.assertEqual(nuc_config.load(path, warn=said.append)["thresholds"]["memory_pressure_pct"], want, text)
        self.assertEqual(len(said), 2)


class Platforms(unittest.TestCase):
    def test_demo_os_boots_carry_no_kernlog_and_raise_nothing(self):
        for os_name in ("windows", "darwin"):
            _cont, _net, boot, _base = demo.snapshot(os_name=os_name)
            self.assertNotIn("kernlog", boot, os_name)
            self.assertIn("kernlog", boot["unsupported"], os_name)
            self.assertEqual(raw(boot.get("kernlog")), [])
            self.assertEqual([i for _s, _t, i in problems.problems_raw({}, None, boot=boot) if i in KERNEL_IDS], [])
        self.assertIn("kernlog", demo.snapshot(os_name="linux")[2])

    def test_collector_marks_the_section_unsupported_there(self):
        for os_name in ("darwin", "windows"):
            self.assertIn("kernlog", collector.UNSUPPORTED_BOOT if os_name == nuc_config.OS_NAME else
                          {"darwin": ("kernlog",), "windows": ("kernlog",)}[os_name])
        self.assertEqual(collector.BOOT_FEATURE["kernlog"], ("boot",))

    def test_the_demo_shows_the_oom_alarm(self):
        _c, _n, boot, _b = demo.snapshot(now=NOW)
        self.assertEqual([i for _s, _t, i in problems.problems_raw({}, None, now=NOW, boot=boot)
                          if i in KERNEL_IDS], ["oom-killed"])


if __name__ == "__main__":
    unittest.main()
