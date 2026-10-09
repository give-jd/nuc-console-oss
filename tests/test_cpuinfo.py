"""The CPU screen's data (src/cpuinfo.py): fake /proc and /sys trees, Windows and macOS structures built here, the contract.

Everything runs on every OS (the trees are built in a temp dir, the Windows buffers with ctypes/struct, the macOS sysctl
values from a dict); `RealHost` runs the real sampler of the OS the tests run on. Model names and values are made up."""
import ctypes
import json
import os
import shutil
import struct
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import cpuinfo  # noqa: E402
import winapi  # noqa: E402

CONTRACT_KEYS = {"model", "vendor", "arch", "sockets", "cores", "threads", "kinds", "cache", "freq", "usage", "load", "rates",
                 "uptime", "temps", "throttle", "notes"}


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def opt(v, check):
    return v is None or check(v)


def check_contract(tc, s):
    """The shape of CpuSampler.sample() as the contract (feat/cpu, section 1) defines it: every key, the types."""
    tc.assertEqual(set(s), CONTRACT_KEYS)
    tc.assertTrue(opt(s["model"], lambda v: isinstance(v, str) and v), s["model"])
    tc.assertTrue(opt(s["vendor"], lambda v: isinstance(v, str) and v))
    tc.assertTrue(opt(s["arch"], lambda v: isinstance(v, str)))
    tc.assertTrue(opt(s["sockets"], lambda v: isinstance(v, int) and v > 0))
    tc.assertTrue(opt(s["cores"], lambda v: isinstance(v, int) and v > 0))
    tc.assertTrue(isinstance(s["threads"], int) and s["threads"] > 0)
    tc.assertIsInstance(s["kinds"], dict)
    for k, v in s["kinds"].items():
        tc.assertIn(k, ("P", "E"))
        tc.assertTrue(isinstance(v, int) or (isinstance(v, list) and all(isinstance(i, int) for i in v)))
    tc.assertIsInstance(s["cache"], dict)
    for k, v in s["cache"].items():
        tc.assertIn(k, ("L1d", "L1i", "L2", "L3"))
        tc.assertTrue(isinstance(v, int) and v > 0)
    f = s["freq"]
    tc.assertEqual(set(f), {"cur", "min", "max", "base", "governor", "driver"})
    tc.assertTrue(all(isinstance(k, int) and num(v) and v > 0 for k, v in f["cur"].items()))
    for k in ("min", "max", "base"):
        tc.assertTrue(opt(f[k], lambda v: num(v) and v > 0))
    for k in ("governor", "driver"):
        tc.assertTrue(opt(f[k], lambda v: isinstance(v, str) and v))
    u = s["usage"]
    tc.assertEqual(set(u), {"total", "cores"})
    if u["total"] is not None:
        tc.assertEqual(set(u["total"]), {"busy", "user", "system", "nice", "iowait", "irq", "steal", "idle"})
        for k, v in u["total"].items():
            tc.assertTrue(opt(v, lambda x: num(x) and 0 <= x <= 100), (k, v))
        for k in ("busy", "user", "system", "nice", "idle"):
            tc.assertTrue(num(u["total"][k]), k)
    ids = [c["id"] for c in u["cores"]]
    tc.assertEqual(ids, sorted(ids))
    for c in u["cores"]:
        tc.assertEqual(set(c), {"id", "busy", "user", "system", "iowait"})
        tc.assertIsInstance(c["id"], int)
        for k in ("busy", "user", "system", "iowait"):
            tc.assertTrue(opt(c[k], lambda x: num(x) and 0 <= x <= 100), (k, c[k]))
    tc.assertTrue(opt(s["load"], lambda v: len(v) == 3 and all(num(x) and x >= 0 for x in v)))
    r = s["rates"]
    tc.assertEqual(set(r), {"ctxt", "intr", "running", "blocked"})
    for k in ("ctxt", "intr"):
        tc.assertTrue(opt(r[k], lambda v: num(v) and v >= 0))
    for k in ("running", "blocked"):
        tc.assertTrue(opt(r[k], lambda v: isinstance(v, int) and v >= 0))
    tc.assertTrue(opt(s["uptime"], lambda v: num(v) and v >= 0))
    t = s["temps"]
    tc.assertEqual(set(t), {"package", "cores", "sensors", "high", "crit", "source"})
    for k in ("package", "high", "crit"):
        tc.assertTrue(opt(t[k], num))
    tc.assertTrue(all(isinstance(k, int) and num(v) for k, v in t["cores"].items()))
    for x in t["sensors"]:
        tc.assertEqual(set(x), {"label", "c", "high", "crit"})
        tc.assertTrue(isinstance(x["label"], str) and num(x["c"]) and opt(x["high"], num) and opt(x["crit"], num))
    tc.assertTrue(opt(t["source"], lambda v: isinstance(v, str)))
    th = s["throttle"]
    tc.assertEqual(set(th), {"package", "cores", "package_s"})
    tc.assertTrue(opt(th["package"], lambda v: isinstance(v, int)) and opt(th["package_s"], num))
    tc.assertTrue(all(isinstance(k, int) and isinstance(v, int) for k, v in th["cores"].items()))
    tc.assertTrue(isinstance(s["notes"], list) and all(isinstance(n, str) and n for n in s["notes"]))
    json.dumps(s)  # the web page serialises it


# ---- fake /proc and /sys trees --------------------------------------------------------------------------------------------

def put(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(text) + "\n")


def proc_stat(cpus, ctxt=100000, intr=50000, running=2, blocked=0, btime=1700000000):
    """cpus: {id: (user, nice, system, idle, iowait, irq, softirq, steal)}; the 'cpu' line is their sum."""
    tot = [sum(v[i] for v in cpus.values()) for i in range(8)]
    lines = ["cpu  " + " ".join(map(str, tot)) + " 0 0"]
    lines += [f"cpu{i} " + " ".join(map(str, v)) + " 0 0" for i, v in sorted(cpus.items())]
    lines += [f"intr {intr} 12 0 0 34", f"ctxt {ctxt}", f"btime {btime}", "processes 4242", f"procs_running {running}",
              f"procs_blocked {blocked}"]
    return "\n".join(lines)


def add_cpu(root, i, core, pkg=0, siblings=None, caches=(), cpufreq=None, throttle=None, capacity=None):
    base = f"sys/devices/system/cpu/cpu{i}"
    put(root, base + "/topology/core_id", core)
    put(root, base + "/topology/physical_package_id", pkg)
    put(root, base + "/topology/core_cpus_list", siblings if siblings is not None else str(i))
    for n, (level, kind, size, shared) in enumerate(caches):
        d = f"{base}/cache/index{n}"
        put(root, d + "/level", level)
        put(root, d + "/type", kind)
        put(root, d + "/size", size)
        put(root, d + "/shared_cpu_list", shared)
    for name, value in (cpufreq or {}).items():
        put(root, f"{base}/cpufreq/{name}", value)
    for name, value in (throttle or {}).items():
        put(root, f"{base}/thermal_throttle/{name}", value)
    if capacity is not None:
        put(root, base + "/cpu_capacity", capacity)


def hwmon(root, n, name, temps):
    """temps: [(label or None, millidegrees, max, crit)] -> temp1.. files."""
    put(root, f"sys/class/hwmon/hwmon{n}/name", name)
    for k, (label, milli, high, crit) in enumerate(temps, 1):
        d = f"sys/class/hwmon/hwmon{n}/temp{k}"
        put(root, d + "_input", milli)
        if label:
            put(root, d + "_label", label)
        if high is not None:
            put(root, d + "_max", high)
        if crit is not None:
            put(root, d + "_crit", crit)


