"""Golden outputs: every screen of the console and every page of the web view, rendered in a world that is the same on every machine.

`FrozenWorld` is that world: a clock that stands still (UTC, whatever TZ says), the host name `demo-host`, the default configuration
(whatever config.ini the machine has), the commands the advice names pinned to their Linux words, and a host that always reads the same
(CPU cores, RAM, swap, root disk, load, uptime, temperatures, network counters), so that `--demo` renders the same bytes on Linux,
macOS and Windows, in any time zone, on any number of CPUs, under any hash seed. `CASES` lists what is locked; tests/golden/ holds the
files; tests/test_golden.py compares them. tools/bench_render.py times the same renders.

The files are what the renderer writes, raw: the ANSI colours included, and the `ESC[K CR LF` that ends every line of a frame (the
console cases are `render.once()` with `--color`, which is what `render.py --once --demo --color` prints). The web ones are the page
as `web.Server.page()` builds it, with a line break between two tags outside <pre> so that a diff can be read.

Regenerate with `NUC_GOLDEN_UPDATE=1 python3 -m unittest tests.test_golden` and review the diff: it is the change in what the user sees.

This module changes nothing in src/. What the world has to patch because the renderer reads it from the host or from the process:
  time, in every src module that imported it: time(), sleep(), localtime(), gmtime(), strftime(), mktime(), ctime(), asctime()
  sum, abs, math.hypot, in every src module: Python 3.12+ adds floats differently from the older ones, and the C library rounds hypot()
          (the abs of a complex number) differently on Linux, macOS and Windows: the figures would differ in the last digit (see _sum, _abs)
  socket.gethostname, os.cpu_count, nuc_config.PORTABLE
  render: CFG (restored in place, the dicts and lists inside it too), MODE, PAGES, ROTATE_S, REFRESH_S, ACCEPT_CMD, PROBLEMS_CMD, CMD,
          KIOSK_HINT, ACCEPTED_PATH, Sampler, meminfo, loadavg, uptime_s, root_disk, telegram_status, DEMO, DEMO_OS, DEMO_HEALTH
  render caches emptied: _CACHE, _HEALTH, _ADVICE, _AI, _TOPO
Whatever a refactor moves into the Sampler (or anywhere else the host is read) has to be faked here, in FakeSampler or in `_host()`.
"""
import contextlib
import difflib
import io
import json
import math as _math
import os
import re
import socket
import sys
import tempfile
import time as _time
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
GOLDEN_DIR = os.path.join(HERE, "golden")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # before render is imported: its CFG and what is derived from it at import time
# the modules the renderer imports on demand are imported now, so that FrozenWorld finds them (and their `time`) when it freezes the clock
import advisor  # noqa: E402,F401
import collector  # noqa: E402,F401
import cpuinfo  # noqa: E402,F401
import demo  # noqa: E402,F401
import graph  # noqa: E402,F401
import graphjs  # noqa: E402,F401
import graphlayout  # noqa: E402,F401
import health  # noqa: E402,F401
import htmlview  # noqa: E402,F401
import nuc_config  # noqa: E402
import procs  # noqa: E402,F401
import render  # noqa: E402
import web  # noqa: E402

NOW = 1_790_000_000      # 2026-09-21 14:13:20 UTC: the header reads 14:13:20, and its burn-in shift is one column
HOST = "demo-host"
CPUS = 8                 # what os.cpu_count() says
_MISSING = object()

# ---- the host: what the renderer would read from /proc, /sys, statvfs or hostinfo, always the same ---------------------------------------
GiB = 2 ** 30
CORES = [0.12, 0.34, 0.08, 0.91, 0.27, 0.05, 0.62, 0.18, 0.44, 0.03, 0.71, 0.15]  # busy fraction of 12 logical CPUs (one of them red)
THERMAL = {"cpu": (62.0, 100.0), "nvme": (44.0, 84.0), "throttle": 3, "throttle_s": 12.5, "clk": (3.1, 4.7), "recent": 0}


def _series(base, n=30):
    """30 deterministic bytes/s samples around base, for the traffic sparklines."""
    return [base * (0.4 + ((i * 7 + 3) % 11) / 10.0) for i in range(n)]


