#!/usr/bin/env python3
"""nuc-console renderer: full-screen ANSI dashboard on tty1. Stdlib only, no privileges.

On macOS and Windows there is no text console to take over: `--kiosk` writes the same screen as an HTML file and shows it
in a full-screen browser (see kiosk()); host metrics come from hostinfo.py instead of /proc.
"""
import collections
import glob
import ipaddress
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import cards  # same directory: the card registry and the KPI model
import ansi  # same directory: the overlay of the help
import cpuinfo  # same directory: the CPU screen's producers
import graph  # same directory: the MAP model
import nuc_config
import procs
import ui
# the console primitives (ansi.py) and the text helpers (ui.py) moved out of this file; render.py draws with them, and tests,
# tools and the other modules (notify.py, htmlview.py) still reach them as render.X, so the names stay here until the cleanup PR
from ansi import ANSI, SPARK, bar, c, cc, cell, clip, columns, fit_join, kv, msg, msg_wrap, pad, section, sparkline, vlen  # noqa: F401
from ui import (CTRL, fmt_ago, fmt_cputime, fmt_dur, fmt_k, fmt_min, fmt_rate, fmt_size, hclean, hcount, hnum, human, num,  # noqa: F401
                plural, qf, safe)
# the exposure model (exposure.py) moved out of this file; render.py draws it and works out the problems from it. Moved; kept for tests
# and tools until the cleanup PR
from exposure import (CELL, DOCKER_PROXIES, EXPOSE_LABEL, EXPOSED_RANK, EXPOSURE_SECTIONS, GROUPS, INFRA_PROCS, PRIVATE_NETS,  # noqa: F401
                      REACH_ORDER, SENSITIVE, SHARED_UDP, TS4, TS6, baseline_diff, bind_scope, docker_verdict, expose_apply, expose_cts,
                      expose_note, expose_over, expose_over_items, expose_policy, expose_unmatched, exposure_keys, exposure_partial,
                      exposure_rows, fw_verdict, group_of, is_private_addr, name_change, new_ports, os_of, rule_match, webapp_rows)

try:  # POSIX terminals only: on Windows the keys come from msvcrt
    import termios
    import tty
except ImportError:
    termios = tty = None
LINUX, WINDOWS, MACOS = nuc_config.LINUX, nuc_config.WINDOWS, nuc_config.MACOS
if not LINUX:
    import hostinfo

STATE = os.environ.get("NUC_CONSOLE_STATE", os.path.join(nuc_config.RUN_DIR, "containers.json"))
NET_STATE = os.environ.get("NUC_CONSOLE_NET", os.path.join(nuc_config.RUN_DIR, "net.json"))
BOOT_STATE = os.environ.get("NUC_CONSOLE_BOOT", os.path.join(nuc_config.RUN_DIR, "boot.json"))
BASELINE = os.environ.get("NUC_CONSOLE_BASELINE", os.path.join(nuc_config.LIB_DIR, "baseline.json"))
CFG = nuc_config.current()  # the process's one configuration dict (tests and --demo change it in place)
# the commands the advice on screen refers to, in the words of this OS
if WINDOWS:
    ACCEPT_CMD = "nuc-console-accept"  # from an administrator prompt
    CMD = {"restart": "Start-ScheduledTask -TaskPath \\nuc-console\\ -TaskName collector (administrator PowerShell)",
           "logs": r"%ProgramData%\nuc-console\logs\collector.log"}
elif MACOS:
    ACCEPT_CMD = "sudo nuc-console-accept"
    CMD = {"restart": "sudo launchctl kickstart -k system/com.nuc-console.collector", "logs": "/var/log/nuc-console/collector.log"}
else:
    ACCEPT_CMD = "sudo nuc-console-accept"
    CMD = {"restart": "sudo systemctl restart nuc-console-collector", "logs": "journalctl -u nuc-console-collector"}
PROBLEMS_CMD = "nuc-console-problems"
if nuc_config.PORTABLE:  # run.sh / run.cmd: no nuc-console-accept on the PATH, no service to restart: the advice says what exists
    ACCEPT_CMD = "run.cmd -Accept" if WINDOWS else "./run.sh --accept"
    PROBLEMS_CMD = "run.cmd -Problems" if WINDOWS else "./run.sh --problems"
    CMD = {"restart": "quit it (Ctrl+C) and start it again", "logs": os.path.join(nuc_config.BASE_DIR, "logs", "collector.log")}
MODE = os.environ.get("NUC_CONSOLE_MODE") or CFG["mode"]  # overview = a single screen, no rotation


def on(feature):
    """Section enabled in config.ini (default: yes)."""
    return CFG["features"].get(feature, True)

ROTATE_S, REFRESH_S, HOLD_S, STALE_S = CFG["rotate_seconds"], CFG["refresh_seconds"], 60, 60  # REFRESH_S: 1-10 s, config.ini
WIDE = 200  # from this width up: containers in 2 columns, exposure and firewall side by side
PAGES = tuple(n for n, ok in (("System", True), ("Network & firewall", on("exposure") or on("firewall")),
                              ("Boot", on("boot"))) if ok)
FULL = False   # True while the detail pages / full view are built: no section hides items
TRUNC = set()  # sections that hid items in the last overview ("… +N more"): the detail pages show them in full
EXPAND = set()  # sections whose caps are lifted because the free space allows it (see page_overview)


def lim(seq, n, section):
    """seq[:n] unless FULL; remembers that `section` hides items so the detail pages can show everything."""
    if FULL or section in EXPAND or len(seq) <= n:
        return seq
    TRUNC.add(section)
    return seq[:n]


def wrap_items(items, w, indent=6, sep="  ·  ", max_lines=None, section=None):
    """Compact list over several lines without splitting items; beyond max_lines the last line ends with '… +N'."""
    if FULL or (section and section in EXPAND):
        max_lines = None
    rows, cur = [], []
    for it in items:
        if cur and indent + vlen(sep.join(cur)) + len(sep) + vlen(it) > w:
            rows.append(cur)
            cur = []
        cur.append(it)
    rows.append(cur)
    rows = [r for r in rows if r]
    if not max_lines or len(rows) <= max_lines:
        return [" " * indent + sep.join(r) for r in rows]
    if section:
        TRUNC.add(section)
    rows, hidden = rows[:max_lines], sum(len(r) for r in rows[max_lines:])
    while len(rows[-1]) > 1 and indent + vlen(sep.join(rows[-1])) + 8 > w:  # 8 = "  … +NNN"
        rows[-1].pop()
        hidden += 1
    return [" " * indent + sep.join(r) for r in rows[:-1]] + [" " * indent + sep.join(rows[-1]) + c(90, f"  … +{hidden}")]


THROTTLE_WINDOW_S = 60
THERMAL_WARN, THERMAL_ERR = ui.THERMAL_WARN, ui.THERMAL_ERR  # fractions of the maximum declared by the sensor (sysfs temp*_max)


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


def fmt_up(sec):
    """'5d 0h', or '?' when the uptime is not known."""
    return "?" if sec is None else fmt_dur(sec)


def fmt_load(load):
    """The three load averages, '?' when they could not be read; '' (nothing to say) where the OS has none ([])."""
    return "" if load == [] else "?" if load is None else " ".join(load)


def up_load_note(up, load):
    """'up 5d 0h · load 0.82 0.64 0.51': ? for what could not be read (None), no load at all where the OS has none ([])."""
    return f"up {fmt_up(up)}" + (f" · load {fmt_load(load)}" if load != [] else "")


def ram_figures(m):
    """(used, total) bytes of the RAM from a Sampler's "mem": None when it does not say (an old kernel without MemAvailable, no data)."""
    try:
        total, avail = m["MemTotal"], m["MemAvailable"]
        return (total - avail, total) if total else None
    except (KeyError, TypeError):
        return None


def swap_figures(m):
    """(used, total) bytes of the swap from a Sampler's "mem": None when there is no swap (or no figures)."""
    try:
        total, free = m["SwapTotal"], m["SwapFree"]
        return (total - free, total) if total else None
    except (KeyError, TypeError):
        return None


def disk_figures(d):
    """(used, total, label) of a Sampler's "disk_root": None when it is missing or empty."""
    try:
        used, total, label = d
        return (used, total, label) if total else None
    except (TypeError, ValueError):
        return None


is_absent, is_disabled = cards.is_absent, cards.is_disabled  # the collectors' notes about a section (cards.py reads them too)


def unavail_msg(d, key, prefix="unavailable"):
    """Line for a section without data: 'not installed' (info) if the tool is missing, 'unavailable: error' if it is broken."""
    m = cards.unavail(d, key, prefix)
    return msg(m.level, m.text)


def thermal_lines(th, bw, maxw=None):
    """Temperatures with bar and thresholds (RAM style) + throttling time. Scale and thresholds come from the sensor."""
    lines = []
    for label, key in (("TEMP", "cpu"), ("NVMe", "nvme")):
        if key in th:
            t, mx = th[key]
            clk = f"   clock {th['clk'][0]:.1f}/{th['clk'][1]:.1f} GHz" if key == "cpu" and th.get("clk") else ""
            limits = f"   limits {THERMAL_WARN * mx:.0f}/{THERMAL_ERR * mx:.0f}°C"
            head = f" {label:<5} {bar(t / mx, bw, THERMAL_WARN, THERMAL_ERR)} {t:.0f}°C/{mx:.0f}°C"
            # when narrow, drop the clock first, then the limits (the line must not exceed the width)
            line = next((x for x in (head + limits + clk, head + limits, head) if maxw is None or vlen(x) <= maxw), head)
            lines.append(line)
    if th.get("throttle_s") is not None:
        rec = th.get("recent")
        state = (c(31, f"✖ THROTTLING now (+{rec} events/min)") if rec else
                 c(32, "✔ none in the last minute") if rec == 0 else c(90, "measuring"))
        lines.append(f" {c(90, 'throt')} {fmt_min(th['throttle_s'])} total since boot   {state}")
    return lines


