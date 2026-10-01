"""nuc-console CPU screen: the processes, like htop (renderer side, unprivileged, every OS).

NAMES ONLY: never the command line, whose arguments may carry passwords or tokens, and the web view can face the network.
Nothing here reads /proc/<pid>/cmdline or environ, `ps -o args|command|comm` (argv, which a process can rewrite) or
KERN_PROCARGS2, nor a Windows command line. The name is what the kernel calls the process: Linux comm, macOS p_comm or the
executable's file name, the Windows image file name.

ProcSampler().sample() returns (CPU contract, section 2):
  {"procs": [{"pid", "ppid", "user", "name", "state", "threads", "nice", "prio", "cpu", "mem", "mem_pct", "time", "start"}],
   "total": {"count", "running", "threads", "unreadable"}, "notes": [...]}
  cpu   = CPU time used since the previous sample() / wall time, in % of one core (100 = one full core)
  time  = CPU seconds (user + system) since the process started; start = epoch seconds
  mem   = resident bytes (RSS; the working set on Windows); mem_pct = mem / physical memory
  state = Linux letters (R running, S sleeping, D uninterruptible, Z zombie, T stopped, I idle kernel thread...)
What cannot be read is None, never 0 or a guess. sample() never raises: a failure gives an empty list and a note.
The first sample (and one after more than MAX_GAP seconds) has no CPU% on Linux and Windows: there is nothing to compare with.

What an unprivileged user can read:
  Linux    /proc/<pid>/stat and status of every process (unless /proc is mounted with hidepid). Kernel threads: no memory.
  macOS    one `ps` per sample. CPU time and memory of other users' processes need root: None, counted as unreadable.
  Windows  Toolhelp snapshot (every process) + OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) per process. Access denied
           (protected or SYSTEM processes, as LOCAL SERVICE): CPU, memory, start and user None, counted as unreadable.
"""
import os
import re
import sys
import time
import unicodedata

import hostinfo
import winapi

try:
    import pwd
except ImportError:  # Windows
    pwd = None

NAME_MAX = 64
MAX_GAP = 30.0  # seconds: an older previous sample would give an average over minutes, not what runs now
FIELDS = ("pid", "ppid", "user", "name", "state", "threads", "nice", "prio", "cpu", "mem", "mem_pct", "time", "start")
NOT_PRINTED = {"Cc", "Cf", "Cs", "Zl", "Zp"}  # control (terminal escapes), format (bidi overrides, zero width), line breaks


def clean_name(s):
    """A process chooses its own name: no control or invisible format characters, 64 characters at most."""
    if not s.isprintable():
        s = "".join(c for c in s if unicodedata.category(c) not in NOT_PRINTED)
    return s.strip()[:NAME_MAX]


def _empty(notes):
    return {"procs": [], "total": {"count": 0, "running": None, "threads": None, "unreadable": 0}, "notes": notes}


def _row(pid, ppid=None, user=None, name="", state=None, threads=None, nice=None, prio=None, mem=None, time_s=None, start=None):
    return {"pid": pid, "ppid": ppid, "user": user, "name": clean_name(name), "state": state, "threads": threads, "nice": nice,
            "prio": prio, "cpu": None, "mem": mem, "mem_pct": None, "time": time_s, "start": start}


# ---- Linux: /proc parsers (pure) ----------------------------------------------------------------------------------------

def parse_stat(data):
    """/proc/<pid>/stat (bytes) -> dict. The name (comm) is between parentheses and may hold spaces, '(' and ')': it ends at
    the LAST ')' (nothing after it can contain one). Fields after it, numbered as in proc(5): 3 state, 4 ppid, 14 utime,
    15 stime (clock ticks), 18 priority, 19 nice, 20 num_threads, 22 starttime (ticks after boot)."""
    lp, rp = data.find(b"("), data.rfind(b")")
    if lp < 0 or rp < lp:
        raise ValueError("unexpected /proc/<pid>/stat")
    f = data[rp + 2:].split()
    return {"pid": int(data[:lp]), "name": data[lp + 1:rp].decode("utf-8", "replace"), "state": f[0].decode("ascii", "replace"),
            "ppid": int(f[1]), "ticks": int(f[11]) + int(f[12]), "prio": int(f[15]), "nice": int(f[16]),
            "threads": int(f[17]), "starttime": int(f[19])}


