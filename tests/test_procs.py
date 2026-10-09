"""CPU screen: the process list (src/procs.py) on every OS.

Linux runs against a fake /proc tree, macOS against `ps` output fixtures, Windows against fake API answers and ctypes
buffers built here: all of it on any OS. Secrets are built at runtime. OnLinux runs the real sampler (macOS and Windows:
tests/test_platforms.py).
"""
import ctypes
import functools
import json
import os
import re
import shutil
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import procs  # noqa: E402
import winapi  # noqa: E402

BTIME = 1700000000
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def check_contract(test, out):
    """The exact shape of section 2 of the CPU contract."""
    test.assertEqual(set(out), {"procs", "total", "notes"})
    test.assertEqual(set(out["total"]), {"count", "running", "threads", "unreadable"})
    test.assertIsInstance(out["total"]["count"], int)
    test.assertIsInstance(out["total"]["unreadable"], int)
    for k in ("running", "threads"):
        test.assertIsInstance(out["total"][k], (int, type(None)))
    test.assertTrue(all(isinstance(n, str) for n in out["notes"]))
    nums = (int, float, type(None))
    for p in out["procs"]:
        test.assertEqual(tuple(p), procs.FIELDS)
        test.assertIsInstance(p["pid"], int)
        test.assertIsInstance(p["name"], str)
        test.assertLessEqual(len(p["name"]), 64)
        test.assertIsNone(CONTROL.search(p["name"]), p["name"])
        test.assertIsInstance(p["user"], (str, type(None)))
        test.assertIn(p["state"], (None, "R", "S", "D", "Z", "T", "t", "I", "X", "x", "K", "W", "P"))
        for k in ("ppid", "threads", "nice", "prio", "mem"):
            test.assertIsInstance(p[k], (int, type(None)), k)
        for k in ("cpu", "mem_pct", "time", "start"):
            test.assertIsInstance(p[k], nums, k)
            test.assertFalse(isinstance(p[k], bool))
        if p["cpu"] is not None:
            test.assertGreaterEqual(p["cpu"], 0)
    json.dumps(out, allow_nan=False)  # what the web view sends


# ---- Linux: a fake /proc ----------------------------------------------------------------------------------------------------

def stat_line(pid, comm, state="S", ppid=1, ticks=(0, 0), prio=20, nice=0, threads=1, start=100, flags=0x400100):
    """/proc/<pid>/stat as the kernel writes it (comm as bytes: whatever the process named itself)."""
    if isinstance(comm, str):
        comm = comm.encode()
    rest = [state, ppid, pid, pid, 0, -1, flags, 10, 0, 0, 0, ticks[0], ticks[1], 0, 0, prio, nice, threads, 0, start,
            12345678, 300] + [0] * 30
    return b"%d (" % pid + comm + b") " + " ".join(str(x) for x in rest).encode() + b"\n"


def status_text(name, uid=1000, euid=None, threads=1, rss_kb=None):
    euid = uid if euid is None else euid
    lines = [f"Name:\t{name}", "Umask:\t0022", "State:\tS (sleeping)", "Tgid:\t1", "Ngid:\t0", "Pid:\t1", "PPid:\t0",
             "TracerPid:\t0", f"Uid:\t{uid}\t{euid}\t{euid}\t{euid}", f"Gid:\t{uid}\t{uid}\t{uid}\t{uid}", "FDSize:\t64"]
    if rss_kb is not None:
        lines += ["VmPeak:\t  20000 kB", "VmSize:\t  20000 kB", "VmHWM:\t  9000 kB", f"VmRSS:\t{rss_kb:>8} kB",
                  "RssAnon:\t  1000 kB"]
    lines += [f"Threads:\t{threads}", "SigQ:\t0/63304", "Cpus_allowed_list:\t0-7", "voluntary_ctxt_switches:\t5"]
    return ("\n".join(lines) + "\n").encode()


