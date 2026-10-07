"""nuc-console inputs: what the console reads from the machine and from the collector's files, with no drawing in it.

The paths of the collector's state (`STATE`, `NET_STATE`, `BOOT_STATE`, `BASELINE`), the readers of /proc, /sys and the OS tools
(thermal, sessions, disks, memory, load), `Sampler` (the figures the System page and the CPU/thermal cards draw, with the history of the
traffic), `cached` (a slow reader run in a background thread) and the loaders of the JSON files. macOS and Windows read their figures
from hostinfo.py. Stdlib only."""
import collections
import glob
import json
import os
import subprocess
import threading
import time

import nuc_config

LINUX = nuc_config.LINUX
if not LINUX:
    import hostinfo


def on(feature):
    """Section enabled in config.ini (default: yes): the answer of render.on(), from the same dict."""
    return nuc_config.current()["features"].get(feature, True)


STATE = os.environ.get("NUC_CONSOLE_STATE", os.path.join(nuc_config.RUN_DIR, "containers.json"))


NET_STATE = os.environ.get("NUC_CONSOLE_NET", os.path.join(nuc_config.RUN_DIR, "net.json"))


BOOT_STATE = os.environ.get("NUC_CONSOLE_BOOT", os.path.join(nuc_config.RUN_DIR, "boot.json"))


BASELINE = os.environ.get("NUC_CONSOLE_BASELINE", os.path.join(nuc_config.LIB_DIR, "baseline.json"))


THROTTLE_WINDOW_S = 60


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def hwmon_dir(name):
    return next((h for h in glob.glob("/sys/class/hwmon/hwmon*") if read_file(h + "/name") == name), None)


