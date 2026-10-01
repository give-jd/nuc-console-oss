"""nuc-console CPU screen: what the processor is and what each core does (renderer side, unprivileged, every OS).

CpuSampler().sample() returns the dict of the CPU data contract (feat/cpu, section 1). What cannot be read is None (or
absent from a per-item dict), never 0 or a guess; every part is read in its own try and a failure leaves a short note.

  Linux    /proc/stat, /proc/loadavg, /proc/uptime, /proc/cpuinfo and /sys (topology, cpufreq, caches, hybrid PMUs,
           hwmon and thermal zones, thermal_throttle). Every path is under `root`: the tests build fake trees.
  macOS    host_processor_info and sysctlbyname through ctypes (hostinfo.py). Temperatures come from the collector.
  Windows  NtQuerySystemInformation, GetLogicalProcessorInformationEx, CallNtPowerInformation, PDH (English counter path,
           the same on every Windows language) and the registry (winapi.py). Temperatures come from the collector.
  other    os.cpu_count(), platform.processor(), os.getloadavg().
No subprocess. The static facts (model, topology, caches, clock limits, which sensor files to read) are read once, and
again only if the set of online CPUs changes; sample() reads the counters and turns them into percentages and rates.
The parsers are pure functions (bytes or text in, data out), tested on every OS.
"""
import ctypes
import math
import os
import platform
import re
import struct
import sys
import time

import hostinfo  # same directory: macOS system calls
import winapi    # same directory: Windows system calls (imports on any OS, loads DLLs only when called)

FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")  # /proc/stat order (guest is inside user)
MIN_INTERVAL = 0.05  # s: a call closer than this to the previous one returns the previous figures, not a delta of nothing
JITTER = 0.01        # a counter may step back by this fraction of the interval (proc(5): iowait can decrease); more = reset
REPLAN_S = 60        # s: how often to look again for a temperature sensor when none was found (a module loaded late)


def empty_temps():
    return {"package": None, "cores": {}, "sensors": [], "high": None, "crit": None, "source": None}


def empty_sample():
    """The contract's shape with nothing known."""
    return {"model": None, "vendor": None, "arch": platform.machine() or None, "sockets": None, "cores": None,
            "threads": os.cpu_count() or 1, "kinds": {}, "cache": {},
            "freq": {"cur": {}, "min": None, "max": None, "base": None, "governor": None, "driver": None},
            "usage": {"total": None, "cores": []}, "load": None,
            "rates": {"ctxt": None, "intr": None, "running": None, "blocked": None}, "uptime": None,
            "temps": empty_temps(), "throttle": {"package": None, "cores": {}, "package_s": None}, "notes": []}


def note(part, err):
    """A short reason for the notes list: the message of our own errors, the error class for the others."""
    if isinstance(err, (NotImplementedError, LookupError)) and str(err):
        return f"{part}: {str(err).strip(chr(39))[:60]}"
    return f"{part}: unreadable ({err.__class__.__name__})"


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, ValueError):  # ValueError: undecodable bytes
        return None


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def _natural(name):
    m = re.search(r"(\d+)$", name)
    return (int(m.group(1)) if m else -1, name)


def _mhz(khz):
    return int(round(khz / 1000.0))


def _clean(text):
    """Collapse the padding some firmware puts in model strings; '' -> None."""
    return " ".join((text or "").split()) or None


# ---- shared: percentages and rates from two readings ----------------------------------------------------------------------

def usage_of(prev, cur):
    """Percentages of the time between two readings of one CPU's counters (dicts of FIELDS, None = not on this OS).

    None when no time passed or a counter went back by more than jitter (a CPU going offline and online again): that
    interval cannot be measured. Small steps back (iowait is documented to decrease) count as 0. Percentages are 0-100."""
    if not prev or not cur:
        return None
    d = {}
    for k in FIELDS:
        a, b = prev.get(k), cur.get(k)
        d[k] = None if a is None or b is None else b - a
    ahead = sum(v for v in d.values() if v and v > 0)
    back = -sum(v for v in d.values() if v and v < 0)
    if ahead <= 0 or back > JITTER * ahead:
        return None
    d = {k: (None if v is None else max(v, 0)) for k, v in d.items()}
    tot = float(sum(v for v in d.values() if v))

    def pct(v):
        return None if v is None else round(min(100.0, 100.0 * v / tot), 1)

    irq = None if d["irq"] is None and d["softirq"] is None else (d["irq"] or 0) + (d["softirq"] or 0)
    return {"busy": pct(tot - (d["idle"] or 0) - (d["iowait"] or 0)), "user": pct(d["user"]), "system": pct(d["system"]),
            "nice": pct(d["nice"]), "iowait": pct(d["iowait"]), "irq": pct(irq), "steal": pct(d["steal"]),
            "idle": pct(d["idle"])}


def sum_fields(cpus):
    """The machine total of per-CPU counters (macOS and Windows have no aggregate line)."""
    out = {}
    for k in FIELDS:
        vals = [c.get(k) for c in cpus.values()]
        out[k] = None if not vals or any(v is None for v in vals) else sum(vals)
    return out


def rate(prev, cur, dt):
    if prev is None or cur is None or dt <= 0 or cur < prev:  # a counter going back: no figure for this interval
        return None
    return round((cur - prev) / dt, 1)


def brand_mhz(model):
    """'Intel(R) Xeon(R) Gold 6230 CPU @ 2.10GHz' -> 2100: the nominal clock printed in the brand string, or None."""
    m = re.search(r"@\s*(\d+(?:\.\d+)?)\s*GHz", model or "")
    return int(round(float(m.group(1)) * 1000)) if m else None


# ---- Linux: parsers (pure) -------------------------------------------------------------------------------------------------

