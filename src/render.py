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
import threading
import time

import nuc_config  # same directory

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
CFG = nuc_config.load()
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
MODE = os.environ.get("NUC_CONSOLE_MODE") or CFG["mode"]  # overview = a single screen, no rotation


def on(feature):
    """Section enabled in config.ini (default: yes)."""
    return CFG["features"].get(feature, True)

ROTATE_S, REFRESH_S, HOLD_S, STALE_S = CFG["rotate_seconds"], 2, 60, 60
WIDE = 200  # from this width up: containers in 2 columns, exposure and firewall side by side
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PAGES = tuple(n for n, ok in (("System", True), ("Network & firewall", on("exposure") or on("firewall")),
                              ("Boot", on("boot"))) if ok)
# typical database/broker ports: exposed to the LAN they are the case to flag
SENSITIVE = {3306, 5432, 5433, 5447, 5984, 6379, 6381, 9200, 27017, 1883, 9001, 18086, 8086}


def c(code, s):
    return f"\x1b[{code}m{s}\x1b[0m"


CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe(s):
    """External data (comm, labels, docker stderr) is untrusted: no escapes/newlines on the physical console."""
    return CTRL.sub("?", str(s))


def vlen(s):
    return len(ANSI.sub("", s))


def pad(s, w):
    return s + " " * max(0, w - vlen(s))


def section(title, w, note=""):
    """Section title: '── TITLE ────────  note'. The caller always puts an empty line before it."""
    t = f" {title} "
    return c(36, "──") + c("1;36", t) + c(36, "─" * max(2, w - 4 - len(t) - (len(note) + 2 if note else 0))) \
        + (c(90, f"  {note}") if note else "")


def msg(level, text):
    """Indented status message: level err/warn/ok/info, with a symbol (colour alone is not enough)."""
    sym, col = {"err": ("✖", 31), "warn": ("!", 33), "ok": ("✔", 32), "info": ("·", 90)}[level]
    return f"   {c(col, sym)} {text}"


def kv(label, value, lw=13):
    return f"   {c(90, pad(label, lw))}{value}"


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


def fit_join(items, sep, w, lead="", c90=False):
    """lead + items joined by sep, dropping trailing items until it fits in w columns (at least one is kept)."""
    items = list(items)
    while len(items) > 1 and vlen(lead + sep.join(items)) > w:
        items.pop()
    text = sep.join(items)
    return lead + (c(90, text) if c90 else text)


def clip(s, w):
    """Cut to w visible columns, leaving ANSI sequences intact."""
    out, n, i = [], 0, 0
    while i < len(s):
        m = ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
        elif n >= w:
            break
        else:
            out.append(s[i])
            n += 1
            i += 1
    return "".join(out) + "\x1b[0m"


def bar(frac, w, warn=0.7, err=0.9):
    frac = min(max(frac, 0.0), 1.0)
    n = round(frac * w)
    col = 32 if frac < warn else 33 if frac < err else 31
    return c(col, "█" * n) + c(90, "░" * (w - n))


def plural(n, word):
    """'1 rule', '2 rules': English count + noun (regular plurals only)."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def human(nbytes):
    if nbytes is None:
        return "-"
    return f"{nbytes / 2**30:.1f}G" if nbytes >= 2**30 else f"{nbytes / 2**20:.0f}M"


def fmt_dur(sec):
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h" if d else f"{h}h {r // 60}m"


THROTTLE_WINDOW_S = 60
THERMAL_WARN, THERMAL_ERR = 0.8, 0.9  # fractions of the maximum declared by the sensor (sysfs temp*_max)


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


PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
                                                  "100.64.0.0/10", "::1/128", "fe80::/10", "fc00::/7")]


def is_private_addr(addr):
    """LAN, loopback, link-local, Tailscale (100.64/10, fd7a::/48 in fc00::/7). Explicit list: Python's `is_private`
    also includes documentation ranges (e.g. 203.0.113.0/24), which are not local at all."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return any(ip in n for n in PRIVATE_NETS)


_CACHE = {}
_THREADS = {}


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
                time.sleep(ttl)
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
    """Samples /proc/stat and the thermal sensors; CPU percentages are deltas between two consecutive calls."""

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
                "fs": cached("fs", 30, read_filesystems) if on("disks") else None}


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


def up_load_note(up, load):
    return f"up {fmt_dur(up)}" + (f" · load {' '.join(load)}" if load else "")


def columns(cols, w, gap=2):
    """Puts blocks of lines side by side; each block is (lines, width)."""
    rows = max(len(x) for x, _ in cols)
    out = []
    for i in range(rows):
        out.append((" " * gap).join(pad(clip(x[i], cw), cw) if i < len(x) else " " * cw for x, cw in cols))
    return out


def fmt_min(sec):
    return f"{sec / 60:.1f} min" if sec >= 60 else f"{sec:.0f} s"


def is_absent(d, key):
    """True if the collector recorded that the tool for that section is not installed (not an error)."""
    return isinstance(d, dict) and key in (d.get("absent") or [])


def is_disabled(d, key):
    """True if the section was switched off in config.ini (the collector skipped it on purpose)."""
    return isinstance(d, dict) and key in (d.get("disabled") or [])


def unavail_msg(d, key, prefix="unavailable"):
    """Line for a section without data: 'not installed' (info) if the tool is missing, 'unavailable: error' if it is broken."""
    if is_disabled(d, key):
        return msg("info", "disabled in config.ini")
    if isinstance(d, dict) and key in (d.get("unsupported") or []):
        return msg("info", "not available on this OS")
    if isinstance(d, dict) and key in (d.get("notes") or {}):
        return msg("info", safe(d["notes"][key])[:70])
    if is_absent(d, key):
        return msg("info", "not installed on this machine")
    return msg("warn", f"{prefix}: " + safe(((d or {}).get("errors") or {}).get(key, "collector needs updating"))[:60])


