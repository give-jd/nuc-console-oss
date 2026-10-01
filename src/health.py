"""nuc-console HEALTH: deterministic findings over the history (which apps cause trouble, disks filling up, hot hours).

report(conn, now, days) returns the dict described in docs/DESIGN.md ("## Health"). Pure: it only reads the (read-only)
history connection, and every rule is a small function over SQL aggregates. Each finding carries a stable id
"<rule>:<subject>", a level (err, warn, info), one sentence with the numbers, and a fixed `fix` text (commands per OS, with
<name>/<unit> placeholders: a name from the database is data and is never put into a command). Names and templates come
from the database: they are cleaned (control and formatting characters removed, length capped) before they reach a text.

Units in the history: RSS in bytes, CPU in seconds per hour, host cpu/mem/swap in percent (0-100), temperatures in C.
With less than 24 h of data the rules that need days (memory trend, disk forecast, new log messages, login spikes) say
"collecting" instead of concluding.
"""
import math
import os
import time
import unicodedata

# ---- thresholds (the contract fixes the numbers; the rest are documented guards) -------------------------------------------
CPU_SHARE_INFO = 0.25         # the top app is reported (info) when it used >= 25% of all CPU time ...
CPU_SHARE_MIN_S = 3600        # ... and the machine used >= 1 core-hour in the period (a quiet box has no "share")
HOG_ONE_CORE_PCT = 80         # cpu hog: >= 80% of one core in an hour ...
HOG_ONE_CORE_HOURS = 6        # ... for >= 6 hours of the period, or
HOG_WIDE_FRAC = 0.5           # >= 50% of all cores in an hour ...
HOG_WIDE_HOURS = 2            # ... for >= 2 hours (only with >= 2 cores: with one core it is the rule above)
HOG_WIDE_MIN_CORES = 2
LEAK_MIN_DAYS = 3             # memory leak: >= 3 consecutive days with data ...
LEAK_MIN_HOURS = 6            # ... a day counts when the app was sampled in >= 6 hours of it
LEAK_MIN_SLOPE_MB = 50        # slope >= max(50 MB/day, 10%/day of the mean size) ...
LEAK_MIN_SLOPE_FRAC = 0.10
LEAK_MIN_R2 = 0.7             # ... r^2 of the linear fit >= 0.7
LEAK_MAX_DROP = 0.05          # "monotonic-ish": a day-to-day drop of more than 5% starts a new series (a restart or a GC)
MEM_PRESSURE_PCT = 95         # memory pressure: host mem_max >= 95% or swap_max >= 50% in >= 3 hours
SWAP_PRESSURE_PCT = 50
PRESSURE_HOURS = 3
CRASH_WARN = 3                # crash/hang loop: >= 3 crashes + hangs of one subject in the period (warn), >= 10 (err)
CRASH_ERR = 10
RESTART_LOOP = 5              # restart loop: >= 5 restarts within 24 h (warn)
EXIT_ERROR_REPEAT = 3         # ... or the same subject exiting with an error >= 3 times in the period
THERMAL_UNKNOWN_HIGH_C = 90   # a hot hour: temp_max >= temp_high of that hour (>= 90 C when the sensor gives none)
THERMAL_WARN_H_PER_DAY = 2    # warn when the hot hours average >= 2 h/day (info when there is any)
DISK_FIT_DAYS = 30            # disk forecast: linear fit over the last 30 days of disk_day ...
DISK_MIN_POINTS = 7           # ... needs >= 7 days of samples spanning >= 6 days
DISK_MIN_R2 = 0.5             # ... and a trend (r^2 >= 0.5): a noisy series gives no date, not a wrong one
DISK_WARN_DAYS = 30           # days_to_full < 30 warn, < 7 err
DISK_ERR_DAYS = 7
DISK_FULL_PCT = 90            # already >= 90% used: warn whatever the trend
DISK_STALE_DAYS = 3           # the last sample is older than this: no forecast, a note
LOG_NOISY_PER_DAY = 2000      # one template logged >= 2000 times a day (info)
LOG_NEW_WARN = 10             # >= 10 templates first seen in the last 24 h (warn, else info)
LOGIN_SPIKE_MIN = 20          # login failures: a day with >= 20 and >= 5x the median day (info) ...
LOGIN_SPIKE_FACTOR = 5
LOGIN_WARN_MIN = 100          # ... warn at >= 100 in a day
BOOT_FACTOR = 1.5             # boot regression: the last boot >= 1.5x the median of the previous ones (info) ...
BOOT_MIN_PREV = 3             # ... which needs >= 3 previous boots
MIN_COVERAGE_H = 24           # less data than this: "collecting", no conclusion from rules that need days
# ---- output sizes ---------------------------------------------------------------------------------------------------------
TOP_N = 10
MAX_PER_RULE = 10             # findings per rule; the rest is a note
SUBJECT_MAX = 64
TEMPLATE_MAX = 160
LEVELS = {"err": 0, "warn": 1, "info": 2}
MB = 1024 * 1024
OSES = ("linux", "darwin", "windows")
HOST_KINDS = ("unexpected_shutdown", "hw_error", "throttle", "disk_low")  # events without a subject are about the machine

TABLES = {
    "cpu": ("app_hour",), "mem": ("app_hour",), "pressure": ("host_hour",), "thermal": ("host_hour",),
    "disk": ("disk_day",), "logs": ("log_day",), "boots": ("boots",), "events": ("events",),
}


# ---- the fix texts: per rule and OS, with placeholders (never a name from the database) -------------------------------------
def _fx(linux, darwin=None, windows=None):
    return {"linux": linux, "darwin": darwin or linux, "windows": windows or linux}