class FakeProc(object):
    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.proc = os.path.join(self.root, "proc")
        os.makedirs(os.path.join(self.proc, "self"))
        self.write("stat", b"cpu  1 2 3 4 5 6 7 0 0 0\ncpu0 1 2 3 4 5 6 7 0 0 0\nintr 1 2\nctxt 99\nbtime %d\nprocesses 7\n" % BTIME)
        self.write("meminfo", b"MemTotal:       16000000 kB\nMemFree:         8000000 kB\nMemAvailable:   12000000 kB\n")
        self.write("uptime", b"1000.00 7000.00\n")
        self.write("self/mounts", b"sysfs /sys sysfs rw 0 0\nproc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n")

    def write(self, rel, data):
        path = os.path.join(self.proc, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:  # binary: no \r\n on Windows
            f.write(data)

    def add(self, pid, comm, uid=1000, euid=None, rss_kb=4096, cmdline=None, **kw):
        self.write(f"{pid}/stat", stat_line(pid, comm, **kw))
        name = comm.decode("utf-8", "replace") if isinstance(comm, bytes) else comm
        self.write(f"{pid}/status", status_text(name.replace("\n", "\\n"), uid=uid, euid=euid, threads=kw.get("threads", 1),
                                                rss_kb=rss_kb))
        if cmdline is not None:
            self.write(f"{pid}/cmdline", cmdline)
            self.write(f"{pid}/environ", cmdline)

    def remove(self, pid):
        shutil.rmtree(os.path.join(self.proc, str(pid)))

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class FakePwd(object):
    USERS = {0: "root", 1000: "alice", 33: "www-data"}

    class Entry(object):
        def __init__(self, name):
            self.pw_name = name

    @classmethod
    def getpwuid(cls, uid):
        if uid not in cls.USERS:
            raise KeyError(uid)
        return cls.Entry(cls.USERS[uid])


class LinuxProc(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProc()
        self.addCleanup(self.fake.close)
        patcher = mock.patch.object(procs, "pwd", FakePwd)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.secret = "pw-" + os.urandom(8).hex()
        f = self.fake
        f.add(1, "systemd", uid=0, ppid=0, ticks=(300, 200), start=1, threads=1, rss_kb=16000)
        f.add(2, "kthreadd", uid=0, ppid=0, rss_kb=None, flags=0x208040)                       # kernel thread: no memory
        f.add(3, "kworker/0:0H-events_highpri", uid=0, ppid=2, state="I", prio=0, nice=-20, rss_kb=None, flags=0x4208060)
        f.add(100, "(sd-pam)", ppid=1, start=500)                                               # parentheses in the name
        f.add(101, "a b) c", state="R", ppid=1, ticks=(50, 50), start=600, threads=4)          # ') ' inside the name
        f.add(102, b"evil\x1b]0;owned\x07\x1b[2Jname\n", ppid=1)                               # terminal escapes
        f.add(103, "defunct-child", state="Z", ppid=101, ticks=(7, 3), rss_kb=None)             # zombie: no VmRSS
        f.add(104, "postgres", ppid=1, uid=4242, start=700,                                     # a uid with no name
              cmdline=b"postgres\0-c\0password=" + self.secret.encode() + b"\0")
        f.add(105, "passwd", ppid=1, uid=1000, euid=0)                                          # setuid: the effective user
        f.add(106, "‮gnp.exe".encode(), ppid=1)                                            # bidi override
        f.add(107, "rt-audio", ppid=1, prio=-51, nice=0)                                        # real-time priority
        os.makedirs(os.path.join(f.proc, "108"))                                                # vanished: no files any more
        os.makedirs(os.path.join(f.proc, "sys"))                                                # not a pid
        self.clock, self.wall = [1000.0], [BTIME + 5000.0]

    def sampler(self, euid=1000):
        s = procs.ProcSampler(root=self.fake.root, system="linux")
        s.clock, s.now, s.clk_tck, s.euid = (lambda: self.clock[0]), (lambda: self.wall[0]), 100, euid
        return s

    def advance(self, secs):
        self.clock[0] += secs
        self.wall[0] += secs

    def test_fields_of_every_kind_of_process(self):
        out = self.sampler().sample()
        check_contract(self, out)
        by = {p["pid"]: p for p in out["procs"]}
        self.assertEqual(sorted(by), [1, 2, 3, 100, 101, 102, 103, 104, 105, 106, 107])
        self.assertEqual(by[1], {"pid": 1, "ppid": 0, "user": "root", "name": "systemd", "state": "S", "threads": 1, "nice": 0,
                                 "prio": 20, "cpu": None, "mem": 16000 * 1024, "mem_pct": 0.1, "time": 5.0,
                                 "start": BTIME + 0.01})
        self.assertEqual((by[2]["name"], by[2]["mem"], by[2]["mem_pct"], by[2]["user"]), ("kthreadd", None, None, "root"))
        self.assertEqual((by[3]["state"], by[3]["nice"], by[3]["prio"]), ("I", -20, 0))
        self.assertEqual(by[100]["name"], "(sd-pam)")
        self.assertEqual((by[101]["name"], by[101]["state"], by[101]["threads"], by[101]["time"], by[101]["start"]),
                         ("a b) c", "R", 4, 1.0, BTIME + 6.0))
        self.assertEqual(by[102]["name"], "evil]0;owned[2Jname")
        self.assertEqual((by[103]["state"], by[103]["mem"], by[103]["time"]), ("Z", None, 0.1))
        self.assertEqual(by[104]["user"], "4242")
        self.assertEqual(by[105]["user"], "root")
        self.assertEqual(by[106]["name"], "gnp.exe")
        self.assertEqual(by[107]["prio"], -51)
        self.assertEqual(out["total"], {"count": 11, "running": 1, "threads": 14, "unreadable": 0})
        self.assertEqual(out["notes"], ["CPU% from the next refresh"])

    def test_cpu_percent_from_two_samples(self):
        s = self.sampler()
        self.assertTrue(all(p["cpu"] is None for p in s.sample()["procs"]))
        self.advance(2.0)
        self.fake.add(1, "systemd", uid=0, ppid=0, ticks=(400, 250), start=1, rss_kb=16000)           # +1.5 s of CPU
        self.fake.add(101, "a b) c", state="R", ppid=1, ticks=(250, 250), start=600, threads=4)       # +4 s: 4 threads busy
        self.fake.add(200, "newborn", ppid=1, ticks=(30, 20), start=int((5000 - 1 + 0.5) * 100))          # born since: 0.5 s
        self.fake.remove(100)
        self.fake.add(100, "reused", ppid=1, ticks=(10, 0), start=(5000 + 1) * 100)                  # the pid was reused
        self.fake.remove(102)                                                                          # exited
        out = s.sample()
        check_contract(self, out)
        by = {p["pid"]: p for p in out["procs"]}
        self.assertEqual(by[1]["cpu"], 75.0)
        self.assertEqual(by[101]["cpu"], 200.0)  # 100 = one full core
        self.assertEqual(by[2]["cpu"], 0.0)
        self.assertEqual(by[200]["cpu"], 25.0)
        self.assertEqual((by[100]["name"], by[100]["cpu"]), ("reused", 5.0))  # its own time, not a delta with the old one
        self.assertNotIn(102, by)
        self.assertEqual(out["notes"], [])

    def test_no_cpu_after_a_long_pause_and_none_for_a_process_missed_before(self):
        s = self.sampler()
        real_read = s._read
        hide = {"105"}

        def read(path):
            if os.path.basename(os.path.dirname(path)) in hide:
                raise PermissionError(13, "Permission denied", path)
            return real_read(path)
        s._read = read
        first = s.sample()
        self.assertEqual(first["total"]["unreadable"], 1)
        self.assertIsNone(first["total"]["running"])  # one process could not be read: no total
        self.assertIn("1 processes could not be read", first["notes"])
        hide.clear()
        self.advance(2.0)
        by = {p["pid"]: p for p in s.sample()["procs"]}
        self.assertIsNone(by[105]["cpu"])  # old process, no reference: unknown, not its whole life in 2 s
        self.assertEqual(by[1]["cpu"], 0.0)
        self.advance(procs.MAX_GAP + 1)
        out = s.sample()
        self.assertTrue(all(p["cpu"] is None for p in out["procs"]))
        self.assertIn("CPU% from the next refresh", out["notes"])

    def test_vanished_and_exiting_processes_are_skipped_silently(self):
        s = self.sampler()
        real_read = s._read

        def read(path):
            if path.endswith(os.path.join("101", "status")):
                raise ProcessLookupError(3, "No such process")  # gone between stat and status
            if path.endswith(os.path.join("100", "stat")):
                return b""
            return real_read(path)
        s._read = read
        out = s.sample()
        pids = [p["pid"] for p in out["procs"]]
        self.assertNotIn(101, pids)
        self.assertNotIn(100, pids)
        self.assertNotIn(108, pids)
        self.assertEqual((out["total"]["count"], out["total"]["unreadable"]), (9, 0))
        self.assertEqual(out["notes"], ["CPU% from the next refresh"])

    def test_hidepid(self):
        self.fake.write("self/mounts", b"proc /proc proc rw,nosuid,nodev,noexec,relatime,hidepid=invisible 0 0\n")
        self.assertIn("/proc hidepid: other users' processes are hidden", self.sampler(euid=1000).sample()["notes"])
        self.assertFalse(any("hidepid" in n for n in self.sampler(euid=0).sample()["notes"]))
        self.assertEqual(procs.parse_hidepid("proc /proc proc rw,hidepid=2,gid=27 0 0\n"), ("invisible", 27))
        self.assertEqual(procs.parse_hidepid("proc /proc proc rw,hidepid=1 0 0\n"), ("noaccess", None))
        self.assertEqual(procs.parse_hidepid("proc /proc proc rw,hidepid=0 0 0\n"), (None, None))
        self.assertEqual(procs.parse_hidepid("proc /proc proc rw,hidepid=2 0 0\nproc /proc proc rw 0 0\n"), (None, None))
        self.assertEqual(procs.parse_hidepid("proc /run/x/proc proc rw,hidepid=2 0 0\n"), (None, None))

    def test_the_command_line_never_leaves(self):
        s = self.sampler()
        s.sample()
        self.advance(1.0)
        text = json.dumps(s.sample())
        self.assertNotIn(self.secret, text)
        self.assertNotIn("password", text)
        self.assertIn('"postgres"', text)

    def test_parsers(self):
        st = procs.parse_stat(stat_line(42, "x) (y", state="D", ppid=7, ticks=(3, 4), prio=39, nice=19, threads=2, start=9))
        self.assertEqual(st, {"pid": 42, "name": "x) (y", "state": "D", "ppid": 7, "ticks": 7, "prio": 39, "nice": 19,
                              "threads": 2, "starttime": 9})
        self.assertEqual(procs.parse_stat(stat_line(43, ")))"))["name"], ")))")
        self.assertRaises(ValueError, procs.parse_stat, b"garbage")
        self.assertEqual(procs.parse_status(status_text("a", uid=1000, euid=0, threads=3, rss_kb=10)),
                         {"uid": 0, "threads": 3, "rss": 10240})
        self.assertEqual(procs.parse_status(status_text("kworker", uid=0, threads=1)), {"uid": 0, "threads": 1})
        # a name cannot fake the lines after it: the kernel escapes '\n' in Name
        self.assertEqual(procs.parse_status(status_text("x\\nVmRSS:\t1 kB", uid=5, rss_kb=None))["uid"], 5)
        self.assertNotIn("rss", procs.parse_status(status_text("x\\nVmRSS:\t1 kB", uid=5, rss_kb=None)))
        self.assertEqual(procs.parse_btime(b"cpu 1\nbtime 123\n"), 123)
        self.assertEqual(procs.parse_memtotal(b"MemTotal:  1000 kB\n"), 1024000)

    def test_names_are_clean_and_short(self):
        self.assertEqual(procs.clean_name("a\x00b\x1b[31mc\x7f\x9bd\te"), "ab[31mcde")
        self.assertEqual(procs.clean_name("x​‮⁦y z﻿"), "xyz")
        self.assertEqual(procs.clean_name("café  中"), "café  中")
        self.assertEqual(procs.clean_name("Google Chrome Helper (Renderer)"), "Google Chrome Helper (Renderer)")
        self.assertEqual(len(procs.clean_name("n" * 300)), 64)
        self.fake.add(300, "x" * 100 + "\x1b", ppid=1)
        by = {p["pid"]: p for p in self.sampler().sample()["procs"]}
        self.assertEqual(by[300]["name"], "x" * 64)

    def test_a_broken_proc_is_a_note_never_an_exception(self):
        out = procs.ProcSampler(root=os.path.join(self.fake.root, "nowhere"), system="linux").sample()
        check_contract(self, out)
        self.assertEqual(out["procs"], [])
        self.assertTrue(out["notes"][0].startswith("process list unavailable"))
        self.assertEqual(procs.ProcSampler(system="plan9").sample()["notes"], ["no process list on this OS"])


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists("/proc/self/stat"), "Linux")
class OnLinux(unittest.TestCase):
    def test_real_proc(self):
        secret = "tok" + os.urandom(6).hex()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--token=" + secret])
        try:
            s = procs.ProcSampler()
            s.sample()
            end = time.monotonic() + 0.3
            while time.monotonic() < end:  # use some CPU
                sum(i * i for i in range(1000))
            t = time.perf_counter()
            out = s.sample()
            elapsed = time.perf_counter() - t
        finally:
            child.kill()
            child.wait()
        check_contract(self, out)
        self.assertNotIn(secret, json.dumps(out))
        by = {p["pid"]: p for p in out["procs"]}
        me = by[os.getpid()]
        with open("/proc/self/comm") as f:
            self.assertEqual(me["name"], f.read().strip())
        self.assertGreater(me["cpu"], 10)
        self.assertGreater(me["mem"], 0)
        self.assertGreaterEqual(me["threads"], 1)
        self.assertLessEqual(me["start"], time.time())
        self.assertIn(child.pid, by)
        self.assertEqual(out["total"]["count"], len(out["procs"]) + 0 * out["total"]["unreadable"])
        self.assertLess(elapsed, 1.0)  # ~15 ms for 600 processes on a laptop; generous for CI


# ---- macOS: `ps` output -------------------------------------------------------------------------------------------------------

def ps_line(pid, ppid, uid, state, nice, pri, pcpu, rss, cputime, etime, name, age=0):
    if age:  # the same process, `age` seconds later (etime = wall time since it started)
        secs = int(procs.parse_duration(etime) + age)
        etime = f"{secs // 86400}-{secs // 3600 % 24:02}:{secs // 60 % 60:02}:{secs % 60:02}"
    return f"{pid:>5} {ppid:>5} {uid:>5} {state:<4} {nice:>3} {pri:>3} {pcpu:>5} {rss:>8} {cputime:>11} {etime:>11} {name:<16}"


def ps_output(own_pid, chrome_time="62:03.50", extra=(), age=0):
    ps_line = functools.partial(globals()["ps_line"], age=age)
    return "\n".join([
        ps_line(0, 0, 0, "Rs", 0, 0, "0.0", 0, "0:00.00", "10-03:04:05", "kernel_task"),     # other user's: unreadable
        ps_line(1, 0, 0, "Ss", 0, 37, "0.1", 13456, "1:23.45", "10-03:04:05", "launchd"),
        ps_line(321, 1, 501, "S", 0, 31, "12.5", 204800, chrome_time, "02:03:04", "Google Chrome He"),
        ps_line(456, 321, 501, "R", 0, 31, "99.0", 102400, "1-02:03:04.50", "05:06", "com.apple.WebKit"),
        ps_line(500, 1, 501, "I", 5, 20, "0,0", 2048, "0:00,10", "00:42", "zsh"),          # ',' decimal point
        ps_line(510, 1, 501, "U", -5, 31, "0.0", 2048, "0:00.10", "00:42", "a b) c"),
        ps_line(789, 1, 0, "Ss", 0, 0, "0.0", 0, "0:00.00", "1-00:00:00", "syslogd"),        # other user's: unreadable
        ps_line(790, 321, 501, "Z", 0, 0, "0.0", 0, "0:00.00", "00:10", "zombie"),
        ps_line(791, 1, 501, "T", 0, 31, "0.0", "-", "-", "00:10", "stopped"),
        ps_line(800, own_pid, 501, "R+", 0, 31, "0.0", 1024, "0:00.00", "00:00", "ps"),     # our own ps: left out
        ps_line(801, 1, 4242, "S", 0, 31, "0.0", 1024, "0:00.50", "00:05", "evil\x1b[2Jname"),
    ] + list(extra)) + "\n"


class MacPs(unittest.TestCase):
    def sampler(self, text):
        s = procs.ProcSampler(system="darwin")
        s.self_pid, s._memtotal = 4242424, 16 * 2 ** 30
        s.clock, s.now = (lambda: self.clock[0]), (lambda: self.wall[0])
        s._ps = lambda: text[0]
        s._exe_names = lambda pids: {p: n for p, n in self.exe.items() if p in pids}
        s._users = {0: "root", 501: "alice"}
        return s

    def setUp(self):
        self.clock, self.wall = [50.0], [1800000000.0]
        self.exe = {321: "Google Chrome Helper (Renderer)", 1: "launchd", 456: "com.apple.WebKit.WebContent",
                    801: "e" * 80 + "\x07"}

    def test_argv_is_fixed_and_never_asks_for_arguments(self):
        self.assertEqual(procs.PS_ARGV[:2], ("ps", "-ax"))
        self.assertEqual(procs.PS_ARGV[2::2], ("-o",) * len(procs.PS_KEYWORDS))
        self.assertEqual(procs.PS_KEYWORDS[-1], "ucomm")  # p_comm, never argv
        self.assertFalse({"args", "command", "comm"} & set(procs.PS_KEYWORDS))
        self.assertFalse(any(" " in a for a in procs.PS_ARGV))

    def test_parse_ps(self):
        rows = {r["pid"]: r for r in procs.parse_ps(ps_output(1) + "  PID header line\n\n")}
        self.assertEqual(rows[321], {"pid": 321, "ppid": 1, "uid": 501, "state": "S", "nice": 0, "pri": 31, "pcpu": 12.5,
                                     "rss": 204800 * 1024, "time": 3723.5, "etime": 7384.0, "name": "Google Chrome He"})
        self.assertEqual((rows[456]["time"], rows[456]["etime"]), (93784.5, 306.0))
        self.assertEqual((rows[500]["state"], rows[500]["pcpu"], rows[500]["time"], rows[500]["nice"]), ("S", 0.0, 0.1, 5))
        self.assertEqual((rows[510]["state"], rows[510]["name"], rows[510]["nice"]), ("D", "a b) c", -5))
        self.assertEqual((rows[791]["rss"], rows[791]["time"]), (None, None))
        self.assertEqual(rows[0]["etime"], 10 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertEqual(len(rows), 11)
        for t, secs in (("0:00.00", 0), ("12:34.56", 754.56), ("123:45.67", 7425.67), ("1:02:03", 3723), ("2-01:00:00", 176400),
                        ("05", 5), ("-", None), ("", None), ("1:2:3:4", None), ("inf", None)):
            self.assertEqual(procs.parse_duration(t), secs, t)

    def test_sampler(self):
        text = [ps_output(4242424)]
        s = self.sampler(text)
        out = s.sample()
        check_contract(self, out)
        by = {p["pid"]: p for p in out["procs"]}
        self.assertNotIn(800, by)
        self.assertEqual(by[321], {"pid": 321, "ppid": 1, "user": "alice", "name": "Google Chrome Helper (Renderer)",
                                   "state": "S", "threads": None, "nice": 0, "prio": 31, "cpu": 12.5, "mem": 204800 * 1024,
                                   "mem_pct": 1.22, "time": 3723.5, "start": 1800000000 - 7384})
        self.assertEqual(by[456]["name"], "com.apple.WebKit.WebContent")
        self.assertEqual(by[510]["name"], "a b) c")  # no executable path: p_comm
        self.assertEqual(by[801]["name"], "e" * 64)
        self.assertEqual(by[801]["user"], "4242" if procs.pwd is None or not _uid_exists(4242) else by[801]["user"])
        for pid in (0, 789):  # other users' processes: no CPU, memory, state or priority, never 0
            self.assertEqual([by[pid][k] for k in ("cpu", "mem", "mem_pct", "time", "state", "prio")], [None] * 6)
            self.assertIsNotNone(by[pid]["start"])
        self.assertEqual((by[790]["state"], by[790]["mem"], by[790]["time"], by[790]["cpu"]), ("Z", None, None, None))
        self.assertEqual((by[791]["state"], by[791]["mem"], by[791]["cpu"]), ("T", None, None))
        self.assertEqual(out["total"], {"count": 10, "running": None, "threads": None, "unreadable": 3})
        self.assertEqual(out["notes"], ["3 processes of other users: CPU and memory need root"])
        # the second sample: CPU% from the difference of TIME, not ps's decaying average
        self.clock[0] += 4.0
        self.wall[0] += 4.0
        text[0] = ps_output(4242424, chrome_time="62:05.50", age=4, extra=[
            ps_line(900, 1, 501, "R", 0, 31, "50.0", 4096, "0:01.00", "00:02", "newborn")])
        by = {p["pid"]: p for p in s.sample()["procs"]}
        self.assertEqual(by[321]["cpu"], 50.0)
        self.assertEqual(by[456]["cpu"], 0.0)
        self.assertEqual(by[900]["cpu"], 25.0)
        self.assertIsNone(by[0]["cpu"])

    def test_ps_failure_is_a_note(self):
        s = self.sampler([""])

        def boom():
            raise FileNotFoundError("ps")
        s._ps = boom
        out = s.sample()
        check_contract(self, out)
        self.assertIn("FileNotFoundError", out["notes"][0])


def _uid_exists(uid):
    try:
        procs.pwd.getpwuid(uid)
        return True
    except KeyError:
        return False


# ---- Windows: API answers and buffers -----------------------------------------------------------------------------------------

FT_1970 = winapi.FILETIME_UNIX_EPOCH


def entry(pid, ppid, name, threads, pri=8):
    e = winapi.ProcessEntry32W(size=ctypes.sizeof(winapi.ProcessEntry32W), pid=pid, ppid=ppid, threads=threads, pri_base=pri)
    for i, ch in enumerate(name.encode("utf-16-le")[::2].decode("latin-1") if False else name):
        e.exe[i] = ord(ch)
    return winapi.parse_process_entry(winapi.ProcessEntry32W.from_buffer_copy(bytes(e)))


class WinProcs(unittest.TestCase):
    def test_structures_match_the_windows_headers(self):
        p64 = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(ctypes.sizeof(winapi.ProcessEntry32W), 568 if p64 else 556)
        self.assertEqual(winapi.ProcessEntry32W.exe.offset, 44 if p64 else 36)
        self.assertEqual(winapi.ProcessEntry32W.threads.offset, 28 if p64 else 20)
        self.assertEqual(winapi.ProcessEntry32W.ppid.offset, 32 if p64 else 24)
        self.assertEqual(ctypes.sizeof(winapi.MemCounters), 72 if p64 else 40)
        self.assertEqual(winapi.MemCounters.working_set.offset, 16 if p64 else 12)

    def test_process_entry_and_filetime(self):
        raw = bytearray(ctypes.sizeof(winapi.ProcessEntry32W))
        e = winapi.ProcessEntry32W.from_buffer(raw)
        e.pid, e.ppid, e.threads, e.pri_base = 4321, 4, 12, 13
        name = "Café-中.exe".encode("utf-16-le") + b"\0\0" + "junk".encode("utf-16-le")
        off = winapi.ProcessEntry32W.exe.offset
        raw[off:off + len(name)] = name
        self.assertEqual(winapi.parse_process_entry(winapi.ProcessEntry32W.from_buffer_copy(bytes(raw))),
                         {"pid": 4321, "ppid": 4, "threads": 12, "pri": 13, "name": "Café-中.exe"})
        self.assertEqual(winapi.filetime_epoch(FT_1970), 0)
        self.assertEqual(winapi.filetime_epoch(FT_1970 + 1700000000 * 10 ** 7 + 5 * 10 ** 6), 1700000000.5)
        self.assertIsNone(winapi.filetime_epoch(0))
        mc = winapi.MemCounters.from_buffer_copy(bytes(8) + bytes(ctypes.sizeof(ctypes.c_size_t))
                                                 + (123456789).to_bytes(ctypes.sizeof(ctypes.c_size_t), "little")
                                                 + bytes(ctypes.sizeof(winapi.MemCounters) - 8 - 2 * ctypes.sizeof(ctypes.c_size_t)))
        self.assertEqual(mc.working_set, 123456789)

    def setUp(self):
        self.calls, self.lookups = [], []
        self.cpu = {500: 10 ** 7, 600: 5 * 10 ** 7}
        self.created = {500: FT_1970 + 1700000000 * 10 ** 7, 600: FT_1970 + 1700000100 * 10 ** 7}
        self.listing = [entry(0, 0, "[System Process]", 8, pri=0), entry(4, 0, "System", 200), entry(500, 4, "svchost.exe", 10),
                        entry(600, 500, "python.exe", 3), entry(700, 500, "gone.exe", 1), entry(701, 500, "exited.exe", 1),
                        entry(800, 600, "evil\x1b[2J.exe", 2)]

        def stats(pid, user=True):
            self.calls.append((pid, user))
            if pid in (4, 800):
                raise OSError(winapi.ERROR_ACCESS_DENIED, "OpenProcess failed (Windows error 5)")
            if pid == 700:
                raise OSError(winapi.ERROR_INVALID_PARAMETER, "OpenProcess failed (Windows error 87)")
            if pid == 701:
                return {"created": 1, "exited": True, "cpu": 1, "rss": 1, "sid": None}
            sid = {500: "S-1-5-18", 600: "S-1-5-21-1-2-3-1001"}[pid] if user else None
            return {"created": self.created[pid], "exited": False, "cpu": self.cpu[pid], "rss": pid * 1024 * 1024, "sid": sid}

        def account(sid):
            self.lookups.append(sid)
            return {"S-1-5-18": "SYSTEM", "S-1-5-21-1-2-3-1001": "alice"}.get(sid)
        for name, fake in (("process_list", lambda: list(self.listing)), ("process_stats", stats), ("account_name", account),
                           ("memory", lambda: {"total": 8 * 2 ** 30, "available": 0, "cache": 0})):
            p = mock.patch.object(winapi, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.clock, self.wall = [10.0], [1700000200.0]

    def sampler(self):
        s = procs.ProcSampler(system="windows")
        s.clock, s.now = (lambda: self.clock[0]), (lambda: self.wall[0])
        return s

    def test_sampler(self):
        s = self.sampler()
        out = s.sample()
        check_contract(self, out)
        by = {p["pid"]: p for p in out["procs"]}
        self.assertEqual(sorted(by), [4, 500, 600, 800])  # no Idle (pid 0), nothing gone or exited
        self.assertEqual(by[4], {"pid": 4, "ppid": 0, "user": None, "name": "System", "state": None, "threads": 200,
                                 "nice": None, "prio": 8, "cpu": None, "mem": None, "mem_pct": None, "time": None,
                                 "start": None})
        self.assertEqual(by[500], {"pid": 500, "ppid": 4, "user": "SYSTEM", "name": "svchost.exe", "state": None, "threads": 10,
                                   "nice": None, "prio": 8, "cpu": None, "mem": 500 * 2 ** 20, "mem_pct": 6.1, "time": 1.0,
                                   "start": 1700000000.0})
        self.assertEqual(by[600]["user"], "alice")
        self.assertEqual(by[800]["name"], "evil[2J.exe")
        self.assertEqual(out["total"], {"count": 4, "running": None, "threads": 215, "unreadable": 2})
        self.assertEqual(out["notes"], ["2 processes could not be opened (access denied): CPU and memory unknown",
                                        "CPU% from the next refresh"])
        # second sample: CPU% from the difference; the owner is not read again, each account looked up once
        self.calls.clear()
        self.clock[0] += 2.0
        self.wall[0] += 2.0
        self.cpu[500] += 10 ** 7
        self.cpu[600] += 4 * 10 ** 7
        by = {p["pid"]: p for p in s.sample()["procs"]}
        self.assertEqual((by[500]["cpu"], by[600]["cpu"]), (50.0, 200.0))
        self.assertIn((500, False), self.calls)
        self.assertEqual(sorted(self.lookups), ["S-1-5-18", "S-1-5-21-1-2-3-1001"])
        # the pid is reused by a new process: its owner is read again, its CPU% is its own
        self.clock[0] += 2.0
        self.wall[0] += 2.0
        self.created[600] = FT_1970 + int(self.wall[0] - 1) * 10 ** 7
        self.cpu[600] = 10 ** 7
        self.calls.clear()
        by = {p["pid"]: p for p in s.sample()["procs"]}
        self.assertIn((600, True), self.calls)
        self.assertEqual(by[600]["cpu"], 50.0)

    def test_a_failing_snapshot_is_a_note(self):
        def boom():
            raise OSError(5, "CreateToolhelp32Snapshot failed (Windows error 5)")
        with mock.patch.object(winapi, "process_list", boom):
            out = self.sampler().sample()
        check_contract(self, out)
        self.assertEqual(out["procs"], [])
        self.assertIn("CreateToolhelp32Snapshot", out["notes"][0])


# ---- names only ------------------------------------------------------------------------------------------------------------

class NamesOnly(unittest.TestCase):
    def test_no_source_of_arguments_is_ever_read(self):
        with open(os.path.join(os.path.dirname(procs.__file__), "procs.py"), encoding="utf-8") as f:
            code = f.read()
        code = re.sub(r'"""(.|\n)*?"""', "", code)                         # docstrings may say what is NOT read
        code = "\n".join(ln.split("#", 1)[0] for ln in code.splitlines())  # and comments
        for word in ("cmdline", "environ", "KERN_PROCARGS", "args=", "command=", '"comm"', "CommandLine", "PEB",
                     "QueryFullProcessImageName", "Win32_Process"):
            self.assertNotIn(word, code)


if __name__ == "__main__":
    unittest.main()
