"""nuc-console history: what happened on this machine over days and weeks, in one small SQLite file (stdlib sqlite3).

Written only by the collector (root / SYSTEM); everyone else opens it read-only (open_ro). Names and counts only: an app is
a process name (never its arguments), a log line is kept as a template with its variable parts removed (so no secret is
stored: see template()). Schema and retention: docs/DESIGN.md ("## Health").

This module owns
  - the schema, its migration (meta schema_version) and the writer (Store): one transaction per flush, WAL, retention;
  - the aggregation of the 60 s samples into per-hour rows (Window, ProcAccount, merge_host);
  - the pure classifiers that turn journal lines (Linux) and Docker state changes into events;
  - template() and clean(), which every text that reaches the database goes through.
The OS-specific readers (journalctl, Event Log, crash reports) live in collector.py, collect_windows.py, collect_darwin.py.
"""
import ipaddress
import json
import os
import re
import time
import unicodedata

try:  # a Python without SQLite: no history, nothing else breaks
    import sqlite3
except ImportError:  # pragma: no cover
    sqlite3 = None

import nuc_config

PATH = os.path.join(nuc_config.LIB_DIR, "history.db")
SCHEMA_VERSION = 1
# retention in days, pruned once a day (events are kept longer than the hourly numbers: they are few and the interesting ones)
RETENTION_DAYS = {"app_hour": 30, "host_hour": 30, "log_day": 30, "events": 90, "disk_day": 400, "boots": 400}
VACUUM_EVERY_S = 7 * 86400
KINDS = ("crash", "hang", "oom", "restart", "exit_error", "service_failed", "unexpected_shutdown", "hw_error", "throttle",
         "disk_low", "login_fail")
APP_TOP = 200                   # apps kept per hour by CPU time ...
APP_BIG_RSS = 100 * 1024 * 1024  # ... plus every app that holds at least this much memory; the rest is summed into OTHER
OTHER = "(other)"
LOG_DAY_ROWS = 3000             # distinct (source, unit, template) per day: a log flood cannot grow the file
MAX_PENDING = 120               # samples kept in memory while the database cannot be written (two hours)
SUBJECT_MAX, DETAIL_MAX, TEMPLATE_MAX = 64, 160, 160

DDL = (
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS app_hour(hour INTEGER, app TEXT, cpu_s REAL, rss_max INTEGER, rss_avg INTEGER, procs_max INTEGER,"
    " samples INTEGER, PRIMARY KEY(hour, app)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS host_hour(hour INTEGER PRIMARY KEY, cpu_avg REAL, cpu_max REAL, mem_avg REAL, mem_max REAL,"
    " swap_max REAL, temp_avg REAL, temp_max REAL, temp_high REAL, throttle INTEGER, load_max REAL, samples INTEGER)",
    "CREATE TABLE IF NOT EXISTS disk_day(day INTEGER, mount TEXT, used INTEGER, total INTEGER, PRIMARY KEY(day, mount))"
    " WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS boots(boot INTEGER PRIMARY KEY, total_s REAL, kernel TEXT)",
    "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, subject TEXT, detail TEXT, source TEXT,"
    " n INTEGER DEFAULT 1)",
    "CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts)",
    "CREATE INDEX IF NOT EXISTS events_ts ON events(ts)",
    'CREATE TABLE IF NOT EXISTS log_day(day INTEGER, source TEXT, unit TEXT, template TEXT, n INTEGER, "first" INTEGER,'
    ' "last" INTEGER, PRIMARY KEY(day, source, unit, template)) WITHOUT ROWID',
)
MIGRATIONS = {}  # {from_version: function(conn)}: upgrades schema `from_version` to `from_version + 1`, inside one transaction


# ---- reading (renderer, web, advisor: unprivileged) ---------------------------------------------------------------------

def _uri(path):
    return "file:" + path.replace("\\", "/").replace("%", "%25").replace("?", "%3f").replace("#", "%23")


def open_ro(path=None):
    """A read-only connection, or None when there is no (readable) history yet.

    WAL: while the collector runs, readers never block it and are never blocked. Once it has closed the file cleanly the -wal
    and -shm files are gone and a reader that cannot create them (unprivileged) is refused: then the file, which nobody is
    writing, is read as immutable."""
    path = path or PATH
    if sqlite3 is None or not os.path.exists(path):
        return None
    for extra in ("mode=ro", "mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(_uri(path) + "?" + extra, uri=True, timeout=2.0)
            conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            return conn
        except sqlite3.Error:
            try:
                conn.close()
            except (NameError, sqlite3.Error):
                pass
    return None


def meta_get(conn, key, default=None):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row else default


# ---- text that reaches the database --------------------------------------------------------------------------------------

NOT_PRINTED = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}  # control (terminal escapes), format (bidi, zero width), line breaks


def clean(text, n=SUBJECT_MAX):
    """A name from outside (process, container, service, file): no control or invisible characters, one line, n at most."""
    s = "" if text is None else str(text)
    if not s.isprintable():
        s = "".join(c if c in " \t" or unicodedata.category(c) not in NOT_PRINTED else " " for c in s)
    return " ".join(s.split())[:n]


KTHREAD = re.compile(r"^([A-Za-z_][\w\-]*)/(?:u?\d|R-|\d+-)")


def app_name(name):
    """The name an app is accounted under: the process name, sanitised; kernel threads ('kworker/u16:3-events', 'ksoftirqd/2')
    lose their per-CPU numbering, which would give hundreds of rows of the same thing."""
    s = clean(name)
    m = KTHREAD.match(s)
    return (m.group(1) if m else s) or "(unnamed)"


# ---- template(): a log message reduced to its shape ----------------------------------------------------------------------
# Numbers, hex ids, UUIDs, addresses, paths, quoted strings, e-mail addresses, tokens and the value after "=" (or after a
# secret-looking "key:") are replaced by placeholders: <n> <hex> <uuid> <ip> <path> <str> <email> <url> <tok> <v>. What is not
# a placeholder is the program's own wording, which is what tells two kinds of message apart. Nothing that can identify a
# person, a host or a secret is meant to survive; the unit tests feed it secrets of every shape.

_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s<>\"']*")
_BEARER = re.compile(r"\b(Bearer|Basic|Digest|Negotiate|NTLM)\s+[A-Za-z0-9+/_.~=\-]{4,}", re.I)
_DQUOTED = re.compile(r'"[^"]*"')
_SQUOTED = re.compile(r"(?<![\w'])'[^']*'(?!\w)")
_BQUOTED = re.compile(r"`[^`']*['`]")
_OPEN_QUOTE = re.compile(r"""(?:"|(?<![\w'])'|`).*$""")  # a quote that never closes (a cut message): it swallows the rest
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)*")
_UNIT_SUFFIX = re.compile(r"\.(?:service|socket|timer|slice|scope|mount|automount|target|path|device|swap)$")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_JWT = re.compile(r"\beyJ[\w\-]*(?:\.[\w\-]+){1,2}|\b[\w\-]{10,}\.[\w\-]{10,}\.[\w\-]{10,}\b")
_IP6 = re.compile(r"(?<![0-9A-Za-z:.])\[?(?:[0-9A-Fa-f]{0,4}:){2,7}(?:[0-9A-Fa-f]{0,4}|\d{1,3}(?:\.\d{1,3}){3})"
                  r"(?:%[0-9A-Za-z._\-]+)?\]?(?![0-9A-Za-z:])")