ARM_VENDORS = {0x41: "ARM", 0x42: "Broadcom", 0x43: "Cavium", 0x46: "Fujitsu", 0x48: "HiSilicon", 0x4E: "NVIDIA",
               0x50: "APM", 0x51: "Qualcomm", 0x53: "Samsung", 0x56: "Marvell", 0x61: "Apple", 0x69: "Intel",
               0x6D: "Microsoft", 0x70: "Phytium", 0xC0: "Ampere"}
ARM_PARTS = {0xC05: "Cortex-A5", 0xC07: "Cortex-A7", 0xC08: "Cortex-A8", 0xC09: "Cortex-A9", 0xC0D: "Cortex-A12",
             0xC0E: "Cortex-A17", 0xC0F: "Cortex-A15", 0xD01: "Cortex-A32", 0xD02: "Cortex-A34", 0xD03: "Cortex-A53",
             0xD04: "Cortex-A35", 0xD05: "Cortex-A55", 0xD06: "Cortex-A65", 0xD07: "Cortex-A57", 0xD08: "Cortex-A72",
             0xD09: "Cortex-A73", 0xD0A: "Cortex-A75", 0xD0B: "Cortex-A76", 0xD0C: "Neoverse-N1", 0xD0D: "Cortex-A77",
             0xD0E: "Cortex-A76AE", 0xD40: "Neoverse-V1", 0xD41: "Cortex-A78", 0xD42: "Cortex-A78AE", 0xD44: "Cortex-X1",
             0xD46: "Cortex-A510", 0xD47: "Cortex-A710", 0xD48: "Cortex-X2", 0xD49: "Neoverse-N2", 0xD4A: "Neoverse-E1",
             0xD4B: "Cortex-A78C", 0xD4D: "Cortex-A715", 0xD4E: "Cortex-X3", 0xD4F: "Neoverse-V2", 0xD80: "Cortex-A520",
             0xD81: "Cortex-A720", 0xD82: "Cortex-X4", 0xD84: "Neoverse-V3", 0xD85: "Cortex-X925", 0xD87: "Cortex-A725",
             0xD8E: "Neoverse-N3"}  # implementer 0x41 (ARM Ltd), as lscpu names them


def parse_cpulist(text):
    """The kernel's cpu list format: '0-7,16,18-19' -> [0, ..., 7, 16, 18, 19]; '' -> []."""
    out = set()
    for part in (text or "").replace("\n", ",").split(","):
        part = part.strip()
        if part:
            a, _, b = part.partition("-")
            out.update(range(int(a), int(b or a) + 1))
    return sorted(out)


def parse_size(text):
    """sysfs cache size '48K', '2048K', '1M' -> bytes; None if unreadable."""
    m = re.match(r"^\s*(\d+)\s*([KMG]?)", text or "")
    return int(m.group(1)) * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[m.group(2)] if m else None


def parse_proc_stat(text):
    """/proc/stat -> {'cpus': {id: {field: ticks}}, 'total': {field: ticks}, 'ctxt', 'intr', 'running', 'blocked', 'btime'}.

    Fields an old kernel does not have are None; guest/guest_nice are already inside user/nice and are left out."""
    out = {"cpus": {}, "total": None, "ctxt": None, "intr": None, "running": None, "blocked": None, "btime": None}
    names = {"ctxt": "ctxt", "intr": "intr", "procs_running": "running", "procs_blocked": "blocked", "btime": "btime"}
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0].startswith("cpu"):
            vals = [int(x) for x in f[1:1 + len(FIELDS)]]
            t = {k: (vals[i] if i < len(vals) else None) for i, k in enumerate(FIELDS)}
            if f[0] == "cpu":
                out["total"] = t
            elif f[0][3:].isdigit():
                out["cpus"][int(f[0][3:])] = t
        elif f[0] in names and len(f) > 1 and f[1].isdigit():
            out[names[f[0]]] = int(f[1])
    return out