X86_INFO = "processor\t: {i}\nvendor_id\t: {vendor}\nmodel name\t: {model}\nphysical id\t: {pkg}\ncore id\t\t: {core}\n\n"


def intel_hybrid(root):
    """A 12th gen laptop, cut down: 2 P cores with 2 threads (cpu0-3) and 4 E cores (cpu4-7); coretemp; intel_pstate."""
    model = "12th Gen Intel(R) Core(TM) i7-1260P"
    cores = [0, 0, 4, 4, 8, 9, 10, 11]
    put(root, "proc/cpuinfo", "".join(X86_INFO.format(i=i, vendor="GenuineIntel", model=model, pkg=0, core=c)
                                      for i, c in enumerate(cores)))
    put(root, "proc/loadavg", "0.52 0.58 0.59 1/456 12345")
    put(root, "proc/uptime", "3600.50 20000.10")
    put(root, "sys/devices/cpu_core/cpus", "0-3")
    put(root, "sys/devices/cpu_atom/cpus", "4-7")
    for i, core in enumerate(cores):
        p = i < 4
        sib = ("0-1" if i < 2 else "2-3") if p else str(i)
        caches = [(1, "Data", "48K" if p else "32K", sib), (1, "Instruction", "32K" if p else "64K", sib),
                  (2, "Unified", "1280K" if p else "2048K", sib if p else "4-7"), (3, "Unified", "18432K", "0-7")]
        freq = {"scaling_cur_freq": 2400000 + 100000 * i, "cpuinfo_min_freq": 400000, "cpuinfo_max_freq": 4700000 if p else 3400000,
                "base_frequency": 2100000 if p else 1500000, "scaling_governor": "powersave", "scaling_driver": "intel_pstate"}
        throttle = {"core_throttle_count": [3, 5, 0, 0, 0, 0, 0, 0][i], "package_throttle_count": 7,
                    "package_throttle_total_time_ms": 2500}
        add_cpu(root, i, core, 0, sib, caches, freq, throttle)
    hwmon(root, 0, "nvme", [("Composite", 41850, 83850, 87850)])
    hwmon(root, 3, "coretemp", [("Package id 0", 52000, 100000, 100000), ("Core 0", 50000, 100000, 100000),
                                ("Core 4", 53000, 100000, 100000), ("Core 8", 48000, 100000, 100000),
                                ("Core 9", 48000, 100000, 100000), ("Core 10", 47000, 100000, 100000),
                                ("Core 11", 49000, 100000, 100000)])
    zero = {i: (0,) * 8 for i in range(8)}
    return zero


def amd_ryzen(root, with_tdie=False):
    """Ryzen 5 5600X: 6 cores, 12 threads (cpu i and i+6 share a core); k10temp with Tctl and Tccd1; acpi-cpufreq."""
    model = "AMD Ryzen 5 5600X 6-Core Processor"
    put(root, "proc/cpuinfo", "".join(X86_INFO.format(i=i, vendor="AuthenticAMD", model=model, pkg=0, core=i % 6) for i in range(12)))
    put(root, "proc/loadavg", "1.00 0.50 0.25 1/300 999")
    put(root, "proc/uptime", "86400.00 600000.00")
    for i in range(12):
        sib = f"{i % 6},{i % 6 + 6}"
        caches = [(1, "Data", "32K", sib), (1, "Instruction", "32K", sib), (2, "Unified", "512K", sib), (3, "Unified", "32768K", "0-11")]
        freq = {"scaling_cur_freq": 3700000, "cpuinfo_min_freq": 2200000, "cpuinfo_max_freq": 4650000,
                "scaling_governor": "schedutil", "scaling_driver": "acpi-cpufreq"}
        add_cpu(root, i, i % 6, 0, sib, caches, freq)
    hwmon(root, 0, "nvme", [("Composite", 38000, 83000, 87000)])
    hwmon(root, 1, "amdgpu", [("edge", 45000, 100000, 100000)])
    if with_tdie:
        hwmon(root, 2, "k10temp", [("Tctl", 81500, None, None), ("Tdie", 61500, None, 95000), ("Tccd1", 58000, None, None)])
    else:
        hwmon(root, 2, "k10temp", [("Tctl", 61500, 95000, 115000), ("Tccd1", 58000, None, None)])
    return {i: (0,) * 8 for i in range(12)}


def raspberry_pi(root):
    """A Pi 4: only 'Hardware', 'Model', 'Serial' and CPU part in /proc/cpuinfo; cpu_thermal; cpufreq without governor files."""
    info = "".join(f"processor\t: {i}\nBogoMIPS\t: 108.00\nFeatures\t: fp asimd evtstrm crc32 cpuid\nCPU implementer\t: 0x41\n"
                   f"CPU architecture: 8\nCPU variant\t: 0x0\nCPU part\t: 0xd08\nCPU revision\t: 3\n\n" for i in range(4))
    info += "Hardware\t: BCM2711\nRevision\t: c03111\nSerial\t\t: 10000000deadbeef\nModel\t\t: Raspberry Pi 4 Model B Rev 1.1\n"
    put(root, "proc/cpuinfo", info)
    put(root, "proc/loadavg", "0.10 0.20 0.30 1/100 500")
    put(root, "proc/uptime", "120.00 400.00")
    for i in range(4):
        caches = [(1, "Data", "32K", str(i)), (1, "Instruction", "48K", str(i)), (2, "Unified", "1024K", "0-3")]
        add_cpu(root, i, i, 0, str(i), caches, {"scaling_cur_freq": 1500000, "cpuinfo_min_freq": 600000, "cpuinfo_max_freq": 1500000})
    hwmon(root, 0, "cpu_thermal", [(None, 48312, None, None)])
    return {i: (0,) * 8 for i in range(4)}


def plain_vm(root):
    """Two vCPUs of a hypervisor: no cpufreq, no sensors, no throttle files."""
    model = "Intel(R) Xeon(R) Gold 6230 CPU @ 2.10GHz"
    put(root, "proc/cpuinfo", "".join(X86_INFO.format(i=i, vendor="GenuineIntel", model=model, pkg=0, core=i) for i in range(2)))
    put(root, "proc/loadavg", "0.00 0.01 0.05 1/90 300")
    put(root, "proc/uptime", "50.00 90.00")
    for i in range(2):
        add_cpu(root, i, i, 0, str(i), [(1, "Data", "32K", str(i)), (2, "Unified", "4096K", str(i)), (3, "Unified", "28160K", "0-1")])
    return {i: (0,) * 8 for i in range(2)}


class Clock(object):
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cpuinfo-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clock = Clock()

    def stat(self, cpus, **kw):
        put(self.root, "proc/stat", proc_stat(cpus, **kw))

    def sampler(self, build, **kw):
        cpus = build(self.root)
        self.stat(cpus, **kw)
        return cpuinfo.CpuSampler(root=self.root, clock=self.clock)

    def start(self, first, build=None, **kw):
        """A sampler whose constructor read `first` (the counters of the first reading) from a tree built by `build`."""
        (build or plain_vm)(self.root)
        self.stat(first, **kw)
        return cpuinfo.CpuSampler(root=self.root, clock=self.clock)

    def step(self, sampler, cpus, dt=2.0, **kw):
        self.clock.t += dt
        self.stat(cpus, **kw)
        return sampler.sample()


# ---- Linux: what the machine is -------------------------------------------------------------------------------------------