def read_thermal():
    """{'cpu': (C, max), 'nvme': (C, max), 'throttle': events_since_boot|None, 'clk': (cur_GHz, max_GHz)|None}.

    The maximum is the sensor's own (temp1_max: CPU package, NVMe composite), not a constant of ours.
    """
    out = {}
    for key, name in (("cpu", "coretemp"), ("nvme", "nvme")):
        h = hwmon_dir(name)
        t, mx = (read_file(f"{h}/temp1_input"), read_file(f"{h}/temp1_max")) if h else (None, None)
        if t and mx:
            out[key] = (int(t) / 1000, int(mx) / 1000)
    # the counters are per logical CPU and differ a lot: the sum only serves to see that it *changes*;
    # the readable figure is the package's total throttling time (the maximum across CPUs)
    base = "/sys/devices/system/cpu/cpu*/thermal_throttle/package_throttle_"
    counts = [x for x in (read_file(f) for f in glob.glob(base + "count")) if x and x.isdigit()]
    times = [x for x in (read_file(f) for f in glob.glob(base + "total_time_ms")) if x and x.isdigit()]
    out["throttle"] = sum(int(x) for x in counts) if counts else None
    out["throttle_s"] = max(int(x) for x in times) / 1000 if times else None
    cur = [read_file(f) for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")]
    mx = read_file("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    cur = [int(x) for x in cur if x and x.isdigit()]
    out["clk"] = (sum(cur) / len(cur) / 1e6, int(mx) / 1e6) if cur and mx and mx.isdigit() else None
    return out


def parse_netdev(text):
    """/proc/net/dev -> {interface: (rx_bytes, tx_bytes)} without loopback and container virtual interfaces."""
    out = {}
    for ln in text.splitlines()[2:]:
        name, _, rest = ln.partition(":")
        name, f = name.strip(), rest.split()
        if name and len(f) >= 9 and not name.startswith(NET_SKIP):
            out[name] = (int(f[0]), int(f[8]))
    return out


def parse_sessions(loginctl_text, ss_text):
    """Local sessions (loginctl) and ssh clients connected now (peers of established connections on port 22)."""
    local = []
    for ln in loginctl_text.splitlines():
        p = ln.split()
        # SESSION UID USER SEAT LEADER CLASS TTY: class 'manager' is the user's systemd instance, not a session
        if len(p) >= 6 and p[5] in ("user", "greeter"):
            local.append({"user": p[2], "tty": p[6] if len(p) > 6 and p[6] != "-" else ""})
    ssh = []
    for ln in ss_text.splitlines():
        f = ln.split()
        if len(f) >= 4:
            ssh.append(f[3].rpartition(":")[0].strip("[]"))
    return {"local": local, "ssh": sorted(set(ssh))}


_CACHE = {}


_THREADS = {}


_SLEEP = time.sleep  # the background threads' own: a test that swaps render.time for a fake clock must not have them move it


def cached(key, ttl, fn):
    """Last value of fn(), recomputed in a background thread every ttl seconds (None until the first one exists).

    Sessions and disks use subprocess and statvfs, which can hang (timeout, stuck network mount): in the drawing loop
    they would freeze the clock and the screen. If fn() raises, the value becomes None: the block says "unavailable"."""
    if key not in _THREADS:
        def loop():
            while True:
                try:
                    _CACHE[key] = fn()
                except Exception:  # noqa: BLE001 - data unavailable: the block will say so, the thread does not die
                    _CACHE[key] = None
                _SLEEP(ttl)
        _THREADS[key] = threading.Thread(target=loop, daemon=True, name=f"cache-{key}")
        _THREADS[key].start()
    return _CACHE.get(key)


def read_sessions():
    if not LINUX:
        return hostinfo.sessions()

    def run(*a):
        r = subprocess.run(list(a), capture_output=True, text=True, timeout=3)
        if r.returncode != 0:  # failed != "none": the block must say "unavailable", not reassure
            raise RuntimeError(f"{a[0]} rc={r.returncode}")
        return r.stdout
    return parse_sessions(run("loginctl", "list-sessions", "--no-legend"),
                          run("ss", "-tnH", "state", "established", "( sport = :22 )"))


def parse_mounts(text):
    """/proc/mounts -> [(mountpoint, fstype)] of real filesystems only, once per device."""
    seen, out = set(), []
    for ln in text.splitlines():
        f = ln.split()
        if len(f) >= 3 and f[2] in REAL_FS and f[0] not in seen and not f[1].startswith("/var/lib/docker"):
            seen.add(f[0])
            out.append((f[1].replace("\\040", " "), f[2]))
    return out


def read_filesystems():
    if not LINUX:
        return hostinfo.filesystems()
    with open("/proc/mounts") as f:
        mounts = parse_mounts(f.read())
    out = []
    for mp, _ in mounts:
        try:
            st = os.statvfs(mp)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total:
            out.append({"mount": mp, "used": (st.f_blocks - st.f_bfree) * st.f_frsize, "total": total})
    return out


class Sampler:
    """Samples /proc/stat and the thermal sensors; CPU percentages are deltas between two consecutive calls.

    sample() is the one place the dashboard reads the host from. Besides the per-core CPU, the thermal sensors, the interfaces, the
    sessions and the filesystems it holds the figures of the SYSTEM block and of the System page, so that --demo can replace all of
    them (demo.sampler_data) and no block calls /proc, statvfs or hostinfo while it draws:
      "mem"        {"MemTotal", "MemAvailable", "Cached", "SwapTotal", "SwapFree"} in bytes, or None (not readable: drawn as ?)
      "disk_root"  (used, total, label) of the system volume, or None
      "uptime"     seconds since boot, or None
      "load"       ['0.12', '0.30', '0.25']; [] where the OS has no load average (Windows); None when it could not be read"""

    def __init__(self):
        self.cpu = self._cpu()
        self.hist = collections.deque(maxlen=64)  # (instant, throttling counter)
        self.net_prev = (time.monotonic(), self._netdev())
        self.net_hist = {}  # interface -> (rx deque, tx deque) of bytes/s

    @staticmethod
    def _netdev():
        try:
            if not LINUX:
                return hostinfo.net_counters()
            with open("/proc/net/dev") as f:
                return parse_netdev(f.read())
        except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
            return {}

    @staticmethod
    def _cpu():
        if not LINUX:
            try:
                return hostinfo.cpu_times()
            except (OSError, ValueError, AttributeError):  # no per-core figures: the CPU bars are left out, nothing invented
                return {}
        out = {}
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu") and line[3].isdigit():
                    p = line.split()
                    v = list(map(int, p[1:9]))
                    out[p[0]] = (sum(v) - v[3] - v[4], sum(v))  # busy = everything except idle+iowait
        return out

    def sample(self):
        cpu = self._cpu()
        per = {k: (cpu[k][0] - self.cpu[k][0]) / max(cpu[k][1] - self.cpu[k][1], 1)
               for k in cpu if k in self.cpu}
        self.cpu = cpu
        now_m, cur = time.monotonic(), self._netdev()
        dt = max(now_m - self.net_prev[0], 0.001)
        net = {}
        for name, (rx, tx) in cur.items():
            prx, ptx = self.net_prev[1].get(name, (rx, tx))
            hrx, htx = self.net_hist.setdefault(name, (collections.deque(maxlen=NET_HIST), collections.deque(maxlen=NET_HIST)))
            hrx.append(max(0, rx - prx) / dt)
            htx.append(max(0, tx - ptx) / dt)
            net[name] = {"rx": hrx[-1], "tx": htx[-1], "rx_tot": rx, "tx_tot": tx, "hist_rx": list(hrx), "hist_tx": list(htx)}
        self.net_prev = (now_m, cur)
        for gone in [n for n in self.net_hist if n not in cur]:
            del self.net_hist[gone]  # interface gone: if it comes back it starts from zero, not from an old history
        th = read_thermal()
        now = time.monotonic()
        if th["throttle"] is not None:
            self.hist.append((now, th["throttle"]))
            old = next(((t, n) for t, n in self.hist if now - t <= THROTTLE_WINDOW_S), None)
            # events in the last minute; None while the history is too short to tell
            rec = th["throttle"] - old[1] if old and now - old[0] >= 10 else None
            th["recent"] = rec if rec is None or rec >= 0 else None  # falling sum = a CPU went offline: invalid figure
        return {"cpu": per, "thermal": th if on("thermal") else {}, "net": net if on("network_traffic") else {},
                "sessions": cached("sessions", 10, read_sessions) if on("sessions") else None,
                "fs": cached("fs", 30, read_filesystems) if on("disks") else None,
                "mem": read_host(meminfo), "disk_root": read_host(root_disk), "uptime": read_host(uptime_s), "load": read_load()}


def meminfo():
    if not LINUX:
        return hostinfo.meminfo()
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            d[k] = int(v.split()[0]) * 1024
    return d


def loadavg():
    """['0.12', '0.30', '0.25'], or None where the OS has no load average (Windows)."""
    if not LINUX:
        return hostinfo.loadavg()
    with open("/proc/loadavg") as f:
        return f.read().split()[:3]


def uptime_s():
    if not LINUX:
        return hostinfo.uptime()
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def root_disk():
    """(used, total, label) of the system volume."""
    if not LINUX:
        return hostinfo.root_disk()
    st = os.statvfs("/")
    return (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_blocks * st.f_frsize, "/"


def read_host(fn):
    """fn() for a Sampler figure: its value, or None when the host cannot give it (the screen draws `?`, whatever /proc, sysctl or the
    Windows API raised: one unreadable figure must not take the SYSTEM block, or the frame, down)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - the answer is "unknown", never a traceback in the middle of the dashboard
        return None


def read_load():
    """The Sampler's "load" (see loadavg()): [] where the OS has no load average (Windows), None when it has one but it could not be read."""
    try:
        load = loadavg()
    except Exception:  # noqa: BLE001 - same rule as read_host()
        return None
    return [] if load is None else load if len(load) == 3 else None


def swap_figures(m):
    """(used, total) bytes of the swap from a Sampler's "mem": None when there is no swap (or no figures)."""
    try:
        total, free = m["SwapTotal"], m["SwapFree"]
        return (total - free, total) if total else None
    except (KeyError, TypeError):
        return None


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_containers(path=None):
    d = load_json(path or STATE)
    return d if isinstance(d, dict) and isinstance(d.get("containers"), list) else None


NET_SKIP = ("lo", "veth", "br-")  # container virtual interfaces: noise


NET_HIST = 30  # history samples for the traffic sparklines


REAL_FS = ("ext4", "ext3", "xfs", "btrfs", "vfat", "f2fs", "zfs", "ntfs3", "exfat", "nfs", "nfs4", "cifs")
