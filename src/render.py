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
import prefs  # same directory: [ui], the console's theme, density, order and KPIs
import procs
import ui
import screens  # same directory: the full screens' view-models (HEALTH: HealthView, the components of the screen)
# the console primitives (ansi.py) and the text helpers (ui.py) moved out of this file; render.py draws with them, and tests,
# tools and the other modules (notify.py, htmlview.py) still reach them as render.X, so the names stay here until the cleanup PR
from ansi import ANSI, SPARK, bar, c, cc, cell, clip, columns, fit_join, hbucket, hspark, kv, msg, msg_wrap, pad, section, sparkline, vlen  # noqa: F401
from ansi import scroll as map_scroll  # noqa: F401 - the lists' scroll position (ansi.scroll)
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


fmt_up, fmt_load, up_load_note = cards.fmt_up, cards.fmt_load, cards.up_load_note  # the figures of the SYSTEM card (cards.py)


ram_figures, disk_figures = cards.ram_figures, cards.disk_figures  # (cards.py)


def swap_figures(m):
    """(used, total) bytes of the swap from a Sampler's "mem": None when there is no swap (or no figures)."""
    try:
        total, free = m["SwapTotal"], m["SwapFree"]
        return (total - free, total) if total else None
    except (KeyError, TypeError):
        return None


is_absent, is_disabled = cards.is_absent, cards.is_disabled  # the collectors' notes about a section (cards.py reads them too)


def unavail_msg(d, key, prefix="unavailable"):
    """Line for a section without data: 'not installed' (info) if the tool is missing, 'unavailable: error' if it is broken."""
    m = cards.unavail(d, key, prefix)
    return msg(m.level, m.text)


def _lines_of(parts, w):
    """The console's lines of a list of components (ansi.render), and whether the drawing cut something."""
    lines, hid = [], False
    for part in parts:
        got, h = ansi.render(part, w)
        lines += got
        hid = hid or h
    return lines, hid


def thermal_lines(th, bw, maxw=None):
    """Temperatures with bar and thresholds (RAM style) + throttling time. Scale and thresholds come from the sensor (cards.thermal_parts)."""
    return _lines_of(cards.thermal_parts(th, bw, maxw), 0)[0]


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


fs = ui.fmt_s


BOOT_COLORS = ansi.STAGE_COLORS  # the boot stages' colours (ansi.py draws the Timeline)


boot_labels, unsupported, BOOT_LABELS = cards.boot_labels, cards.unsupported, cards.BOOT_LABELS  # (cards.py)


def boot_block_avvio(b, up, w):
    return [section("BOOT", w)] + _lines_of(cards.boot_start_parts(b, up, w), w)[0]


def boot_block_lente(b, w, k):
    return _lines_of(cards.boot_slowest_parts(b, cards.Caps(w, FULL, EXPAND, TRUNC), k), w)[0]


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
    return _lines_of(cards.boot_journal_parts(b, cards.Caps(w, FULL, EXPAND, TRUNC), k, w), w)[0]


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
    about). What only the web has room for, None in a list that problems() did not build: .info = (title, why, fix, accept command) of each
    item, in the same order (the catalog's words for this OS and this install); .known = what was accepted, [{id, text, reason, ts}] (as
    many as .accepted); .cmds = the commands the advice refers to: {problems, accept, forget}."""
    accepted = 0
    pids = None
    info = None
    known = None

    @property
    def cmds(self):
        return {"problems": PROBLEMS_CMD, "accept": ACCEPT_CMD, "forget": ACCEPT_CMD + " --forget"}


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
    out.pids, out.info, out.known = [], [], []
    for sev, text, pid in problems_raw(*a, **kw):
        if pid in acc and acc[pid]["fp"] == fingerprint(sev, text, pid):
            out.accepted += 1
            out.known.append({"id": pid, "text": text, "reason": acc[pid].get("reason", ""), "ts": acc[pid].get("ts")})
        else:
            title, why, fix = CATALOG.get(pid, (pid, "", ""))
            out.append((sev, text))
            out.pids.append(pid)
            out.info.append((title, why, fix, "" if pid in NOT_ACCEPTABLE else f'{ACCEPT_CMD} --problem {pid} --reason "..."'))
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


def _block(card_id, title, body, w):
    """The console lines of a card's body (components), drawn under its title."""
    return ansi.card_lines(ui.Card(card_id, title, "", "ok", body), w)[0]


def exposure_block(net, cont, w, new=None):
    """The whole exposure matrix (the Network page, and the overview when there is room), within the FULL / EXPAND / TRUNC globals."""
    ctx = cards.Ctx(net=net, cont=cont, new=new, cfg=CFG)
    rows = expose_apply(exposure_rows(net, cont), net, cont)
    return _block("exposure", "EXPOSURE", cards.exposure_full(ctx, cards.Caps(w, FULL, EXPAND, TRUNC), rows), w)


def native_fw_lines(net):
    """macOS/Windows: the status line of the OS firewall (Windows Firewall per network profile, macOS Application Firewall)."""
    return [x for n in cards._fw_native_status(net) for x in ansi.render(n, 80)[0]]