def fmt_ago(sec):
    sec = max(sec, 0)  # clocks out of sync must not produce "-5 s"
    return f"{sec:.0f} s" if sec < 90 else f"{sec / 60:.0f} min" if sec < 5400 else f"{sec / 3600:.0f} h"


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
    m, lines = meminfo(), []
    load, up = loadavg(), uptime_s()
    disk_used, disk_tot, disk_label = root_disk()
    lines.append(f" up {fmt_dur(up)}" + (f"   load {' '.join(load)}" if load else ""))
    lines.append("")
    bw = max(10, min(60, w - 40))
    ram_used = m["MemTotal"] - m["MemAvailable"]
    swap_used = m["SwapTotal"] - m["SwapFree"]
    lines.append(f" RAM   {bar(ram_used / m['MemTotal'], bw)} {human(ram_used)}/{human(m['MemTotal'])}"
                 f"  cache {human(m['Cached'])}")
    if m["SwapTotal"]:
        lines.append(f" SWAP  {bar(swap_used / m['SwapTotal'], bw)} {human(swap_used)}/{human(m['SwapTotal'])}")
    lines.append(f" DISK  {bar(disk_used / disk_tot, bw)} {human(disk_used)}/{human(disk_tot)}  {safe(disk_label)}")
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


TS4, TS6 = ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")
NET_STALE_S = 120
# network sections the exposure classification depends on: if one is missing, the baseline comparison is not reliable.
# The others (Tailscale, fail2ban, drops, databases...) are secondary: an error there must not silence the port alarms.
EXPOSURE_SECTIONS = frozenset(("listeners", "ufw", "docker_user", "serve", "firewall"))
# processes that listen on behalf of containers: docker-proxy (Linux), the Docker Desktop / OrbStack / Rancher backends
DOCKER_PROXIES = {"docker-proxy", "com.docker.backend", "com.docker.vpnkit", "vpnkit", "vpnkit-bridge", "com.docker.proxy",
                  "OrbStack Helper", "limactl", "rancher-desktop"}  # not wslrelay: it forwards any WSL port, not only containers


def os_of(d):
    """Which OS wrote a state file: 'linux' for files written before the field existed."""
    return (d or {}).get("os") or "linux"


def exposure_partial(net):
    return bool(set((net or {}).get("errors") or {}) & EXPOSURE_SECTIONS)
NAMEW = 36
BOOT_STALE_S = 900
NCOL3 = 225  # from this width the single screen uses three columns
NET_SKIP = ("lo", "veth", "br-")  # container virtual interfaces: noise
NET_HIST = 30  # history samples for the traffic sparklines
REAL_FS = ("ext4", "ext3", "xfs", "btrfs", "vfat", "f2fs", "zfs", "ntfs3", "exfat", "nfs", "nfs4", "cifs")
BOOT_WINDOW_S = 900  # a container started within 15 min of boot "started with the boot"


def bind_scope(addr):
    """Where a connection can come from, looking only at the bind address."""
    if addr in ("*", "", "0.0.0.0", "::"):
        return "wild"
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "lan"
    return "lo" if ip.is_loopback else "ts" if ip in TS4 or ip in TS6 else "lan"


def rule_match(to, port, proto):
    """(result, interface). Result: True/False, 'all' (Anywhere) or None = rule that cannot be interpreted."""
    to = to.replace(" (v6)", "").strip()
    iface = ""
    m = re.search(r"\s+on\s+(\S+)$", to)
    if m:
        iface, to = m.group(1), to[:m.start()].strip()
    spec, _, pr = to.partition("/")
    if pr and pr != proto:
        return False, iface
    if spec == "Anywhere":
        return "all", iface
    if spec == "OpenSSH":  # ufw's default profile: 22/tcp
        return port == 22 and proto == "tcp", iface
    if re.fullmatch(r"[\d,:]+", spec):  # 22, 80,443, 8000:8010
        for item in spec.split(","):
            lo, _, hi = item.partition(":")
            if lo.isdigit() and int(lo) <= port <= int(hi or lo):
                return True, iface
        return False, iface
    return None, iface  # application profiles, destinations with an IP...


def fw_verdict(port, proto, ufw):
    """(state, note). State: open | filtered | blocked | nofw | unknown.

    Safety rule: whatever cannot be interpreted is 'unknown' (treated as exposed), never 'blocked'.
    First match wins, as in ufw. Rules for the tailscale* interface concern the tailnet, not the LAN.
    ponytail: reads `ufw status verbose`, not the real iptables rules; rules written outside ufw are not seen.
    """
    if ufw is None:
        return "unknown", "ufw n/a"
    if not ufw.get("active"):
        return "nofw", "ufw off"
    d = ufw.get("default", "")
    if "deny (incoming)" not in d and "reject (incoming)" not in d:
        return "open", "default allow"
    allow_src, deny_src, unknown, v6_allow = [], [], False, False
    for r in ufw.get("rules", []):
        act = r["action"]
        allow, deny = act.startswith(("ALLOW", "LIMIT")), act.startswith(("DENY", "REJECT"))
        if not (allow or deny) or "OUT" in act or "FWD" in act:
            continue
        m, iface = rule_match(r["to"], port, proto)
        if m is False or iface.startswith("tailscale"):
            continue
        if m is None:
            unknown = True
            continue
        src = r["from"].replace(" (v6)", "")
        if re.search(r"\s\d", src):  # source with a port ("Anywhere 53"): not interpreted
            unknown = True
            continue
        if "(v6)" in r["to"] + r["from"]:
            v6_allow = v6_allow or allow
            continue
        if src.startswith("Anywhere"):
            if allow:
                return ("filtered", "open except " + ", ".join(deny_src)[:24]) if deny_src else ("open", "open")
            if allow_src:
                break
            return "blocked", "blocked (deny)"
        (allow_src if allow else deny_src).append(src)
    if allow_src:
        return "filtered", "only " + ", ".join(dict.fromkeys(allow_src))[:30]
    if unknown:
        return "unknown", "rule not understood"
    if v6_allow:
        return "unknown", "IPv6 rule only"
    return "blocked", "blocked"


def docker_verdict(du):
    """(cell, note, red_note) for a port published by Docker.

    Rules in DOCKER-USER see the *container* port (DNAT already done), not the published one: a rule with
    --dport cannot be attributed to the port shown, hence 'unknown'. Only a DROP/REJECT without --dport is certain.
    """
    if du is None:
        return 3, "docker: DOCKER-USER n/a", False
    if not du:
        return 1, "docker: bypasses ufw", True
    blanket, unclear = False, False
    for rule in du:
        target = rule.rpartition("-j ")[2].strip()
        if target.startswith(("DROP", "REJECT")):
            if "--dport" in rule or "--dports" in rule:
                unclear = True
            else:
                blanket = True
        elif target not in ("ACCEPT", "RETURN"):
            unclear = True  # jump to an external chain (e.g. ufw-user-forward)
    if blanket:
        return 2, f"docker: DROP in DOCKER-USER ({len(du)})", False
    if unclear:
        return 3, "docker: per-port/chain rules, check by hand", False
    return 1, f"docker: DOCKER-USER without DROP ({len(du)})", True