def parse_cpuinfo(text):
    """/proc/cpuinfo -> {'model', 'vendor', 'packages': set, 'pairs': {(physical id, core id)}}.

    x86 has 'model name' and 'vendor_id'. ARM often has only 'CPU implementer'/'CPU part' (named here as lscpu does) and,
    once, 'Model' (the board) and 'Hardware'; POWER has 'cpu', RISC-V 'uarch'. The 'Serial' line is never read."""
    blocks, cur = [], {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if not sep:
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        cur[k.strip()] = v.strip()
    if cur:
        blocks.append(cur)
    cpus = [b for b in blocks if "processor" in b]

    def first(key):
        return next((b[key] for b in blocks if b.get(key)), None)

    parts, vendor = [], None
    for b in cpus:
        try:
            impl, part = int(b.get("CPU implementer", ""), 16), int(b.get("CPU part", ""), 16)
        except ValueError:
            continue
        vendor = vendor or ARM_VENDORS.get(impl)
        name = ARM_PARTS.get(part) if impl == 0x41 else None
        if name and name not in parts:
            parts.append(name)
    model = first("model name")
    if parts and (not model or model.startswith("ARMv")):  # 'ARMv7 Processor rev 3 (v7l)' says less than 'Cortex-A72'
        model = " + ".join(parts)
    model = model or first("uarch") or first("cpu") or first("Model") or first("Hardware")
    return {"model": _clean(model), "vendor": first("vendor_id") or vendor,
            "packages": {b["physical id"] for b in cpus if b.get("physical id")},
            "pairs": {(b["physical id"], b["core id"]) for b in cpus if b.get("physical id") and b.get("core id")}}


def cache_name(level, kind):
    if level == 1:
        return {"Data": "L1d", "Instruction": "L1i"}.get(kind)
    return {2: "L2", 3: "L3"}.get(level) if kind != "Instruction" else None


def soc_sensor(name):
    """ARM thermal zones exported as hwmon ('cpu-thermal' zone -> 'cpu_thermal'): the CPU or the whole SoC."""
    n = name.replace("-", "_")
    return n.endswith("_thermal") and n.startswith(("cpu", "soc", "bigcore", "littlecore", "cluster"))


def _soc_rank(name):
    n = name.replace("-", "_")
    return ({"cpu_thermal": 0, "soc_thermal": 1}.get(n, 2), n)


CPU_DRIVERS = ("coretemp", "zenpower", "k10temp", "k8temp", "via_cputemp")  # zenpower replaces k10temp when loaded


# ---- Linux ------------------------------------------------------------------------------------------------------------------

class LinuxReader(object):
    def __init__(self, root="/"):
        self.root = root
        self.sys_cpu = os.path.join(root, "sys", "devices", "system", "cpu")
        self.info = {"packages": set(), "pairs": set()}
        self.core_of = {}
        self.freq_files, self.policy = [], None
        self.throttle_dirs = []
        self.plan, self.plan_t = None, 0.0
        self.btime = None

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def cpu(self, i, *parts):
        return os.path.join(self.sys_cpu, f"cpu{i}", *parts)

    def online(self, ids):
        """The CPUs of /proc/stat; without it, the cpuN directories of sysfs."""
        return ids or sorted(int(d[3:]) for d in _listdir(self.sys_cpu) if re.match(r"cpu\d+$", d))

    # static facts
    def identity(self, ids):
        with open(self.p("proc", "cpuinfo")) as f:
            self.info = parse_cpuinfo(f.read())
        return {"model": self.info["model"], "vendor": self.info["vendor"], "brand_base": brand_mhz(self.info["model"])}

    def topology(self, ids):
        ids = self.online(ids)
        siblings, keys, pkgs, core_of = set(), set(), set(), {}
        for i in ids:
            t = self.cpu(i, "topology")
            sib = _read(os.path.join(t, "core_cpus_list")) or _read(os.path.join(t, "thread_siblings_list"))
            core, pkg = _int(_read(os.path.join(t, "core_id"))), _int(_read(os.path.join(t, "physical_package_id")))
            die = _int(_read(os.path.join(t, "die_id"))) or 0
            siblings.add(sib)
            if core is not None and core >= 0:
                core_of[i] = core
                keys.add((pkg, die, core))
            if pkg is not None and pkg >= 0:
                pkgs.add(pkg)
        self.core_of = core_of
        cores = len(siblings) if siblings and None not in siblings else \
            len(keys) if keys and len(core_of) == len(ids) else len(self.info["pairs"]) or None
        self.throttle_dirs = [(i, core_of.get(i), self.cpu(i, "thermal_throttle")) for i in ids
                              if os.path.isdir(self.cpu(i, "thermal_throttle"))]
        return {"sockets": len(pkgs) or len(self.info["packages"]) or None, "cores": cores, "threads": len(ids) or None,
                "kinds": self.kinds(ids)}

    def kinds(self, ids):
        """Intel hybrid: the cpu_core (P) and cpu_atom (E) PMUs list their CPUs. ARM big.LITTLE: cpu_capacity, the
        kernel's relative performance of each CPU; the lowest capacity is E when it is clearly lower (not a few %)."""
        p, e = _read(self.p("sys", "devices", "cpu_core", "cpus")), _read(self.p("sys", "devices", "cpu_atom", "cpus"))
        if p or e:
            return {k: parse_cpulist(v) for k, v in (("P", p), ("E", e)) if v}
        caps = {i: _int(_read(self.cpu(i, "cpu_capacity"))) for i in ids}
        if ids and all(caps.values()) and min(caps.values()) <= 0.85 * max(caps.values()):
            low = min(caps.values())
            return {"P": [i for i in ids if caps[i] != low], "E": [i for i in ids if caps[i] == low]}
        return {}

    def cache(self, ids):
        """Total size of each cache level, every cache counted once (shared_cpu_list tells which CPUs share it)."""
        seen, out, unknown = set(), {}, set()
        for i in self.online(ids):
            base = self.cpu(i, "cache")
            for d in sorted(x for x in _listdir(base) if x.startswith("index")):
                p = os.path.join(base, d)
                name = cache_name(_int(_read(os.path.join(p, "level"))), _read(os.path.join(p, "type")))
                if not name:
                    continue
                share = _read(os.path.join(p, "shared_cpu_list"))
                share = share if share is not None else _read(os.path.join(p, "id"))
                if share is None:  # without it a shared cache would be counted once per CPU
                    unknown.add(name)
                    continue
                if (name, share) not in seen:
                    seen.add((name, share))
                    size = parse_size(_read(os.path.join(p, "size")))
                    if size:
                        out[name] = out.get(name, 0) + size
        return {"cache": {k: v for k, v in out.items() if k not in unknown}}

    def freq_limits(self, ids):
        mins, maxs, bases, files, policy = [], [], [], [], None
        for i in self.online(ids):
            d = self.cpu(i, "cpufreq")
            if not os.path.isdir(d):
                continue
            policy = policy or d
            for name in ("scaling_cur_freq", "cpuinfo_cur_freq"):  # the second one is usually root-only
                f = os.path.join(d, name)
                if os.access(f, os.R_OK):
                    files.append((i, f))
                    break
            for lst, name in ((mins, "cpuinfo_min_freq"), (maxs, "cpuinfo_max_freq"), (bases, "base_frequency")):
                v = _int(_read(os.path.join(d, name)))
                if v:
                    lst.append(v)
        self.freq_files, self.policy = files, policy
        out = {"min": _mhz(min(mins)) if mins else None, "max": _mhz(max(maxs)) if maxs else None,
               "base": _mhz(max(bases)) if bases else None}  # hybrid: the P cores' base, the one Intel advertises
        if policy is None:
            out["notes"] = ["clock: no cpufreq in sysfs"]
        return out

    # counters and readings
    def counters(self):
        with open(self.p("proc", "stat")) as f:
            st = parse_proc_stat(f.read())
        if not st["cpus"]:
            raise LookupError("no cpu lines in /proc/stat")
        self.btime = st.pop("btime")
        return st

    def freq_now(self):
        cur = {}
        for i, path in self.freq_files:
            v = _int(_read(path))
            if v:
                cur[i] = _mhz(v)
        gov = _read(os.path.join(self.policy, "scaling_governor")) if self.policy else None
        drv = _read(os.path.join(self.policy, "scaling_driver")) if self.policy else None
        return {"cur": cur, "governor": gov or None, "driver": drv or None}

    def load(self):
        with open(self.p("proc", "loadavg")) as f:
            return [float(x) for x in f.read().split()[:3]]

    def uptime(self):
        try:
            with open(self.p("proc", "uptime")) as f:
                return float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            if self.btime:
                return max(0.0, time.time() - self.btime)
            raise

    def throttle(self):
        """thermal_throttle counters are per logical CPU and differ: a core's is the max of its threads, the package's
        count and time the max across CPUs (the CPU that saw the most events), as render.py does for the time."""
        cores, counts, times = {}, [], []
        for _, core, d in self.throttle_dirs:
            c = _int(_read(os.path.join(d, "core_throttle_count")))
            if c is not None and core is not None:
                cores[core] = max(c, cores.get(core, 0))
            v = _int(_read(os.path.join(d, "package_throttle_count")))
            if v is not None:
                counts.append(v)
            v = _int(_read(os.path.join(d, "package_throttle_total_time_ms")))
            if v is not None:
                times.append(v)
        return {"package": max(counts) if counts else None, "cores": cores,
                "package_s": max(times) / 1000.0 if times else None}

    # temperatures: which files to read is decided once (a plan), each sample reads only the inputs
    def temps(self):
        now = time.monotonic()
        if self.plan is None or (not self.plan["inputs"] and now - self.plan_t > REPLAN_S):
            self.plan, self.plan_t = self.temp_plan(), now
        plan, out = self.plan, empty_temps()
        if not plan["inputs"]:
            out["notes"] = ["temperature: no CPU sensor found"]
            return out
        out["source"] = plan["source"]
        for s in plan["inputs"]:
            raw = _int(_read(s["path"]))
            if raw is None:
                self.plan = None  # a sensor went away (module unloaded, hwmon renumbered): look again next time
                continue
            c = round(raw / 1000.0, 1)
            if not -40 < c < 150:  # a sensor error code, not a temperature
                continue
            out["sensors"].append({"label": s["label"], "c": c, "high": s["high"], "crit": s["crit"]})
            if s["role"] == "package" and (out["package"] is None or c > out["package"]):
                out["package"], out["high"], out["crit"] = c, s["high"], s["crit"]
            elif s["role"] == "core":
                out["cores"][s["core"]] = c
        return out

    def temp_plan(self):
        """CPU drivers (coretemp, k10temp, zenpower...) first, then ARM SoC zones exported as hwmon, then the thermal
        zones (x86_pkg_temp, SoC), last the ACPI zone (a motherboard reading, labelled as such)."""
        hw, found = self.p("sys", "class", "hwmon"), []
        for d in sorted(_listdir(hw), key=_natural):
            path = os.path.join(hw, d)
            name = _read(os.path.join(path, "name"))
            if name is None and _read(os.path.join(path, "device", "name")):  # old kernels: the files are in device/
                path = os.path.join(path, "device")
                name = _read(os.path.join(path, "name"))
            if name:
                found.append((name, path))
        drivers = [n for n, _ in found if n in CPU_DRIVERS]
        if drivers:
            best = min(drivers, key=CPU_DRIVERS.index)
            return self.hwmon_plan([x for x in found if x[0] == best])
        soc = sorted((x for x in found if soc_sensor(x[0])), key=lambda x: _soc_rank(x[0]))
        if soc:
            return self.hwmon_plan(soc)
        zones = self.zone_plan(lambda t: t == "x86_pkg_temp") or self.zone_plan(soc_sensor)
        if zones:
            return zones
        acpi = [x for x in found if x[0] == "acpitz"]
        if acpi:
            return self.hwmon_plan(acpi[:1])
        return self.zone_plan(lambda t: t == "acpitz") or {"source": None, "inputs": []}

    def hwmon_plan(self, items):
        inputs = []
        for name, d in items:
            files = _listdir(d)
            idx = sorted(int(m.group(1)) for m in (re.match(r"temp(\d+)_input$", f) for f in files) if m)
            labels = {i: _read(os.path.join(d, f"temp{i}_label")) for i in idx}
            pkg = next((int(v.split()[-1]) for v in labels.values() if v and re.match(r"Package id \d+$", v)), None)
            for i in idx:
                label = labels[i] or (name if len(idx) == 1 else f"{name} {i}")
                m = re.match(r"Core\s+(\d+)$", label)
                role, core = ("package", None) if label.startswith("Package id") else \
                    ("core", int(m.group(1))) if m and not pkg else ("other", None)
                if m and pkg:  # a second socket: its core ids repeat the first one's, so they stay in the list only
                    label = f"{label} (package {pkg})"
                inputs.append({"label": label, "path": os.path.join(d, f"temp{i}_input"), "role": role, "core": core,
                               "high": self.milli(os.path.join(d, f"temp{i}_max")),
                               "crit": self.milli(os.path.join(d, f"temp{i}_crit"))})
        if inputs and not any(s["role"] == "package" for s in inputs):
            # no 'Package id' label: AMD's Tdie (the real one) or Tctl (control value, may carry an offset), else the first
            pick = next((s for s in inputs if s["label"] == "Tdie"), None) or \
                next((s for s in inputs if s["label"] == "Tctl"), None) or inputs[0]
            pick["role"] = "package"
        return {"source": items[0][0], "inputs": inputs}

    def zone_plan(self, wanted):
        tz, inputs, source = self.p("sys", "class", "thermal"), [], None
        for d in sorted((x for x in _listdir(tz) if x.startswith("thermal_zone")), key=_natural):
            kind = _read(os.path.join(tz, d, "type"))
            if not kind or not wanted(kind):
                continue
            source = source or kind
            trips = {}
            for f in _listdir(os.path.join(tz, d)):
                m = re.match(r"trip_point_(\d+)_type$", f)
                if m:
                    t = self.milli(os.path.join(tz, d, f"trip_point_{m.group(1)}_temp"))
                    if t and t > 0:
                        trips.setdefault(_read(os.path.join(tz, d, f)), t)
            inputs.append({"label": kind, "path": os.path.join(tz, d, "temp"), "core": None,
                           "role": "package" if kind == "x86_pkg_temp" or not inputs else "other",
                           "high": trips.get("hot") or trips.get("passive"), "crit": trips.get("critical")})
        if len(inputs) > 1:
            for n, s in enumerate(inputs):
                s["label"] = f"{s['label']} {n}"
        return {"source": source, "inputs": inputs} if inputs else None

    @staticmethod
    def milli(path):
        v = _int(_read(path))
        return round(v / 1000.0, 1) if v else None


# ---- macOS: parsers (pure) ---------------------------------------------------------------------------------------------------

def sysctl_int(raw):
    return int.from_bytes(raw[:8], "little") if raw else None


def sysctl_str(raw):
    return _clean(raw.split(b"\0", 1)[0].decode("utf-8", "replace")) if raw else None


def sysctl_ints(raw):
    n = len(raw or b"") // 8
    return list(struct.unpack(f"<{n}Q", raw[:n * 8])) if n else []


def parse_timeval(raw):
    """kern.boottime, a struct timeval (64-bit seconds, 32-bit microseconds) -> epoch seconds."""
    if not raw or len(raw) < 12:
        raise ValueError("kern.boottime unreadable")
    sec, usec = struct.unpack_from("<qi", raw)
    return sec + usec / 1e6


def mac_facts(get):
    """The static facts of a Mac from its sysctl values; `get(name)` returns the raw bytes or None (hostinfo.mac_sysctl).

    Apple Silicon: no clock sysctl (None: the collector reads the cluster clocks), P/E counts from hw.perflevelN (ids
    unknown), caches per perflevel: L1 per core, L2 per cluster of cpusperl2 CPUs. Intel: one perflevel or none; caches
    counted with hw.cacheconfig (how many logical CPUs share each level)."""
    def i(name):
        return sysctl_int(get(name))

    model = sysctl_str(get("machdep.cpu.brand_string"))
    logical, physical = i("hw.logicalcpu"), i("hw.physicalcpu")
    levels = i("hw.nperflevels") or 0
    kinds, cache, missing = {}, {}, set()
    for n in range(levels):
        lv = f"hw.perflevel{n}."
        phys = i(lv + "physicalcpu")
        logi = i(lv + "logicalcpu") or phys
        if levels > 1 and logi:
            name = sysctl_str(get(lv + "name"))
            kind = "E" if name == "Efficiency" or (name is None and n == levels - 1) else "P"
            kinds[kind] = kinds.get(kind, 0) + logi
        for key, size, per in (("L1d", "l1dcachesize", None), ("L1i", "l1icachesize", None),
                               ("L2", "l2cachesize", "cpusperl2"), ("L3", "l3cachesize", "cpusperl3")):
            sz, share = i(lv + size), (i(lv + per) if per else None)
            count = phys if per is None else (int(math.ceil(float(logi) / share)) if share and logi else None)
            if sz and count:
                cache[key] = cache.get(key, 0) + sz * count
            else:
                missing.add(key)
    cache = {k: v for k, v in cache.items() if k not in missing}  # a level missing on one perflevel: no partial sum
    shares = sysctl_ints(get("hw.cacheconfig"))  # [memory, L1, L2, L3]: logical CPUs sharing one cache of each level
    for key, size, lvl in (("L1d", "hw.l1dcachesize", 1), ("L1i", "hw.l1icachesize", 1), ("L2", "hw.l2cachesize", 2),
                           ("L3", "hw.l3cachesize", 3)):
        sz = i(size)
        if key not in cache and sz and logical and len(shares) > lvl and shares[lvl]:
            cache[key] = sz * max(1, logical // shares[lvl])
    hz, hz_min, hz_max = i("hw.cpufrequency"), i("hw.cpufrequency_min"), i("hw.cpufrequency_max")
    base = brand_mhz(model) or (int(round(hz / 1e6)) if hz else None)
    return {"model": model, "vendor": sysctl_str(get("machdep.cpu.vendor")) or ("Apple" if model and model.startswith("Apple") else None),
            "sockets": i("hw.packages"), "cores": physical, "threads": logical, "kinds": kinds, "cache": cache,
            "cur": int(round(hz / 1e6)) if hz else None, "base": base,
            # the min and max sysctls repeat the nominal clock on most Intel Macs: only a real range is a range
            "min": int(round(hz_min / 1e6)) if hz_min and hz and hz_min < hz else None,
            "max": int(round(hz_max / 1e6)) if hz_max and hz and hz_max > hz else None}


class MacReader(object):
    def __init__(self, get=None, ticks=None):
        self.get = get or hostinfo.mac_sysctl
        self.ticks = ticks or hostinfo.mac_cpu_ticks
        self.facts, self.boot = None, None

    def _facts(self):
        if self.facts is None:
            self.facts = mac_facts(self.get)
        return self.facts

    def identity(self, ids):
        f = self._facts()
        return {"model": f["model"], "vendor": f["vendor"]}

    def topology(self, ids):
        f = self._facts()
        return {"sockets": f["sockets"], "cores": f["cores"], "threads": f["threads"] or len(ids) or None, "kinds": f["kinds"]}

    def cache(self, ids):
        return {"cache": self._facts()["cache"]}

    def freq_limits(self, ids):
        f = self._facts()
        return {"min": f["min"], "max": f["max"], "base": f["base"]}

    def counters(self):
        cpus = {}
        for n, (user, system, idle, nice) in enumerate(self.ticks()):
            cpus[n] = {"user": user, "nice": nice, "system": system, "idle": idle,
                       "iowait": None, "irq": None, "softirq": None, "steal": None}
        if not cpus:
            raise LookupError("no CPU in host_processor_info")
        return {"cpus": cpus, "total": None, "ctxt": None, "intr": None, "running": None, "blocked": None}

    def freq_now(self):
        cur = self._facts()["cur"]  # Intel only: one value for the whole CPU, under id -1 (contract)
        return {"cur": {-1: cur} if cur else {}, "governor": None, "driver": None}

    def load(self):
        return [round(x, 2) for x in os.getloadavg()]

    def uptime(self):
        if self.boot is None:
            self.boot = parse_timeval(self.get("kern.boottime"))
        return max(0.0, time.time() - self.boot)


# ---- Windows: parsers (pure) -------------------------------------------------------------------------------------------------

SPI_CONTEXT_SWITCHES = 296  # SYSTEM_PERFORMANCE_INFORMATION: 4 LARGE_INTEGERs + 66 ULONGs before it (layout fixed since NT 4)
REL_CORE, REL_CACHE, REL_PACKAGE, REL_GROUP = 0, 2, 3, 4
PDH_PERFORMANCE = "\\Processor Information(*)\\% Processor Performance"


def parse_cpu_perf(raw):
    """SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION array -> [(idle, kernel, user, dpc, interrupt, interrupt count)].

    Times in 100 ns; kernel time includes the idle, DPC and interrupt time."""
    size = ctypes.sizeof(winapi.CpuPerf)
    n = len(raw) // size
    rows = (winapi.CpuPerf * n).from_buffer_copy(raw[:n * size]) if n else []
    return [(r.idle, r.kernel, r.user, r.dpc, r.interrupt, r.interrupts) for r in rows]


def parse_power_info(raw):
    """CallNtPowerInformation(ProcessorInformation) -> [{'number', 'max', 'cur', 'limit'}] in MHz, one per logical CPU.

    'max' is the rated (nominal) clock, not the turbo one; 'cur' is the nominal clock times the current throttle."""
    n = len(raw) // 24
    return [dict(zip(("number", "max", "cur", "limit"), struct.unpack_from("<4I", raw, k * 24))) for k in range(n)]


def parse_context_switches(raw):
    if len(raw) < SPI_CONTEXT_SWITCHES + 4:
        raise ValueError("SystemPerformanceInformation too short")
    return struct.unpack_from("<I", raw, SPI_CONTEXT_SWITCHES)[0]


def parse_slpi_ex(raw, ptr=None):
    """GetLogicalProcessorInformationEx(RelationAll) buffer -> {'cores', 'sockets', 'kinds', 'cache', 'groups', 'offsets'}.

    Variable-size records (winnt.h): Relationship (DWORD), Size (DWORD), then
      core:    Flags (byte 8), EfficiencyClass (byte 9), GroupCount (WORD at 30), GROUP_AFFINITY[] at 32
      cache:   Level (byte 8), CacheSize (DWORD at 12), Type (DWORD at 16: 0 unified, 1 instruction, 2 data, 3 trace);
               one record per cache, so the sizes add up to the total
      package: one record per socket
      group:   ActiveGroupCount (WORD at 10), PROCESSOR_GROUP_INFO[] at 32 (ActiveProcessorCount at +1)
    GROUP_AFFINITY is {KAFFINITY Mask; WORD Group; WORD Reserved[3]}, the mask pointer-sized. Hybrid CPUs: the cores of
    the highest EfficiencyClass are P, the others E (one class only: not hybrid)."""
    ptr = ptr or ctypes.sizeof(ctypes.c_void_p)
    aff, ginfo = ptr + 8, 40 + ptr
    pos, cores, sockets, groups, cache = 0, [], 0, [], {}
    while pos + 8 <= len(raw):
        rel, size = struct.unpack_from("<II", raw, pos)
        if size < 8 or pos + size > len(raw):
            break
        if rel == REL_CORE:
            n = struct.unpack_from("<H", raw, pos + 30)[0] or 1
            masks = []
            for k in range(n):
                o = pos + 32 + k * aff
                masks.append((struct.unpack_from("<H", raw, o + ptr)[0], int.from_bytes(raw[o:o + ptr], "little")))
            cores.append((raw[pos + 9], masks))
        elif rel == REL_PACKAGE:
            sockets += 1
        elif rel == REL_CACHE:
            level, csize, ctype = raw[pos + 8], struct.unpack_from("<I", raw, pos + 12)[0], struct.unpack_from("<I", raw, pos + 16)[0]
            name = cache_name(level, {0: "Unified", 1: "Instruction", 2: "Data"}.get(ctype))
            if name and not (level == 1 and ctype == 0):
                cache[name] = cache.get(name, 0) + csize
        elif rel == REL_GROUP:
            active = struct.unpack_from("<H", raw, pos + 10)[0]
            groups = [raw[pos + 32 + g * ginfo + 1] for g in range(active)]
        pos += size
    offsets, acc = {}, 0
    for g, n in enumerate(groups):
        offsets[g], acc = acc, acc + n

    def ids(masks):
        return [offsets.get(g, 64 * g) + b for g, m in masks for b in range(ptr * 8) if m >> b & 1]

    classes, kinds = {e for e, _ in cores}, {}
    if len(classes) > 1:
        top = max(classes)
        kinds = {"P": sorted(i for e, m in cores if e == top for i in ids(m)),
                 "E": sorted(i for e, m in cores if e != top for i in ids(m))}
    return {"cores": len(cores), "sockets": sockets, "kinds": kinds, "cache": cache, "groups": groups, "offsets": offsets}


def pdh_cpu_values(values, offsets):
    """'Processor Information' instances {'0,3': v, '0,_Total': v, '_Total': v} -> {logical id: v} ('group,number')."""
    out = {}
    for name, v in values.items():
        g, sep, n = name.partition(",")
        if sep and g.isdigit() and n.isdigit():
            out[offsets.get(int(g), 64 * int(g)) + int(n)] = v
    return out


class WindowsReader(object):
    def __init__(self, api=None):
        self.api = api or winapi
        self.layout = None
        self.unwrapped = {}
        self.max_mhz = {}
        self.pdh, self.pdh_error = None, None

    def _layout(self):
        if self.layout is None:  # read once; a failure is remembered too (no system call per refresh to fail again)
            try:
                self.layout = parse_slpi_ex(self.api.logical_processor_info())
            except Exception as e:  # noqa: BLE001
                self.layout = e
        if isinstance(self.layout, Exception):
            raise self.layout
        return self.layout

    def _ncpu(self):
        """Logical CPUs of all the processor groups (os.cpu_count() sees one group on older Pythons)."""
        try:
            return sum(self._layout()["groups"]) or os.cpu_count() or 1
        except Exception:  # noqa: BLE001
            return os.cpu_count() or 1

    def _offsets(self):
        try:
            return self._layout()["offsets"]
        except Exception:  # noqa: BLE001
            return {}

    def identity(self, ids):
        reg = self.api.processor_registry()
        model, mhz = _clean(reg.get("name")), reg.get("mhz")  # the registry pads the name with spaces
        return {"model": model, "vendor": _clean(reg.get("vendor")),
                "brand_base": brand_mhz(model) or (int(mhz) if mhz else None)}

    def topology(self, ids):
        lay = self._layout()
        return {"sockets": lay["sockets"] or None, "cores": lay["cores"] or None, "threads": self._ncpu() or len(ids) or None,
                "kinds": lay["kinds"]}

    def cache(self, ids):
        return {"cache": self._layout()["cache"]}

    def freq_limits(self, ids):
        """Windows knows the rated clock of each CPU (MaxMhz: the base, not the turbo) but no turbo maximum: max is None."""
        power = parse_power_info(self.api.power_info(self._ncpu()))
        self.max_mhz = {k: p["max"] for k, p in enumerate(power) if p["max"]}
        if self.pdh is None and self.pdh_error is None:  # created now so that the first sample has an interval to measure
            try:
                self.pdh = self.api.PdhCounter(PDH_PERFORMANCE)
            except (OSError, AttributeError, ValueError) as e:
                self.pdh_error = e
        return {"min": None, "max": None, "base": max(self.max_mhz.values()) if self.max_mhz else None}

    def _unwrap(self, key, v):
        """ULONG counters wrap around at 2**32 (context switches: about a day at 50,000/s): a 64-bit running count."""
        last = self.unwrapped.get(key)
        total = v if last is None else last[1] + ((v - last[0]) % (1 << 32))
        self.unwrapped[key] = (v, total)
        return total

    def counters(self):
        parts, complete = self.api.cpu_perf()
        cpus, intr, notes = {}, 0, []
        for first, raw in parts:
            for k, (idle, kernel, user, dpc, interrupt, count) in enumerate(parse_cpu_perf(raw)):
                cpus[first + k] = {"user": user, "nice": 0, "system": kernel - idle - dpc - interrupt, "idle": idle,
                                   "iowait": None, "irq": interrupt, "softirq": dpc, "steal": None}
                intr += self._unwrap(("intr", first + k), count)
        if not cpus:
            raise LookupError("no CPU in NtQuerySystemInformation")
        if not complete:
            notes.append(f"usage: processor group 0 only ({len(cpus)} of {self._ncpu()} CPUs)")
        try:
            ctxt = self._unwrap("ctxt", parse_context_switches(self.api.system_performance_info()))
        except (OSError, ValueError, AttributeError) as e:
            ctxt = None
            notes.append(note("context switches", e))
        return {"cpus": cpus, "total": None, "ctxt": ctxt, "intr": intr, "running": None, "blocked": None, "notes": notes}

    def freq_now(self):
        """The effective clock: rated clock x '% Processor Performance' (above 100 in turbo), as Task Manager shows it.
        Without PDH, CallNtPowerInformation's CurrentMhz, which never goes above the rated clock."""
        cur, notes = {}, []
        if self.pdh is not None:
            try:
                perf = pdh_cpu_values(self.pdh.values(), self._offsets())
                cur = {k: int(round(self.max_mhz[k] * v / 100.0)) for k, v in perf.items() if k in self.max_mhz and v > 0}
            except (OSError, ValueError, AttributeError, LookupError):
                cur = {}
        if not cur:
            power = parse_power_info(self.api.power_info(self._ncpu()))
            cur = {k: p["cur"] for k, p in enumerate(power) if p["cur"]}
            if self.pdh_error is not None:
                notes.append("clock: rated value only (PDH unavailable)")
        return {"cur": cur, "governor": None, "driver": None, "notes": notes}

    def load(self):
        return None  # Windows has no load average

    def uptime(self):
        return self.api.uptime()


# ---- any other OS --------------------------------------------------------------------------------------------------------------

class OtherReader(object):
    def identity(self, ids):
        model = platform.processor() or None
        return {"model": None if model == platform.machine() else model}  # on some systems it is only the architecture

    def counters(self):
        raise NotImplementedError("not available on this OS")

    def load(self):
        return [round(x, 2) for x in os.getloadavg()]


def make_reader(root=None, system=None):
    system = system or ("linux" if root else sys.platform)
    if system.startswith("linux"):
        return LinuxReader(root or "/")
    if system == "darwin":
        return MacReader()
    if system == "win32":
        return WindowsReader()
    return OtherReader()


# ---- the sampler ---------------------------------------------------------------------------------------------------------------

class CpuSampler(object):
    """CpuSampler().sample() -> the contract dict; percentages and rates are deltas since the previous call.

    root: Linux tree to read (tests); system: 'linux' | 'darwin' | 'win32' | other (default: this OS);
    reader: a reader object (tests); clock: monotonic seconds (tests)."""

    STATIC = (("model", "identity"), ("topology", "topology"), ("cache", "cache"), ("clock", "freq_limits"))
    DYNAMIC = (("clock", "freq_now"), ("load", "load"), ("uptime", "uptime"), ("temperature", "temps"), ("throttle", "throttle"))

    def __init__(self, root=None, system=None, reader=None, clock=None):
        self.reader = reader or make_reader(root, system)
        self.clock = clock or time.monotonic
        self.arch = platform.machine() or None
        self.static, self.static_notes, self.static_ids = {}, [], None
        self.last = {"total": None, "cores": {}, "ctxt": None, "intr": None}
        self.prev = self._counters([])
        self.prev_t = self.clock()
        self._load_static(sorted(self.prev["cpus"]) if self.prev else [])

    def _counters(self, notes):
        try:
            cur = self.reader.counters()
        except Exception as e:  # noqa: BLE001 - fail soft: one part, one note
            notes.append(note("usage", e))
            return None
        notes.extend(cur.pop("notes", []))
        return cur

    def _load_static(self, ids):
        st = {"model": None, "vendor": None, "sockets": None, "cores": None, "threads": len(ids) or os.cpu_count() or 1,
              "kinds": {}, "cache": {}, "min": None, "max": None, "base": None, "brand_base": None}
        notes = []
        for part, name in self.STATIC:
            fn = getattr(self.reader, name, None)
            if fn is None:
                continue
            try:
                got = dict(fn(ids) or {})
            except Exception as e:  # noqa: BLE001
                notes.append(note(part, e))
                continue
            notes.extend(got.pop("notes", []))
            st.update({k: v for k, v in got.items() if v is not None})
        if st["base"] is None:
            st["base"] = st["brand_base"]
        self.static, self.static_notes, self.static_ids = st, notes, list(ids)

    def _delta(self, cur, now):
        rates = {"ctxt": None, "intr": None, "running": None, "blocked": None}
        if cur is None:
            return {"total": None, "cores": []}, rates
        rates.update(running=cur.get("running"), blocked=cur.get("blocked"))
        ids = sorted(cur["cpus"])
        if self.prev is None or now - self.prev_t < MIN_INTERVAL:
            # nothing to measure yet, or too close to the previous call: the previous figures, the baseline stays put
            if self.prev is None:
                self.prev, self.prev_t = cur, now
            rates.update(ctxt=self.last["ctxt"], intr=self.last["intr"])
            return {"total": self.last["total"], "cores": [self.last["cores"][i] for i in ids if i in self.last["cores"]]}, rates
        prev, dt = self.prev, now - self.prev_t
        tot = usage_of(prev["total"] or sum_fields(prev["cpus"]), cur["total"] or sum_fields(cur["cpus"]))
        if tot is not None:
            self.last["total"] = tot
        cores = {}
        for i in ids:
            u = usage_of(prev["cpus"].get(i), cur["cpus"][i])
            if u is not None:
                cores[i] = {"id": i, "busy": u["busy"], "user": u["user"], "system": u["system"], "iowait": u["iowait"]}
            elif i in self.last["cores"]:  # a counter reset on this CPU: its previous figures for one more refresh
                cores[i] = self.last["cores"][i]
        self.last["cores"] = cores
        for k in ("ctxt", "intr"):
            self.last[k] = rate(prev.get(k), cur.get(k), dt)
        rates.update(ctxt=self.last["ctxt"], intr=self.last["intr"])
        self.prev, self.prev_t = cur, now
        return {"total": self.last["total"], "cores": [cores[i] for i in ids if i in cores]}, rates

    def sample(self):
        notes = []
        now = self.clock()
        cur = self._counters(notes)
        if cur is not None and sorted(cur["cpus"]) != self.static_ids:  # a CPU went offline or online: topology again
            self._load_static(sorted(cur["cpus"]))
        st = self.static
        out = empty_sample()
        out.update({k: st[k] for k in ("model", "vendor", "sockets", "cores", "threads", "kinds", "cache")})
        out["arch"] = self.arch
        out["freq"].update({k: st[k] for k in ("min", "max", "base")})
        out["usage"], out["rates"] = self._delta(cur, now)
        for part, name in self.DYNAMIC:
            fn = getattr(self.reader, name, None)
            if fn is None:
                continue
            try:
                val = fn()
            except Exception as e:  # noqa: BLE001
                notes.append(note(part, e))
                continue
            if isinstance(val, dict):
                notes.extend(val.pop("notes", []))
            if name == "freq_now":
                out["freq"].update(val)
            elif name == "temps":
                out["temps"].update(val or {})
            elif name == "throttle":
                out["throttle"].update(val or {})
            elif name == "uptime":
                out["uptime"] = round(val, 1) if val is not None else None
            else:
                out[name] = val
        for n in self.static_notes + notes:  # each reason once (a note can come from both the static and the dynamic read)
            if n not in out["notes"]:
                out["notes"].append(n)
        return out