class FakeSampler(object):
    """render.Sampler with nothing to read: the same cores, temperatures and interface on every call, on every machine."""

    def __init__(self):
        pass

    def sample(self):
        rx, tx = 1_200_000.0, 340_000.0
        net = {"eth0": {"rx": rx, "tx": tx, "rx_tot": 81 * GiB, "tx_tot": 19 * GiB, "hist_rx": _series(rx), "hist_tx": _series(tx)}}
        return {"cpu": {f"cpu{i}": v for i, v in enumerate(CORES)},
                "thermal": dict(THERMAL) if render.on("thermal") else {},
                "net": net if render.on("network_traffic") else {},
                "sessions": {"local": [{"user": "alice", "tty": "tty1"}], "ssh": []} if render.on("sessions") else None,
                "fs": [{"mount": "/", "used": 180 * GiB, "total": 480 * GiB}] if render.on("disks") else None}


def _host():
    """The functions render.py reads the machine through, replaced by ones that answer the same everywhere (name -> function)."""
    mem = {"MemTotal": 16 * GiB, "MemAvailable": 9 * GiB, "MemFree": 4 * GiB, "Cached": 5 * GiB, "Buffers": GiB // 4,
           "SwapTotal": 2 * GiB, "SwapFree": 2 * GiB - 300 * 2 ** 20}
    return {"meminfo": lambda: dict(mem), "loadavg": lambda: ["0.42", "0.38", "0.35"], "uptime_s": lambda: 273_120.0,
            "root_disk": lambda: (180 * GiB, 480 * GiB, "/"),
            "telegram_status": lambda path=None: None}  # notify.py's status.json: there is none (read from the host otherwise)


# ---- the clock -----------------------------------------------------------------------------------------------------------------------

class Clock(object):
    """Stands for the `time` module in the code under test: the instant stands still (sleep() moves it by what was asked, nobody waits),
    and local time is UTC, so that no TZ, DST or locale is in the output. Everything else (monotonic, perf_counter...) is the real one."""
    timezone, altzone, daylight, tzname = 0, 0, 0, ("UTC", "UTC")

    def __init__(self, now=NOW):
        self.now = float(now)

    def time(self):
        return self.now

    def sleep(self, secs):
        self.now += max(0.0, float(secs))

    def gmtime(self, secs=None):
        return _time.gmtime(self.now if secs is None else secs)

    localtime = gmtime

    def strftime(self, fmt, t=None):
        return _time.strftime(fmt, self.gmtime() if t is None else t)

    def ctime(self, secs=None):
        return _time.asctime(self.gmtime(secs))

    def asctime(self, t=None):
        return _time.asctime(self.gmtime() if t is None else t)

    def mktime(self, t):
        import calendar
        return float(calendar.timegm(t))

    def __getattr__(self, name):
        return getattr(_time, name)


def _sum(iterable, start=0):
    """sum() as Python 3.8 to 3.11 add floats: one after the other. From 3.12 the builtin compensates the rounding (Neumaier), which moves the
    last digit of a mean here and there (demo.py's CPU usage: 'user 16.0' or '15.9'): the world adds the old way, on every Python."""
    total = start
    for x in iterable:
        total = total + x
    return total


def _abs(x):
    """abs() of a complex number is the C library's hypot(), which Linux, macOS and Windows do not round alike in the last bit; the graph
    view's layout (graphlayout.py: a few hundred relaxation steps on complex positions) moves a node by 0.1 px or more on that. Here it
    is sqrt(re*re + im*im): exact operations only, the same bits everywhere. (A float's abs is the builtin's, as it is exact.)"""
    if isinstance(x, complex):
        return _math.sqrt(x.real * x.real + x.imag * x.imag)
    return abs(x)


class _Math(object):
    """The `math` module for src/: hypot() (web.py's edge ends) built of exact operations too, as _abs() does it."""

    def hypot(self, *xs):
        return _math.sqrt(_sum([x * x for x in xs], 0.0))

    def __getattr__(self, name):
        return getattr(_math, name)


_MATH = _Math()