class LinuxMachines(Fixture):
    def test_intel_hybrid_laptop(self):
        s = self.sampler(intel_hybrid)
        s = self.step(s, {i: (10,) * 8 for i in range(8)})
        check_contract(self, s)
        self.assertEqual((s["model"], s["vendor"]), ("12th Gen Intel(R) Core(TM) i7-1260P", "GenuineIntel"))
        self.assertEqual((s["sockets"], s["cores"], s["threads"]), (1, 6, 8))
        self.assertEqual(s["kinds"], {"P": [0, 1, 2, 3], "E": [4, 5, 6, 7]})
        k = 1024
        self.assertEqual(s["cache"], {"L1d": 2 * 48 * k + 4 * 32 * k, "L1i": 2 * 32 * k + 4 * 64 * k,
                                      "L2": 2 * 1280 * k + 2048 * k, "L3": 18432 * k})  # every cache counted once
        f = s["freq"]
        self.assertEqual(f["cur"], {i: 2400 + 100 * i for i in range(8)})
        self.assertEqual((f["min"], f["max"], f["base"]), (400, 4700, 2100))  # the P cores' base
        self.assertEqual((f["governor"], f["driver"]), ("powersave", "intel_pstate"))
        self.assertEqual(s["load"], [0.52, 0.58, 0.59])
        self.assertEqual(s["uptime"], 3600.5)
        t = s["temps"]
        self.assertEqual((t["package"], t["high"], t["crit"], t["source"]), (52.0, 100.0, 100.0, "coretemp"))
        self.assertEqual(t["cores"], {0: 50.0, 4: 53.0, 8: 48.0, 9: 48.0, 10: 47.0, 11: 49.0})
        self.assertEqual([x["label"] for x in t["sensors"]][:3], ["Package id 0", "Core 0", "Core 4"])
        self.assertNotIn("Composite", [x["label"] for x in t["sensors"]])  # the NVMe drive is not the CPU
        self.assertEqual(t["sensors"][1], {"label": "Core 0", "c": 50.0, "high": 100.0, "crit": 100.0})
        # a core's count is the highest of its threads (3 and 5 -> 5); the package's the highest of all CPUs
        self.assertEqual(s["throttle"], {"package": 7, "cores": {0: 5, 4: 0, 8: 0, 9: 0, 10: 0, 11: 0}, "package_s": 2.5})
        self.assertEqual(s["notes"], [])

    def test_amd_ryzen_with_k10temp(self):
        s = self.sampler(amd_ryzen)
        s = self.step(s, {i: (10,) * 8 for i in range(12)})
        check_contract(self, s)
        self.assertEqual((s["model"], s["vendor"]), ("AMD Ryzen 5 5600X 6-Core Processor", "AuthenticAMD"))
        self.assertEqual((s["sockets"], s["cores"], s["threads"], s["kinds"]), (1, 6, 12, {}))
        self.assertEqual(s["cache"], {"L1d": 6 * 32 * 1024, "L1i": 6 * 32 * 1024, "L2": 6 * 512 * 1024, "L3": 32768 * 1024})
        self.assertEqual((s["freq"]["min"], s["freq"]["max"], s["freq"]["base"]), (2200, 4650, None))
        self.assertEqual((s["freq"]["governor"], s["freq"]["driver"]), ("schedutil", "acpi-cpufreq"))
        t = s["temps"]
        self.assertEqual((t["package"], t["high"], t["crit"], t["source"]), (61.5, 95.0, 115.0, "k10temp"))
        self.assertEqual(t["cores"], {})  # Tctl and Tccd1 are not cores
        self.assertEqual([(x["label"], x["c"]) for x in t["sensors"]], [("Tctl", 61.5), ("Tccd1", 58.0)])
        self.assertEqual(s["throttle"], {"package": None, "cores": {}, "package_s": None})  # AMD: no thermal_throttle

    def test_k10temp_prefers_tdie_over_tctl(self):
        s = self.sampler(lambda r: amd_ryzen(r, with_tdie=True))
        s = self.step(s, {i: (10,) * 8 for i in range(12)})
        self.assertEqual((s["temps"]["package"], s["temps"]["crit"]), (61.5, 95.0))  # Tctl carries an offset on some models
        self.assertEqual([x["label"] for x in s["temps"]["sensors"]], ["Tctl", "Tdie", "Tccd1"])

    def test_raspberry_pi(self):
        s = self.sampler(raspberry_pi)
        s = self.step(s, {i: (10,) * 8 for i in range(4)})
        check_contract(self, s)
        self.assertEqual((s["model"], s["vendor"]), ("Cortex-A72", "ARM"))  # 'Model' is the board, the CPU is named by its part
        self.assertEqual((s["sockets"], s["cores"], s["threads"], s["kinds"]), (1, 4, 4, {}))
        self.assertEqual(s["cache"], {"L1d": 4 * 32 * 1024, "L1i": 4 * 48 * 1024, "L2": 1024 * 1024})
        self.assertEqual(s["freq"], {"cur": {i: 1500 for i in range(4)}, "min": 600, "max": 1500, "base": None,
                                     "governor": None, "driver": None})
        self.assertEqual((s["temps"]["package"], s["temps"]["source"]), (48.3, "cpu_thermal"))
        self.assertEqual(s["temps"]["sensors"], [{"label": "cpu_thermal", "c": 48.3, "high": None, "crit": None}])
        self.assertNotIn("deadbeef", json.dumps(s))  # the board serial is never read

    def test_big_little_by_cpu_capacity(self):
        idle = {i: (10,) * 8 for i in range(4)}
        raspberry_pi(self.root)
        for i, cap in enumerate((446, 446, 1024, 1024)):
            put(self.root, f"sys/devices/system/cpu/cpu{i}/cpu_capacity", cap)
        self.stat(idle)
        self.assertEqual(self.step(cpuinfo.CpuSampler(root=self.root, clock=self.clock), idle)["kinds"], {"P": [2, 3], "E": [0, 1]})
        for i in range(4):
            put(self.root, f"sys/devices/system/cpu/cpu{i}/cpu_capacity", 1024)  # all the same: not big.LITTLE
        self.assertEqual(self.step(cpuinfo.CpuSampler(root=self.root, clock=self.clock), idle)["kinds"], {})

    def test_vm_with_no_sensors_and_no_cpufreq(self):
        s = self.step(self.start({i: (0,) * 8 for i in range(2)}), {i: (10,) * 8 for i in range(2)})
        check_contract(self, s)
        self.assertEqual((s["model"], s["threads"], s["cores"]), ("Intel(R) Xeon(R) Gold 6230 CPU @ 2.10GHz", 2, 2))
        self.assertEqual(s["freq"], {"cur": {}, "min": None, "max": None, "base": 2100, "governor": None, "driver": None})
        self.assertEqual(s["temps"], {"package": None, "cores": {}, "sensors": [], "high": None, "crit": None, "source": None})
        self.assertEqual(s["throttle"], {"package": None, "cores": {}, "package_s": None})
        self.assertEqual(s["notes"], ["clock: no cpufreq in sysfs", "temperature: no CPU sensor found"])
        self.assertEqual(s["load"], [0.0, 0.01, 0.05])

    def test_thermal_zone_fallbacks(self):
        idle = {i: (0,) * 8 for i in range(2)}
        plain_vm(self.root)
        put(self.root, "sys/class/thermal/thermal_zone0/type", "acpitz")
        put(self.root, "sys/class/thermal/thermal_zone0/temp", "27800")
        put(self.root, "sys/class/thermal/thermal_zone0/trip_point_0_type", "critical")
        put(self.root, "sys/class/thermal/thermal_zone0/trip_point_0_temp", "105000")
        put(self.root, "sys/class/thermal/thermal_zone1/type", "x86_pkg_temp")
        put(self.root, "sys/class/thermal/thermal_zone1/temp", "45000")
        s = self.step(self.start(idle), idle)
        self.assertEqual((s["temps"]["package"], s["temps"]["source"]), (45.0, "x86_pkg_temp"))  # before the board's ACPI zone
        shutil.rmtree(os.path.join(self.root, "sys/class/thermal/thermal_zone1"))
        s = self.step(self.start(idle), idle)
        self.assertEqual((s["temps"]["package"], s["temps"]["source"], s["temps"]["crit"]), (27.8, "acpitz", 105.0))

    def test_second_socket_keeps_its_cores_out_of_the_first_ones(self):
        idle = {i: (0,) * 8 for i in range(2)}
        plain_vm(self.root)
        hwmon(self.root, 0, "coretemp", [("Package id 0", 50000, None, None), ("Core 0", 49000, None, None)])
        hwmon(self.root, 1, "coretemp", [("Package id 1", 60000, None, None), ("Core 0", 59000, None, None)])
        s = self.step(self.start(idle), idle)
        self.assertEqual((s["temps"]["package"], s["temps"]["cores"]), (60.0, {0: 49.0}))
        self.assertIn("Core 0 (package 1)", [x["label"] for x in s["temps"]["sensors"]])

    def test_sensor_error_values_are_not_temperatures(self):
        idle = {i: (0,) * 8 for i in range(4)}
        sampler = self.start(idle, raspberry_pi)
        put(self.root, "sys/class/hwmon/hwmon0/temp1_input", "-128000")
        s = self.step(sampler, idle)
        self.assertIsNone(s["temps"]["package"])