FIXES = {
    "cpu-hog": _fx(
        "top (or htop) shows it now; docker stats / systemctl status <unit>; cap it: docker update --cpus 2 <name> or "
        "CPUQuota=200% in a systemd drop-in (systemctl edit <unit>); check its log for a loop",
        "Activity Monitor > CPU (sort by % CPU); check its log; quit or update the app; for a daemon: sudo launchctl print system/<label>",
        "Task Manager > Details (sort by CPU); Get-Process <name>; check its log in Event Viewer; restart or update it"),
    "cpu-share": _fx(
        "expected? then nothing to do. Else: top, docker stats, systemctl status <unit> show what it does",
        "expected? then nothing to do. Else: Activity Monitor > CPU shows what it does",
        "expected? then nothing to do. Else: Task Manager > Details shows what it does"),
    "mem-leak": _fx(
        "ps -eo pid,rss,comm --sort=-rss | head; docker stats --no-stream; stopgap: systemctl restart <unit> / docker restart <name>; "
        "cap it so it cannot starve the rest: docker update --memory 1g --memory-swap 1g <name> or MemoryMax=1G in a drop-in; "
        "then update it or report the leak",
        "Activity Monitor > Memory (sort by Memory); relaunch the app (a daemon: sudo launchctl kickstart -k system/<label>); update it",
        "Task Manager > Details > Memory; Resource Monitor > Memory; Restart-Service <name> as a stopgap; update it or report the leak"),
    "memory-pressure": _fx(
        "free -h; ps -eo pid,rss,comm --sort=-rss | head; docker stats --no-stream; give the biggest a limit "
        "(docker update --memory <size> <name>), stop what is not needed, add RAM or swap",
        "Activity Monitor > Memory (the pressure graph); quit the biggest apps; add RAM",
        "Task Manager > Performance > Memory; Get-Process | Sort WS -Descending | Select -First 10; stop what is not needed; add RAM"),
    "crash-loop": _fx(
        "journalctl -u <unit> -b; coredumpctl list; docker logs --tail 100 <name>; fix the cause (config, version, a missing "
        "dependency); systemctl reset-failed <unit> once fixed",
        "Console > Crash Reports (~/Library/Logs/DiagnosticReports, /Library/Logs/DiagnosticReports): open the newest report of the "
        "app; update or reinstall it",
        "Event Viewer > Windows Logs > Application: Application Error / Application Hang name the faulting module; update or "
        "reinstall it; sfc /scannow if it is a system component"),
    "oom": _fx(
        "journalctl -k | grep -i 'killed process'; free -h; give it a limit it respects (docker update --memory 2g <name> or "
        "MemoryMax= in a drop-in) and raise it if the app really needs more; add RAM or swap; look for a leak (mem-leak)",
        "Console > Crash Reports: JetsamEvent-*.ips lists the memory of every process; quit the biggest apps; add RAM",
        "Event Viewer > Windows Logs > System: Resource-Exhaustion-Detector 2004 names the biggest consumers; add RAM or raise "
        "the page file; restart the app that grows"),
    "restart-loop": _fx(
        "docker ps -a; docker inspect <name> (State.ExitCode, State.OOMKilled); docker logs --tail 100 <name>; systemctl status "
        "<unit>; fix the cause; a restart back-off (RestartSec= in the unit) keeps a crash loop from burning CPU",
        "sudo launchctl print system/<label>; its log is named in the plist (StandardErrorPath); fix the cause; ThrottleInterval "
        "slows a restart loop",
        "Get-Service <name>; Event Viewer > Windows Logs > System (Service Control Manager) says why it stopped; services.msc > "
        "Recovery: restart with a delay"),
    "service-failed": _fx(
        "systemctl status <unit>; journalctl -u <unit> -b; systemctl reset-failed <unit> once handled",
        "sudo launchctl print system/<label>; its log is named in the plist (StandardErrorPath)",
        "Get-Service <name>; Event Viewer > Windows Logs > System (Service Control Manager) says why; Start-Service <name>"),
    "unexpected-shutdown": _fx(
        "journalctl -b -1 -e shows the end of the previous boot; last -x | head; check the power (UPS, cable, supply), the "
        "temperature (sensors) and the RAM (memtest86+)",
        "Console > Log Reports > the .panic reports in /Library/Logs/DiagnosticReports; check the power and the temperature; "
        "Apple Diagnostics (hold D at startup)",
        "Event Viewer > Windows Logs > System: Kernel-Power 41 and the BugCheck 1001 after it; check the power supply, the "
        "temperature and the drivers; analyse C:\\Windows\\Minidump with WinDbg"),
    "hw-error": _fx(
        "journalctl -k -p err | grep -iE 'mce|edac|hardware error|nvme|ata'; smartctl -a /dev/<disk>; memtest86+; replace what it names",
        "Apple Diagnostics (hold D at startup); Console > Crash Reports",
        "Event Viewer > Windows Logs > System: WHEA-Logger names the component (CPU, memory, PCIe, disk); update the firmware and "
        "drivers; run the vendor diagnostics"),
    "thermal": _fx(
        "sensors; clean the dust, check the fans and the airflow, move the box off the heat; reduce the load of the apps listed",
        "Activity Monitor > Energy; check the airflow and the fans; quit what runs hot; pmset -g thermlog",
        "Task Manager > Performance; clean the dust, check the fans and the airflow; power plan: maximum processor state 90%"),
    "throttle": _fx(
        "journalctl -k | grep -i throttl; sensors; fix the cooling first (dust, fan, paste), then the load",
        "pmset -g thermlog; check the airflow and the fans; quit what runs hot",
        "Task Manager > Performance > CPU (speed below base); clean the dust, check the fans; power plan: maximum processor state 90%"),
    "disk-full": _fx(
        "df -h; du -xh --max-depth=1 / 2>/dev/null | sort -rh | head; docker system df (docker image prune, docker builder prune); "
        "journalctl --vacuum-size=500M; apt clean",
        "About This Mac > Storage; du -xhd1 / | sort -rh | head; docker system df; tmutil listlocalsnapshots /",
        "Settings > System > Storage; cleanmgr; docker system df if Docker Desktop is used; move or delete the biggest files"),
    "log-noisy": _fx(
        "journalctl -u <unit> -n 50 -p warning; docker logs --tail 50 <name>; fix what it complains about or lower its log level; "
        "LogRateLimitIntervalSec in journald.conf as a stopgap",
        "Console.app: filter by the process; fix what it complains about",
        "Event Viewer > Windows Logs: filter by the source; fix its cause"),
    "log-new": _fx(
        "journalctl -p warning --since '24 hours ago' | tail -50; new messages after an update or a config change are normal, "
        "otherwise look at the unit that writes them",
        "Console.app: filter by the process, last 24 hours; new messages after an update are normal",
        "Event Viewer > Windows Logs > System / Application: filter Warning and Error, last 24 hours; new messages after an update "
        "are normal"),
    "login-fail": _fx(
        "journalctl -u ssh -u sshd | grep -i fail | tail; fail2ban-client status sshd; sudo ufw limit 22/tcp; PasswordAuthentication "
        "no in sshd_config; do not expose port 22 to the Internet (use a VPN or Tailscale)",
        "System Settings > General > Sharing > Remote Login: turn it off if not needed, else allow only your users; do not "
        "expose it to the Internet",
        "Event Viewer > Windows Logs > Security (4625) shows the source; do not expose RDP/SSH to the Internet; use a VPN or "
        "Tailscale; account lockout policy"),
    "boot-regression": _fx(
        "systemd-analyze blame | head; systemd-analyze critical-chain; disable what is new and slow (systemctl disable <unit>)",
        "System Settings > General > Login Items: remove what is new and slow",
        "Task Manager > Startup apps: disable what is new and slow; Event Viewer > Applications and Services Logs > Microsoft > "
        "Windows > Diagnostics-Performance (event 100)"),
}