_IP4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b(?:/\d{1,2})?(?::\d{1,5})?")
_WINPATH = re.compile(r"\b[A-Za-z]:[\\/][^\s\"'<>|]*|\\\\[^\s\"'<>|]+|\S*\\\S*")
_UNIXPATH = re.compile(r"(?<![\w/.\-])(?:~|\.\.?)?/[^\s\"'<>|;,)\]]+")
_RELPATH = re.compile(r"\b[\w.\-@+]+(?:/[\w.\-@+]+){2,}")
_MAC = re.compile(r"\b(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}\b")
_HEX = re.compile(r"\b0[xX][0-9a-fA-F]+\b|\b[0-9a-fA-F]{8,}\b")
_FLAG = re.compile(r"(?i)(?<!\S)(-{1,2}[\w\-]*?(?:pass(?:word|wd|phrase)?|secret|token|key|auth|cred\w*)[\w\-]*)([=\s]+)(?!-)\S+")
_LONGTOK = re.compile(r"[A-Za-z0-9+/_=\-]{16,}")
_KV = re.compile(r"(?P<k>[A-Za-z_][\w.\-]*)=\S*")
_COLONKV = re.compile(r"(?P<k>[A-Za-z_][\w.\-]*):(?=[^\s:])\S+")
_SECRET_KEY = re.compile(r"(?i)\b(pass(?:word|wd|phrase)?|pwd|secret|token|api[_\- ]?key|auth(?:orization)?|bearer|cookie|"
                         r"credentials?|session(?:[_\-]?id)?|private[_\- ]?key|access[_\- ]?key)\b\s*(?::|\bis\b)\s*\S+")
_SECRET_WORD = re.compile(r"(?i)\b(pass(?:words?|wd|phrase|code)?|pwd|secret|token|api[_\- ]?key|pin)\s+(?![<\-])(?=\S*[\d!@#$%^&*+=?|~\\/])\S+")
_USER = re.compile(r"(?<![\w<])(user(?:name)?)\s+(?!<|is\b|from\b|for\b|not\b|at\b|on\b|in\b|and\b)\S+")
_NUM = re.compile(r"\d+")
_NUMS = re.compile(r"<n>(?:[.,:/\-]<n>)+")
_SPACES = re.compile(r"\s+")


def _email(m):
    return m.group(0) if _UNIT_SUFFIX.search(m.group(0)) else "<email>"


def _ip6(m):
    cand = m.group(0).strip("[]").split("%")[0]
    try:
        ipaddress.IPv6Address(cand)
    except ValueError:
        return m.group(0)  # a time of day, a pair of ids: not an address
    return "<ip>"


def _longtok(m):
    t = m.group(0)
    mixed = re.search(r"\d", t) and re.search(r"[A-Za-z]", t)
    return "<tok>" if mixed or len(t) >= 40 or re.search(r"[+/=]", t) else t