def page_sistema(s, w, cont=None):
    m, load, up, disk = s.get("mem"), s.get("load"), s.get("uptime"), disk_figures(s.get("disk_root"))  # the Sampler's: nothing is read here
    lines = [f" up {fmt_up(up)}" + (f"   load {fmt_load(load)}" if load != [] else ""), ""]
    bw = max(10, min(60, w - 40))
    ram, swap = ram_figures(m), swap_figures(m)
    lines.append(f" RAM   {bar(ram[0] / ram[1], bw)} {human(ram[0])}/{human(ram[1])}  cache {human(m.get('Cached'))}" if ram
                 else f" RAM   {c(33, '?')}")
    if swap:
        lines.append(f" SWAP  {bar(swap[0] / swap[1], bw)} {human(swap[0])}/{human(swap[1])}")
    lines.append(f" DISK  {bar(disk[0] / disk[1], bw)} {human(disk[0])}/{human(disk[1])}  {safe(disk[2])}" if disk
                 else f" DISK  {c(33, '?')}")
    if on("thermal"):
        lines += thermal_lines(s.get("thermal") or {}, bw)
    lines.append("")
    cores = sorted(s["cpu"].items(), key=lambda kv: int(kv[0][3:]))
    cw = 26
    ncol = max(1, (w - 1) // cw)
    cells = [f" {k[3:]:>2} {bar(v, 12)} {v * 100:3.0f}%" for k, v in cores]
    for i in range(0, len(cells), ncol):
        lines.append("".join(pad(x, cw) for x in cells[i:i + ncol]))
    lines.append("")
    return lines + (containers_block(cont if cont is not None else load_containers(), w) if on("containers") else [])


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_containers(path=None):
    d = load_json(path or STATE)
    return d if isinstance(d, dict) and isinstance(d.get("containers"), list) else None


def fmt_ports(ports):
    # '*' = all interfaces, 'lo:' = loopback, 'IP:' = bound to a specific address
    return " ".join(f"{'*' if p['s'] == '*' else p['s'] + ':' if p['s'] != 'lo' else 'lo:'}{p['p']}"
                    for p in ports)


def container_box(proj, cts, w):
    inner = w - 2
    title = f"─ {proj[:60]} ({len(cts)}) "
    lines = ["┌" + title + "─" * max(0, inner - len(title)) + "┐"]
    for ct in sorted(cts, key=lambda x: x["name"]):
        st = ct["status"]
        ok = "unhealthy" not in st and "Restarting" not in st and ct["state"] == "running"
        dot = c(32, "●") if ok and "starting" not in st else c(33 if ok else 31, "●")
        row = (f" {dot} {pad(safe(ct['name'])[:36], 37)}{pad(safe(st)[:22], 23)}"
               f"{pad(human(ct['mem']), 7)} {fmt_ports(ct['ports'])}")
        lines.append("│" + pad(clip(row, inner), inner) + "│")
    lines.append("└" + "─" * inner + "┘")
    return lines


def containers_block(data, w, now=None):
    now = now or time.time()
    if data is None:
        return [c(31, " collector not running: no state in " + STATE)]
    lines = []
    if data.get("absent"):
        return [msg("info", "docker not installed on this machine")]
    if data.get("error"):
        lines.append(c(31, " docker: " + safe(data["error"])))
    age = now - data.get("ts", 0)
    if age > STALE_S:
        lines.append(c(33, f" stale data ({int(age)} s old): collector stopped?"))
    groups = {}
    for ct in data["containers"]:
        groups.setdefault(safe(ct["project"]) or "(standalone)", []).append(ct)
    total_mem = sum(ct["mem"] or 0 for ct in data["containers"])
    lines.append(f" {plural(len(data['containers']), 'container')}   RAM {human(total_mem)}   "
                 f"ports: * = all interfaces, lo: = local only")
    ncol = 2 if w >= WIDE else 1
    cw = (w - 2 * (ncol - 1)) // ncol
    boxes = [container_box(proj, groups[proj], cw) for proj in sorted(groups)]
    if ncol == 1:
        return lines + [ln for b in boxes for ln in b]
    # columns in reading order: the first fills up to half of the total lines
    half, cols, acc = sum(len(b) for b in boxes) / 2, [[], []], 0
    for b in boxes:
        cols[0 if acc + len(b) / 2 < half else 1].extend(b)
        acc += len(b)
    return lines + columns([(cols[0], cw), (cols[1], cw)], w)


page_container = containers_block  # historical name used by the tests


NET_STALE_S, BOOT_STALE_S = cards.NET_STALE_S, cards.BOOT_STALE_S  # the collectors' data older than this is stale (seconds)
NAMEW = 36
NCOL3 = 225  # from this width the single screen uses three columns
NET_SKIP = ("lo", "veth", "br-")  # container virtual interfaces: noise
NET_HIST = 30  # history samples for the traffic sparklines
REAL_FS = ("ext4", "ext3", "xfs", "btrfs", "vfat", "f2fs", "zfs", "ntfs3", "exfat", "nfs", "nfs4", "cifs")
BOOT_WINDOW_S = 900  # a container started within 15 min of boot "started with the boot"


def fs(x):
    return f"{x:.1f}s"


BOOT_COLORS = {"firmware": 35, "loader": 34, "kernel": 36, "initrd": 33, "userspace": 32, "main path": 36, "post boot": 32}
# the same BOOT blocks speak of systemd units, Windows services or launchd daemons depending on who wrote boot.json
BOOT_LABELS = {
    "linux": {"failed_one": "failed systemd unit", "failed_short": "failed unit", "journal_in": " in this boot's journal",
              "failed_title": "FAILED UNITS", "enabled_title": "SERVICES ENABLED AT BOOT", "journal_title": "BOOT JOURNAL",
              "journal_short": "journal", "kernel": "kernel "},
    "windows": {"failed_one": "failed service", "failed_short": "failed service", "journal_in": " in the System event log since boot",
                "failed_title": "FAILED SERVICES", "enabled_title": "AUTOMATIC SERVICES", "journal_title": "SYSTEM EVENT LOG",
                "journal_short": "events", "kernel": ""},
    "darwin": {"failed_one": "failed launch daemon", "failed_short": "failed daemon", "journal_in": " in the system log",
               "failed_title": "FAILED LAUNCH DAEMONS", "enabled_title": "LAUNCH DAEMONS (third-party)", "journal_title": "SYSTEM LOG",
               "journal_short": "log", "kernel": ""},
}


def boot_labels(b):
    return BOOT_LABELS.get(os_of(b), BOOT_LABELS["linux"])


def unsupported(d, key):
    """The collector of this OS has no such section (e.g. systemd-analyze blame on Windows): leave the block out."""
    return isinstance(d, dict) and key in (d.get("unsupported") or [])


def boot_block_avvio(b, up, w):
    an, lbl = b.get("analyze"), boot_labels(b)
    lines = [section("BOOT", w), ""]
    if an is None:
        head = [f"   {safe(b.get('kernel', '?'))}   up {fmt_dur(up)}"] if os_of(b) != "linux" else []
        return lines + head + [unavail_msg(b, "analyze", "boot times unavailable")]
    parts, total = an["parts"], max(an["total"], 0.001)
    bw = max(20, min(w - 8, 80))
    widths = {k: max(1, round(bw * v / total)) for k, v in parts.items()}
    lines.append(f"   boot finished in {c(1, fs(total))}   {lbl['kernel']}{safe(b.get('kernel', '?'))}   up {fmt_dur(up)}")
    lines.append("   " + "".join(c(BOOT_COLORS.get(k, 37), "█" * n) for k, n in widths.items()))
    lines += wrap_items([c(BOOT_COLORS.get(k, 37), "■") + f" {k} {fs(v)}" for k, v in parts.items()], w, indent=3, sep="  ")
    return lines


def boot_block_lente(b, w, k):
    lines = [section("SLOWEST UNITS", w, "activation time: not all of them block boot"), ""]
    bl = b.get("blame")
    if bl is None:
        return lines + [unavail_msg(b, "blame")]
    top = lim(bl, k, "boot")
    mx = max((x["s"] for x in top), default=1) or 1
    for x in top:
        n = max(1, round(20 * x["s"] / mx))
        t = c(33, fs(x["s"])) if x["s"] >= 5 else fs(x["s"])
        lines.append(f"   {pad(safe(x['unit'])[:38], 39)}{c(36, '█' * n)}{c(90, '░' * (20 - n))}  {t}")
    return lines


def boot_block_fallite(b, w):
    lines = [section(boot_labels(b)["failed_title"], w), ""]
    f = b.get("failed")
    if f is None:
        return lines + [unavail_msg(b, "failed")]
    return lines + ([msg("err", safe(u)) for u in f] if f else [msg("ok", "none")])


def boot_block_servizi(b, w, k):
    lines = [section(boot_labels(b)["enabled_title"], w), ""]
    en = b.get("enabled")
    if en is None:
        return lines + [unavail_msg(b, "enabled")]
    act = [e for e in en if e["state"] == "active"]
    off = [e for e in en if e["state"] != "active"]
    lines.append(f"   {len(en)} enabled   {c(32, f'{len(act)} active')}   {len(off)} inactive (often one-shots already done)")
    lines += wrap_items([c(90, e["unit"].replace(".service", "")) for e in act], w, indent=5, max_lines=k, section="boot")
    fail = [e for e in off if e["state"] == "failed"]
    if fail:
        lines += [msg("err", safe(e["unit"]) + " failed") for e in fail]
    return lines


def boot_block_container(b, w, k, now):
    lines = [section("CONTAINERS & REBOOT", w), ""]
    cs = b.get("containers")
    if cs is None:
        return lines + [unavail_msg(b, "containers")]
    bt = b.get("btime", 0)
    at_boot = [x for x in cs if x["restart"] != "no" and x["started"] - bt <= BOOT_WINDOW_S]
    manual = [x for x in cs if x["restart"] == "no"]
    lines.append(f"   {c(32, str(len(at_boot)))} started at boot (restart policy)   "
                 f"{c(33, str(len(manual))) if manual else 0} started by hand: won't restart on reboot")
    if manual:
        lines += wrap_items([safe(x["name"]) for x in manual], w, indent=5, max_lines=k)
    return lines


def boot_block_journal(b, w, k):
    j = b.get("journal")
    lines = [section(boot_labels(b)["journal_title"], w, "warning and worse"), ""]
    if j is None:
        return lines + [unavail_msg(b, "journal")]
    cap = " (last 500)" if j.get("capped") else ""
    lines.append(f"   {c(31, str(j['err'])) if j['err'] else 0} errors   {c(33, str(j['warn'])) if j['warn'] else 0} warning{cap}")
    room = max(20, w - 45)
    for e in lim(j["top"], k, "boot"):
        col = 31 if e["pr"] <= 3 else 33
        lines.append(f"   {c(col, '●')} {pad(safe(e['id'])[:26], 27)}{e['n']:>4}×  {c(90, safe(e['last'])[:room])}")
    return lines


def tight(lines):
    """Compact mode: removes the empty line right after each section title."""
    out = []
    for i, ln in enumerate(lines):
        if ln == "" and i and ANSI.sub("", lines[i - 1]).startswith("──"):
            continue
        out.append(ln)
    return out


def page_boot(b, w, body_h, now=None):
    now = now or time.time()
    if b is None:
        return ["", msg("err", "boot collector not running: no state in " + BOOT_STATE)]
    head = [msg("warn", f"boot data stale ({int(now - b.get('ts', 0))} s old)"), ""] if now - b.get("ts", 0) > BOOT_STALE_S else []
    up = now - b.get("btime", now)
    wide = w >= WIDE
    # a single page: if it does not fit the height the lists shrink (k), then it splits like the others
    # macOS/Windows: the blocks their collector has no data for are left out, not shown empty
    slow = (lambda bw, k: []) if unsupported(b, "blame") else (lambda bw, k: boot_block_lente(b, bw, k) + [""])
    jour = (lambda bw, k: []) if unsupported(b, "journal") else (lambda bw, k: [""] + boot_block_journal(b, bw, k))
    for k in (10, 8, 6, 4, 3):
        if wide:
            lw, rw = int(w * 0.5), w - int(w * 0.5) - 3
            left = boot_block_avvio(b, up, lw) + [""] + slow(lw, k) + boot_block_fallite(b, lw)
            right = (boot_block_servizi(b, rw, k // 2 + 1) + [""] + boot_block_container(b, rw, k // 2, now)
                     + jour(rw, k))
            lines = head + columns([(left, lw), (right, rw)], w, gap=3)
        else:
            lines = (head + boot_block_avvio(b, up, w) + [""] + slow(w, k)
                     + boot_block_fallite(b, w) + [""] + boot_block_servizi(b, w, k // 3 + 1) + [""]
                     + boot_block_container(b, w, k // 3, now) + jour(w, k))
        if len(lines) <= body_h:
            return lines
    return tight(lines) if len(tight(lines)) <= body_h else lines


def load_baseline(path=None):
    """valid dict | None if missing | 'corrotta' (corrupt) if it exists but is unreadable (not 'missing': must be flagged)."""
    path = path or BASELINE
    if not os.path.exists(path):
        return None
    d = load_json(path)
    return d if isinstance(d, dict) and isinstance(d.get("ports"), dict) else "corrotta"


def accept_baseline(if_missing=False, path=None, now=None):
    """Saves the current set of exposed ports as 'expected'. As root: sudo nuc-console-accept.

    Refuses a stale or partial state: it would bless as normal what could not be measured.
    """
    path, now = path or BASELINE, now or time.time()
    if if_missing and isinstance(load_baseline(path), dict):
        print("baseline already present: left untouched")
        return 0
    net, cont = load_json(NET_STATE), load_containers()
    if (not isinstance(net, dict) or now - net.get("ts", 0) > NET_STALE_S or exposure_partial(net)
            or cont is None or now - cont.get("ts", 0) > STALE_S):
        print("network/container state missing, stale or incomplete: baseline not created", file=sys.stderr)
        return 1
    cur = exposure_keys(net, cont)
    if cur is None:
        print("port list unavailable: baseline not created", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"ts": now, "ports": cur}, f, indent=1)
        f.flush()
        os.fsync(f.fileno())  # a power cut must not leave an empty file
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print(f"baseline written: {len(cur)} exposed ports in {path}")
    return 0


TELEGRAM_DOWN_S, TELEGRAM_FAILING_S = 300, 600  # notify.py rewrites status.json every 30 s: older than 5 min = not running; 10 min of failed sends = say so
TELEGRAM_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")  # a bot token must never reach the screen, whoever put it in a message


def telegram_status(path=None):
    """notify.py's status.json (counters and the last error, never a secret): a dict; None if missing or broken; False if this user may
    not read it (on Windows only administrators and the service accounts can open the notifier's folder, where the token is)."""
    try:
        with open(path or os.path.join(nuc_config.NOTIFY_DIR, "status.json"), encoding="utf-8") as f:
            d = json.load(f)
    except PermissionError:
        return False
    except (OSError, ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def telegram_state(now, path=None):
    """The notifier as status.json shows it: ("ok" | "unreadable" | "unpaired" | "down" | "failing", how long and why, for "failing")."""
    tg = telegram_status(path)
    if tg is False:
        return "unreadable", ""  # cannot look: neither a problem nor a proof that all is well
    if tg and tg.get("paired") is False:
        return "unpaired", ""  # its last word, even if it has stopped since (it exits at once when there is no chat)
    ts, since = (tg or {}).get("ts"), (tg or {}).get("failing_since")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or now - ts > TELEGRAM_DOWN_S:
        return "down", ""
    if not isinstance(since, bool) and isinstance(since, (int, float)) and now - since > TELEGRAM_FAILING_S:
        err = TELEGRAM_TOKEN.sub("<token>", safe(tg.get("last_error") or ""))[:60].strip()
        return "failing", fmt_ago(now - since) + (": " + err if err else "")
    return "ok", ""


def problems_raw(net, cont, now=None, boot=False, thermal=None, baseline=False):
    """Every anomaly, by decreasing severity: [(3=port change | 2=error | 1=warning, text, problem id)]. Ids are stable."""
    now, out = now or time.time(), []
    if CFG.get("config_error"):  # config.ini exists but could not be read: nothing in it ([expose], [webapps]) is applied
        out.append((2, "config.ini unreadable: defaults in use ([expose] and [webapps] not applied)", "config-unreadable"))
    if not on("containers"):
        pass
    elif cont is None:
        out.append((2, "container collector not running", "collector-containers"))
    else:
        if now - cont.get("ts", 0) > STALE_S:
            out.append((1, f"container state stale ({int(now - cont.get('ts', 0))} s old)", "stale-containers"))
        down = [ct for ct in cont["containers"] if ct["state"] != "running"]
        sick = [ct for ct in cont["containers"] if "unhealthy" in ct["status"] or "Restarting" in ct["status"]]
        if sick:
            out.append((2, plural(len(sick), "unhealthy container"), "unhealthy-container"))
        if down:
            out.append((1, plural(len(down), "container") + " exited with an error", "container-exited"))
    if thermal and on("thermal"):
        for label, key in (("CPU", "cpu"), ("NVMe", "nvme")):
            if key in thermal:
                t, mx = thermal[key]
                if t >= THERMAL_ERR * mx:
                    out.append((2, f"{label} at {t:.0f}°C: above the {THERMAL_ERR * mx:.0f}°C threshold", "thermal"))
                elif t >= THERMAL_WARN * mx:
                    out.append((1, f"{label} at {t:.0f}°C: above the {THERMAL_WARN * mx:.0f}°C threshold", "thermal"))
        if thermal.get("recent"):
            out.append((1, f"CPU thermal throttling: {thermal['recent']} events in the last minute", "throttling"))
    if not on("boot"):
        pass
    elif boot is None:
        out.append((1, "boot collector not running", "collector-boot"))
    elif boot:
        lbl = boot_labels(boot)
        if boot.get("failed"):
            out.append((2, plural(len(boot["failed"]), lbl["failed_one"]) + ": " + ", ".join(safe(u) for u in boot["failed"][:3]), "failed-units"))
        if boot.get("journal") and boot["journal"]["err"]:
            out.append((1, plural(boot["journal"]["err"], "error") + lbl["journal_in"], "journal-errors"))
    if net is None:
        out.append((2, "network collector not running", "collector-net"))
        return sorted(out, key=lambda x: -x[0])
    if now - net.get("ts", 0) > NET_STALE_S:
        out.append((1, f"network data stale ({int(now - net.get('ts', 0))} s old)", "stale-net"))
    if net.get("errors"):
        out.append((1, "network sections not collected: " + ", ".join(net["errors"]), "net-sections"))
    ufw, fw = net.get("ufw"), net.get("firewall")
    if os_of(net) != "linux":  # the OS firewall (Windows Firewall, macOS Application Firewall) instead of ufw
        name = (fw or {}).get("name") or "firewall"
        if fw is None and is_disabled(net, "firewall"):
            pass
        elif fw is None:
            out.append((2, "firewall state unreadable: LAN exposure unknown", "firewall-unreadable"))
        else:
            if fw.get("off"):
                where = f" on the {', '.join(fw['off'])} network" if fw.get("kind") == "windows" else ""
                out.append((2, f"{name} off{where}: listening services are reachable from the LAN", "firewall-off"))
            if fw.get("policy"):
                out.append((1, f"{name} has rules from Group Policy: they are not read", "firewall-policy"))
    elif ufw is None and is_disabled(net, "ufw"):
        pass  # firewall switched off in config.ini: the user's choice, not an alarm
    elif ufw is None and is_absent(net, "ufw"):
        out.append((1, "ufw not installed: LAN filtering cannot be verified", "ufw-missing"))
    elif ufw is None:
        out.append((2, "ufw unreadable", "ufw-unreadable"))
    elif not ufw["active"]:
        out.append((2, "ufw off: no LAN filtering for non-Docker services", "ufw-off"))
    if net.get("listeners") is not None and baseline == "corrotta":
        out.append((2, f"port baseline unreadable: regenerate it with {ACCEPT_CMD}", "baseline-unreadable"))
    elif net.get("listeners") is not None and baseline is None:
        out.append((1, f"port baseline missing: create it with {ACCEPT_CMD}", "baseline-missing"))
    elif isinstance(baseline, dict) and net.get("listeners") is not None:
        if exposure_partial(net):  # partial data: a comparison would raise false alarms (or hide real ones)
            out.append((1, "port comparison suspended: network sections unreadable", "port-compare-suspended"))
        else:
            new, gone, changed = baseline_diff(exposure_keys(net, cont) or {}, baseline)
            for k, v in lim(list(new.items()), 3, "attention"):
                port, _, group = k.partition(":")
                out.append((3, f"NEW exposed port: {port} {group.lower()} ({safe(v['name'])[:24]})", "port-new"))
            if len(new) > 3 and not FULL:
                out.append((3, f"… and {len(new) - 3} more new exposed ports", "port-new"))
            for k, why in lim(list(changed.items()), 3, "attention"):
                out.append((3, f"CHANGED {k.partition(':')[0]}: {why}", "port-changed"))
            if gone:
                out.append((1, plural(len(gone), "port") + f" no longer exposed: if intended, {ACCEPT_CMD}", "port-gone"))
    if net.get("listeners") is not None:
        rows = exposure_rows(net, cont)
        pub = [r for r in rows if r["net"] == 1]
        expected = {p for ports in CFG["webapps"].values() for p in ports if p not in SENSITIVE}  # declared web apps: intended exposure
        bypass = [r for r in rows if r["bad_note"] and r["note"].startswith("docker") and r["lan"] == 1
                  and not (r["proto"] == "tcp" and r["port"] in expected)]
        dbs = [r for r in rows if r["warn"]]
        if dbs:
            out.append((2, f"{len(dbs)} DB/broker open on LAN", "db-open-lan"))
        if bypass:
            out.append((1, plural(len(bypass), "Docker port") + " bypassing ufw (DOCKER-USER empty)", "docker-bypass"))
        if pub:
            out.append((1, plural(len(pub), "service") + f" public on the Internet (Funnel :{pub[0]['port']})", "funnel-public"))
        if CFG["expose"]:  # [expose] only adds alarms: the ones above stay as they are, declared or not
            over = expose_over_items(expose_apply(rows, net, cont))
            if over:
                out.append((2, plural(len(over), "service") + (" reaches" if len(over) == 1 else " reach") + " beyond config.ini: " + ", ".join(over), "over-exposed"))
            ok = (isinstance(cont, dict) and not any(cont.get(k) for k in ("error", "absent", "disabled")) and isinstance(boot, dict)
                  and not {"links", "dbs"} & set(net.get("errors") or ()))  # with a source of names missing every name looks wrong
            lost = expose_unmatched(net, cont, boot) if ok else []
            if lost:
                out.append((1, "[expose] " + ", ".join(f"'{safe(k)}'" for k in lost) + (" matches" if len(lost) == 1 else " match") + " no service", "expose-unmatched"))
    if CFG["telegram"]["enabled"]:  # notify.py (docs/TELEGRAM.md): only then its status.json is read; switched off = nothing to say
        state, why = telegram_state(now)
        if state == "unpaired":
            out.append((1, "Telegram notifications on, but not paired", "telegram-unpaired"))
        elif state == "down":
            out.append((1, "Telegram notifier not running", "telegram-failing"))
        elif state == "failing":
            out.append((1, f"Telegram notifications failing for {why}", "telegram-failing"))
    return sorted(out, key=lambda x: -x[0])


ACCEPTED_PATH = os.environ.get("NUC_CONSOLE_ACCEPTED", os.path.join(nuc_config.LIB_DIR, "accepted.json"))

# id -> (what it is, why it matters, how to handle it). Shown by `nuc-console-problems`.
CATALOG = {
    "collector-containers": ("Container collector not running", "no container data", "sudo systemctl status nuc-console-collector; journalctl -u nuc-console-collector"),
    "collector-net": ("Network collector not running", "no exposure/firewall data", "sudo systemctl restart nuc-console-collector"),
    "collector-boot": ("Boot collector not running", "no boot data", "sudo systemctl restart nuc-console-collector"),
    "stale-containers": ("Container state is old", "the collector stopped updating", "sudo systemctl restart nuc-console-collector"),
    "stale-net": ("Network data is old", "the collector stopped updating", "sudo systemctl restart nuc-console-collector"),
    "net-sections": ("Some network sections could not be collected", "the exposure picture may be incomplete", "journalctl -u nuc-console-collector; the section name is in the message"),
    "unhealthy-container": ("Container unhealthy or restarting", "the service may be down or degraded", "docker ps; docker logs <name>; fix the healthcheck or the app. A wrong healthcheck path is the usual cause"),
    "container-exited": ("Container exited with an error", "a service that should run is down", "docker ps -a; docker logs <name>; docker start <name> or remove it if obsolete"),
    "thermal": ("Temperature above the threshold", "throttling and hardware wear", "check airflow/dust; sensors; reduce load"),
    "throttling": ("CPU thermal throttling", "the CPU is slowing itself down", "check cooling; look at the thermal bars"),
    "failed-units": ("Failed systemd units", "a service that should run is down", "systemctl --failed; journalctl -u <unit>; systemctl reset-failed once handled"),
    "journal-errors": ("Errors in this boot's journal", "usually noise (docker veth races, firmware ACPI), sometimes a real fault", "journalctl -b -p err -o short | sort | uniq -c | sort -rn | head; accept it if it is known noise"),
    "ufw-off": ("ufw is off", "no filtering for non-Docker services on the LAN", "sudo scripts/enable-ufw.sh (LAN=<your subnet>) - keeps a rollback timer"),
    "ufw-missing": ("ufw not installed", "LAN filtering cannot be verified", "install ufw, or accept this if you use nftables/firewalld"),
    "ufw-unreadable": ("ufw status unreadable", "firewall state unknown", "sudo ufw status verbose; journalctl -u nuc-console-collector"),
    "docker-bypass": ("Docker ports bypass ufw", "a port published on 0.0.0.0 is reachable from the LAN whatever ufw says", "publish on 127.0.0.1 (compose: \"127.0.0.1:PORT:PORT\") or declare the app under [webapps] in config.ini if the exposure is intended; or add a DOCKER-USER rule"),
    "db-open-lan": ("Database/broker open on the LAN", "data services should not be reachable from the network", "publish the DB on 127.0.0.1 (scripts/rebind-all-dbs.sh) or stop it if unused"),
    "funnel-public": ("Service public on the Internet (Tailscale Funnel)", "anyone on the Internet can reach it", "tailscale funnel status; turn it off if not needed: tailscale funnel --https=PORT off"),
    "over-exposed": ("Service reaches further than config.ini says", "you declared under [expose] how far it should be reachable, and it is reachable from more places",
                     "bind it to 127.0.0.1 (or to the interface you meant), close the port in the firewall, or turn the Funnel off; if the wider reach is intended, say so under [expose] in config.ini"),
    "expose-unmatched": ("[expose] name matches no service", "a name that matches nothing (a typo, or a service that was removed) guards nothing",
                         "fix the name under [expose] in config.ini (container, compose service or project, process, unit, database, [webapps] name) or remove the line; ports are never checked"),
    "config-unreadable": ("config.ini unreadable", "the file exists but could not be read, so the defaults are in use: [expose] and [webapps] are not applied and nothing is checked against them",
                          "check config.ini for a key starting with ':' (write ports as 8080) or a section without a header; the exact error is in the service logs / stderr; restart the services after fixing it"),
    "baseline-missing": ("Port baseline missing", "new ports cannot be detected", "sudo nuc-console-accept"),
    "baseline-unreadable": ("Port baseline unreadable", "new ports cannot be detected", "sudo nuc-console-accept"),
    "port-compare-suspended": ("Port comparison suspended", "network sections were unreadable", "see net-sections"),
    "port-new": ("New exposed port", "something started listening where it did not before", "identify it (ss -ltnp); if intended: sudo nuc-console-accept; if not, stop it"),
    "port-changed": ("Exposed port changed", "a different service or a weaker filter on a known port", "check what changed; if intended: sudo nuc-console-accept"),
    "port-gone": ("Port no longer exposed", "a service you expected is gone", "if intended: sudo nuc-console-accept"),
}


CATALOG.update({  # macOS/Windows: the OS firewall in place of ufw (the advice is per OS, below)
    "firewall-off": ("Firewall off", "every listening service is reachable from the network", "turn the firewall on"),
    "firewall-unreadable": ("Firewall state unreadable", "LAN exposure cannot be judged: ports are shown as unknown", f"{CMD['logs']}"),
    "firewall-policy": ("Firewall rules from Group Policy", "rules set by policy are not in the local store: those ports are shown as unknown",
                        "Get-NetFirewallRule -PolicyStore ActiveStore lists the effective rules"),
})
# the same problems, explained with the commands of this OS
OS_CATALOG = {
    "windows": {
        "collector-containers": ("Container collector not running", "no container data",
                                 r"Get-ScheduledTask -TaskPath \nuc-console\ ; log: " + CMD["logs"]),
        "collector-net": ("Network collector not running", "no exposure/firewall data", CMD["restart"]),
        "collector-boot": ("Boot collector not running", "no boot data", CMD["restart"]),
        "stale-containers": ("Container state is old", "the collector stopped updating", CMD["restart"]),
        "stale-net": ("Network data is old", "the collector stopped updating", CMD["restart"]),
        "net-sections": ("Some network sections could not be collected", "the exposure picture may be incomplete",
                         CMD["logs"] + "; the section name is in the message"),
        "failed-units": ("Automatic services stopped with an error", "a service that should run is down",
                         "Get-Service <name>; Event Viewer > Windows Logs > System says why; Start-Service <name>"),
        "journal-errors": ("Errors in the System event log since boot", "usually noise (DCOM permissions, drivers), sometimes a real fault",
                           "Event Viewer > Windows Logs > System; accept it if it is known noise"),
        "db-open-lan": ("Database/broker open on the LAN", "data services should not be reachable from the network",
                        "publish the DB on 127.0.0.1 (compose: \"127.0.0.1:5432:5432\") or stop it if unused"),
        "firewall-off": ("Windows Firewall off", "every listening service is reachable from that network",
                         "Windows Security > Firewall & network protection: turn it on; or, as administrator: Set-NetFirewallProfile -All -Enabled True"),
        "baseline-missing": ("Port baseline missing", "new ports cannot be detected", "nuc-console-accept (administrator prompt)"),
        "baseline-unreadable": ("Port baseline unreadable", "new ports cannot be detected", "nuc-console-accept (administrator prompt)"),
        "port-new": ("New exposed port", "something started listening where it did not before",
                     "identify it (Get-NetTCPConnection -State Listen); if intended: nuc-console-accept (administrator); if not, stop it"),
        "port-changed": ("Exposed port changed", "a different service or a weaker filter on a known port",
                         "check what changed; if intended: nuc-console-accept (administrator)"),
        "port-gone": ("Port no longer exposed", "a service you expected is gone", "if intended: nuc-console-accept (administrator)"),
    },
    "darwin": {
        "collector-containers": ("Container collector not running", "no container data",
                                 "sudo launchctl print system/com.nuc-console.collector; log: " + CMD["logs"]),
        "collector-net": ("Network collector not running", "no exposure/firewall data", CMD["restart"]),
        "collector-boot": ("Boot collector not running", "no boot data", CMD["restart"]),
        "stale-containers": ("Container state is old", "the collector stopped updating", CMD["restart"]),
        "stale-net": ("Network data is old", "the collector stopped updating", CMD["restart"]),
        "net-sections": ("Some network sections could not be collected", "the exposure picture may be incomplete",
                         CMD["logs"] + "; the section name is in the message"),
        "failed-units": ("Launch daemons that exited with an error", "a service that should run is down",
                         "sudo launchctl print system/<label>; its log is named in the plist (StandardErrorPath)"),
        "firewall-off": ("macOS firewall off", "every listening service is reachable from the network",
                         "System Settings > Network > Firewall: turn it on (or: sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate on)"),
        "port-new": ("New exposed port", "something started listening where it did not before",
                     "identify it (sudo lsof -nP -iTCP -sTCP:LISTEN); if intended: sudo nuc-console-accept; if not, stop it"),
    },
}
CATALOG.update({  # notify.py (Telegram, docs/TELEGRAM.md); only when [telegram] enabled = yes
    "telegram-unpaired": ("Telegram notifications on, but not paired", "no alert can reach your phone: this machine does not know your chat",
                          "sudo nuc-console-telegram --setup (the bot token from @BotFather, your @username), then tap the link it prints and press Start"),
    "telegram-failing": ("Telegram notifier not running or failing", "new problems are not reaching your phone",
                         "sudo nuc-console-telegram --status; sudo nuc-console-telegram --test; journalctl -u nuc-console-notify; "
                         "sudo systemctl restart nuc-console-notify"),
})
OS_CATALOG["windows"].update({
    "telegram-unpaired": ("Telegram notifications on, but not paired", "no alert can reach your phone: this machine does not know your chat",
                          "nuc-console-telegram.cmd --setup (administrator prompt: the bot token from @BotFather, your @username), "
                          "then tap the link it prints and press Start"),
    "telegram-failing": ("Telegram notifier not running or failing", "new problems are not reaching your phone",
                         "nuc-console-telegram.cmd --status; nuc-console-telegram.cmd --test; log: %ProgramData%\\nuc-console\\logs\\notify.log; "
                         "restart: nuc-console-telegram.cmd --on (administrator prompt)"),
})
OS_CATALOG["darwin"].update({
    "telegram-unpaired": ("Telegram notifications on, but not paired", "no alert can reach your phone: this machine does not know your chat",
                          "sudo nuc-console-telegram --setup (the bot token from @BotFather, your @username), then tap the link it prints and press Start"),
    "telegram-failing": ("Telegram notifier not running or failing", "new problems are not reaching your phone",
                         "sudo nuc-console-telegram --status; sudo nuc-console-telegram --test; log: /var/log/nuc-console/notify.log; "
                         "restart: sudo launchctl kickstart -k system/com.nuc-console.notify"),
})
CATALOG.update(OS_CATALOG.get(nuc_config.OS_NAME, {}))
if nuc_config.PORTABLE:
    CATALOG = {k: (t, w, re.sub(r"(?:sudo )?nuc-console-accept(?: \(administrator prompt\))?", ACCEPT_CMD, a)) for k, (t, w, a) in CATALOG.items()}
    # the collector is part of the run, not a service: no systemctl, launchctl or scheduled task, and its log is a file
    _again = CMD["restart"] + "; log: " + CMD["logs"]
    CATALOG.update({k: (CATALOG[k][0], CATALOG[k][1], a) for k, a in {
        "collector-containers": _again, "collector-net": _again, "collector-boot": _again,
        "stale-containers": _again, "stale-net": _again,
        "net-sections": CMD["logs"] + "; the section name is in the message",
        "ufw-unreadable": "sudo ufw status verbose; log: " + CMD["logs"],
        # the Telegram notifier is a service of an installed nuc-console: a portable run does not start it
        "telegram-unpaired": "a portable run sends no Telegram message: install nuc-console (docs/TELEGRAM.md), or [telegram] enabled = no",
        "telegram-failing": "a portable run sends no Telegram message: install nuc-console (docs/TELEGRAM.md), or [telegram] enabled = no",
    }.items()})


NOT_ACCEPTABLE = {"port-new", "port-changed", "port-gone"}  # port changes are handled by the baseline: sudo nuc-console-accept
COUNT_MATTERS = {"db-open-lan", "docker-bypass", "funnel-public", "unhealthy-container", "container-exited", "failed-units"}
COUNT_MATTERS.add("over-exposed")  # which services go beyond [expose] matters, not only how many: a new one is a new problem
COUNT_MATTERS.add("expose-unmatched")  # a new typo is a new problem: an accepted one must not hide it


def fingerprint(sev, text, pid):
    """What was accepted: severity + text. For exposure/health items the exact text (one more is a new problem); for noisy
    counters (journal errors, temperatures) the digits are ignored, so 118 -> 120 stays accepted but a worse severity does not."""
    return f"{sev}|" + (text if pid in COUNT_MATTERS else re.sub(r"\d+", "#", text))


class ProblemList(list):
    """List of (severity, text) with .accepted = how many known items were left out (shown under ATTENTION) and .pids = the problem id
    of each item, in the same order (None when the list was not built by problems(): cards.Ctx then cannot tell which card a problem is
    about)."""
    accepted = 0
    pids = None


def load_accepted(path=None):
    """{problem id: {"reason", "fp", ...}} accepted as known. A missing or broken file means nothing is accepted: it never hides by accident."""
    try:
        with open(path or ACCEPTED_PATH) as f:
            d = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if k in CATALOG and k not in NOT_ACCEPTABLE and isinstance(v, dict)
            and isinstance(v.get("fp"), str) and isinstance(v.get("reason", ""), str)}


def current_problem_records():
    smp = Sampler()
    time.sleep(0.5)
    st, sm = snapshot(200), smp.sample()
    return problem_records(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])


def accept_problem(pid, reason="", forget=False, path=None, now=None, records=None):
    """Mark what you see now as known (dimmed, counted under ATTENTION) or forget it. As root:
    sudo nuc-console-accept --problem ID --reason ... The acceptance is tied to the current severity and text: a worse or
    different situation shows up again."""
    path = path or ACCEPTED_PATH
    if pid not in CATALOG:
        print("unknown problem id; known: " + ", ".join(sorted(CATALOG)), file=sys.stderr)
        return 2
    cur = load_accepted(path)
    if forget:
        cur.pop(pid, None)
    else:
        if pid in NOT_ACCEPTABLE:
            print(f"port changes are accepted with the baseline: {ACCEPT_CMD} (no --problem)", file=sys.stderr)
            return 2
        reason = CTRL.sub(" ", reason).strip()
        if not reason:
            print(f"--reason is required: write why this is acceptable (it is shown in `{PROBLEMS_CMD}`)", file=sys.stderr)
            return 2
        recs = current_problem_records() if records is None else records
        rec = next((r for r in recs if r["id"] == pid), None)
        if rec is None:
            print(f"{pid} is not a current problem: nothing to accept", file=sys.stderr)
            return 2
        cur[pid] = {"reason": reason[:200], "ts": now or time.time(), "fp": rec["fingerprint"]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print(("forgot " if forget else "accepted ") + pid)
    return 0


def problems(*a, **kw):
    """Anomalies to show, by decreasing severity: ProblemList of (3|2|1, text), without the ones you accepted. Empty = all ok."""
    acc = load_accepted()
    out = ProblemList()
    out.pids = []
    for sev, text, pid in problems_raw(*a, **kw):
        if pid in acc and acc[pid]["fp"] == fingerprint(sev, text, pid):
            out.accepted += 1
        else:
            out.append((sev, text))
            out.pids.append(pid)
    return out


def problem_records(*a, **kw):
    """Every anomaly with its analysis, for `nuc-console-problems`: [{id, severity, text, accepted, reason, title, why, fix, fingerprint}]."""
    acc = load_accepted()
    out = []
    for sev, text, pid in problems_raw(*a, **kw):
        title, why, fix = CATALOG.get(pid, (pid, "", ""))
        fp = fingerprint(sev, text, pid)
        out.append({"id": pid, "severity": {3: "port-change", 2: "error", 1: "warning"}.get(sev, "warning"), "text": text,
                    "accepted": pid in acc and acc[pid]["fp"] == fp, "reason": (acc.get(pid) or {}).get("reason", ""), "title": title,
                    "why": why, "fix": fix, "fingerprint": fp, "acceptable": pid not in NOT_ACCEPTABLE})
    return out


def print_problems(argv):
    """`render.py --problems [--json]`: every current anomaly, with why it matters and how to fix it (read-only)."""
    recs = current_problem_records()
    if "--json" in argv:
        print(json.dumps(recs, indent=1, ensure_ascii=False))
        return 0
    if not recs:
        print("no problems")
        return 0
    print(f"{len(recs)} problems ({sum(r['accepted'] for r in recs)} accepted)\n")
    for r in recs:
        print(f"[{r['severity']}] {r['id']}" + ("   (ACCEPTED: " + safe(r["reason"]) + ")" if r["accepted"] else ""))
        print(f"    {safe(r['text'])}")
        if r["why"]:
            print(f"    why: {r['why']}")
        if r["fix"]:
            print(f"    fix: {r['fix']}")
        if r["acceptable"]:
            print(f"    accept if known:  {ACCEPT_CMD} --problem {r['id']} --reason \"...\"")
        else:
            print(f"    port changes are accepted with the baseline: {ACCEPT_CMD}")
    return 0


def safe_problems(*a, **kw):
    """problems() that never raises: a malformed state must not send the main loop into a crash-loop."""
    try:
        return problems(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return [(2, "unparseable state: " + safe(repr(e))[:60])]


def status_pill(pb):
    """(text, colour code) for the header: always visible, with a symbol besides the colour."""
    if not pb:
        return "✔ ALL OK", ui.sgr("banner_ok")
    if any(sev == 3 for sev, _ in pb):
        return "✖ EXPOSED PORTS CHANGED", ui.sgr("banner_err")
    n_err = sum(1 for sev, _ in pb if sev == 2)
    return (f"✖ {len(pb)} PROBLEMS", ui.sgr("banner_err")) if n_err else (f"! {len(pb)} WARNINGS", ui.sgr("banner_warn"))


def exposure_block(net, cont, w, new=None):
    new = new or {}
    rows = expose_apply(exposure_rows(net, cont), net, cont)
    by = {g: [r for r in rows if group_of(r) == g] for g, _ in GROUPS}
    warn = sum(r["warn"] for r in rows)
    count = (f"   Internet {c('1;31', len(by['INTERNET'])) if by['INTERNET'] else 0}    "
             f"LAN {c(33, len(by['LAN'])) if by['LAN'] else 0}    Tailscale {sum(r['ts'] == 1 for r in rows)}    "
             f"Local {len(by['LOCALE'])}")
    alert = c(31, f"⚠ {warn} DB/broker open on LAN") if warn else ""
    summary = [count + "        " + alert] if not warn or vlen(count) + 8 + vlen(alert) <= w else [count, "   " + alert]
    lines = [section("EXPOSURE", w), ""] + summary + [
             clip(c(90, "   ● open   ◐ filtered by source   ? unknown (treated as open)   · no"), w), ""]
    nw = max(12, min(NAMEW, w - 57))  # narrow column (3 columns): the name gets shorter, notes and cells stay visible
    lines.append(c(1, "   " + pad("PORT", 9) + pad("SERVICE", nw + 1)
                   + "".join(x.center(6) for x in ("LOC", "LAN", "TS", "NET")) + "  NOTE"))
    for g, title in GROUPS:
        if not by[g]:
            continue
        lines += ["", c(1, f"   {title}") + c(90, f"  ({len(by[g])})")]
        if g == "LOCALE":  # exception-based: local is not a risk, compact list
            lines += wrap_items([f"{r['port']} {r['name']}" for r in by[g]], w, indent=5)
            continue
        for r in by[g]:
            mark = c(31, "⚠") if r["warn"] else " "
            tag = new.get(f"{r['port']}/{r['proto'][0]}:{g}")
            declared = r["proto"] == "tcp" and r["port"] in {p for ps in CFG["webapps"].values() for p in ps if p not in SENSITIVE}
            cells = [cell(r["loc"], loc=True), cell(r["lan"], r["warn"]), cell(r["ts"], r["warn"]), cell(r["net"], net=True)]
            head = (f"   {r['port']:>5}/{r['proto'][0]} {mark}{pad(r['name'][:nw - 1], nw)}"
                    + "".join(f"  {x}   " for x in cells) + " ")
            room = w - vlen(head) - (len(tag) + 1 if tag else 0)  # never clip mid-word: end with an ellipsis
            base = ("declared: " + r["note"].replace("docker: bypasses ufw", "docker")) if declared and r["bad_note"] else r["note"]
            xn = expose_note(r)  # [expose]: beyond what config.ini says (red, instead of the note) or within it (grey, before the note)
            if xn:
                base = xn[0] if xn[1] else xn[0] + (" · " + base if base else "")
            text = base if vlen(base) <= room else base[:max(room - 1, 0)] + "…"
            note = c(31, text) if (xn and xn[1]) or (r["bad_note"] and not declared) else c(90, text)
            if tag:
                note = c("1;31", tag + " ") + note
            lines.append(head + note)
    return lines


def native_fw_lines(net):
    """macOS/Windows: the status line of the OS firewall (Windows Firewall per network profile, macOS Application Firewall)."""
    fw, err = net.get("firewall"), net.get("errors") or {}
    if fw is None and is_disabled(net, "firewall"):
        return [msg("info", "firewall check disabled in config.ini")]
    if fw is None:
        return [msg("err", "firewall unreadable: " + safe(err.get("firewall", "?"))[:70])]
    name = safe(fw.get("name") or "firewall")
    if fw.get("kind") == "windows":
        active = [n for n, p in (fw.get("profiles") or {}).items() if p.get("active")]
        line = (msg("err", c("1;31", f"{name} OFF") + f" on {safe(', '.join(fw['off']))}: no filtering there") if fw.get("off")
                else msg("ok", f"{name} on" + (f" (active: {safe(', '.join(active))})" if active else "")))
        return [line] + ([msg("warn", "rules from Group Policy are not read: those ports show ?")] if fw.get("policy") else [])
    if fw.get("state") == 0:
        return [msg("err", c("1;31", f"{name} OFF") + ": every listening program is reachable from the LAN")]
    return [msg("ok", f"{name} " + ("blocking all incoming" if fw.get("block_all") else "on") + (" · stealth" if fw.get("stealth") else ""))]


def native_fw_details(net, w, max_rules=None):
    """macOS/Windows FIREWALL body: the configuration in a few lines, then which rule or setting opens each listening port."""
    fw, lines = net.get("firewall"), []
    if fw and fw.get("kind") == "windows":
        for name, p in (fw.get("profiles") or {}).items():
            state = "on" if p.get("enabled", True) else c(31, "OFF")
            lines.append(kv(name, f"{state}   inbound {'allow' if p.get('inbound') == 1 else 'block'}"
                            + ("   block all" if p.get("block_all") else "") + (c(36, "   ← active") if p.get("active") else "")))
        if fw.get("networks"):
            lines.append(kv("networks", "  ".join(f"{safe(n['alias'])}: {safe(n['category'])}" for n in fw["networks"])))
        lines.append(kv("rules", f"{fw.get('allow_rules', 0)} allow · {fw.get('block_rules', 0)} block (enabled, inbound)"))
    elif fw:
        pf = fw.get("pf")
        lines.append(kv("signed apps", f"built-in {'allowed' if fw.get('builtin') else 'asked'} · downloaded "
                        f"{'allowed' if fw.get('downloaded') else 'asked'}"))
        lines.append(kv("app rules", f"{fw.get('apps_allowed', 0)} allowed · {fw.get('apps_blocked', 0)} blocked"))
        lines.append(kv("pf", "unreadable" if pf is None else
                        (f"on, {plural(pf.get('rules', 0), 'rule')} of its own (not interpreted)" if pf.get("enabled") else "off")))
    opened = {}
    for lst in net.get("listeners") or []:
        st, note = (lst.get("fw") or ["", ""])[:2]
        if st in ("open", "nofw") and bind_scope(lst["addr"]) != "lo":
            opened.setdefault(note, set()).add((lst["port"], lst["proto"][0]))
    if opened:
        rows = sorted(opened.items(), key=lambda kv_: min(kv_[1]))
        shown = rows if (max_rules is None or FULL or "firewall" in EXPAND) else rows[:max_rules]
        if len(shown) < len(rows):
            TRUNC.add("firewall")
        lines += ["", c(1, f"   WHAT LETS PORTS IN ({len(rows)})"), c(1, "   " + pad("RULE / SETTING", 44) + "PORTS")]
        for note, ports in shown:
            plist = ", ".join(f"{p}/{x}" for p, x in sorted(ports))
            lines.append(f"   {pad(safe(note)[:42], 44)}{clip(plist, max(10, w - 48))}")
        if len(shown) < len(rows):
            lines.append(c(90, f"   … +{len(rows) - len(shown)} more"))
    return lines


def fw_status_lines(net):
    """The two most important status lines: ufw and DOCKER-USER (macOS/Windows: the OS firewall)."""
    if os_of(net) != "linux":
        return native_fw_lines(net)
    err = net.get("errors") or {}
    ufw, du = net.get("ufw"), net.get("docker_user")
    lines = []
    if ufw is None and is_absent(net, "ufw"):
        lines.append(msg("info", "ufw not installed: LAN filtering cannot be verified from here (nft/firewalld?)"))
    elif ufw is None:
        lines.append(msg("err", "ufw unreadable: " + safe(err.get("ufw", "?"))[:80]))
    elif not ufw["active"]:
        lines.append(msg("err", c("1;31", "ufw OFF") + ": no LAN filtering for non-Docker services"))
    else:
        lines.append(msg("ok", "ufw active"))
    if du is None and (is_absent(net, "docker_user") or is_absent(net, "iptables")):
        pass  # no iptables/Docker: the DOCKER-USER chain does not exist, no line to show
    elif du is None:
        lines.append(msg("err", "DOCKER-USER unreadable: " + safe(err.get("docker_user", "?"))[:70]))
    elif not du:
        lines.append(msg("warn", "DOCKER-USER empty: ports published by containers bypass ufw"))
    else:
        lines.append(msg("ok", f"DOCKER-USER: {plural(len(du), 'rule')}"))
    return lines


def short_default(text):
    """'deny (incoming), allow (outgoing), deny (routed)' -> 'in deny · out allow · fwd deny' (fits a 3-column layout)."""
    names = {"incoming": "in", "outgoing": "out", "routed": "fwd"}
    found = re.findall(r"(\w+) \((incoming|outgoing|routed)\)", str(text))
    return safe("  ·  ".join(f"{names[d]} {a}" for a, d in found)) if found else safe(text)


def firewall_block(net, w, max_rules=None):
    err = net.get("errors") or {}
    ufw, du, ipt = net.get("ufw"), net.get("docker_user"), net.get("iptables")
    lines = [section("FIREWALL", w), ""] + fw_status_lines(net) + [""]  # the status before any detail
    if os_of(net) != "linux":
        return lines + native_fw_details(net, w, max_rules)
    if ufw is not None:
        lines.append(kv("ufw", f"{short_default(ufw['default'])}   log: {safe(ufw['logging'])}" if ufw["active"]
                        else c(31, "off (no rules in force)")))
    if ipt:
        pol, cnt = ipt["policy"], ipt["count"]
        lines.append(kv("iptables", "   ".join(f"{k} {pol.get(k, '?')} ({plural(cnt.get(k, 0), 'rule')})"
                                                for k in ("INPUT", "FORWARD"))))
        lines.append(kv("tailscale", c(32, "ts-input accepts tailscale0") if ipt["ts_input"]
                        else c(33, "ts-input rule not found: TS may not be open")))
    elif is_absent(net, "iptables"):
        lines.append(kv("iptables", c(90, "not installed")))
    elif "iptables" in err:
        lines.append(kv("iptables", c(31, "n/a: " + safe(err["iptables"])[:70])))
    f2b = net.get("f2b")
    if f2b is None and is_absent(net, "f2b"):
        pass  # fail2ban not installed: no line
    elif f2b is None or f2b.get("error"):
        lines.append(kv("fail2ban", c(31, "n/a: " + safe((f2b or {}).get("error") or err.get("f2b", "?"))[:70])))
    else:
        for j in f2b["jails"]:
            lines.append(kv("fail2ban", f"{safe(j['name'])}: {j['banned']} ban  {safe(' '.join(j['ips']))}"))
    dr = net.get("drops")
    if ufw is not None and ufw.get("logging", "").startswith("off"):
        lines.append(kv("drop 1h", c(33, "ufw logging off: blocks not logged")))
    elif dr is None and is_absent(net, "drops"):
        pass  # no journalctl: no drop count
    elif dr is None:
        lines.append(kv("drop 1h", c(33, "n/a " + safe(err.get("drops", "")))))
    else:
        lines.append(kv("drop 1h", str(dr["n"])))
        if dr["dpt"]:
            lines.append(kv("  ports", "  ".join(f"{safe(k)}×{v}" for k, v in dr["dpt"])))
            lines.append(kv("  sources", "  ".join(f"{safe(k)}×{v}" for k, v in dr["src"])))
    if ufw and ufw["rules"]:
        # IPv4 inbound only: IPv6 rules mirror them and outbound ones do not filter (default allow): keeping
        # them all would take the table to dozens of lines and drop the single screen to the compact level
        v6 = lambda r: "(v6)" in r["to"] + r["from"]
        rules_in = [r for r in ufw["rules"] if "OUT" not in r["action"] and "FWD" not in r["action"] and not v6(r)]
        n_out = sum("OUT" in r["action"] for r in ufw["rules"])
        n_v6 = sum(v6(r) and "OUT" not in r["action"] and "FWD" not in r["action"] for r in ufw["rules"])
        hidden = ", ".join(x for x in (f"{n_out} outbound" if n_out else "", f"{n_v6} mirrored IPv6" if n_v6 else "") if x)
        lines += ["", c(1, f"   UFW INBOUND RULES ({len(rules_in)})") + (c(90, f"   + hidden: {hidden}") if hidden else ""),
                  c(1, "   " + pad("TO", 28) + pad("ACTION", 14) + "FROM")]
        open_all = lambda r: (r["action"].startswith(("ALLOW", "LIMIT")) and r["from"].startswith("Anywhere")
                              and not r["to"].startswith("Anywhere"))  # exposes the port to the world: must be seen first
        shown = rules_in if (max_rules is None or FULL or "firewall" in EXPAND) else sorted(rules_in, key=lambda r: not open_all(r))[:max_rules]
        if len(shown) < len(rules_in):
            TRUNC.add("firewall")
        for r in shown:  # no cap: all of them, and the page splits by itself if they do not fit
            row = f"   {pad(safe(r['to'])[:26], 28)}{pad(safe(r['action'])[:12], 14)}{safe(r['from'])}"
            lines.append(c(33, row) if open_all(r) else row)
        if len(shown) < len(rules_in):
            lines.append(c(90, f"   … +{plural(len(rules_in) - len(shown), 'rule')}"))
    return lines


def page_rete(net, cont, w, now=None, baseline=False):
    if net is None:
        return ["", msg("err", "network collector not running: no state in " + NET_STATE)]
    now = now or time.time()
    pb = safe_problems(net, cont, now, baseline=baseline)
    new = new_ports(net, cont, baseline)
    if not on("exposure"):  # section switched off in config.ini: draw only the other one
        return [section("ATTENTION", w), ""] + [x for sev, t in pb[:6] for x in msg_wrap("err" if sev >= 2 else "warn", t, w)] + [""] + firewall_block(net, w)
    if not on("firewall"):
        return [section("ATTENTION", w), ""] + [x for sev, t in pb[:6] for x in msg_wrap("err" if sev >= 2 else "warn", t, w)] + [""] + exposure_block(net, cont, w, new)
    head = [section("ATTENTION", w), ""] + ([x for sev, t in pb[:6] for x in msg_wrap("err" if sev >= 2 else "warn", t, w)] if pb
                                              else [msg("ok", "no problems detected")]) + [""]

    if net.get("listeners") is None:  # without the port list "LAN 0" would look like "nothing exposed"
        if is_absent(net, "listeners"):
            return head + ["", msg("warn", "EXPOSURE unavailable: `ss` is missing (iproute2 package)")] + [""] + firewall_block(net, w)
        return head + ["", msg("err", "EXPOSURE unavailable: "
                                 + safe((net.get("errors") or {}).get("listeners", "?")))] \
            + [""] + firewall_block(net, w)
    if w >= WIDE:
        ew = int(w * 0.62)
        return head + columns([(exposure_block(net, cont, ew, new), ew), (firewall_block(net, w - ew - 3), w - ew - 3)],
                              w, gap=3)
    return head + exposure_block(net, cont, w, new) + ["", ""] + firewall_block(net, w)


def short_name(name, project=""):
    """'ethibid-api-1' -> 'api' (without the stack prefix and the replica index)."""
    n = safe(name)
    if project and n.startswith(project + "-"):
        n = n[len(project) + 1:]
    return re.sub(r"-\d+$", "", n)


def ct_ok(ct):
    return ct["state"] == "running" and "unhealthy" not in ct["status"] and "Restarting" not in ct["status"]


def stack_lines(cont, w, cap):
    """For each stack: a header with counts and RAM, then its services with a status dot."""
    groups = {}
    for ct in cont["containers"]:
        groups.setdefault(safe(ct["project"]), []).append(ct)
    lines = []
    for proj in sorted(groups, key=lambda x: (x == "", x)):
        cts = sorted(groups[proj], key=lambda x: x["name"])
        bad = sum(not ct_ok(ct) for ct in cts)
        head = (f" {c('1;36', '▸')} {c(1, pad((proj or '(no stack)')[:26], 27))}{len(cts) - bad}/{len(cts)} running   "
                f"RAM {human(sum(ct['mem'] or 0 for ct in cts))}" + (c(31, f"   ✖ {bad}") if bad else ""))
        items = [(c(32, "●") if ct_ok(ct) else c(31, "✖")) + " " + short_name(ct["name"], proj) for ct in cts]
        lines += [head] + wrap_items(items, w, indent=5, sep="   ", max_lines=cap, section="containers")
    return lines


def ov_sistema(s, w, k, cont=None):
    m, disk = s.get("mem"), disk_figures(s.get("disk_root"))  # the Sampler's figures: this block reads nothing from the host
    bw = max(8, min(40, w - 52))
    ram = ram_figures(m)
    lines = [section("SYSTEM", w, up_load_note(s.get("uptime"), s.get("load"))),
             f" RAM   {bar(ram[0] / ram[1], bw)} {human(ram[0])}/{human(ram[1])}   cache {human(m.get('Cached'))}" if ram else f" RAM   {c(33, '?')}",
             f" DISK  {bar(disk[0] / disk[1], bw)} {human(disk[0])}/{human(disk[1])}" if disk else f" DISK  {c(33, '?')}"]
    if on("thermal"):  # (the sampler leaves it empty when the feature is off; the demo's is not, so the switch is honoured here too)
        lines += thermal_lines(s.get("thermal") or {}, bw, w)
    cores = [v for _, v in sorted(s["cpu"].items(), key=lambda kv: int(kv[0][3:]))]
    if cores:
        mean = sum(cores) / len(cores)
        if k <= 2:  # one bar per core, in columns: the normal look; only the tiny-console levels (k>=3) compress to one character per core
            lines.append(f" CPU   {bar(mean, bw)} {mean * 100:.0f}%   {len(cores)} threads")
            cells = [f" {i:>2} {bar(v, 9)} {v * 100:3.0f}%" for i, v in enumerate(cores)]
            per = max(1, min(6, (w - 1) // 20))
            lines += ["".join(pad(x, 20) for x in cells[i:i + per]) for i in range(0, len(cells), per)]
        else:
            spark = "".join(c(32 if v < 0.7 else 33 if v < 0.9 else 31, SPARK[min(7, int(v * 8))]) for v in cores)
            lines.append(f" CPU   {bar(mean, bw)} {mean * 100:.0f}%   core {spark}")
    if k <= -2 and cont:
        top = lim(sorted((ct for ct in cont["containers"] if ct["mem"]), key=lambda ct: -ct["mem"]), 5, "system")
        if top:
            lines += ["", c(1, " HEAVIEST CONTAINERS (RAM)")]
            mx = top[0]["mem"]
            lines += [f" {pad(safe(ct['name'])[:34], 35)}{pad(human(ct['mem']), 7)}{bar(ct['mem'] / mx, 20, 2, 2)}" for ct in top]
    return lines


def ov_container(cont, w, k):
    lines = [section("CONTAINER", w)]
    if cont is None:
        return lines + [msg("err", "container collector not running")]
    if cont.get("absent"):
        return lines + [msg("info", "docker not installed on this machine")]
    cs = cont["containers"]
    down = [x for x in cs if x["state"] != "running"]
    sick = [x for x in cs if x["state"] == "running" and not ct_ok(x)]
    ok = len(cs) - len(down) - len(sick)
    lines.append(f" {plural(len(cs), 'container')}   RAM {human(sum(x['mem'] or 0 for x in cs))}   {c(32, '●')} {ok} ok"
                 + (f"   {c(31, f'✖ {len(down)} stopped')}" if down else "")
                 + (f"   {c(31, f'✖ {len(sick)} unhealthy')}" if sick else ""))
    if k <= 2:  # detail: which stacks and which services are running
        return lines + stack_lines(cont, w, {-2: 4, -1: 3, 0: 3, 1: 2, 2: 1}[k])
    groups = {}
    for x in cs:
        groups[safe(x["project"]) or "(standalone)"] = groups.get(safe(x["project"]) or "(standalone)", 0) + 1
    if k < 4:
        lines += wrap_items([f"{g} {n}" for g, n in sorted(groups.items())], w, indent=1, max_lines=1, section="containers")
    for x in (sick + down)[:max(0, 4 - k)]:
        lines.append(f" {c(31, '✖')} {pad(safe(x['name'])[:36], 37)}{c(90, safe(x['status'])[:30])}")
    return lines


def fmt_db_port(p):
    if p["c"] == "host":
        return "host network"
    pre = "lo:" if p["s"] == "lo" else "*" if p["s"] == "*" else p["s"] + ":"
    return f"{pre}{p['p'] or '?'}" + ("/u" if p["c"].endswith("/udp") else "")


def ov_database(net, cont, w, k):
    lines = [section("DATABASE", w)]
    if net is None:
        return lines + [msg("err", "network collector not running")]
    dbs = net.get("dbs")
    if dbs is None:
        return lines + [unavail_msg(net, "dbs")]
    items = dbs["items"]
    if not items:
        return lines + [msg("info", "no databases running")]
    exposed = lambda it: it["host_net"] or any(p["s"] != "lo" for p in it["ports"])
    open_n = sum(exposed(it) for it in items)
    lines[0] = section("DATABASE", w, f"{len(items)} running" + (f" · {open_n} exposed" if open_n else ""))
    now = time.time()
    kw = min(14, max(len(safe(it["kind"])) for it in items) + 1)
    txt = {it["name"]: " ".join(fmt_db_port(p) for p in it["ports"]) or "docker net only" for it in items}
    pw = min(28, max(len(t) for t in txt.values()) + 2)
    for it in items:
        lines.append(f" {c(31 if exposed(it) else 32, '●')} {c(1, pad(safe(it['kind']), kw))}{pad(safe(it['name'])[:30], 31)}"
                     f"{pad(txt[it['name']], pw)}" + (c(31, "exposed") if exposed(it) else c(90, "local only")))
        if k > 1:
            continue
        proj = safe(it["project"])
        names = lambda lst: ", ".join(short_name(n, proj) for n in lst)
        who = []
        if it["active"]:
            who.append(c(32, "in use now: ") + names(it["active"]))
        declared = [n for n in it["usano"] if n not in it["active"]]
        if declared:
            who.append("declared by " + names(declared))
        if it["stessa_rete"]:
            who.append(c(90, "same network: " + names(it["stessa_rete"])))
        if it["host_clients"]:
            who.append("host processes: " + ", ".join(safe(x) for x in it["host_clients"]))
        if not who:
            who.append(c(33, "no known service") + c(90, " (bridge: cannot tell)"))
        ext = it["external"]
        if ext and now - ext[0]["last"] < 900:
            who.append(c(31, "external clients now: " + ", ".join(safe(e["ip"]) for e in ext if now - e["last"] < 900)))
        elif ext:
            who.append(c(33, f"last external client {safe(ext[0]['ip'])} {fmt_ago(now - ext[0]['last'])} ago"))
        elif it["ext_source"] == "netns":
            who.append(c(90, f"no external client seen in {fmt_ago(now - dbs['since'])}"))
        else:
            who.append(c(33, "external clients: not detectable"))
        wrapped = wrap_items([clip(x, w - 14) for x in who], w, indent=8, sep="   ")  # wrapped, never silently cut
        lines.append("      " + c(90, "→") + wrapped[0][7:])
        lines += wrapped[1:]
    return lines


def ov_esposizione(net, cont, w, k, new=None):
    new = new or {}
    if k < 0 and net is not None and net.get("listeners") is not None:
        return exposure_block(net, cont, w, new)  # enough room: the full table
    lines = [section("EXPOSURE", w)]
    if net is None or net.get("listeners") is None:
        return lines + [msg("err", "unavailable")]
    rows = expose_apply(exposure_rows(net, cont), net, cont)
    by = {g: [r for r in rows if group_of(r) == g] for g, _ in GROUPS}
    warn = sum(r["warn"] for r in rows)
    lines.append(f" Internet {c('1;31', len(by['INTERNET'])) if by['INTERNET'] else 0}   LAN {len(by['LAN'])}   "
                 f"tailnet only {len(by['TAILNET'])}   local only {len(by['LOCALE'])}"
                 + (f"   {c(31, f'⚠ {warn} DB/broker on LAN')}" if warn else ""))
    for r in lim(by["INTERNET"], 2, "exposure"):
        tag = new.get(f"{r['port']}/{r['proto'][0]}:INTERNET")
        xn = expose_note(r)
        lines.append(f" {c('1;31', '●')} {c('1;31', tag + ' ') if tag else ''}{r['port']}/{r['proto'][0]} "
                     f"{r['name'][:40]}  {c(31, 'public on the Internet')}")
        if xn:  # [expose]: after the line when it fits, else below it
            mark = c(31 if xn[1] else 90, xn[0])
            lines[-1:] = [lines[-1] + "  " + mark] if vlen(lines[-1]) + 2 + vlen(mark) <= w else [lines[-1], "   " + mark]
    items = []
    for r in by["LAN"]:
        tag = new.get(f"{r['port']}/{r['proto'][0]}:LAN")
        label = f"{r['port']} {r['name'][:22]}"
        xn = expose_note(r)
        items.append((0 if tag or (xn and xn[1]) else 1, (c("1;31", tag + " ") if tag else "") + (c(31, "⚠" + label) if r["warn"] else label)
                      + (" " + c(31 if xn[1] else 90, xn[0]) if xn else "")))
    # new/changed items first: they must not end up behind the '… +N'
    lines += wrap_items([t for _, t in sorted(items, key=lambda x: x[0])], w, indent=1, max_lines=max(1, 3 - min(k, 2)), section="exposure")
    return lines


def ov_firewall(net, w, k):
    if k < 0 and net is not None:
        # enough room: with the rule list (capped at the intermediate level, most exposed first)
        return firewall_block(net, w, None if (k <= -2 or "firewall" in EXPAND) else 10)
    lines = [section("FIREWALL", w)]
    if net is None:
        return lines + [msg("err", "network collector not running")]
    lines += fw_status_lines(net)
    ipt, f2b, dr = net.get("iptables"), net.get("f2b"), net.get("drops")
    bits = []
    if os_of(net) != "linux" and net.get("firewall"):
        let_in = {(x["port"], x["proto"]) for x in net.get("listeners") or []
                  if (x.get("fw") or [""])[0] in ("open", "nofw") and bind_scope(x["addr"]) != "lo"}
        bits.append(f"{plural(len(let_in), 'listening port')} let in")
    if ipt:
        bits.append("INPUT " + ipt["policy"].get("INPUT", "?") + " · FORWARD " + ipt["policy"].get("FORWARD", "?"))
        bits.append(c(32, "ts-input ✔") if ipt["ts_input"] else c(33, "ts-input ?"))
    if f2b and not f2b.get("error"):
        bits.append("fail2ban " + " ".join(f"{safe(j['name'])}:{j['banned']}" for j in f2b["jails"]))
    if dr is not None:
        bits.append(f"drop 1h {dr['n']}")
    lines += wrap_items(bits, w, indent=3, sep="   ")
    return lines


def ov_boot(b, w, k, now=None):
    if k <= -2 and b is not None:  # the full boot detail only if there really is room to spare
        up = (now or time.time()) - b.get("btime", time.time())
        lines = boot_block_avvio(b, up, w)
        for key, blk in (("blame", lambda: boot_block_lente(b, w, 5)), ("journal", lambda: boot_block_journal(b, w, 4))):
            if not unsupported(b, key):  # macOS/Windows: no empty "not available" blocks
                lines += [""] + blk()
        return lines
    lines = [section("BOOT", w)]
    if b is None:
        return lines + [msg("warn", "boot collector not running")]
    an, j, failed, lbl = b.get("analyze"), b.get("journal"), b.get("failed"), boot_labels(b)
    bits = []
    if an:
        bits.append(f"finished in {c(1, fs(an['total']))}")
    if failed is not None:
        bits.append(c(31, f"✖ {plural(len(failed), lbl['failed_short'])}") if failed else c(32, f"✔ 0 {lbl['failed_short']}s"))
    if j:
        bits.append(f"{lbl['journal_short']} {c(31, str(j['err'])) if j['err'] else 0} err · {c(33, str(j['warn'])) if j['warn'] else 0} warn")
    lines.append(fit_join(bits, "   ", w, " "))
    if b.get("blame") and k < 3:
        top = [f"{x['unit'].replace('.service', '')} {fs(x['s'])}" for x in b["blame"][:max(1, 3 - k)]]
        lines.append(fit_join(top, "  ·  ", w, " slowest: ", c90=True))
    return lines


def _native_lines(builder, ctx, w, k):
    """The console lines of a card built of components (cards.py), for the callers that want the section as it was drawn: the card is
    built within the FULL / EXPAND / TRUNC globals and drawn by ansi.card_lines; an error is the caller's (safe_block's)."""
    card = builder(ctx, k, cards.Caps(w, FULL, EXPAND, TRUNC))
    lines, hid = ansi.card_lines(card, w)
    if hid:
        TRUNC.add(card.id)
    return lines


def ov_traffico(s, w, k):
    return _native_lines(cards.network_traffic_card, cards.Ctx(s=s, cfg=CFG), w, k)


def ov_sessioni(s, w, k):
    return _native_lines(cards.sessions_card, cards.Ctx(s=s, cfg=CFG), w, k)


def ov_tailscale(net, w, k):
    return _native_lines(cards.tailscale_card, cards.Ctx(net=net, cfg=CFG), w, k)


REACH_LABEL = {"INTERNET": "Internet", "LAN": "LAN+tailnet", "TAILNET": "tailnet", "LOCALE": "local only"}


def ov_webapp(net, cont, w, k):
    lines = [section("WEB APPS", w)]
    if net is None:
        return lines + [msg("err", "network collector not running")]
    rows = webapp_rows(net, cont)
    if net.get("listeners") is None and not rows:
        return lines + [unavail_msg(net, "listeners")]
    if not rows:
        return lines + [msg("info", "no web apps found (declare the ones you expect under [webapps] in config.ini)")]
    up, down = sum(r["state"] == "up" for r in rows), sum(r["state"] == "down" for r in rows)
    lines[0] = section("WEB APPS", w, f"{up} active" + (f" · {down} down (expected)" if down else ""))
    declared = bool(CFG["webapps"])
    nw = max(8, min(22, w - 47))
    cap = 12 if k <= 0 else 6 if k <= 2 else 4
    if FULL or "webapps" in EXPAND:
        cap = len(rows)
    elif len(rows) > cap:
        TRUNC.add("webapps")
    for r in rows[:cap]:
        ports = ",".join(str(p) for p in r["ports"][:3]) + ("…" if len(r["ports"]) > 3 else "")
        if r["state"] == "down":
            mark, reach, flag = c(33, "○"), c(90, "not listening"), c(33, "DOWN (expected)")
        else:
            mark = c(32, "●") if r["expected"] or not declared else c(33, "●")
            col = {"INTERNET": 31, "LAN": 33, "TAILNET": 0, "LOCALE": 90}.get(r["reach"], 0)
            reach = c(col, REACH_LABEL.get(r["reach"], "?")) if col else REACH_LABEL.get(r["reach"], "?")
            flag = c(90, "not declared") if declared and not r["expected"] else ""
        lines.append(f" {mark} {pad(safe(r['name'])[:nw], nw + 1)}{pad(ports, 13)}{pad(reach, 14)}{flag}")
    if len(rows) > cap:
        lines.append(c(90, f" … +{len(rows) - cap} more"))
    return lines


def ov_docker(boot, w, k):
    return _native_lines(cards.docker_disk_card, cards.Ctx(boot=boot, cfg=CFG), w, k)


def ov_dischi(s, w, k):
    return _native_lines(cards.disks_card, cards.Ctx(s=s, cfg=CFG), w, k)


def safe_block(fn, title, width, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001 - a broken block must not empty the screen
        return [section(title, width), msg("err", safe(repr(e))[:60])]


def ov_attention(pb, bw, k):
    shown = lim(pb, max(3, 6 - k), "attention")
    rows = [x for sev, t in shown for x in msg_wrap("err" if sev >= 2 else "warn", t, bw)]
    extra = [c(90, f"   … +{len(pb) - len(shown)} more")] if len(pb) > len(shown) else []
    acc = getattr(pb, "accepted", 0)
    known = [c(90, f"   · {acc} accepted as known ({PROBLEMS_CMD})")] if acc else []
    return [section("ATTENTION", bw)] + (rows + extra if pb else [msg("ok", "no problems detected")]) + known


# The overview's sections as cards (cards.py): each builder returns the card with the lines this file has always drawn for it (ui.Raw)
# and the state its problems give it, so the console is what it was while a section is rebuilt out of components.
# (id, title, feature, lines(ctx, k, caps)); the width a card is drawn in is caps.width
OV_CARDS = (
    ("attention", "ATTENTION", True, lambda x, k, cp: ov_attention(x.problems, cp.width, k)),
    ("exposure", "EXPOSURE", "exposure", lambda x, k, cp: safe_block(ov_esposizione, "EXPOSURE", cp.width, x.net, x.cont, cp.width, k, x.new)),
    ("webapps", "WEB APPS", "webapps", lambda x, k, cp: safe_block(ov_webapp, "WEB APPS", cp.width, x.net, x.cont, cp.width, k)),
    ("firewall", "FIREWALL", "firewall", lambda x, k, cp: safe_block(ov_firewall, "FIREWALL", cp.width, x.net, cp.width, k)),
    ("system", "SYSTEM", True, lambda x, k, cp: safe_block(ov_sistema, "SYSTEM", cp.width, x.s, cp.width, k, x.cont)),
    ("containers", "CONTAINER", "containers", lambda x, k, cp: safe_block(ov_container, "CONTAINER", cp.width, x.cont, cp.width, k)),
    ("databases", "DATABASE", "databases", lambda x, k, cp: safe_block(ov_database, "DATABASE", cp.width, x.net, x.cont, cp.width, k)),
    ("boot", "BOOT", "boot", lambda x, k, cp: safe_block(ov_boot, "BOOT", cp.width, x.boot, cp.width, k)),
    ("network_traffic", None, None, None),  # the cards built of components (cards.NATIVE): their builders are in cards.py
    ("sessions", None, None, None),
    ("tailscale", None, None, None),
    ("docker_disk", None, None, None),
    ("disks", None, None, None),
)


def _overview_builder(id, lines):
    return lambda x, k, cp: cards.raw_card(id, x, lines(x, k, cp))


for _id, _title, _feature, _lines in OV_CARDS:
    if _id in cards.NATIVE:
        cards.register(_id, *cards.NATIVE[_id][:2], cards.NATIVE[_id][2])
    else:
        cards.register(_id, _title, _feature, _overview_builder(_id, _lines))


def pack(blocks, ncol, cw, w, body_h, gap):
    """Fills the columns in the given order, left to right, top to bottom: a block goes in the current column, or in the
    next one if it does not fit; earlier columns are never back-filled, so the order on screen is the order requested
    (first-fit used to move sections around whenever a line more or less changed). None if one does not fit anywhere."""
    cols = [[] for _ in range(ncol)]
    ci = 0
    for fn in blocks:
        lines = fn(cw)
        if gap and CFG["spacing"] and len(lines) > 1 and lines[1] != "":  # a little air under each section title
            lines = [lines[0], ""] + lines[1:]
        while ci < ncol:
            col = cols[ci]
            need = len(lines) + (len(gap) if col else 0)
            if len(col) + need <= body_h:
                col.extend((gap if col else []) + lines)
                break
            ci += 1
        else:
            return None
    return columns([(col, cw) for col in cols], w, gap=3) if ncol > 1 else cols[0]


def page_overview(s, cont, net, boot, w, body_h, pb=None, baseline=False, now=None, details=None, scroll=False):
    """Everything on one screen. If it does not fit, details shrink (k = 0..3); at the last level no empty lines.

    `details`: pass a list to receive the detail pages (sections that hid items, shown in full), see slides().
    `scroll`: a browser page that scrolls (body_h is ignored): every section and every item at the richest level, nothing cut,
    in columns as even as possible. A bigger text (fewer columns) then means a longer page, never less content."""
    global FULL
    pb = safe_problems(net, cont, now, boot=boot, thermal=s.get("thermal"), baseline=baseline) if pb is None else pb
    new = new_ports(net, cont, baseline)

    ctx = cards.Ctx(s=s, cont=cont, net=net, boot=boot, problems=pb, cfg=CFG, now=now, baseline=baseline, new=new)  # this frame's data and memo
    caps_at = lambda width: cards.Caps(width, FULL, EXPAND, TRUNC)  # noqa: E731 - the globals, seen as the Caps the registry asks for

    ncol = 3 if w >= NCOL3 else 2 if w >= WIDE else 1
    cw = (w - 3 * (ncol - 1)) // ncol

    def card_block(n, k, c_):
        """The lines of card n at level k in a column c_ wide: its title and body drawn by ansi.card_lines (a Raw card's lines as they are),
        remembered for the frame by what changes them; a card that hid items tells the Details pages."""
        caps = caps_at(c_)
        card = cards.build(n, ctx, k, caps)
        lines, hid = ctx.once(("lines", n, k) + caps.key(n), lambda: ansi.card_lines(card, c_))
        if hid:
            TRUNC.add(n)
        return list(lines)

    def make_cand(k):
        """(card id, block) per section at detail level k: a section switched off in config.ini does not appear. The card comes from the
        registry (cards.build), which remembers it for this frame: the levels and expand() ask for the same card again and again."""
        have = {"attention", "exposure", "firewall", "system", "containers"}
        if k < 4:
            have |= {"databases", "boot", "webapps"}
        if k <= 3 and (w >= WIDE or scroll):  # wide consoles (or a page that scrolls): the detail sections stay at every level
            have |= {"network_traffic", "sessions", "tailscale", "docker_disk", "disks"}
        # the order is fixed (config.ini [dashboard] sections), never decided by which block happens to fit where
        return [(n, lambda c_, n=n: card_block(n, k, c_)) for n in CFG["sections"]
                if n in have and cards.enabled(n, CFG)]

    def detail_pages(trunc):
        """The sections that hid items ("… +N more"), built in full at the richest level and laid out page by page."""
        global FULL
        FULL = True
        try:
            blocks = [fn(cw) for n, fn in make_cand(-2) if n in trunc]
        finally:
            FULL = False
        pages, cols, ci = [], [[] for _ in range(ncol)], 0
        flush = lambda: pages.append(columns([(col, cw) for col in cols], w, gap=3) if ncol > 1 else list(cols[0]))
        for lines in blocks:
            for chunk in [lines[i:i + body_h] for i in range(0, len(lines), body_h)]:
                while True:
                    col = cols[ci]
                    if len(col) + len(chunk) + (1 if col else 0) <= body_h:
                        col.extend(([""] if col else []) + chunk)
                        break
                    if ci + 1 < ncol:
                        ci += 1
                    else:
                        flush()
                        cols, ci = [[] for _ in range(ncol)], 0
        if any(cols):
            flush()
        return pages

    if scroll:
        FULL = True
        try:
            pre = [fn(cw) for _, fn in make_cand(-2)]
        finally:
            FULL = False
        blocks = [(lambda c_, lines=lines: lines) for lines in pre]
        # the shortest column height that holds every section in the fixed order: the columns come out even
        lo, hi = max(len(x) + 2 for x in pre), sum(len(x) + 2 for x in pre)
        while lo < hi:
            mid = (lo + hi) // 2
            if pack(blocks, ncol, cw, w, mid, [""]) is None:
                lo = mid + 1
            else:
                hi = mid
        return pack(blocks, ncol, cw, w, hi, [""])

    # first detail is removed keeping the empty lines between blocks; only at the very end are those removed too
    # from the richest (k=-2, full tables) to the most compact; on very small consoles the last level drops BOOT and DATABASE
    levels = ((-2, True), (-1, True), (0, True), (1, True), (2, True), (3, True), (3, False), (4, False))
    for i, (k, spaced) in enumerate(levels):
        TRUNC.clear()  # only what the level that is finally shown hides counts
        blocks = [fn for _, fn in make_cand(k)]
        last = i == len(levels) - 1
        lines = pack(blocks, ncol, cw, w, 10 ** 6 if last else body_h, [""] if spaced else [])  # last level: no limit (the page splits)
        if lines is not None:
            if not last:
                # the caps ("… +N more") are not about space: lift each one if the layout still fits, so a free corner of the
                # screen is used before anything is pushed to the Details pages. Section order = priority. If something stays
                # cut, try once more without the air under the titles: complete content beats spacing.
                def expand(first_lines):
                    ls = first_lines
                    try:
                        for name in [n for n, _ in make_cand(k) if n in set(TRUNC)]:
                            EXPAND.add(name)
                            TRUNC.clear()
                            try_lines = pack([fn for _, fn in make_cand(k)], ncol, cw, w, body_h, [""] if spaced else [])
                            if try_lines is None:
                                EXPAND.discard(name)
                            else:
                                ls = try_lines
                        TRUNC.clear()
                        ls = pack([fn for _, fn in make_cand(k)], ncol, cw, w, body_h, [""] if spaced else []) or ls
                        return ls, set(TRUNC)
                    finally:
                        EXPAND.clear()
                        TRUNC.clear()
                lines, left = expand(lines)
                if left and CFG["spacing"]:
                    saved_spacing = CFG["spacing"]
                    CFG["spacing"] = 0
                    try:
                        TRUNC.clear()
                        tight = pack([fn for _, fn in make_cand(k)], ncol, cw, w, body_h, [""] if spaced else [])
                        tight_lines, tight_left = expand(tight) if tight is not None else (None, left)
                    finally:
                        CFG["spacing"] = saved_spacing
                    if tight_lines is not None and len(tight_left) < len(left):
                        lines, left = tight_lines, tight_left
                TRUNC.clear()
                TRUNC.update(left)
            if details is not None and CFG["details"] and TRUNC:
                details.extend(detail_pages(set(TRUNC)))
            return lines  # the last level has a 10**6 limit: we always return here


def slides(s, cont, net, w, body_h, boot=None, baseline=False, mode=None, scroll=False, cpu_feed=None, cpu_lazy=False):
    """Each page is split into chunks body_h tall: (page name, index, total, lines). scroll: one page, any height, nothing cut.
    cpu_feed: where the CPU slide ([dashboard] cpu_in_rotation) reads; cpu_lazy: leave it empty (see fill_cpu)."""
    det = []
    if scroll:
        return [("Overview", 1, 1, page_overview(s, cont, net, boot, w, body_h, baseline=baseline, scroll=True))]
    if (mode or MODE) == "overview":
        pages = (("Overview", lambda: page_overview(s, cont, net, boot, w, body_h, baseline=baseline, details=det)),)
    else:
        every = {"System": lambda: page_sistema(s, w, cont),
                 "Network & firewall": lambda: page_rete(net, cont, w, baseline=baseline),
                 "Boot": lambda: page_boot(boot, w, body_h)}
        pages = tuple((n, every[n]) for n in PAGES)
    out = []
    for name, fn in pages:
        try:
            lines = fn()
        except Exception as e:  # a broken page must not bring the process down (crash-loop = black tty1)
            lines = [c(31, f" error on page {name}: {safe(repr(e))[:w - 20]}")]
        chunks = [lines[i:i + body_h] for i in range(0, len(lines), body_h)] or [[]]
        out += [(name, i + 1, len(chunks), ch) for i, ch in enumerate(chunks)]
    out += [("Details", i + 1, len(det), pg) for i, pg in enumerate(det)]  # full content of what the overview cut ("… +N more")
    if CFG.get("map_in_rotation") and on("map"):  # a monitor without a keyboard sees the Map too, opened as far as it fits
        try:
            lines = map_slide(cont, net, boot, baseline, w, body_h)
        except Exception as e:  # noqa: BLE001 - same rule as the pages above
            lines = [c(31, f" error on page Map: {safe(repr(e))[:w - 20]}")]
        out.append(("Map", 1, 1, lines))
    if CFG.get("cpu_in_rotation") and on("cpu"):  # and the CPU: the processor and the top processes, for a monitor without a keyboard
        try:
            lines = [c(90, " CPU: shown when its turn comes")] if cpu_lazy else cpu_slide(cpu_feed or CpuFeed(), w, body_h)
        except Exception as e:  # noqa: BLE001 - same rule as the pages above
            lines = [c(31, f" error on page CPU: {safe(repr(e))[:w - 20]}")]
        out.append(("CPU", 1, 1, lines))
    if CFG.get("health_in_rotation") and on("health"):  # likewise the Health screen: the findings that fit and the top apps
        try:
            lines = health_slide(w, body_h)
        except Exception as e:  # noqa: BLE001 - same rule as the pages above
            lines = [c(31, f" error on page Health: {safe(repr(e))[:w - 20]}")]
        out.append(("Health", 1, 1, lines))
    return out


def slide_seconds(slide, n):
    """How long a slide stays: the overview longer when it is followed by detail pages (nobody can press a key on that monitor)."""
    return CFG["overview_seconds"] if slide[0] == "Overview" and n > 1 else ROTATE_S


def pick_slide(sl, t):
    """Index of the slide shown t seconds after the start, cycling through all of them."""
    durs = [slide_seconds(x, len(sl)) for x in sl]
    t %= sum(durs)
    for i, d in enumerate(durs):
        if t < d:
            return i
        t -= d
    return 0


PAUSED = False  # Z: the redraw is paused, the header says so


def frame(slide, idx, n, w, h, pb=None, keys=True, hint="", page=False, mapkey=None, foot=None, cpukey=None, healthkey=None, aikey=None):
    """page=True: a browser page, where all w columns are usable (the Linux console needs w = its width - 1).
    mapkey / cpukey / healthkey / aikey: say that `2` opens the Map, `3` the CPU screen, `4` the Health screen, `5` the AI screen
    (default: when keys are); foot: a footer of its own, else the overview's, made from ui.KEYMAP."""
    name, part, parts, body = slide
    text, code = status_pill(pb or [])
    shift = " " * (int(time.time() // 600) % 3)  # every 10 min shift the header
    tail = f" │ {name}" + (f" {part}/{parts}" if parts > 1 else "") + f" │ {time.strftime('%H:%M:%S')}" + (" │ paused" if PAUSED else "")
    host, room = socket.gethostname(), w - len(text) - 2 - len(shift) - 1 - len(tail)
    if len(host) > room:  # a long host name (macOS: 'xyz-…-ABCD.local') must never push the status off the screen
        host = host[:max(room - 1, 1)] + "…"
    left = shift + f" {host}" + tail
    head = c(code, pad(left, max(len(left), w - len(text) - 2)) + text + "  ")
    head = clip(head, w)
    size = f"{w}x{h}" if page else f"{w + 1}x{h}"
    if foot is None:
        flags = {"map": mapkey, "cpu": cpukey, "health": healthkey, "ai": aikey}
        shown = lambda f: bool(keys if flags.get(f) is None else flags[f]) and on(f)  # noqa: E731
        foot = c(90, overview_footer(name, idx, n, w, size, keys, hint, shown))
    foot = clip(foot, w)
    rows = [head] + [clip(x, w) for x in body]
    rows += [""] * (h - 1 - len(rows)) + [foot]
    return "\x1b[K\r\n".join(rows[:h])  # \x1b[K: clears what is left of the previous frame


def overview_footer(name, idx, n, w, size, keys, hint, shown):
    """The footer of the rotating pages, plain text: where we are, the keys of the overview (ui.KEYMAP), then the console size and the
    hint, which go first when it is narrow. keys=False (a browser page): no keys. shown(feature): is that screen's digit offered?"""
    lead = " single screen   " if n == 1 else f" screen {idx + 1}/{n}   "
    items = []
    if keys or any(shown(f) for f in ("map", "cpu", "health", "ai")):
        items = ui.footer_items("overview", shown, dyn={"back": ("q: quit", None) if nuc_config.PORTABLE else None,  # Esc does nothing here
                                                        "slide-prev": None if n == 1 else ("←→: slide", None)})
    if n > 1 and name == "Details":
        items.append((97, "details: everything the overview cut ('… +N more')", ""))
    items.append((99, f"console {size}", None))
    if hint:
        items.append((98, hint, ""))
    return ui.fit(lead, items, w)


def first_slide_of(sl, page_idx):
    return next((i for i, s in enumerate(sl) if s[0] == PAGES[page_idx]), 0)


DEMO = False  # --demo: synthetic data (src/demo.py) instead of the real state
DEMO_OS = None  # --demo-os windows|darwin: the demo as the macOS/Windows collector would write it


def snapshot(w):
    """State read from disk, as the main loop sees it."""
    if DEMO:
        import demo
        cont, net, boot, base = demo.snapshot(os_name=DEMO_OS)
        return dict(cont=cont, net=net, boot=boot, baseline=base)
    return dict(cont=load_containers(), net=load_json(NET_STATE), boot=load_json(BOOT_STATE), baseline=load_baseline())


def host_sample(smp):
    """The Sampler's reading for a screen. Under --demo it is the demo machine's (demo.sampler_data), whatever sampler is given (the web view
    always has a real one) and without calling it: nothing of the machine running the demo is read or shown. Without a sampler: no figures
    ({"thermal": {}}: the screens that only judge the header's problems)."""
    if DEMO:
        import demo
        return demo.sampler_data(DEMO_OS)
    return smp.sample() if smp else {"thermal": {}}


def demo_defaults():
    """--demo: the host name and the [webapps] the screenshots show (one up, one expected-but-down)."""
    socket.gethostname = lambda: "demo-host"
    if not CFG["webapps"]:
        CFG["webapps"] = {"shop-web": [8080], "admin-console": [9443]}
    if not CFG["expose"]:  # one service within its reach, two beyond it (the Funnel's backend is the process 'node': a unit on Linux only)
        CFG["expose"] = {"shop-web": "LAN", "shop-db": "LOCALE", ("n8n" if DEMO_OS in (None, "linux") else "node"): "TAILNET"}


def map_graph(smp=None):
    """(MAP graph, header problems) of the current state: shared by the console's Map screen and the web view's map page."""
    st = snapshot(0)
    sm = host_sample(smp)
    if DEMO:
        demo_defaults()
    G = graph.build(st["cont"], st["net"], st["boot"], CFG["webapps"], baseline=st["baseline"], expose=CFG["expose"])
    return G, safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


# ---- MAP screen: graph.py's tree drawn on the console, moved through with the keyboard ------------------------------------

MAP_PANE_W = 140  # from this width up the details pane sits beside the tree, below it otherwise
MAP_IDLE_S = 600  # the Map left alone this long gives the monitor back to the rotation (nobody may be at the keyboard)
ST_COL = {"err": "31", "down": "31", "warn": "33", "unknown": "33", "ok": "32", "info": ""}
LV_COL = {"err": "31", "warn": "33", "ok": "32", "info": "90"}
EV_COL = {"seen": "1", "declared": "", "possible": "90", "bind": "90"}  # how sure the link is: bright, normal, dim


class MapView(object):
    """The interactive Map: open branches (graph.State), the selected row (its key survives refreshes; its index is where
    the cursor stays when that row vanishes), the scroll position, the details pane, when it was opened and last touched."""

    def __init__(self, now=None):
        self.st, self.cur, self.idx, self.top, self.details = graph.State(), None, 0, 0, False
        self.opened = self.touched = now or time.time()


def map_sync(mv, rs):
    """The cursor back on its row after the rows changed: by key, else the same index (clamped). Returns the index."""
    i = graph.find(rs, mv.cur) if mv.cur else None
    mv.idx = i if i is not None else max(0, min(mv.idx, len(rs) - 1))
    mv.cur = rs[mv.idx]["key"] if rs else None
    return mv.idx


def map_parent(rs, i):
    d = rs[i]["depth"]
    return next((j for j in range(i - 1, -1, -1) if rs[j]["depth"] < d), i)


def map_key(mv, key, rs, page=10):
    """One key in the Map, on the rows rs drawn from mv.st (what each key does: ui.KEYMAP, scope map). Returns 'back' (leave the Map: Esc or q
    when no details pane is open), 'rows' (branches opened or closed: rebuild the rows, then map_sync) or '' (only the cursor or the
    details pane changed). The keys of every screen (digits, Tab, ?) are the dispatcher's."""
    act = ui.action("map", key)
    if act == "back":
        if mv.details:  # Esc closes the details pane first
            mv.details = False
            return ""
        return "back"
    if act == "details":
        mv.details = not mv.details
        return ""
    if act in ("expand", "problems"):
        if act == "expand":
            mv.st.expand_all()
        else:
            mv.st.only = not mv.st.only
        return "rows"
    if not rs or act not in ("move", "page", "open", "close", "collapse"):
        return ""
    i = map_sync(mv, rs)
    row = rs[i]
    if act == "collapse":  # every branch closes: the cursor goes up to its root, which stays
        i = next((j for j in range(i, -1, -1) if rs[j]["depth"] == 0), i)
        mv.idx, mv.cur = i, rs[i]["key"]
        mv.st.collapse_all()
        return "rows"
    if act in ("open", "close") and (row["open"] if act == "close" else row["kids"] and not row["open"]):
        mv.st.toggle(row)
        return "rows"
    i = {"up": i - 1, "k": i - 1, "down": i + 1, "j": i + 1, "pgup": i - page, "pgdn": i + page, "home": 0, "end": len(rs) - 1,
         "right": i + 1 if row["open"] else i, "l": i + 1 if row["open"] else i,  # already open: down to its first child
         "left": map_parent(rs, i), "h": map_parent(rs, i)}.get(key, i)  # closed or a leaf: up to its parent
    mv.idx = max(0, min(i, len(rs) - 1))
    mv.cur = rs[mv.idx]["key"]
    return ""


def map_layout(G, w, h, details=False):
    """(notes shown, tree rows, details rows, details beside the tree?) of a Map body h lines tall."""
    notes = min(len(G["notes"]), max(0, (h - 4) // 4))
    avail = max(1, h - 1 - notes)
    if not details or avail < 6:
        return notes, avail, 0, False
    if w >= MAP_PANE_W:
        return notes, avail, avail, True
    return notes, avail - avail // 2, avail // 2, False


def map_scroll(top, i, n, rows, margin=2):
    """First tree row shown, so that row i stays in sight, a few rows from the edges when there is room."""
    m = min(margin, max(0, (rows - 1) // 2))
    top = max(min(top, i - m), i + m + 1 - rows)
    return max(0, min(top, n - rows))


def map_title(G, w, only=False):
    """'── MAP  27 nodes · 14 links · ✖ 7 problems ───── ━━► seen  ╌╌► declared …': the legend goes first when narrow."""
    k = graph.counts(G)
    probs = (c(31, f"✖ {k['problems']} need" + ("s" if k["problems"] == 1 else "") + " attention") if k["problems"]
             else c(90, "none needs attention"))  # not a green: missing data also draws nothing
    left = (c(36, "──") + c("1;36", " MAP ") + " " + c(90, f"{plural(k['nodes'], 'node')} · {plural(k['edges'], 'link')} · ") + probs
            + (c("1;33", "  problems only") if only else "") + " ")
    legend = []
    for item in graph.LEGEND.split("  "):
        glyph, _, word = item.partition(" ")
        ev = "seen" if "━" in glyph else "declared" if "╌" in glyph else "possible" if "┄" in glyph else "bind"
        legend.append(cc(EV_COL[ev], glyph) + " " + c(90, word))
    while legend and vlen(left) + 4 + vlen("  ".join(legend)) > w:
        legend.pop()
    right = "  ".join(legend)
    return clip(left + c(36, "─" * max(0, w - vlen(left) - vlen(right) - (1 if right else 0))) + (" " + right if right else ""), w)


def map_row(G, row):
    """One tree row: tree (dim), toggle, arrow (by evidence), label (by state), owner of a port, port used, sub, note."""
    p, n = graph.parts(G, row), G["nodes"].get(row["node"]) or {}
    st, label = p["state"], safe(p["label"])
    s = (c(90, safe(p["tree"])) if p["tree"] else "") + c("36" if p["toggle"] in ("▸", "▾") else "90", safe(p["toggle"])) + " "
    if p["arrow"]:
        s += cc(EV_COL.get(p["ev"], ""), safe(p["arrow"])) + " "
    if not row["depth"]:
        s += c("1;" + {"err": "31", "down": "31", "warn": "33"}.get(st, "36"), label)
    else:  # a symbol too: colour alone is not enough
        s += cc(ST_COL.get(st, ""), {"down": "✖ ", "unknown": "? "}.get(st, "") + label)
    if p["kind"] == "port":
        s += "  " + (c(33, "?") if p["owner"] in ("", "?") else c(1, safe(p["owner"])))
    if p["port"]:
        s += " " + safe(p["port"])
    if p["sub"]:
        s += "  " + c(90, safe(p["sub"]))
    if p["note"]:
        lv = next((lv for lv, t in n.get("findings") or [] if t == p["note"]), "")
        s += "  " + c({"err": "31", "warn": "33"}.get(lv, "90"), safe(p["note"]))
    return s


def map_pane(G, nid, w, h):
    """Everything known about a node, w columns, h lines at most: long values wrap, what does not fit is counted."""
    items = graph.details(G, nid)
    lw = min(max([len(safe(x[0])) for x in items] + [4]) + 2, 18, max(6, w // 3))
    out = [section("DETAILS", w)]
    for i, (label, value, level) in enumerate(items):
        label, col = safe(label), LV_COL.get(level, "")
        if i == 0:
            col = "1;" + col if col else "1"
        head = cc(col if label.strip() in ("!", "·") else "90", pad(label[:lw - 1], lw))
        chunks = textwrap.wrap(safe(value), max(8, w - lw), break_on_hyphens=False) or [""]
        out += [(head if j == 0 else " " * lw) + cc(col, x) for j, x in enumerate(chunks)]
    if len(out) > h:
        out = out[:max(0, h - 1)] + [c(90, f" … +{len(out) - h + 1} more lines")]
    return out[:h]


def map_view(G, rs, w, h, cursor_key=None, details=None, top=None, only=False):
    """(the Map body: at most h lines, none wider than w; the first tree row shown). details: True = the selected row's
    node, or a node id. Without a cursor (the rotation slide) what does not fit is counted on the last line."""
    notes, tree_h, pane_h, side = map_layout(G, w, h, bool(details))
    out = [map_title(G, w, only)]
    nl = [safe(x) for x in G["notes"]]
    if len(nl) > notes:
        nl = nl[:max(0, notes - 1)] + ([f"… +{len(nl) - notes + 1} more notes"] if notes else [])
    out += [clip(c(90, "  · " + x), w) for x in nl]
    i = graph.find(rs, cursor_key) if cursor_key else None
    tw = w - (int(w * 0.42) + 3 if side else 0)
    if not rs:
        tree, top = [msg("info", "no problem on any path: p shows every path" if only else "nothing to draw: see the notes above")], 0
    elif cursor_key is None and len(rs) > tree_h:
        tree, top = [clip(map_row(G, r), tw) for r in rs[:tree_h - 1]] + [c(90, f"   … +{len(rs) - tree_h + 1} more rows")], 0
    else:
        top = map_scroll(top or 0, i or 0, len(rs), tree_h)
        tree = [c(7, pad(ANSI.sub("", clip(map_row(G, rs[j]), tw)), tw)) if j == i else clip(map_row(G, rs[j]), tw)
                for j in range(top, min(len(rs), top + tree_h))]
    nid = details if isinstance(details, str) else rs[i if i is not None else 0]["node"] if rs else None
    if pane_h and nid:
        tree += [""] * (tree_h - len(tree))
        if side:
            pane = map_pane(G, nid, w - tw - 3, pane_h)
            tree = [pad(t, tw) + c(90, " │ ") + (pane[k] if k < len(pane) else "") for k, t in enumerate(tree)]
        else:
            tree += map_pane(G, nid, w, pane_h)
    return [clip(x, w) for x in (out + tree)[:h]], top


def map_lines(G, rs, w, h, cursor_key=None, details=None, top=None, only=False):
    """The Map body as ANSI lines (title and legend, notes, tree, details pane): the console, --once, the rotation slide."""
    return map_view(G, rs, w, h, cursor_key, details, top, only)[0]


def map_footer(mv, n, w, truncated=False):
    """Where the cursor is, and the keys (ui.KEYMAP): in short words when the screen is narrow, then the least needed go first."""
    hide, only = mv.details, mv.st.only
    pos = f"{mv.idx + 1}/{n}{'+' if truncated else ''}" if n else "0/0"
    dyn = {"details": ("Enter: " + ("hide details" if hide else "details"), "Enter: " + ("hide" if hide else "details")),
           "problems": ("p: " + ("all paths" if only else "problems only"), "p: " + ("all" if only else "problems"))}
    return clip(c(90, ui.footer("map", f" row {pos}   ", w, on, dyn)), w)


def map_screen(G, pb, mv, w, h):
    """(the interactive Map as one frame: header, body, key help; the rows it shows): the live loop and --once."""
    rs = graph.rows(G, mv.st)
    map_sync(mv, rs)
    body, mv.top = map_view(G, rs, w, h - 2, mv.cur, mv.details, mv.top, mv.st.only)
    return frame(("Map", 1, 1, body), 0, 1, w, h, pb, foot=map_footer(mv, len(rs), w, getattr(rs, "truncated", False))), rs


def map_slide(cont, net, boot, baseline, w, body_h):
    """The Map among the rotating pages ([dashboard] map_in_rotation): no cursor, opened level by level while it fits."""
    G = graph.build(cont, net, boot, CFG["webapps"], baseline=baseline, expose=CFG["expose"])
    return map_lines(G, graph.rows(G, graph.State(open=graph.fit_open(G, map_layout(G, w, body_h)[1]))), w, body_h)


def map_select(G, mv, text):
    """The cursor on the first row whose name (or port owner) contains text, the branches above it opened. False: none."""
    t = text.lower()
    for rs in (graph.rows(G, mv.st), graph.rows(G, graph.State(all=True, only=mv.st.only))):
        i = next((j for j, r in enumerate(rs) if t in (G["nodes"][r["node"]]["label"] + " " + graph.parts(G, r)["owner"]).lower()), None)
        if i is None:
            continue
        d = rs[i]["depth"]
        for r in reversed(rs[:i]):  # its ancestors: the nearest row above it at each smaller depth
            if r["depth"] < d:
                d = r["depth"]
                mv.st.shut.discard(r["key"])
                if d:
                    mv.st.open.add(r["key"])
        mv.cur = rs[i]["key"]
        return True
    return False


def map_once(argv, w, h):
    """`--once --view map`: the Map screen as the console draws it (tests, README screenshots).
    --expand all: every branch open (e); --expand fit: opened level by level while it fits the screen, as the rotation
    slide does; --expand N: the same for at most N tree rows; none: only the roots open, as when `m` opens it.
    --select TEXT: the cursor on the first row whose name contains TEXT (any case), opening the branches above it;
    --details: the details pane of the selected row (Enter); --only: problems only (p)."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    G, pb = map_graph(None if DEMO else Sampler())
    mv = MapView()
    mv.st.only, mv.details = "--only" in argv, "--details" in argv
    exp = opt("--expand")
    if exp == "all":
        mv.st.expand_all()
    elif exp == "fit" or exp.isdigit():
        mv.st.open = graph.fit_open(G, int(exp) if exp.isdigit() else map_layout(G, w, h - 2, mv.details)[1])
    if opt("--select"):
        map_select(G, mv, opt("--select"))
    return map_screen(G, pb, mv, w, h)[0]


# ---- CPU screen: the processor, every logical CPU, the temperatures and the processes (htop, plus temperatures) ------------
# Data: cpuinfo.CpuSampler and procs.ProcSampler (the contract is in docs/DESIGN.md), plus sensors.json on macOS/Windows.
# Everything a producer hands over is data: numbers go through num(), text through safe(), a missing value is drawn as "?".

SENSORS = os.environ.get("NUC_CONSOLE_SENSORS", os.path.join(nuc_config.RUN_DIR, "sensors.json"))  # written by the macOS/Windows collector
SENSORS_STALE_S = 60  # an older sensors.json is not "now": it is ignored, and the screen says so
CPU_SORTS = ("cpu", "mem", "time", "pid", "user")
CPU_SORT_KEYS = {"p": "cpu", "m": "mem", "t": "time", "n": "pid", "u": "user"}  # htop's letters
CPU_SORT_NAME = {"cpu": "CPU%", "mem": "memory", "time": "CPU time", "pid": "PID", "user": "user"}
CPU_SORT_SHORT = {"cpu": "cpu", "mem": "mem", "time": "time", "pid": "pid", "user": "user"}  # what the footer calls them
CPU_PANE_W = 140  # from this width up the process details sit beside the table, below it otherwise
CPU_IDLE_S = MAP_IDLE_S  # left alone this long, the CPU screen gives the monitor back to the rotation
CPU_STATES = {"R": "running", "S": "sleeping", "D": "uninterruptible (disk) wait", "Z": "zombie: exited, not yet collected",
              "T": "stopped", "I": "idle", "U": "uninterruptible wait", "X": "dead"}
CPU_PRESSURE = {"nominal": "32", "moderate": "33", "heavy": "31", "trapping": "31", "sleeping": "90"}  # macOS thermal pressure
CPU_CELL_MIN_BAR, CPU_CELL_MAX_BAR = 10, 32


def idict(d):
    """{int: value} of a dict whose keys may be digits or strings (JSON keys are always strings); other keys are dropped."""
    out = {}
    for k, v in (d.items() if isinstance(d, dict) else ()):
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            pass
    return out


def dget(d, *keys):
    """d[k1][k2]... or None when any step is missing or not a dict."""
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def dd(x):
    """x when it is a dict, else an empty one: a producer's section that is not what the contract says is a section with nothing in it."""
    return x if isinstance(x, dict) else {}


def cpu_os():
    """Whose machine the CPU screen describes: the demo's OS under --demo, else this one."""
    if DEMO:
        return DEMO_OS if DEMO_OS in ("windows", "darwin") else "linux"
    return "windows" if WINDOWS else "darwin" if MACOS else "linux"


def cpu_merge(cpu, sens, now):
    """(cpu, extra): the sampler's data with the collector's temperatures filled in where the sampler had none (macOS/Windows; on
    Linux the sampler reads sysfs itself), and extra = {pressure, clusters, notes: [(level, text)]} from sensors.json. A sensors.json
    older than SENSORS_STALE_S is ignored with a note: its numbers are not the temperatures of now."""
    cpu = dict(cpu)
    temps = dict(dd(cpu.get("temps")))
    temps["cores"] = {k: v for k, v in idict(temps.get("cores")).items() if num(v) is not None}
    extra = {"pressure": None, "clusters": [], "notes": []}
    if cpu_os() != "linux":
        ts = num(dget(sens, "ts"))
        if not isinstance(sens, dict):
            extra["notes"].append(("warn", "temperatures: ? (no sensors.json: the collector is not running, or [features] cpu = no)"))
        elif ts is None or now - ts > SENSORS_STALE_S:
            age = "" if ts is None else f" ({fmt_ago(now - ts)} old)"
            extra["notes"].append(("warn", f"temperatures: ? (sensors.json is stale{age}: the collector stopped writing it)"))
        else:
            sc = dd(sens.get("cpu"))
            if num(temps.get("package")) is None and num(sc.get("package")) is not None:
                temps["package"] = float(sc["package"])
            for k, v in idict(sc.get("cores")).items():
                if num(v) is not None and k not in temps["cores"]:
                    temps["cores"][k] = float(v)
            if not temps.get("sensors"):
                temps["sensors"] = [{"label": x.get("label"), "c": num(x.get("c")), "high": None, "crit": None}
                                    for x in sc.get("sensors") or [] if isinstance(x, dict) and num(x.get("c")) is not None]
            if not temps.get("source") and isinstance(sc.get("source"), str):
                temps["source"] = sc["source"]
            if isinstance(sc.get("pressure"), str):
                extra["pressure"] = sc["pressure"]
            extra["clusters"] = [x for x in sc.get("clusters") or [] if isinstance(x, dict)]
            errors = dd(sens.get("errors"))
            extra["notes"] += [("info", f"{k}: {v}") for k, v in list(errors.items())[:2]]
    cpu["temps"] = temps
    return cpu, extra


def cpu_data(feed, settle=0.0):
    """One reading of everything the screen shows: {cpu, procs, extra, at}. A sampler that raises leaves its part empty and a note."""
    now, notes = time.time(), []
    if DEMO:
        import demo
        raw_cpu, raw_pr, sens = demo.cpu_sample(DEMO_OS, now), demo.proc_sample(DEMO_OS, now), demo.sensors(DEMO_OS, now)
    else:
        if feed.cs is None:  # created on the first read: process sampling costs CPU, only a screen that is shown pays for it
            for attr, what, make in (("cs", "cpu", lambda: cpuinfo.CpuSampler()), ("ps", "process", lambda: procs.ProcSampler())):
                try:
                    setattr(feed, attr, make())
                except Exception as e:  # noqa: BLE001 - a broken producer leaves its half of the screen empty
                    setattr(feed, attr, False)
                    notes.append(safe(f"{what} sampler: {type(e).__name__}"))
            if settle and feed.ps:  # a first reading, so that the next one has a CPU% to show
                try:
                    feed.ps.sample()
                    time.sleep(settle)
                except Exception:  # noqa: BLE001
                    pass

        def read(src, what):
            try:
                r = src.sample() if src else None
            except Exception as e:  # noqa: BLE001
                notes.append(safe(f"{what} sampler failed: {type(e).__name__}"))
                r = None
            return r if isinstance(r, dict) else {}
        raw_cpu, raw_pr = read(feed.cs, "cpu"), read(feed.ps, "process")
        sens = load_json(SENSORS) if cpu_os() != "linux" else None
    cpu, extra = cpu_merge(raw_cpu, sens, now)
    pl = [p for p in (raw_pr.get("procs") if isinstance(raw_pr.get("procs"), list) else []) if isinstance(p, dict) and isinstance(p.get("pid"), int) and not isinstance(p["pid"], bool)]
    total = dd(raw_pr.get("total"))
    for x in notes + list(cpu.get("notes") or []) + list(raw_pr.get("notes") or []):  # what the producers could not read, once each
        if isinstance(x, str) and ("info", safe(x)) not in extra["notes"]:
            extra["notes"].append(("info", safe(x)))
    return {"cpu": cpu, "procs": {"procs": pl, "total": total}, "extra": extra, "at": now}


class CpuFeed(object):
    """The CPU screen's data source: one CpuSampler and one ProcSampler, created at the first read (process sampling costs CPU: only a
    screen that is shown owns one). read() hands the last reading back while it is younger than max_age (the web page: one sampling
    per refresh interval, whoever asks). settle: seconds between a first process reading and the one shown, so that a one-off
    screen or a page already has a CPU% (the console instead shows "measuring" for a second)."""

    def __init__(self, settle=0.0, max_age=0.0):
        self.cs = self.ps = None
        self.at, self.data, self.demo, self.settle, self.max_age = 0.0, None, None, settle, max_age

    def read(self):
        now = time.time()
        if self.data is None or self.demo != (DEMO, DEMO_OS) or not 0 <= now - self.at < self.max_age:
            self.demo = (DEMO, DEMO_OS)
            self.data, self.at = cpu_data(self, self.settle), now
        return self.data


class CpuView(object):
    """The interactive CPU screen: the sort, the process under the cursor (its pid survives refreshes; its index is where the
    cursor stays when that process vanishes), the scroll position, the details pane, when it was opened and last touched."""

    def __init__(self, now=None):
        self.sort, self.cur, self.idx, self.top, self.details, self.page = "cpu", None, 0, 0, False, 10
        self.feed = CpuFeed()
        self.opened = self.touched = now or time.time()


def cpu_rows(pl, sort="cpu"):
    """The processes in the order of `sort`: CPU%, memory, CPU time (largest first), PID, user (smallest first). A process whose value
    is unknown goes last, by PID."""
    field = {"cpu": "cpu", "mem": "mem", "time": "time", "pid": "pid", "user": "user"}.get(sort, "cpu")
    val = lambda p: (str(p["user"]).lower() if isinstance(p.get("user"), str) else None) if field == "user" else num(p.get(field))  # noqa: E731
    known = [p for p in pl if val(p) is not None]
    rest = sorted((p for p in pl if val(p) is None), key=lambda p: p["pid"])
    if field in ("pid", "user"):
        known.sort(key=lambda p: (val(p), p["pid"]))
    else:
        known.sort(key=lambda p: (-val(p), p["pid"]))
    return known + rest


def cpu_sync(cv, rows):
    """The cursor back on its process: by pid, else the same index (clamped). Returns the index."""
    i = next((j for j, p in enumerate(rows) if p["pid"] == cv.cur), None) if cv.cur is not None else None
    cv.idx = i if i is not None else max(0, min(cv.idx, len(rows) - 1))
    cv.cur = rows[cv.idx]["pid"] if rows else None
    return cv.idx


def cpu_key(cv, key, rows, page=10):
    """One key on the CPU screen (what each key does: ui.KEYMAP, scope cpu). Returns 'back' (leave it: Esc or q when no details pane is
    open), 'rows' (the sort changed: sort again, then cpu_sync) or ''."""
    k = key.lower() if len(key) == 1 else key  # P and p are the same key
    act = ui.action("cpu", key)
    if act == "back":
        if cv.details:
            cv.details = False
            return ""
        return "back"
    if act == "sort":
        cv.sort = CPU_SORT_KEYS[k]
        return "rows"
    if act == "details":
        cv.details = not cv.details
        return ""
    if not rows or act not in ("move", "page"):
        return ""
    i = cpu_sync(cv, rows)
    i = {"up": i - 1, "k": i - 1, "down": i + 1, "j": i + 1, "pgup": i - page, "pgdn": i + page, "home": 0, "end": len(rows) - 1}.get(k, i)
    cv.idx = max(0, min(i, len(rows) - 1))
    cv.cur = rows[cv.idx]["pid"]
    return ""


def cpu_select(rows, cv, text):
    """The cursor on the process whose pid is text, else on the first one whose name contains it (any case). False: none."""
    t = text.strip().lower()
    for hit in (lambda p: t == str(p["pid"]), lambda p: t in str(p.get("name")).lower()):
        for p in rows:
            if t and hit(p):
                cv.cur = p["pid"]
                return True
    return False


# -- what is known about each logical CPU

_TOPO = {}


def cpu_topology(ids):
    """{logical CPU: physical core id} from sysfs (Linux): the key the sampler's per-core temperatures are filed under."""
    if DEMO or cpu_os() != "linux" or not LINUX:
        return {}
    key = tuple(ids)
    if key not in _TOPO:
        got = {i: read_file(f"/sys/devices/system/cpu/cpu{i}/topology/core_id") for i in ids}
        _TOPO[key] = {i: int(v) for i, v in got.items() if v and v.lstrip("-").isdigit()}
    return _TOPO[key]


def cpu_core_of(d, ids):
    """{logical CPU: physical core id} among the cores that have a temperature. The sampler's own `core_of` map when it gives one, else
    sysfs' topology (Linux); without either, the logical CPUs of a core are taken to be next to each other (how Windows numbers
    them) and the cores to be in the order of their sensors, which only holds when every core has one sensor and the same number
    of threads. Otherwise unknown: nothing is invented."""
    cpu = d["cpu"]
    by_core = {k for k, v in idict(dget(cpu, "temps", "cores")).items() if num(v) is not None}
    core_of = idict(cpu.get("core_of")) or cpu_topology(ids)
    if core_of:
        return {i: core_of[i] for i in ids if core_of.get(i) in by_core}
    n = int(num(cpu.get("cores")) or 0)
    if n and len(by_core) == n and len(ids) % n == 0 and ids == sorted(ids):
        per, order = len(ids) // n, sorted(by_core)
        return {i: order[j // per] for j, i in enumerate(ids)}
    return {}


def cpu_core_temps(d, ids):
    """{logical CPU: C}: the temperature of the physical core each one runs on."""
    by_core = {k: float(v) for k, v in idict(dget(d["cpu"], "temps", "cores")).items() if num(v) is not None}
    return {i: by_core[k] for i, k in cpu_core_of(d, ids).items()}


def cpu_tags(d, ids):
    """{logical CPU: 'P' or 'E'}: from the sampler's kinds (ids), else from the cluster names of the Apple Silicon sensors."""
    out = {}
    kinds = dget(d["cpu"], "kinds")
    for tag in ("P", "E"):
        for i in (kinds.get(tag) if isinstance(kinds, dict) and isinstance(kinds.get(tag), (list, tuple)) else []):
            if isinstance(i, int):
                out[i] = tag
    for cl in d["extra"]["clusters"]:
        tag = str(cl.get("name") or "")[:1].upper()
        out.update({i: tag for i in idict(cl.get("cpus")) if tag in ("P", "E")})
    return {i: t for i, t in out.items() if i in ids}


def cpu_mhz(d):
    """{logical CPU: MHz}: the sampler's per-CPU clocks (an id of -1 is a whole-machine value, not a CPU), else the Apple clusters'."""
    out = {i: float(v) for i, v in idict(dget(d["cpu"], "freq", "cur")).items() if i >= 0 and num(v) is not None}
    for cl in d["extra"]["clusters"]:
        for i, v in idict(cl.get("cpus")).items():
            if num(v) is not None:
                out.setdefault(i, float(v))
    return out


def cpu_usage_rows(d):
    rows = dget(d["cpu"], "usage", "cores")
    return sorted((r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and isinstance(r.get("id"), int)
                   and not isinstance(r["id"], bool)), key=lambda r: r["id"])


# -- drawing

def cpu_bar(parts, w):
    """A w-wide bar of consecutive segments [(percent, colour, glyph)], the rest idle: htop's CPU meter (user green, system red,
    other busy blue, I/O wait grey; a different glyph for the wait, so that colour is not all that tells it)."""
    out, done, acc = "", 0, 0.0
    for pct, col, ch in parts:
        acc += max(num(pct) or 0.0, 0.0)
        end = min(w, int(round(acc * w / 100.0)))
        if end > done:
            out += c(col, ch * (end - done))
            done = end
    return out + c(90, "░" * (w - done))


def cpu_parts(user, system, iowait, busy):
    """The segments of one CPU's bar. Without the user/system split the whole busy part is one plain segment."""
    user, system, iowait, busy = num(user), num(system), num(iowait), num(busy)
    if busy is None:
        return []
    split = [(user, "32", "█"), (system, "31", "█"), (max(busy - (user or 0) - (system or 0), 0.0), "34", "█")] \
        if user is not None and system is not None else [(busy, "36", "█")]
    return split + ([(iowait, "90", "▒")] if iowait else [])


def pct_col(p, warn=70, err=90):
    p = num(p)
    return "" if p is None else "31" if p >= err else "33" if p >= warn else ""


def cpu_head(d, w, h):
    """The title and the machine in a few lines: what it is, then what it is doing."""
    cpu, pr = d["cpu"], d["procs"]
    lab = lambda name, value: c(90, name + " ") + value  # noqa: E731
    qi = lambda x: "?" if num(x) is None else str(int(num(x)))  # noqa: E731
    model = safe(cpu.get("model") or "?")
    ident = [lab("sockets", qi(cpu.get("sockets"))), lab("cores", qi(cpu.get("cores"))), lab("threads", qi(cpu.get("threads")))]
    kinds = dd(cpu.get("kinds"))
    if kinds:
        cnt = lambda x: len(x) if isinstance(x, (list, tuple)) else qi(x)  # noqa: E731
        ident.append(f"P {cnt(kinds.get('P'))} + E {cnt(kinds.get('E'))} " + ("threads" if isinstance(kinds.get("P"), (list, tuple)) else "cores"))
    ident.append(lab("arch", safe(cpu.get("arch") or "?")))
    cache = dd(cpu.get("cache"))
    sizes = [f"{k} {fmt_size(cache[k])}" for k in ("L1d", "L1i", "L2", "L3") if k in cache]
    ident.append(lab("cache", "  ".join(sizes) if sizes else "?"))
    fr = dd(cpu.get("freq"))
    gov = [safe(x) for x in (fr.get("governor"), fr.get("driver")) if isinstance(x, str) and x]
    ident.append(lab("governor", "/".join(gov) if gov else "?"))
    lo, hi, base = num(fr.get("min")), num(fr.get("max")), num(fr.get("base"))
    clock = [x for x in idict(fr.get("cur")).items() if x[0] < 0 and num(x[1]) is not None]
    ident.append(lab("clock", (f"{qf(lo)}-{qf(hi)} MHz" if lo is not None or hi is not None else "?") + (f" (base {qf(base)})" if base is not None else "")
                     + (f" now {qf(clock[0][1])}" if clock else "")))
    rates, load = dd(cpu.get("rates")), cpu.get("load")
    tot = pr["total"]
    run = rates.get("running") if num(rates.get("running")) is not None else tot.get("running")
    up = num(cpu.get("uptime"))
    act = [lab("up", fmt_dur(up) if up is not None else "?"),
           lab("load", " ".join(qf(x, ".2f") for x in load[:3]) if isinstance(load, (list, tuple)) and len(load) >= 3 else "?"),
           lab("ctxt", fmt_k(rates.get("ctxt")) + "/s"), lab("intr", fmt_k(rates.get("intr")) + "/s"),
           lab("running", qf(run)), lab("blocked", qf(rates.get("blocked")))]
    lines = [section("CPU", w, model)]
    lines += wrap_items(ident, w, indent=1, sep="  ·  ", max_lines=1 if h < 28 else 2)
    lines += wrap_items(act, w, indent=1, sep="  ·  ", max_lines=1)
    return lines


def cpu_cells(d):
    """One dict per logical CPU: id, tag, busy/user/system/iowait, mhz, temp. None for what is not known."""
    rows = cpu_usage_rows(d)
    ids = [r["id"] for r in rows]
    tags, mhz, temps = cpu_tags(d, ids), cpu_mhz(d), cpu_core_temps(d, ids)
    return [{"id": r["id"], "tag": tags.get(r["id"], ""), "busy": num(r.get("busy")), "user": r.get("user"), "system": r.get("system"),
             "iowait": r.get("iowait"), "mhz": mhz.get(r["id"]), "temp": temps.get(r["id"])} for r in rows]


def cpu_limit(d):
    """The package sensor's own limit (crit, else high): what the temperatures are measured against. None = not known."""
    t = dd(d["cpu"].get("temps"))
    return num(t.get("crit")) or num(t.get("high"))


def temp_col(t, mx):
    t = num(t)
    return "" if t is None or not mx else "31" if t >= THERMAL_ERR * mx else "33" if t >= THERMAL_WARN * mx else ""


def cpu_grid(d, w):
    """(top lines, cell rows, cells per row, cells): the whole CPU as one bar with its split, then one cell per logical CPU in 2-4
    columns, htop style: id, P/E, bar (user green, system red, I/O wait grey), busy %, GHz and °C where known."""
    tot = dget(d["cpu"], "usage", "total")
    cells = cpu_cells(d)
    show_f, show_t = any(x["mhz"] is not None for x in cells), any(x["temp"] is not None for x in cells)
    show_tag = any(x["tag"] for x in cells)
    idw = max([len(str(x["id"])) for x in cells] + [2])
    fixed = idw + (1 if show_tag else 0) + 1 + 1 + 4 + (6 if show_f else 0) + (6 if show_t else 0)  # id tag _ bar _ busy _GHz _temp
    gap, room = 2, w - 1  # one column of margin
    ncol = 1
    for n in (4, 3, 2):
        if n <= max(1, len(cells)) and (room - (n - 1) * gap) // n - fixed >= CPU_CELL_MIN_BAR:
            ncol = n
            break
    cw = (room - (ncol - 1) * gap) // ncol
    bw = max(4, min(CPU_CELL_MAX_BAR, cw - fixed))
    top = []
    if isinstance(tot, dict):
        busy, user, system, iow = (num(tot.get(k)) for k in ("busy", "user", "system", "iowait"))
        items = [c(32, "user") + " " + qf(user, ".1f"), c(31, "sys") + " " + qf(system, ".1f"), "nice " + qf(tot.get("nice"), ".1f"),
                 c(90, "iowait") + " " + qf(iow, ".1f"), "irq " + qf(tot.get("irq"), ".1f"), "steal " + qf(tot.get("steal"), ".1f"),
                 "idle " + qf(tot.get("idle"), ".1f")]
        shown = cc(pct_col(busy), f"{busy:5.1f}%") if busy is not None else "    ?%"
        top.append(fit_join(items, "  ", w, f" {c(1, 'ALL')} {cpu_bar(cpu_parts(user, system, iow, busy), bw)} {shown}  "))
    if not cells:
        return top + [msg("warn", "per-CPU usage: ? (the sampler gave none)")], [], 1, 0
    mx = cpu_limit(d)
    out = []
    for x in cells:
        busy = x["busy"]
        s = f"{x['id']:>{idw}}"
        if show_tag:
            s += c("1;36", x["tag"]) if x["tag"] == "P" else c(90, x["tag"]) if x["tag"] else " "
        s += " " + cpu_bar(cpu_parts(x["user"], x["system"], x["iowait"], busy), bw)
        s += " " + (cc(pct_col(busy), f"{busy:3.0f}%") if busy is not None else "   ?")
        if show_f:
            s += " " + (f"{x['mhz'] / 1000:4.2f}G" if x["mhz"] is not None else "    ?")
        if show_t:
            s += " " + (cc(temp_col(x["temp"], mx), f"{x['temp']:3.0f}°C") if x["temp"] is not None else "    ?")
        out.append(s)
    rows = [" " + (" " * gap).join(pad(clip(x, cw), cw) for x in out[i:i + ncol]).rstrip() for i in range(0, len(out), ncol)]
    return top, rows, ncol, len(cells)


def cpu_grid_lines(grid, n):
    """The grid in at most n lines: the whole-CPU bar first, then rows of cells; when some are left out the last line counts them."""
    top, rows, ncol, ncells = grid
    if len(top) + len(rows) <= n:
        return top + rows
    keep = max(n - len(top) - 1, 0)
    return (top + rows[:keep] + [c(90, f" … +{max(ncells - keep * ncol, 0)} more CPUs")])[:n]


def deg(x):
    return "?" if num(x) is None else f"{num(x):.0f}°C"


def cpu_temps(d, w):
    """The temperatures block, most important line first: package with its limits, hottest core and throttling, then macOS pressure and
    clusters, every sensor, and what is missing."""
    cpu, extra = d["cpu"], d["extra"]
    t = dd(cpu.get("temps"))
    pkg, high, crit, mx = num(t.get("package")), num(t.get("high")), num(t.get("crit")), cpu_limit(d)
    bw = max(10, min(40, w - 60))
    lines = [section("TEMPERATURES", w, "source " + safe(t.get("source") or "?"))]
    if pkg is None:
        lines.append(f" {c(90, 'PKG')}   ?   {c(90, 'no package temperature')}")
    elif mx:
        lines.append(f" {c(90, 'PKG')}   {bar(pkg / mx, bw, THERMAL_WARN, THERMAL_ERR)} {pkg:.0f}°C/{mx:.0f}°C   "
                     f"{c(90, 'high')} {deg(high)}  {c(90, 'crit')} {deg(crit)}")
    else:
        lines.append(f" {c(90, 'PKG')}   {pkg:.0f}°C   {c(90, 'high ?  crit ?')}")
    ids = [r["id"] for r in cpu_usage_rows(d)]
    by_core = {k: float(v) for k, v in idict(t.get("cores")).items() if num(v) is not None}
    on_core = cpu_core_of(d, ids)
    summary = []
    if by_core:
        core, hot = max(by_core.items(), key=lambda kv: (kv[1], -kv[0]))
        cpus = [str(i) for i, k in on_core.items() if k == core]
        summary.append(c(90, "hottest core ") + f"{core} " + cc(temp_col(hot, mx), f"{hot:.0f}°C")
                       + (c(90, " (cpu " + ",".join(cpus[:4]) + ")") if cpus and len(cpus) <= 4 else ""))
    else:
        summary.append(c(90, "hottest core ") + "?")
    th = dd(cpu.get("throttle"))
    n, secs, cores = num(th.get("package")), num(th.get("package_s")), idict(th.get("cores"))
    per = sorted(((k, num(v)) for k, v in cores.items() if num(v) is not None), key=lambda kv: (-kv[1], kv[0]))
    if n is None and not per:
        summary.append(c(90, "throttled ") + "?")
    else:
        txt = f"{n:.0f} events" if n is not None else "? events"
        if secs is not None:
            txt += f", {fmt_min(secs)} in all"
        if per:
            txt += "; cores " + ", ".join(f"{k}: {v:.0f}" for k, v in per[:3]) + (f" … +{len(per) - 3}" if len(per) > 3 else "")
        hit = bool(n) or any(v for _, v in per)
        summary.append(c(90, "throttled ") + c(33 if hit else 32, ("! " if hit else "✔ ") + txt))
    lines.append(" " + "   ·   ".join(summary))
    if cpu_os() == "darwin" or extra["pressure"] or extra["clusters"]:
        pr = extra["pressure"]
        parts = [c(90, "thermal pressure ") + (c(CPU_PRESSURE.get(pr.lower(), "33"), safe(pr)) if isinstance(pr, str) else "?")]
        parts += [safe(cl.get("name") or "?") + " " + qf(cl.get("mhz")) + " MHz " + qf(cl.get("active")) + "% active" for cl in extra["clusters"]]
        lines.append(" " + "   ·   ".join(parts))
    sensors = [s for s in (t.get("sensors") or []) if isinstance(s, dict) and num(s.get("c")) is not None]
    if sensors:
        items = [safe(s.get("label") or "?") + " " + cc(temp_col(s["c"], num(s.get("crit")) or mx), f"{s['c']:.0f}°C") for s in sensors]
        rows = wrap_items(items, w, indent=9, sep="  ·  ", max_lines=2)
        rows[0] = " " + c(90, "sensors") + " " + rows[0][9:]
        lines += rows
    lines += [msg(lv, safe(text)) for lv, text in extra["notes"]]
    return lines


def cut_lines(lines, n, what="lines"):
    """The first n lines; when some are left out the last one says how many ('… +3 more lines')."""
    if len(lines) <= n:
        return lines
    return lines[:max(n - 1, 0)] + ([c(90, f" … +{len(lines) - n + 1} more {what}")] if n else [])


# -- the processes

CPU_COLS = (("pid", "PID", ">"), ("user", "USER", "<"), ("state", "S", "<"), ("nice", "NI", ">"), ("threads", "THR", ">"), ("cpu", "CPU%", ">"),
            ("mem_pct", "MEM%", ">"), ("mem", "RSS", ">"), ("time", "TIME", ">"))
CPU_COL_W = {"user": 9, "state": 1, "nice": 3, "cpu": 5, "mem_pct": 5, "mem": 6, "time": 8}
CPU_SORT_COLS = {"cpu": ("cpu",), "mem": ("mem_pct", "mem"), "time": ("time",), "pid": ("pid",), "user": ("user",)}
CPU_DROPPABLE = ("state", "nice", "threads")  # a column unknown for every process on this OS (Windows has no state or nice) is left out
CPU_STATE_COL = {"R": "32", "D": "33", "Z": "31", "T": "33"}


def cpu_cell_text(p, key):
    v = p.get(key)
    if key == "user":
        s = safe(v) if isinstance(v, str) and v else "?"
        return s if len(s) <= 9 else s[:8] + "+"
    if key == "state":
        return safe(v)[:1] if isinstance(v, str) and v else "?"
    if key == "cpu":
        return "?" if num(v) is None else f"{num(v):.1f}" if num(v) < 1000 else f"{num(v):.0f}"
    if key == "mem_pct":
        return qf(v, ".1f")
    if key == "mem":
        return fmt_size(v)
    if key == "time":
        return fmt_cputime(v)
    return qf(v)  # pid, nice, threads


def cpu_columns(rows, w, minname=12):
    """[(key, title, align, width)] that fit in w columns beside a name of at least minname: dropped from the right, the name stays."""
    wid = dict(CPU_COL_W)
    wid["pid"] = max([len(str(p["pid"])) for p in rows] + [5])
    wid["threads"] = max([len(cpu_cell_text(p, "threads")) for p in rows] + [3])
    unknown = lambda p, k: num(p.get(k)) is None and not isinstance(p.get(k), str)  # noqa: E731
    cols = [(k, t, a, wid[k]) for k, t, a in CPU_COLS if not (k in CPU_DROPPABLE and rows and all(unknown(p, k) for p in rows))]
    while cols and 1 + sum(x[3] + 1 for x in cols) + minname > w:
        cols.pop()
    return cols


def cpu_proc_line(p, cols, nw):
    s = " "
    for key, _, align, width in cols:
        txt = cpu_cell_text(p, key)
        cell = f"{txt:>{width}}" if align == ">" else f"{txt:<{width}}"
        if key == "state":
            cell = cc(CPU_STATE_COL.get(txt, ""), cell)
        elif key == "cpu":
            cell = cc(pct_col(p.get("cpu"), 50, 100), cell)
        s += cell + " "
    return s + safe(p.get("name") or "?")[:nw]


def cpu_pane(p, rows, w, h, now, two=False):
    """Everything known about one process in at most h lines of w columns: the contract's fields, the parent's name, '?' for what is
    unknown. two: the fields in two columns (a pane under the table, which has few lines to spare)."""
    ppid = p.get("ppid") if isinstance(p.get("ppid"), int) else None
    parent = next((q for q in rows if q["pid"] == ppid), None) if ppid is not None else None
    st = p.get("state") if isinstance(p.get("state"), str) and p.get("state") else None
    start, mem = num(p.get("start")), num(p.get("mem"))
    when = "?" if start is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start)) + (f" ({fmt_ago(now - start)} ago)" if now >= start else "")
    items = [("parent", "?" if ppid is None else f"{ppid}  " + (safe(parent.get("name") or "?") if parent else "? (not in the list)")),
             ("user", safe(p.get("user") or "?")), ("state", "?" if st is None else safe(st[:1]) + "  " + CPU_STATES.get(st[:1], "")),
             ("threads", qf(p.get("threads"))), ("nice", qf(p.get("nice"))), ("priority", qf(p.get("prio"))),
             ("CPU", qf(p.get("cpu"), ".1f", " %") + ("  (100 % = one core)" if num(p.get("cpu")) is not None else "")),
             ("memory", qf(p.get("mem_pct"), ".1f", " %") + ("" if mem is None else f"  {fmt_size(mem)} resident")),
             ("CPU time", fmt_cputime(p.get("time"))), ("started", when),
             ("children", str(sum(1 for q in rows if q.get("ppid") == p["pid"])))]
    out = [section(f"PROCESS {p['pid']}", w, safe(p.get("name") or "?"))]
    if two:
        half = (len(items) + 1) // 2
        cw = (w - 2) // 2
        for a, b in zip(items[:half], items[half:] + [None] * half):
            out.append(" " + "".join(pad(clip(c(90, pad(x[0], 9)) + x[1], cw), cw + 1) if x else "" for x in (a, b)).rstrip())
    else:
        lw = 10
        for label, value in items:
            chunks = textwrap.wrap(value, max(8, w - lw - 1), break_on_hyphens=False) or [""]
            out += [" " + (c(90, pad(label, lw)) if j == 0 else " " * lw) + x for j, x in enumerate(chunks)]
    return cut_lines(out, h, "details")


def cpu_procs(d, rows, w, avail, cur, top, sort):
    """The process table in `avail` lines (title, column heads, rows). With a cursor it scrolls to keep it in sight; without one
    (the rotation slide, a web page) what does not fit is counted on the last line.
    -> (lines, first row shown, rows visible, [(line, pid)] of the rows drawn)."""
    tot = d["procs"]["total"]
    cnt = int(num(tot.get("count")) or len(rows))
    run, thr, unread = num(tot.get("running")), num(tot.get("threads")), num(tot.get("unreadable"))
    note = f"{cnt} total" + (f" · {run:.0f} running" if run is not None else "") + (f" · {thr:.0f} threads" if thr is not None else "") \
        + (f" · {unread:.0f} unreadable" if unread else "") + f" · by {CPU_SORT_NAME.get(sort, sort)}"
    if rows and all(num(p.get("cpu")) is None for p in rows):
        note += " · CPU% ?: measuring"
    lines = [section("PROCESSES", w, note)]
    if not rows:
        return (lines + [msg("warn", "processes: ? (the sampler gave none)")])[:avail], 0, 0, []
    cols = cpu_columns(rows, w)
    nw = max(4, w - 1 - sum(x[3] + 1 for x in cols))
    head = " "
    for key, title, align, width in cols:
        mark = key in CPU_SORT_COLS.get(sort, ())  # the column the rows are in the order of: yellow, with an arrow for the direction
        title = title + ("▼" if sort in ("cpu", "mem", "time") else "▲") if mark and key != "mem" else title
        cell = f"{title:>{width}}" if align == ">" else f"{title:<{width}}"
        head += c("1;33" if mark else "1;36", cell) + " "
    lines.append(head + c("1;36", "NAME"))
    vis = max(0, avail - 2)
    i = next((j for j, p in enumerate(rows) if p["pid"] == cur), None) if cur is not None else None
    rest = 0
    if i is None:
        top = 0
        if len(rows) > vis:
            vis = max(0, vis - 1)  # the last line says what is left out
            rest = len(rows) - vis
    else:
        top = map_scroll(top, i, len(rows), vis) if vis else 0
    shown = rows[top:top + vis]
    pids = []
    for j, p in enumerate(shown):
        line = cpu_proc_line(p, cols, nw)
        lines.append(c(7, pad(ANSI.sub("", clip(line, w)), w)) if i is not None and top + j == i else line)
        pids.append((len(lines) - 1, p["pid"]))
    if rest:
        lines.append(c(90, f" … +{rest} more processes"))
    return lines[:avail], top, len(shown), pids


def cpu_view(d, w, h, sort="cpu", cur=None, details=False, top=0):
    """(the CPU screen's body: at most h lines, none wider than w; the first process shown; the processes in order; how many are
    visible; [(line, pid)] of the process rows). cur: the pid under the cursor (None: no cursor, the table is cut at the bottom);
    details: the cursor's process in a pane (beside the table from CPU_PANE_W columns on, below it otherwise)."""
    rows = cpu_rows(d["procs"]["procs"], sort)
    head = cpu_head(d, w, h)
    grid = cpu_grid(d, w)
    n_grid = len(grid[0]) + len(grid[1])
    temps = cpu_temps(d, w)
    sel = next((p for p in rows if p["pid"] == cur), None) if details else None
    side = sel is not None and w >= CPU_PANE_W
    tw = w - (max(46, int(w * 0.42)) + 3 if side else 0)
    pane = cpu_pane(sel, rows, w, 40, d["at"], two=w >= 70) if sel is not None and not side else []
    free = h - len(head)
    p_min = 2 + (5 if cur is not None else 3)
    pane_h = min(len(pane), max(0, free // 2))
    free -= pane_h
    g_len = min(n_grid, max(min(n_grid, 3), free - p_min - min(len(temps), 3)))
    free -= g_len
    t_len = min(len(temps), max(0, free - p_min))
    if t_len < min(len(temps), 3):  # not even the title and two lines: the processes get the room instead of a "… +N more"
        t_len = 0
    free -= t_len
    lines = head + cpu_grid_lines(grid, g_len) + cut_lines(temps[1:] if t_len < len(temps) else temps, t_len)  # cut: no title, the lines say it
    table, top, vis, pids = cpu_procs(d, rows, tw, max(free, 0), cur, top, sort)
    pids = [(len(lines) + k, pid) for k, pid in pids]
    if side:  # the pane may be taller than a short table: it has all the room the table could have had
        beside = cpu_pane(sel, rows, w - tw - 3, max(free, 0), d["at"])
        table = [pad(table[k] if k < len(table) else "", tw) + c(90, " │ ") + (beside[k] if k < len(beside) else "")
                 for k in range(max(len(table), len(beside)))]
    lines += table + (cut_lines(pane, pane_h, "details") if pane_h < len(pane) else pane)
    return [clip(x, w) for x in lines[:h]], top, rows, vis, pids


def cpu_footer(cv, n, w):
    """Where the cursor is, and the keys (ui.KEYMAP): in short words when the screen is narrow, then the least needed go first."""
    sorts = "  ".join(f"{k.upper()} {CPU_SORT_SHORT[v]}" for k, v in CPU_SORT_KEYS.items())
    dyn = {"sort": ("sort: " + sorts, sorts), "details": ("Enter: " + ("hide details" if cv.details else "details"), "Enter: details")}
    text = ui.footer("cpu", f" row {cv.idx + 1}/{n}   " if n else " row 0/0   ", w, on, dyn)
    here = f"{next(k for k, v in CPU_SORT_KEYS.items() if v == cv.sort).upper()} {CPU_SORT_SHORT[cv.sort]}"
    return clip(c(90, text.replace(here, "\x1b[0m" + c("1;7", here) + "\x1b[90m", 1)), w)  # the sort in use, reversed


def cpu_screen(d, pb, cv, w, h):
    """(the interactive CPU screen as one frame: header, body, key help; the processes in order): the live loop and --once."""
    rows = cpu_rows(d["procs"]["procs"], cv.sort)
    cpu_sync(cv, rows)
    body, cv.top, _, vis, _ = cpu_view(d, w, h - 2, cv.sort, cv.cur, cv.details, cv.top)
    cv.page = max(1, vis - 1)
    return frame(("CPU", 1, 1, body), 0, 1, w, h, pb, foot=cpu_footer(cv, len(rows), w)), rows


def cpu_slide(feed, w, body_h):
    """The CPU among the rotating pages ([dashboard] cpu_in_rotation): header, CPUs, temperatures, the top processes that fit."""
    return cpu_view(feed.read(), w, body_h)[0]


def fill_cpu(sl, idx, w, body_h, feed):
    """The CPU slide is built empty by slides(lazy): its samplers cost CPU, so only the slide on screen gets its data."""
    if sl[idx][0] == "CPU":
        try:
            lines = cpu_slide(feed, w, body_h)
        except Exception as e:  # noqa: BLE001 - same rule as the other pages
            lines = [c(31, f" error on page CPU: {safe(repr(e))[:w - 20]}")]
        sl[idx] = ("CPU", 1, 1, lines)


def cpu_problems(smp=None):
    """The header's problems of the current state: the CPU screen has no graph of its own to take them from."""
    st = snapshot(0)
    sm = host_sample(smp)
    if DEMO:
        demo_defaults()
    return safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


def cpu_once(argv, w, h):
    """`--once --view cpu`: the CPU screen as the console draws it (tests, screenshots). --sort cpu|mem|time|pid|user (anything else:
    cpu); --select NAME: the cursor on the first process whose name contains NAME (any case) or whose pid it is; --details: the
    details pane of the process under the cursor (Enter)."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    d = CpuFeed(settle=0.5).read()
    cv = CpuView()
    cv.sort = opt("--sort") if opt("--sort") in CPU_SORTS else "cpu"
    cv.details = "--details" in argv
    if opt("--select"):
        cpu_select(cpu_rows(d["procs"]["procs"], cv.sort), cv, opt("--select"))
    return cpu_screen(d, cpu_problems(None if DEMO else Sampler()), cv, w, h)[0]


def cpu_web(d, pb, w, h, sort="cpu", sel=None, scroll=False):
    """(the CPU screen for a browser page: one frame; [(line of the frame, pid)] of the process rows, which the page makes links).
    sel: the pid whose details are shown, if it is one of this reading's processes. scroll: as tall as its content."""
    sel = sel if any(p["pid"] == sel for p in d["procs"]["procs"]) else None
    body, _, rows, _, pids = cpu_view(d, w, 10 ** 4 if scroll else h - 2, sort, sel, sel is not None)
    tail = c(90, f" by {CPU_SORT_NAME.get(sort, sort)} · {len(rows)} processes listed" + (" · details of the highlighted row" if sel is not None else ""))
    return frame(("CPU", 1, 1, body), 0, 1, w, len(body) + 2 if scroll else h, pb, foot=tail, page=True), [(i + 1, pid) for i, pid in pids]


def render_screen(smp, w, h, mode=None, n=0, at=None, keys=True, page=False, scroll=False, cpu_feed=None):
    """One frame as an ANSI string and the number of slides: used by --once and by the web view (web.py).
    at = a time: the slide shown at that moment of the rotation (overview, then Details pages), as on the console."""
    st, sm = snapshot(w), host_sample(smp)
    if DEMO:
        demo_defaults()
    sl = slides(sm, st["cont"], st["net"], w, h - 2, st["boot"], st["baseline"], mode=mode, scroll=scroll, cpu_lazy=True)
    if scroll:  # the page is as tall as its content (header + body + footer)
        h = len(sl[0][3]) + 2
    n = pick_slide(sl, at) if at is not None else n
    fill_cpu(sl, n % len(sl), w, h - 2, cpu_feed or CpuFeed(settle=0.5))  # only the slide shown reads the processes
    return frame(sl[n % len(sl)], n % len(sl), len(sl), w, h,
                 safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"]), keys=keys, page=page), len(sl)


def render_screens(smp, w, h, mode=None, keys=True, page=False, cpu_feed=None):
    """Every slide (overview + detail pages) as ANSI frames: the web "full details" view."""
    st, sm = snapshot(w), host_sample(smp)
    if DEMO:
        demo_defaults()
    sl = slides(sm, st["cont"], st["net"], w, h - 2, st["boot"], st["baseline"], mode=mode, cpu_feed=cpu_feed or CpuFeed(settle=0.5))
    pb = safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
    return [frame(x, i, len(sl), w, h, pb, keys=keys, page=page) for i, x in enumerate(sl)]


def utf8_stdout():
    """Windows pipes default to the ANSI code page: the bars and symbols must not crash `--once > file` or `| tool`."""
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if sys.stdout is not None and enc != "utf8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def once(argv):
    global DEMO, DEMO_OS, DEMO_HEALTH
    DEMO = "--demo" in argv
    DEMO_OS = argv[argv.index("--demo-os") + 1] if "--demo-os" in argv[:-1] else None
    DEMO_HEALTH = argv[argv.index("--demo-health") + 1] if "--demo-health" in argv[:-1] else ""
    arg = lambda k, d: int(argv[argv.index(k) + 1]) if k in argv else d
    w, h, n = arg("--cols", 120) - 1, arg("--rows", 33), arg("--slide", 0)
    view = argv[argv.index("--view") + 1] if "--view" in argv[:-1] else ""
    if view == "map":  # the Map screen (see map_once for its options)
        if not on("map"):
            print("the map is off: [features] map = no in config.ini", file=sys.stderr)
            return 2
        out = map_once(argv, w, h)
    elif view == "cpu":  # the CPU screen (see cpu_once)
        if not on("cpu"):
            print("the CPU screen is off: [features] cpu = no in config.ini", file=sys.stderr)
            return 2
        out = cpu_once(argv, w, h)
    elif view == "health":  # the Health screen (see health_once for its options)
        if not on("health"):
            print("the health screen is off: [features] health = no in config.ini", file=sys.stderr)
            return 2
        out = health_once(argv, w, h)
        if out is None:
            return 2
    elif view == "ai":  # the AI screen (see ai_once for its options)
        if not on("ai"):
            print("the AI screen is off: [features] ai = no in config.ini", file=sys.stderr)
            return 2
        out = ai_once(argv, w, h)
    else:
        smp = None if DEMO else Sampler()  # the demo reads nothing from this machine (render_screen)
        if smp:
            smp.sample()  # starts the background reads (sessions, disks): they have the half second below to arrive
            time.sleep(0.5)
        out, _ = render_screen(smp, w, h, n=n)
    print(out if "--color" in argv else ANSI.sub("", out))  # --color keeps the ANSI codes (used by tools/ansi2svg.py)


# ---- HEALTH screen: health.py's report (the history over days and weeks) on the console, moved through with the keyboard ------

HEALTH_DAYS = (1, 7, 30)                                    # the periods: keys 1/d, 7/w, 3/m (the web: period=1|7|30)
HEALTH_KEYS = {"d": 1, "w": 7, "m": 30}  # the digits are the screens
HEALTH_TTL = 60          # the report is computed at most this often per period, whatever the number of keys or requests
HEALTH_IDLE_S = 600      # the screen left alone this long gives the monitor back to the rotation (nobody may be at the keyboard)
HEALTH_PANE_W = 140      # from this width up the details pane sits beside the findings, below them otherwise
HEALTH_NONE = "no history yet: the collector starts recording when [features] health is on; data appears after the first hour"
DEMO_HEALTH = ""         # --demo-health little|none: the demo with 5 hours of history, or none (demo.HEALTH_VARIANTS)
LEVEL_PILL = {"err": ("✖ ERR ", "1;41;37"), "warn": ("! WARN", "1;43;30"), "info": ("· INFO", "90")}  # a symbol besides the colour
KIND_ORDER = ("oom", "crash", "hang", "unexpected_shutdown", "hw_error", "service_failed", "restart", "exit_error", "throttle", "disk_low",
              "login_fail")
KIND_LABEL = {"oom": "out of memory", "crash": "crash", "hang": "hang", "unexpected_shutdown": "unexpected off", "hw_error": "hardware",
              "service_failed": "service failed", "restart": "restart", "exit_error": "exit error", "throttle": "throttle",
              "disk_low": "disk low", "login_fail": "login failed"}
KIND_COL = {"oom": "31", "unexpected_shutdown": "31", "hw_error": "31", "crash": "33", "hang": "33", "service_failed": "33", "restart": "33",
            "exit_error": "33"}
_HEALTH, _HEALTH_LOCK = {}, threading.Lock()


def health_series(conn, rep):
    """The top_cpu / top_mem rows get "series": CPU seconds / mean RSS of the app per hour (24 h) or per day (longer), oldest first, None
    where nothing was recorded. health.report() has no series: the screen asks app_hour (read-only) itself, once per report. Rows that
    already carry one (the demo, a later report()) are left alone. A history that cannot be read costs the sparklines, nothing else."""
    try:
        per_hour, days = rep["period"]["days"] <= 1, int(rep["period"]["days"])
        rows = [x for k in ("top_cpu", "top_mem") for x in rep.get(k) or [] if isinstance(x, dict) and "series" not in x]
        names = sorted({x["app"] for x in rows})
        if not names:
            return
        h1 = int(rep["period"]["to"] // 3600)
        n = 24 if per_hour else days
        unit = 1 if per_hour else 24
        first = (h1 if per_hour else h1 // 24) - n + 1  # the bucket of the oldest value
        cpu, mem = {}, {}
        for app, b, secs, rss in conn.execute(
                "SELECT app, hour / ?, SUM(cpu_s), AVG(rss_avg) FROM app_hour WHERE hour >= ? AND hour <= ? AND app IN (%s) GROUP BY app, hour / ?"
                % ",".join("?" * len(names)), (unit, first * unit, h1) + tuple(names) + (unit,)):
            if 0 <= b - first < n:
                cpu.setdefault(app, [None] * n)[b - first], mem.setdefault(app, [None] * n)[b - first] = secs, rss
        for x in rep.get("top_cpu") or []:
            if isinstance(x, dict) and "series" not in x:
                x["series"] = cpu.get(x["app"], [])
        for x in rep.get("top_mem") or []:
            if isinstance(x, dict) and "series" not in x:
                x["series"] = [None if v is None else v / 2 ** 20 for v in mem.get(x["app"], [])]  # MB
    except Exception:  # noqa: BLE001 - sparklines are a nicety
        pass


def health_build(days, now):
    """{"report": dict | None, "msg": why there is none, "err": bool, "at": now}: the demo, or the history opened read-only."""
    if DEMO:
        import demo
        return {"report": demo.health_report(DEMO_OS, days, now, variant=DEMO_HEALTH), "msg": "", "err": False, "at": now}
    try:
        import health  # a missing or broken module costs this screen, never the dashboard
        import history
        conn = history.open_ro()
        if conn is None:
            return {"report": None, "msg": HEALTH_NONE, "err": False, "at": now}
        try:
            rep = health.report(conn, days=days)
            health_series(conn, rep)
        finally:
            conn.close()
        return {"report": rep, "msg": "", "err": False, "at": now}
    except Exception as e:  # noqa: BLE001 - say so, and keep the dashboard
        return {"report": None, "msg": "the history could not be read: " + safe(repr(e))[:100], "err": True, "at": now}


def health_data(days):
    """health_build() of the last `days` days (1, 7 or 30), at most once per HEALTH_TTL per period: the console and every web request
    share it, so a key or a page never reads the history. A failure is kept for the same time (no retry on every key)."""
    days = days if days in HEALTH_DAYS else 7
    with _HEALTH_LOCK:
        now = time.time()
        hit = _HEALTH.get(days)
        if hit is None or not 0 <= now - hit["at"] < HEALTH_TTL:
            hit = _HEALTH[days] = health_build(days, now)
        return hit


ADVICE_TTL = 30          # the advisor's cached answer is looked up at most this often per report: a frame must not read a file
ADVICE_LINES = 6         # lines the HEALTH screen has room for under the findings
ADVICE_NONE = "no advice yet: nuc-console-ask --advise asks the local model (this screen never does, it only shows an answer that is there)"
_ADVICE, _ADVICE_LOCK = {}, threading.Lock()


def health_advice(report):
    """(on, the advisor's answer for this report): on = [ai] enabled and its endpoint allowed; the answer is the CACHED one (None: nothing
    cached yet, or {"error"}). It never asks the model: a key or a page must not wait for a generation (nuc-console-ask --advise does that).
    Remembered for ADVICE_TTL per report object. Never raises: a broken advisor is no advice."""
    with _ADVICE_LOCK:
        now, hit = time.time(), _ADVICE.get("hit")
        if hit is not None and hit[0] is report and 0 <= now - hit[1] < ADVICE_TTL:
            return hit[2]
        try:
            import advisor
            cfg = ai_cfg()
            res = (True, advisor.try_advise(report, cfg, cached_only=True)) if advisor.available(cfg)[0] else (False, None)
        except Exception:  # noqa: BLE001
            res = (False, None)
        _ADVICE["hit"] = (report, now, res)
        return res


def health_extra_lines(report, w):
    """The ADVICE block under the findings: the advisor's cached answer (see health_advice), as at most ADVICE_LINES ANSI lines none wider
    than w, or [] when the advisor is off. The web page calls web.health_extra_html(report) at the same place."""
    on_, res = health_advice(report)
    if not on_:
        return []
    try:
        import advisor
        lines = advisor.lines(res, max(20, w - 2)) if res else textwrap.wrap(ADVICE_NONE, max(20, w - 2))
        text = [" " + hclean(x) for x in lines]
    except Exception:  # noqa: BLE001
        return []
    if not res:
        return [c(90, x) for x in text[:ADVICE_LINES]]
    cut = len(text) > ADVICE_LINES
    if cut:
        text = text[:ADVICE_LINES - 1] + [f" … +{len(text) - ADVICE_LINES + 1} more lines: nuc-console-ask --advise"]
    return [c(90, text[0])] + text[1:-1 if cut else None] + ([c(90, text[-1])] if cut else [])


def hansi(line):
    """A line from the advisor hook: its colours (SGR) stay, every other escape sequence and control character becomes '?'."""
    return "".join(x if re.fullmatch(r"\x1b\[[0-9;]*m", x) else hclean(x) for x in re.split(r"(\x1b\[[0-9;]*m)", str(line)))


def hwhen(ts, fmt="%Y-%m-%d %H:%M"):
    """An epoch as UTC (the report's own texts say UTC), '?' when it is not one."""
    try:
        return time.strftime(fmt, time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def hago(ts):
    sec = time.time() - hnum(ts, time.time())
    return "now" if sec < 90 else f"{sec / 60:.0f} min ago" if sec < 5400 else f"{sec / 3600:.0f} h ago" if sec < 129600 else f"{sec / 86400:.0f} d ago"


def hmsg(level, text, w):
    """msg() as one or more lines: it wraps at w instead of running off the screen (the no-history message is long)."""
    rows = textwrap.wrap(hclean(text), max(10, w - 5)) or [""]
    return [msg(level, rows[0])] + ["     " + x for x in rows[1:]]


def hsec(title, w, note=""):
    """section() that drops its note rather than overflowing a narrow column."""
    return section(title, w, note if len(note) + len(title) + 10 <= w else "")


def hbar(frac, w):
    n = round(min(max(frac, 0.0), 1.0) * w)
    return c(36, "█" * n) + c(90, "░" * (w - n))


def hbucket(vals, n):
    """vals as at most n values: the mean of each group (None when a group has no value)."""
    if len(vals) <= n:
        return list(vals)
    step = len(vals) / float(n)
    out = []
    for i in range(n):
        g = [v for v in vals[int(i * step):int((i + 1) * step) or 1] if v is not None]
        out.append(sum(g) / len(g) if g else None)
    return out


def hspark(vals, width):
    """vals (None = not recorded) as small bars scaled to their own maximum, `width` columns; none recorded: blank."""
    vals = hbucket([hnum(v, None) if v is not None else None for v in vals], width)
    top = max([v for v in vals if v is not None] or [0])
    bars = "".join(" " if v is None else SPARK[0] if top <= 0 else SPARK[min(7, int(v / top * 7.999))] for v in vals)
    return pad(bars, width)


def hjoin(items, w, lead="", sep="  ·  "):
    """lead + items (ANSI strings) joined by sep; the ones that do not fit are counted: '… +N'. At least one is always kept (clipped)."""
    keep = list(items)
    while len(keep) > 1 and vlen(lead + sep.join(keep)) + (8 if len(keep) < len(items) else 0) > w:
        keep.pop()
    more = len(items) - len(keep)
    return clip(lead + sep.join(keep) + (c(90, f"  … +{more}") if more else ""), w)


def hcut(lines, n, w):
    """lines as exactly at most n lines: the last one says how many were left out."""
    if len(lines) <= n:
        return lines
    return lines[:max(0, n - 1)] + ([c(90, clip(f" … +{len(lines) - n + 1} more lines", w))] if n else [])


def hrows(rows, n):
    """(the rows to draw, how many are left out) for a list of room n: one row more than n is drawn rather than a '… +1' line."""
    return (rows, 0) if len(rows) <= n + 1 else (rows[:n - 1], len(rows) - n + 1)


def health_findings(R):
    """The findings of the report that can be drawn (a dict with an id), in the report's order (err, warn, info)."""
    return [f for f in (R or {}).get("findings") or [] if isinstance(f, dict) and isinstance(f.get("id"), str)]


def health_nothing(R):
    """Why the list of findings is empty: with less than a day of data no conclusion is not 'all fine'."""
    return ("too little data to conclude anything yet" if hnum((R.get("coverage") or {}).get("hours")) < 24
            else "nothing to report in this period")


def health_level(f):
    return f.get("level") if f.get("level") in LEVEL_PILL else "info"


def hfact(k, v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        if v > 1e9 and (k == "last" or k.endswith("_hour")):  # an epoch
            return hwhen(v) + " UTC"
        v = hnum(v)
        return "%d" % v if v == int(v) else ("%.2f" % v).rstrip("0").rstrip(".")
    return hclean(v, 80)


def health_details(f):
    """What the details of a finding say, as plain cleaned values: (level, title, text, [(fact, value)], fix). Console and web share it."""
    facts = f.get("facts") if isinstance(f.get("facts"), dict) else {}
    return (health_level(f), hclean(f.get("title"), 100), hclean(f.get("text"), 400), [(hclean(k, 40), hfact(str(k), v)) for k, v in facts.items()],
            hclean(f.get("fix"), 600))


def health_find_row(f, w):
    """A finding on one line: level pill, title, the text as far as it fits."""
    label, code = LEVEL_PILL[health_level(f)]
    title, text = hclean(f.get("title"), max(10, w - 12)), hclean(f.get("text"))
    room = w - 11 - len(title)
    return f" {c(code, ' ' + label + ' ')} " + c(1, title) + ("  " + c(90, text if len(text) <= room - 2 else text[:room - 3] + "…") if room >= 14 else "")


def health_counts(fl):
    n = {lv: sum(1 for f in fl if health_level(f) == lv) for lv in LEVEL_PILL}
    bits = [c(col, f"{sym} {n[lv]} {word}") for lv, sym, word, col in (("err", "✖", "err", 31), ("warn", "!", "warn", 33), ("info", "·", "info", 90)) if n[lv]]
    return "  ".join(bits) if bits else c(90, "no findings")


def health_title(R, w, days, fl, selector=True):
    """'── HEALTH  last 7 days · since 2026-09-24 14:00 UTC · 168 h of data · ✖ 1 err ───── d:24h w:7d m:30d': the period, how much history
    the report rests on, the findings per level; the least needed go first when narrow."""
    cov = (R or {}).get("coverage") or {}
    hours = hnum(cov.get("hours"))
    bits = [c(90, "last 24 hours" if days == 1 else f"last {days} days"),
            c(90, (f"since {hwhen(cov.get('since'))} UTC · " if cov.get("since") else "") + f"{hours:.0f} h of data")]
    if R is not None and hours:
        bits.append(health_counts(fl))
    left = c(36, "──") + c("1;36", " HEALTH ") + " "
    right = " ".join(c(7 if d == days else 90, f" {k}:{lab} ") for d, k, lab in ((1, "d", "24h"), (7, "w", "7d"), (30, "m", "30d"))) if selector else ""
    while len(bits) > 1 and vlen(left + c(90, " · ").join(bits)) + vlen(right) + 3 > w:
        bits.pop(1)
    text = left + c(90, " · ").join(bits) + " "
    return clip(text + c(36, "─" * max(0, w - vlen(text) - vlen(right) - (1 if right else 0))) + (" " + right if right else ""), w)


def hb_cpu(R, w, k, days):
    rows, n = [x for x in R.get("top_cpu") or [] if isinstance(x, dict)], (10, 5, 4, 3, 2)[k + 1]
    lines = [hsec("TOP CPU", w, "share of CPU time · per " + ("hour" if days <= 1 else "day"))]
    if not rows:
        return lines + [c(90, " no CPU data")]
    if k == 3:  # one line: the biggest users
        return lines + [hjoin([hclean(x.get("app"), 20) + f" {hnum(x.get('share')) * 100:.0f}%" for x in rows], w, " ", "  ·  ")]
    nm, sw = max(8, min(18, w // 4)), 14 if w < 70 else 24 if w < 100 else 30
    top = max(hnum(x.get("share")) for x in rows) or 1.0  # the bar compares the apps, the number is the share of all CPU time
    rows, hidden = hrows(rows, n)
    for x in rows:
        sh, ser = hnum(x.get("share")), x.get("series")
        items = [pad(hclean(x.get("app"), nm), nm), hbar(sh / top, 8) + f" {sh * 100:3.0f}%", pad(f"avg {hnum(x.get('avg_pct')):.0f}%", 8)]
        if isinstance(ser, list) and ser:
            items.append(c(36, hspark(ser, min(len(ser), sw))))
        if x.get("peak_hour"):
            items.append(c(90, "peak " + hwhen(x["peak_hour"], "%a %H:%M")))
        lines.append(fit_join(items, "  ", w, " "))
    return lines + ([c(90, f" … +{hidden} more")] if hidden else [])


def hb_mem(R, w, k, days):
    rows, n = [x for x in R.get("top_mem") or [] if isinstance(x, dict)], (10, 5, 4, 3, 2)[k + 1]
    lines = [hsec("TOP MEMORY", w, "RSS · per " + ("hour" if days <= 1 else "day"))]
    if not rows:
        return lines + [c(90, " no memory data")]
    rising = lambda x: x.get("trend_mb_day") is not None and hnum(x.get("trend_mb_day")) >= 50  # noqa: E731
    if k == 3:  # one line: the biggest, with an arrow on the ones that keep growing
        return lines + [hjoin([hclean(x.get("app"), 20) + f" {human(hnum(x.get('rss_avg')))}" + (c(33, " ↗") if rising(x) else "") for x in rows], w, " ", "  ·  ")]
    nm, sw = max(8, min(18, w // 4)), 14 if w < 70 else 24 if w < 100 else 30
    trend = any(x.get("trend_mb_day") is not None for x in rows)
    rows, hidden = hrows(rows, n)
    for x in rows:
        t, ser = x.get("trend_mb_day"), x.get("series")
        items = [pad(hclean(x.get("app"), nm), nm), pad(f"avg {human(hnum(x.get('rss_avg')))}", 9), pad(f"max {human(hnum(x.get('rss_max')))}", 9)]
        if trend:
            t = None if t is None else hnum(t)
            items.append(pad(c(90, "no trend") if t is None else c(33, f"↗ {t:+.0f}M/day") if rising(x) else c(90, f"↘ {t:+.0f}M/day") if t <= -50
                             else c(90, "→ steady"), 11))
        if isinstance(ser, list) and ser:
            items.append(c(36, hspark(ser, min(len(ser), sw))))
        lines.append(fit_join(items, "  ", w, " "))
    return lines + ([c(90, f" … +{hidden} more")] if hidden else [])


def hb_events(R, w, k, days):
    ev = R.get("events") if isinstance(R.get("events"), dict) else {}
    kinds = [x for x in KIND_ORDER if ev.get(x)] + sorted(x for x in ev if x not in KIND_ORDER and ev[x])
    lines = [hsec("EVENTS", w, "by kind · subject ×times, last")]
    if not kinds:
        return lines + [c(90, " none recorded in this period")]
    rows = {x: [r for r in ev[x] if isinstance(r, dict)] for x in kinds}
    if k == 3:  # one line: the totals
        return lines + [hjoin([c(KIND_COL.get(x, "90"), hclean(KIND_LABEL.get(x, x), 14)) + f" {hcount(sum(hnum(r.get('n')) for r in rows[x]))}" for x in kinds],
                              w, " ", "  ")]
    nk, ns = (12, 8, 6, 4)[k + 1], (6, 4, 3, 2)[k + 1]
    shown, hidden = hrows(kinds, nk)
    for x in shown:
        items = [hclean(r.get("subject"), 40) + f" ×{hcount(r.get('n'))} " + c(90, hago(r.get("last"))) for r in rows[x]]
        lines.append(hjoin(items[:ns] if len(items) <= ns + 1 else items[:ns - 1], w, " " + c(KIND_COL.get(x, "90"), pad(hclean(KIND_LABEL.get(x, x), 14), 14)) + " ")
                     + (c(90, f"  … +{len(items) - ns + 1}") if len(items) > ns + 1 else ""))
    return lines + ([c(90, f" … +{hidden} more kinds")] if hidden else [])


def hb_logs(R, w, k, days):
    rows, n = [x for x in R.get("logs") or [] if isinstance(x, dict)], (10, 6, 4, 3, 2)[k + 1]
    lines = [hsec("NOISY / NEW LOGS", w, "messages in the period")]
    if not rows:
        return lines + [c(90, " none recorded in this period")]
    rows, hidden = hrows(rows, n)
    ww = max(8, max(len(hclean(x.get("unit") or x.get("source"), 16)) for x in rows))
    for x in rows:
        head = f" {hcount(x.get('n')):>6}  " + (c(33, "NEW") if x.get("new") else "   ") + " " + pad(hclean(x.get("unit") or x.get("source"), 16), ww) + " "
        room = w - vlen(head)
        tpl = hclean(x.get("template"))
        lines.append(head + c(90, tpl if len(tpl) <= room else tpl[:max(room - 1, 0)] + "…"))
    return lines + ([c(90, f" … +{hidden} more")] if hidden else [])


def hb_disks(R, w, k, days):
    rows, n = [x for x in R.get("disks") or [] if isinstance(x, dict)], (10, 6, 4, 3, 2)[k + 1]
    lines = [hsec("DISKS", w, "used · days to full at the current growth")]
    if not rows:
        return lines + [c(90, " no disk data")]
    full = lambda x: x.get("days_to_full")  # noqa: E731

    def when(x):
        d = full(x)
        if d is None:
            return c(90, "no trend")
        d = hnum(d)
        return c(31 if d < 7 else 33 if d < 30 else 90, "full now" if d < 1 / 24.0 else f"full in {d * 24:.0f} h" if d < 1 else f"full in {d:.0f} d" if d < 365 else "full in > 1 y")
    if k == 3:
        return lines + [hjoin([hclean(x.get("mount"), 14) + f" {hnum(x.get('used_pct')):.0f}%" + ("" if full(x) is None else " " + when(x)) for x in rows],
                              w, " ", "  ·  ")]
    rows, hidden = hrows(rows, n)
    mw = min(14, max(6, max(len(hclean(x.get("mount"))) for x in rows)))
    bw = max(6, min(24, w - mw - 26))
    for x in rows:
        pct = hnum(x.get("used_pct"))
        lines.append(f" {pad(hclean(x.get('mount'), mw), mw)} {bar(pct / 100.0, bw)} {pct:3.0f}%  {when(x)}")
    return lines + ([c(90, f" … +{hidden} more")] if hidden else [])


def hb_thermal(R, w, k, days):
    th = R.get("thermal") if isinstance(R.get("thermal"), dict) else {}
    hot, top = hnum(th.get("hours_hot")), th.get("max")
    lines = [hsec("THERMAL", w, "hours at or above the temperature limit")]
    if top is None:  # no sensor: unknown is not "cool"
        return lines + [" " + c(33, "?") + c(90, " no temperature data in this period")]
    apps = [f"{hclean(a.get('app'), 20)} {hnum(a.get('share')) * 100:.0f}%" for a in th.get("apps_when_hot") or [] if isinstance(a, dict)]
    head = (c(31 if hot >= 24 else 33, f"{hot:.0f} h hot") if hot else c(90, "never hot")) + c(90, f"  ·  max {hnum(top):.0f} °C")
    if k == 3 or not apps:
        return lines + [hjoin([head] + ([c(90, "when hot: " + ", ".join(apps[:3]))] if apps else []), w, " ", "  ·  ")]
    return lines + [" " + head, hjoin(apps, w, " " + c(90, "when hot: "), "  ·  ")]


def hb_boots(R, w, k, days):
    rows = [x for x in R.get("boots") or [] if isinstance(x, dict)]
    lines = [hsec("BOOTS", w, "boot time")]
    vals = [hnum(x.get("total_s"), None) if x.get("total_s") is not None else None for x in rows]
    known = sorted(v for v in vals if v)
    if not known:
        return lines + [c(90, " no boot times recorded" if not rows else " ? boot times unknown")]
    last = vals[-1]
    med = known[len(known) // 2] if len(known) % 2 else (known[len(known) // 2 - 1] + known[len(known) // 2]) / 2.0
    return lines + [" " + c(36, hspark(vals, min(len(vals), 21))) + "  " + c(90, "last " + ("?" if not last else f"{last:.0f} s") + f" · median {med:.0f} s · {plural(len(vals), 'boot')}")]


HEALTH_BLOCKS = {"cpu": hb_cpu, "mem": hb_mem, "events": hb_events, "logs": hb_logs, "disks": hb_disks, "thermal": hb_thermal, "boots": hb_boots}
HEALTH_GROUPS = {3: (("cpu", "mem"), ("events", "logs"), ("disks", "thermal", "boots")),
                 2: (("cpu", "mem", "thermal", "boots"), ("events", "logs", "disks")),
                 1: (("cpu", "mem", "events", "logs", "disks", "thermal", "boots"),)}


def health_columns(R, w, k):
    """The sections under the findings at level k (-1 everything, 0 the usual, 3 one or two lines each), one list of lines per column:
    1 column up to 109 wide, 2 up to 189, 3 from 190."""
    ncol = 3 if w >= 190 else 2 if w >= 110 else 1
    cw, days = (w - 3 * (ncol - 1)) // ncol, int(hnum((R.get("period") or {}).get("days"), 7))
    cols = []
    for names in HEALTH_GROUPS[ncol]:
        col = []
        for name in names:
            try:
                blk = HEALTH_BLOCKS[name](R, cw, k, days)
            except Exception as e:  # noqa: BLE001 - one odd row must not blank the other sections
                blk = [c(33, f" {name}: could not be shown: " + safe(repr(e))[:cw - 40])]
            col += ([""] if col and k <= 1 else []) + [clip(x, cw) for x in blk]
        cols.append(col)
    return cols, ncol, cw


def health_tables(R, w, h=None):
    """The sections under the findings as lines: the fullest level that fits h lines (None: everything, the web page scrolls)."""
    for k in ((-1,) if h is None else (0, 1, 2, 3)):
        cols, ncol, cw = health_columns(R, w, k)
        if h is None or max(len(x) for x in cols) <= h:
            break
    cols = [hcut(x, h, cw) for x in cols] if h is not None else cols
    return columns([(x, cw) for x in cols], w, gap=3) if ncol > 1 else cols[0]


def health_pane(f, w, h):
    """Everything known about a finding, w columns, h lines at most: the text and the fix wrap, what does not fit is counted."""
    level, title, text, facts, fix = health_details(f)
    lw = 7
    out = [section("DETAILS", w), " " + cc("1;" + LV_COL.get(level, ""), title)]
    for label, body in (("what", text), ("facts", ""), ("fix", fix)):
        if label == "facts":
            rows = wrap_items([f"{k} {c(1, v)}" for k, v in facts], w, lw + 1, "  ·  ") if facts else []
            out += [" " + c(90, pad(label, lw)) + r[lw + 1:] if i == 0 else r for i, r in enumerate(rows)]
            continue
        chunks = textwrap.wrap(body, max(8, w - lw - 1), break_on_hyphens=False) or ["?"]
        out += [(" " + c(90, pad(label, lw)) if i == 0 else " " * (lw + 1)) + x for i, x in enumerate(chunks)]
    return [clip(x, w) for x in hcut(out, h, w)]


class HealthView(object):
    """The interactive Health screen: the period, the selected finding (its id survives refreshes and period changes; its index is where the
    cursor stays when that finding vanishes), the scroll position, the details pane, when it was opened and last touched."""

    def __init__(self, days=7, now=None):
        self.days, self.cur, self.idx, self.top, self.details, self.rows = days, None, 0, 0, False, 10
        self.opened = self.touched = now or time.time()


def health_sync(hv, fl):
    """The cursor back on its finding: by id, else the same index (clamped). Returns the index."""
    i = next((j for j, f in enumerate(fl) if f["id"] == hv.cur), None) if hv.cur else None
    hv.idx = i if i is not None else max(0, min(hv.idx, len(fl) - 1))
    hv.cur = fl[hv.idx]["id"] if fl else None
    return hv.idx


def health_key(hv, key, fl):
    """One key on the Health screen (what each key does: ui.KEYMAP, scope health). Returns 'back' (leave it: Esc or q when no details pane
    is open), 'period' (another period: the report is asked for again, from its cache) or '' (only the cursor or the details pane changed)."""
    act = ui.action("health", key)
    if act == "back":
        if hv.details:
            hv.details = False
            return ""
        return "back"
    if act == "period":
        days, hv.days = hv.days, HEALTH_KEYS[key]
        return "period" if days != hv.days else ""
    if act == "details":
        hv.details = not hv.details
        return ""
    if not fl or act not in ("move", "page"):
        return ""
    i, page = health_sync(hv, fl), max(1, hv.rows - 1)
    i = {"up": i - 1, "k": i - 1, "down": i + 1, "j": i + 1, "pgup": i - page, "pgdn": i + page, "home": 0, "end": len(fl) - 1}.get(key, i)
    hv.idx = max(0, min(i, len(fl) - 1))
    hv.cur = fl[hv.idx]["id"]
    return ""


def health_body(data, hv, fl, w, h):
    """The Health screen's body: at most h lines, none wider than w. Title, notes, findings (the cursor's row in reverse), the advisor's
    ADVICE block, then the details pane (below the findings, beside them from HEALTH_PANE_W) or the sections of tables."""
    R = data["report"]
    if R is None:  # no history, or it could not be read
        return [health_title(None, w, hv.days, [])] + hmsg("err" if data.get("err") else "info", data["msg"], w)
    out = [health_title(R, w, hv.days, fl)]
    notes = [hclean(x) for x in R.get("notes") or [] if isinstance(x, str)]
    if not (R.get("coverage") or {}).get("since"):  # nothing recorded yet: the message below says it
        notes = [x for x in notes if x != "no history yet"]
    nn = 2 if h >= 30 else 1
    shown = notes if len(notes) <= nn else notes[:nn - 1] + [f"… +{len(notes) - nn + 1} more notes"]
    out += [clip(c(90, "  · " + x), w) for x in shown]
    if not hnum((R.get("coverage") or {}).get("hours")):
        return (out + hmsg("info", "no data in this period" if (R.get("coverage") or {}).get("since") else HEALTH_NONE, w))[:h]
    adv = [clip(hansi(x), w) for x in health_extra_lines(R, w)[:6]]
    avail = h - len(out) - (len(adv) + 1 if adv else 0)
    nf, pane = len(fl), bool(hv.details and fl)
    side = pane and w >= HEALTH_PANE_W
    tmin = max(len(x) for x in health_columns(R, w, 3)[0])  # the sections at their shortest
    nothing = hmsg("info", health_nothing(R), w) if not nf else []  # never a green: what is not recorded is not fine
    if not nf:
        area = 1 + len(nothing)
    elif side:  # as tall as the details need (at least the list), leaving the sections their shortest form
        health_sync(hv, fl)
        area = max(6, min(avail - tmin, max(1 + nf, len(health_pane(fl[hv.idx], w - int(w * 0.45) - 3, 99)))))
    elif pane:
        area = 1 + max(1, min(nf, avail // 3, 8))
    else:
        area = 1 + max(min(nf, 3), min(nf, avail - 1 - tmin))
    rows = area - 1
    fw = int(w * 0.45) if side else w
    hv.rows = max(1, rows)
    head = [section("FINDINGS", fw, "")]
    if not nf:
        head += nothing
        lst = []
    else:
        health_sync(hv, fl)
        hv.top = map_scroll(hv.top, hv.idx, nf, rows)
        lst = [c(7, pad(ANSI.sub("", clip(health_find_row(f, fw), fw)), fw)) if j == hv.idx else clip(health_find_row(f, fw), fw)
               for j, f in enumerate(fl) if hv.top <= j < hv.top + rows]
    block = head + lst
    if side:
        pw = w - fw - 3
        pn = health_pane(fl[hv.idx], pw, area)
        block = [pad(x, fw) + c(90, " │ ") + (pn[i] if i < len(pn) else "") for i, x in enumerate(block + [""] * (area - len(block)))]
    out += block
    if adv:
        out += [section("ADVICE", w)] + adv
    if pane and not side:
        out += health_pane(fl[hv.idx], w, avail - area)
    else:
        out += health_tables(R, w, max(0, avail - area))
    return [clip(x, w) for x in out[:h]]


def health_footer(hv, n, w):
    """Where the cursor is, and the keys (ui.KEYMAP): in short words when the screen is narrow, then the least needed go first."""
    pos = f"finding {hv.idx + 1}/{n}" if n else "no findings"
    dyn = {"details": ("Enter: " + ("hide details" if hv.details else "details"), "Enter: " + ("hide" if hv.details else "details"))}
    return clip(c(90, ui.footer("health", f" {pos}   ", w, on, dyn)), w)


def health_screen(data, pb, hv, w, h):
    """(the interactive Health screen as one frame: header, body, key help; its findings): the live loop and --once."""
    fl = health_findings(data["report"])
    health_sync(hv, fl)
    body = health_body(data, hv, fl, w, h - 2)
    return frame(("Health", 1, 1, body), 0, 1, w, h, pb, foot=health_footer(hv, len(fl), w)), fl


def health_slide(w, body_h):
    """The Health screen among the rotating pages ([dashboard] health_in_rotation): no cursor; the findings that fit (the rest counted),
    then the top CPU and memory users."""
    data = health_data(7)
    R = data["report"]
    if R is None:
        return ([section("HEALTH", w)] + hmsg("err" if data.get("err") else "info", data["msg"], w))[:body_h]
    fl = health_findings(R)
    out = [health_title(R, w, 7, fl, selector=False)]
    notes = [hclean(x) for x in R.get("notes") or [] if isinstance(x, str) and (R.get("coverage") or {}).get("since")]
    if notes and body_h >= 10:  # "collecting: 5 hours so far": a monitor nobody types on must not look conclusive
        out.append(clip(c(90, "  · " + notes[0]), w))
    avail = body_h - len(out)
    if not hnum((R.get("coverage") or {}).get("hours")):
        return (out + hmsg("info", "no data in this period" if (R.get("coverage") or {}).get("since") else HEALTH_NONE, w))[:body_h]
    ncol, apps = (2 if w >= 110 else 1), []
    cw, days = ((w - 3) // 2 if ncol == 2 else w), int(hnum((R.get("period") or {}).get("days"), 7))
    for k in (0, 1, 2, 3):
        cols = [hb_cpu(R, cw, k, days), hb_mem(R, cw, k, days)]
        cand = columns([(x, cw) for x in cols], w, gap=3) if ncol == 2 else cols[0] + cols[1]
        if len(cand) <= avail - 3:  # the findings keep at least a title and two rows
            apps = cand
            break
    rows = max(0, avail - len(apps) - 1)
    out.append(section("FINDINGS", w))
    if not fl:
        out += hmsg("info", health_nothing(R), w)
    elif rows:
        out += [clip(health_find_row(f, w), w) for f in fl[:rows if len(fl) <= rows else rows - 1]]
        if len(fl) > rows:
            out.append(c(90, f"   … +{len(fl) - rows + 1} more findings"))
    return [clip(x, w) for x in (out + apps)[:body_h]]


def health_select(fl, hv, text):
    """The cursor on the first finding whose id or title contains text (any case). False: none does."""
    t = text.lower()
    i = next((j for j, f in enumerate(fl) if t in (f["id"] + " " + str(f.get("title", ""))).lower()), None)
    if i is None:
        return False
    hv.idx, hv.cur = i, fl[i]["id"]
    return True


def health_state(smp, days):
    """(the cached report data, the header's problems): shared by the console loop and --once."""
    st = snapshot(0)
    sm = host_sample(smp)
    if DEMO:
        demo_defaults()
    return health_data(days), safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


def health_once(argv, w, h):
    """`--once --view health`: the Health screen as the console draws it (tests, screenshots). --period 1|7|30 (days, default 7),
    --select TEXT: the cursor on the first finding whose id or title contains TEXT, --details: its details pane (Enter).
    --demo-health little|none: the demo with 5 hours of history, or with none. None: nothing to draw (the error is printed)."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    if opt("--period") not in ("", "1", "7", "30"):
        print("--period must be 1, 7 or 30 (days)", file=sys.stderr)
        return None
    hv = HealthView(int(opt("--period") or 7))
    hv.details = "--details" in argv
    data, pb = health_state(None if DEMO else Sampler(), hv.days)
    if opt("--select"):
        health_select(health_findings(data["report"]), hv, opt("--select"))
    return health_screen(data, pb, hv, w, h)[0]


# ---- AI screen: what this machine can run, and which local model to choose (aisetup.catalog(): the console, --once, the web page) ---
# Data: aisetup.catalog() (docs/AI.md): {hw, dir, runtime, recommended, active, models: [{..., assess, installed, pinned, commands}]}.
# Everything in it is data (a model's name comes from a catalog file, a GPU's from a driver): text goes through hclean(), numbers through
# num(), a value that is missing is drawn as "?". The screen runs nothing and downloads nothing: it shows the commands to type.

AI_PANE_W = 140          # from this width up the details sit beside the list, below it otherwise
AI_IDLE_S = 600          # the screen left alone this long gives the monitor back to the rotation (nobody may be at the keyboard)
AI_TTL = 10              # the catalog is read at most this often, whatever the number of keys or requests
AI_PROBE_TTL = 60        # does the model server answer? looked at most this often ...
AI_PROBE_TIMEOUT = 1.0   # ... for at most this long ...
AI_PROBE_STUCK = 30      # ... in a thread of its own (a key or a page never waits for it; one stuck this long is given up)
AI_ID_MAX = 64           # characters of a model id: it is a cursor, and a value in a URL
AI_NAME_MIN = 24         # the name column keeps this much (or its longest name) before another column is given up
AI_NONE = "the model catalog could not be read"
AI_VERDICT = {  # verdict -> (label: a symbol besides the colour, SGR of the pill, web class, SGR of the text)
    "gpu": ("✔ FITS GPU", "1;42;30", "g", "32"), "partial": ("◐ GPU+CPU", "1;46;30", "c", "36"), "ram": ("✔ FITS RAM", "1;42;30", "g", "32"),
    "slow": ("! SLOW", "1;43;30", "y", "33"), "no": ("✖ TOO BIG", "1;41;37", "r", "31")}
AI_UNKNOWN = ("? UNKNOWN", "90", "d", "90")
AI_BACKEND = {"cuda": "CUDA", "rocm": "ROCm", "metal": "Metal", "vulkan": "Vulkan"}
_AI, _AI_LOCK = {}, threading.Lock()
_AIPROBE, _AIPROBE_LOCK = {"res": None, "at": 0.0, "key": None, "thread": None, "started": 0.0}, threading.Lock()


def ai_build(now):
    """{"cat": dict | None, "msg": why there is none, "err": bool, "at": now}: the demo's machine, or aisetup.catalog() (it reads the
    hardware and the files of the AI directory, never the network)."""
    if DEMO:
        return {"cat": ai_engine().demo_catalog(DEMO_OS), "msg": "", "err": False, "at": now}  # the invented machine, as the simulated actions left it
    try:
        import aisetup  # a missing or broken module costs this screen, never the dashboard
        cat = aisetup.catalog()  # the folder the buttons work in (aisetup.work_dir()), the active model as the AI page chose it
        if not isinstance(cat, dict):
            raise TypeError("catalog() gave no dict")
        return {"cat": cat, "msg": "", "err": False, "at": now}
    except Exception as e:  # noqa: BLE001 - say so, and keep the dashboard
        return {"cat": None, "msg": AI_NONE + ": " + safe(repr(e))[:100], "err": True, "at": now}


def ai_data():
    """ai_build() at most once per AI_TTL: the console and every web request share it, so a key or a page does not read the hardware
    files again. A failure is kept for the same time (no retry on every key)."""
    with _AI_LOCK:
        now, key = time.time(), (DEMO, DEMO_OS)
        hit = _AI.get("hit")
        ver = ai_version()
        if hit is None or hit["key"] != key or not 0 <= now - hit["at"] < AI_TTL or hit.get("ver", ver) != ver:  # a job moved: read it again
            hit = _AI["hit"] = dict(ai_build(now), key=key, ver=ver)
        return hit


def ai_report(days):
    """The HEALTH report of the last `days` days, from the history the screens share (health_data): what "advice now" is asked about."""
    import advisor
    data = health_data(days)
    if data.get("report") is None:
        raise advisor.NoHistory(str(data.get("msg") or "no history yet"))
    return data["report"]


def ai_engine():
    """The engine of this process (src/aiweb.py), told where this program's settings and HEALTH report are (it must not import render: it may be
    __main__): the real one, or while --demo is on the demo's: the invented machines act on an engine of their own, in memory (nothing is
    downloaded, started or written)."""
    import aiweb
    aiweb.bind(cfg=lambda: CFG, report=ai_report)
    eng = aiweb.engine()
    return eng if eng.demo == bool(DEMO) else aiweb.configure(demo=bool(DEMO))


def ai_version():
    """The engine's change counter (a download ended, a server started...): the catalog is read again when it moves. 0 without an engine."""
    try:
        import aiweb
        return aiweb.version()
    except Exception:  # noqa: BLE001
        return 0


def ai_cfg():
    """The advisor's settings as the AI page and screen left them: config.ini with web.json laid over it (advisor.effective_cfg)."""
    try:
        import advisor
        import aiweb
        return advisor.effective_cfg(CFG, aiweb.engine().web_path())
    except Exception:  # noqa: BLE001 - a broken module is config.ini's word
        return CFG


def ai_probe_run(ai):
    """One look at the model server of [ai] endpoint: {"state": "answering" | "down", "msg", "models"}. Never raises."""
    try:
        import advisor
        info = advisor.endpoint_info(ai.get("endpoint"), bool(ai.get("allow_remote")))
        models = advisor.list_models(info, AI_PROBE_TIMEOUT)
        return {"state": "answering", "msg": "", "models": [hclean(x, 80) for x in models[:20]]}
    except Exception as e:  # noqa: BLE001 - an AdvisorError says what to do about it; anything else is only named
        return {"state": "down", "msg": hclean(str(e) if hasattr(e, "exit_code") else type(e).__name__, 160), "models": []}


def ai_probe_work(ai, key):
    res = ai_probe_run(ai)
    with _AIPROBE_LOCK:
        _AIPROBE.update(res=res, at=time.time(), key=key)


def ai_probe(wait=0.0):
    """Does the model server answer? The last probe, or None while there is none yet. A probe older than AI_PROBE_TTL is made again in a thread
    of its own and the older answer stays until the new one is in: a key or a page never waits for the network (wait: --once, which may).
    Nothing is asked while [ai] enabled = no."""
    ai = (CFG if DEMO else ai_cfg()).get("ai") or {}
    if not ai.get("enabled"):
        return {"state": "off", "msg": "[ai] enabled = no in config.ini", "models": []}
    key = (str(ai.get("endpoint")), bool(ai.get("allow_remote")))
    with _AIPROBE_LOCK:
        now, st = time.time(), _AIPROBE
        t = st["thread"]
        if (st["key"] != key or not 0 <= now - st["at"] < AI_PROBE_TTL) and (t is None or not t.is_alive() or now - st["started"] > AI_PROBE_STUCK):
            t = st["thread"] = threading.Thread(target=ai_probe_work, args=(dict(ai), key), daemon=True)
            st["started"] = now
            t.start()
    if wait and t is not None:
        t.join(wait)
    with _AIPROBE_LOCK:
        return st["res"] if st["key"] == key else None


def ai_status(wait=0.0):
    """What the STATUS section reads: {enabled, endpoint, model, probe}: [ai] as config.ini has it, and the last probe of its server."""
    if DEMO:
        eng = ai_engine()
        st = eng.demo_status(DEMO_OS)
        st["snap"] = eng.snapshot()
        st["switch"] = st["snap"]["switch"]
        return st
    ai = ai_cfg().get("ai") or {}
    out = {"enabled": bool(ai.get("enabled")), "endpoint": hclean(ai.get("endpoint"), 120), "model": hclean(ai.get("model"), 80), "probe": ai_probe(wait)}
    try:
        out["snap"] = ai_engine().snapshot()
        out["switch"] = out["snap"]["switch"]
    except Exception:  # noqa: BLE001
        pass
    return out


def ai_state(smp):
    """(the catalog's data, the header's problems): shared by the console loop, --once and the web page."""
    st = snapshot(0)
    sm = host_sample(smp)
    if DEMO:
        demo_defaults()
    return ai_data(), safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


def ai_id(x):
    return hclean(x, AI_ID_MAX) if isinstance(x, str) and x.strip() else None


def ai_mb(x):
    """Megabytes (MiB) as the usual words: 400 MB, 4.9 GB, 128 GB; '?' when it is not a number."""
    x = num(x)
    if x is None:
        return "?"
    return f"{x:.0f} MB" if x < 1024 else f"{x / 1024:.1f} GB" if x < 102400 else f"{x / 1024:.0f} GB"


def ai_params(p, a):
    """'8.2B', or '30B-A3B' for a mixture of experts (3B parameters work per token): the size of the model in billions of parameters."""
    p, a = num(p), num(a)
    return "?" if p is None else f"{p:g}B" + (f"-A{a:g}B" if a else "")


def ai_tok(t):
    """'40-70' tokens per second: an estimate, said so in the header, the legend and the details; '-' when there is none."""
    if not t:
        return "-"
    f = lambda x: f"{x:.0f}" if x >= 10 else f"{x:.1f}".rstrip("0").rstrip(".")  # noqa: E731
    return f(t[0]) if f(t[0]) == f(t[1]) else f"{f(t[0])}-{f(t[1])}"


def ai_rows(cat):
    """The models of a catalog as plain rows, best first (rank, then the catalog's own order): every text cleaned, every number checked.
    The id of a row is what the cursor and the web's sel= are made of, so the same text is never in two rows. Console and web share it."""
    cat = dd(cat)
    rec, act = ai_id(cat.get("recommended")), ai_id(cat.get("active"))
    rows, seen = [], set()
    for i, m in enumerate(cat.get("models") if isinstance(cat.get("models"), list) else []):
        mid = ai_id(m.get("id")) if isinstance(m, dict) else None
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        a, cmds, tok = dd(m.get("assess")), dd(m.get("commands")), dd(m.get("assess")).get("tok_s")
        tok = (num(tok[0]), num(tok[1])) if isinstance(tok, (list, tuple)) and len(tok) == 2 else None
        size = num(m.get("approx_mb"))
        if size is None and num(m.get("size")) is not None:
            size = num(m.get("size")) / 2 ** 20
        rows.append({
            "id": mid, "i": i, "name": hclean(m.get("name") or mid, 60), "rank": num(m.get("rank")), "params": ai_params(m.get("params_b"), m.get("active_b")),
            "size_mb": size, "need_mb": num(a.get("need_mb")), "verdict": a.get("verdict") if a.get("verdict") in AI_VERDICT else None,
            "where": hclean(a.get("where"), 12), "gpu_layers": num(a.get("gpu_layers")), "layers": num(m.get("layers")),
            "tok": tok if tok and None not in tok else None, "why": hclean(a.get("why"), 300), "license": hclean(m.get("license"), 40),
            "quant": hclean(m.get("quant"), 20), "ctx_max": num(m.get("ctx_max")), "notes": hclean(m.get("notes"), 200),
            "installed": bool(m.get("installed")), "pinned": bool(m.get("pinned")), "rec": mid == rec, "active": mid == act,
            "commands": {k: hclean(cmds.get(k), 200) for k in ("install", "use", "remove") if isinstance(cmds.get(k), str) and cmds[k].strip()}})
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0.0, r["i"]))
    return rows


class AiView(object):
    """The interactive AI screen: the selected model (its id survives refreshes; its index is where the cursor stays when that model
    vanishes), the scroll position, the details pane, when it was opened and last touched."""

    def __init__(self, now=None):
        self.cur, self.idx, self.top, self.details, self.rows = None, 0, 0, False, 10
        self.confirm = None   # (kind, model id, the question) while a key waits for y or n
        self.msg = None       # (level, text): a line only this screen says (the engine's own answers are in its snapshot)
        self.opened = self.touched = now or time.time()


def ai_sync(av, rows):
    """The cursor back on its model: by id, else the same index (clamped); the first time on the recommended one, where Enter shows what
    to type. Returns the index."""
    if av.cur is None and rows:
        av.idx = next((j for j, r in enumerate(rows) if r["rec"]), 0)
    else:
        i = next((j for j, r in enumerate(rows) if r["id"] == av.cur), None)
        av.idx = i if i is not None else max(0, min(av.idx, len(rows) - 1))
    av.cur = rows[av.idx]["id"] if rows else None
    return av.idx


AI_ACTIONS = ("toggle", "use", "delete", "delete-all", "cancel")  # AI on/off, use the model, delete it, delete all, cancel (ui.KEYMAP, scope ai)


def ai_key(av, key, rows):
    """One key on the AI screen. Returns 'back' (leave it), an action for ai_do() ('toggle', 'use', 'delete', 'delete-all', 'cancel', and 'yes' for the
    question that is waiting) or '' (only the cursor or the details pane changed). What each key does: ui.KEYMAP, scope ai."""
    if av.confirm:  # a question is waiting: y does it, any other key says no
        yes = key in ("y", "Y")
        if not yes:
            av.confirm = None
        return "yes" if yes else ""
    act = ui.action("ai", key)
    if act == "back":
        if av.details:  # Esc closes the details pane first
            av.details = False
            return ""
        return "back"
    if act in AI_ACTIONS:
        return act
    if act == "details":
        av.details = not av.details
        return ""
    if not rows or act not in ("move", "page"):
        return ""
    i, page = ai_sync(av, rows), max(1, av.rows - 1)
    i = {"up": i - 1, "k": i - 1, "down": i + 1, "j": i + 1, "pgup": i - page, "pgdn": i + page, "home": 0, "end": len(rows) - 1}.get(key, i)
    av.idx = max(0, min(i, len(rows) - 1))
    av.cur = rows[av.idx]["id"]
    return ""


def ai_select(rows, av, text):
    """The cursor on the first model whose id or name contains text (any case). False: none does."""
    t = text.strip().lower()
    i = next((j for j, r in enumerate(rows) if t and t in (r["id"] + " " + r["name"]).lower()), None)
    if i is None:
        return False
    av.idx, av.cur = i, rows[i]["id"]
    return True


def ai_do(av, act, rows):
    """What a key of the AI screen asked (ai_key): done through the engine, which works in the background (a download, the server, the answers
    come back as its snapshot: ai_work_lines draws them), or a question first (av.confirm, answered with y). A line only this screen says
    goes in av.msg. Never raises: an engine that cannot start is a line."""
    av.msg = None
    try:
        import aiweb
        eng = ai_engine()
        if act == "yes":
            kind, mid, _q = av.confirm or (None, None, None)
            av.confirm = None
            return {"on": lambda: eng.turn_on(mid), "delete": lambda: eng.delete(mid), "delete-all": eng.delete_all}.get(kind, lambda: None)()
        if eng.locked():
            av.msg = ("err", aiweb.LOCKED)
            return None
        row = rows[ai_sync(av, rows)] if rows else None
        if act == "cancel":
            return eng.cancel()
        if act == "toggle":
            snap = eng.snapshot()
            if snap["state"][0] == "working":
                return eng.cancel()
            if snap["switch"]["on"]:
                return eng.turn_off()
            ch = eng.choice()
            if ch["model"] is None and ch["recommended"]:  # nothing chosen yet: ask about the recommended one first, naming its size
                t = next((r for r in rows if r["id"] == ch["recommended"]), None)
                todo = "installed here" if ch["installed"] else (f"{ai_mb((ch['size'] or 0) / 2 ** 20)} to download" if ch["size"] else "not downloadable yet")
                av.confirm = ("on", ch["recommended"], f"Turn AI on with {t['name'] if t else ch['recommended']} ({todo})?")
                return None
            return eng.turn_on()
        if row is None:
            av.msg = ("warn", "no model is selected")
            return None
        if act == "use":
            return eng.use_model(row["id"])
        if act == "delete":
            if not row["installed"]:
                av.msg = ("warn", f"{row['name']} is not installed: nothing to delete")
                return None
            av.confirm = ("delete", row["id"], f"Delete the files of {row['name']} ({ai_mb(row['size_mb'])})?")
        elif act == "delete-all":
            used = dd(dd(ai_data()["cat"]).get("space")).get("used")
            if not used and not any(r["installed"] for r in rows):
                av.msg = ("warn", "nothing is downloaded: nothing to delete")
                return None
            av.confirm = ("delete-all", None, f"Delete the runtime and every downloaded model ({aisetup_size(used)})?")
    except Exception as e:  # noqa: BLE001 - the screen goes on
        av.msg = ("err", "the AI engine could not do that: " + hclean(repr(e), 120))
    return None


def aisetup_size(n):
    """5000000000 -> '5.0 GB' ('nothing' for none): sizes of files, as aisetup says them."""
    try:
        import aisetup
        return aisetup.fmt_size(n) if n else "nothing"
    except Exception:  # noqa: BLE001
        return "?"


def ai_busy():
    """Something is running (a download, the server starting, an answer): the screen looks again every second. False without an engine."""
    try:
        return bool(ai_engine().snapshot()["busy"])
    except Exception:  # noqa: BLE001
        return False


def ai_work_lines(st, cat, av, w, k=0):
    """The lines under the title: whether the AI is on and what it is doing (a download with its bar, the server starting, an error), that it is locked,
    the answer to the last key, and the folder the models are downloaded to with what it holds and what is free. At level k the folder goes first
    when the screen is small (k 1), then the lock and the answer (k 2), the state line stays (k 3). [] when the status has no engine snapshot."""
    snap = dd(st).get("snap")
    if not isinstance(snap, dict) or not isinstance(snap.get("state"), (list, tuple)):
        return []
    state, text = snap["state"]
    mark, col = {"off": ("○ OFF", "90"), "working": ("◐ WORKING", "36"), "running": ("● ON", "32"), "on": ("● ON", "32"), "error": ("✖ ERROR", "31")}.get(state, ("?", "90"))
    job, extra = snap.get("job"), ""
    if state == "working" and job and job.get("total"):
        n = min(14, round(14 * job["done"] / job["total"]))
        extra = " " + c(36, "█" * n) + c(90, "░" * (14 - n))
    tail = c(90, "   (c: cancel)") if state == "working" and not snap.get("locked") else ""
    lock = c(33, "[locked by config.ini] ") if snap.get("locked") and k >= 2 else ""  # the line that says it is gone: it goes up front, where a narrow screen keeps it
    out = [clip(" " + c("1;" + col, pad(mark, 10)) + " " + lock + hclean(text, 200) + extra + tail, w)]
    if k >= 3:
        return out
    note = av.msg if av is not None and av.msg else None
    if note is None and isinstance(snap.get("notice"), dict):
        note = ("ok" if snap["notice"].get("ok") else "err", snap["notice"].get("text"))
    if snap.get("locked") and k < 2:
        out.append(clip(" " + c(33, "locked by config.ini ([ai] web_actions = no): this screen only shows"), w))
    if note and note[1] and k < 3:
        out.append(clip(" " + c({"ok": 32, "err": 31, "warn": 33}.get(note[0], 90), hclean(note[1], 300)), w))
    cat = dd(cat)
    if cat.get("dir") and k < 1:
        sp = dd(cat.get("space"))
        free = num(sp.get("free"))
        out.insert(1, clip(" " + c(90, "folder") + " " + hclean(cat["dir"], 120) + c(90, f" · {aisetup_size(sp.get('used'))} downloaded · "
                                                                                   + (f"{aisetup_size(free)} free on that disk" if free else "free space unknown")), w))
    return out


def ai_title(rows, w):
    """'── AI  what this machine can run · 12 models · ✔ 7 fit  ! 2 slow  ✖ 2 too big': the tagline goes first when narrow, then the count."""
    n = {v: sum(1 for r in rows or [] if r["verdict"] == v) for v in AI_VERDICT}
    counts = "  ".join(c(col, f"{sym} {k} {word}") for k, sym, word, col in ((n["gpu"] + n["ram"], "✔", "fit", 32), (n["partial"], "◐", "gpu+cpu", 36),
                                                                           (n["slow"], "!", "slow", 33), (n["no"], "✖", "too big", 31)) if k)
    tag, count = c(90, "what this machine can run"), c(90, plural(len(rows or []), "model"))
    left = c(36, "──") + c("1;36", " AI ") + " "
    for bits in ([tag, count, counts], [count, counts], [counts], [count]) if rows is not None else ([],):
        bits = [x for x in bits if x]
        if vlen(left + c(90, " · ").join(bits)) + 3 <= w:
            break
    text = left + c(90, " · ").join(bits) + " "
    return clip(text + c(36, "─" * max(0, w - vlen(text))), w)


def ai_simd(cpu):
    flags = {x.lower() for x in cpu.get("flags") if isinstance(x, str)} if isinstance(cpu.get("flags"), list) else set()
    return [n for n, ok in (("AVX2", "avx2" in flags), ("AVX-512", any(x.startswith("avx512") for x in flags)), ("NEON", "neon" in flags)) if ok], flags


def ai_cpu_name(s):
    """A processor's name without the trademarks, the clock and the core count: 'Intel(R) Core(TM) i7-10750H CPU @ 2.60GHz' -> 'Intel Core i7-10750H'."""
    s = hclean(s, 80)
    for pat in (r"\((?:R|TM|r|tm)\)", r"\s*@\s*[0-9.]+\s*[GM]Hz", r"\s+\d+-Core Processor", r"\s+CPU\b"):
        s = re.sub(pat, "", s)
    return re.sub(r"\s+", " ", s).strip()


def ai_cpu_text(hw, short=False):
    """'AMD Ryzen 7 5800X · 8 cores / 16 threads · AVX2' (short: the name and the threads)."""
    cpu = dd(hw.get("cpu"))
    cores, threads = num(cpu.get("cores")), num(cpu.get("threads"))
    topo = f"{threads:.0f} threads" if threads else ""
    if cores:
        topo = f"{cores:.0f} cores" + (f" / {threads:.0f} threads" if threads and threads != cores else "")
    simd, flags = ai_simd(cpu)
    x86 = any(k in str(hw.get("arch")).lower() for k in ("x86", "amd64", "i386", "i686"))
    if short:
        return c(1, ai_cpu_name(cpu.get("model")) or "?") + (c(90, f" · {threads or cores:.0f} threads") if threads or cores else "")
    return (c(1, ai_cpu_name(cpu.get("model")) or "?") + (c(90, " · " + topo) if topo else "") + (c(90, " · " + " ".join(simd)) if simd else "")
            + (c(33, " · no AVX2: slow") if x86 and flags and "avx2" not in flags and not simd else ""))


def ai_memory_text(tot, free, bw):
    """'██████░░░░  18.4 GB free of 31.2 GB': a bar of what is in use (bw 0: none), what is free of the total; '(free: ?)' when only the total is known."""
    if free is None:
        return ((c(90, "░" * bw) + "  ") if bw else "") + ai_mb(tot) + c(90, " (free: ?)")
    return ((bar(1 - free / tot, bw) + "  ") if bw else "") + f"{ai_mb(free)} free of {ai_mb(tot)}"


def ai_ram_text(hw, bw):
    tot, free = num(dd(hw.get("ram")).get("total_mb")), num(dd(hw.get("ram")).get("available_mb"))
    return ai_memory_text(tot, free, bw) if tot else c(33, "?") + c(90, " could not be read")


def ai_gpu_text(g, bw):
    """(the GPU's name and backend, its memory): a bar of the video memory in use (bw 0: none), or 'unified memory' when it shares the RAM."""
    backend = AI_BACKEND.get(g.get("backend"))
    head = c(1, hclean(g.get("name"), 48) or "?") + (c(90, " · " + backend) if backend else c(33, " · no usable backend: not used"))
    if g.get("unified"):
        return head, c(90, "unified memory: it shares the RAM" if bw else "unified memory")
    tot = num(g.get("vram_mb"))
    return head, ai_memory_text(tot, num(g.get("vram_free_mb")), bw) if tot else c(33, "?") + c(90, " video memory could not be read")


def ai_hw_lines(hw, w, k=0):
    """The HARDWARE section at level k: 0 everything (the bars, eight GPUs, a GPU on two lines when it does not fit on one, three notes), 1 one
    line each without bars (three GPUs) and one note, 2 the CPU and the RAM on one line (two GPUs) and no notes, 3 nothing. Lines at most w wide."""
    if k >= 3:
        return []
    hw = dd(hw)
    gpus = [g for g in hw.get("gpus") if isinstance(g, dict)] if isinstance(hw.get("gpus"), list) else []
    notes = [hclean(x, 200) for x in hw.get("notes") if isinstance(x, str) and x.strip()] if isinstance(hw.get("notes"), list) else []
    bw = max(6, min(24, (w - 7) // 5))
    lab = lambda t: " " + c(90, pad(t, 5)) + " "  # noqa: E731
    lines = [hsec("HARDWARE", w, " ".join(hclean(hw.get(x), 12) for x in ("os", "arch") if hw.get(x)))]
    if k >= 2:
        lines.append(clip(" " + ai_cpu_text(hw, True) + c(90, "  ·  RAM ") + ai_ram_text(hw, 0), w))
    else:
        lines += [clip(lab("CPU") + ai_cpu_text(hw), w), clip(lab("RAM") + ai_ram_text(hw, bw), w)]
    keep = (8, 3, 2)[k]  # the GPUs drawn: every one when there is room
    for i, g in enumerate(gpus[:keep]):
        head, mem = ai_gpu_text(g, bw if k == 0 else 0)  # the bars only at level 0
        one = lab("GPU" if i == 0 or k >= 2 else "") + head + "  " + mem
        lines += [clip(one, w)] if k >= 1 or vlen(one) <= w else [clip(lab("GPU" if i == 0 else "") + head, w), clip(lab("") + mem, w)]
    if len(gpus) > keep:
        lines.append(clip(c(90, f"       … +{len(gpus) - keep} more GPUs"), w))
    if not gpus:
        lines.append(clip(lab("GPU") + c(33, "none found") + c(90, ": the models run on the CPU, from RAM"), w))
    if k < 2:
        nn = 3 if k == 0 else 1
        shown = notes if len(notes) <= nn else notes[:nn - 1] + [f"… +{len(notes) - nn + 1} more notes"]
        for x in shown:
            rows = textwrap.wrap(x, max(10, w - 4), break_on_hyphens=False) if k == 0 else [x]
            lines += [c(90, "  · " + rows[0] if len(rows[0]) + 4 <= w else "  · " + rows[0][:max(1, w - 5)] + "…")] + [c(90, "    " + y) for y in rows[1:2]]
    return lines


def ai_status_texts(st, cat, ids):
    """The STATUS section's pieces as ANSI text: what [ai] says (adv, with its note), the endpoint and whether it answers (ep, ans, and ans in
    short), the model in use (model), the runtime (run), the directory of the files (files)."""
    st, probe, rt = dd(st), dd(st).get("probe"), dd(dd(cat).get("runtime"))
    on_, sw = bool(st.get("enabled")), dd(st.get("switch"))
    note = ("  ([ai] enabled = yes)" if on_ else "  ([ai] enabled = no in config.ini)") if not sw else \
        "  ([ai] enabled = yes)" if sw.get("by") == "config" else "  (turned on from the AI page or screen)" if on_ else "  (off: the AI switch turns it on)"
    out = {"on": on_, "adv": c(32, "✔ on") if on_ else c(90, "· off"), "note": c(90, note), "ep": hclean(st.get("endpoint"), 120)}
    out["detail"] = ""
    if not on_ or (probe or {}).get("state") == "off":
        out["short"] = c(90, "· not asked while the advisor is off")
    elif probe is None:
        out["short"] = c(90, "· checking…")
    elif probe.get("state") == "answering":
        names = [x for x in probe.get("models") or [] if isinstance(x, str)]
        out["short"] = c(32, "✔ answering")
        out["detail"] = plural(len(names), "model") + (": " + ", ".join(names[:3]) + ("…" if len(names) > 3 else "") if names else "")
    else:
        out["short"] = c(31, "✖ not answering")
        out["detail"] = hclean(probe.get("msg"), 160)
    model = hclean(st.get("model"), 80)
    out["model"] = (c(36, "●") + " " + c(1, model) + (c(90, "  in the catalog") if model in ids else c(33, "  not in the catalog")) if model
                    else c(90, "· none chosen ([ai] model is empty)"))
    out["run"] = (c(32, "✔ installed") + (c(90, f" ({hclean(rt.get('version'), 20)})") if rt.get("version") else "") if rt.get("installed")
                  else c(33, "! not installed") + c(90, ": setup downloads it"))
    out["files"] = hclean(dd(cat).get("dir"), 120)
    out["ans"] = out["short"] + (c(90, " · " + out["detail"]) if out["detail"] else "")
    return out


def ai_status_lines(st, cat, ids, w, k=0):
    """The STATUS section at level k: 0 six lines (more when the server's answer wraps), 1 two (no endpoint, runtime or files), 2 one without a
    title, 3 nothing."""
    if k >= 3:
        return []
    t = ai_status_texts(st, cat, ids)
    lab = lambda x: " " + c(90, pad(x, 9)) + " "  # noqa: E731
    if k >= 2:
        return [clip(" " + c(90, "status ") + t["adv"] + c(90, " · ") + t["short"] + c(90, " · ") + t["model"], w)]
    if k == 1:
        return [hsec("STATUS", w), clip(lab("advisor") + t["adv"] + (c(90, " · ") + t["short"] if t["on"] else t["note"]), w), clip(lab("model") + t["model"], w)]
    srv = [lab("server") + t["ans"]]
    if vlen(srv[0]) > w:  # what the server said does not fit beside the verdict: under it, wrapped
        srv = [lab("server") + t["short"]] + [lab("") + c(90, x) for x in textwrap.wrap(t["detail"], max(10, w - 11), break_on_hyphens=False)[:2]]
    return [hsec("STATUS", w), clip(lab("advisor") + t["adv"] + t["note"], w), clip(lab("endpoint") + t["ep"], w)] + [clip(x, w) for x in srv] \
        + [clip(lab("model") + t["model"], w), clip(lab("runtime") + t["run"], w)] + ([clip(lab("files") + c(90, t["files"]), w)] if t["files"] else [])


def ai_legend(w):
    """One line: what the marks in front of a model mean, and that the speed is a guess."""
    return hjoin([c(33, "★") + c(90, " recommended"), c(32, "✓") + c(90, " installed"), c(36, "●") + c(90, " active"),
                  c(90, "tok/s: rough estimate")], w, " ", "  ·  ")


def ai_layout(rows, w):
    """(the columns to draw, the name's width, the params' width, the notes' width) of a table w wide: the notes go first when it is narrow,
    then the parameters, the speed, the size, and the name keeps AI_NAME_MIN (or its longest name) before any of them."""
    want = {"params": max([len(r["params"]) for r in rows] + [6]), "size": 7, "need": 7, "verdict": 12, "tok": 9}
    names = max([len(r["name"]) for r in rows] + [5])
    keep = ["params", "size", "need", "verdict", "tok"]
    fixed = lambda: 6 + sum(want[k] + 2 for k in keep)  # noqa: E731  # " " + the marks + 2, and every column with its gap
    for drop in ("params", "tok", "size", "need", "verdict"):
        if w - fixed() >= min(names, AI_NAME_MIN):
            break
        keep.remove(drop)
    nw = max(4, min(names, w - fixed()))
    room = w - fixed() - nw - 2
    return keep, nw, want["params"], (min(room, 100) if room >= 16 and "verdict" in keep else 0)


def ai_header(lay):
    keep, nw, pw, nn = lay
    cells = {"params": pad("params", pw), "size": " size".rjust(7), "need": "needs".rjust(7), "verdict": " verdict".ljust(12), "tok": "est tok/s"}
    return c(90, "      " + pad("model", nw + 2) + "  ".join(cells[k] for k in keep) + ("  notes" if nn else ""))


def ai_pill(v):
    label, code = AI_VERDICT.get(v, AI_UNKNOWN)[:2]
    return c(code, " " + label.ljust(10) + " ")


def ai_row_text(r, lay):
    """One model as a table row: the marks (recommended, installed, active), the name, the columns that fit, the notes."""
    keep, nw, pw, nn = lay
    name = r["name"] if len(r["name"]) <= nw else r["name"][:nw - 1] + "…"
    cells = {"params": c(90, pad(r["params"], pw)), "size": ai_mb(r["size_mb"]).rjust(7), "need": ai_mb(r["need_mb"]).rjust(7), "verdict": ai_pill(r["verdict"]),
             "tok": pad(ai_tok(r["tok"]), 9)}
    marks = (c(33, "★") if r["rec"] else " ") + (c(32, "✓") if r["installed"] else " ") + (c(36, "●") if r["active"] else " ")
    return (" " + marks + "  " + (c(90, pad(name, nw)) if r["verdict"] == "no" else pad(name, nw)) + "  " + "  ".join(cells[k] for k in keep)
            + ("  " + c(90, r["notes"][:nn - 1] + "…" if len(r["notes"]) > nn else r["notes"]) if nn else ""))


def ai_where(r):
    """Where the model would run, in words: 'all 36 layers on the GPU', '19 of 36 layers on the GPU, the rest in RAM', 'the CPU, from RAM'."""
    g, n = r["gpu_layers"], r["layers"]
    if not g:
        return "all on the GPU" if r["verdict"] == "gpu" else "the CPU, from RAM" if r["verdict"] in ("ram", "slow") else "-"
    if n and g >= n:
        return f"all {n:.0f} layers on the GPU"
    return f"{g:.0f} of {n:.0f} layers on the GPU, the rest in RAM" if n else f"{g:.0f} layers on the GPU, the rest in RAM"


def ai_details(r, windows=False):
    """What the details of a model say, as plain cleaned values: (title, [(label, value, kind)]). kind: the verdict for the verdict row,
    'cmd' for a command to type, 'warn', 'dim' or ''. The commands are the catalog's, word for word, right after the state: when the screen
    is too small the notes and the licence are cut, not what to type. Console and web share it."""
    label = AI_VERDICT.get(r["verdict"], AI_UNKNOWN)[0]
    items = [("verdict", label, r["verdict"] or "")]
    if r["why"]:
        items.append(("why", r["why"], ""))
    items.append(("speed", f"about {ai_tok(r['tok'])} tokens/s (a rough estimate, not a promise)" if r["tok"] else "no estimate", ""))
    state = [x for x, ok in (("★ recommended for this machine", r["rec"]), ("✓ installed", r["installed"]), ("● active: [ai] model", r["active"])) if ok]
    items.append(("state", " · ".join(state) if state else "not installed", ""))
    cmds = r["commands"]
    if windows:
        items.append(("prompt", "run the commands in an administrator prompt (PowerShell or Command Prompt)", "dim"))
    if not r["installed"]:
        can = r["pinned"] and "install" in cmds  # what cannot be downloaded has no command to type
        items.append(("install", cmds["install"] if can else "not pinned yet: this build cannot download it", "cmd" if can else "warn"))
    if r["installed"] and not r["active"] and "use" in cmds:
        items.append(("use", cmds["use"], "cmd"))
    if r["installed"] and "remove" in cmds:
        items.append(("remove", cmds["remove"], "cmd"))
    items.append(("needs", f"{ai_mb(r['need_mb'])} of memory: the file ({ai_mb(r['size_mb'])}), the context and the runtime", ""))
    if r["verdict"] in ("gpu", "partial", "ram", "slow"):
        items.append(("where", ai_where(r), ""))
    ctx = f"context up to {r['ctx_max']:.0f} tokens" if r["ctx_max"] else ""
    items.append(("model", " · ".join(x for x in (f"{r['params']} parameters", r["quant"], ctx) if x), ""))
    items.append(("licence", r["license"] or "?", ""))
    if r["notes"]:
        items.append(("notes", r["notes"], ""))
    return r["name"], items


def ai_pane(r, w, h, windows=False):
    """Everything known about a model, w columns, h lines at most: the sentences wrap, the commands stay whole (clipped when the screen is
    narrower than they are), what does not fit is counted."""
    title, items = ai_details(r, windows)
    lw = 9
    out = [hsec("DETAILS", w), " " + c(1, title)]
    for label, value, kind in items:
        chunks = [value] if kind == "cmd" else textwrap.wrap(value, max(8, w - lw - 1), break_on_hyphens=False) or ["?"]
        for j, x in enumerate(chunks):
            col = AI_VERDICT.get(kind, AI_UNKNOWN)[3] if kind in AI_VERDICT else {"cmd": "36", "warn": "33", "dim": "90"}.get(kind, "")
            out.append(" " + (c(90, pad(label, lw)) if j == 0 else " " * lw) + cc(col, x))
    return [clip(x, w) for x in hcut(out, h, w)]


def ai_body(data, st, av, rows, w, h):
    """The AI screen's body: at most h lines, none wider than w. Title, HARDWARE (and STATUS beside it from 110 columns), MODELS with the
    cursor's row in reverse video, the legend, then the details (below the list, beside it from AI_PANE_W) or, without them, STATUS under
    the list. The sections lose detail from the bottom up, as the screen gets smaller, before the list loses rows."""
    cat = data["cat"]
    if cat is None:  # the catalog could not be read
        return ([ai_title(None, w)] + hmsg("err" if data.get("err") else "info", data["msg"], w))[:h]
    n, hwd = len(rows), dd(cat.get("hw"))
    ai_sync(av, rows)
    sel = rows[av.idx] if rows else None
    pane = bool(av.details and sel)
    side, two, ids = pane and w >= AI_PANE_W, w >= 110, {r["id"] for r in rows}
    windows = hwd.get("os") == "windows"
    fw = int(w * 0.55) if side else w
    lay = ai_layout(rows, fw)
    full = len(ai_pane(sel, w - fw - 3 if side else w, 99, windows)) if pane else 0
    for want in ((6, 4) if pane and not side else (8, 4)):  # first the comfortable list, then the tight one
        for k in range(4):
            work = ai_work_lines(st, cat, av, w, k)  # the switch, the folder, the last answer: they give way last, one line at a time
            if two:
                lw = (w - 3) * 6 // 11
                top, bottom = columns([(ai_hw_lines(hwd, lw, k), lw), (ai_status_lines(st, cat, ids, w - 3 - lw, k), w - 3 - lw)], w, gap=3) if k < 3 else [], []
            else:
                top, bottom = ai_hw_lines(hwd, w, k), [] if pane else ai_status_lines(st, cat, ids, w, k)
            avail = h - 1 - len(work) - len(top) - len(bottom) - 3  # the title and the work lines; MODELS and its header and the legend
            if pane and not side:  # the details need their lines: the list keeps what is left
                rows_n = min(n, max(want, avail - full))
                fits = avail - rows_n >= full
            else:  # the sections above give up detail before the list loses rows (or the details beside it their height)
                rows_n = min(n, avail)
                fits = rows_n >= min(n, want) and (not side or avail + 2 >= min(full, 12))
            if fits:
                break
        if fits:
            break
    else:
        rows_n = min(n, avail // 2 if pane and not side else avail)  # a screen too small for either: the list and the details share it
    rows_n = max(1, rows_n) if n else 0
    out = [ai_title(rows, w)] + work + top
    av.rows, av.top = max(1, rows_n), map_scroll(av.top, av.idx, n, rows_n) if n else 0
    note = f"{av.top + 1}-{min(n, av.top + rows_n)} of {n} · best first" if n > rows_n else "best first"
    block = [hsec("MODELS", fw, note), ai_header(lay)]
    if not n:
        block.append(msg("info", "the catalog lists no model"))
    for j in range(av.top, min(n, av.top + rows_n)):
        line = clip(ai_row_text(rows[j], lay), fw)
        block.append(c(7, pad(ANSI.sub("", line), fw)) if j == av.idx else line)
    if side:
        pn = ai_pane(sel, w - fw - 3, max(len(block), min(full, avail + 2)), windows)
        block = [pad(x, fw) + c(90, " │ ") + (pn[i] if i < len(pn) else "") for i, x in enumerate(block + [""] * (len(pn) - len(block)))]
    out += block + [ai_legend(w)]
    if pane and not side:
        out += ai_pane(sel, w, max(0, avail - rows_n), windows)
    else:
        out += bottom
    return [clip(x, w) for x in out[:h]]


def ai_footer(av, n, w, snap=None):
    """Where the cursor is, and the keys: in short words when the screen is narrow, then the least needed go first. The keys that act (AI on/off,
    use the model, delete, delete all, cancel) are there unless [ai] web_actions = no locked them (snap says); a question that waits is the footer."""
    if av.confirm:
        ask = " " + av.confirm[2]
        return clip(c("1;33", ask) + c(90, "  y: yes   any other key: no" if len(ask) + 27 <= w else "  [y/n]"), w)
    acts = isinstance(snap, dict) and not snap.get("locked")
    pos = f"model {av.idx + 1}/{n}" if n else "no models"
    dyn = {"details": ("Enter: " + ("hide details" if av.details else "details"), "Enter: " + ("hide" if av.details else "details"))}
    extra = [(8, "questions: web page or nuc-console-ask", "")] if acts else []
    return clip(c(90, ui.footer("ai", f" {pos}   ", w, on, dyn, skip=() if acts else AI_ACTIONS, extra=extra)), w)


def ai_screen(data, pb, av, w, h, wait=0.0):
    """(the interactive AI screen as one frame: header, body, key help; its model rows): the live loop and --once."""
    rows = ai_rows(data["cat"])
    ai_sync(av, rows)
    st = ai_status(wait)
    body = ai_body(data, st, av, rows, w, h - 2)
    return frame(("AI", 1, 1, body), 0, 1, w, h, pb, foot=ai_footer(av, len(rows), w, st.get("snap"))), rows


def ai_once(argv, w, h):
    """`--once --view ai`: the AI screen as the console draws it (tests, screenshots). --select TEXT: the cursor on the first model whose id or
    name contains TEXT (any case), --details: its details (Enter). --demo, with --demo-os windows|darwin: three invented machines (src/demo.py)."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    av = AiView()
    av.details = "--details" in argv
    data, pb = ai_state(None if DEMO else Sampler())
    if opt("--select"):
        ai_select(ai_rows(data["cat"]), av, opt("--select"))
    return ai_screen(data, pb, av, w, h, wait=AI_PROBE_TIMEOUT + 0.5)[0]


# ---- kiosk: macOS/Windows have no text console to take over, the monitor shows the screen in a full-screen browser -----

def user_dir():
    """Per-user folder for the kiosk page and the browser profile (the kiosk runs as the logged-in user)."""
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    elif MACOS:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "nuc-console")


def kiosk_grid():
    """(columns, rows) of the kiosk screen: [dashboard] columns/rows if set, else from the monitor's aspect ratio.

    The page scales its font so the grid fills the screen; 64 rows of text, as many columns as the shape allows
    (16:9 -> 237 columns, the 3-column layout; 16:10 -> 213 columns, 2 columns)."""
    rows = CFG["rows"] or 64
    if CFG["columns"]:
        return CFG["columns"], rows
    size = hostinfo.screen_size() if not LINUX else None
    aspect = size[0] / size[1] if size else 16 / 9
    return max(100, min(400, round(rows * aspect * 1.25 / 0.6))), rows


BROWSERS = {
    "windows": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
                r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles%\Mozilla Firefox\firefox.exe", r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
                r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe"],  # Firefox last: it cannot start full screen (see browser_command)
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
               "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "/Applications/Chromium.app/Contents/MacOS/Chromium"],
    "linux": ["chromium", "chromium-browser", "google-chrome", "microsoft-edge", "brave-browser", "firefox"],
}


def find_browser(choice=None):
    """Path of the browser that will show the kiosk: [display] browser in config.ini, else the first one installed."""
    choice = choice or CFG["display"]["browser"]
    if choice == "none":
        return None
    if choice != "auto":
        return choice
    for cand in BROWSERS[nuc_config.OS_NAME]:
        path = re.sub(r"%([^%]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), cand)
        found = shutil.which(path) if LINUX else (path if os.path.isfile(path) else None)
        if found:
            return found
    return ""  # none found: macOS falls back on Safari through `open`


# shown in the kiosk's footer: the page has nothing to click, the keyboard is the way out
KIOSK_HINT = "Cmd+Q closes · Ctrl+Cmd+F leaves full screen" if MACOS else "Alt+F4 closes · F11 leaves full screen"


def browser_command(exe, url, profile):
    """A full-screen app window (no tabs, no address bar), no first-run pages, a profile of its own (never the user's tabs
    and logins). Not the browsers' locked "kiosk" mode: that one swallows Alt+F4 & co. and the screen could not be closed."""
    if exe == "" and MACOS:
        return ["/usr/bin/open", "-a", "Safari", url]  # Safari has no full-screen flag: Ctrl+Cmd+F once
    if not exe:
        return None
    if "firefox" in os.path.basename(exe).lower():
        return [exe, "--new-window", url]  # Firefox: only its locked kiosk mode starts full screen; F11 instead
    return [exe, "--app=" + url, "--start-fullscreen", "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble", "--noerrdialogs", "--user-data-dir=" + profile]


def kiosk_command(exe, target, base):
    """browser_command() for the kiosk, and the log line of a browser that cannot start full screen (Firefox)."""
    cmd = browser_command(exe, target, os.path.join(base, "browser"))
    if cmd and "firefox" in os.path.basename(exe).lower():
        print("Firefox does not start full screen: press F11 in its window", file=sys.stderr, flush=True)
    return cmd


def default_browser(exe, target):
    """Windows, none of BROWSERS found (find_browser() said ""; not `[display] browser = none`): the default browser, in a normal window
    (no flag can make it full screen: F11). True once opened."""
    if exe != "" or not WINDOWS:
        return False
    try:
        os.startfile(target)  # as this user, like open_in_browser
    except OSError as e:
        print("no supported browser found for full screen, and the default browser did not open:", repr(e)[:200], file=sys.stderr, flush=True)
        return False
    print("no supported browser found for full screen: opened the default browser (F11 for full screen)", file=sys.stderr, flush=True)
    return True


def launch(cmd):
    """Starts the browser, detached from our console (its output is not ours to show)."""
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_text_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:  # no CRLF translation on Windows
        f.write(text)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:  # Windows: the browser is reading the previous frame right now
            time.sleep(0.05)
    return False


def web_up(port, wait):
    """True once the local web view answers; at login it may still be starting (it starts at boot)."""
    end = time.time() + wait
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
                conn.sendall(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                if b" 200 " in conn.recv(64):
                    return True
        except OSError:
            pass
        if time.time() >= end:
            return False
        time.sleep(2)


def dashboard_url(fullscreen=False, cols=0, rows=0):
    query = {"fit": 1, "cols": cols, "rows": rows, "rotate": 1, "kiosk": 1} if fullscreen else {"fit": 1}
    from urllib.parse import urlencode
    return f"http://127.0.0.1:{CFG['web']['port']}/?" + urlencode(query)


def read_web_token(path):
    """(the token in [web] token_file, "") if this user can read it, else ("", why). The text never holds the token."""
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read(512).strip()
    except (OSError, ValueError) as e:  # not there, not allowed (the service user's 0600 file), not text
        return "", f"{path} cannot be read by this user ({e.strerror if isinstance(e, OSError) and e.strerror else type(e).__name__})"
    if not re.fullmatch(r"[A-Za-z0-9._~-]{16,}", token):  # what the web view accepts (src/web.py TOKEN_OK): safe in a URL, too
        return "", f"{path} does not hold a token the web view accepts"
    return token, ""


def open_in_browser(argv):
    """`render.py --open`: the dashboard in a normal window of the default browser ([display] mode = browser, at every login).

    The page is the local web view (127.0.0.1, started by the installer at boot): at login it may need a few seconds more. With a
    token in [web], the URL carries it when this user can read the token file (the web view moves it into a cookie and keeps the view);
    otherwise the dashboard is the page written to a file, as `--kiosk` does."""
    base = user_dir()
    os.makedirs(base, exist_ok=True)
    if sys.stderr is None or "--log" in argv[:-1]:  # pythonw / launched at logon: no console to write to
        nuc_config.log_to(argv[argv.index("--log") + 1] if "--log" in argv[:-1] else os.path.join(base, "display.log"))
    url, token = dashboard_url(), ""
    if CFG["web"]["token_file"]:
        token, why = read_web_token(CFG["web"]["token_file"])
        if not token:
            print(f"[web] token_file is set but {why}: the dashboard is shown from a page written to a file instead", file=sys.stderr, flush=True)
            return kiosk_file(argv, base, *kiosk_grid())
    if not web_up(CFG["web"]["port"], 60):
        print(f"the web view does not answer on 127.0.0.1:{CFG['web']['port']}: dashboard not opened", file=sys.stderr, flush=True)
        return 1
    print(f"open -> {url}" + (" (with the token of [web] token_file)" if token else ""), file=sys.stderr, flush=True)  # never the token itself
    if token:
        url += "&token=" + token
    if WINDOWS:
        os.startfile(url)  # the default browser, as this user
    elif MACOS:
        subprocess.run(["/usr/bin/open", url], timeout=30)
    else:
        import webbrowser
        webbrowser.open(url)
    return 0


def kiosk(argv):
    """`render.py --kiosk`: the dashboard full screen, for the monitor of a Mac or a Windows PC (the display at login).

    It opens the local web view (127.0.0.1, started by the installer) in a full-screen browser window: overview and Details
    pages take turns, A- / A+ change the text size. Without the web view (a Linux desktop, or a token in [web]) it falls
    back on a page written to a file every 2 s (`--file` forces it; `--html FILE`, `--no-browser`)."""
    base = user_dir()
    os.makedirs(base, exist_ok=True)
    if sys.stderr is None or "--log" in argv[:-1]:  # pythonw / launched at logon: no console to write to
        nuc_config.log_to(argv[argv.index("--log") + 1] if "--log" in argv[:-1] else os.path.join(base, "display.log"))
    cols, rows = kiosk_grid()
    web = CFG["web"]
    if "--file" not in argv and not web["token_file"] and web_up(web["port"], 60):
        url = dashboard_url(fullscreen=True, cols=cols, rows=rows)
        exe = find_browser()
        cmd = kiosk_command(exe, url, base)
        print(f"kiosk -> {url}", file=sys.stderr, flush=True)
        if "--no-browser" not in argv and cmd:
            launch(cmd)
            return 0
        if "--no-browser" not in argv and default_browser(exe, url):
            return 0
        print("no browser started: open " + url, file=sys.stderr, flush=True)
        return 0 if "--no-browser" in argv else 1
    return kiosk_file(argv, base, cols, rows)


def kiosk_file(argv, base, cols, rows):
    """The fallback: the dashboard as an HTML file rewritten every 2 s (no network at all). Ends when the browser closes."""
    import htmlview
    zoom = CFG["display"]["zoom"]
    cols, rows = max(60, round(cols * 100 / zoom)), max(16, round(rows * 100 / zoom))  # bigger text = a smaller grid
    path = argv[argv.index("--html") + 1] if "--html" in argv[:-1] else os.path.join(base, "display.html")
    w, h = cols, rows  # a browser page: every column is usable (no Linux console last-column quirk)
    print(f"kiosk {cols}x{rows} -> {path}", file=sys.stderr, flush=True)
    smp, t0, browser, started, cmd, cpu_feed = Sampler(), time.time(), None, 0.0, None, None
    while True:
        try:
            st, sm = snapshot(w), smp.sample()
            sl = slides(sm, st["cont"], st["net"], w, h - 2, st["boot"], st["baseline"], cpu_lazy=True)
            idx = pick_slide(sl, time.time() - t0) % len(sl)
            if sl[idx][0] == "CPU":  # its samplers run only while it is on screen (see main)
                cpu_feed = cpu_feed or CpuFeed()
                fill_cpu(sl, idx, w, h - 2, cpu_feed)
            else:
                cpu_feed = None
            screen = frame(sl[idx], idx, len(sl), w, h,
                           safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"]),
                           keys=False, hint=KIOSK_HINT, page=True)
            write_text_atomic(path, htmlview.kiosk_page(screen, cols, rows, REFRESH_S, socket.gethostname()))
        except Exception as e:  # noqa: BLE001 - a broken frame must not close the kiosk: the next one may be fine
            print("kiosk frame error:", repr(e)[:200], file=sys.stderr, flush=True)
        if browser is None and "--no-browser" not in argv:
            import pathlib
            exe = find_browser()
            cmd = kiosk_command(exe, pathlib.Path(path).resolve().as_uri(), base)
            if cmd:
                browser, started = launch(cmd), time.time()
            else:
                browser = False
                if not default_browser(exe, path):
                    print("no browser found: open " + path + " yourself, or set [display] browser in config.ini", file=sys.stderr, flush=True)
        # the viewer closed the window (Alt+F4): stop. A browser that quits at once handed the page to a running one: keep going
        if browser and cmd[0] != "/usr/bin/open" and browser.poll() is not None and time.time() - started > 10:
            return 0
        time.sleep(REFRESH_S)


# ---- keys: the same names on every OS (up down left right pgup pgdn home end tab btab enter esc space, or the character) ---

KEY_CHAR = {"\t": "tab", "\r": "enter", "\n": "enter", "\x1b": "esc", " ": "space"}
CSI_KEY = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "btab"}
TILDE_KEY = {"1": "home", "7": "home", "4": "end", "8": "end", "5": "pgup", "6": "pgdn"}  # Linux VT, xterm, rxvt
WIN_SCAN = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "I": "pgup", "Q": "pgdn", "\x0f": "btab"}
ESC_SEQ = re.compile(r"\x1b(\[\[|\[|O)([0-9;]*)([A-Za-z~])")  # CSI / SS3 (+ modifiers); '\x1b[[A' = F1 on the Linux VT
ESC_TAIL = re.compile(rb"\x1b(\[\[?[0-9;]*|O)?$")  # a read that ends inside a sequence (or on a lone Esc)


def decode_keys(raw):
    """Key names in what a POSIX terminal sent: one read may hold several keys. Unknown sequences (F keys) are dropped."""
    s = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\x1b" and s[i + 1:i + 2] in ("[", "O"):
            m = ESC_SEQ.match(s, i)
            if m:
                intro, args, fin = m.groups()
                name = "" if intro == "[[" else TILDE_KEY.get(args.split(";")[0], "") if fin == "~" else CSI_KEY.get(fin, "")
                out += [name] if name else []
            i = m.end() if m else i + 2  # cut short: dropped, never read as Esc + letters
            continue
        if ch == "\r" and s[i + 1:i + 2] == "\n":
            i += 1
            continue
        name = KEY_CHAR.get(ch) or (ch if ch.isprintable() else "")
        out += [name] if name else []
        i += 1
    return out


def read_keys(fd, wait):
    """Keys typed on the POSIX terminal fd within `wait` seconds: [] if none, None when the terminal is gone."""
    if not select.select([fd], [], [], max(0.0, wait))[0]:
        return []
    raw = os.read(fd, 64)
    if not raw:
        return None
    for _ in range(4):  # an escape sequence cut in two by the read, or a lone Esc: the rest comes within milliseconds
        if not ESC_TAIL.search(raw) or not select.select([fd], [], [], 0.05)[0]:
            break
        more = os.read(fd, 64)
        if not more:
            break
        raw += more
    return decode_keys(raw)


def win_key(ch, nxt=""):
    """The name of a key read with msvcrt.getwch(): arrows & co. are two reads, '\\x00' or '\\xe0' then a scan code."""
    if ch in ("\x00", "\xe0") and nxt:
        return WIN_SCAN.get(nxt, "")
    if ch == "\x00":
        return ""
    return KEY_CHAR.get(ch) or (ch if ch.isprintable() else "")


def windows_key(timeout):
    """The name of a key typed in a Windows console within `timeout` seconds, or '' (msvcrt has no select)."""
    import msvcrt
    end = time.monotonic() + timeout
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            # a prefix is followed at once by its scan code; '\xe0' alone is a letter ('à' on an Italian keyboard)
            nxt = msvcrt.getwch() if ch == "\x00" or (ch == "\xe0" and msvcrt.kbhit()) else ""
            return win_key(ch, nxt)
        if time.monotonic() >= end:
            return ""
        time.sleep(0.05)


HELP_W = 64  # the help box's widest size (columns), inside the frame


def help_box(scope, w, h, enabled, portable=None, paused=False):
    """The `?` overlay's box: the screen's own keys and the global ones from ui.KEYMAP, as many as fit in a frame w x h (the header and the
    footer stay visible): the last ones of the table go first and the box says how many are left. Lines of equal width, ANSI allowed."""
    groups = ui.help_rows(scope, enabled, nuc_config.PORTABLE if portable is None else portable, paused)
    bw = max(20, min(HELP_W, w - 2))
    lw = min(max(len(k) for _t, items in groups for k, _v in items) + 2, bw // 2)
    inner = bw - 4
    room = max(1, h - 5)  # the frame's header and footer, the box's two borders, the line that says how to close it
    keep = [list(items) for _t, items in groups]
    dropped = 0

    def size(sep):
        shown = [g for g in keep if g]
        return sum(1 + len(g) for g in shown) + (len(shown) - 1 if sep and shown else 0) + (1 if dropped else 0)
    sep = size(True) <= room  # a blank line between the groups when there is room
    while size(sep) > room and any(keep):
        next(g for g in reversed(keep) if g).pop()
        dropped += 1
    lines = []
    for (title, _items), g in zip(groups, keep):
        if g:
            lines += ([""] if lines and sep else []) + [c(ui.sgr("accent_strong"), title)]
            lines += [c(ui.sgr("strong"), pad(k[:lw - 2], lw)) + v[:inner - lw] for k, v in g]
    if dropped:
        lines.append(c(ui.sgr("muted"), f"+{dropped} more"))
    lines.append(c(ui.sgr("muted"), "any key closes this help"))
    top = c(ui.sgr("accent"), "┌─ ") + c(ui.sgr("accent_strong"), "Keys") + c(ui.sgr("accent"), " " + "─" * (bw - 9) + "┐")
    box = [c(ui.sgr("accent"), "│") + " " + pad(clip(x, inner), inner) + " " + c(ui.sgr("accent"), "│") for x in lines]
    return [top] + box + [c(ui.sgr("accent"), "└" + "─" * (bw - 2) + "┘")]


def help_overlay(screen, scope, w, h, enabled, paused=False):
    """The frame (as frame() returns it) with the help box over its middle."""
    lines = screen.split("\x1b[K\r\n")
    return "\x1b[K\r\n".join(ansi.overlay(lines, help_box(scope, w, h, enabled, paused=paused), w))


def main(argv):
    global PAUSED
    utf8_stdout()
    if "--problems" in argv:
        return print_problems(argv)
    if "--accept" in argv and ("--problem" in argv or "--forget" in argv):
        flag = "--problem" if "--problem" in argv else "--forget"
        opt = lambda k: argv[argv.index(k) + 1] if k in argv and argv.index(k) + 1 < len(argv) else ""
        return accept_problem(opt(flag), opt("--reason"), forget=flag == "--forget")
    if "--accept" in argv:
        return accept_baseline(if_missing="--if-missing" in argv)
    if "--demo" in argv and "--once" not in argv:
        print("--demo only works together with --once", file=sys.stderr)
        return 2
    if "--once" in argv:
        return once(argv)
    if "--kiosk" in argv:
        return kiosk(argv)
    if "--open" in argv:
        return open_in_browser(argv)
    smp = Sampler()
    fd = sys.stdin.fileno() if sys.stdin else -1
    old = termios.tcgetattr(fd) if termios and fd >= 0 and os.isatty(fd) else None
    win_keys = WINDOWS and fd >= 0 and os.isatty(fd)
    if WINDOWS:
        import winapi
        winapi.enable_vt()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    out = sys.stdout
    try:
        if old:
            tty.setcbreak(fd)
        out.write("\x1b[?25l\x1b[2J")
        t0, hold_until, held, size = time.time(), 0, 0, None
        map_ok = on("map") and bool(old or win_keys)  # the Map needs a keyboard: without one only map_in_rotation shows it
        cpu_ok = on("cpu") and bool(old or win_keys)  # so does the CPU screen (cpu_in_rotation is for a monitor without one)
        health_ok = on("health") and bool(old or win_keys)  # and the Health screen (health_in_rotation without one)
        ai_ok = on("ai") and bool(old or win_keys)  # and the AI screen (it is not part of the rotation: nobody chooses a model from a monitor)
        mv, G, pb, fresh, rs = None, None, [], 0.0, []  # the Map while it is shown, its graph and problems, next data refresh
        err, last_pb = None, []  # why the Map or Health could not be drawn (until the next refresh); the rotation's last problems
        cv, cpu_d, cpu_fresh, cpu_rows_, rot_feed = None, None, 0.0, [], None  # the CPU screen, its data and rows; the rotation slide's feed
        hv, hd, hl = None, None, []  # the Health screen while it is shown, its report data and findings
        av, ad, al = None, None, []  # the AI screen while it is shown, its catalog data and model rows
        avail = {"map": map_ok, "cpu": cpu_ok, "health": health_ok, "ai": ai_ok}  # the screens the digits (and Tab) can open
        enabled = lambda f: avail.get(f, True)  # noqa: E731
        sl, idx, ov_cache = [], 0, None  # the overview's slides and the one shown; what a paused overview keeps showing
        paused, pause_t, pre_hold, pause_ov = False, 0.0, 0, False  # Z: the redraw is paused (since when; the hold it interrupted)
        dirty, help_open = True, False  # a frame is due even when paused; the `?` overlay is shown

        def resume():
            """Z again (or another screen): the redraw goes on; a paused overview carries on with the slide it was at."""
            nonlocal paused, t0, hold_until
            if paused and pause_ov:
                gap = time.time() - pause_t
                t0, hold_until = t0 + gap, pre_hold + gap
            paused = False
        while True:
            w, h = shutil.get_terminal_size((120, 33))
            # config.ini [dashboard] columns/rows: layout size forced smaller than the real console (never larger: it would run off-screen)
            w, h = min(w, CFG["columns"] or w), min(h, CFG["rows"] or h)
            if (w, h) != size:  # the real console size ends up in the journal: journalctl -u nuc-console
                print(f"console {w}x{h} mode={MODE}", file=sys.stderr, flush=True)
                out.write("\x1b[2J")
                size, dirty = (w, h), True
            w -= 1  # the Linux VT keeps the cursor on the last column: \x1b[K there would erase the last character
            now = time.time()
            if mv is not None and now - mv.touched > MAP_IDLE_S:  # nobody at the keyboard: the monitor goes back to the rotation
                t0, mv, paused, dirty = t0 + now - mv.opened, None, False, True
                out.write("\x1b[2J")
            if cv is not None and now - cv.touched > CPU_IDLE_S:  # same for the CPU screen
                t0, cv, paused, dirty = t0 + now - cv.opened, None, False, True
                out.write("\x1b[2J")
            if hv is not None and now - hv.touched > HEALTH_IDLE_S:  # and the Health screen
                t0, hv, paused, dirty = t0 + now - hv.opened, None, False, True
                out.write("\x1b[2J")
            if av is not None and now - av.touched > AI_IDLE_S:  # and the AI screen
                t0, av, paused, dirty = t0 + now - av.opened, None, False, True
                out.write("\x1b[2J")
            PAUSED = paused
            if mv is not None:  # the Map: new data every REFRESH_S, a new frame at every key
                try:
                    if now >= fresh and not paused:
                        fresh, err = now + REFRESH_S, None  # set first: after a failure keys redraw the error, never postpone the retry
                        G, pb = map_graph(smp)
                    if err is None:
                        screen, rs = map_screen(G, pb, mv, w, h)
                except Exception as e:  # noqa: BLE001 - a broken map must not take the console down; Esc still goes back
                    G, rs, err = None, [], e
                    pb = last_pb + [(2, "the map could not be built")]  # its problems are unknown: never a reassuring "ALL OK"
                if err is not None:
                    screen = frame(("Map", 1, 1, [c(31, f" error on the map: {safe(repr(err))[:w - 20]}")]), 0, 1, w, h, pb,
                                   foot=c(90, " Esc: back   1-5: screens"))
                wait = fresh - time.time()
            elif cv is not None:  # the CPU screen: new data every REFRESH_S (the first one after a second: a CPU% needs two readings)
                try:
                    if now >= cpu_fresh and not paused:
                        cpu_fresh = now + (min(1.0, REFRESH_S) if cpu_d is None else REFRESH_S)
                        st, sm = snapshot(w), smp.sample()  # the header's status pill stays true while the screen is open
                        last_pb = safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
                        cpu_d = cv.feed.read()
                    screen, cpu_rows_ = cpu_screen(cpu_d, last_pb, cv, w, h)
                except Exception as e:  # noqa: BLE001 - a broken screen must not take the console down; c/Esc still go back
                    cpu_rows_ = []
                    screen = frame(("CPU", 1, 1, [c(31, f" error on the CPU screen: {safe(repr(e))[:w - 26]}")]), 0, 1, w, h,
                                   last_pb + [(2, "the CPU screen could not be drawn")], foot=c(90, " Esc: back   1-5: screens"))
                wait = cpu_fresh - time.time()
            elif hv is not None:  # the Health screen: the report comes from its one-minute cache, a new frame at every key
                try:
                    if now >= fresh and not paused:
                        fresh, err = now + REFRESH_S, None
                        hd, pb = health_state(smp, hv.days)
                    if err is None:
                        screen, hl = health_screen(hd, pb, hv, w, h)
                except Exception as e:  # noqa: BLE001 - a broken screen must not take the console down; Esc still goes back
                    hd, hl, err = None, [], e
                    pb = last_pb + [(2, "the health screen could not be built")]  # never a reassuring "ALL OK"
                if err is not None:
                    screen = frame(("Health", 1, 1, [c(31, f" error on the health screen: {safe(repr(err))[:w - 30]}")]), 0, 1, w, h, pb,
                                   foot=c(90, " Esc: back   1-5: screens"))
                wait = fresh - time.time()
            elif av is not None:  # the AI screen: the catalog comes from its short cache, a new frame at every key
                try:
                    if now >= fresh and not paused:
                        fresh, err = now + (1.0 if ai_busy() else REFRESH_S), None  # while something runs (a download) it looks again every second
                        ad, pb = ai_state(smp)
                    if err is None:
                        screen, al = ai_screen(ad, pb, av, w, h)
                except Exception as e:  # noqa: BLE001 - a broken screen must not take the console down; Esc still goes back
                    ad, al, err = None, [], e
                    pb = last_pb + [(2, "the AI screen could not be built")]  # never a reassuring "ALL OK"
                if err is not None:
                    screen = frame(("AI", 1, 1, [c(31, f" error on the AI screen: {safe(repr(err))[:w - 28]}")]), 0, 1, w, h, pb,
                                   foot=c(90, " Esc: back   1-5: screens"))
                wait = fresh - time.time()
            else:
                if paused and ov_cache is not None:  # the slide and the data it was paused on
                    sl, last_pb = ov_cache
                    idx = held % len(sl)
                else:
                    st, sm = snapshot(w), smp.sample()
                    sl = slides(sm, st["cont"], st["net"], w, h - 2, st["boot"], st["baseline"], cpu_lazy=True)
                    idx = (held if now < hold_until else pick_slide(sl, now - t0)) % len(sl)
                    if sl[idx][0] == "CPU":  # its samplers run only while it is on screen
                        rot_feed = rot_feed or CpuFeed()
                        fill_cpu(sl, idx, w, h - 2, rot_feed)
                    else:
                        rot_feed = None
                    last_pb = safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
                    ov_cache = (sl, last_pb)
                screen = frame(sl[idx], idx, len(sl), w, h, last_pb, keys=bool(old or win_keys), mapkey=map_ok, cpukey=cpu_ok, healthkey=health_ok,
                               aikey=ai_ok)  # no keyboard (a monitor): no keys are offered
                wait = REFRESH_S
            scope = "map" if mv is not None else "cpu" if cv is not None else "health" if hv is not None else "ai" if av is not None else "overview"
            if help_open:
                screen = help_overlay(screen, scope, w, h, enabled, paused)
            if paused:  # nothing new to draw until a key: the frame, the clock and the data stay as they are
                wait = REFRESH_S
            if dirty or not paused:
                out.write("\x1b[H" + screen)
                out.flush()
                dirty = False
            keys = []
            if old:
                keys = read_keys(fd, wait)
                if keys is None:  # tty closed/hangup: select always fires, without a pause it would be a busy loop
                    time.sleep(REFRESH_S)
                    continue
            elif win_keys:
                keys = [k for k in [windows_key(wait)] if k]
            else:
                time.sleep(max(0.0, wait))
            for k in keys:
                dirty = True
                if help_open:  # any key closes the help, and does nothing else
                    help_open = False
                    continue
                view = mv if mv is not None else cv if cv is not None else hv if hv is not None else av
                if view is not None:
                    view.touched = time.time()
                asking = av is not None and bool(av.confirm)  # a question waits: any key answers it (y: yes), as it always did
                act = "" if asking else ui.action(scope, k, enabled)
                tgt = None  # the screen to go to
                if act == "screen":
                    tgt = dict(ui.screen_keys(enabled)).get(k)  # a screen that is off: nothing happens
                elif act in ("next", "prev"):
                    order = [n for _d, n in ui.screen_keys(enabled)]
                    tgt = order[(order.index(scope) + (1 if act == "next" else -1)) % len(order)] if scope in order else None
                elif act.startswith("open-"):  # the overview's letters m c h a
                    tgt = act[5:] if enabled(act[5:]) else None
                elif act == "help":
                    help_open = True
                elif act == "redraw":
                    out.write("\x1b[2J")
                    if not paused:  # new data too
                        fresh, cpu_fresh, ov_cache = 0.0, 0.0, None
                elif act == "pause":
                    if paused:
                        resume()
                        fresh, cpu_fresh, ov_cache = 0.0, 0.0, None
                    else:
                        paused, pause_t, pause_ov = True, time.time(), scope == "overview"
                        if pause_ov:  # this slide stays until the redraw goes on
                            pre_hold, held, hold_until = hold_until, idx, float("inf")
                elif scope == "overview":
                    if act == "back" and k == "q" and nuc_config.PORTABLE:  # run.sh in a terminal: q quits (on the monitor of an install it must not)
                        return 0
                    if act in ("slide-prev", "slide-next") and sl:  # held like a digit used to: HOLD_S
                        held = (idx + (1 if act == "slide-next" else -1)) % len(sl)
                        idx = held
                        hold_until = hold_until if paused else time.time() + HOLD_S
                        out.write("\x1b[2J")
                elif cv is not None:
                    res = cpu_key(cv, k, cpu_rows_, cv.page)
                    if res == "back":
                        tgt = "overview"
                    elif res == "rows" and cpu_d is not None:  # the next key of the same read moves on the new order
                        cpu_rows_ = cpu_rows(cpu_d["procs"]["procs"], cv.sort)
                        cpu_sync(cv, cpu_rows_)
                elif hv is not None:
                    res = health_key(hv, k, hl)
                    if res == "back":
                        tgt = "overview"
                    elif res == "period":  # asked again (the report of that period may be cached)
                        fresh = 0.0
                elif av is not None:
                    res = ai_key(av, k, al)
                    if res == "back":
                        tgt = "overview"
                    elif res:  # a key that acts: the engine does it in the background, the screen shows what it says
                        ai_do(av, res, al)
                        fresh = 0.0
                elif mv is not None:
                    res = map_key(mv, k, rs, max(1, map_layout(G, w, h - 2, mv.details)[1] - 1) if G else 10)
                    if res == "back":
                        tgt = "overview"
                    elif res == "rows" and G is not None:  # the next key of the same read moves on the new tree
                        rs = graph.rows(G, mv.st)
                        map_sync(mv, rs)
                if tgt == "overview" and scope == "overview" and sl:  # 1 on the overview: its first slide
                    held = next((i for i, x in enumerate(sl) if x[0] == "Overview"), 0)
                    idx = held
                    hold_until = hold_until if paused else time.time() + HOLD_S
                    out.write("\x1b[2J")
                elif tgt is not None and tgt != scope and (tgt == "overview" or avail.get(tgt)):
                    opened = view.opened if view is not None else time.time()  # the rotation stays paused all the time on the screens
                    resume()
                    mv = cv = hv = av = None
                    cpu_d = None
                    if tgt == "overview":
                        t0 += time.time() - opened  # the rotation was paused: it goes on where it was
                        ov_cache = None
                    elif tgt == "map":
                        mv, fresh, err = MapView(), 0.0, None
                    elif tgt == "cpu":
                        cv, cpu_fresh, rot_feed = CpuView(), 0.0, None
                    elif tgt == "health":
                        hv, fresh, err = HealthView(), 0.0, None
                    else:
                        av, fresh, err = AiView(), 0.0, None
                    nv = mv if mv is not None else cv if cv is not None else hv if hv is not None else av
                    if nv is not None:
                        nv.opened = nv.touched = opened
                    out.write("\x1b[2J")
                    break
    finally:
        PAUSED = False
        out.write("\x1b[?25h\x1b[0m")
        out.flush()
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        try:  # a model server this screen started ends with it (nothing happens when it started none)
            import aiweb
            aiweb.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))  # --accept must be able to fail: install.sh and the user's script check the exit code
    except KeyboardInterrupt:  # Ctrl+C in a terminal (run.sh): the terminal is already restored, no traceback
        sys.exit(130)
    except PermissionError as e:  # --accept as a normal user: say what to do instead of a traceback
        print(f"permission denied: {e.filename or e}: run it as " + ("administrator" if WINDOWS else "root (sudo)"), file=sys.stderr)
        sys.exit(1)