# ---- small helpers ----------------------------------------------------------------------------------------------------------
def _clean(s, n=SUBJECT_MAX):
    """A name or template from the database as plain one-line text: no control, format or separator characters, capped."""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ch in " \t\r\n" or cat in ("Zs", "Zl", "Zp"):
            out.append(" ")
        elif cat[0] == "C":
            continue
        else:
            out.append(ch)
    t = " ".join("".join(out).split())
    return t if len(t) <= n else t[:max(n - 1, 0)] + "\u2026"


def _num(x):
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return 0
        return int(x) if x == int(x) and abs(x) < 1e15 else round(x, 2)
    return x


def _facts(d):
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        out[str(k)] = _clean(v, TEMPLATE_MAX) if isinstance(v, str) else _num(v)
    return out


def _when(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
    except (OverflowError, ValueError, OSError, TypeError):
        return "?"


def _size(b):
    return "%.1f GB" % (b / 1073741824.0) if b >= 1073741824 else "%d MB" % round(b / MB)


def _times(n):
    return "1 time" if n == 1 else "%d times" % n


def _hours(n):
    return "1 hour" if n == 1 else "%d hours" % n


def _median(v):
    s = sorted(v)
    n = len(s)
    return None if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0)


def _linfit(xs, ys):
    """Least squares: (slope, r2), or None for fewer than two distinct x. A flat series has r2 0 (no trend)."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / float(n), sum(ys) / float(n)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / sxx, (sxy * sxy / (sxx * syy) if syy else 0.0)


def _norm(name):
    n = (name or "").lower()
    return n[:-8] if n.endswith(".service") else n


class _Ctx(object):
    """One report run: the period, the connection, the notes and the findings, and the SQL results shared by rules."""

    def __init__(self, conn, now, days, cores=None):
        self.conn, self.now, self.days = conn, now, days
        self.t0 = now - days * 86400
        self.h0, self.h1 = int(math.ceil(self.t0 / 3600.0)), int(now // 3600)
        self.notes, self.findings, self.cache = [], [], {}
        self.span = "the last 24 hours" if days == 1 else "the last %d days" % days
        self.tables = set(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall()) if "meta" in self.tables else {}
        self.os = meta.get("os") if meta.get("os") in OSES else "linux"
        self.schema = meta.get("schema_version")
        if cores is None:
            try:
                cores = int(meta.get("cores"))
            except (TypeError, ValueError):
                cores = os.cpu_count() or 1
        self.cores = max(1, int(cores))

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def once(self, key, fn):
        if key not in self.cache:
            self.cache[key] = fn(self)
        return self.cache[key]

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def add(self, rule, subject, level, title, text, facts, fix_key=None):
        sub = _clean(subject) if subject is not None else ""
        fid = "%s:%s" % (rule, sub or "host")
        return {"id": fid, "level": level, "title": _clean(title, 100), "text": _clean(text, 400),
                "fix": FIXES[fix_key or rule][self.os], "facts": _facts(facts), "subject": sub or None}

    @property
    def coverage(self):
        return self.once("coverage", _coverage)

    @property
    def collecting(self):
        return self.coverage[0] < MIN_COVERAGE_H


def _coverage(ctx):
    """(hours with data in the period, first hour of data in the whole history as an epoch or None)."""
    hours, since = set(), []
    for t in ("host_hour", "app_hour"):
        if t in ctx.tables:
            hours.update(r[0] for r in ctx.q("SELECT DISTINCT hour FROM %s WHERE hour >= ? AND hour <= ?" % t, (ctx.h0, ctx.h1)))
            first = ctx.q("SELECT MIN(hour) FROM %s" % t)[0][0]
            if first is not None:
                since.append(first)
    return len(hours), (min(since) * 3600 if since else None)


# ---- shared SQL: per app, events, host hours --------------------------------------------------------------------------------
def _apps(ctx):
    """app -> totals over the period, from one pass over app_hour (grouped per app and day)."""
    one = HOG_ONE_CORE_PCT / 100.0 * 3600
    wide = ctx.cores >= HOG_WIDE_MIN_CORES
    apps = {}
    for app, day, cpu, hi, wd, rmax, rsum, rn in ctx.q(
            "SELECT app, hour / 24, SUM(cpu_s), SUM(cpu_s >= ?), %s, MAX(rss_max), SUM(rss_avg), COUNT(rss_avg) FROM app_hour "
            "WHERE hour >= ? AND hour <= ? GROUP BY app, hour / 24" % ("SUM(cpu_s >= ?)" if wide else "0"),
            (one,) + ((ctx.cores * HOG_WIDE_FRAC * 3600,) if wide else ()) + (ctx.h0, ctx.h1)):
        a = apps.setdefault(app, {"cpu_s": 0.0, "hi": 0, "wide": 0, "rss_max": 0, "rss_sum": 0.0, "rss_n": 0, "days": {}})
        a["cpu_s"] += cpu or 0
        a["hi"] += hi or 0
        a["wide"] += wd or 0
        a["rss_max"] = max(a["rss_max"], rmax or 0)
        a["rss_sum"] += rsum or 0
        a["rss_n"] += rn or 0
        a["days"][day] = (rsum or 0, rn or 0)
    return apps


def _events(ctx):
    """kind -> {subject: [n, last]} over the period, subjects cleaned and merged."""
    out = {}
    for kind, subj, n, last in ctx.q("SELECT kind, subject, SUM(COALESCE(n, 1)), MAX(ts) FROM events WHERE ts >= ? GROUP BY kind, subject",
                                     (ctx.t0,)):
        k = _clean(kind, 32)
        s = _clean(subj) or ("host" if k in HOST_KINDS else "?")
        e = out.setdefault(k, {}).setdefault(s, [0, 0])
        e[0] += n or 0
        e[1] = max(e[1], last or 0)
    return out


def _ev(ctx, *kinds):
    """[(subject, n, last)] of these kinds merged, biggest first."""
    ev, m = ctx.once("events", _events), {}
    for k in kinds:
        for s, (n, last) in ev.get(k, {}).items():
            e = m.setdefault(s, [0, 0])
            e[0] += n
            e[1] = max(e[1], last)
    return sorted(((s, n, last) for s, (n, last) in m.items()), key=lambda r: (-r[1], r[0]))


def _hot_clause(col_high="h.temp_high"):
    return "h.temp_max >= CASE WHEN %s > 0 THEN %s ELSE %d END" % (col_high, col_high, THERMAL_UNKNOWN_HIGH_C)


def _top_apps_in_hours(ctx, where, args, agg):
    """The biggest apps (by `agg`, a SQL aggregate over app_hour a) in the host hours matching `where` (on host_hour h)."""
    return ctx.q("SELECT a.app, %s FROM host_hour h CROSS JOIN app_hour a ON a.hour = h.hour WHERE h.hour >= ? AND h.hour <= ? AND (%s) "
                 "GROUP BY a.app ORDER BY 2 DESC, a.app LIMIT 5" % (agg, where), (ctx.h0, ctx.h1) + tuple(args))


# ---- sections: the data the screen shows ------------------------------------------------------------------------------------
def _peaks(ctx, names):
    """app -> (peak hour as an epoch, peak cpu_s) over the period, for the named apps."""
    out, names = {}, list(names)
    for i in range(0, len(names), 200):
        chunk = names[i:i + 200]
        for app, hour, cpu in ctx.q("SELECT app, hour, cpu_s FROM app_hour WHERE hour >= ? AND hour <= ? AND app IN (%s)"
                                    % ",".join("?" * len(chunk)), (ctx.h0, ctx.h1) + tuple(chunk)):
            if cpu is not None and (app not in out or cpu > out[app][1]):
                out[app] = (hour * 3600, cpu)
    return out


def _cpu_data(ctx):
    """(top_cpu rows, hog list, total cpu_s). Peaks are fetched once for the top apps and the hogs."""
    apps = ctx.once("apps", _apps)
    total = sum(a["cpu_s"] for a in apps.values())
    order = sorted(apps, key=lambda k: (-apps[k]["cpu_s"], k))
    top = [k for k in order[:TOP_N] if apps[k]["cpu_s"] > 0]
    hogs = [k for k in order if apps[k]["hi"] >= HOG_ONE_CORE_HOURS or apps[k]["wide"] >= HOG_WIDE_HOURS]
    hogs.sort(key=lambda k: (-(apps[k]["hi"] + apps[k]["wide"]), -apps[k]["cpu_s"], k))
    if len(hogs) > MAX_PER_RULE:
        ctx.note("cpu-hog: %d more apps not listed" % (len(hogs) - MAX_PER_RULE))
        hogs = hogs[:MAX_PER_RULE]
    peaks = _peaks(ctx, set(top) | set(hogs))
    secs = max(ctx.coverage[0], 1) * 3600.0
    rows = []
    for k in top:
        a = apps[k]
        rows.append({"app": _clean(k) or "?", "cpu_s": round(a["cpu_s"], 1), "share": round(a["cpu_s"] / total, 4) if total else 0.0,
                     "avg_pct": round(a["cpu_s"] / secs * 100, 1), "peak_hour": peaks[k][0] if k in peaks else None})
    return rows, hogs, total, peaks


def _daily_series(a):
    """[(day, mean rss)] of the latest run of consecutive days with enough hours of data."""
    days = sorted(d for d, (s, n) in a["days"].items() if n >= LEAK_MIN_HOURS)
    if not days:
        return []
    run = [days[-1]]
    for d in reversed(days[:-1]):
        if d != run[-1] - 1:
            break
        run.append(d)
    run.reverse()
    return [(d, a["days"][d][0] / a["days"][d][1]) for d in run]


def _leak_series(series):
    """The newest monotonic-ish suffix of a daily series (a drop of more than LEAK_MAX_DROP starts a new one)."""
    i = len(series) - 1
    while i > 0 and series[i][1] >= series[i - 1][1] * (1 - LEAK_MAX_DROP):
        i -= 1
    return series[i:]


def _mem_data(ctx):
    """top_mem rows (with the daily trend where >= 3 days exist) and the raw trend per app."""
    apps = ctx.once("apps", _apps)
    order = sorted((k for k in apps if apps[k]["rss_n"]), key=lambda k: (-apps[k]["rss_sum"] / apps[k]["rss_n"], -apps[k]["rss_max"], k))
    rows = []
    for k in order[:TOP_N]:
        a = apps[k]
        s = _daily_series(a)
        fit = _linfit([d for d, _ in s], [v for _, v in s]) if len(s) >= LEAK_MIN_DAYS else None
        rows.append({"app": _clean(k) or "?", "rss_max": int(a["rss_max"]), "rss_avg": int(a["rss_sum"] / a["rss_n"]),
                     "trend_mb_day": round(fit[0] / MB, 1) if fit else None})
    return rows


def _thermal_data(ctx):
    """{"hours_hot", "max", "apps_when_hot"} plus the figures the rules need."""
    rows = ctx.q("SELECT temp_max, temp_high, throttle FROM host_hour WHERE hour >= ? AND hour <= ?", (ctx.h0, ctx.h1))
    temps = [r[0] for r in rows if r[0] is not None]
    hot = sum(1 for t, high, _ in rows if t is not None and t >= (high if high is not None and high > 0 else THERMAL_UNKNOWN_HIGH_C))
    thr = sum(1 for r in rows if r[2] and r[2] > 0)
    apps = []
    if hot and "app_hour" in ctx.tables:
        allcpu = ctx.q("SELECT SUM(a.cpu_s) FROM host_hour h CROSS JOIN app_hour a ON a.hour = h.hour WHERE h.hour >= ? AND h.hour <= ? AND %s"
                       % _hot_clause(), (ctx.h0, ctx.h1))[0][0] or 0
        for app, cpu in _top_apps_in_hours(ctx, _hot_clause(), (), "SUM(a.cpu_s)"):
            apps.append({"app": _clean(app) or "?", "cpu_s": round(cpu or 0, 1), "share": round((cpu or 0) / allcpu, 4) if allcpu else 0.0})
    return {"hours_hot": hot, "max": round(max(temps), 1) if temps else None, "apps_when_hot": apps, "_throttle_h": thr}


def _disks_data(ctx):
    """[(mount, used_pct, days_to_full|None, fit facts, stale days|None)] for every mount sampled in the last 30 days."""
    today = int(ctx.now // 86400)
    pts = {}
    for mount, day, used, total in ctx.q("SELECT mount, day, used, total FROM disk_day WHERE day >= ? AND day <= ? ORDER BY mount, day",
                                         (today - DISK_FIT_DAYS, today)):
        if used is not None and total:
            pts.setdefault(mount, []).append((day, used, total))
    out = []
    for mount, p in pts.items():
        day, used, total = p[-1]
        pct = used / float(total) * 100
        fit = {"used_gb": round(used / 1073741824.0, 1), "total_gb": round(total / 1073741824.0, 1)}
        left, stale = None, today - day
        if len(p) >= DISK_MIN_POINTS and p[-1][0] - p[0][0] >= DISK_MIN_POINTS - 1 and not ctx.collecting:
            if stale > DISK_STALE_DAYS:
                ctx.note("disk %s: no sample for %d days, no forecast" % (_clean(mount), stale))
            else:
                f = _linfit([d for d, _, _ in p], [u for _, u, _ in p])
                if f and f[0] > 0 and f[1] >= DISK_MIN_R2:
                    left = max(0.0, (total - used) / f[0])
                    fit["growth_gb_day"] = round(f[0] / 1073741824.0, 2)
                    fit["fit_days"] = len(p)
        out.append({"mount": _clean(mount) or "?", "used_pct": round(pct, 1), "days_to_full": round(left, 1) if left is not None else None,
                    "_fit": fit})
    out.sort(key=lambda r: (-r["used_pct"], r["mount"]))
    return out[:20]


def _logs_data(ctx):
    """(rows for the period biggest first, baseline known?) with the NEW flag (first seen in the last 24 h)."""
    cutoff = ctx.now - 86400
    rows = ctx.q("SELECT source, unit, template, SUM(CASE WHEN day >= ? THEN n ELSE 0 END), MIN(first) FROM log_day "
                 "GROUP BY source, unit, template", (int(ctx.t0 // 86400),))
    old = ctx.q("SELECT 1 FROM log_day WHERE first < ? LIMIT 1", (cutoff,))
    baseline = bool(old) and not ctx.collecting
    out = []
    for src, unit, tpl, n, first in rows:
        n = n or 0
        new = bool(baseline and first is not None and first >= cutoff)
        if n > 0 or new:
            out.append({"source": _clean(src, 32), "unit": _clean(unit), "template": _clean(tpl, TEMPLATE_MAX), "n": int(n), "new": new})
    out.sort(key=lambda r: (-r["n"], r["source"], r["unit"], r["template"]))
    return out, baseline


def _boots_data(ctx):
    rows = ctx.q("SELECT boot, total_s FROM boots ORDER BY boot DESC LIMIT 21")
    return list(reversed(rows))


# ---- rules: each returns findings -------------------------------------------------------------------------------------------
def rule_unexpected_shutdown(ctx):
    return [ctx.add("unexpected-shutdown", s, "err", "Unexpected shutdown",
                    "The machine shut down unexpectedly %s in %s (last %s)." % (_times(n), ctx.span, _when(last)),
                    {"n": n, "last": last}) for s, n, last in _ev(ctx, "unexpected_shutdown")]


def rule_hw_error(ctx):
    return [ctx.add("hw-error", s, "err", "Hardware errors reported",
                    "%s reported %d hardware error%s in %s (last %s)." % (s, n, "" if n == 1 else "s", ctx.span, _when(last)),
                    {"n": n, "last": last}) for s, n, last in _ev(ctx, "hw_error")]


def rule_oom(ctx):
    return [ctx.add("oom", s, "err", "Out of memory: %s" % s,
                    "%s was hit by an out-of-memory event %s in %s (last %s)." % (s, _times(n), ctx.span, _when(last)),
                    {"n": n, "last": last}) for s, n, last in _ev(ctx, "oom")]


def rule_crash_loop(ctx):
    ev = ctx.once("events", _events)
    out = []
    for s, n, last in _ev(ctx, "crash", "hang"):
        if n < CRASH_WARN:
            continue
        c, h = ev.get("crash", {}).get(s, [0, 0])[0], ev.get("hang", {}).get(s, [0, 0])[0]
        what = " and ".join(x for x in ("crashed %s" % _times(c) if c else "", "hung %s" % _times(h) if h else "") if x)
        out.append(ctx.add("crash-loop", s, "err" if n >= CRASH_ERR else "warn", "Crashing: %s" % s,
                           "%s %s in %s (last %s)." % (s, what, ctx.span, _when(last)), {"n": n, "crashes": c, "hangs": h, "last": last}))
    return out


def rule_restart_loop(ctx):
    per, rows = {}, ctx.q("SELECT subject, ts, COALESCE(n, 1) FROM events WHERE kind = 'restart' AND ts >= ? ORDER BY subject, ts", (ctx.t0,))
    series = {}
    for subj, ts, n in rows:
        series.setdefault(_clean(subj) or "?", []).append((ts, n or 0))
    for s, ev in series.items():
        ev.sort()
        best, lo, win = 0, 0, 0
        for ts, n in ev:
            win += n
            while ev[lo][0] <= ts - 86400:
                win -= ev[lo][1]
                lo += 1
            best = max(best, win)
        per[s] = (best, sum(n for _, n in ev), ev[-1][0])
    errs = dict((s, (n, last)) for s, n, last in _ev(ctx, "exit_error"))
    out = []
    for s in sorted(set(per) | set(errs)):
        best, total, last = per.get(s, (0, 0, 0))
        ee, elast = errs.get(s, (0, 0))
        if best < RESTART_LOOP and ee < EXIT_ERROR_REPEAT:
            continue
        parts = []
        if best >= RESTART_LOOP:
            parts.append("restarted %s within 24 hours%s" % (_times(best), "" if total == best else " (%d in total)" % total))
        if ee >= EXIT_ERROR_REPEAT:
            parts.append("exited with an error %s" % _times(ee))
        out.append((best + ee, ctx.add("restart-loop", s, "warn", "Restarting: %s" % s,
                                       "%s %s in %s (last %s)." % (s, " and ".join(parts), ctx.span, _when(max(last, elast))),
                                       {"max_restarts_24h": best, "restarts": total, "exit_errors": ee, "last": max(last, elast)})))
    out.sort(key=lambda r: (-r[0], r[1]["id"]))
    return [f for _, f in out]


def rule_service_failed(ctx):
    return [ctx.add("service-failed", s, "warn", "Service failed: %s" % s,
                    "%s failed %s in %s (last %s)." % (s, _times(n), ctx.span, _when(last)), {"n": n, "last": last})
            for s, n, last in _ev(ctx, "service_failed")]


def rule_memory_pressure(ctx):
    where, args = "h.mem_max >= ? OR h.swap_max >= ?", (MEM_PRESSURE_PCT, SWAP_PRESSURE_PCT)
    rows = ctx.q("SELECT mem_max, swap_max FROM host_hour WHERE hour >= ? AND hour <= ? AND (mem_max >= ? OR swap_max >= ?)",
                 (ctx.h0, ctx.h1) + args)
    if len(rows) < PRESSURE_HOURS:
        return []
    apps = _top_apps_in_hours(ctx, where, args, "MAX(a.rss_max)") if "app_hour" in ctx.tables else []
    top = [(_clean(a) or "?", int(r or 0)) for a, r in apps[:3]]
    mem = max(r[0] or 0 for r in rows)
    swap = max(r[1] or 0 for r in rows)
    text = "Memory was above %d%% or swap above %d%% in %s of %s (peak memory %d%%, swap %d%%)" % (
        MEM_PRESSURE_PCT, SWAP_PRESSURE_PCT, _hours(len(rows)), ctx.span, round(mem), round(swap))
    if top:
        text += "; biggest then: " + ", ".join("%s %s" % (a, _size(r)) for a, r in top)
    facts = {"hours": len(rows), "mem_max_pct": mem, "swap_max_pct": swap}
    for i, (a, r) in enumerate(top):
        facts["top%d_app" % (i + 1)], facts["top%d_rss_mb" % (i + 1)] = a, round(r / MB)
    return [ctx.add("memory-pressure", None, "warn", "Memory pressure", text + ".", facts)]


def rule_memory_leak(ctx):
    if ctx.collecting:
        return []
    apps = ctx.once("apps", _apps)
    cand = []
    for k, a in apps.items():
        if a["rss_max"] < 2 * LEAK_MIN_SLOPE_MB * MB:  # it cannot have grown by >= 50 MB/day for 3 days below this
            continue
        s = _leak_series(_daily_series(a))
        if len(s) < LEAK_MIN_DAYS:
            continue
        fit = _linfit([d for d, _ in s], [v for _, v in s])
        mean = sum(v for _, v in s) / len(s)
        if not fit or fit[0] <= 0 or fit[1] < LEAK_MIN_R2 or fit[0] < max(LEAK_MIN_SLOPE_MB * MB, LEAK_MIN_SLOPE_FRAC * mean):
            continue
        cand.append((fit[0], k, s, fit[1]))
    out = []
    if cand:
        restarts = {}
        if "events" in ctx.tables:
            for subj, ts in ctx.q("SELECT subject, ts FROM events WHERE kind IN ('restart', 'crash', 'oom', 'exit_error') AND ts >= ?", (ctx.t0,)):
                restarts.setdefault(_norm(subj), []).append(ts)
        for slope, k, s, r2 in sorted(cand, key=lambda c: (-c[0], c[1])):
            lo, hi = s[0][0] * 86400, (s[-1][0] + 1) * 86400
            if any(lo <= ts < hi for ts in restarts.get(_norm(k), ())):
                continue  # a restart in the window explains the growth (warm-up) better than a leak does
            name = _clean(k) or "?"
            out.append(ctx.add("mem-leak", name, "warn", "Memory keeps growing: %s" % name,
                               "%s grew about %d MB/day over %d days (from %s to %s, r2 %.2f), a leak is possible."
                               % (name, round(slope / MB), len(s), _size(s[0][1]), _size(s[-1][1]), r2),
                               {"slope_mb_day": round(slope / MB, 1), "days": len(s), "start_mb": round(s[0][1] / MB),
                                "now_mb": round(s[-1][1] / MB), "r2": round(r2, 2)}))
    if len(out) > MAX_PER_RULE:
        ctx.note("mem-leak: %d more apps not listed" % (len(out) - MAX_PER_RULE))
    if ctx.days < LEAK_MIN_DAYS:
        ctx.note("memory trends need a period of at least %d days" % LEAK_MIN_DAYS)
    return out[:MAX_PER_RULE]


def rule_cpu_hog(ctx):
    _, hogs, _, peaks = ctx.once("cpu", _cpu_data)
    apps = ctx.once("apps", _apps)
    secs = max(ctx.coverage[0], 1) * 3600.0
    out = []
    for k in hogs:
        a, name = apps[k], _clean(k) or "?"
        avg = a["cpu_s"] / secs * 100
        peak = peaks[k][1] / 36.0 if k in peaks else 0.0
        when = "peak %d%%" % round(peak) + ("" if k not in peaks else ", busiest at %s" % _when(peaks[k][0]))
        if a["hi"] >= HOG_ONE_CORE_HOURS:
            text = "%s used over %d%% of one core for %s in %s (%s)." % (name, HOG_ONE_CORE_PCT, _hours(a["hi"]), ctx.span, when)
        else:
            text = "%s used over half of the %d cores for %s in %s (%s)." % (name, ctx.cores, _hours(a["wide"]), ctx.span, when)
        out.append(ctx.add("cpu-hog", name, "warn", "Keeps the CPU busy: %s" % name, text,
                           {"hours_over_80pct_core": a["hi"], "hours_over_half_cores": a["wide"], "cores": ctx.cores,
                            "avg_pct": round(avg, 1), "peak_pct": round(peak, 1), "cpu_s": round(a["cpu_s"]),
                            "peak_hour": peaks[k][0] if k in peaks else None}))
    return out


def rule_cpu_share(ctx):
    rows, hogs, total, _ = ctx.once("cpu", _cpu_data)
    if not rows or total < CPU_SHARE_MIN_S or rows[0]["share"] < CPU_SHARE_INFO:
        return []
    top = rows[0]
    if any((_clean(k) or "?") == top["app"] for k in hogs):
        return []  # already a cpu-hog finding
    return [ctx.add("cpu-share", top["app"], "info", "Biggest CPU user: %s" % top["app"],
                    "%s used %d%% of all CPU time in %s (average %d%% of one core%s)." % (
                        top["app"], round(top["share"] * 100), ctx.span, round(top["avg_pct"]),
                        "" if top["peak_hour"] is None else ", busiest at %s" % _when(top["peak_hour"])),
                    {"share_pct": round(top["share"] * 100, 1), "avg_pct": top["avg_pct"], "cpu_s": top["cpu_s"],
                     "peak_hour": top["peak_hour"]})]


def rule_thermal(ctx):
    t = ctx.once("thermal", _thermal_data)
    hot = t["hours_hot"]
    if not hot:
        return []
    per_day = hot * 24.0 / max(ctx.coverage[0], MIN_COVERAGE_H)
    names = [a["app"] for a in t["apps_when_hot"][:3]]
    text = "%s at or above the temperature limit in %s (about %.1f h/day, max %d C)" % (_hours(hot), ctx.span, per_day, round(t["max"]))
    if names:
        text += "; busiest then: " + ", ".join(names)
    return [ctx.add("thermal", None, "warn" if per_day >= THERMAL_WARN_H_PER_DAY else "info", "Running hot", text + ".",
                    {"hours_hot": hot, "hours_per_day": round(per_day, 1), "max_c": t["max"], "apps": ", ".join(names)})]


def rule_throttle(ctx):
    t = ctx.once("thermal", _thermal_data)
    ev = _ev(ctx, "throttle")
    n = sum(r[1] for r in ev)
    hours = t["_throttle_h"]
    if not n and not hours:
        return []
    last = max([r[2] for r in ev] or [0])
    text = "The CPU throttled itself in %s of %s" % (_hours(hours), ctx.span) if hours else "The CPU throttled itself in %s" % ctx.span
    if n:
        text += " (%s logged%s)" % (_times(n), ", last %s" % _when(last) if last else "")
    return [ctx.add("throttle", None, "warn", "CPU throttling", text + ".", {"hours": hours, "events": n, "last": last or None})]


def rule_disk_full(ctx):
    out = []
    for d in ctx.once("disks", _disks_data):
        left, pct = d["days_to_full"], d["used_pct"]
        if left is not None and left < DISK_ERR_DAYS:
            level = "err"
        elif (left is not None and left < DISK_WARN_DAYS) or pct >= DISK_FULL_PCT:
            level = "warn"
        else:
            continue
        text = "%s is %d%% full" % (d["mount"], round(pct))
        if left is not None and left < DISK_WARN_DAYS:
            text += ", growing %s GB/day: full in about %d days" % (d["_fit"]["growth_gb_day"], math.ceil(left))
        elif left is not None:
            text += ", full in about %d days" % round(left)
        out.append((left if left is not None else 1e9, ctx.add("disk-full", d["mount"], level, "Disk filling up: %s" % d["mount"], text + ".",
                                                               dict(d["_fit"], used_pct=pct, days_to_full=left))))
    out.sort(key=lambda r: (r[0], r[1]["id"]))
    return [f for _, f in out]


def rule_log_noisy(ctx):
    rows, _ = ctx.once("logs", _logs_data)
    per = max(ctx.coverage[0], MIN_COVERAGE_H) / 24.0
    out = []
    for r in rows[:3]:
        if r["n"] / per >= LOG_NOISY_PER_DAY:
            name = r["unit"] or r["source"] or "?"
            out.append(ctx.add("log-noisy", name, "info", "Noisy log: %s" % name,
                               "%s logged one message %d times in %s (about %d a day): \"%s\"." % (
                                   name, r["n"], ctx.span, round(r["n"] / per), r["template"][:80]),
                               {"n": r["n"], "per_day": round(r["n"] / per), "source": r["source"], "template": r["template"]}))
    return out


def rule_log_new(ctx):
    rows, baseline = ctx.once("logs", _logs_data)
    if ctx.collecting:
        return []
    if not baseline:
        if rows:
            ctx.note("new log messages not evaluated: no log history older than 24 hours")
        return []
    new = [r for r in rows if r["new"]]
    if not new:
        return []
    top = new[0]
    return [ctx.add("log-new", None, "warn" if len(new) >= LOG_NEW_WARN else "info", "New log messages",
                    "%d log message%s appeared for the first time in the last 24 hours, most often from %s (%d times)." % (
                        len(new), "" if len(new) == 1 else "s", top["unit"] or top["source"] or "?", top["n"]),
                    {"new_templates": len(new), "top_unit": top["unit"] or top["source"], "top_n": top["n"], "top_template": top["template"]})]


def rule_login_fail(ctx):
    if ctx.collecting:
        return []
    per, today = {}, int(ctx.now // 86400)
    for subj, day, n in ctx.q("SELECT subject, ts / 86400, SUM(COALESCE(n, 1)) FROM events WHERE kind = 'login_fail' AND ts >= ? "
                              "GROUP BY subject, ts / 86400", (ctx.t0,)):
        d = per.setdefault(_clean(subj) or "?", {})
        d[day] = d.get(day, 0) + (n or 0)
    first = int(max(ctx.t0, ctx.coverage[1] or ctx.t0) // 86400)
    out = []
    for s, d in per.items():
        series = [d.get(x, 0) for x in range(first, today + 1)]
        med = _median(series)
        peak = max(series)
        if peak < max(LOGIN_SPIKE_MIN, LOGIN_SPIKE_FACTOR * med):
            continue
        spiked = sum(1 for v in series if v >= max(LOGIN_SPIKE_MIN, LOGIN_SPIKE_FACTOR * med))
        pday = first + series.index(peak)
        out.append((peak, ctx.add("login-fail", s, "warn" if peak >= LOGIN_WARN_MIN else "info", "Failed logins: %s" % s,
                                  "%d failed logins on %s against a median of %d a day; %d in %s." % (
                                      peak, _when(pday * 86400)[:10], round(med), sum(series), ctx.span),
                                  {"peak_day": peak, "peak_date": _when(pday * 86400)[:10], "median_day": med, "total": sum(series),
                                   "days_over": spiked})))
    out.sort(key=lambda r: (-r[0], r[1]["id"]))
    return [f for _, f in out]


def rule_boot_regression(ctx):
    rows = ctx.once("boots", _boots_data)
    if not rows or not rows[-1][1] or rows[-1][1] <= 0:
        return []  # the last boot has no time (unknown): nothing to compare
    boots = [(b, t) for b, t in rows if t is not None and t > 0]
    if len(boots) < BOOT_MIN_PREV + 1:
        return []
    last, prev = boots[-1][1], [t for _, t in boots[:-1]]
    med = _median(prev)
    if last < BOOT_FACTOR * med:
        return []
    return [ctx.add("boot-regression", None, "info", "Slower boot",
                    "The last boot took %d s, %.1fx the median of the %d boots before (%d s)." % (round(last), last / med, len(prev), round(med)),
                    {"last_s": round(last, 1), "median_s": round(med, 1), "ratio": round(last / med, 2), "boots": len(prev)})]


# (name, tables, rule) in the order findings of the same level appear
RULES = [
    ("unexpected-shutdown", "events", rule_unexpected_shutdown), ("hw-error", "events", rule_hw_error), ("oom", "events", rule_oom),
    ("crash-loop", "events", rule_crash_loop), ("restart-loop", "events", rule_restart_loop),
    ("service-failed", "events", rule_service_failed), ("memory-pressure", "pressure", rule_memory_pressure),
    ("mem-leak", "mem", rule_memory_leak), ("cpu-hog", "cpu", rule_cpu_hog), ("thermal", "thermal", rule_thermal),
    ("throttle", "thermal", rule_throttle), ("disk-full", "disk", rule_disk_full), ("cpu-share", "cpu", rule_cpu_share),
    ("log-noisy", "logs", rule_log_noisy), ("log-new", "logs", rule_log_new), ("login-fail", "events", rule_login_fail),
    ("boot-regression", "boots", rule_boot_regression),
]


def _run(ctx, name, tables, fn, default=None):
    """fn(ctx) unless a table is missing; a failing rule or section is a note, never an exception and never silence."""
    missing = [t for t in TABLES[tables] if t not in ctx.tables]
    if missing:
        ctx.note("history incomplete: no %s table" % ", ".join(missing))
        return default
    try:
        return fn(ctx)
    except Exception:  # fail per rule, like the collector: one broken rule must not blank the others
        ctx.note("%s could not be evaluated" % name)
        return default


def _events_out(ctx):
    out = {}
    for kind in sorted(ctx.once("events", _events)):
        rows = _ev(ctx, kind)[:TOP_N]
        out[kind] = [{"subject": s, "n": n, "last": last} for s, n, last in rows]
    return out


def report(conn, now=None, days=7, cores=None):
    """The findings and tables of the last `days` days (1..400) of the history, ending at `now` (default: the clock).
    `cores` is the number of CPU cores for the "half of all cores" test (default: meta "cores", else this machine's)."""
    now = time.time() if now is None else now
    days = max(1, min(400, int(days)))
    ctx = _Ctx(conn, now, days, cores)
    if ctx.schema is not None and ctx.schema != "1":
        ctx.note("history schema %s is not the one this version reads (1)" % _clean(ctx.schema, 16))
    try:
        hours, since = ctx.coverage
    except Exception:
        hours, since = 0, None
        ctx.note("coverage could not be evaluated")
    if not hours:
        ctx.note("no history yet" if since is None else "no data in this period")
    elif ctx.collecting:
        ctx.note("collecting: %d hours so far; trends need %d hours of data" % (hours, MIN_COVERAGE_H))
    found = []
    for name, tables, fn in RULES:
        found.extend(_run(ctx, name, tables, fn, []))
    seen, findings = set(), []
    for f in found:
        if f["id"] not in seen:
            seen.add(f["id"])
            findings.append(f)
    findings.sort(key=lambda f: LEVELS[f["level"]])  # stable: the rule order above is the order within a level
    cpu = _run(ctx, "top_cpu", "cpu", lambda c: c.once("cpu", _cpu_data), ([], [], 0, {}))
    thermal = _run(ctx, "thermal", "thermal", lambda c: c.once("thermal", _thermal_data),
                   {"hours_hot": 0, "max": None, "apps_when_hot": [], "_throttle_h": 0})
    disks = _run(ctx, "disks", "disk", lambda c: c.once("disks", _disks_data), [])
    logs = _run(ctx, "logs", "logs", lambda c: c.once("logs", _logs_data), ([], False))[0]
    keep, extra = logs[:15], [r for r in logs[15:] if r["new"]][:10]
    boots = _run(ctx, "boots", "boots", lambda c: c.once("boots", _boots_data), [])
    return {"period": {"from": ctx.t0, "to": now, "days": days}, "coverage": {"hours": hours, "since": since},
            "findings": findings, "top_cpu": cpu[0],
            "top_mem": _run(ctx, "top_mem", "mem", _mem_data, []),
            "events": _run(ctx, "events", "events", _events_out, {}),
            "logs": sorted(keep + extra, key=lambda r: (-r["n"], r["source"], r["unit"], r["template"])),
            "disks": [{"mount": d["mount"], "used_pct": d["used_pct"], "days_to_full": d["days_to_full"]} for d in disks],
            "thermal": {"hours_hot": thermal["hours_hot"], "max": thermal["max"], "apps_when_hot": thermal["apps_when_hot"]},
            "boots": [{"boot": b, "total_s": t} for b, t in boots], "notes": ctx.notes}