def template(text, limit=TEMPLATE_MAX):
    """A log message reduced to its shape (see above), one line, `limit` characters at most. Never raises."""
    try:
        s = "" if text is None else str(text)[:4000]
        s = next((ln for ln in s.splitlines() if ln.strip()), "")
        s = "".join(c if unicodedata.category(c) not in NOT_PRINTED else " " for c in s)
        s = _URL.sub("<url>", s)
        s = _BEARER.sub(r"\1 <tok>", s)
        for rx in (_DQUOTED, _SQUOTED, _BQUOTED, _OPEN_QUOTE):
            s = rx.sub("<str>", s)
        s = _EMAIL.sub(_email, s)
        s = _UUID.sub("<uuid>", s)
        s = _JWT.sub("<tok>", s)
        s = _IP6.sub(_ip6, s)
        s = _IP4.sub("<ip>", s)
        s = _WINPATH.sub("<path>", s)
        s = _UNIXPATH.sub("<path>", s)
        s = _RELPATH.sub("<path>", s)
        s = _MAC.sub("<hex>", s)
        s = _HEX.sub("<hex>", s)
        s = _FLAG.sub(r"\1\2<v>", s)
        s = _KV.sub(r"\g<k>=<v>", s)
        s = _COLONKV.sub(r"\g<k>:<v>", s)
        s = _LONGTOK.sub(_longtok, s)
        s = _SECRET_KEY.sub(lambda m: m.group(1) + ": <v>", s)
        s = _SECRET_WORD.sub(r"\1 <v>", s)  # "pass s3cr3t!": a secret said after its name without a separator
        s = _USER.sub(r"\1 <str>", s)
        s = _NUM.sub("<n>", s)
        s = _NUMS.sub("<n>", s)
        s = _SPACES.sub(" ", s).strip()
        if len(s) > limit:
            s = s[:limit]
            if s.rfind("<") > s.rfind(">"):  # not in the middle of a placeholder
                s = s[:s.rfind("<")]
            s = s.rstrip()
        return s
    except Exception:  # noqa: BLE001 - a log line must never stop the history
        return "<unparsable>"


# ---- aggregation of the 60 s samples -------------------------------------------------------------------------------------

def _avg(a, na, b, nb):
    """Average of two averages over na and nb samples; None (never measured) is ignored."""
    if a is None or not na:
        return b
    if b is None or not nb:
        return a
    return (a * na + b * nb) / float(na + nb)


def _max(a, b):
    return b if a is None else a if b is None else max(a, b)


def _sum(a, b):
    return b if a is None else a if b is None else a + b


def _r(x, d=2):
    return None if x is None else round(x, d)


HOST_COLS = ("cpu_avg", "cpu_max", "mem_avg", "mem_max", "swap_max", "temp_avg", "temp_max", "temp_high", "throttle", "load_max",
             "samples")


def host_row(snaps):
    """[{'cpu', 'mem', 'swap', 'load', 'temp', 'temp_high', 'throttle'}] of one hour -> dict of the host_hour columns."""
    def vals(k):
        return [s[k] for s in snaps if s.get(k) is not None]
    cpu, mem, swap, load, temp, thr = vals("cpu"), vals("mem"), vals("swap"), vals("load"), vals("temp"), vals("throttle")
    high = vals("temp_high")
    return {"cpu_avg": _r(sum(cpu) / len(cpu)) if cpu else None, "cpu_max": _r(max(cpu)) if cpu else None,
            "mem_avg": _r(sum(mem) / len(mem)) if mem else None, "mem_max": _r(max(mem)) if mem else None,
            "swap_max": _r(max(swap)) if swap else None,
            "temp_avg": _r(sum(temp) / len(temp), 1) if temp else None, "temp_max": _r(max(temp), 1) if temp else None,
            "temp_high": _r(high[-1], 1) if high else None,  # the sensor's own "high" threshold (latest known)
            "throttle": int(sum(thr)) if thr else None, "load_max": _r(max(load)) if load else None,
            "samples": len(snaps)}


def merge_host(old, new):
    """Two host_hour dicts of the same hour -> the row as if all samples had gone into one (averages weighted by samples)."""
    no, nn = old["samples"] or 0, new["samples"] or 0
    return {"cpu_avg": _r(_avg(old["cpu_avg"], no, new["cpu_avg"], nn)), "cpu_max": _max(old["cpu_max"], new["cpu_max"]),
            "mem_avg": _r(_avg(old["mem_avg"], no, new["mem_avg"], nn)), "mem_max": _max(old["mem_max"], new["mem_max"]),
            "swap_max": _max(old["swap_max"], new["swap_max"]),
            "temp_avg": _r(_avg(old["temp_avg"], no, new["temp_avg"], nn), 1), "temp_max": _max(old["temp_max"], new["temp_max"]),
            "temp_high": new["temp_high"] if new["temp_high"] is not None else old["temp_high"],
            "throttle": _sum(old["throttle"], new["throttle"]), "load_max": _max(old["load_max"], new["load_max"]),
            "samples": no + nn}


def app_row(snaps):
    """[(cpu_s, rss, procs)] of one app over the samples of one hour -> dict of the app_hour columns."""
    rss = [s[1] for s in snaps]
    return {"cpu_s": round(sum(s[0] for s in snaps), 3), "rss_max": int(max(rss)), "rss_avg": int(round(sum(rss) / float(len(rss)))),
            "procs_max": int(max(s[2] for s in snaps)), "samples": len(snaps)}


def merge_app(old, new):
    so, sn = old["samples"] or 0, new["samples"] or 0
    tot = float(so + sn) or 1.0
    return {"cpu_s": round(old["cpu_s"] + new["cpu_s"], 3), "rss_max": max(old["rss_max"], new["rss_max"]),
            "rss_avg": int(round((old["rss_avg"] * so + new["rss_avg"] * sn) / tot)),
            "procs_max": max(old["procs_max"], new["procs_max"]), "samples": so + sn}