CELL = {"open": 1, "nofw": 1, "filtered": 2, "blocked": 0, "unknown": 3}
# UDP discovery ports that every browser or OS component binds at the same time (macOS/Windows): one stable name, or the
# "service" of the port would flip between chrome and msedge at every pass and raise CHANGED alarms
SHARED_UDP = {5353: "mDNS", 5355: "LLMNR", 1900: "SSDP", 3702: "WS-Discovery", 137: "NetBIOS", 138: "NetBIOS"}
EXPOSED_RANK = {"open": 4, "nofw": 4, "unknown": 3, "filtered": 2, "blocked": 1}


def exposure_rows(net, cont):
    """One row per (port, proto, bind class): 8443 on loopback and 8443 on Tailscale are different services.

    ponytail: the TS column assumes tailscaled has its `ts-input` rules (accept everything from tailscale0
    before ufw): a reachable bind is open to tailnet peers regardless of ufw. Check with
    `iptables -S ts-input`; holds only if Tailscale's netfilter mode is on (default).
    The container name is looked up by port only (two containers on the same port with different IPs: the first wins).
    """
    ls, ufw, du = net.get("listeners") or [], net.get("ufw"), net.get("docker_user")
    # macOS/Windows: the collector already judged each socket against the firewall (it works per program there); the same
    # firewall filters the Tailscale interface too, and Docker Desktop's port proxy is an ordinary program behind it
    native = os_of(net) != "linux"
    serve_err = "serve" in (net.get("errors") or {})
    serve = {x["port"]: x for x in net.get("serve") or []}
    funnel_ports = {x["port"] for x in net.get("serve") or [] if x["funnel"]}
    published = {}
    for ct in (cont or {}).get("containers", []):
        if ct["state"] == "running":
            for p in ct["ports"]:
                if isinstance(p["p"], int):
                    published.setdefault((p["p"]), ct["name"])
    rows = {}
    for l in ls:
        sc = bind_scope(l["addr"])
        r = rows.setdefault((l["port"], l["proto"], sc), {"proc": "", "fw": None, "procs": set()})
        r["proc"] = r["proc"] or l["proc"]
        if l["proc"]:
            r["procs"].add(l["proc"])
        v = l.get("fw")
        if v and v[0] in CELL and (r["fw"] is None or EXPOSED_RANK[v[0]] > EXPOSED_RANK[r["fw"][0]]):
            r["fw"] = v  # IPv4 and IPv6 sockets of one port: the most exposed verdict counts
    # ports published by Docker with no listening socket (userland-proxy disabled): hidden from ss
    have = {(port, proto) for (port, proto, _sc) in rows}
    for ct in (cont or {}).get("containers", []):
        if ct["state"] != "running":
            continue
        for p in ct["ports"]:
            if isinstance(p["p"], int) and (p["p"], "tcp") not in have:
                sc = "lo" if p["s"] == "lo" else "wild" if p["s"] == "*" else bind_scope(p["s"])
                rows.setdefault((p["p"], "tcp", sc), {"proc": "docker-proxy", "fw": None, "procs": set()})
    out = []
    for (port, proto, sc), r in rows.items():
        via_docker = r["proc"] in DOCKER_PROXIES or (not r["proc"] and port in published)
        name = published.get(port, "container?") if via_docker else (r["proc"] or "?")
        if native and not via_docker and proto == "udp" and port in SHARED_UDP:
            name = f"{SHARED_UDP[port]} ({', '.join(sorted(r['procs'])) or '?'})"
        elif native and not via_docker and len(r["procs"]) > 1:  # several programs on one port: the same name whatever the order
            name = ", ".join(sorted(r["procs"]))
        sv = serve.get(port) if sc == "ts" else None
        if sv:
            name = f"{'funnel' if sv['funnel'] else 'serve'} {sv['path']} → " + sv["target"].replace("http://", "")
        lan_cell, note, bad = 0, "", False
        ts_cell = 1 if sc in ("wild", "ts") else 0
        if native and sc in ("wild", "lan", "ts"):
            state, fnote = r["fw"] or ("unknown", "firewall n/a")
            if sc == "ts":
                ts_cell, note = CELL[state], "tailnet only" + ("" if state in ("open", "nofw") else " · " + fnote)
            else:
                lan_cell, note, bad = CELL[state], fnote, state in ("unknown", "nofw")
                ts_cell = CELL[state] if sc == "wild" else 0
        elif sc in ("wild", "lan"):
            if via_docker:
                lan_cell, note, bad = docker_verdict(du)
            else:
                state, fnote = fw_verdict(port, proto, ufw)
                lan_cell = CELL[state]
                note = "LAN " + fnote if not fnote.startswith("ufw") else fnote
                bad = state in ("unknown", "nofw")
        elif sc == "ts":
            note = "tailnet only"
        net_cell = 1 if port in funnel_ports and sc in ("ts", "wild") else 3 if serve_err and sc == "ts" else 0
        if net_cell == 1:
            note, bad = "FUNNEL: public", True
        out.append({"port": port, "proto": proto, "name": safe(name), "loc": 0 if sc == "ts" else 1,
                    "lan": lan_cell, "ts": ts_cell, "net": net_cell,
                    "note": safe(note), "bad_note": bad, "warn": port in SENSITIVE and lan_cell in (1, 3)})
    out.sort(key=lambda x: (-(x["net"] == 1), -(x["lan"] in (1, 3)), -x["ts"], -x["warn"], x["port"], x["proto"]))
    return out


def cell(v, warn=False, net=False, loc=False):
    if loc:  # local is not an alarm: neutral colour
        return c(37, "●") if v else c(90, "·")
    if v == 3:
        return c(33, "?")
    if net:
        return c("1;31", "●") if v else c(90, "·")
    if v == 1:
        return c("31" if warn else "33", "●")
    return c(36, "◐") if v == 2 else c(90, "·")


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