# ---- Linux: counters, deltas, percentages -------------------------------------------------------------------------------------

class LinuxDeltas(Fixture):
    A = {0: (100, 0, 50, 800, 10, 0, 5, 0), 1: (200, 0, 100, 700, 0, 0, 0, 0)}
    B = {0: (220, 0, 90, 1000, 30, 10, 15, 0), 1: (250, 0, 150, 1100, 0, 0, 0, 0)}  # cpu0: +120 +40 +200 +20 +10 +10

    def test_percentages_of_the_interval(self):
        s = self.start(self.A, ctxt=1000, intr=5000)
        s = self.step(s, self.B, dt=2.0, ctxt=3000, intr=5500)
        c0, c1 = s["usage"]["cores"]
        # cpu0: user 120, system 40, idle 200, iowait 20, irq 10, softirq 10 = 400 ticks
        self.assertEqual(c0, {"id": 0, "busy": 45.0, "user": 30.0, "system": 10.0, "iowait": 5.0})
        self.assertEqual(c1, {"id": 1, "busy": 20.0, "user": 10.0, "system": 10.0, "iowait": 0.0})  # 50+50 of 500
        t = s["usage"]["total"]
        self.assertEqual((t["user"], t["system"], t["nice"], t["iowait"], t["irq"], t["steal"], t["idle"]),
                         (18.9, 10.0, 0.0, 2.2, 2.2, 0.0, 66.7))  # of 900 ticks
        self.assertEqual(t["busy"], 31.1)
        self.assertEqual(s["rates"], {"ctxt": 1000.0, "intr": 250.0, "running": 2, "blocked": 0})

    def test_a_call_too_close_to_the_last_returns_the_last_figures(self):
        s = self.start(self.A)
        first = self.step(s, self.B)
        again = self.step(s, {0: (900, 0, 900, 1000, 30, 10, 15, 0), 1: (250, 0, 150, 1100, 0, 0, 0, 0)}, dt=0.01)
        self.assertEqual(again["usage"], first["usage"])
        self.assertEqual(again["rates"]["ctxt"], first["rates"]["ctxt"])
        later = self.step(s, {0: (900, 0, 900, 1000, 30, 10, 15, 0), 1: (250, 0, 150, 1100, 0, 0, 0, 0)}, dt=2.0)
        self.assertEqual(later["usage"]["cores"][0]["busy"], 100.0)  # measured from the baseline that stayed put

    def test_first_sample_right_after_the_constructor_has_no_figures_yet(self):
        s = self.start(self.A).sample()  # no time passed: nothing to measure, so None, never since-boot averages
        self.assertEqual((s["usage"]["total"], s["usage"]["cores"]), (None, []))
        self.assertEqual((s["rates"]["ctxt"], s["rates"]["intr"]), (None, None))
        check_contract(self, s)

    def test_a_cpu_counter_going_backwards_keeps_the_previous_figures(self):
        s = self.start(self.A)
        good = self.step(s, self.B, ctxt=200000)
        # cpu1 went offline and online: its counters restart from 0; the aggregate steps back as well
        reset = {0: (300, 0, 100, 1200, 30, 10, 15, 0), 1: (5, 0, 5, 20, 0, 0, 0, 0)}
        s2 = self.step(s, reset, ctxt=1)  # ctxt also went back: no rate
        self.assertEqual(s2["usage"]["cores"][1], good["usage"]["cores"][1])
        self.assertEqual(s2["usage"]["cores"][0]["id"], 0)
        self.assertNotEqual(s2["usage"]["cores"][0], good["usage"]["cores"][0])
        self.assertIsNone(s2["rates"]["ctxt"])
        check_contract(self, s2)
        # and the next interval is measured normally again
        nxt = {0: (400, 0, 100, 1300, 30, 10, 15, 0), 1: (105, 0, 5, 120, 0, 0, 0, 0)}
        s3 = self.step(s, nxt, ctxt=2001)
        self.assertEqual(s3["usage"]["cores"][1]["busy"], 50.0)
        self.assertEqual(s3["rates"]["ctxt"], 1000.0)

    def test_iowait_may_step_back_a_little(self):
        s = self.start({0: (100, 0, 50, 800, 100, 0, 0, 0), 1: (0,) * 8})
        s = self.step(s, {0: (200, 0, 150, 1000, 99, 0, 0, 0), 1: (0,) * 8})  # proc(5): iowait is not monotonic
        c = s["usage"]["cores"][0]
        self.assertEqual((c["iowait"], c["user"], c["system"]), (0.0, 25.0, 25.0))
        self.assertEqual(c["busy"], 50.0)

    def test_a_cpu_going_offline_and_online(self):
        s = self.start(self.A)
        out = self.step(s, {0: self.B[0]})  # cpu1 gone from /proc/stat
        check_contract(self, out)
        self.assertEqual([c["id"] for c in out["usage"]["cores"]], [0])
        self.assertEqual(out["threads"], 1)
        out = self.step(s, self.B)  # and back: its first reading is a baseline, not a delta of nothing
        check_contract(self, out)
        self.assertEqual(out["threads"], 2)
        self.assertEqual([c["id"] for c in out["usage"]["cores"]], [0])
        out = self.step(s, {0: (300, 0, 100, 1100, 30, 10, 15, 0), 1: (300, 0, 200, 1200, 0, 0, 0, 0)})
        self.assertEqual([c["id"] for c in out["usage"]["cores"]], [0, 1])
        self.assertEqual(out["usage"]["cores"][1]["busy"], 50.0)

    def test_no_time_passed_in_the_counters(self):
        s = self.start(self.A)
        s = self.step(s, self.A)  # same counters: a CPU that did not tick (all offline?): no percentages, not 0 %
        self.assertEqual(s["usage"]["cores"], [])
        self.assertIsNone(s["usage"]["total"])

    def test_every_part_fails_alone(self):
        idle = intel_hybrid(self.root)
        self.stat(idle)
        for rel in ("proc/loadavg", "proc/uptime", "sys/devices/cpu_core/cpus", "sys/devices/cpu_atom/cpus"):
            os.remove(os.path.join(self.root, rel))
        shutil.rmtree(os.path.join(self.root, "sys/class/hwmon"))
        s = self.step(cpuinfo.CpuSampler(root=self.root, clock=self.clock), {i: (10,) * 8 for i in range(8)})
        check_contract(self, s)
        self.assertIsNone(s["load"])
        self.assertAlmostEqual(s["uptime"], time.time() - 1700000000, delta=5)  # /proc/uptime gone: from btime
        self.assertEqual(s["kinds"], {})
        self.assertIsNone(s["temps"]["package"])
        self.assertTrue(any(n.startswith("load:") for n in s["notes"]), s["notes"])
        self.assertEqual(s["model"], "12th Gen Intel(R) Core(TM) i7-1260P")  # the rest is still there

    def test_without_proc_stat(self):
        plain_vm(self.root)  # no /proc/stat written
        s = cpuinfo.CpuSampler(root=self.root).sample()
        check_contract(self, s)
        self.assertEqual((s["usage"]["total"], s["usage"]["cores"]), (None, []))
        self.assertTrue(any(n.startswith("usage:") for n in s["notes"]), s["notes"])
        self.assertEqual(s["model"], "Intel(R) Xeon(R) Gold 6230 CPU @ 2.10GHz")

    def test_an_empty_tree_never_raises(self):
        s = cpuinfo.CpuSampler(root=self.root).sample()
        check_contract(self, s)
        self.assertGreaterEqual(len(s["notes"]), 3)

    def test_sample_is_cheap_with_32_cpus(self):
        def big(root):
            model = "Intel(R) Xeon(R) W-3335 CPU @ 3.40GHz"
            put(root, "proc/cpuinfo", "".join(X86_INFO.format(i=i, vendor="GenuineIntel", model=model, pkg=0, core=i // 2)
                                              for i in range(32)))
            put(root, "proc/loadavg", "1 1 1 1/1 1")
            put(root, "proc/uptime", "10 10")
            for i in range(32):
                sib = f"{i // 2 * 2}-{i // 2 * 2 + 1}"
                add_cpu(root, i, i // 2, 0, sib, [(1, "Data", "48K", sib), (2, "Unified", "1280K", sib), (3, "Unified", "36M", "0-31")],
                        {"scaling_cur_freq": 3000000, "cpuinfo_min_freq": 800000, "cpuinfo_max_freq": 4000000,
                         "scaling_governor": "powersave", "scaling_driver": "intel_pstate"},
                        {"core_throttle_count": 0, "package_throttle_count": 0, "package_throttle_total_time_ms": 0})
            hwmon(root, 0, "coretemp", [("Package id 0", 50000, 100000, 100000)] + [(f"Core {c}", 48000, 100000, 100000) for c in range(16)])
            return {i: (0,) * 8 for i in range(32)}
        s = self.sampler(big)
        self.assertEqual(s.sample()["threads"], 32)
        self.clock.t += 1
        self.stat({i: (50,) * 8 for i in range(32)})
        t0 = time.perf_counter()
        out = s.sample()
        took = time.perf_counter() - t0
        self.assertEqual(len(out["usage"]["cores"]), 32)
        self.assertLess(took, 0.2, f"sample() took {took * 1000:.1f} ms")  # about 2 ms where it matters; the bound only catches blunders


# ---- Linux: pure parsers ----------------------------------------------------------------------------------------------------

class LinuxParsers(unittest.TestCase):
    def test_cpulist_and_sizes(self):
        self.assertEqual(cpuinfo.parse_cpulist("0-7,16,18-19\n"), [0, 1, 2, 3, 4, 5, 6, 7, 16, 18, 19])
        self.assertEqual(cpuinfo.parse_cpulist(""), [])
        self.assertEqual(cpuinfo.parse_cpulist("3"), [3])
        self.assertEqual([cpuinfo.parse_size(x) for x in ("48K", "2048K", "36M", "512", "?", None)],
                         [49152, 2097152, 37748736, 512, None, None])

    def test_proc_stat(self):
        st = cpuinfo.parse_proc_stat(proc_stat({0: (1, 2, 3, 4, 5, 6, 7, 8), 1: (1, 1, 1, 1, 1, 1, 1, 1)}, ctxt=9, intr=8, running=3,
                                               blocked=1, btime=5))
        self.assertEqual(sorted(st["cpus"]), [0, 1])
        self.assertEqual(st["cpus"][0], dict(zip(cpuinfo.FIELDS, (1, 2, 3, 4, 5, 6, 7, 8))))
        self.assertEqual(st["total"]["user"], 2)
        self.assertEqual((st["ctxt"], st["intr"], st["running"], st["blocked"], st["btime"]), (9, 8, 3, 1, 5))
        old = cpuinfo.parse_proc_stat("cpu  1 2 3 4\ncpu0 1 2 3 4\n")  # a kernel without iowait, irq, steal
        self.assertEqual((old["cpus"][0]["idle"], old["cpus"][0]["iowait"], old["cpus"][0]["steal"]), (4, None, None))
        self.assertEqual(old["ctxt"], None)

    def test_usage_of(self):
        a = dict(zip(cpuinfo.FIELDS, (0,) * 8))
        b = dict(a, user=50, idle=50)
        u = cpuinfo.usage_of(a, b)
        self.assertEqual((u["busy"], u["user"], u["idle"]), (50.0, 50.0, 50.0))
        self.assertIsNone(cpuinfo.usage_of(a, a))
        self.assertIsNone(cpuinfo.usage_of(None, b))
        self.assertIsNone(cpuinfo.usage_of(b, a))  # everything went back
        self.assertEqual(cpuinfo.usage_of(a, dict(b, steal=None))["steal"], None)  # None stays None, never 0

    def test_rate(self):
        self.assertEqual(cpuinfo.rate(10, 30, 2.0), 10.0)
        self.assertIsNone(cpuinfo.rate(30, 10, 2.0))
        self.assertIsNone(cpuinfo.rate(None, 10, 2.0))
        self.assertIsNone(cpuinfo.rate(1, 2, 0))

    def test_cpuinfo_x86_and_arm(self):
        x = cpuinfo.parse_cpuinfo(X86_INFO.format(i=0, vendor="GenuineIntel", model="Intel(R)  Core(TM)  i5  CPU @ 1.60GHz", pkg=0, core=0)
                                  + X86_INFO.format(i=1, vendor="GenuineIntel", model="x", pkg=1, core=0))
        self.assertEqual((x["model"], x["vendor"]), ("Intel(R) Core(TM) i5 CPU @ 1.60GHz", "GenuineIntel"))
        self.assertEqual((x["packages"], x["pairs"]), ({"0", "1"}, {("0", "0"), ("1", "0")}))
        self.assertEqual(cpuinfo.brand_mhz(x["model"]), 1600)
        self.assertIsNone(cpuinfo.brand_mhz("AMD Ryzen 5 5600X 6-Core Processor"))
        arm = cpuinfo.parse_cpuinfo("processor : 0\nmodel name : ARMv7 Processor rev 3 (v7l)\nCPU implementer : 0x41\nCPU part : 0xc07\n\n"
                                    "Hardware : BCM2835\nSerial : 00000000cafebabe\n")
        self.assertEqual((arm["model"], arm["vendor"]), ("Cortex-A7", "ARM"))
        only_board = cpuinfo.parse_cpuinfo("processor : 0\nHardware : Some SoC\n")
        self.assertEqual(only_board["model"], "Some SoC")
        self.assertEqual(cpuinfo.parse_cpuinfo("")["model"], None)


# ---- macOS: sysctl values ------------------------------------------------------------------------------------------------------

def i32(v):
    return struct.pack("<i", v)


def i64(v):
    return struct.pack("<q", v)


def text(v):
    return v.encode() + b"\0"


def apple_silicon():
    """Apple M1 Pro: 8 P + 2 E; no hw.cpufrequency, no machdep.cpu.vendor, no L3."""
    d = {"machdep.cpu.brand_string": text("Apple M1 Pro"), "hw.logicalcpu": i32(10), "hw.physicalcpu": i32(10), "hw.packages": i32(1),
         "hw.nperflevels": i32(2), "hw.l1dcachesize": i64(65536), "hw.l2cachesize": i64(4194304)}
    for n, (name, cpus, l1d, l1i, l2, per) in enumerate((("Performance", 8, 131072, 196608, 12582912, 4),
                                                        ("Efficiency", 2, 65536, 131072, 4194304, 2))):
        d.update({f"hw.perflevel{n}.name": text(name), f"hw.perflevel{n}.physicalcpu": i32(cpus),
                  f"hw.perflevel{n}.logicalcpu": i32(cpus), f"hw.perflevel{n}.l1dcachesize": i32(l1d),
                  f"hw.perflevel{n}.l1icachesize": i32(l1i), f"hw.perflevel{n}.l2cachesize": i32(l2),
                  f"hw.perflevel{n}.cpusperl2": i32(per)})
    return d


def intel_mac():
    """MacBook Pro i7-9750H: 6 cores, 12 threads; the clock sysctls only on Intel; hw.cacheconfig for the sharing."""
    return {"machdep.cpu.brand_string": text("Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz"), "machdep.cpu.vendor": text("GenuineIntel"),
            "hw.logicalcpu": i32(12), "hw.physicalcpu": i32(6), "hw.packages": i32(1), "hw.cpufrequency": i64(2600000000),
            "hw.cpufrequency_max": i64(2600000000), "hw.cpufrequency_min": i64(2600000000), "hw.l1dcachesize": i64(32768),
            "hw.l1icachesize": i64(32768), "hw.l2cachesize": i64(262144), "hw.l3cachesize": i64(12582912),
            "hw.cacheconfig": struct.pack("<4Q", 12, 2, 2, 12)}


class MacParsers(unittest.TestCase):
    def test_sysctl_values(self):
        self.assertEqual((cpuinfo.sysctl_int(i32(12)), cpuinfo.sysctl_int(i64(2600000000)), cpuinfo.sysctl_int(None)), (12, 2600000000, None))
        self.assertEqual(cpuinfo.sysctl_str(text("  Apple   M2  ")), "Apple M2")
        self.assertIsNone(cpuinfo.sysctl_str(b"\0"))
        self.assertEqual(cpuinfo.sysctl_ints(struct.pack("<3Q", 1, 2, 3)), [1, 2, 3])
        self.assertEqual(cpuinfo.sysctl_ints(None), [])
        self.assertEqual(cpuinfo.parse_timeval(struct.pack("<qi4x", 1700000000, 500000)), 1700000000.5)
        with self.assertRaises(ValueError):
            cpuinfo.parse_timeval(None)

    def test_apple_silicon_facts(self):
        f = cpuinfo.mac_facts(apple_silicon().get)
        self.assertEqual((f["model"], f["vendor"], f["sockets"], f["cores"], f["threads"]), ("Apple M1 Pro", "Apple", 1, 10, 10))
        self.assertEqual(f["kinds"], {"P": 8, "E": 2})  # the ids are not known on a Mac: counts
        self.assertEqual(f["cache"], {"L1d": 8 * 131072 + 2 * 65536, "L1i": 8 * 196608 + 2 * 131072,
                                      "L2": 2 * 12582912 + 4194304})  # L2 per cluster
        self.assertEqual((f["cur"], f["min"], f["max"], f["base"]), (None, None, None, None))  # no clock sysctl on Apple Silicon

    def test_intel_mac_facts(self):
        f = cpuinfo.mac_facts(intel_mac().get)
        self.assertEqual((f["model"], f["vendor"], f["sockets"], f["cores"], f["threads"]),
                         ("Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz", "GenuineIntel", 1, 6, 12))
        self.assertEqual(f["kinds"], {})
        self.assertEqual(f["cache"], {"L1d": 6 * 32768, "L1i": 6 * 32768, "L2": 6 * 262144, "L3": 12582912})
        self.assertEqual((f["cur"], f["base"], f["min"], f["max"]), (2600, 2600, None, None))  # min = max = nominal: no range

    def test_unknown_values_stay_unknown(self):
        f = cpuinfo.mac_facts({}.get)
        self.assertEqual((f["model"], f["vendor"], f["cores"], f["cache"], f["kinds"], f["cur"]), (None, None, None, {}, {}, None))

    def test_mac_reader_through_the_sampler(self):
        sysctl = intel_mac()
        sysctl["kern.boottime"] = struct.pack("<qi4x", int(time.time()) - 3600, 0)
        ticks = [[(100, 50, 800, 0), (200, 100, 700, 0), (0, 0, 0, 0), (0, 0, 0, 0)]]
        clock = Clock()
        reader = cpuinfo.MacReader(get=sysctl.get, ticks=lambda: ticks[0])
        s = cpuinfo.CpuSampler(system="darwin", reader=reader, clock=clock)
        clock.t += 2
        ticks[0] = [(150, 100, 900, 10), (300, 100, 800, 0), (10, 0, 90, 0), (0, 0, 100, 0)]  # (user, system, idle, nice)
        with mock.patch.object(os, "getloadavg", create=True, return_value=(1.5, 1.25, 1.0)):
            out = s.sample()
        check_contract(self, out)
        self.assertEqual(out["model"], "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz")
        self.assertEqual(out["freq"]["cur"], {-1: 2600})
        self.assertEqual(out["load"], [1.5, 1.25, 1.0])
        self.assertAlmostEqual(out["uptime"], 3600, delta=5)
        c0 = out["usage"]["cores"][0]  # +50 user +50 system +100 idle +10 nice = 210
        self.assertEqual((c0["busy"], c0["user"], c0["system"], c0["iowait"]), (52.4, 23.8, 23.8, None))
        self.assertEqual([c["id"] for c in out["usage"]["cores"]], [0, 1, 2, 3])
        t = out["usage"]["total"]
        self.assertEqual((t["nice"], t["iowait"], t["irq"], t["steal"]), (1.6, None, None, None))  # not on macOS: None, not 0
        self.assertEqual(out["rates"], {"ctxt": None, "intr": None, "running": None, "blocked": None})
        self.assertEqual(out["temps"]["source"], None)  # the collector provides the macOS temperatures

    def test_mac_failures_are_notes(self):
        def boom():
            raise OSError("host_processor_info failed")
        s = cpuinfo.CpuSampler(system="darwin", reader=cpuinfo.MacReader(get={}.get, ticks=boom))
        out = s.sample()
        check_contract(self, out)
        self.assertEqual(out["usage"], {"total": None, "cores": []})
        self.assertTrue(any(n.startswith("usage:") for n in out["notes"]), out["notes"])


# ---- Windows: structures built here --------------------------------------------------------------------------------------------

def slpi_core(eff, masks, ptr=8):
    aff = ptr + 8
    b = bytearray(32 + aff * len(masks))
    struct.pack_into("<II", b, 0, 0, len(b))
    b[9] = eff
    struct.pack_into("<H", b, 30, len(masks))
    for k, (g, m) in enumerate(masks):
        o = 32 + k * aff
        b[o:o + ptr] = m.to_bytes(ptr, "little")
        struct.pack_into("<H", b, o + ptr, g)
    return bytes(b)


def slpi_cache(level, kind, size, mask, ptr=8):
    b = bytearray(40 + ptr + 8)
    struct.pack_into("<II", b, 0, 2, len(b))
    b[8] = level
    struct.pack_into("<I", b, 12, size)
    struct.pack_into("<I", b, 16, kind)  # 0 unified, 1 instruction, 2 data
    struct.pack_into("<H", b, 38, 1)
    b[40:40 + ptr] = mask.to_bytes(ptr, "little")
    return bytes(b)


def slpi_package(mask, ptr=8):
    b = bytearray(32 + ptr + 8)
    struct.pack_into("<II", b, 0, 3, len(b))
    struct.pack_into("<H", b, 30, 1)
    b[32:32 + ptr] = mask.to_bytes(ptr, "little")
    return bytes(b)


def slpi_groups(active, ptr=8):
    info = 40 + ptr
    b = bytearray(32 + info * len(active))
    struct.pack_into("<II", b, 0, 4, len(b))
    struct.pack_into("<HH", b, 8, len(active), len(active))
    for g, n in enumerate(active):
        b[32 + g * info + 0] = n
        b[32 + g * info + 1] = n
    return bytes(b)


def win_hybrid(ptr=8):
    """2 P cores with SMT (CPU 0-3, EfficiencyClass 1) and 4 E cores (CPU 4-7, class 0), one package, one group of 8."""
    recs = [slpi_core(1, [(0, 0b11)], ptr), slpi_core(1, [(0, 0b1100)], ptr)]
    recs += [slpi_core(0, [(0, 1 << i)], ptr) for i in (4, 5, 6, 7)]
    recs += [slpi_cache(1, 2, 48 << 10, 0b11, ptr), slpi_cache(1, 2, 48 << 10, 0b1100, ptr)]
    recs += [slpi_cache(1, 2, 32 << 10, 1 << i, ptr) for i in (4, 5, 6, 7)]
    recs += [slpi_cache(1, 1, 32 << 10, 0b11, ptr), slpi_cache(1, 1, 32 << 10, 0b1100, ptr)]
    recs += [slpi_cache(2, 0, 1280 << 10, 0b11, ptr), slpi_cache(2, 0, 1280 << 10, 0b1100, ptr), slpi_cache(2, 0, 2048 << 10, 0xF0, ptr)]
    recs += [slpi_cache(3, 0, 18 << 20, 0xFF, ptr), slpi_package(0xFF, ptr), slpi_groups([8], ptr)]
    return b"".join(recs)


def perf_raw(rows):
    arr = (winapi.CpuPerf * len(rows))()
    for a, r in zip(arr, rows):
        a.idle, a.kernel, a.user, a.dpc, a.interrupt, a.interrupts = r
    return bytes(arr)


def power_raw(rows):
    arr = (winapi.PowerInfo * len(rows))()
    for a, (number, mx, cur, limit) in zip(arr, rows):
        a.number, a.max_mhz, a.current_mhz, a.mhz_limit = number, mx, cur, limit
    return bytes(arr)


def spi_raw(ctxt, size=312):
    b = bytearray(size)
    struct.pack_into("<I", b, cpuinfo.SPI_CONTEXT_SWITCHES, ctxt)
    return bytes(b)


class FakePdh(object):
    def __init__(self, path):
        self.path = path
        self.data = {"0,0": 120.0, "0,1": 50.0, "0,_Total": 85.0, "_Total": 85.0}

    def values(self):
        if self.data is None:
            raise OSError("PdhCollectQueryData failed")
        return self.data


class FakeWin(object):
    def __init__(self, ptr=8):
        self.slpi = win_hybrid(ptr)
        self.perf = [(1000, 2000, 500, 20, 30, 100), (900, 1900, 400, 10, 10, 50)] + [(0, 0, 0, 0, 0, 0)] * 6
        self.ctxt = 1000
        self.pdh = None
        self.PdhCounter = self.make_pdh

    def make_pdh(self, path):
        self.pdh = FakePdh(path)
        return self.pdh

    def logical_processor_info(self):
        return self.slpi

    def processor_registry(self):
        return {"name": "12th Gen Intel(R) Core(TM) i7-1260P          ", "vendor": "GenuineIntel", "mhz": 2100}

    def power_info(self, n):
        return power_raw([(i, 2100, 1800, 2100) for i in range(8)])

    def cpu_perf(self):
        return [(0, perf_raw(self.perf))], True

    def system_performance_info(self):
        return spi_raw(self.ctxt)

    def uptime(self):
        return 3600.0


class WindowsParsers(unittest.TestCase):
    def test_structure_sizes(self):
        self.assertEqual(ctypes.sizeof(winapi.PowerInfo), 24)  # PROCESSOR_POWER_INFORMATION
        self.assertEqual(ctypes.sizeof(winapi.PdhValue), 16)
        self.assertEqual(ctypes.sizeof(winapi.PdhItem), 16 + ctypes.sizeof(ctypes.c_void_p) + (8 - ctypes.sizeof(ctypes.c_void_p)) % 8)

    def test_cores_caches_sockets_and_efficiency_classes(self):
        for ptr in (8, 4):  # 64-bit and 32-bit layouts
            lay = cpuinfo.parse_slpi_ex(win_hybrid(ptr), ptr)
            self.assertEqual((lay["cores"], lay["sockets"], lay["groups"]), (6, 1, [8]), ptr)
            self.assertEqual(lay["kinds"], {"P": [0, 1, 2, 3], "E": [4, 5, 6, 7]}, ptr)
            self.assertEqual(lay["cache"], {"L1d": (2 * 48 + 4 * 32) << 10, "L1i": 64 << 10, "L2": (2 * 1280 + 2048) << 10,
                                            "L3": 18 << 20}, ptr)

    def test_one_efficiency_class_is_not_hybrid(self):
        raw = slpi_core(0, [(0, 3)]) + slpi_core(0, [(0, 12)]) + slpi_package(15)
        lay = cpuinfo.parse_slpi_ex(raw)
        self.assertEqual((lay["cores"], lay["kinds"]), (2, {}))

    def test_processor_groups_number_the_cpus_across_groups(self):
        raw = slpi_core(1, [(0, 0b100)]) + slpi_core(0, [(1, 0b1)]) + slpi_core(0, [(1, 0b10)]) + slpi_groups([3, 2])
        lay = cpuinfo.parse_slpi_ex(raw)
        self.assertEqual(lay["offsets"], {0: 0, 1: 3})
        self.assertEqual(lay["kinds"], {"P": [2], "E": [3, 4]})

    def test_garbage_does_not_raise_or_loop(self):
        self.assertEqual(cpuinfo.parse_slpi_ex(b"")["cores"], 0)
        self.assertEqual(cpuinfo.parse_slpi_ex(b"\0" * 64)["cores"], 0)  # a record of size 0: stop
        self.assertEqual(cpuinfo.parse_slpi_ex(struct.pack("<II", 0, 9999) + b"\0" * 8)["cores"], 0)  # a size past the end

    def test_cpu_perf_and_power_info(self):
        rows = cpuinfo.parse_cpu_perf(perf_raw([(1, 2, 3, 4, 5, 6), (7, 8, 9, 10, 11, 12)]))
        self.assertEqual(rows, [(1, 2, 3, 4, 5, 6), (7, 8, 9, 10, 11, 12)])
        self.assertEqual(cpuinfo.parse_cpu_perf(b"short"), [])
        p = cpuinfo.parse_power_info(power_raw([(0, 2100, 1800, 2100), (1, 2100, 2100, 2100)]))
        self.assertEqual(p[1], {"number": 1, "max": 2100, "cur": 2100, "limit": 2100})
        self.assertEqual(len(p), 2)

    def test_context_switches_and_pdh_names(self):
        self.assertEqual(cpuinfo.parse_context_switches(spi_raw(123456)), 123456)
        with self.assertRaises(ValueError):
            cpuinfo.parse_context_switches(b"\0" * 100)
        v = {"0,0": 1.0, "0,1": 2.0, "1,0": 3.0, "0,_Total": 9.0, "_Total": 9.0}
        self.assertEqual(cpuinfo.pdh_cpu_values(v, {0: 0, 1: 8}), {0: 1.0, 1: 2.0, 8: 3.0})


class WindowsReaderThroughTheSampler(unittest.TestCase):
    def sampler(self, api):
        self.clock = Clock()
        return cpuinfo.CpuSampler(system="win32", reader=cpuinfo.WindowsReader(api), clock=self.clock)

    def test_machine_and_deltas(self):
        api = FakeWin()
        s = self.sampler(api)
        self.clock.t += 2
        api.perf[0] = (2000, 3500, 800, 120, 130, 160)  # cpu0: idle +1000 kernel +1500 user +300 dpc +100 interrupt +100
        api.ctxt = 5000
        out = s.sample()
        check_contract(self, out)
        self.assertEqual((out["model"], out["vendor"]), ("12th Gen Intel(R) Core(TM) i7-1260P", "GenuineIntel"))
        self.assertEqual((out["sockets"], out["cores"], out["threads"]), (1, 6, 8))
        self.assertEqual(out["kinds"], {"P": [0, 1, 2, 3], "E": [4, 5, 6, 7]})
        self.assertEqual(out["cache"]["L2"], (2 * 1280 + 2048) << 10)
        c0 = out["usage"]["cores"][0]  # system = kernel - idle - dpc - interrupt = 300 of 1800
        self.assertEqual((c0["busy"], c0["user"], c0["system"], c0["iowait"]), (44.4, 16.7, 16.7, None))
        self.assertEqual(len(out["usage"]["cores"]), 1)  # the other CPUs did not tick: no figure, not 0 %
        t = out["usage"]["total"]
        self.assertEqual((t["busy"], t["idle"], t["irq"], t["iowait"], t["steal"]), (44.4, 55.6, 11.1, None, None))
        self.assertEqual(out["rates"]["ctxt"], 2000.0)
        self.assertEqual(out["rates"]["intr"], 30.0)  # the CPUs' interrupt counts added up: (160 - 100) / 2 s
        self.assertIsNone(out["load"])
        self.assertEqual(out["uptime"], 3600.0)
        self.assertEqual(out["freq"]["cur"], {0: 2520, 1: 1050})  # 2100 MHz x 120 % (turbo) and x 50 %
        self.assertEqual((out["freq"]["base"], out["freq"]["min"], out["freq"]["max"]), (2100, None, None))
        self.assertEqual(out["temps"]["source"], None)
        self.assertEqual(out["notes"], [])

    def test_context_switch_counter_wraps_at_32_bits(self):
        api = FakeWin()
        s = self.sampler(api)
        api.ctxt = 4294967000
        self.clock.t += 1
        s.sample()
        api.ctxt = 100  # wrapped: 396 more
        self.clock.t += 1
        self.assertEqual(s.sample()["rates"]["ctxt"], 396.0)

    def test_clock_without_pdh_falls_back_on_the_power_information(self):
        api = FakeWin()
        s = self.sampler(api)
        api.pdh.data = None
        self.clock.t += 2
        out = s.sample()
        self.assertEqual(out["freq"]["cur"], {i: 1800 for i in range(8)})  # CurrentMhz: never above the rated clock
        api2 = FakeWin()
        api2.PdhCounter = mock.Mock(side_effect=OSError("no PDH"))
        out = self.sampler(api2).sample()
        self.assertEqual(out["freq"]["cur"], {i: 1800 for i in range(8)})
        self.assertIn("clock: rated value only (PDH unavailable)", out["notes"])

    def test_failures_are_notes_and_leave_the_rest(self):
        api = FakeWin()
        api.system_performance_info = mock.Mock(side_effect=OSError(5, "denied"))
        api.logical_processor_info = mock.Mock(side_effect=OSError(5, "denied"))
        s = self.sampler(api)
        self.clock.t += 2
        api.perf[0] = (2000, 3500, 800, 120, 130, 160)
        out = s.sample()
        check_contract(self, out)
        self.assertIsNone(out["rates"]["ctxt"])
        self.assertTrue(any(n.startswith("context switches:") for n in out["notes"]), out["notes"])
        self.assertTrue(any(n.startswith("topology:") for n in out["notes"]), out["notes"])
        self.assertEqual(out["model"], "12th Gen Intel(R) Core(TM) i7-1260P")
        self.assertEqual(len(out["usage"]["cores"]), 1)

    def test_more_than_64_cpus_without_the_other_groups(self):
        api = FakeWin()
        api.cpu_perf = lambda: ([(0, perf_raw(api.perf))], False)
        s = self.sampler(api)
        self.clock.t += 2
        api.perf[0] = (2000, 3500, 800, 120, 130, 160)
        api.slpi = win_hybrid() + slpi_groups([64, 64])  # a second group record: 128 CPUs
        out = s.sample()
        self.assertTrue(any(n.startswith("usage: processor group 0 only") for n in out["notes"]), out["notes"])
        check_contract(self, out)


class OtherSystems(unittest.TestCase):
    def test_unknown_os_gets_the_stdlib(self):
        s = cpuinfo.CpuSampler(system="freebsd13").sample()
        check_contract(self, s)
        self.assertEqual(s["threads"], os.cpu_count() or 1)
        self.assertEqual(s["usage"], {"total": None, "cores": []})
        self.assertTrue(any(n.startswith("usage:") for n in s["notes"]), s["notes"])


# ---- the real thing, on the OS the tests run on ----------------------------------------------------------------------------------

class RealHost(unittest.TestCase):
    def test_real_sampler_keeps_the_contract(self):
        s = cpuinfo.CpuSampler()
        time.sleep(0.15)
        t0 = time.perf_counter()
        out = s.sample()
        took = time.perf_counter() - t0
        check_contract(self, out)
        self.assertEqual(out["threads"], os.cpu_count() or 1)
        self.assertLess(took, 1.0, f"sample() took {took * 1000:.0f} ms")
        self.assertTrue(out["usage"]["total"] is None or 0 <= out["usage"]["total"]["busy"] <= 100)

    @unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists("/proc/stat"), "Linux with /proc")
    def test_linux_reads_the_counters(self):
        s = cpuinfo.CpuSampler()
        time.sleep(0.15)
        out = s.sample()
        self.assertIsNotNone(out["usage"]["total"])
        self.assertTrue(out["usage"]["cores"])
        self.assertIsNotNone(out["load"])
        self.assertGreater(out["uptime"], 0)
        self.assertIsNotNone(out["rates"]["running"])


if __name__ == "__main__":
    unittest.main()