def native_fw_details(net, w, max_rules=None):
    """macOS/Windows FIREWALL body: the configuration in a few lines, then which rule or setting opens each listening port."""
    return [x for n in cards.fw_native_details(net, cards.Caps(w, FULL, EXPAND, TRUNC), max_rules) for x in ansi.render(n, w)[0]]


def fw_status_lines(net):
    """The two most important status lines: ufw and DOCKER-USER (macOS/Windows: the OS firewall)."""
    return [x for n in cards.fw_status(net) for x in ansi.render(n, 80)[0]]


short_default = cards.short_default


def firewall_block(net, w, max_rules=None):
    """The whole firewall block: status, configuration, rules (max_rules: how many, None for all of them)."""
    return _block("firewall", "FIREWALL", cards.firewall_full(net, cards.Caps(w, FULL, EXPAND, TRUNC), max_rules), w)


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


short_name, ct_ok, fmt_db_port = cards.short_name, cards.ct_ok, cards.fmt_db_port  # (cards.py)


def stack_lines(cont, w, cap):
    """For each stack: a header with counts and RAM, then its services with a status dot."""
    lines, hid = _lines_of(cards.stack_parts(cont, cap, cards.Caps(w, FULL, EXPAND, TRUNC)), w)
    if hid:
        TRUNC.add("containers")
    return lines


def ov_sistema(s, w, k, cont=None):
    return _native_lines(cards.system_card, cards.Ctx(s=s, cont=cont, cfg=CFG), w, k)


def ov_container(cont, w, k):
    return _native_lines(cards.containers_card, cards.Ctx(cont=cont, cfg=CFG), w, k)


def ov_database(net, cont, w, k):
    return _native_lines(cards.databases_card, cards.Ctx(net=net, cont=cont, cfg=CFG), w, k)


def ov_esposizione(net, cont, w, k, new=None):
    return _native_lines(cards.exposure_card, cards.Ctx(net=net, cont=cont, new=new, cfg=CFG), w, k)


def ov_firewall(net, w, k):
    return _native_lines(cards.firewall_card, cards.Ctx(net=net, cfg=CFG), w, k)


def ov_boot(b, w, k, now=None):
    return _native_lines(cards.boot_card, cards.Ctx(boot=b, now=now, cfg=CFG), w, k)


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


def ov_webapp(net, cont, w, k):
    return _native_lines(cards.webapps_card, cards.Ctx(net=net, cont=cont, cfg=CFG), w, k)


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
    return _native_lines(cards.attention_card, cards.Ctx(problems=pb, cfg=CFG), bw, k)