def exposure_keys(net, cont):
    """{'22/t:LAN': {'name': service, 'lan': filter state}} of ports reachable from outside only; None if unknown.

    Comparing service and filter state too (open/filtered) avoids missing a rule change or a change of
    the process listening on the same port. tailscaled's ephemeral ports (>= 32768, except the fixed
    41641) are excluded: they change at every start and would raise false alarms.
    ponytail: does not tell TCP/UDP apart for Docker-published ports, and the exact name 'tailscaled' is trusted.
    """
    if net is None or net.get("listeners") is None:
        return None
    # macOS/Windows: a shared discovery port is that protocol, whichever browser happens to hold it now
    stable = lambda r: SHARED_UDP[r["port"]] if os_of(net) != "linux" and r["proto"] == "udp" and r["port"] in SHARED_UDP else r["name"]  # noqa: E731
    return {f"{r['port']}/{r['proto'][0]}:{group_of(r)}": {"name": stable(r), "lan": r["lan"]}
            for r in exposure_rows(net, cont)
            if group_of(r) != "LOCALE" and not (r["name"] == "tailscaled" and r["port"] >= 32768 and r["port"] != 41641)}


def load_baseline(path=None):
    """valid dict | None if missing | 'corrotta' (corrupt) if it exists but is unreadable (not 'missing': must be flagged)."""
    path = path or BASELINE
    if not os.path.exists(path):
        return None
    d = load_json(path)
    return d if isinstance(d, dict) and isinstance(d.get("ports"), dict) else "corrotta"


def name_change(old, new, width=20):
    """'old → new' starting where the two names start to differ (a plain cut at 20 chars showed identical prefixes)."""
    old, new = safe(old), safe(new)
    p = 0
    while p < min(len(old), len(new)) and old[p] == new[p]:
        p += 1
    start = max(0, p - 6)  # a little context before the first difference
    lead = "…" if start else ""
    return f"{lead}{old[start:start + width]} → {lead}{new[start:start + width]}"


def baseline_diff(cur, base):
    """(new, gone, changed) against the accepted baseline; 'changed' = same port/group but another service,
    or a LAN filter that went from 'by source' to 'open to all'."""
    old = base["ports"]
    val = lambda v: v if isinstance(v, dict) else {"name": v, "lan": None}
    new = {k: v for k, v in cur.items() if k not in old}
    gone = {k: val(v) for k, v in old.items() if k not in cur}
    changed = {}
    # a shared discovery port now named by its protocol (macOS/Windows): whatever program a baseline recorded there is the same
    renamed = lambda k, new: new in SHARED_UDP.values() and "/u:" in k  # noqa: E731
    for k, v in cur.items():
        if k in old:
            o = val(old[k])
            if o["name"] != v["name"] and on("containers") and not renamed(k, v["name"]):  # containers off: names can't be resolved
                changed[k] = "service " + name_change(o["name"], v["name"])
            elif o["lan"] == 2 and v["lan"] in (1, 3) and on("firewall"):  # firewall off: verdict unknown, not a rule change
                changed[k] = "was filtered by source, now open to the whole LAN"
    return new, gone, changed


def new_ports(net, cont, baseline):
    """{key: 'NEW'|'CHANGED'}; empty if it cannot be computed or data is partial (never an exception)."""
    try:
        if isinstance(baseline, dict) and net and net.get("listeners") is not None and not exposure_partial(net):
            new, _, changed = baseline_diff(exposure_keys(net, cont) or {}, baseline)
            return {**{k: "NEW" for k in new}, **{k: "CHANGED" for k in changed}}
    except Exception:  # noqa: BLE001
        pass
    return {}


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


def problems_raw(net, cont, now=None, boot=False, thermal=None, baseline=False):
    """Every anomaly, by decreasing severity: [(3=port change | 2=error | 1=warning, text, problem id)]. Ids are stable."""
    now, out = now or time.time(), []
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
CATALOG.update(OS_CATALOG.get(nuc_config.OS_NAME, {}))


NOT_ACCEPTABLE = {"port-new", "port-changed", "port-gone"}  # port changes are handled by the baseline: sudo nuc-console-accept
COUNT_MATTERS = {"db-open-lan", "docker-bypass", "funnel-public", "unhealthy-container", "container-exited", "failed-units"}


def fingerprint(sev, text, pid):
    """What was accepted: severity + text. For exposure/health items the exact text (one more is a new problem); for noisy
    counters (journal errors, temperatures) the digits are ignored, so 118 -> 120 stays accepted but a worse severity does not."""
    return f"{sev}|" + (text if pid in COUNT_MATTERS else re.sub(r"\d+", "#", text))


class ProblemList(list):
    """List of (severity, text) with .accepted = how many known items were left out (shown under ATTENTION)."""
    accepted = 0


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
            print("--reason is required: write why this is acceptable (it is shown in `nuc-console-problems`)", file=sys.stderr)
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
    for sev, text, pid in problems_raw(*a, **kw):
        if pid in acc and acc[pid]["fp"] == fingerprint(sev, text, pid):
            out.accepted += 1
        else:
            out.append((sev, text))
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
        return "✔ ALL OK", "1;7"
    if any(sev == 3 for sev, _ in pb):
        return "✖ EXPOSED PORTS CHANGED", "1;41;37"
    n_err = sum(1 for sev, _ in pb if sev == 2)
    return (f"✖ {len(pb)} PROBLEMS", "1;41;37") if n_err else (f"! {len(pb)} WARNINGS", "1;43;30")


GROUPS = (("INTERNET", "Reachable from the Internet (Tailscale Funnel)"),
          ("LAN", "Open on the LAN (and on Tailscale)"),
          ("TAILNET", "Tailnet only"),
          ("LOCALE", "This machine only"))  # keys are stored in baseline.json: never rename them


def group_of(r):
    return "INTERNET" if r["net"] == 1 else "LAN" if r["lan"] in (1, 2, 3) else "TAILNET" if r["ts"] else "LOCALE"