def _src_modules():
    """The loaded modules of src/ (the renderer's, the demo's, the web view's ...)."""
    return [m for m in list(sys.modules.values()) if getattr(m, "__file__", None) and os.path.dirname(os.path.abspath(m.__file__)) == SRC]


# ---- the world -----------------------------------------------------------------------------------------------------------------------

class FrozenWorld(object):
    """`with FrozenWorld(cfg={...}, mode="overview", accepted=()) as world:` then world.once(argv) or world.page(query) (or world.server).

    cfg: changes to the default configuration, as render.CFG holds it: {"spacing": 0}, {"features": {"map": False}} (a dict merges into
    the default's). mode: render.MODE, "overview" or "rotate". accepted: (problem id, severity, text) of the problems the user accepted as
    known. Everything is put back on exit: the same objects with the same contents (the tests of this repository change render.CFG in
    place and keep references to what is inside it)."""

    def __init__(self, cfg=None, mode="overview", accepted=(), now=NOW):
        self.cfg, self.mode, self.accepted, self.clock = cfg or {}, mode, accepted, Clock(now)
        self._undo, self._tmp, self._server = [], None, None

    # -- patching, with a way back
    def set(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name, _MISSING)))
        setattr(obj, name, value)

    def scrub(self, d):
        """Empty the dict d (a cache); on exit it has what it had."""
        self._undo.append((d, None, dict(d)))
        d.clear()

    def _freeze_config(self):
        """render.CFG = the defaults (what nuc_config.load gives when there is no config.ini) + self.cfg, in place. Every dict and list
        inside it is saved and put back in place on exit too: whoever holds one of them still holds it."""
        seen, stack = set(), [render.CFG]
        while stack:
            x = stack.pop()
            if id(x) in seen:
                continue
            seen.add(id(x))
            stack.extend(v for v in (x.values() if isinstance(x, dict) else x) if isinstance(v, (dict, list)))
            self._undo.append((x, None, dict(x) if isinstance(x, dict) else list(x)))
        new = nuc_config.load("/nonexistent")
        for k, v in self.cfg.items():
            new[k] = dict(new[k], **v) if isinstance(v, dict) and isinstance(new.get(k), dict) else v
        render.CFG.clear()
        render.CFG.update(new)

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        try:
            for mod in _src_modules():
                if getattr(mod, "time", None) is _time:  # `import time`: the clock stands for it
                    self.set(mod, "time", self.clock)
                if getattr(mod, "math", None) is _math:
                    self.set(mod, "math", _MATH)
                self.set(mod, "sum", _sum)  # these two shadow the builtins in that module
                self.set(mod, "abs", _abs)
            self.set(nuc_config, "PORTABLE", "")  # NUC_CONSOLE_HOME: the portable run words some advice and footers differently
            self.set(socket, "gethostname", lambda: HOST)  # render.demo_defaults() puts the same name on it
            self.set(os, "cpu_count", lambda: CPUS)
            self._freeze_config()
            self.set(render, "MODE", self.mode)
            self.set(render, "PAGES", ("System", "Network & firewall", "Boot"))
            self.set(render, "ROTATE_S", 15)
            self.set(render, "REFRESH_S", 2)
            self.set(render, "ACCEPT_CMD", "sudo nuc-console-accept")  # the words of Linux, installed (not the portable run)
            self.set(render, "PROBLEMS_CMD", "nuc-console-problems")
            self.set(render, "CMD", {"restart": "sudo systemctl restart nuc-console-collector", "logs": "journalctl -u nuc-console-collector"})
            self.set(render, "KIOSK_HINT", "Alt+F4 closes · F11 leaves full screen")
            path = os.path.join(self._tmp.name, "accepted.json")  # not there: nothing accepted, whatever the host has
            if self.accepted:
                with open(path, "w") as f:
                    json.dump({pid: {"reason": "known", "fp": render.fingerprint(sev, text, pid)} for pid, sev, text in self.accepted}, f)
            self.set(render, "ACCEPTED_PATH", path)
            self.set(render, "Sampler", FakeSampler)
            for name, fn in _host().items():
                self.set(render, name, fn)
            for name in ("DEMO", "DEMO_OS", "DEMO_HEALTH"):  # --demo and web.Server set them: put back on exit
                self.set(render, name, getattr(render, name))
            for name in ("_CACHE", "_HEALTH", "_ADVICE", "_AI", "_TOPO"):
                if isinstance(getattr(render, name, None), dict):
                    self.scrub(getattr(render, name))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self._server is not None:
                self._server.server_close()
        finally:
            while self._undo:
                obj, name, old = self._undo.pop()
                if name is None:  # a container: its contents as they were
                    if isinstance(obj, dict):
                        obj.clear()
                        obj.update(old)
                    else:
                        obj[:] = old
                elif old is _MISSING:
                    delattr(obj, name)
                else:
                    setattr(obj, name, old)
            if self._tmp is not None:
                self._tmp.cleanup()
                self._tmp = None
        return False

    # -- what to render
    def once(self, argv):
        """`render.py --once --demo --color ARGV`: what the console prints for one frame (the frame, then a line feed)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = render.once(["render.py", "--once", "--demo", "--color"] + list(argv))
        if code:
            raise RuntimeError(f"render.once({argv}) returned {code}")
        return buf.getvalue()

    @property
    def server(self):
        """A web.Server in demo mode that is never started: its page() builds the pages, there is no request and no thread."""
        if self._server is None:
            cfg = dict(nuc_config.load("/nonexistent")["web"], refresh_seconds=2)
            self._server = web.Server(("127.0.0.1", 0), cfg, "", demo=True)
        return self._server

    def page(self, query):
        """The page web.Server serves for ?QUERY (clamped as the request handler does), as its golden file has it."""
        srv = self.server
        srv.cache.clear()
        page = srv.page(**web.view_params(parse_qs(query)))
        return web_text(str(page), getattr(page, "csp", web.CSP))


# ---- the web files -------------------------------------------------------------------------------------------------------------------

PRE = re.compile(r"(<pre\b.*?</pre>)", re.S)
CSRF = re.compile(r'(<input[^>]*\bname="(?:csrf|_csrf|csrf_token)"[^>]*\bvalue=")[^"]*(")')  # a token that is new in every process


def web_text(html_text, csp):
    """The golden text of a page: the Content-Security-Policy it is served with in a comment on top, the CSRF token (if the page has
    one) masked, and a line break between two tags that touch, outside <pre> (inside, the screen's own lines are the lines), so that a
    page of 100 KB is not one line. The clock is not masked: it is the frozen one."""
    parts = PRE.split(html_text)
    parts[0::2] = [p.replace("><", ">\n<") for p in parts[0::2]]
    text = "".join(parts).replace("><pre", ">\n<pre").replace("</pre><", "</pre>\n<")  # the seams of the <pre> blocks
    text = CSRF.sub(r"\1<csrf>\2", text)
    return f"<!-- Content-Security-Policy: {csp} -->\n{text}\n"


# ---- the cases -----------------------------------------------------------------------------------------------------------------------

class Case(object):
    """One golden file. console: args are the arguments of `render.py --once --demo --color`; web: query is the query string of the page
    (the name starts with 'web-'). cfg: changes to the configuration; mode: render.MODE; accepted: see FrozenWorld."""
    __slots__ = ("name", "args", "query", "cfg", "mode", "accepted")

    def __init__(self, name, args=(), query=None, cfg=None, mode="overview", accepted=()):
        self.name, self.args, self.query, self.cfg, self.mode, self.accepted = name, tuple(args), query, cfg or {}, mode, accepted

    @property
    def filename(self):
        return self.name + (".html" if self.query is not None else ".txt")

    @property
    def path(self):
        return os.path.join(GOLDEN_DIR, self.filename)

    def render(self):
        with FrozenWorld(self.cfg, self.mode, self.accepted) as world:
            return world.page(self.query) if self.query is not None else world.once(self.args)


def _size(w, h):
    return ("--cols", str(w), "--rows", str(h))


SIZES = ((79, 24), (120, 33), (200, 50), (226, 50))
SMALL = ((120, 33), (200, 50))
SELECTED = {  # what a case selects: the key of a MAP row (graph.path_key of its path: shop-api-1 under STACKS) and of a graph node, a finding id, a pid
    "row": "99ae4e80a5", "node": "881afd34cb", "finding": "mem-leak:node", "pid": "1610"}
ROTATION = {"map_in_rotation": True, "cpu_in_rotation": True, "health_in_rotation": True}  # the slides: ..., Map, CPU, Health
ACCEPTED = (("container-exited", 1, "1 container exited with an error"),
            ("docker-bypass", 1, "1 Docker port bypassing ufw (DOCKER-USER empty)"))


def _cases():
    out = []
    add = lambda *a, **kw: out.append(Case(*a, **kw))  # noqa: E731

    # the overview: the four sizes, with and without the air under the section titles. At 79x24 it is two screens (-p2)
    for w, h in SIZES:
        for spacing in (1, 0):
            add(f"overview-{w}x{h}-spacing{spacing}", _size(w, h), cfg={"spacing": spacing})
    for spacing in (1, 0):
        add(f"overview-79x24-spacing{spacing}-p2", ("--slide", "1") + _size(79, 24), cfg={"spacing": spacing})
    # the same machine as the Windows and the macOS collectors see it
    for os_name in ("windows", "darwin"):
        add(f"overview-{os_name}-200x50", ("--demo-os", os_name) + _size(200, 50))
    # what the overview cuts ("... +N more") goes to Details pages: there are some at 79x24 (two) and at 120x33 (one)
    add("details-79x24-1", ("--slide", "2") + _size(79, 24))
    add("details-79x24-2", ("--slide", "3") + _size(79, 24))
    add("details-120x33", ("--slide", "1") + _size(120, 33))
    # problems the user accepted as known (a line under ATTENTION), and the Telegram notifier that is on and silent
    add("overview-accepted-120x33", _size(120, 33), accepted=ACCEPTED)
    add("overview-telegram-200x50", _size(200, 50), cfg={"telegram": {"enabled": True}})

    # [dashboard] mode = rotate: one page after the other (a page that does not fit goes on to a second screen)
    for i, name in enumerate(("system", "network-1", "network-2", "boot")):
        add(f"rotate-120x33-{name}", ("--slide", str(i)) + _size(120, 33), mode="rotate")
    for i, name in enumerate(("system", "network", "boot")):
        add(f"rotate-226x50-{name}", ("--slide", str(i)) + _size(226, 50), mode="rotate")

    # the overview with the Map, the CPU and the Health in the rotation (they are its last three slides)
    for w, h in SMALL:
        for i, name in ((-3, "map"), (-2, "cpu"), (-1, "health")):
            add(f"slide-{name}-{w}x{h}", ("--slide", str(i)) + _size(w, h), cfg=ROTATION)

    # the MAP: opened as far as it fits, a node selected with its details
    for w, h in SMALL:
        add(f"map-{w}x{h}", ("--view", "map", "--expand", "fit", "--select", "shop-api", "--details") + _size(w, h))
    add("map-roots-120x33", ("--view", "map") + _size(120, 33))
    add("map-all-only-200x50", ("--view", "map", "--expand", "all", "--only") + _size(200, 50))
    for os_name in ("windows", "darwin"):
        add(f"map-{os_name}-200x50", ("--view", "map", "--expand", "fit", "--demo-os", os_name) + _size(200, 50))

    # the CPU: sorted by CPU and by memory, a process selected with its details
    for w, h in SMALL:
        add(f"cpu-sort-cpu-{w}x{h}", ("--view", "cpu", "--sort", "cpu", "--select", "node", "--details") + _size(w, h))
        add(f"cpu-sort-mem-{w}x{h}", ("--view", "cpu", "--sort", "mem", "--select", "java", "--details") + _size(w, h))
    add("cpu-120x33", ("--view", "cpu") + _size(120, 33))
    for os_name in ("windows", "darwin"):
        add(f"cpu-{os_name}-200x50", ("--view", "cpu", "--demo-os", os_name) + _size(200, 50))

    # the HEALTH: the three periods, a machine with little history and one with none, a finding selected with its details
    for w, h in SMALL:
        for days in (1, 7, 30):
            add(f"health-{days}d-{w}x{h}", ("--view", "health", "--period", str(days)) + _size(w, h))
        add(f"health-little-{w}x{h}", ("--view", "health", "--demo-health", "little") + _size(w, h))
        add(f"health-none-{w}x{h}", ("--view", "health", "--demo-health", "none") + _size(w, h))
        add(f"health-details-{w}x{h}", ("--view", "health", "--select", SELECTED["finding"], "--details") + _size(w, h))
    for os_name in ("windows", "darwin"):
        add(f"health-{os_name}-200x50", ("--view", "health", "--demo-os", os_name) + _size(200, 50))

    # the AI: the three invented machines, a model selected with its details
    for w, h in SMALL:
        add(f"ai-{w}x{h}", ("--view", "ai") + _size(w, h))
        add(f"ai-details-{w}x{h}", ("--view", "ai", "--select", "qwen3-8b", "--details") + _size(w, h))
    for os_name in ("windows", "darwin"):
        add(f"ai-{os_name}-200x50", ("--view", "ai", "--demo-os", os_name) + _size(200, 50))

    # the web view: the pages web.Server serves in demo mode (?query)
    pages = (("dashboard", ""), ("dashboard-compact", "cols=100"), ("dashboard-full", "full=1"), ("dashboard-rotate", "rotate=1"),
             ("cpu", "view=cpu"), ("cpu-details", f"view=cpu&sort=mem&sel={SELECTED['pid']}"),
             ("map-tree", "view=map"), ("map-tree-details", f"view=map&all=1&sel={SELECTED['row']}"),
             ("map-graph", "view=map&as=graph"), ("map-graph-details", f"view=map&as=graph&sel={SELECTED['node']}"),
             ("health", "view=health"), ("health-details", f"view=health&sel={SELECTED['finding']}"),
             ("ai", "view=ai"), ("ai-details", "view=ai&sel=qwen3-8b"))
    for name, query in pages:
        add("web-" + name, query=query)
    return out


CASES = _cases()
BY_NAME = {c.name: c for c in CASES}
assert len(BY_NAME) == len(CASES), "two cases with one name"


# ---- reading, writing and comparing the files ----------------------------------------------------------------------------------------

def read(case):
    """The golden file's text, or None when there is none."""
    try:
        with open(case.path, "rb") as f:
            return f.read().decode("utf-8")
    except FileNotFoundError:
        return None


def write(case, text):
    """Writes the file as it is: UTF-8, no line end changed (the lines of a frame end in CR LF on purpose)."""
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    with open(case.path, "wb") as f:
        f.write(text.encode("utf-8"))


def visible(line, limit=220):
    """A line of a golden for a human: the escape sequences and CR spelled out, a very long line cut."""
    line = line.replace("\x1b", "\\e").replace("\r", "\\r")
    return line if len(line) <= limit else line[:limit] + f"... (+{len(line) - limit})"


def diff(expected, actual, name, lines=40):
    """The first `lines` lines of a unified diff of two golden texts, readable (see visible())."""
    a, b = [visible(x) for x in expected.split("\n")], [visible(x) for x in actual.split("\n")]
    out = list(difflib.unified_diff(a, b, "golden/" + name, "actual/" + name, lineterm="", n=1))
    more = len(out) - lines
    return "\n".join(out[:lines] + ([f"... {more} more diff lines"] if more > 0 else []))


def check(names=None):
    """Renders the cases (all, or the named ones) and compares: [(case, expected or None, actual)] of those that differ."""
    wrong = []
    for case in (CASES if names is None else [BY_NAME[n] for n in names]):
        got, want = case.render(), read(case)
        if got != want:
            wrong.append((case, want, got))
    return wrong


def main(argv):
    """python3 tests/golden.py [--update] [NAME ...]: renders the cases and compares them with the files (or rewrites the files)."""
    update = "--update" in argv
    names = [a for a in argv if not a.startswith("--")] or None
    wrong = check(names)
    for case, want, got in wrong:
        if update:
            write(case, got)
        else:
            print(f"DIFFERENT {case.name}" + ("" if want is not None else " (no golden file)"))
            print(diff(want or "", got, case.filename))
    print(f"{len(CASES) if names is None else len(names)} cases, {len(wrong)} " + ("written" if update else "different"))
    return 0 if update or not wrong else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