def _status_field(data, key):
    i = data.find(key)
    if i < 0:
        return None
    j = data.find(b"\n", i + 1)
    return data[i + len(key):j if j >= 0 else len(data)].split()


def parse_status(data):
    """/proc/<pid>/status (bytes) -> {'uid': effective uid, 'threads', 'rss': bytes}; a key is missing when the file has no
    such line: kernel threads and zombies have no VmRSS (no memory of their own). The kernel escapes the Name line, so a
    process name cannot fake the lines that follow it."""
    out = {}
    uid, threads, rss = _status_field(data, b"\nUid:"), _status_field(data, b"\nThreads:"), _status_field(data, b"\nVmRSS:")
    if uid and len(uid) > 1:
        out["uid"] = int(uid[1])  # real, EFFECTIVE, saved, fs: the effective user, as ps, top and htop show it
    if threads:
        out["threads"] = int(threads[0])
    if rss:
        out["rss"] = int(rss[0]) * 1024  # "VmRSS:  1234 kB"
    return out


def parse_btime(data):
    """/proc/stat -> boot time (epoch seconds)."""
    for ln in data.split(b"\n"):
        if ln.startswith(b"btime "):
            return int(ln.split()[1])
    raise ValueError("no btime in /proc/stat")


def parse_memtotal(data):
    """/proc/meminfo -> MemTotal in bytes."""
    for ln in data.split(b"\n"):
        if ln.startswith(b"MemTotal:"):
            return int(ln.split()[1]) * 1024
    raise ValueError("no MemTotal in /proc/meminfo")


def parse_hidepid(text):
    """/proc/self/mounts -> (mode, gid) of the /proc mount: mode None (everything visible), 'noaccess' (other users'
    /proc/<pid> listed but unreadable) or 'invisible' (not listed); gid = the group exempted by gid=, or None."""
    mode, gid = None, None
    for ln in text.splitlines():
        f = ln.split()
        if len(f) < 4 or f[1] != "/proc" or f[2] != "proc":
            continue
        mode, gid = None, None  # the last mount on /proc is the one in use
        for opt in f[3].split(","):
            k, _, v = opt.partition("=")
            if k == "hidepid":
                mode = {"1": "noaccess", "noaccess": "noaccess", "2": "invisible", "invisible": "invisible",
                        "4": "invisible", "ptraceable": "invisible"}.get(v)
            elif k == "gid" and v.isdigit():
                gid = int(v)
    return mode, gid


# ---- macOS: `ps` output (pure) ------------------------------------------------------------------------------------------

# One `ps` per sample, fixed argv, no shell. Each keyword in its own -o with an empty header ("pid="): no header line, and
# no doubt about where a header text ends. The name is `ucomm` (p_comm, the executable's file name as the kernel recorded
# it at exec, at most 16 characters): NOT `comm`, which on macOS is argv[0] and so can hold whatever a process wrote over its
# arguments (process titles like "unicorn master -c ..."). It is the last column: it may contain spaces.
# `user` is avoided (ps may cut long names): the numeric effective uid is looked up in the password database instead.
# `etime` gives the start (`lstart` is a localised date with spaces). `%cpu` is a decaying average: used only when there is
# no previous sample to compare `time` with. Threads: ps has no per-process thread count (None).
PS_KEYWORDS = ("pid", "ppid", "uid", "state", "nice", "pri", "%cpu", "rss", "time", "etime", "ucomm")
PS_ARGV = ("ps", "-ax") + tuple(x for k in PS_KEYWORDS for x in ("-o", k + "="))
MAC_STATES = {"R": "R", "S": "S", "I": "S", "U": "D", "T": "T", "Z": "Z"}  # macOS I = asleep for more than 20 s, U = uninterruptible


DURATION = re.compile(r"^(?:(\d+)-)?((?:\d+:){0,2}\d+(?:\.\d+)?)$")
NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def parse_duration(text):
    """ps times '[[dd-]hh:]mm:ss[.cc]' (TIME: minutes may exceed 59; ',' as decimal point in some locales) -> seconds, or
    None for '-' and anything else."""
    m = DURATION.match(text.strip().replace(",", "."))
    if not m:
        return None
    secs = 0.0
    for x in m.group(2).split(":"):
        secs = secs * 60 + float(x)
    return secs + int(m.group(1) or 0) * 86400