def exposure_block(net, cont, w, new=None):
    new = new or {}
    rows = exposure_rows(net, cont)
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
            text = base if vlen(base) <= room else base[:max(room - 1, 0)] + "…"
            note = c(31, text) if r["bad_note"] and not declared else c(90, text)
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
        return [section("ATTENTION", w), ""] + [msg("err" if sev >= 2 else "warn", t) for sev, t in pb[:6]] + [""] + firewall_block(net, w)
    if not on("firewall"):
        return [section("ATTENTION", w), ""] + [msg("err" if sev >= 2 else "warn", t) for sev, t in pb[:6]] + [""] + exposure_block(net, cont, w, new)
    head = [section("ATTENTION", w), ""] + ([msg("err" if sev >= 2 else "warn", t) for sev, t in pb[:6]] if pb
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


SPARK = "▁▂▃▄▅▆▇█"


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
    m = meminfo()
    disk_used, disk_tot, _ = root_disk()
    bw = max(8, min(40, w - 52))
    ram = m["MemTotal"] - m["MemAvailable"]
    lines = [section("SYSTEM", w, up_load_note(uptime_s(), loadavg())),
             f" RAM   {bar(ram / m['MemTotal'], bw)} {human(ram)}/{human(m['MemTotal'])}   cache {human(m['Cached'])}",
             f" DISK  {bar(disk_used / disk_tot, bw)} {human(disk_used)}/{human(disk_tot)}"]
    lines += thermal_lines(s.get("thermal") or {}, bw, w)
    cores = [v for _, v in sorted(s["cpu"].items(), key=lambda kv: int(kv[0][3:]))]
    if cores:
        mean = sum(cores) / len(cores)
        if k <= 2:  # one bar per core, in columns: the normal look; only the tiny-console levels (k>=3) compress to one character per core
            lines.append(f" CPU   {bar(mean, bw)} {mean * 100:.0f}%   {len(cores)} cores")
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
    rows = exposure_rows(net, cont)
    by = {g: [r for r in rows if group_of(r) == g] for g, _ in GROUPS}
    warn = sum(r["warn"] for r in rows)
    lines.append(f" Internet {c('1;31', len(by['INTERNET'])) if by['INTERNET'] else 0}   LAN {len(by['LAN'])}   "
                 f"tailnet only {len(by['TAILNET'])}   local only {len(by['LOCALE'])}"
                 + (f"   {c(31, f'⚠ {warn} DB/broker on LAN')}" if warn else ""))
    for r in lim(by["INTERNET"], 2, "exposure"):
        tag = new.get(f"{r['port']}/{r['proto'][0]}:INTERNET")
        lines.append(f" {c('1;31', '●')} {c('1;31', tag + ' ') if tag else ''}{r['port']}/{r['proto'][0]} "
                     f"{r['name'][:40]}  {c(31, 'public on the Internet')}")
    items = []
    for r in by["LAN"]:
        tag = new.get(f"{r['port']}/{r['proto'][0]}:LAN")
        label = f"{r['port']} {r['name'][:22]}"
        items.append((0 if tag else 1, (c("1;31", tag + " ") if tag else "") + (c(31, "⚠" + label) if r["warn"] else label)))
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


def fmt_rate(bps):
    for unit in ("B", "kB", "MB", "GB"):
        if bps < 1000 or unit == "GB":
            return f"{bps:.0f} B/s" if unit == "B" else f"{bps:.1f} {unit}/s"
        bps /= 1000


def sparkline(values, width):
    """Last `width` values as small bars; scaled to the series maximum (with a floor, so noise is not blown up)."""
    vals = list(values)[-width:]
    top = max(max(vals, default=0), 1024)
    return c(90, "▁" * (width - len(vals))) + "".join(SPARK[min(7, int(v / top * 8))] for v in vals)


def ov_traffico(s, w, k):
    lines = [section("NETWORK TRAFFIC", w, "↓ received · ↑ sent")]
    nets = s.get("net")
    if not nets:
        return lines + [msg("info", "no interfaces")]
    sw = 12 if w >= 100 else 8  # shorter sparkline in narrow columns: the line must not be cut
    for name, v in lim(sorted(nets.items(), key=lambda kv: -(kv[1]["rx_tot"] + kv[1]["tx_tot"])), 5, "network_traffic"):
        lines.append(f" {pad(safe(name)[:11], 12)}{c(32, '↓')} {pad(fmt_rate(v['rx']), 10)}{sparkline(v['hist_rx'], sw)} "
                     f"{c(36, '↑')} {pad(fmt_rate(v['tx']), 10)}{sparkline(v['hist_tx'], sw)}"
                     + c(90, f" ↓{human(v['rx_tot'])} ↑{human(v['tx_tot'])}"))
    if len(nets) > 5 and not FULL:
        lines.append(c(90, f" … +{len(nets) - 5} more"))
    return lines


def ov_sessioni(s, w, k):
    lines = [section("SESSIONS", w)]
    sess = s.get("sessions")
    if sess is None:
        return lines + [msg("warn", "unavailable")]
    remote = [ip for ip in sess["ssh"] if not is_private_addr(ip)]
    lines.append(f" {plural(len(sess['local']), 'user session')}   ssh: " + (
        c(31 if remote else 32, f"{len(sess['ssh'])} connected") if sess["ssh"]
        else c(90, "none")))
    for ip in lim(sess["ssh"], 4, "sessions"):
        lines.append(f" {c(31, '✖') if ip in remote else c(32, '●')} ssh from {safe(ip)}  "
                     + (c(31, "address NOT local or Tailscale") if ip in remote else c(90, "LAN or Tailscale")))
    if len(sess["ssh"]) > 4 and not FULL:
        lines.append(c(90, f" … +{len(sess['ssh']) - 4} more ssh clients"))
    for key, label in (("rdp", "remote desktop"), ("vnc", "screen sharing")):  # Windows RDP, macOS Screen Sharing
        peers = sess.get(key) or []
        if peers:
            far = [ip for ip in peers if not is_private_addr(ip)]
            lines.append(f" {c(31, '✖') if far else c(32, '●')} {label} from {safe(', '.join(peers[:3]))}"
                         + (c(31, "  address NOT local or Tailscale") if far else c(90, "  LAN or Tailscale")))
    ttys = sorted({x["tty"] for x in sess["local"] if x["tty"]})
    if ttys:
        lines += wrap_items([safe(t) for t in ttys], w, indent=1, sep=" ", max_lines=1, section="sessions")
    return lines


def ov_tailscale(net, w, k):
    lines = [section("TAILSCALE", w)]
    ts = (net or {}).get("ts_peers")
    if ts is None:
        return lines + [unavail_msg(net, "ts_peers")]
    peers, me = ts["peers"], ts["self"]
    on = sum(p["online"] for p in peers)
    now = time.time()
    stale = now - net.get("ts", now) > NET_STALE_S
    lines[0] = section("TAILSCALE", w, f"{safe(me['name'])} · {on}/{len(peers)} nodes online" + (" · exit node" if me["exit_option"] else "")
                       + (f" · stale data ({fmt_ago(now - net['ts'])} old)" if stale else ""))
    for p in lim(peers, 8, "tailscale"):
        if p["online"]:
            state = c(32, "online ") + c(90, "direct" if p["direct"] else f"via relay {safe(p['relay'])}")
        else:
            state = c(90, "offline · " + (f"seen {fmt_ago(now - p['last_seen'])} ago" if p["last_seen"] else "never seen"))
        lines.append(f" {c(32, '●') if p['online'] else c(90, '○')} {pad(safe(p['name'])[:18], 19)}{pad(safe(p['os'])[:8], 9)}{state}"
                     + (c(33, "  exit node in use") if p["exit"] else ""))
    if len(peers) > 8 and not FULL:
        lines.append(c(90, f" … +{len(peers) - 8} nodes"))
    return lines


INFRA_PROCS = {"sshd", "tailscaled", "systemd-resolve", "systemd-resolved", "cupsd", "avahi-daemon", "chronyd", "rpcbind", "dnsmasq", "named",
               # Windows and macOS system services that listen on their own (not web apps)
               "System", "svchost", "lsass", "wininit", "services", "spoolsv", "launchd", "mDNSResponder", "rapportd", "ControlCenter",
               "sharingd", "remoted", "configd", "netbiosd", "Tailscale", "tailscale-ipn"}
REACH_ORDER = ("INTERNET", "LAN", "TAILNET", "LOCALE")
REACH_LABEL = {"INTERNET": "Internet", "LAN": "LAN+tailnet", "TAILNET": "tailnet", "LOCALE": "local only"}


def webapp_rows(net, cont):
    """Web apps: the ones you declared under [webapps] (up or down) and the listeners found on their own.

    -> [{name, ports, state: 'up'|'down', reach: INTERNET|LAN|TAILNET|LOCALE|None, expected: bool}] sorted for display."""
    rows = exposure_rows(net, cont) if net and net.get("listeners") is not None else []
    db_names = {it["name"] for it in ((net or {}).get("dbs") or {}).get("items", [])}
    by_port = {}
    for r in rows:
        by_port.setdefault(r["port"], []).append(r)
    widest = lambda rs: min((group_of(r) for r in rs), key=REACH_ORDER.index) if rs else None
    out, used = [], set()
    for name, ports in CFG["webapps"].items():
        hit = [r for p in ports for r in by_port.get(p, [])]
        out.append({"name": name, "ports": list(ports), "state": "up" if hit else "down", "reach": widest(hit), "expected": True})
        used |= set(ports)
    found = {}
    desktop = os_of(net) != "linux"  # macOS/Windows desktops: dozens of apps listen on 127.0.0.1, list only what is reachable
    for r in rows:
        if desktop and group_of(r) == "LOCALE":
            continue
        if (r["port"] in used or r["port"] in SENSITIVE or r["port"] == 22 or r["proto"] != "tcp" or r["name"] in INFRA_PROCS
                or r["name"].startswith("svchost/") or r["name"] in db_names or r["name"] == "?"):
            continue
        found.setdefault(r["name"], []).append(r)
    for name, rs in found.items():
        if name.startswith(("funnel ", "serve ")):
            name = " ".join(name.split()[:2])  # 'funnel /webhook → 127.0.0.1:…' -> 'funnel /webhook'
        out.append({"name": name, "ports": sorted({r["port"] for r in rs}), "state": "up", "reach": widest(rs), "expected": False})
    rank = lambda x: (not x["expected"], x["state"] != "up", REACH_ORDER.index(x["reach"]) if x["reach"] else 9, x["name"])
    return sorted(out, key=rank)


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
    lines = [section("DOCKER · DISK", w)]
    if boot and time.time() - boot.get("ts", time.time()) > BOOT_STALE_S:
        lines[0] = section("DOCKER · DISK", w, f"stale data ({fmt_ago(time.time() - boot['ts'])} old)")
    df = (boot or {}).get("docker_df")
    if df is None:
        return lines + [unavail_msg(boot, "docker_df")]
    for r in df["rows"]:
        recl = safe(r["reclaimable"])
        lines.append(f" {pad(safe(r['type']), 14)}{safe(r['count']):>4} ({safe(r['active'])} in use)  {pad(safe(r['size']), 9)} unused {recl}")
    dang = df.get("dangling_images")
    if dang is not None:
        lines.append(c(90 if not dang["bytes"] else 33, f" dangling images: {dang['count']} ({human(dang['bytes']) if dang['bytes'] else '0B'}): safe to prune"))
    if df.get("volumes_unused"):
        anon = df.get("volumes_unused_anonymous") or 0
        lines.append(c(33, f" {df['volumes_unused']} unused volumes ({anon} anonymous): may hold data, check before pruning"))
    lines.append(c(90, " unused = no container uses it; tagged images can be re-pulled"))
    return lines


def ov_dischi(s, w, k):
    lines = [section("DISKS", w)]
    fs = s.get("fs")
    if not fs:
        return lines + [msg("info" if fs == [] else "warn", "no filesystems" if fs == [] else "unavailable")]
    bw = max(8, min(30, w - 42))
    for f in lim(fs, 5, "disks"):
        lines.append(f" {pad(safe(f['mount'])[:14], 15)}{bar(f['used'] / f['total'], bw)} {human(f['used'])}/{human(f['total'])}")
    if len(fs) > 5 and not FULL:
        lines.append(c(90, f" … +{len(fs) - 5} more"))
    return lines


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


def page_overview(s, cont, net, boot, w, body_h, pb=None, baseline=False, now=None, details=None):
    """Everything on one screen. If it does not fit, details shrink (k = 0..3); at the last level no empty lines.

    `details`: pass a list to receive the detail pages (sections that hid items, shown in full), see slides()."""
    pb = safe_problems(net, cont, now, boot=boot, thermal=s.get("thermal"), baseline=baseline) if pb is None else pb
    new = new_ports(net, cont, baseline)

    def block(fn, title, width, *a):
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001 - a broken block must not empty the screen
            return [section(title, width), msg("err", safe(repr(e))[:60])]

    def guardare(bw, k):
        shown = lim(pb, max(3, 6 - k), "attention")
        rows = [msg("err" if sev >= 2 else "warn", t) for sev, t in shown]
        extra = [c(90, f"   … +{len(pb) - len(rows)} more")] if len(pb) > len(rows) else []
        acc = getattr(pb, "accepted", 0)
        known = [c(90, f"   · {acc} accepted as known (nuc-console-problems)")] if acc else []
        return [section("ATTENTION", bw)] + (rows + extra if pb else [msg("ok", "no problems detected")]) + known

    ncol = 3 if w >= NCOL3 else 2 if w >= WIDE else 1
    cw = (w - 3 * (ncol - 1)) // ncol

    def make_cand(k):
        """(feature, block) per section at detail level k: a section switched off in config.ini does not appear."""
        cand = {"attention": (True, lambda c_: guardare(c_, k)),
                "exposure": ("exposure", lambda c_: block(ov_esposizione, "EXPOSURE", c_, net, cont, c_, k, new)),
                "firewall": ("firewall", lambda c_: block(ov_firewall, "FIREWALL", c_, net, c_, k)),
                "system": (True, lambda c_: block(ov_sistema, "SYSTEM", c_, s, c_, k, cont)),
                "containers": ("containers", lambda c_: block(ov_container, "CONTAINER", c_, cont, c_, k))}
        if k < 4:
            cand["databases"] = ("databases", lambda c_: block(ov_database, "DATABASE", c_, net, cont, c_, k))
            cand["boot"] = ("boot", lambda c_: block(ov_boot, "BOOT", c_, boot, c_, k))
            cand["webapps"] = ("webapps", lambda c_: block(ov_webapp, "WEB APPS", c_, net, cont, c_, k))
        if k <= 3 and w >= WIDE:  # wide consoles: the detail sections stay at every level (they shrink, they do not vanish)
            cand.update(network_traffic=("network_traffic", lambda c_: block(ov_traffico, "NETWORK TRAFFIC", c_, s, c_, k)),
                        sessions=("sessions", lambda c_: block(ov_sessioni, "SESSIONS", c_, s, c_, k)),
                        tailscale=("tailscale", lambda c_: block(ov_tailscale, "TAILSCALE", c_, net, c_, k)),
                        docker_disk=("docker_disk", lambda c_: block(ov_docker, "DOCKER · DISK", c_, boot, c_, k)),
                        disks=("disks", lambda c_: block(ov_dischi, "DISKS", c_, s, c_, k)))
        # the order is fixed (config.ini [dashboard] sections), never decided by which block happens to fit where
        return [(n, cand[n][1]) for n in CFG["sections"] if n in cand and (cand[n][0] is True or on(cand[n][0]))]

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


def slides(s, cont, net, w, body_h, boot=None, baseline=False, mode=None):
    """Each page is split into chunks body_h tall: (page name, index, total, lines)."""
    det = []
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


def frame(slide, idx, n, w, h, pb=None, keys=True, hint="", page=False):
    """page=True: a browser page, where all w columns are usable (the Linux console needs w = its width - 1)."""
    name, part, parts, body = slide
    left = (" " * (int(time.time() // 600) % 3) + f" {socket.gethostname()} │ {name}"  # every 10 min shift the header
            + (f" {part}/{parts}" if parts > 1 else "") + f" │ {time.strftime('%H:%M:%S')}")
    text, code = status_pill(pb or [])
    head = c(code, pad(left, max(len(left), w - len(text) - 2)) + text + "  ")
    head = clip(head, w)
    size = f"{w}x{h}" if page else f"{w + 1}x{h}"
    foot = c(90, (f" single screen   console {size}" if n == 1 else
                  f" screen {idx + 1}/{n}" + ("   details: everything the overview cut ('… +N more')" if name == "Details" else "")
                  + (f"   keys 1-{len(PAGES)}: jump to page" if keys else "") + f"   console {size}") + (f"   {hint}" if hint else ""))
    foot = clip(foot, w)
    rows = [head] + [clip(x, w) for x in body]
    rows += [""] * (h - 1 - len(rows)) + [foot]
    return "\x1b[K\r\n".join(rows[:h])  # \x1b[K: clears what is left of the previous frame


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


def render_screen(smp, w, h, mode=None, n=0, at=None, keys=True, page=False):
    """One frame as an ANSI string and the number of slides: used by --once and by the web view (web.py).
    at = a time: the slide shown at that moment of the rotation (overview, then Details pages), as on the console."""
    st, sm = snapshot(w), smp.sample()
    if DEMO:
        import demo
        sm = demo.sampler_data(sm, DEMO_OS)
        socket.gethostname = lambda: "demo-host"
        if not CFG["webapps"]:
            CFG["webapps"] = {"shop-web": [8080], "admin-console": [9443]}  # one up, one expected-but-down
    sl = slides(sm, st["cont"], st["net"], w, h - 2, st["boot"], st["baseline"], mode=mode)
    n = pick_slide(sl, at) if at is not None else n
    return frame(sl[n % len(sl)], n % len(sl), len(sl), w, h,
                 safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"]), keys=keys, page=page), len(sl)


def render_screens(smp, w, h, mode=None, keys=True, page=False):
    """Every slide (overview + detail pages) as ANSI frames: the web "full details" view."""
    st, sm = snapshot(w), smp.sample()
    if DEMO:
        import demo
        sm = demo.sampler_data(sm, DEMO_OS)
        socket.gethostname = lambda: "demo-host"
        if not CFG["webapps"]:
            CFG["webapps"] = {"shop-web": [8080], "admin-console": [9443]}
    sl = slides(sm, st["cont"], st["net"], w, h - 2, st["boot"], st["baseline"], mode=mode)
    pb = safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
    return [frame(x, i, len(sl), w, h, pb, keys=keys, page=page) for i, x in enumerate(sl)]


def utf8_stdout():
    """Windows pipes default to the ANSI code page: the bars and symbols must not crash `--once > file` or `| tool`."""
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if sys.stdout is not None and enc != "utf8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def once(argv):
    global DEMO, DEMO_OS
    DEMO = "--demo" in argv
    DEMO_OS = argv[argv.index("--demo-os") + 1] if "--demo-os" in argv[:-1] else None
    arg = lambda k, d: int(argv[argv.index(k) + 1]) if k in argv else d
    w, h, n = arg("--cols", 120) - 1, arg("--rows", 33), arg("--slide", 0)
    smp = Sampler()
    smp.sample()  # starts the background reads (sessions, disks): they have the half second below to arrive
    time.sleep(0.5)
    out, _ = render_screen(smp, w, h, n=n)
    print(out if "--color" in argv else ANSI.sub("", out))  # --color keeps the ANSI codes (used by tools/ansi2svg.py)


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
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
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


def open_in_browser(argv):
    """`render.py --open`: the dashboard in a normal window of the default browser ([display] mode = browser, at every login).

    The page is the local web view (127.0.0.1, started by the installer at boot): at login it may need a few seconds more."""
    base = user_dir()
    os.makedirs(base, exist_ok=True)
    if sys.stderr is None or "--log" in argv[:-1]:  # pythonw / launched at logon: no console to write to
        nuc_config.log_to(argv[argv.index("--log") + 1] if "--log" in argv[:-1] else os.path.join(base, "display.log"))
    url = dashboard_url()
    if not web_up(CFG["web"]["port"], 60):
        print(f"the web view does not answer on 127.0.0.1:{CFG['web']['port']}: dashboard not opened", file=sys.stderr, flush=True)
        return 1
    print(f"open -> {url}", file=sys.stderr, flush=True)
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
        cmd = browser_command(find_browser(), url, os.path.join(base, "browser"))
        print(f"kiosk -> {url}", file=sys.stderr, flush=True)
        if not cmd or "--no-browser" in argv:
            print("no browser started: open " + url, file=sys.stderr, flush=True)
            return 0 if "--no-browser" in argv else 1
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    return kiosk_file(argv, base, cols, rows)


def kiosk_file(argv, base, cols, rows):
    """The fallback: the dashboard as an HTML file rewritten every 2 s (no network at all). Ends when the browser closes."""
    import htmlview
    zoom = CFG["display"]["zoom"]
    cols, rows = max(60, round(cols * 100 / zoom)), max(16, round(rows * 100 / zoom))  # bigger text = a smaller grid
    path = argv[argv.index("--html") + 1] if "--html" in argv[:-1] else os.path.join(base, "display.html")
    w, h = cols, rows  # a browser page: every column is usable (no Linux console last-column quirk)
    print(f"kiosk {cols}x{rows} -> {path}", file=sys.stderr, flush=True)
    smp, t0, browser, started, cmd = Sampler(), time.time(), None, 0.0, None
    while True:
        try:
            st, sm = snapshot(w), smp.sample()
            sl = slides(sm, st["cont"], st["net"], w, h - 2, st["boot"], st["baseline"])
            idx = pick_slide(sl, time.time() - t0)
            screen = frame(sl[idx % len(sl)], idx % len(sl), len(sl), w, h,
                           safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"]),
                           keys=False, hint=KIOSK_HINT, page=True)
            write_text_atomic(path, htmlview.kiosk_page(screen, cols, rows, REFRESH_S, socket.gethostname()))
        except Exception as e:  # noqa: BLE001 - a broken frame must not close the kiosk: the next one may be fine
            print("kiosk frame error:", repr(e)[:200], file=sys.stderr, flush=True)
        if browser is None and "--no-browser" not in argv:
            import pathlib
            cmd = browser_command(find_browser(), pathlib.Path(path).resolve().as_uri(), os.path.join(base, "browser"))
            if cmd:
                browser, started = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), time.time()
            else:
                browser = False
                print("no browser found: open " + path + " yourself, or set [display] browser in config.ini", file=sys.stderr, flush=True)
        # the viewer closed the window (Alt+F4): stop. A browser that quits at once handed the page to a running one: keep going
        if browser and cmd[0] != "/usr/bin/open" and browser.poll() is not None and time.time() - started > 10:
            return 0
        time.sleep(REFRESH_S)


def windows_key(timeout):
    """A key typed in a Windows console within `timeout` seconds, or '' (msvcrt has no select)."""
    import msvcrt
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if msvcrt.kbhit():
            return msvcrt.getwch()
        time.sleep(0.05)
    return ""


def main(argv):
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
        while True:
            w, h = shutil.get_terminal_size((120, 33))
            # config.ini [dashboard] columns/rows: layout size forced smaller than the real console (never larger: it would run off-screen)
            w, h = min(w, CFG["columns"] or w), min(h, CFG["rows"] or h)
            if (w, h) != size:  # the real console size ends up in the journal: journalctl -u nuc-console
                print(f"console {w}x{h} mode={MODE}", file=sys.stderr, flush=True)
                out.write("\x1b[2J")
                size = (w, h)
            w -= 1  # the Linux VT keeps the cursor on the last column: \x1b[K there would erase the last character
            st, sm = snapshot(w), smp.sample()
            sl = slides(sm, st["cont"], st["net"], w, h - 2, st["boot"], st["baseline"])
            now = time.time()
            idx = held if now < hold_until else pick_slide(sl, now - t0)
            out.write("\x1b[H" + frame(sl[idx % len(sl)], idx % len(sl), len(sl), w, h,
                                        safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"],
                                                      baseline=st["baseline"])))
            out.flush()
            k = ""
            if old and select.select([fd], [], [], REFRESH_S)[0]:
                raw = os.read(fd, 8)
                if not raw:  # tty closed/hangup: select always fires, without a pause it would be a busy loop
                    time.sleep(REFRESH_S)
                    continue
                k = raw.decode(errors="ignore")[:1]
            elif win_keys:
                k = windows_key(REFRESH_S)
            elif not old:
                time.sleep(REFRESH_S)
            if k.isdigit() and 1 <= int(k) <= len(PAGES):
                held, hold_until = first_slide_of(sl, int(k) - 1), time.time() + HOLD_S
                out.write("\x1b[2J")
    finally:
        out.write("\x1b[?25h\x1b[0m")
        out.flush()
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))  # --accept must be able to fail: install.sh and the user's script check the exit code
    except PermissionError as e:  # --accept as a normal user: say what to do instead of a traceback
        print(f"permission denied: {e.filename or e}: run it as " + ("administrator" if WINDOWS else "root (sudo)"), file=sys.stderr)
        sys.exit(1)