# The overview's sections as cards (cards.py): each builder returns the card with the lines this file has always drawn for it (ui.Raw)
# and the state its problems give it, so the console is what it was while a section is rebuilt out of components.
# (id, title, feature, lines(ctx, k, caps)); the width a card is drawn in is caps.width
OV_CARDS = (
    ("attention", None, None, None),  # the cards built of components (cards.NATIVE): their builders are in cards.py
    ("exposure", None, None, None),
    ("webapps", None, None, None),
    ("firewall", None, None, None),
    ("system", None, None, None),
    ("containers", None, None, None),
    ("databases", None, None, None),
    ("boot", None, None, None),
    ("network_traffic", None, None, None),
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
        if gap and spacing_on() and len(lines) > 1 and lines[1] != "":  # a little air under each section title
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
        # the order is fixed (config.ini [dashboard] sections, or [ui] layout / order: card_order), never decided by which block happens to fit where
        def lines_of(n, c_):
            return mark_title(card_block(n, k, c_), cards.build(n, ctx, k, caps_at(c_)).state)  # the card's state in front of its title, when it is not fine
        return [(n, lambda c_, n=n: lines_of(n, c_)) for n in card_order(ctx, CFG["sections"]) if n in have and cards.enabled(n, CFG)]

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
    if ui_cfg().get("density") == "wall":  # a wall display is read from afar: it starts at level 0, without the two richest levels (-2, -1)
        levels = levels[2:]
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
                if left and spacing_on():
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


# ---- [ui] on the console: the theme, the density, the cards' order, the tab bar and the KPI line ---------------------------------
# config.ini [ui] (prefs.parse_ui, in CFG["ui"]) is the only source here: a console has no cookie and no URL. Without it nothing below changes
# what the console draws except the header's tab bar, the KPI line (on a tall screen) and the state symbol on a section's title.

UI_THEMES = {"light": "light", "high-contrast": "hc"}  # [ui] theme -> ui.ANSI_THEMES (auto and dark: the default one)
KPI_MIN_ROWS = 30  # the KPI line shows from this many rows up, or whenever [ui] kpis is set
TAB_SHORT = {"overview": "Ov", "map": "Map", "cpu": "CPU", "health": "Hlth", "ai": "AI"}  # the tab bar when the line is narrow
KPI_TOKEN = {"ok": "ok", "warn": "warn", "err": "err", "down": "err", "unknown": "unknown", "info": "info"}
TITLE_STATES = ("warn", "err", "down", "unknown")  # the states a section's title says besides the colour (ok and info are quiet)
LAYOUT_KEYS = ("layout", "hidden", "order", "preset")  # [ui] keys that take the order of the cards from prefs (else [dashboard] sections)


def ui_cfg():
    return CFG.get("ui") or {}


def theme_name():
    """The ANSI theme the frame is written in: NO_COLOR (set and not empty) is always mono, else [ui] theme."""
    if os.environ.get("NO_COLOR"):
        return "mono"
    return UI_THEMES.get(ui_cfg().get("theme"), "default")


def themed(screen):
    """The frame in the console's theme (the screens are drawn in the default one)."""
    return ansi.retheme(screen, theme_name())


def spacing_on():
    """Empty lines under the section titles: [dashboard] spacing, and never in the compact density."""
    return bool(CFG["spacing"]) and ui_cfg().get("density") != "compact"


def kpi_on(h, page=False):
    """Is there a KPI line on a screen h rows tall? A browser page (page=True) has its own."""
    return not page and (h >= KPI_MIN_ROWS or bool(ui_cfg().get("kpis")))


def body_rows(h, page=False):
    """The rows a screen's body has: the frame less the header, the footer and the KPI line."""
    return h - 2 - (1 if kpi_on(h, page) else 0)


def make_ctx(st, sm, pb, now=None):
    """The cards.Ctx of the KPI line from what a screen has read (snapshot(), the sampler's reading, the header's problems)."""
    ids = prefs.effective(ui_cfg())[0]["kpis"]
    net, cont, base = st["net"], st["cont"], st["baseline"]
    ctx = cards.Ctx(s=sm, cont=cont, net=net, boot=st["boot"], problems=pb, cfg=CFG, now=now, baseline=base, new=new_ports(net, cont, base))
    for field, ask in (("health", lambda: health_data(7)), ("ai", ai_status)):  # only the KPIs that read them cost a read
        if field in ids:
            try:
                setattr(ctx, field, ask())
            except Exception:  # noqa: BLE001 - a source that fails is an unknown KPI, never a broken screen
                pass
    return ctx


KEEP = {}  # what the screens last read (st, sm: map_graph, cpu_problems, health_state, ai_state) and the Ctx made of it (kept_ctx)


def keep(st, sm):
    """Remembers what a screen has read, for the KPI line of its frame."""
    KEEP.update(st=st, sm=sm)


def kept_ctx(pb):
    """The cards.Ctx of the KPI line of a screen that has no Ctx of its own: made of what was last read (keep) and the header's problems
    pb, once for each reading. Nothing read: only the problems are known, the other KPIs are '?'."""
    st, sm = KEEP.get("st"), KEEP.get("sm")
    if st is None:
        return cards.Ctx(problems=pb or [], cfg=CFG)
    hit = KEEP.get("ctx")
    if hit is None or hit[0] is not st or hit[1] is not sm or hit[2] is not pb:
        hit = KEEP["ctx"] = (st, sm, pb, make_ctx(st, sm, pb))
    return hit[3]


def kpi_line(ctx, w):
    """The KPI line: symbol, label and value of each KPI of [ui] (kpis or the preset's), the last ones dropped until it fits w columns."""
    items = []
    for k in cards.kpis(ctx, prefs.effective(ui_cfg())[0]["kpis"]):
        col, val = ui.sgr(KPI_TOKEN[k.state]), k.value + k.unit
        items.append((len(f"{k.symbol} {k.label} {val}"),
                      cc(col, k.symbol) + " " + c(ui.sgr("muted"), k.label) + " " + (val if k.state in ("ok", "info") else cc(col, val))))
    while len(items) > 1 and 1 + sum(n for n, _ in items) + 3 * (len(items) - 1) > w:
        items.pop()
    return clip(" " + "   ".join(t for _, t in items), w)


def card_order(ctx, base):
    """The overview's cards in the order to draw them: [dashboard] sections (`base`) unless [ui] says layout, hidden, preset or order.
    Then it is prefs' layout without the hidden cards, and with `order = severity` the cards with the worst state first: attention
    stays where the layout puts it (first), the cards of one state keep their order. The order is a function of the states, so a card
    moves only when its own state changes (or another one's does): the same states are the same order, frame after frame."""
    ui_ = ui_cfg()
    if not any(k in ui_ for k in LAYOUT_KEYS):
        return list(base)
    ids = [n for n, _w in prefs.visible_cards(prefs.effective(ui_)[0], base)]
    if ui_.get("order") != "severity":
        return ids
    states = ctx.once("card_states", lambda: {n: cards.card_state(n, ctx) for n in ids})
    rank = {n: -ui._SEVERITY[states[n]] for n in ids}
    first = ids[:1] if ids[:1] == ["attention"] else []
    return first + sorted(ids[len(first):], key=lambda n: rank[n])  # sorted() is stable


def mark_title(lines, state):
    """The card's lines with its state in front of the title ('── ✖ EXPOSURE ──'), the rule shortened by as much: width does not change.
    Quiet states (ok, info) and a first line that is not a section title are left alone."""
    if state not in TITLE_STATES or not lines:
        return lines
    strong, accent = ui.sgr("accent_strong"), ui.sgr("accent")
    head, line = f"\x1b[{strong}m ", lines[0]
    at = line.find(head)
    if at < 0:
        return lines
    line = line[:at] + head + "\x1b[0m" + c(ui.sgr(KPI_TOKEN[state]), ui.SYMBOLS[state]) + head + line[at + len(head):]
    fill = f"\x1b[{accent}m" + "─" * 4  # the rule after the title: 2 columns shorter, for the symbol and its blank
    cut = line.find(fill)
    if cut >= 0:
        line = line[:cut + len(fill) - 4] + line[cut + len(fill) - 2:]
    return [line] + lines[1:]


PAUSED = False  # Z: the redraw is paused, the header says so


def tab_bar(cur, shown, short, rev_on, rev_off):
    """The screens' tabs: '[1 Overview]  2 Map  3 CPU  4 Health  5 AI', or '[1·Ov] 2·Map 3·CPU 4·Hlth 5·AI' when short. The current one has
    brackets and is in reverse (rev_on / rev_off: the sequences, which depend on the bar being in reverse itself): never colour alone.
    A screen whose feature is off is left out; the digits are the screens', they do not move."""
    out = []
    for i, (name, feat, title) in enumerate(ui.SCREENS):
        if name != cur and (short is None or feat and not shown(feat)):  # short None: only the current tab, for a very narrow console
            continue
        label = f"{i + 1}·{TAB_SHORT[name]}" if short is not False else f"{i + 1} {title}"
        out.append(f"{rev_on}[{label}]{rev_off}" if name == cur else label)
    return (" " if short is not False else "  ").join(out)


def console_head(name, part, parts, w, text, code, shift, shown):
    """The header line of the console: ' host │ [1 Overview]  2 Map  3 CPU  4 Health  5 AI │ HH:MM:SS … ✖ N PROBLEMS' on the pill's colour.
    The tabs have a short form when the long one leaves no room for the host name. A page of the overview that is not 'Overview'
    (System, Boot, Details...) says which, after the tabs (the footer says which screen of how many)."""
    cur, title = next(((n, t) for n, _f, t in ui.SCREENS if t == name), ("overview", "Overview"))
    bar_rev = theme_name() == "mono" or "7" in code.split(";")  # the bar is in reverse already: the current tab is the one that is not
    rev_on, rev_off = ("\x1b[27m", "\x1b[7m") if bar_rev else ("\x1b[7m", "\x1b[27m")
    extra = f" │ {name}" + (f" {part}/{parts}" if parts > 1 else "") if name != title else ""  # the footer counts the screens
    tail = extra + f" │ {time.strftime('%H:%M:%S')}" + (" │ paused" if PAUSED else "") + " "
    host = socket.gethostname()
    for short in (False, True, None):
        pre = shift + " "
        mid = " │ " + tab_bar(cur, shown, short, rev_on, rev_off) + tail
        room = w - len(text) - 2 - vlen(pre + mid)
        if room >= min(len(host), 14) or short is None:  # a form is chosen when the host name keeps 14 columns (all of it, if it is shorter)
            break
    if len(host) > room:  # a long host name (macOS: 'xyz-…-ABCD.local') must never push the status off the screen
        host = host[:max(room - 1, 1)] + "…"
    left = pre + host + mid
    return clip(c(code, pad(left, max(vlen(left), w - len(text) - 2)) + text + "  "), w)


def frame(slide, idx, n, w, h, pb=None, keys=True, hint="", page=False, mapkey=None, foot=None, cpukey=None, healthkey=None, aikey=None,
          ctx=None):
    """page=True: a browser page, where all w columns are usable (the Linux console needs w = its width - 1).
    mapkey / cpukey / healthkey / aikey: say that `2` opens the Map, `3` the CPU screen, `4` the Health screen, `5` the AI screen
    (default: when keys are); foot: a footer of its own, else the overview's, made from ui.KEYMAP.
    The console's frame has the tab bar on top and, from KPI_MIN_ROWS rows (or with [ui] kpis), the KPI line of ctx (a cards.Ctx; without
    one, of what the screens last read: kept_ctx); its body is body_rows(h) tall. A browser page keeps the plain header and has no KPI line."""
    name, part, parts, body = slide
    text, code = status_pill(pb or [])
    shift = " " * (int(time.time() // 600) % 3)  # every 10 min shift the header
    flags = {"map": mapkey, "cpu": cpukey, "health": healthkey, "ai": aikey}
    shown = lambda f: bool(keys if flags.get(f) is None else flags[f]) and on(f)  # noqa: E731
    if page:
        tail = f" │ {name}" + (f" {part}/{parts}" if parts > 1 else "") + f" │ {time.strftime('%H:%M:%S')}" + (" │ paused" if PAUSED else "")
        host, room = socket.gethostname(), w - len(text) - 2 - len(shift) - 1 - len(tail)
        if len(host) > room:  # a long host name (macOS: 'xyz-…-ABCD.local') must never push the status off the screen
            host = host[:max(room - 1, 1)] + "…"
        left = shift + f" {host}" + tail
        head = clip(c(code, pad(left, max(len(left), w - len(text) - 2)) + text + "  "), w)
    else:
        head = console_head(name, part, parts, w, text, code, shift, shown)
    size = f"{w}x{h}" if page else f"{w + 1}x{h}"
    if foot is None:
        foot = c(90, overview_footer(name, idx, n, w, size, keys, hint, shown))
    foot = clip(foot, w)
    rows = [head]
    if kpi_on(h, page):
        rows.append(kpi_line(ctx if ctx is not None else kept_ctx(pb), w))
    rows += [clip(x, w) for x in body[:max(0, h - 1 - len(rows))]]
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
    keep(st, sm)
    if DEMO:
        demo_defaults()
    G = graph.build(st["cont"], st["net"], st["boot"], CFG["webapps"], baseline=st["baseline"], expose=CFG["expose"])
    return G, safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


# ---- MAP screen: graph.py's tree drawn on the console, moved through with the keyboard ------------------------------------
# The screen's model (MapView, its keys, the tree, the details, the layout) moved to screens.py as components that ansi.render draws; what stays here is
# what needs this process (the footer's enabled keys, the frame, the producers). Moved; kept for tests and tools until the cleanup PR.
from screens import (MAP_IDLE_S, MAP_PANE_W, MapView, map_key, map_layout, map_parent, map_select, map_sync, map_view)  # noqa: E402,F401


def map_title(G, w, only=False):
    """The Map's heading line (title, figures, legend), w columns wide. Moved (screens.map_title); kept for tests and tools."""
    return ansi.render(screens.map_title(G, only), w)[0][0]


def map_row(G, row):
    """One tree row as an ANSI string, not cut. Moved (screens.map_branch); kept for tests and tools."""
    b = screens.map_branch(G, row)
    return (ansi.style(b.prefix, "muted") if b.prefix else "") + ansi.inline(b.mark) + " " + ansi.inline(b.body)


def map_pane(G, nid, w, h):
    """Everything known about a node, w columns, h lines at most. Moved (screens.map_props); kept for tests and tools."""
    return ansi.render(screens.map_props(G, nid, h), w)[0]


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
    body, mv.top = map_view(G, rs, w, body_rows(h), mv.cur, mv.details, mv.top, mv.st.only)
    return frame(("Map", 1, 1, body), 0, 1, w, h, pb, foot=map_footer(mv, len(rs), w, getattr(rs, "truncated", False))), rs


def map_slide(cont, net, boot, baseline, w, body_h):
    """The Map among the rotating pages ([dashboard] map_in_rotation): no cursor, opened level by level while it fits."""
    G = graph.build(cont, net, boot, CFG["webapps"], baseline=baseline, expose=CFG["expose"])
    return map_lines(G, graph.rows(G, graph.State(open=graph.fit_open(G, map_layout(G, w, body_h)[1]))), w, body_h)


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
        mv.st.open = graph.fit_open(G, int(exp) if exp.isdigit() else map_layout(G, w, body_rows(h), mv.details)[1])
    if opt("--select"):
        map_select(G, mv, opt("--select"))
    return map_screen(G, pb, mv, w, h)[0]


# ---- CPU screen: the processor, every logical CPU, the temperatures and the processes (htop, plus temperatures) ------------
# Data: cpuinfo.CpuSampler and procs.ProcSampler (the contract is in docs/DESIGN.md), plus sensors.json on macOS/Windows.
# Everything a producer hands over is data: numbers go through num(), text through safe(), a missing value is drawn as "?".

SENSORS = os.environ.get("NUC_CONSOLE_SENSORS", os.path.join(nuc_config.RUN_DIR, "sensors.json"))  # written by the macOS/Windows collector
SENSORS_STALE_S = 60  # an older sensors.json is not "now": it is ignored, and the screen says so
CPU_IDLE_S = MAP_IDLE_S  # left alone this long, the CPU screen gives the monitor back to the rotation
# the CPU screen's constants and the helpers for what a producer hands over moved out of this file (screens.py, ui.py). Moved; kept for tests
# and tools until the cleanup PR
from screens import (CPU_CELL_MAX_BAR, CPU_CELL_MIN_BAR, CPU_PANE_W, CPU_SORT_KEYS, CPU_SORT_NAME, CPU_SORT_SHORT, CPU_SORTS,  # noqa: E402,F401
                     CPU_STATES, cpu_rows, cpu_select, cpu_sync, cpu_key)
from ui import dd, dget, idict  # noqa: E402,F401
import screens  # noqa: E402 - the view-models of the full screens



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


class CpuView(screens.CpuView):
    """The interactive CPU screen's state (screens.CpuView, with the data source of this process). Moved; kept for tests and tools."""

    def __init__(self, now=None):
        screens.CpuView.__init__(self, CpuFeed(), now or time.time())


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
    """screens.cpu_core_of with this machine's topology. Moved; kept for tests and tools."""
    return screens.cpu_core_of(d, ids, cpu_topology)


def cpu_core_temps(d, ids):
    """screens.cpu_core_temps with this machine's topology. Moved; kept for tests and tools."""
    return screens.cpu_core_temps(d, ids, cpu_topology)


def cpu_ctx(full=None):
    """What the CPU screen needs of this process (screens.CpuCtx): whose machine it describes, the topology, whether nothing is cut (full:
    default, as the detail pages are being built), the clock."""
    return screens.CpuCtx(cpu_os(), cpu_topology, FULL if full is None else full, time)


def cpu_view(d, w, h, sort="cpu", cur=None, details=False, top=0):
    """(the CPU screen's body: at most h lines, none wider than w; the first process shown; the processes in order; how many are
    visible; [(line, pid)] of the process rows). cur: the pid under the cursor (None: no cursor, the table is cut at the bottom);
    details: the cursor's process in a pane (beside the table from CPU_PANE_W columns on, below it otherwise). screens.cpu_view makes
    the components; this draws them."""
    sc = screens.cpu_view(d, cpu_ctx(), w, h, sort, cur, details, top)
    lines = [x for node in sc.nodes for x in ansi.render(node, w)[0]]
    return [clip(x, w) for x in lines[:h]], sc.top, sc.rows, sc.vis, sc.pids



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
    body, cv.top, _, vis, _ = cpu_view(d, w, body_rows(h), cv.sort, cv.cur, cv.details, cv.top)
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
    keep(st, sm)
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
    pb = safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
    ctx = make_ctx(st, sm, pb) if kpi_on(h, page) else None  # the KPI line (a console; a browser page has its own)
    sl = slides(sm, st["cont"], st["net"], w, body_rows(h, page), st["boot"], st["baseline"], mode=mode, scroll=scroll, cpu_lazy=True)
    if scroll:  # the page is as tall as its content (header + body + footer)
        h = len(sl[0][3]) + 2
    n = pick_slide(sl, at) if at is not None else n
    fill_cpu(sl, n % len(sl), w, body_rows(h, page), cpu_feed or CpuFeed(settle=0.5))  # only the slide shown reads the processes
    return frame(sl[n % len(sl)], n % len(sl), len(sl), w, h, pb, keys=keys, page=page, ctx=ctx), len(sl)


def render_screens(smp, w, h, mode=None, keys=True, page=False, cpu_feed=None):
    """Every slide (overview + detail pages) as ANSI frames: the web "full details" view."""
    st, sm = snapshot(w), host_sample(smp)
    if DEMO:
        demo_defaults()
    sl = slides(sm, st["cont"], st["net"], w, body_rows(h, page), st["boot"], st["baseline"], mode=mode, cpu_feed=cpu_feed or CpuFeed(settle=0.5))
    pb = safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
    ctx = make_ctx(st, sm, pb) if kpi_on(h, page) else None
    return [frame(x, i, len(sl), w, h, pb, keys=keys, page=page, ctx=ctx) for i, x in enumerate(sl)]


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
    if view == "start":  # the screen the console opens at: [ui] start_view (the overview when it is not set)
        view = ui_cfg().get("start_view", "overview")
        view = "" if view == "overview" else view
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
    print(themed(out) if "--color" in argv else ANSI.sub("", out))  # --color keeps the ANSI codes (used by tools/ansi2svg.py)


# ---- HEALTH screen: health.py's report (the history over days and weeks) on the console, moved through with the keyboard ------

# Moved to screens.py (the HEALTH view-model); kept for tests and tools
from screens import (HEALTH_DAYS, HEALTH_KEYS, HEALTH_NONE, HEALTH_PANE_W, KIND_LABEL, KIND_ORDER, LEVEL_PILL, HealthView, hfact,  # noqa: E402,F401
                     health_details, health_findings, health_key, health_level, health_nothing, health_select, health_sync, hrows, hwhen)
HEALTH_TTL = 60          # the report is computed at most this often per period, whatever the number of keys or requests
HEALTH_IDLE_S = 600      # the screen left alone this long gives the monitor back to the rotation (nobody may be at the keyboard)
def hago(ts):
    return screens.hago(ts, time.time())


DEMO_HEALTH = ""         # --demo-health little|none: the demo with 5 hours of history, or none (demo.HEALTH_VARIANTS)
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


def hjoin(items, w, lead="", sep="  ·  "):
    """lead + items (ANSI strings) joined by sep; the ones that do not fit are counted: '… +N'. At least one is always kept (clipped)."""
    keep = list(items)
    while len(keep) > 1 and vlen(lead + sep.join(keep)) + (8 if len(keep) < len(items) else 0) > w:
        keep.pop()
    more = len(items) - len(keep)
    return clip(lead + sep.join(keep) + (c(90, f"  … +{more}") if more else ""), w)


hcut = ansi.cut_to  # lines as at most n lines: the last one says how many were left out


def health_find_row(f, w):
    """A finding on one line (not cut to w): level pill, title, the text as far as it fits."""
    return ansi.finding_text(screens.health_finding(f, False, False, False), w)


def health_title(R, w, days, fl, selector=True):
    """The title line of the screen at w columns (screens.health_title drawn by ansi): kept for the classic web page and the rotation."""
    return ansi.render(screens.health_title(R, days, fl, selector), w)[0][0]


def health_tables(R, w, h=None):
    """The sections under the findings as lines: the fullest level that fits h lines (None: everything, the web page scrolls)."""
    return screens.health_tables_lines(R, w, h, time.time())


def health_advice_node(R, w):
    """The ADVICE block for the console: the advisor's lines (health_extra_lines, cut and cleaned) in a ui.Advice, None when there are none."""
    lines = [clip(hansi(x), w) for x in health_extra_lines(R, w)[:6]]
    return ui.Advice(lines=lines) if lines else None


def health_body(data, hv, fl, w, h):
    """The Health screen's body: at most h lines, none wider than w (screens.health_lines draws the components the screen is made of)."""
    return screens.health_lines(data, hv, fl, w, h, health_advice_node, time.time())


def health_footer(hv, n, w):
    """Where the cursor is, and the keys (ui.KEYMAP): in short words when the screen is narrow, then the least needed go first."""
    pos = f"finding {hv.idx + 1}/{n}" if n else "no findings"
    dyn = {"details": ("Enter: " + ("hide details" if hv.details else "details"), "Enter: " + ("hide" if hv.details else "details"))}
    return clip(c(90, ui.footer("health", f" {pos}   ", w, on, dyn)), w)


def health_screen(data, pb, hv, w, h):
    """(the interactive Health screen as one frame: header, body, key help; its findings): the live loop and --once."""
    fl = health_findings(data["report"])
    health_sync(hv, fl)
    body = health_body(data, hv, fl, w, body_rows(h))
    return frame(("Health", 1, 1, body), 0, 1, w, h, pb, foot=health_footer(hv, len(fl), w)), fl


def health_slide(w, body_h):
    """The Health screen among the rotating pages ([dashboard] health_in_rotation): no cursor; the findings that fit (the rest counted),
    then the top CPU and memory users."""
    data = health_data(7)
    R = data["report"]
    if R is None:
        return ([section("HEALTH", w)] + ansi.render(ui.Group(screens._msg("err" if data.get("err") else "info", data["msg"], w)), w)[0])[:body_h]
    fl = health_findings(R)
    now = time.time()
    out = [health_title(R, w, 7, fl, selector=False)]
    notes = [hclean(x) for x in R.get("notes") or [] if isinstance(x, str) and (R.get("coverage") or {}).get("since")]
    if notes and body_h >= 10:  # "collecting: 5 hours so far": a monitor nobody types on must not look conclusive
        out.append(clip(c(90, "  · " + notes[0]), w))
    avail = body_h - len(out)
    if not hnum((R.get("coverage") or {}).get("hours")):
        return (out + ansi.render(ui.Group(screens._msg("info", "no data in this period" if (R.get("coverage") or {}).get("since") else HEALTH_NONE, w)), w)[0])[:body_h]
    ncol, apps = (2 if w >= 110 else 1), []
    cw, days = ((w - 3) // 2 if ncol == 2 else w), int(hnum((R.get("period") or {}).get("days"), 7))
    for k in (0, 1, 2, 3):
        cols = [ansi.render(ui.Group(f(R, cw, k, days, now)), cw)[0] for f in (screens.hb_cpu, screens.hb_mem)]
        cand = columns([(x, cw) for x in cols], w, gap=3) if ncol == 2 else cols[0] + cols[1]
        if len(cand) <= avail - 3:  # the findings keep at least a title and two rows
            apps = cand
            break
    rows = max(0, avail - len(apps) - 1)
    out.append(section("FINDINGS", w))
    if not fl:
        out += ansi.render(ui.Group(screens._msg("info", health_nothing(R), w)), w)[0]
    elif rows:
        out += [clip(health_find_row(f, w), w) for f in fl[:rows if len(fl) <= rows else rows - 1]]
        if len(fl) > rows:
            out.append(c(90, f"   … +{len(fl) - rows + 1} more findings"))
    return [clip(x, w) for x in (out + apps)[:body_h]]


def health_state(smp, days):
    """(the cached report data, the header's problems): shared by the console loop and --once."""
    st = snapshot(0)
    sm = host_sample(smp)
    keep(st, sm)
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

# The screen's view-model moved to screens.py (the AI block: the model rows, the view and its keys, every piece drawn as components); what is
# left here reads the world (the catalog, the engine, the probe of the model server) and runs the live loop. Moved; kept for tests and tools
# until the cleanup PR
from screens import (AI_ACTIONS, AI_BACKEND, AI_ID_MAX, AI_NAME_MIN, AI_PANE_W, AI_UNKNOWN, AI_VERDICT, AiView, ai_cpu_name, ai_details,  # noqa: E402,F401
                     ai_id, ai_key, ai_layout, ai_mb, ai_params, ai_rows, ai_select, ai_sync, ai_tok, ai_where)
aisetup_size = screens.ai_size


def _ai_lines(nodes, w):
    return ansi.render(ui.Group(nodes), w)[0]


def ai_title(rows, w):
    return ansi.render(screens.ai_title(rows, w), w)[0][0]


def ai_hw_lines(hw, w, k=0):
    return _ai_lines(screens.ai_hw_nodes(hw, w, k), w)


def ai_status_lines(st, cat, ids, w, k=0):
    return _ai_lines(screens.ai_status_nodes(st, cat, ids, w, k), w)


def ai_work_lines(st, cat, av, w, k=0):
    return _ai_lines(screens.ai_work_nodes(st, cat, av, w, k), w)


def ai_legend(w):
    return ansi.render(screens.ai_legend(w), w)[0][0]


def ai_body(data, st, av, rows, w, h):
    return screens.ai_lines(data, st, av, rows, w, h)


def ai_footer(av, n, w, snap=None):
    """The footer of the AI screen as one line (screens.ai_footer drawn)."""
    return ansi.render(screens.ai_footer(av, n, w, snap, on), w)[0][0]


AI_IDLE_S = 600          # the screen left alone this long gives the monitor back to the rotation (nobody may be at the keyboard)
AI_TTL = 10              # the catalog is read at most this often, whatever the number of keys or requests
AI_PROBE_TTL = 60        # does the model server answer? looked at most this often ...
AI_PROBE_TIMEOUT = 1.0   # ... for at most this long ...
AI_PROBE_STUCK = 30      # ... in a thread of its own (a key or a page never waits for it; one stuck this long is given up)
AI_NONE = "the model catalog could not be read"
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
    keep(st, sm)
    if DEMO:
        demo_defaults()
    return ai_data(), safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])




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


def ai_busy():
    """Something is running (a download, the server starting, an answer): the screen looks again every second. False without an engine."""
    try:
        return bool(ai_engine().snapshot()["busy"])
    except Exception:  # noqa: BLE001
        return False


def ai_screen(data, pb, av, w, h, wait=0.0):
    """(the interactive AI screen as one frame: header, body, key help; its model rows): the live loop and --once."""
    rows = ai_rows(data["cat"])
    ai_sync(av, rows)
    st = ai_status(wait)
    body = ai_body(data, st, av, rows, w, body_rows(h))
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
    """The page the display opens. [ui] web = classic: the classic page (fit to the window; full screen: sized to the grid, the pages taking
    turns, the kiosk footer). [ui] web = app: the shell (app=1); full screen: its wall display (ui=1.dw: wall density; kiosk=1: scrolls by
    itself, says how to close the window). Both take the token the caller appends."""
    app = (CFG.get("ui") or {}).get("web") == "app"
    if app:
        query = {"app": 1, "ui": "1.dw", "kiosk": 1} if fullscreen else {"app": 1}
    else:
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


def new_view(name, opened):
    """(mv, cv, hv, av): the screen `name` (map, cpu, health, ai) open, the others None."""
    v = {"map": MapView, "cpu": CpuView, "health": HealthView, "ai": AiView}[name]()
    v.opened = v.touched = opened
    return tuple(v if n == name else None for n in ("map", "cpu", "health", "ai"))


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
        kctx = None  # the overview's cards.Ctx (the KPI line); the other screens' is made of what they read (kept_ctx)
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
        start = ui_cfg().get("start_view", "overview")  # [ui] start_view: the screen at the start, and where an idle one goes back to
        start = start if start != "overview" and avail.get(start) else "overview"  # a screen needs its keyboard (and its feature on)
        if start != "overview":
            mv, cv, hv, av = new_view(start, time.time())

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
            was_open = any(x is not None for x in (mv, cv, hv, av))
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
            if start != "overview" and was_open and mv is cv is hv is av is None:  # an idle screen: back to the start view, not the rotation
                mv, cv, hv, av = new_view(start, now)
                fresh = cpu_fresh = 0.0
                err, cpu_d = None, None
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
                        keep(st, sm)
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
                    sl, last_pb, kctx = ov_cache
                    idx = held % len(sl)
                else:
                    st, sm = snapshot(w), smp.sample()
                    sl = slides(sm, st["cont"], st["net"], w, body_rows(h), st["boot"], st["baseline"], cpu_lazy=True)
                    idx = (held if now < hold_until else pick_slide(sl, now - t0)) % len(sl)
                    if sl[idx][0] == "CPU":  # its samplers run only while it is on screen
                        rot_feed = rot_feed or CpuFeed()
                        fill_cpu(sl, idx, w, body_rows(h), rot_feed)
                    else:
                        rot_feed = None
                    last_pb = safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
                    kctx = make_ctx(st, sm, last_pb) if kpi_on(h) else None
                    ov_cache = (sl, last_pb, kctx)
                screen = frame(sl[idx], idx, len(sl), w, h, last_pb, keys=bool(old or win_keys), mapkey=map_ok, cpukey=cpu_ok, healthkey=health_ok,
                               aikey=ai_ok, ctx=kctx)  # no keyboard (a monitor): no keys are offered
                wait = REFRESH_S
            scope = "map" if mv is not None else "cpu" if cv is not None else "health" if hv is not None else "ai" if av is not None else "overview"
            if help_open:
                screen = help_overlay(screen, scope, w, h, enabled, paused)
            if paused:  # nothing new to draw until a key: the frame, the clock and the data stay as they are
                wait = REFRESH_S
            if dirty or not paused:
                out.write("\x1b[H" + themed(screen))
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
                    res = map_key(mv, k, rs, max(1, map_layout(G, w, body_rows(h), mv.details)[1] - 1) if G else 10)
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