def _num(text, kind=int):
    """A number as ps prints it, or None ('-', anything else)."""
    text = text.replace(",", ".")
    return kind(float(text)) if NUMBER.match(text) and (kind is float or "." not in text) else None


def parse_ps(text):
    """`ps -ax -o pid= ... -o ucomm=` (PS_KEYWORDS) -> [{'pid', 'ppid', 'uid', 'state', 'nice', 'pri', 'pcpu', 'rss',
    'time', 'etime', 'name'}]: numbers or None where ps printed something else ('-'), rss in bytes, times in seconds,
    state as the Linux letter (macOS I = idle sleeper -> S, U = uninterruptible -> D)."""
    out = []
    n = len(PS_KEYWORDS)
    for ln in text.splitlines():
        f = ln.split(None, n - 1)
        if len(f) < n - 1 or not f[0].isdigit():  # a header line, or a line cut short
            continue
        rss = _num(f[7])
        out.append({"pid": int(f[0]), "ppid": _num(f[1]), "uid": _num(f[2]), "state": MAC_STATES.get(f[3][:1]),
                    "nice": _num(f[4]), "pri": _num(f[5]), "pcpu": _num(f[6], float),
                    "rss": rss * 1024 if rss is not None else None, "time": parse_duration(f[8]),
                    "etime": parse_duration(f[9]), "name": f[10].rstrip() if len(f) > 10 else ""})
    return out


def _same_start(a, b):
    """macOS starts come from etime (whole seconds, read at a slightly different moment each time)."""
    return a is not None and b is not None and abs(a - b) <= 2


# ---- the sampler ----------------------------------------------------------------------------------------------------------