class Window(object):
    """The samples taken since the last flush (every 60 s, flushed every 5 minutes): rows() turns them into app_hour and
    host_hour rows, applying the cap on the number of apps per hour."""

    def __init__(self):
        self.snaps = []  # [(ts, {app: [cpu_s, rss, procs]}, host dict)]

    def add(self, ts, apps, host):
        if not apps and not any(v is not None for v in (host or {}).values()):
            return  # nothing was read at all
        self.snaps.append((ts, apps or {}, host or {}))
        del self.snaps[:-MAX_PENDING]

    def clear(self):
        self.snaps = []

    def rows(self, top=APP_TOP, big=APP_BIG_RSS):
        """-> ({(hour, app): app_hour dict}, {hour: host_hour dict})"""
        by_hour = {}
        for ts, apps, host in self.snaps:
            by_hour.setdefault(int(ts // 3600), []).append((apps, host))
        app_rows, host_rows = {}, {}
        for hour, items in by_hour.items():
            host_rows[hour] = host_row([h for _, h in items])
            per = {}  # app -> [(cpu_s, rss, procs)] one entry per sample in which it ran
            for apps, _ in items:
                for name, (cpu_s, rss, n) in apps.items():
                    per.setdefault(name, []).append((cpu_s, rss, n))
            rows = {name: app_row(s) for name, s in per.items()}
            keep = set(sorted(rows, key=lambda a: (rows[a]["cpu_s"], rows[a]["rss_max"]), reverse=True)[:top])
            keep.update(a for a, r in rows.items() if r["rss_max"] >= big)
            rest = [a for a in rows if a not in keep]
            for a in keep:
                app_rows[(hour, a)] = rows[a]
            if rest:  # summed per sample, so rss_max is the most the small apps held at the same time
                merged = []
                for apps, _ in items:
                    part = [v for a, v in apps.items() if a in rest]
                    if part:
                        merged.append((sum(v[0] for v in part), sum(v[1] for v in part), sum(v[2] for v in part)))
                if merged:
                    app_rows[(hour, OTHER)] = app_row(merged)
        return app_rows, host_rows


class ProcAccount(object):
    """CPU seconds per app from successive process lists (procs.ProcSampler rows: pid, name, mem, time, start, cpu).

    The CPU time each process has used since it started (`time`) is compared with the previous list: exact over any interval
    (the sampler's own cpu% is measured against a short window and is None after a long gap). A process first seen since the
    previous list had used nothing before it; one without a readable `time` falls back to cpu% * elapsed."""

    def __init__(self, ncpu=None, max_elapsed=300.0):
        self.prev, self.prev_t, self.prev_wall = {}, None, None
        self.ncpu = ncpu or os.cpu_count() or 1
        self.max_elapsed = max_elapsed

    def update(self, procs, mono, wall):
        """-> {app: [cpu_s used since the previous call, rss bytes (all its processes), process count]}"""
        elapsed = mono - self.prev_t if self.prev_t is not None else None
        apps, cur = {}, {}
        for p in procs:
            try:
                name = app_name(p.get("name"))
                pid, t, start = p.get("pid"), p.get("time"), p.get("start")
                row = apps.setdefault(name, [0.0, 0, 0])
                row[1] += int(p.get("mem") or 0)
                row[2] += 1
                if t is not None:
                    cur[pid] = (start, t)
                if elapsed is None or elapsed <= 0:
                    continue
                old = self.prev.get(pid)
                used = None
                if t is not None and old is not None and (start is None or old[0] is None or abs(start - old[0]) <= 2) and t >= old[1]:
                    used = t - old[1]
                elif t is not None and (old is not None or (start is not None and self.prev_wall is not None and start >= self.prev_wall - 2)):
                    used = t  # a new process (or a pid reused): all of it was used since the previous list
                elif p.get("cpu") is not None:
                    used = p["cpu"] / 100.0 * min(elapsed, self.max_elapsed)
                if used:
                    row[0] += min(max(used, 0.0), self.ncpu * max(elapsed, 1.0))  # a bogus counter cannot invent days of CPU
            except (TypeError, ValueError, AttributeError):
                continue
        self.prev, self.prev_t, self.prev_wall = cur, mono, wall
        return apps


def pick_temp(temps, sensors=None, now=None, max_age=120):
    """-> (temperature in C, the sensor's 'high' threshold or None): the package temperature, else the hottest core, else the
    hottest CPU sensor. temps is cpuinfo's "temps" dict; sensors the collector's sensors.json (macOS/Windows), used when
    cpuinfo has nothing and the file is fresh. None when nothing is known (never a made-up number)."""
    def best(d):
        if not isinstance(d, dict):
            return None
        pkg = d.get("package")
        if isinstance(pkg, (int, float)):
            return float(pkg)
        cores = [v for v in (d.get("cores") or {}).values() if isinstance(v, (int, float))]
        if cores:
            return float(max(cores))
        sens = [s.get("c") for s in d.get("sensors") or [] if isinstance(s, dict) and isinstance(s.get("c"), (int, float))]
        return float(max(sens)) if sens else None
    t = best(temps)
    high = temps.get("high") if isinstance(temps, dict) and isinstance(temps.get("high"), (int, float)) else None
    if t is None and isinstance(sensors, dict):
        ts = sensors.get("ts")
        if isinstance(ts, (int, float)) and now is not None and abs(now - ts) <= max_age:
            t = best(sensors.get("cpu"))
    return t, high


def throttle_count(throttle):
    """cpuinfo's "throttle" dict -> one cumulative counter (package, else the sum of the cores), or None."""
    if not isinstance(throttle, dict):
        return None
    if isinstance(throttle.get("package"), (int, float)):
        return int(throttle["package"])
    cores = [v for v in (throttle.get("cores") or {}).values() if isinstance(v, (int, float))]
    return int(sum(cores)) if cores else None


def mem_percent(info):
    """{'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'} (bytes, the /proc/meminfo keys) -> (memory %, swap %); swap None
    when there is none (or the OS does not say)."""
    total, avail = info.get("MemTotal"), info.get("MemAvailable")
    mem = max(0.0, min(100.0, (total - avail) * 100.0 / total)) if total and avail is not None else None
    st, sf = info.get("SwapTotal"), info.get("SwapFree")
    swap = max(0.0, min(100.0, (st - sf) * 100.0 / st)) if st and sf is not None else None
    return mem, swap


def parse_meminfo(text):
    out = {}
    for ln in text.splitlines():
        k, _, v = ln.partition(":")
        f = v.split()
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree") and f and f[0].isdigit():
            out[k] = int(f[0]) * 1024
    return out


REAL_FS = ("ext4", "ext3", "xfs", "btrfs", "vfat", "f2fs", "zfs", "ntfs3", "exfat", "nfs", "nfs4", "cifs")  # as render.py


def parse_mounts(text):
    """/proc/mounts -> [mountpoint] of real filesystems only, once per device (the filter the DISKS section uses)."""
    seen, out = set(), []
    for ln in text.splitlines():
        f = ln.split()
        if len(f) >= 3 and f[2] in REAL_FS and f[0] not in seen and not f[1].startswith("/var/lib/docker"):
            seen.add(f[0])
            out.append(f[1].replace("\\040", " "))
    return out


# ---- events from the Linux journal (pure) --------------------------------------------------------------------------------

# journalctl, run by the collector (fixed argument lists; the cursor is the only variable part, validated): three sources,
# each with a cursor in meta. Warnings and worse; kernel notices (a segfault is KERN_INFO); sshd (INFO: priority <= 4 only
# would never see a failed password). Each line is classified once: login_fail only in "sshd", the rest in "journal"
# (priority <= 4) and "kernel" (5-6), so no message is counted twice.
JOURNAL_FIELDS = "MESSAGE,PRIORITY,SYSLOG_IDENTIFIER,_SYSTEMD_UNIT,_TRANSPORT,UNIT,COREDUMP_EXE,COREDUMP_COMM"
JOURNAL_SOURCES = (
    ("journal", ("-p", "0..4")),
    ("kernel", ("_TRANSPORT=kernel", "-p", "5..6")),
    ("sshd", ("-t", "sshd", "-t", "sshd-session")),
)
JOURNAL_CAP = 5000
CURSOR = re.compile(r"^[A-Za-z0-9=;_\-]{1,512}$")

OOM = re.compile(r"[Oo]ut of memory: Killed process \d+ \((.*?)\)")
SEGV = re.compile(r"^(?:traps: )?(.+?)\[\d+\]:? (segfault at |general protection fault|trap )")
FAILED = re.compile(r"^(\S+): Failed with result '([^']*)'")
MAIN_EXIT = re.compile(r"^(\S+): Main process exited, code=(\w+), status=(\d+)(?:/(\w+))?")
RESULTS = ("exit-code", "signal", "core-dump", "timeout", "watchdog", "resources", "protocol", "start-limit-hit", "oom-kill",
           "success")
SSH_FAIL = ("Failed password", "Invalid user")


def _text(v):
    """A journal field: a string, or (non UTF-8 data) a list of byte values."""
    if isinstance(v, str):
        return v
    if isinstance(v, list) and all(isinstance(b, int) and 0 <= b < 256 for b in v[:4096]):
        return bytes(v[:4096]).decode("utf-8", "replace")
    return ""


D_COREDUMP = "core dumped"


def classify_journal(d, source="journal"):
    """One journal entry (dict from `journalctl -o json`) -> [(kind, subject, detail)]. Messages are matched, never stored:
    subjects are names, details fixed words (the result of a unit, a signal name) - never text from the line. Users and
    addresses of failed ssh logins are not even looked at."""
    msg = _text(d.get("MESSAGE"))
    ident = _text(d.get("SYSLOG_IDENTIFIER"))
    out = []
    if source == "sshd":
        if ident in ("sshd", "sshd-session") and msg.startswith(SSH_FAIL):
            out.append(("login_fail", "sshd", "failed ssh login"))
        return out
    exe = _text(d.get("COREDUMP_EXE"))
    if exe:
        out.append(("crash", clean(re.split(r"[\\/]", exe.rstrip("\\/"))[-1]) or "(unknown)", D_COREDUMP))
    if _text(d.get("_TRANSPORT")) == "kernel" or ident == "kernel":
        m = OOM.search(msg)
        if m:
            out.append(("oom", clean(m.group(1)) or "(unknown)", "kernel OOM killer"))
        m = SEGV.match(msg)
        if m:
            out.append(("crash", clean(m.group(1)) or "(unknown)",
                        "segmentation fault" if m.group(2).startswith("segfault") else "fault in the process"))
    if source == "kernel":
        return out
    m = FAILED.match(msg)
    if m:
        res = m.group(2) if m.group(2) in RESULTS else "other"
        out.append(("service_failed", clean(m.group(1)) or "(unknown)", "failed with result " + res))
    m = MAIN_EXIT.match(msg)
    if m:
        unit, code, status, sig = m.groups()
        sig = clean(sig or "", 16)
        if code == "dumped":
            out.append(("crash", clean(unit) or "(unknown)", "main process dumped core" + (" (%s)" % sig if sig else "")))
        elif code == "killed":
            out.append(("exit_error", clean(unit) or "(unknown)", "main process killed" + (" (%s)" % sig if sig else "")))
        elif code == "exited" and status != "0":
            out.append(("exit_error", clean(unit) or "(unknown)", "main process exited with status " + status))
    return out


def _same_app(a, b):
    """Kernel names are the process name cut at 15 characters, unit names carry '.service': one app, three spellings."""
    a, b = re.sub(r"\.service$", "", a), re.sub(r"\.service$", "", b)
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def dedupe_crashes(rows):
    """Events (merge_events rows) of one cycle -> the same without the doubles: a crash with a core dump is reported by the
    kernel (segfault), by systemd (main process dumped core) and by systemd-coredump. Per app the crashes are the most any one
    of the three counted; the core dump rows stay as they are and the others are cut down by as many as they cover. Rows of
    other kinds, and crashes of other apps, are never touched."""
    groups = []  # crash rows of one app (three spellings of its name) each
    for r in rows:
        if r["kind"] == "crash":
            for g in groups:
                if any(_same_app(r["subject"], o["subject"]) for o in g):
                    g.append(r)
                    break
            else:
                groups.append([r])
    new_n = {}  # id(row) -> its count now (0: dropped)
    for g in groups:
        core = sum(r["n"] for r in g if r["detail"] == D_COREDUMP)
        readers = {}
        for r in g:
            if r["detail"] != D_COREDUMP:
                readers.setdefault(r["detail"].startswith("main process"), []).append(r)
        best = max(readers.values(), key=lambda rs: sum(r["n"] for r in rs), default=[])
        left = core
        for r in best:
            take = min(left, r["n"])
            left -= take
            new_n[id(r)] = r["n"] - take
        for rs in readers.values():
            for r in rs:
                new_n.setdefault(id(r), 0)
    return [r if id(r) not in new_n else dict(r, n=new_n[id(r)]) for r in rows if new_n.get(id(r), 1) > 0]


def parse_journal_events(text, source="journal", cap=None, now=None):
    """`journalctl -o json` output (one JSON object per line) -> {'events', 'logs', 'cursor', 'ts', 'read', 'capped'}.

    events: [{'ts', 'kind', 'subject', 'detail', 'source', 'n'}], one per (kind, subject, detail) with its count;
    logs (source "journal" only): [{'ts', 'source', 'unit', 'template'}] of every message of priority 3 or worse;
    cursor/ts: of the last entry read (None if there was none). At most `cap` entries are read: `capped` says there are more
    (the cursor then points at the last one read: the next pass goes on from there)."""
    now = int(now if now is not None else time.time())
    cap = JOURNAL_CAP if cap is None else cap
    lines = [ln for ln in text.splitlines() if ln.strip()]
    events, logs, cursor, last_ts, read = [], [], None, None, 0
    for ln in lines[:cap]:
        try:
            d = json.loads(ln)
            if not isinstance(d, dict):
                continue
        except ValueError:
            continue
        read += 1
        if isinstance(d.get("__CURSOR"), str) and CURSOR.match(d["__CURSOR"]):
            cursor = d["__CURSOR"]
        try:
            ts = int(int(d["__REALTIME_TIMESTAMP"]) // 1000000)
        except (KeyError, TypeError, ValueError):
            ts = now
        last_ts = ts
        for kind, subject, detail in classify_journal(d, source):
            events.append({"ts": ts, "kind": kind, "subject": subject, "detail": detail, "source": "journal", "n": 1})
        if source == "journal":
            try:
                pr = int(d.get("PRIORITY", 7))
            except (TypeError, ValueError):
                pr = 7
            if pr <= 3:
                unit = clean(_text(d.get("_SYSTEMD_UNIT")) or _text(d.get("SYSLOG_IDENTIFIER")) or "?")
                logs.append({"ts": ts, "source": "journal", "unit": unit or "?", "template": template(_text(d.get("MESSAGE")))})
    return {"events": merge_events(events), "logs": logs, "cursor": cursor, "ts": last_ts, "read": read, "capped": len(lines) > cap}


def merge_events(events):
    """Events of one pass with the same (kind, subject, detail, source) -> one row with its count; ts = the last one."""
    out = {}
    for e in events:
        key = (e["kind"], e["subject"], e["detail"], e["source"])
        row = out.get(key)
        if row is None:
            out[key] = {"ts": int(e["ts"]), "kind": e["kind"], "subject": e["subject"], "detail": e["detail"],
                        "source": e["source"], "n": int(e.get("n", 1))}
        else:
            row["ts"] = max(row["ts"], int(e["ts"]))
            row["n"] += int(e.get("n", 1))
    return sorted(out.values(), key=lambda r: (r["ts"], r["kind"], r["subject"]))


# ---- events from the Docker state (pure) ---------------------------------------------------------------------------------

DOCKER_FORMAT = "{{.Name}}|{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.ExitCode}}|{{.State.Status}}|{{.State.StartedAt}}"
CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")


def parse_docker_state(text):
    """`docker inspect --format DOCKER_FORMAT` -> {name: {'restarts', 'oom', 'exit', 'status', 'started'}}"""
    out = {}
    for ln in text.splitlines():
        f = ln.rsplit("|", 5)
        if len(f) != 6:
            continue
        name, restarts, oom, code, status, started = f
        try:
            out[clean(name.lstrip("/"))] = {"restarts": int(restarts), "oom": oom.strip().lower() == "true", "exit": int(code),
                                            "status": clean(status, 16), "started": clean(started, 40)}
        except ValueError:
            continue
        if len(out) >= 500:
            break
    return out


def docker_events(prev, cur, now):
    """Two container states of the same host (None for the first look: nothing is reported, it is only the baseline) ->
    events: restart (the restart counter went up, or it was running and has started again), oom (OOMKilled newly set),
    exit_error (newly exited with a code other than 0)."""
    if prev is None:
        return []
    ev = []

    def add(kind, name, detail, n=1):
        ev.append({"ts": int(now), "kind": kind, "subject": name, "detail": detail, "source": "docker", "n": n})
    for name, c in sorted(cur.items()):
        p = prev.get(name)
        started_again = p is not None and c["started"] != p["started"]
        oom = c["oom"] and (p is None or not p["oom"] or started_again)
        rs = c["restarts"] - p["restarts"] if p is not None and c["restarts"] > p["restarts"] else 0
        if rs == 0 and p is not None and started_again and p["status"] in ("running", "restarting") and c["status"] != "created":
            rs = 1
        if rs > 0:
            add("restart", name, "container restarted", rs)
        if oom:
            add("oom", name, "container killed for memory (OOMKilled)")
        elif c["status"] == "exited" and c["exit"] != 0 and (p is None or p["status"] != "exited" or p["exit"] != c["exit"] or started_again):
            add("exit_error", name, "exited with code %d" % c["exit"])
    return ev


# ---- the writer (collector only) -----------------------------------------------------------------------------------------

class Store(object):
    """The history database, opened for writing. One thread uses it (the collector's history thread). Every flush is a single
    transaction; a failed one is rolled back and the connection reopened on the next call, so a full disk or a locked file
    costs the data of that flush at most, never the thread."""

    def __init__(self, path=None, now=time.time):
        self.path = path or PATH
        self.now = now
        self.conn = None
        self.connect()

    # -- connection --

    def connect(self):
        if sqlite3 is None:
            raise RuntimeError("this Python has no sqlite3")
        if self.conn is not None:
            return self.conn
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            conn = self._open()
        except sqlite3.DatabaseError as e:  # not a database (damaged): keep it for a look, start again
            if "not a database" not in str(e) and "malformed" not in str(e):
                raise  # locked, disk I/O error, no space...: not a reason to throw the history away
            os.replace(self.path, "%s.corrupt-%d" % (self.path, int(self.now())))
            conn = self._open()
        self.conn = conn
        return conn

    def _open(self):
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
        try:
            if os.name == "posix":
                os.chmod(self.path, 0o644)  # before WAL creates its files: they take the mode of this one
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            migrate(conn, self.now())
            self.set_cores(os.cpu_count() or 1, conn)
            self._chmod()
        except Exception:
            conn.close()
            raise
        return conn

    def _chmod(self):
        if os.name == "posix":  # readers are unprivileged; on Windows the files inherit the ACL of the directory
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.chmod(self.path + suffix, 0o644)
                except OSError:
                    pass

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None

    def tx(self):
        return _Tx(self)

    # -- meta --

    def meta(self, key, default=None):
        return meta_get(self.connect(), key, default)

    def set_meta(self, key, value):
        with self.tx() as c:
            _set_meta(c, key, value)

    def set_cores(self, n, conn=None):
        """meta "cores": the number of logical CPUs (health.py needs it for 'half of all cores'). Written only when it changes."""
        conn = conn or self.connect()
        row = conn.execute("SELECT value FROM meta WHERE key = 'cores'").fetchone()
        if not row or row[0] != str(int(n)):
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('cores', ?)", (str(int(n)),))

    # -- writes --

    def add_hours(self, app_rows, host_rows, meta=None):
        """Adds one flush to app_hour / host_hour: a row of an hour that already exists is added to (CPU seconds summed,
        maxima kept, averages weighted by their samples). One transaction."""
        with self.tx() as c:
            for (hour, app), r in app_rows.items():
                old = c.execute("SELECT cpu_s, rss_max, rss_avg, procs_max, samples FROM app_hour WHERE hour = ? AND app = ?",
                                (hour, app)).fetchone()
                if old:
                    r = merge_app(dict(zip(("cpu_s", "rss_max", "rss_avg", "procs_max", "samples"), old)), r)
                c.execute("INSERT OR REPLACE INTO app_hour VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (hour, app, r["cpu_s"], r["rss_max"], r["rss_avg"], r["procs_max"], r["samples"]))
            for hour in {h for h, _ in app_rows}:
                self._cap_hour(c, hour)
            for hour, r in host_rows.items():
                old = c.execute("SELECT %s FROM host_hour WHERE hour = ?" % ", ".join(HOST_COLS), (hour,)).fetchone()
                if old:
                    r = merge_host(dict(zip(HOST_COLS, old)), r)
                c.execute("INSERT OR REPLACE INTO host_hour VALUES (?, %s)" % ", ".join("?" * len(HOST_COLS)),
                          (hour,) + tuple(r[k] for k in HOST_COLS))
            for k, v in (meta or {}).items():
                _set_meta(c, k, v)

    @staticmethod
    def _cap_hour(c, hour):
        """The cap per hour (APP_TOP apps by CPU time, plus the big ones) over the whole hour: every flush keeps its own top
        apps, so an hour that saw different apps in each flush can hold more; the excess is summed into OTHER."""
        rows = c.execute("SELECT app, cpu_s, rss_max, rss_avg, procs_max, samples FROM app_hour WHERE hour = ? AND app != ?",
                         (hour, OTHER)).fetchall()
        if len(rows) <= APP_TOP:
            return
        rest = [r for r in sorted(rows, key=lambda r: (r[1], r[2]), reverse=True)[APP_TOP:] if r[2] < APP_BIG_RSS]
        if not rest:
            return
        old = c.execute("SELECT cpu_s, rss_max, rss_avg, procs_max, samples FROM app_hour WHERE hour = ? AND app = ?",
                        (hour, OTHER)).fetchone() or (0.0, 0, 0, 0, 0)
        c.executemany("DELETE FROM app_hour WHERE hour = ? AND app = ?", [(hour, r[0]) for r in rest])
        c.execute("INSERT OR REPLACE INTO app_hour VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (hour, OTHER, round(old[0] + sum(r[1] for r in rest), 3), max(old[1], sum(r[2] for r in rest)),
                   old[2] + sum(r[3] for r in rest), old[3] + sum(r[4] for r in rest), max([old[4]] + [r[5] for r in rest])))

    def add_events(self, events=(), logs=(), meta=None):
        """Events, log templates and the cursors that say they were read: one transaction (a crash in between can neither
        lose nor repeat them). events: dicts as merge_events() returns; logs: [{'ts', 'source', 'unit', 'template'}]."""
        horizon = self.now() - RETENTION_DAYS["events"] * 86400
        per_day = {}
        for g in logs:
            key = (int(g["ts"] // 86400), clean(g["source"], 16), clean(g["unit"], SUBJECT_MAX) or "?",
                   clean(g["template"], TEMPLATE_MAX) or "?")
            row = per_day.setdefault(key, [0, int(g["ts"]), int(g["ts"])])
            row[0] += 1
            row[1], row[2] = min(row[1], int(g["ts"])), max(row[2], int(g["ts"]))
        with self.tx() as c:
            for e in events:
                if e["kind"] not in KINDS or e["ts"] < horizon:
                    continue
                c.execute("INSERT INTO events(ts, kind, subject, detail, source, n) VALUES (?, ?, ?, ?, ?, ?)",
                          (int(e["ts"]), e["kind"], clean(e["subject"]) or "(unknown)", clean(e["detail"], DETAIL_MAX),
                           clean(e["source"], 16), int(e.get("n", 1))))
            counted = {}
            for (day, source, unit, tmpl), (n, first, last) in per_day.items():
                cur = c.execute('UPDATE log_day SET n = n + ?, "first" = min("first", ?), "last" = max("last", ?) '
                                "WHERE day = ? AND source = ? AND unit = ? AND template = ?", (n, first, last, day, source, unit, tmpl))
                if cur.rowcount:
                    continue
                if day not in counted:
                    counted[day] = c.execute("SELECT COUNT(*) FROM log_day WHERE day = ?", (day,)).fetchone()[0]
                if counted[day] >= LOG_DAY_ROWS:
                    continue
                c.execute("INSERT INTO log_day VALUES (?, ?, ?, ?, ?, ?, ?)", (day, source, unit, tmpl, n, first, last))
                counted[day] += 1
            for k, v in (meta or {}).items():
                _set_meta(c, k, v)

    def put_disks(self, day, rows):
        """rows: [{'mount', 'used', 'total'}]: one reading a day per mount (the last of the day stays)."""
        with self.tx() as c:
            for r in rows:
                c.execute("INSERT OR REPLACE INTO disk_day VALUES (?, ?, ?, ?)",
                          (int(day), clean(r["mount"], 120), int(r["used"]), int(r["total"])))

    def put_boot(self, boot, total_s, kernel):
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO boots VALUES (?, ?, ?)", (int(boot), float(total_s), clean(kernel, 96) or None))

    # -- retention --

    def prune(self, now=None):
        """Deletes what is older than its retention; returns the number of rows removed per table."""
        now = int(now if now is not None else self.now())
        cut = {"app_hour": now // 3600 - RETENTION_DAYS["app_hour"] * 24, "host_hour": now // 3600 - RETENTION_DAYS["host_hour"] * 24,
               "log_day": now // 86400 - RETENTION_DAYS["log_day"], "events": now - RETENTION_DAYS["events"] * 86400,
               "disk_day": now // 86400 - RETENTION_DAYS["disk_day"], "boots": now - RETENTION_DAYS["boots"] * 86400}
        col = {"app_hour": "hour", "host_hour": "hour", "log_day": "day", "events": "ts", "disk_day": "day", "boots": "boot"}
        gone = {}
        with self.tx() as c:
            for table, limit in cut.items():
                gone[table] = c.execute("DELETE FROM %s WHERE %s < ?" % (table, col[table]), (limit,)).rowcount
        return gone

    def vacuum_if_due(self, now=None):
        """VACUUM (and a WAL checkpoint) at most once a week; True if it ran."""
        now = int(now if now is not None else self.now())
        last = self.meta("last_vacuum")
        if last and last.isdigit() and now - int(last) < VACUUM_EVERY_S:
            return False
        c = self.connect()
        try:
            c.execute("VACUUM")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            return False  # a reader holds it, or no space: next time
        self.set_meta("last_vacuum", str(now))
        return True


def _set_meta(c, key, value):
    c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value if value is None else str(value)))


class _Tx(object):
    """`with store.tx() as conn:` BEGIN IMMEDIATE ... COMMIT, or ROLLBACK (and the connection dropped) on an error."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.conn = self.store.connect()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            self.store.close()
            raise
        return self.conn

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            self.store.close()  # reopened on the next use
            if et is None:
                raise
        return False


def migrate(conn, now=None):
    """Creates the schema or brings an older one up to SCHEMA_VERSION. Safe to run again and again (idempotent); a database
    written by a NEWER version is refused (RuntimeError) rather than damaged."""
    now = int(now if now is not None else time.time())
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    version = int(row[0]) if row and str(row[0]).isdigit() else 0
    if version > SCHEMA_VERSION:
        raise RuntimeError("history.db is schema %d, this nuc-console knows %d: upgrade nuc-console" % (version, SCHEMA_VERSION))
    conn.execute("BEGIN IMMEDIATE")
    try:
        for stmt in DDL:
            conn.execute(stmt)
        while version < SCHEMA_VERSION:
            if version in MIGRATIONS:
                MIGRATIONS[version](conn)
            version += 1
        conn.execute("INSERT OR IGNORE INTO meta VALUES ('created', ?)", (str(now),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('os', ?)", (nuc_config.OS_NAME,))  # health.py picks its fix texts by it
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def json_meta(store, key, default=None):
    """A meta value kept as JSON (a set of container states, the last errors)."""
    try:
        v = store.meta(key)
        return json.loads(v) if v else default
    except ValueError:
        return default