class ProcSampler(object):
    """sample() -> the process list (see the module docstring). root and system are for tests: a fake /proc tree on any OS."""

    def __init__(self, root="/", system=None):
        self.system = system or ("windows" if sys.platform == "win32" else "darwin" if sys.platform == "darwin"
                                 else "linux" if sys.platform.startswith("linux") else sys.platform)
        self.proc = os.path.join(root, "proc")
        self.clock, self.now = time.monotonic, time.time
        self.clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.euid = os.geteuid() if hasattr(os, "geteuid") else None
        self.self_pid = os.getpid()
        self.prev = {}         # pid -> (start key, CPU seconds) of the previous sample
        self.prev_t = None     # monotonic time of the previous sample
        self.prev_wall = None  # its wall-clock time: processes started after it are new
        self._users = {}       # uid -> name (Linux, macOS); SID -> name (Windows)
        self._sids = {}        # Windows: pid -> (creation FILETIME, SID)
        self._names = {}       # macOS: pid -> (start, executable file name)
        self._hidepid = False  # Linux: not read yet
        self._memtotal = None  # macOS: hw.memsize, read once

    # -- common --

    def sample(self):
        try:
            return self._sample()
        except Exception as e:  # fail-soft: the screen says why, the renderer keeps running
            return _empty([f"process list unavailable ({type(e).__name__}: {str(e)[:80]})"])

    def _sample(self):
        t, wall = self.clock(), self.now()
        interval = t - self.prev_t if self.prev_t is not None else None
        if interval is not None and not 0 < interval <= MAX_GAP:
            interval = None
        if self.system == "linux":
            rows, count, unreadable, memtotal, tol, notes = self._linux(wall)
        elif self.system == "darwin":
            rows, count, unreadable, memtotal, tol, notes = self._darwin(wall)
        elif self.system == "windows":
            rows, count, unreadable, memtotal, tol, notes = self._windows()
        else:
            return _empty(["no process list on this OS"])
        cur, no_delta = {}, False
        for r in rows:
            key, fallback = r.pop("_key"), r.pop("_pcpu", None)
            if r["mem"] is not None and memtotal:
                r["mem_pct"] = round(r["mem"] * 100.0 / memtotal, 2)
            if r["time"] is None:
                continue
            cur[r["pid"]] = (key, r["time"])
            p = self.prev.get(r["pid"])
            if interval is None:
                cpu, no_delta = fallback, True
            elif p is not None and key is not None and p[0] is not None and abs(key - p[0]) <= tol:
                cpu = max(0.0, (r["time"] - p[1]) * 100.0 / interval)
            elif r["start"] is not None and r["start"] >= self.prev_wall - 1.0:  # started since: it had used nothing then
                cpu = r["time"] * 100.0 / interval
            else:  # missed by the previous sample (unreadable then): nothing to compare with
                cpu = fallback
            r["cpu"] = round(cpu, 1) if cpu is not None else None
        if no_delta and self.system != "darwin":  # macOS: ps's own average stands in
            notes.append("CPU% from the next refresh")
        self.prev, self.prev_t, self.prev_wall = cur, t, wall
        rows.sort(key=lambda r: r["pid"])
        states, threads = [r["state"] for r in rows], [r["threads"] for r in rows]
        partial = count > len(rows)  # Linux: unreadable processes are counted, not listed
        return {"procs": rows,
                "total": {"count": count, "running": None if partial or None in states else states.count("R"),
                          "threads": None if partial or None in threads else sum(threads), "unreadable": unreadable},
                "notes": notes}

    def _user(self, uid):
        """uid -> user name, cached (NSS can be slow); the number itself when the password database has no such user."""
        if uid is None:
            return None
        name = self._users.get(uid)
        if name is None:
            try:
                name = pwd.getpwuid(uid).pw_name if pwd else str(uid)
            except KeyError:
                name = str(uid)
            self._users[uid] = name
        return name

    # -- Linux --

    def _read(self, path):
        """The whole file (procfs gives it in one read when the buffer is large enough). os.open: no Python file object."""
        fd = os.open(path, os.O_RDONLY)
        try:
            data = os.read(fd, 16384)
            if len(data) == 16384:
                chunks = [data]
                while chunks[-1]:
                    chunks.append(os.read(fd, 65536))
                data = b"".join(chunks)
            return data
        finally:
            os.close(fd)

    def _linux(self, wall):
        proc, hz, notes = self.proc, float(self.clk_tck), []
        btime = parse_btime(self._read(os.path.join(proc, "stat")))
        try:
            memtotal = parse_memtotal(self._read(os.path.join(proc, "meminfo")))
        except (OSError, ValueError):
            memtotal = None
            notes.append("total memory unknown: no MEM%")
        if self._hidepid is False:
            try:
                with open(os.path.join(proc, "self", "mounts"), encoding="utf-8", errors="replace") as f:
                    self._hidepid = parse_hidepid(f.read())
            except OSError:
                self._hidepid = (None, None)
        rows, count, unreadable = [], 0, 0
        for d in os.listdir(proc):
            if not d.isdigit():
                continue
            base = os.path.join(proc, d)
            try:
                data = self._read(os.path.join(base, "stat"))
                if not data:  # exiting
                    continue
                st = parse_stat(data)
                sts = parse_status(self._read(os.path.join(base, "status")))
            except (FileNotFoundError, ProcessLookupError):  # gone between the listing and the read: never existed for us
                continue
            except (OSError, ValueError, IndexError):  # permission (hidepid=noaccess) or a format we do not know
                count += 1
                unreadable += 1
                continue
            count += 1
            uid = sts.get("uid")
            rows.append(dict(_row(st["pid"], ppid=st["ppid"], user=self._user(uid), name=st["name"], state=st["state"] or None,
                                  threads=sts.get("threads", st["threads"]), nice=st["nice"], prio=st["prio"],
                                  mem=sts.get("rss"), time_s=round(st["ticks"] / hz, 2),
                                  start=round(btime + st["starttime"] / hz, 2)),
                             _key=st["starttime"]))
        mode, gid = self._hidepid
        if mode and self.euid != 0 and (gid is None or gid not in getattr(os, "getgroups", list)()):
            notes.append("/proc hidepid: other users' processes are " + ("hidden" if mode == "invisible" else "unreadable"))
        if unreadable:
            notes.append(f"{unreadable} processes could not be read")
        return rows, count, unreadable, memtotal, 0, notes

    # -- macOS --

    def _ps(self):
        return hostinfo._run(*PS_ARGV, timeout=5)

    def _exe_names(self, pids):
        """pid -> file name of the executable (proc_pidpath: the kernel's path of the image; what it refuses is left out, the caller uses ucomm)."""
        return {pid: os.path.basename(p) for pid, p in hostinfo.mac_exe_paths(pids).items() if p}

    def _darwin(self, wall):
        notes = []
        if self._memtotal is None:
            try:
                self._memtotal = hostinfo._sysctl_int("hw.memsize")
            except (OSError, AttributeError, ValueError):
                notes.append("total memory unknown: no MEM%")
        ps = [r for r in parse_ps(self._ps()) if not (r["ppid"] == self.self_pid and r["name"] == "ps")]  # not our own ps
        for r in ps:
            r["start"] = round(wall - r["etime"]) if r["etime"] is not None else None
        # p_comm is cut at 16 characters ("Google Chrome He"): the executable's file name is complete. Once per process.
        alive = {r["pid"] for r in ps}
        names = {pid: v for pid, v in self._names.items() if pid in alive}
        todo = [r["pid"] for r in ps if r["pid"] not in names or not _same_start(names[r["pid"]][0], r["start"])]
        if todo:
            try:
                found = self._exe_names(todo)
            except (OSError, AttributeError, ValueError):
                found = {}
            for r in ps:
                if r["pid"] in todo:
                    names[r["pid"]] = (r["start"], found.get(r["pid"]))
        self._names = names
        rows, unreadable = [], 0
        for r in ps:
            # Every process is listed (KERN_PROC_ALL needs no privilege), but CPU time, memory and the run state come from
            # the task info, which xnu gives only for one's own processes unless root (proc_pidinfo PROC_PIDTASKALLINFO is
            # same-user only): ps then prints 0 for rss and time. A live process always has resident memory, so rss 0 means
            # "not readable": None, never 0. Zombies have no memory at all (and ps prints 0:00.00 as their time).
            zombie = r["state"] == "Z"
            readable = bool(r["rss"]) or zombie
            if not readable:
                unreadable += 1
            row = _row(r["pid"], ppid=r["ppid"], user=self._user(r["uid"]), name=names[r["pid"]][1] or r["name"],
                       state=r["state"] if readable or r["state"] in ("Z", "T") else None, nice=r["nice"],
                       prio=r["pri"] if readable else None, mem=r["rss"] if readable and not zombie else None,
                       time_s=r["time"] if readable and not zombie else None, start=r["start"])
            row["_key"], row["_pcpu"] = r["start"], r["pcpu"] if readable and not zombie else None
            rows.append(row)
        if unreadable:
            notes.append(f"{unreadable} processes of other users: CPU and memory need root")
        return rows, len(rows), unreadable, self._memtotal, 2, notes  # start from etime: whole seconds, may move by one

    # -- Windows --

    def _windows(self):
        notes = []
        try:
            memtotal = winapi.memory()["total"]
        except (OSError, AttributeError, KeyError):
            memtotal = None
            notes.append("total memory unknown: no MEM%")
        rows, unreadable, sids = [], 0, {}
        for e in winapi.process_list():
            pid = e["pid"]
            if pid == 0:  # "System Idle Process": not a process, one thread per CPU that counts the idle time
                continue
            row = _row(pid, ppid=e["ppid"], name=e["name"], threads=e["threads"], prio=e["pri"])  # nice, state: not on Windows
            row["_key"] = None
            known = self._sids.get(pid)
            try:
                st = winapi.process_stats(pid, user=known is None)
                if known is not None and known[0] != st["created"]:  # the pid now belongs to another process
                    known, st = None, winapi.process_stats(pid, user=True)
            except OSError as ex:
                if ex.errno == winapi.ERROR_INVALID_PARAMETER:  # exited since the snapshot
                    continue
                unreadable += 1  # access denied: listed with what the snapshot says
                rows.append(row)
                continue
            if st["exited"]:
                continue
            sid = known[1] if known is not None else st["sid"]
            sids[pid] = (st["created"], sid)
            if sid is not None and sid not in self._users:
                self._users[sid] = winapi.account_name(sid)  # once per account: a domain lookup can be slow
            row.update(user=self._users.get(sid), mem=st["rss"], _key=st["created"],
                       time=round(st["cpu"] / 1e7, 2) if st["cpu"] is not None else None,
                       start=round(winapi.filetime_epoch(st["created"]), 2) if st["created"] else None)
            rows.append(row)
        self._sids = sids
        if unreadable:
            notes.append(f"{unreadable} processes could not be opened (access denied): CPU and memory unknown")
        return rows, len(rows), unreadable, memtotal, 0, notes
