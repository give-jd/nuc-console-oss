"""The full screens' view-models (stdlib only, Python 3.8+): what each screen is made of, as the components of ui.py.

A screen of the console (CPU, Health, the Map and the AI) is a function of its data and of the size it is drawn at: it
returns components (ui.py) and, for a list, the numbers the live loop needs (the first row shown, how many are visible). The state a screen
keeps while it is open lives here too (CpuView, HealthView, MapView, AiView and their keys: ui.KEYMAP says what each key does). The console draws the
components with ansi.render, the web with htmlview.html: one model, two renderers.

Nothing here reads a file, the configuration, the host or the clock, and nothing draws: what a screen needs to know of the world comes in as
an argument (a small context object such as CpuCtx, the time now, the advisor's answer) or inside the data. Producers, the live loop and the
page handlers stay in render.py and web.py.

Layout that only the console has (how many lines the room is, which block is cut, where a table's rows end) is decided here from the size
and written into the components' console fields (ui.Cap, ui.Split, ui.Cols, the widths of a ui.Col); the web ignores them and draws every
row, because a page scrolls (a Health builder is told None for the size when the web is the reader). The console draws these screens byte
for byte as it did before they were components (tests/test_screen_cpu.py, tests/test_screen_health.py, tests/test_screen_ai.py and tests/golden
keep it so).

Each screen has a block below, marked `# ---- NAME`.
"""
import re
import textwrap
import time

import ansi
import graph
import ui
from ui import (KV, THERMAL_ERR, THERMAL_WARN, Action, Badge, Bar, Cap, Col, Controls, Finding, Grid, Group, Head, Kpi, Line, Meter, More, Msg, Only, Pane, Question, Row, Series, Span,
                Spec, Split, Table, Tiles, Wrap, dd, dget, fmt_ago, fmt_cputime, fmt_dur, fmt_k, fmt_min, fmt_size, hclean, hcount, hnum, human, idict,
                num, plural, qf, safe)


# ---- CPU ------------------------------------------------------------------------------------------------------------------------
# Data: {cpu, procs, extra, at} as render.cpu_data reads it (the contract of cpuinfo.CpuSampler and procs.ProcSampler is in
# docs/DESIGN.md). Everything a producer hands over is data: numbers go through num(), text through safe(), a missing value is '?'.

CPU_SORTS = ("cpu", "mem", "time", "pid", "user")
CPU_SORT_KEYS = {"p": "cpu", "m": "mem", "t": "time", "n": "pid", "u": "user"}  # htop's letters
CPU_SORT_NAME = {"cpu": "CPU%", "mem": "memory", "time": "CPU time", "pid": "PID", "user": "user"}
CPU_SORT_SHORT = {"cpu": "cpu", "mem": "mem", "time": "time", "pid": "pid", "user": "user"}  # what the footer calls them
CPU_PANE_W = 140  # from this width up the process details sit beside the table, below it otherwise
CPU_STATES = {"R": "running", "S": "sleeping", "D": "uninterruptible (disk) wait", "Z": "zombie: exited, not yet collected",
              "T": "stopped", "I": "idle", "U": "uninterruptible wait", "X": "dead"}
CPU_PRESSURE = {"nominal": "ok", "moderate": "warn", "heavy": "err", "trapping": "err", "sleeping": "muted"}  # macOS thermal pressure, as tones
CPU_CELL_MIN_BAR, CPU_CELL_MAX_BAR = 10, 32
CPU_COLS = (("pid", "PID", ">"), ("user", "USER", "<"), ("state", "S", "<"), ("nice", "NI", ">"), ("threads", "THR", ">"), ("cpu", "CPU%", ">"),
            ("mem_pct", "MEM%", ">"), ("mem", "RSS", ">"), ("time", "TIME", ">"))
CPU_COL_W = {"user": 9, "state": 1, "nice": 3, "cpu": 5, "mem_pct": 5, "mem": 6, "time": 8}
CPU_SORT_COLS = {"cpu": ("cpu",), "mem": ("mem_pct", "mem"), "time": ("time",), "pid": ("pid",), "user": ("user",)}
CPU_COL_SORT = {"pid": "pid", "user": "user", "cpu": "cpu", "mem_pct": "mem", "mem": "mem", "time": "time"}  # the sort a column's label asks for
CPU_DROPPABLE = ("state", "nice", "threads")  # a column unknown for every process on this OS (Windows has no state or nice) is left out
CPU_STATE_TONE = {"R": "ok", "D": "warn", "Z": "err", "T": "warn"}
CPU_PRIO = {"state": 3, "nice": 3, "threads": 3, "time": 3, "user": 2, "mem": 2, "mem_pct": 1}  # the web drops a column in a narrow panel: the biggest first
WEB_W = 200  # the width the web's model is laid out for: nothing is dropped or cut at it (the page has its own, CSS)


class CpuCtx(object):
    """What the CPU screen needs of the world besides its data: os (whose machine: linux, windows or darwin), topo (a function
    (logical CPU ids) -> {cpu: physical core id} for a Linux that the sampler did not describe, or None), full (nothing is left out:
    the detail pages) and time (the clock module: the pane writes when a process started)."""
    __slots__ = ("os", "topo", "full", "time")

    def __init__(self, os="linux", topo=None, full=False, clock=None):
        self.os, self.topo, self.full, self.time = os, topo, bool(full), clock or time


class CpuLinks(object):
    """The links of the web's CPU screen: sort(name) -> the URL of the page in that order, row(pid) -> the URL that selects (or, for the one
    selected, deselects) a process."""
    __slots__ = ("sort", "row")

    def __init__(self, sort, row):
        self.sort, self.row = sort, row


class CpuView(object):
    """The interactive CPU screen: the sort, the process under the cursor (its pid survives refreshes; its index is where the
    cursor stays when that process vanishes), the scroll position, the details pane, when it was opened and last touched. feed: the
    data source (render.CpuFeed) the live loop reads."""

    def __init__(self, feed=None, now=None):
        self.sort, self.cur, self.idx, self.top, self.details, self.page = "cpu", None, 0, 0, False, 10
        self.feed = feed
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


def sort_keys(sort):
    """The keys of the keymap that sort the rows by `sort`, as a link's data-key writes them ('p P')."""
    letters = [k for k, v in CPU_SORT_KEYS.items() if v == sort]
    return " ".join(x for k in letters for x in (k, k.upper()))


# -- what is known about each logical CPU

def cpu_usage_rows(d):
    rows = dget(d["cpu"], "usage", "cores")
    return sorted((r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and isinstance(r.get("id"), int)
                   and not isinstance(r["id"], bool)), key=lambda r: r["id"])


def cpu_core_of(d, ids, topo=None):
    """{logical CPU: physical core id} among the cores that have a temperature. The sampler's own `core_of` map when it gives one, else
    the topology (topo(ids): sysfs on Linux); without either, the logical CPUs of a core are taken to be next to each other (how Windows
    numbers them) and the cores to be in the order of their sensors, which only holds when every core has one sensor and the same number
    of threads. Otherwise unknown: nothing is invented."""
    cpu = d["cpu"]
    by_core = {k for k, v in idict(dget(cpu, "temps", "cores")).items() if num(v) is not None}
    core_of = idict(cpu.get("core_of")) or (topo(ids) if topo else {})
    if core_of:
        return {i: core_of[i] for i in ids if core_of.get(i) in by_core}
    n = int(num(cpu.get("cores")) or 0)
    if n and len(by_core) == n and len(ids) % n == 0 and ids == sorted(ids):
        per, order = len(ids) // n, sorted(by_core)
        return {i: order[j // per] for j, i in enumerate(ids)}
    return {}


def cpu_core_temps(d, ids, topo=None):
    """{logical CPU: C}: the temperature of the physical core each one runs on."""
    by_core = {k: float(v) for k, v in idict(dget(d["cpu"], "temps", "cores")).items() if num(v) is not None}
    return {i: by_core[k] for i, k in cpu_core_of(d, ids, topo).items()}


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


def cpu_cells(d, ctx):
    """One dict per logical CPU: id, tag, busy/user/system/iowait, mhz, temp. None for what is not known."""
    rows = cpu_usage_rows(d)
    ids = [r["id"] for r in rows]
    tags, mhz, temps = cpu_tags(d, ids), cpu_mhz(d), cpu_core_temps(d, ids, ctx.topo)
    return [{"id": r["id"], "tag": tags.get(r["id"], ""), "busy": num(r.get("busy")), "user": r.get("user"), "system": r.get("system"),
             "iowait": r.get("iowait"), "mhz": mhz.get(r["id"]), "temp": temps.get(r["id"])} for r in rows]


def cpu_limit(d):
    """The package sensor's own limit (crit, else high): what the temperatures are measured against. None = not known."""
    t = dd(d["cpu"].get("temps"))
    return num(t.get("crit")) or num(t.get("high"))


# -- tones: what a figure says about itself

def pct_tone(p, warn=70, err=90):
    p = num(p)
    return None if p is None else "err" if p >= err else "warn" if p >= warn else None


def temp_tone(t, mx):
    t = num(t)
    return None if t is None or not mx else "err" if t >= THERMAL_ERR * mx else "warn" if t >= THERMAL_WARN * mx else None


def cpu_parts(user, system, iowait, busy):
    """The parts of one CPU's meter: user, system and the rest of the busy time, then the I/O wait. Without the user/system split the whole
    busy part is one plain part."""
    user, system, iowait, busy = num(user), num(system), num(iowait), num(busy)
    if busy is None:
        return []
    split = [(user, "user"), (system, "system"), (max(busy - (user or 0) - (system or 0), 0.0), "other")] \
        if user is not None and system is not None else [(busy, "busy")]
    return split + ([(iowait, "iowait")] if iowait else [])


def _join(items, sep):
    """The spans of several Lines (or Spans, or text) as one list, a Span of `sep` between them."""
    out = []
    for i, it in enumerate(items):
        if i:
            out.append(Span(sep))
        out += it.spans if isinstance(it, Line) else [it if isinstance(it, Span) else Span(it)]
    return out


def _lines(nodes, w):
    """The console's lines of components (what the layout counts)."""
    return [x for n in nodes for x in ansi.render(n, w)[0]]


# -- the header

def cpu_head(d, ctx, w, h):
    """The title and the machine in a few lines: what it is, then what it is doing (the console as text, the web as key figures)."""
    cpu, pr = d["cpu"], d["procs"]
    lab = lambda name, value: Line([Span(name + " ", "muted"), Span(value)])  # noqa: E731
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
    return [Head("CPU", model), Wrap(ident, "  ·  ", None if ctx.full else 1 if h < 28 else 2, 1),
            Only("console", [Wrap(act, "  ·  ", None if ctx.full else 1, 1)]), Only("web", [cpu_tiles(d)])]


def cpu_tiles(d):
    """The key figures of the header for the web: the same facts the console writes as its activity line, each with its state."""
    cpu, pr = d["cpu"], d["procs"]
    tot = dget(cpu, "usage", "total")
    busy = num(tot.get("busy")) if isinstance(tot, dict) else None
    rates, load = dd(cpu.get("rates")), cpu.get("load")
    run = rates.get("running") if num(rates.get("running")) is not None else pr["total"].get("running")
    up = num(cpu.get("uptime"))
    fr = dd(cpu.get("freq"))
    now = [x[1] for x in idict(fr.get("cur")).items() if x[0] < 0 and num(x[1]) is not None]
    each = list(cpu_mhz(d).values())  # no whole-machine clock: the mean of the CPUs'
    t = dd(cpu.get("temps"))
    pkg, mx = num(t.get("package")), cpu_limit(d)
    has_load = isinstance(load, (list, tuple)) and len(load) >= 3 and all(num(x) is not None for x in load[:3])
    tiles = [
        Kpi("busy", "busy", qf(busy), "%", "unknown" if busy is None else "err" if busy >= 90 else "warn" if busy >= 70 else "ok",
            hint="every logical CPU together"),
        Kpi("load", "load", qf(load[0], ".2f") if has_load else "?", "", "info" if has_load else "unknown",
            hint=("1 / 5 / 15 min: " + " ".join(qf(x, ".2f") for x in load[:3])) if has_load else "load average: not known here"),
        Kpi("clock", "clock", f"{(num(now[0]) if now else sum(each) / len(each)) / 1000:.2f}" if now or each else "?", " GHz",
            "info" if now or each else "unknown", hint="the whole machine, now" if now else "mean of the CPUs, now" if each else "no clock reading"),
        Kpi("temp", "package", qf(pkg), "°C", "unknown" if pkg is None else temp_tone(pkg, mx) or ("ok" if mx else "info"),
            hint=f"limit {qf(mx)}°C" if mx else "no limit declared"),
        Kpi("up", "up", fmt_dur(up) if up is not None else "?", "", "info" if up is not None else "unknown", hint="since the last boot"),
        Kpi("tasks", "running", qf(run), "", "info" if num(run) is not None else "unknown",
            hint=f"blocked {qf(rates.get('blocked'))} · context switches {fmt_k(rates.get('ctxt'))}/s · interrupts {fmt_k(rates.get('intr'))}/s"),
    ]
    return Tiles(tiles)


# -- every logical CPU

def cpu_grid(d, ctx, w):
    """(top nodes, one Line per logical CPU, cells per row, the console's cell width, number of CPUs): the whole CPU as one meter with its
    split, then one cell per logical CPU in 2-4 columns, htop style: id, P/E, meter (user green, system red, I/O wait grey), busy %, GHz
    and °C where known."""
    tot = dget(d["cpu"], "usage", "total")
    cells = cpu_cells(d, ctx)
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
        items = [Line([Span("user", "ok"), " " + qf(user, ".1f")]), Line([Span("sys", "err"), " " + qf(system, ".1f")]),
                 "nice " + qf(tot.get("nice"), ".1f"), Line([Span("iowait", "muted"), " " + qf(iow, ".1f")]), "irq " + qf(tot.get("irq"), ".1f"),
                 "steal " + qf(tot.get("steal"), ".1f"), "idle " + qf(tot.get("idle"), ".1f")]
        shown = Span(f"{busy:5.1f}%", pct_tone(busy)) if busy is not None else Span("    ?%")
        lead = [" ", Span("ALL", bold=True), " ", Meter(cpu_parts(user, system, iow, busy), bw), " ", shown, "  "]
        room_left = w - ansi.vlen(ansi.inline(Line(lead)))
        widths = [ansi.vlen(ansi.inline(x if isinstance(x, Line) else Span(x))) for x in items]
        while len(items) > 1 and sum(widths) + 2 * (len(items) - 1) > room_left:  # as many as fit, the first ones; at least one
            items.pop()
            widths.pop()
        top.append(Line(lead + _join(items, "  ")))
    if not cells:
        return top + [Msg("warn", "per-CPU usage: ? (the sampler gave none)")], [], 1, cw, 0
    mx = cpu_limit(d)
    out = []
    for x in cells:
        busy = x["busy"]
        spans = [Span(f"{x['id']:>{idw}}")]
        if show_tag:
            spans.append(Span(x["tag"], "accent_strong") if x["tag"] == "P" else Span(x["tag"], "muted") if x["tag"] else Span(" "))
        spans += [" ", Meter(cpu_parts(x["user"], x["system"], x["iowait"], busy), bw), " ",
                  Span(f"{busy:3.0f}%", pct_tone(busy)) if busy is not None else Span("   ?")]
        if show_f:
            spans += [" ", Span(f"{x['mhz'] / 1000:4.2f}G" if x["mhz"] is not None else "    ?")]
        if show_t:
            spans += [" ", Span(f"{x['temp']:3.0f}°C", temp_tone(x["temp"], mx)) if x["temp"] is not None else Span("    ?")]
        out.append(Line(spans))
    return top, out, ncol, cw, len(cells)


def cpu_grid_nodes(grid, n):
    """The grid in at most n console lines: the whole-CPU meter first, then rows of cells; when some are left out the last line counts them."""
    top, cells, ncol, cw, ncells = grid
    rows = -(-len(cells) // ncol)
    cells_node = lambda items: Grid(items, cw, ncol, gap=2, lead=1, fit=True)  # noqa: E731
    if len(top) + rows <= n:
        return top + ([cells_node(cells)] if cells else [])
    keep = max(n - len(top) - 1, 0)
    return [Cap(top + ([cells_node(cells[:keep * ncol])] if keep else []) + [More(max(ncells - keep * ncol, 0), "more CPUs", indent=1)], n, None)]


def _grid_lines(grid, n):
    """How many console lines cpu_grid_nodes(grid, n) takes."""
    top, cells, ncol, _cw, _n = grid
    rows = -(-len(cells) // ncol)
    return len(top) + rows if len(top) + rows <= n else min(n, len(top) + max(n - len(top) - 1, 0) + 1)


# -- the temperatures

def deg(x):
    return "?" if num(x) is None else f"{num(x):.0f}°C"


def cpu_temps(d, ctx, w):
    """The temperatures block, most important line first: package with its limits, hottest core and throttling, then macOS pressure and
    clusters, every sensor, and what is missing. The first node is its title."""
    cpu, extra = d["cpu"], d["extra"]
    t = dd(cpu.get("temps"))
    pkg, high, crit, mx = num(t.get("package")), num(t.get("high")), num(t.get("crit")), cpu_limit(d)
    bw = max(10, min(40, w - 60))
    nodes = [Head("TEMPERATURES", "source " + safe(t.get("source") or "?"))]
    if pkg is None:
        nodes.append(Line([" ", Span("PKG", "muted"), "   ?   ", Span("no package temperature", "muted")]))
    elif mx:
        nodes.append(Line([" ", Span("PKG", "muted"), "   ", Bar(pkg / mx, f"{pkg:.0f}°C/{mx:.0f}°C", THERMAL_WARN, THERMAL_ERR, bw), "   ",
                           Span("high", "muted"), " " + deg(high) + "  ", Span("crit", "muted"), " " + deg(crit)]))
    else:
        nodes.append(Line([" ", Span("PKG", "muted"), f"   {pkg:.0f}°C   ", Span("high ?  crit ?", "muted")]))
    ids = [r["id"] for r in cpu_usage_rows(d)]
    by_core = {k: float(v) for k, v in idict(t.get("cores")).items() if num(v) is not None}
    on_core = cpu_core_of(d, ids, ctx.topo)
    summary = []
    if by_core:
        core, hot = max(by_core.items(), key=lambda kv: (kv[1], -kv[0]))
        cpus = [str(i) for i, k in on_core.items() if k == core]
        summary.append(Line([Span("hottest core ", "muted"), f"{core} ", Span(f"{hot:.0f}°C", temp_tone(hot, mx))]
                            + ([Span(" (cpu " + ",".join(cpus[:4]) + ")", "muted")] if cpus and len(cpus) <= 4 else [])))
    else:
        summary.append(Line([Span("hottest core ", "muted"), "?"]))
    th = dd(cpu.get("throttle"))
    n, secs, cores = num(th.get("package")), num(th.get("package_s")), idict(th.get("cores"))
    per = sorted(((k, num(v)) for k, v in cores.items() if num(v) is not None), key=lambda kv: (-kv[1], kv[0]))
    if n is None and not per:
        summary.append(Line([Span("throttled ", "muted"), "?"]))
    else:
        txt = f"{n:.0f} events" if n is not None else "? events"
        if secs is not None:
            txt += f", {fmt_min(secs)} in all"
        if per:
            txt += "; cores " + ", ".join(f"{k}: {v:.0f}" for k, v in per[:3]) + (f" … +{len(per) - 3}" if len(per) > 3 else "")
        hit = bool(n) or any(v for _, v in per)
        summary.append(Line([Span("throttled ", "muted"), Span(("! " if hit else "✔ ") + txt, "warn" if hit else "ok")]))
    nodes.append(Line([" "] + _join(summary, "   ·   ")))
    if ctx.os == "darwin" or extra["pressure"] or extra["clusters"]:
        pr = extra["pressure"]
        parts = [Line([Span("thermal pressure ", "muted"), Span(safe(pr), CPU_PRESSURE.get(pr.lower(), "warn")) if isinstance(pr, str) else "?"])]
        parts += [safe(cl.get("name") or "?") + " " + qf(cl.get("mhz")) + " MHz " + qf(cl.get("active")) + "% active" for cl in extra["clusters"]]
        nodes.append(Line([" "] + _join(parts, "   ·   ")))
    sensors = [s for s in (t.get("sensors") or []) if isinstance(s, dict) and num(s.get("c")) is not None]
    if sensors:
        items = [Line([safe(s.get("label") or "?") + " ", Span(f"{s['c']:.0f}°C", temp_tone(s["c"], num(s.get("crit")) or mx))]) for s in sensors]
        nodes.append(Wrap(items, "  ·  ", None if ctx.full else 2, 9, lead=Span("sensors", "muted")))
    nodes += [Msg(lv, safe(text)) for lv, text in extra["notes"]]
    return nodes


# -- the processes

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


def _map_scroll(top, i, n, rows, margin=2):
    """First row shown, so that row i stays in sight, a few rows from the edges when there is room."""
    m = min(margin, max(0, (rows - 1) // 2))
    top = max(min(top, i - m), i + m + 1 - rows)
    return max(0, min(top, n - rows))


def cpu_pane(p, rows, now, ctx, two=False, h=40):
    """Everything known about one process in at most h console lines: the contract's fields, the parent's name, '?' for what is unknown.
    two: the fields in two columns (a pane under the table, which has few lines to spare)."""
    ppid = p.get("ppid") if isinstance(p.get("ppid"), int) else None
    parent = next((q for q in rows if q["pid"] == ppid), None) if ppid is not None else None
    st = p.get("state") if isinstance(p.get("state"), str) and p.get("state") else None
    start, mem = num(p.get("start")), num(p.get("mem"))
    clk = ctx.time
    when = "?" if start is None else clk.strftime("%Y-%m-%d %H:%M:%S", clk.localtime(start)) + (f" ({fmt_ago(now - start)} ago)" if now >= start else "")
    items = [("parent", "?" if ppid is None else f"{ppid}  " + (safe(parent.get("name") or "?") if parent else "? (not in the list)")),
             ("user", safe(p.get("user") or "?")), ("state", "?" if st is None else safe(st[:1]) + "  " + CPU_STATES.get(st[:1], "")),
             ("threads", qf(p.get("threads"))), ("nice", qf(p.get("nice"))), ("priority", qf(p.get("prio"))),
             ("CPU", qf(p.get("cpu"), ".1f", " %") + ("  (100 % = one core)" if num(p.get("cpu")) is not None else "")),
             ("memory", qf(p.get("mem_pct"), ".1f", " %") + ("" if mem is None else f"  {fmt_size(mem)} resident")),
             ("CPU time", fmt_cputime(p.get("time"))), ("started", when),
             ("children", str(sum(1 for q in rows if q.get("ppid") == p["pid"])))]
    kv = KV(items, 9, 1, False, 2) if two else KV(items, 10, 1, True, 1)
    return [Cap([Head(f"PROCESS {p['pid']}", safe(p.get("name") or "?")), kv], h, "details")]


def cpu_procs(d, ctx, rows, w, avail, cur, top, sort, links=None):
    """The process table in `avail` console lines (title, column heads, rows). With a cursor it scrolls to keep it in sight; without one
    (the rotation slide, a web page) what does not fit is counted on the last line.
    -> (nodes, first row shown, rows visible, [(line of the nodes, pid)] of the rows drawn)."""
    tot = d["procs"]["total"]
    cnt = int(num(tot.get("count")) or len(rows))
    run, thr, unread = num(tot.get("running")), num(tot.get("threads")), num(tot.get("unreadable"))
    note = f"{cnt} total" + (f" · {run:.0f} running" if run is not None else "") + (f" · {thr:.0f} threads" if thr is not None else "") \
        + (f" · {unread:.0f} unreadable" if unread else "") + f" · by {CPU_SORT_NAME.get(sort, sort)}"
    if rows and all(num(p.get("cpu")) is None for p in rows):
        note += " · CPU% ?: measuring"
    head = Head("PROCESSES", note)
    if not rows:
        return [Cap([head, Msg("warn", "processes: ? (the sampler gave none)")], avail, None)], 0, 0, []
    cols = cpu_columns(rows, w)
    nw = max(4, w - 1 - sum(x[3] + 1 for x in cols))
    marked = CPU_SORT_COLS.get(sort, ())  # the columns the rows are in the order of
    tcols = []
    for key, title, align, width in cols:
        mark = None if key not in marked else "mark" if key == "mem" else "desc" if sort in ("cpu", "mem", "time") else "asc"
        by = CPU_COL_SORT.get(key)
        tcols.append(Col(key, title, "r" if align == ">" else "l", CPU_PRIO.get(key, 0), align == ">", width, 1, None, key in ("state", "cpu"),
                         links.sort(by) if links and by else None, sort_keys(by) if by and key != "mem" else "", mark))
    tcols.append(Col("name", "NAME"))
    vis = max(0, avail - 2)
    i = next((j for j, p in enumerate(rows) if p["pid"] == cur), None) if cur is not None else None
    rest = 0
    if i is None:
        top = 0
        if len(rows) > vis:
            vis = max(0, vis - 1)  # the last line says what is left out
            rest = len(rows) - vis
    else:
        top = _map_scroll(top, i, len(rows), vis) if vis else 0
    shown = rows[top:top + vis]
    trows = []
    for j, p in enumerate(shown):
        cells = []
        for key, _t, _a, width in cols:
            txt = cpu_cell_text(p, key)
            tone = CPU_STATE_TONE.get(txt) if key == "state" else pct_tone(p.get("cpu"), 50, 100) if key == "cpu" else None
            cells.append(Span(txt, tone, full=safe(p.get("user")) if key == "user" and isinstance(p.get("user"), str) and p.get("user") else None))
        name = safe(p.get("name") or "?")
        cells.append(Span(name[:nw], full=name))
        trows.append(Row(cells, "sel" if i is not None and top + j == i else None, str(p["pid"]), links.row(p["pid"]) if links else None))
    table = Table(tcols, trows, head=True, indent=1, fill=True, head_tone="accent_strong", solid=w)
    nodes = [head, table] + ([More(rest, "more processes", indent=1)] if rest else [])
    return [Cap(nodes, avail, None)], top, len(shown), [(2 + j, p["pid"]) for j, p in enumerate(shown)]


class CpuScreen(object):
    """The CPU screen's body for one size: nodes (components, in the order they are drawn), top (the first process shown), rows (the
    processes in order), vis (how many are visible) and pids ([(line of the body, pid)] of the process rows drawn)."""
    __slots__ = ("nodes", "top", "rows", "vis", "pids")

    def __init__(self, nodes, top, rows, vis, pids):
        self.nodes, self.top, self.rows, self.vis, self.pids = nodes, top, rows, vis, pids


def cpu_view(d, ctx, w, h, sort="cpu", cur=None, details=False, top=0, links=None):
    """The CPU screen's body as components for a console w columns wide and at most h lines tall (the web passes WEB_W and a huge h:
    nothing is cut): the header, the CPUs, the temperatures, the processes. cur: the pid under the cursor (None: no cursor, the table is
    cut at the bottom); details: the cursor's process in a pane (beside the table from CPU_PANE_W columns on, below it otherwise).
    links: the CpuLinks of the web page (None: the console)."""
    rows = cpu_rows(d["procs"]["procs"], sort)
    head = cpu_head(d, ctx, w, h)
    n_head = len(_lines(head, w))
    grid = cpu_grid(d, ctx, w)
    n_grid = len(grid[0]) + -(-len(grid[1]) // grid[2])
    temps = cpu_temps(d, ctx, w)
    n_temps = len(_lines(temps, w))
    sel = next((p for p in rows if p["pid"] == cur), None) if details else None
    side = sel is not None and w >= CPU_PANE_W
    tw = w - (max(46, int(w * 0.42)) + 3 if side else 0)
    pane = cpu_pane(sel, rows, d["at"], ctx, two=w >= 70) if sel is not None and not side else []
    n_pane = len(_lines(pane, w))
    free = h - n_head
    p_min = 2 + (5 if cur is not None else 3)
    pane_h = min(n_pane, max(0, free // 2))
    free -= pane_h
    g_len = min(n_grid, max(min(n_grid, 3), free - p_min - min(n_temps, 3)))
    free -= g_len
    t_len = min(n_temps, max(0, free - p_min))
    if t_len < min(n_temps, 3):  # not even the title and two lines: the processes get the room instead of a "… +N more"
        t_len = 0
    free -= t_len
    cut = t_len < n_temps
    t_nodes = [Cap(temps[1:] if cut else temps, t_len)]  # cut: no title, the lines say it
    nodes = [Group(head), Group(cpu_grid_nodes(grid, g_len)), Group(t_nodes)]
    table, top, vis, pids = cpu_procs(d, ctx, rows, tw, max(free, 0), cur, top, sort, links)
    base = n_head + _grid_lines(grid, g_len) + (min(n_temps - 1, t_len) if cut else n_temps)
    if side:  # the pane may be taller than a short table: it has all the room the table could have had
        beside = cpu_pane(sel, rows, d["at"], ctx, h=max(free, 0))
        nodes.append(Split([Group(table)], [Group(beside)], tw))
    else:
        nodes.append(Group(table))
        if pane:
            nodes.append(Group([Cap(pane, pane_h, "details")] if pane_h < n_pane else pane))
    return CpuScreen(nodes, top, rows, vis, [(base + k, pid) for k, pid in pids])




# ---- HEALTH ---------------------------------------------------------------------------------------------------------------------
# Data: health.report() (docs/DESIGN.md): findings (id, level, title, text, facts, fix), top_cpu / top_mem, events, logs, disks, thermal, boots, notes.
# Every text in it came from the machine (an app, a unit, a log template): it goes through hclean() when a component is built, a number
# through hnum(), a value that is missing is drawn '?'.

HEALTH_DAYS = (1, 7, 30)                                    # the periods: keys d/w/m (the web: period=1|7|30)
PERIOD_LABELS = ("24h", "7d", "30d")
PERIOD_KEYS = tuple(k for r in ui.KEYMAP if r.scope == "health" and r.action == "period" for k in r.keys)  # d w m, as the keymap says
HEALTH_KEYS = dict(zip(PERIOD_KEYS, HEALTH_DAYS))
HEALTH_PANE_W = 140      # from this width up the details pane sits beside the findings, below them otherwise
HEALTH_NONE = "no history yet: the collector starts recording when [features] health is on; data appears after the first hour"
LEVEL_PILL = {"err": ("✖ ERR ", "1;41;37"), "warn": ("! WARN", "1;43;30"), "info": ("· INFO", "90")}  # a symbol besides the colour
KIND_ORDER = ("oom", "crash", "hang", "unexpected_shutdown", "hw_error", "service_failed", "restart", "exit_error", "throttle", "disk_low",
              "login_fail")
KIND_LABEL = {"oom": "out of memory", "crash": "crash", "hang": "hang", "unexpected_shutdown": "unexpected off", "hw_error": "hardware",
              "service_failed": "service failed", "restart": "restart", "exit_error": "exit error", "throttle": "throttle",
              "disk_low": "disk low", "login_fail": "login failed"}
KIND_TONE = {"oom": "err", "unexpected_shutdown": "err", "hw_error": "err", "crash": "warn", "hang": "warn", "service_failed": "warn", "restart": "warn",
             "exit_error": "warn"}  # the others are muted
SECTION_NOTES = {"cpu": "share of CPU time · per ", "mem": "RSS · per "}


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


def health_select(fl, hv, text):
    """The cursor on the first finding whose id or title contains text (any case). False: none does."""
    t = text.lower()
    i = next((j for j, f in enumerate(fl) if t in (f["id"] + " " + str(f.get("title", ""))).lower()), None)
    if i is None:
        return False
    hv.idx, hv.cur = i, fl[i]["id"]
    return True


# -- the report's pieces ---------------------------------------------------------------------------------------------------------

def health_findings(R):
    """The findings of the report that can be drawn (a dict with an id), in the report's order (err, warn, info)."""
    return [f for f in (R or {}).get("findings") or [] if isinstance(f, dict) and isinstance(f.get("id"), str)]


def health_nothing(R):
    """Why the list of findings is empty: with less than a day of data no conclusion is not 'all fine'."""
    return ("too little data to conclude anything yet" if hnum((R.get("coverage") or {}).get("hours")) < 24
            else "nothing to report in this period")


def health_level(f):
    return f.get("level") if f.get("level") in LEVEL_PILL else "info"


def hwhen(ts, fmt="%Y-%m-%d %H:%M"):
    """An epoch as UTC (the report's own texts say UTC), '?' when it is not one."""
    try:
        return time.strftime(fmt, time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def hago(ts, now=None):
    sec = (time.time() if now is None else now) - hnum(ts, time.time() if now is None else now)
    return "now" if sec < 90 else f"{sec / 60:.0f} min ago" if sec < 5400 else f"{sec / 3600:.0f} h ago" if sec < 129600 else f"{sec / 86400:.0f} d ago"


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


def hrows(rows, n):
    """(the rows to draw, how many are left out) for a list of room n: one row more than n is drawn rather than a '... +1' line."""
    return (rows, 0) if len(rows) <= n + 1 else (rows[:n - 1], len(rows) - n + 1)


# -- small builders --------------------------------------------------------------------------------------------------------------

def _muted(text):
    return Line([Span(text, "muted")])


def _msg(level, text, w):
    """A message that wraps at the console's width instead of running off the screen (the no-history message is long)."""
    if w is None:
        return [Msg(level, hclean(text))]
    rows = textwrap.wrap(hclean(text), max(10, w - 5)) or [""]
    return [Msg(level, rows[0])] + [Line([Span("     " + x)]) for x in rows[1:]]


def _head(title, note, w):
    """A section's heading; the console drops the note rather than overflowing a narrow column."""
    return Head(title, note if w is None or len(note) + len(title) + 10 <= w else "")


def _joined(items, w, lead=(), sep="  ·  "):
    """One line: the lead, then the items (Span / Line) joined by sep; the console leaves out the last ones that do not fit and counts them
    ('... +N'), keeps one at least, and cuts the line to w. w None: every item."""
    keep, lead = list(items), [Span(x) if isinstance(x, str) else x for x in lead]
    lead_len = sum(len(x.text) for x in lead)
    if w is not None:
        while len(keep) > 1 and lead_len + len(sep) * (len(keep) - 1) + sum(len(getattr(x, "text", "")) for x in keep) + (8 if len(keep) < len(items) else 0) > w:
            keep.pop()
    spans = list(lead)
    for i, it in enumerate(keep):
        spans += ([Span(sep)] if i else []) + [it]
    if len(keep) < len(items):
        spans.append(Span(f"  … +{len(items) - len(keep)}", "muted"))
    return Line(spans, clip=w)


def _more(hidden, what="more"):
    return [More(hidden, what, indent=1)] if hidden else []


def _tone_of(kind):
    return KIND_TONE.get(kind, "muted")


# -- the sections under the findings ---------------------------------------------------------------------------------------------

def _sizes(w):
    """(width of the name column, columns of a series) for a section w columns wide; None: the web, which has its own."""
    return (None, None) if w is None else (max(8, min(18, w // 4)), 14 if w < 70 else 24 if w < 100 else 30)


def hb_cpu(R, w, k, days, now=None):
    rows, n = [x for x in R.get("top_cpu") or [] if isinstance(x, dict)], (10, 5, 4, 3, 2)[k + 1]
    out = [_head("TOP CPU", SECTION_NOTES["cpu"] + ("hour" if days <= 1 else "day"), w)]
    if not rows:
        return out + [_muted(" no CPU data")]
    if k == 3:  # one line: the biggest users
        return out + [_joined([Span(hclean(x.get("app"), 20) + f" {hnum(x.get('share')) * 100:.0f}%") for x in rows], w, " ")]
    nm, sw = _sizes(w)
    top = max(hnum(x.get("share")) for x in rows) or 1.0  # the bar compares the apps, the number is the share of all CPU time
    rows, hidden = (rows, 0) if w is None else hrows(rows, n)
    body = []
    for x in rows:
        sh, ser = hnum(x.get("share")), x.get("series")
        avg = f"{hnum(x.get('avg_pct')):.0f}%"
        body.append(Row([Span(hclean(x.get("app"), nm or 0)), Bar(sh / top, f"{sh * 100:3.0f}%", w=8, tone="accent"), Span("avg " + avg, full=avg),
                         Series(ser, len(ser) if sw is None else min(len(ser), sw)) if isinstance(ser, list) and ser else Span(""),
                         Span("peak " + hwhen(x["peak_hour"], "%a %H:%M"), "muted", full=hwhen(x["peak_hour"], "%a %H:%M")) if x.get("peak_hour") else Span("")]))
    cols = [Col("app", "App", w=nm, gap=2), Col("share", "Share of CPU time", gap=2), Col("avg", "Avg", w=8, gap=2, num=True, wprio=1),
            Col("trend", "Per " + ("hour" if days <= 1 else "day"), gap=2, wprio=3), Col("peak", "Peak", wprio=2)]
    return out + [Table(cols, body, fit=True)] + _more(hidden)


def hb_mem(R, w, k, days, now=None):
    rows, n = [x for x in R.get("top_mem") or [] if isinstance(x, dict)], (10, 5, 4, 3, 2)[k + 1]
    out = [_head("TOP MEMORY", SECTION_NOTES["mem"] + ("hour" if days <= 1 else "day"), w)]
    if not rows:
        return out + [_muted(" no memory data")]
    rising = lambda x: x.get("trend_mb_day") is not None and hnum(x.get("trend_mb_day")) >= 50  # noqa: E731
    if k == 3:  # one line: the biggest, with an arrow on the ones that keep growing
        return out + [_joined([Line([Span(hclean(x.get("app"), 20) + f" {human(hnum(x.get('rss_avg')))}")] + ([Span(" ↗", "warn")] if rising(x) else []))
                               for x in rows], w, " ")]
    nm, sw = _sizes(w)
    trend = any(x.get("trend_mb_day") is not None for x in rows)
    rows, hidden = (rows, 0) if w is None else hrows(rows, n)
    body = []
    for x in rows:
        t, ser = x.get("trend_mb_day"), x.get("series")
        cells = [Span(hclean(x.get("app"), nm or 0)), Span("avg " + human(hnum(x.get("rss_avg"))), full=human(hnum(x.get("rss_avg")))),
                 Span("max " + human(hnum(x.get("rss_max"))), full=human(hnum(x.get("rss_max"))))]
        if trend:
            t = None if t is None else hnum(t)
            cells.append(Span("no trend", "muted") if t is None else Span(f"↗ {t:+.0f}M/day", "warn") if rising(x) else Span(f"↘ {t:+.0f}M/day", "muted")
                         if t <= -50 else Span("→ steady", "muted"))
        cells.append(Series(ser, len(ser) if sw is None else min(len(ser), sw)) if isinstance(ser, list) and ser else Span(""))
        body.append(Row(cells))
    cols = [Col("app", "App", w=nm, gap=2), Col("avg", "Avg", w=9, gap=2, num=True), Col("max", "Max", w=9, gap=2, num=True, wprio=1)]
    cols += [Col("trend", "Trend", w=11, gap=2, wprio=2)] if trend else []
    cols.append(Col("series", "Per " + ("hour" if days <= 1 else "day"), wprio=3))
    return out + [Table(cols, body, fit=True)] + _more(hidden)


def hb_events(R, w, k, days, now=None):
    ev = R.get("events") if isinstance(R.get("events"), dict) else {}
    kinds = [x for x in KIND_ORDER if ev.get(x)] + sorted(x for x in ev if x not in KIND_ORDER and ev[x])
    out = [_head("EVENTS", "by kind · subject ×times, last", w)]
    if not kinds:
        return out + [_muted(" none recorded in this period")]
    rows = {x: [r for r in ev[x] if isinstance(r, dict)] for x in kinds}
    if k == 3:  # one line: the totals
        return out + [_joined([Line([Span(hclean(KIND_LABEL.get(x, x), 14), _tone_of(x)), Span(f" {hcount(sum(hnum(r.get('n')) for r in rows[x]))}")]) for x in kinds],
                              w, " ", "  ")]
    nk, ns = (12, 8, 6, 4)[k + 1], (6, 4, 3, 2)[k + 1]
    shown, hidden = (kinds, 0) if w is None else hrows(kinds, nk)
    body = []
    for x in shown:
        items = [Line([Span(hclean(r.get("subject"), 40) + f" ×{hcount(r.get('n'))} "), Span(hago(r.get("last"), now), "muted")]) for r in rows[x]]
        label = hclean(KIND_LABEL.get(x, x), 14)
        if w is None:
            what = _joined(items, None)
        else:
            more = len(items) > ns + 1
            part = _joined(items[:ns - 1] if more else items[:ns], w - 16)  # the label, 14 columns and its two spaces, are before it
            what = Line([part] + ([Span(f"  … +{len(items) - ns + 1}", "muted")] if more else []))
        body.append(Row([Span(label.ljust(14), _tone_of(x), full=label), what]))
    return out + [Table([Col("kind", "Kind", w=14, gap=1), Col("what", "Subject ×times, last")], body)] + _more(hidden, "more kinds")


def hb_logs(R, w, k, days, now=None):
    rows, n = [x for x in R.get("logs") or [] if isinstance(x, dict)], (10, 6, 4, 3, 2)[k + 1]
    out = [_head("NOISY / NEW LOGS", "messages in the period", w)]
    if not rows:
        return out + [_muted(" none recorded in this period")]
    rows, hidden = (rows, 0) if w is None else hrows(rows, n)
    cut = 0 if w is None else 16
    ww = 0 if w is None else max(8, max(len(hclean(x.get("unit") or x.get("source"), 16)) for x in rows))
    body = []
    for x in rows:
        count, tpl = hcount(x.get("n")), hclean(x.get("template"))
        if w is not None:
            room = w - (1 + max(6, len(count)) + 2 + 3 + 1 + ww + 1)
            shown = tpl if len(tpl) <= room else tpl[:max(room - 1, 0)] + "…"
        else:
            shown = tpl
        body.append(Row([Span(count), Span("NEW", "warn") if x.get("new") else Span(""), Span(hclean(x.get("unit") or x.get("source"), cut)),
                         Span(shown, "muted", full=tpl)]))
    cols = [Col("n", "Messages", align="r", w=6, gap=2, num=True), Col("new", "New", w=3, gap=1), Col("unit", "Unit", w=ww or None, gap=1, wprio=2), Col("msg", "Message")]
    return out + [Table(cols, body)] + _more(hidden)


def hb_disks(R, w, k, days, now=None):
    rows, n = [x for x in R.get("disks") or [] if isinstance(x, dict)], (10, 6, 4, 3, 2)[k + 1]
    out = [_head("DISKS", "used · days to full at the current growth", w)]
    if not rows:
        return out + [_muted(" no disk data")]
    full = lambda x: x.get("days_to_full")  # noqa: E731

    def when(x):
        d = full(x)
        if d is None:
            return Span("no trend", "muted")
        d = hnum(d)
        return Span("full now" if d < 1 / 24.0 else f"full in {d * 24:.0f} h" if d < 1 else f"full in {d:.0f} d" if d < 365 else "full in > 1 y",
                    "err" if d < 7 else "warn" if d < 30 else "muted")
    if k == 3:
        return out + [_joined([Line([Span(hclean(x.get("mount"), 14) + f" {hnum(x.get('used_pct')):.0f}%")]
                                    + ([] if full(x) is None else [Span(" "), when(x)])) for x in rows], w, " ")]
    rows, hidden = (rows, 0) if w is None else hrows(rows, n)
    mw = None if w is None else min(14, max(6, max(len(hclean(x.get("mount"))) for x in rows)))
    bw = 10 if w is None else max(6, min(24, w - mw - 26))
    body = []
    for x in rows:
        pct = hnum(x.get("used_pct"))
        body.append(Row([Span(hclean(x.get("mount"), mw or 0)), Bar(pct / 100.0, "", w=bw), Span(f"{pct:3.0f}%"), when(x)]))
    cols = [Col("mount", "Mount", w=mw, gap=1), Col("used", "Used", gap=1, wprio=3), Col("pct", "", gap=2, num=True), Col("full", "Full in")]
    return out + [Table(cols, body)] + _more(hidden)


def hb_thermal(R, w, k, days, now=None):
    th = R.get("thermal") if isinstance(R.get("thermal"), dict) else {}
    hot, top = hnum(th.get("hours_hot")), th.get("max")
    out = [_head("THERMAL", "hours at or above the temperature limit", w)]
    if top is None:  # no sensor: unknown is not "cool"
        return out + [Line([Span(" "), Span("?", "warn"), Span(" no temperature data in this period", "muted")])]
    apps = [f"{hclean(a.get('app'), 20)} {hnum(a.get('share')) * 100:.0f}%" for a in th.get("apps_when_hot") or [] if isinstance(a, dict)]
    head = Line([Span(f"{hot:.0f} h hot", "err" if hot >= 24 else "warn") if hot else Span("never hot", "muted"), Span(f"  ·  max {hnum(top):.0f} °C", "muted")])
    if k == 3 or not apps:
        return out + [_joined([head] + ([Span("when hot: " + ", ".join(apps[:3]), "muted")] if apps else []), w, " ")]
    return out + [Line([Span(" "), head]), _joined([Span(x) for x in apps], w, [Span(" "), Span("when hot: ", "muted")])]


def hb_boots(R, w, k, days, now=None):
    rows = [x for x in R.get("boots") or [] if isinstance(x, dict)]
    out = [_head("BOOTS", "boot time", w)]
    vals = [hnum(x.get("total_s"), None) if x.get("total_s") is not None else None for x in rows]
    known = sorted(v for v in vals if v)
    if not known:
        return out + [_muted(" no boot times recorded" if not rows else " ? boot times unknown")]
    last = vals[-1]
    med = known[len(known) // 2] if len(known) % 2 else (known[len(known) // 2 - 1] + known[len(known) // 2]) / 2.0
    return out + [Line([Span(" "), Series(vals, min(len(vals), 21)), Span("  "),
                        Span("last " + ("?" if not last else f"{last:.0f} s") + f" · median {med:.0f} s · {plural(len(vals), 'boot')}", "muted")])]


HEALTH_BLOCKS = {"cpu": hb_cpu, "mem": hb_mem, "events": hb_events, "logs": hb_logs, "disks": hb_disks, "thermal": hb_thermal, "boots": hb_boots}
HEALTH_GROUPS = {3: (("cpu", "mem"), ("events", "logs"), ("disks", "thermal", "boots")),
                 2: (("cpu", "mem", "thermal", "boots"), ("events", "logs", "disks")),
                 1: (("cpu", "mem", "events", "logs", "disks", "thermal", "boots"),)}


def health_columns(R, w, k, now=None):
    """(the sections under the findings at level k (-1 everything, 0 the usual, 3 one or two lines each) as one group of components per
    column, the columns' width): 1 column up to 109 wide, 2 up to 189, 3 from 190. w None: the web, three groups in a grid."""
    ncol = 3 if w is None or w >= 190 else 2 if w >= 110 else 1
    cw = None if w is None else (w - 3 * (ncol - 1)) // ncol
    days = int(hnum((R.get("period") or {}).get("days"), 7))
    cols = []
    for names in HEALTH_GROUPS[ncol]:
        col = []
        for name in names:
            try:
                blk = HEALTH_BLOCKS[name](R, cw, k, days, now)
            except Exception as e:  # noqa: BLE001 - one odd row must not blank the other sections
                blk = [Line([Span(f" {name}: could not be shown: " + ui.safe(repr(e))[:(cw or 80) - 40], "warn")])]
            col += ([Line()] if col and k <= 1 else []) + blk
        cols.append(col)
    return cols, cw


def _height(col, cw):
    return len(ansi.render(ui.Group(col), cw)[0])


def health_tables(R, w, h=None, now=None):
    """The sections under the findings as one Cols: the fullest level that fits h lines (None: everything, the web page scrolls)."""
    for k in ((-1,) if h is None else (0, 1, 2, 3)):
        cols, cw = health_columns(R, w, k, now)
        if h is None or max(_height(x, cw) for x in cols) <= h:
            break
    return ui.Cols([(ui.Group(x), cw) for x in cols], gap=3, h=h)


def health_tables_lines(R, w, h=None, now=None):
    """health_tables() as console lines."""
    return ansi.render(health_tables(R, w, h, now), w)[0]


def health_pane(f, w, h):
    """The details of a finding (what it is, its facts, the fix), cut to h lines."""
    level, title, text, facts, fix = health_details(f)
    return Pane(level, title, text, facts, fix, h)


# -- the heading, the findings, the whole screen ----------------------------------------------------------------------------------

def health_counts(fl):
    n = {lv: sum(1 for f in fl if health_level(f) == lv) for lv in LEVEL_PILL}
    bits = [Span(f"{sym} {n[lv]} {word}", tone) for lv, sym, word, tone in (("err", "✖", "err", "err"), ("warn", "!", "warn", "warn"), ("info", "·", "info", "muted")) if n[lv]]
    return Line([x for i, b in enumerate(bits) for x in ([Span("  ")] if i else []) + [b]]) if bits else Span("no findings", "muted")


def health_title(R, days, fl, selector=True, href=None):
    """'-- HEALTH  last 7 days · since 2026-09-24 14:00 UTC · 168 h of data · 1 err ------ d:24h w:7d m:30d': the period, how much history
    the report rests on, the findings per level (the console drops the least needed first when narrow). href: days -> the address of the
    web page for that period."""
    cov = (R or {}).get("coverage") or {}
    hours = hnum(cov.get("hours"))
    parts = [Span("last 24 hours" if days == 1 else f"last {days} days", "muted"),
             Span((f"since {hwhen(cov.get('since'))} UTC · " if cov.get("since") else "") + f"{hours:.0f} h of data", "muted")]
    if R is not None and hours:
        parts.append(health_counts(fl))
    seg = ui.Seg("period", [(lab, key, d == days, href(d) if href else None) for d, key, lab in zip(HEALTH_DAYS, PERIOD_KEYS, PERIOD_LABELS)]) if selector else None
    return ui.Title("HEALTH", parts, seg)


def health_finding(f, cursor=False, open_=False, detail=True):
    """A finding of the report as a component; detail: with its Pane (the web; the console shows the Pane of the selected one apart)."""
    return Finding(f["id"], health_level(f), hclean(f.get("title")), hclean(f.get("text")), health_pane(f, 0, None) if detail else None, cursor, open_)


def _notes(R, h):
    notes = [hclean(x) for x in R.get("notes") or [] if isinstance(x, str)]
    if not (R.get("coverage") or {}).get("since"):  # nothing recorded yet: the message below says it
        notes = [x for x in notes if x != "no history yet"]
    if h is not None:
        nn = 2 if h >= 30 else 1
        notes = notes if len(notes) <= nn else notes[:nn - 1] + [f"… +{len(notes) - nn + 1} more notes"]
    return notes


def _no_data(R):
    return "no data in this period" if (R.get("coverage") or {}).get("since") else HEALTH_NONE


def health_model(data, hv, fl, advice=None, href=None, now=None):
    """The whole Health screen for the web: components, nothing cut. data: render.health_data()'s answer, hv the view (its days and its
    selected finding, which is shown open), fl the findings, advice: ui.Advice or None, href: days -> the address of that period."""
    R = data["report"]
    out = [health_title(R, hv.days, fl, True, href)]
    if R is None:
        return out + _msg("err" if data.get("err") else "info", data["msg"], None)
    out += [Line([Span("· " + x, "muted")]) for x in _notes(R, None)]
    if not hnum((R.get("coverage") or {}).get("hours")):
        return out + _msg("info", _no_data(R), None)
    out.append(ui.Group([Head("FINDINGS")] + ([health_finding(f, False, f["id"] == hv.cur) for f in fl] if fl else _msg("info", health_nothing(R), None))))
    if advice is not None:
        out.append(advice)
    if R.get("coverage"):
        out.append(health_tables(R, None, None, now))
    return out


def health_lines(data, hv, fl, w, h, advice, now=None):
    """The Health screen's body as console lines: at most h, none wider than w. Title, notes, findings (the cursor's row in reverse), the
    advisor's ADVICE block (advice: (report, w) -> ui.Advice with its lines, or None; asked once the screen has something to say), then
    the details pane (below the findings, beside them from HEALTH_PANE_W) or the sections of tables."""
    R = data["report"]
    if R is None:  # no history, or it could not be read
        return ansi.render(ui.Group([health_title(None, hv.days, [])] + _msg("err" if data.get("err") else "info", data["msg"], w)), w)[0]
    out = [health_title(R, hv.days, fl)] + [Line([Span("  \u00b7 " + x, "muted")], clip=w) for x in _notes(R, h)]
    if not hnum((R.get("coverage") or {}).get("hours")):
        return ansi.render(ui.Group(out + _msg("info", _no_data(R), w)), w)[0][:h]
    adv = advice(R, w) if advice else None
    adv = adv if adv is not None and adv.lines else None
    avail = h - len(out) - (len(adv.lines) + 1 if adv else 0)
    nf, pane = len(fl), bool(hv.details and fl)
    side = pane and w >= HEALTH_PANE_W
    cols, cw = health_columns(R, w, 3, now)  # the sections at their shortest
    tmin = max(_height(x, cw) for x in cols)
    nothing = _msg("info", health_nothing(R), w) if not nf else []  # never a green: what is not recorded is not fine
    if not nf:
        area = 1 + len(ansi.render(ui.Group(nothing), w)[0])
    elif side:  # as tall as the details need (at least the list), leaving the sections their shortest form
        health_sync(hv, fl)
        area = max(6, min(avail - tmin, max(1 + nf, len(ansi.render(health_pane(fl[hv.idx], 0, 99), w - int(w * 0.45) - 3)[0]))))
    elif pane:
        area = 1 + max(1, min(nf, avail // 3, 8))
    else:
        area = 1 + max(min(nf, 3), min(nf, avail - 1 - tmin))
    rows = area - 1
    fw = int(w * 0.45) if side else w
    hv.rows = max(1, rows)
    block = [Head("FINDINGS")]
    if not nf:
        block += nothing
    else:
        health_sync(hv, fl)
        hv.top = ansi.scroll(hv.top, hv.idx, nf, rows)
        block += [health_finding(f, j == hv.idx, False, False) for j, f in enumerate(fl) if hv.top <= j < hv.top + rows]
    if side:
        out.append(ui.Split(block, [health_pane(fl[hv.idx], 0, area)], fw, area))
    else:
        out += block
    if adv:
        out.append(adv)
    if pane and not side:
        out.append(health_pane(fl[hv.idx], 0, avail - area))
    else:
        out.append(health_tables(R, w, max(0, avail - area), now))
    return [ansi.clip(x, w) for x in ansi.render(ui.Group(out), w)[0][:h]]


# ---- MAP ------------------------------------------------------------------------------------------------------------------------
# Data: graph.build()'s graph (nodes, edges, notes) and graph.rows()' rows of it (docs/DESIGN.md, src/graph.py). Every name in it is data from the
# machine (a container, a process, a remote address): it goes through ui.safe when a component is built, whatever it holds.

MAP_PANE_W = 140  # from this width up the details pane sits beside the tree, below it otherwise
MAP_IDLE_S = 600  # the Map left alone this long gives the monitor back to the rotation (nobody may be at the keyboard)
MAP_TONE = {"err": "err", "down": "err", "warn": "warn", "unknown": "unknown", "ok": "ok"}  # the tone of a row's name by state (info: none)
MAP_ROOT_TONE = {"err": "err", "down": "err", "warn": "warn"}  # a root's name is bold, and accent when nothing is wrong
MAP_EV_TONE = {"seen": "strong", "declared": None, "possible": "muted", "bind": "muted"}  # how sure the link is: bright, normal, dim
MAP_MARK = {"down": "✖ ", "unknown": "? "}  # a symbol before the name besides its colour (the web draws the symbol of every state itself)


_SP1, _SP2 = Span(" "), Span("  ")  # the spaces between the parts of a row: shared (a component is never changed once built)


class MapView(object):
    """The interactive Map: open branches (graph.State), the selected row (its key survives refreshes; its index is where
    the cursor stays when that row vanishes), the scroll position, the details pane, when it was opened and last touched."""

    def __init__(self, now=None):
        self.st, self.cur, self.idx, self.top, self.details = graph.State(), None, 0, 0, False
        self.opened = self.touched = now or time.time()


class MapLinks(object):
    """The links of the web's Map: row(row) -> the URL that selects (or, for the selected one, deselects) a row, toggle(row) -> the one that opens
    or closes its branch, close (the URL without the selection), expand and collapse (every branch open / only the roots), paths(only) -> the
    view of every path (False) or of the paths that lead to a problem (True), graph (the graph view, or None)."""
    __slots__ = ("row", "toggle", "close", "expand", "collapse", "paths", "graph")

    def __init__(self, row, toggle, close=None, expand=None, collapse=None, paths=None, graph=None):
        self.row, self.toggle, self.close, self.expand, self.collapse, self.paths, self.graph = row, toggle, close, expand, collapse, paths, graph


class MapScreen(object):
    """The Map as components: nodes, and top (the first tree row shown: where the live loop keeps its scroll position)."""
    __slots__ = ("nodes", "top")

    def __init__(self, nodes, top=0):
        self.nodes, self.top = nodes, top


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


def map_layout(G, w, h, details=False):
    """(notes shown, tree rows, details rows, details beside the tree?) of a Map body h lines tall."""
    notes = min(len(G["notes"]), max(0, (h - 4) // 4))
    avail = max(1, h - 1 - notes)
    if not details or avail < 6:
        return notes, avail, 0, False
    if w >= MAP_PANE_W:
        return notes, avail, avail, True
    return notes, avail - avail // 2, avail // 2, False


# -- the pieces ------------------------------------------------------------------------------------------------------------------

def map_legend():
    """What the arrows of the tree mean (graph.LEGEND): each glyph in the tone of how sure the link is."""
    items = []
    for item in graph.LEGEND.split("  "):
        glyph, _, word = item.partition(" ")
        ev = "seen" if "━" in glyph else "declared" if "╌" in glyph else "possible" if "┄" in glyph else "bind"
        items.append((glyph, word, MAP_EV_TONE[ev]))
    return ui.Legend(items)


def map_title(G, only=False, links=None, st=None, web=False):
    """'-- MAP  27 nodes · 14 links · ✖ 7 need attention ----- ━━► seen  ╌╌► declared ...': the legend goes right, its last items first when narrow.
    The web (web=True) has its figures apart and, besides the legend, the choices as links (links: MapLinks, st: graph.State)."""
    k = graph.counts(G)
    probs = (Span(f"✖ {k['problems']} need" + ("s" if k["problems"] == 1 else "") + " attention", "err") if k["problems"]
             else Span("none needs attention", "muted"))  # not a green: missing data also draws nothing
    if not web:  # one coloured line: the figures' separators are part of its muted text
        parts = [Line([Span(f"{plural(k['nodes'], 'node')} · {plural(k['edges'], 'link')} · ", "muted"), probs]
                      + ([Span("  problems only", "warn", True)] if only else []))]
        return ui.Title("MAP", parts, None, map_legend())
    parts = [Span(plural(k["nodes"], "node"), "muted"), Span(plural(k["edges"], "link"), "muted"), probs]
    parts += [Span("problems only", "warn", True)] if only else []
    segs = []
    if links is not None and st is not None:
        segs = [ui.Seg("expand", [("expand all", "e", bool(st.all), links.expand), ("collapse all", "c", not (st.all or st.open or st.shut), links.collapse)]),
                ui.Seg("paths", [("all paths", "p", not only, links.paths(False)), ("problems only", "p", only, links.paths(True))])]
        if links.graph:
            segs.append(ui.Seg("view", [("tree", "", True, None), ("graph", "", False, links.graph)]))
    return ui.Title("MAP", parts, None, map_legend(), segs)


def map_notes(G, n=None, w=None):
    """The graph's notes as lines ('· text'), at most n (the last says how many were left out), each cut to w columns (None: not cut)."""
    nl = [ui.safe(x) for x in G["notes"]]
    if n is not None and len(nl) > n:
        nl = nl[:max(0, n - 1)] + ([f"… +{len(nl) - n + 1} more notes"] if n else [])
    return [Line([Span("  · " + x, "muted")], clip=w) for x in nl]


def map_branch(G, row, cursor=False, links=None, web=False, after=None):
    """One tree row as a component: tree glyphs (console), the mark (▸ opens, ▾ is open, · a leaf, ↻ already above), the arrow by evidence, the name
    by state (a symbol too: colour alone is not enough), the owner of a port, the port used, the qualifier, the note (the worst finding)."""
    p, n = graph.parts(G, row), G["nodes"].get(row["node"]) or {}
    st, spans = p["state"], []
    if p["arrow"]:
        spans += [Span(p["arrow"], MAP_EV_TONE.get(p["ev"])), _SP1]
    if not row["depth"]:
        spans.append(Span(p["label"], MAP_ROOT_TONE.get(st, "accent"), True))
    else:
        spans.append(Span(("" if web else MAP_MARK.get(st, "")) + p["label"], MAP_TONE.get(st)))
    if p["kind"] == "port":
        spans += [_SP2, Span("?", "warn") if p["owner"] in ("", "?") else Span(p["owner"], "strong")]
    if p["port"]:
        spans.append(Span(" " + p["port"]))
    if p["sub"]:
        spans += [_SP2, Span(p["sub"], "muted")]
    if p["note"]:
        lv = next((lv for lv, t in n.get("findings") or [] if t == p["note"]), "")
        spans += [_SP2, Span(p["note"], {"err": "err", "warn": "warn"}.get(lv, "muted"))]
    mark = Span(p["toggle"], "accent" if p["toggle"] in ("▸", "▾") else "muted")
    href = mark_href = None
    tip = "already shown above on this path" if row["cycle"] else "nothing below"
    if links is not None:
        href = links.row(row)
        if row["kids"] and not row["cycle"]:
            mark_href, tip = links.toggle(row), "close" if row["open"] else f"open: {row['kids']} below"
    return ui.Branch(row["key"], row["depth"], mark, Line(spans), st if st in ui.STATES else "unknown", href, mark_href, cursor, p["tree"], tip, after)


def map_props(G, nid, h=None, close=None):
    """Everything known about a node (graph.details) as the DETAILS pane, cut to h lines on the console."""
    return ui.Props("DETAILS", graph.details(G, nid), h, close)


def map_nodes(G, rs, w, h, cursor_key=None, details=None, top=None, only=False):
    """The Map's body as components for a console w columns wide and at most h lines tall: the title and the legend, the notes, the tree (its
    rows from `top`, scrolled so that the cursor's row stays in sight) and the details pane (beside the tree from MAP_PANE_W, below it
    otherwise). details: True = the selected row's node, or a node id. Without a cursor (the rotation slide) what does not fit is counted on
    the last line. -> MapScreen: the nodes (the console cuts them to h lines) and the first tree row shown."""
    notes, tree_h, pane_h, side = map_layout(G, w, h, bool(details))
    out = [map_title(G, only)] + map_notes(G, notes, w)
    i = graph.find(rs, cursor_key) if cursor_key else None
    if not rs:
        tree, top, n = [Msg("info", "no problem on any path: p shows every path" if only else "nothing to draw: see the notes above")], 0, 1
    elif cursor_key is None and len(rs) > tree_h:
        tree, top, n = [ui.Outline([map_branch(G, r) for r in rs[:tree_h - 1]]), More(len(rs) - tree_h + 1, "more rows", indent=3)], 0, tree_h
    else:
        top = ansi.scroll(top or 0, i or 0, len(rs), tree_h)
        shown = range(top, min(len(rs), top + tree_h))
        tree, n = [ui.Outline([map_branch(G, rs[j], j == i) for j in shown])], len(shown)
    nid = details if isinstance(details, str) else rs[i if i is not None else 0]["node"] if rs else None
    if pane_h and nid:
        if side:
            tw = w - (int(w * 0.42) + 3)
            out.append(ui.Split(tree, [map_props(G, nid, pane_h)], tw, tree_h))
        else:
            out += tree + [Line()] * (tree_h - n) + [map_props(G, nid, pane_h)]
    else:
        out += tree
    return MapScreen(out, top)


def map_view(G, rs, w, h, cursor_key=None, details=None, top=None, only=False):
    """(the Map body as console lines: at most h, none wider than w; the first tree row shown)."""
    sc = map_nodes(G, rs, w, h, cursor_key, details, top, only)
    return [ansi.clip(x, w) for x in ansi.render(ui.Group(sc.nodes), w)[0][:h]], sc.top


def map_web(G, rs, st, sel, nid, links, limit=None, truncated=False):
    """The Map for the web, nothing cut: the title with its choices, the notes, the tree (every row a link that selects it, its mark one that
    opens or closes it) and, beside it, the details of the selected node (nid: its node; None: no pane) with a link that closes them.
    limit: the rows the page draws at most (truncated: there were more)."""
    out = [map_title(G, st.only, links, st, True)] + map_notes(G)
    if rs:
        near = map_props(G, nid, None, links.close) if nid is not None else None  # the same details again, under the selected row (CSS shows one of the two)
        tree = [Group([ui.Outline([map_branch(G, r, r["key"] == sel, links, True, near if r["key"] == sel else None) for r in rs])])]
        if truncated:
            tree.append(Msg("info", f"… more than {limit} rows: close some branches, or show problems only"))
    else:
        tree = [Msg("info", "no problem on any path: show every path to see the map" if st.only else
                    "nothing to show yet" + (": see the notes above" if G["notes"] else ""))]
    if nid is not None:
        out.append(ui.Split(tree, [Group([map_props(G, nid, None, links.close)])]))
    else:
        out += tree
    return out


# ---- AI -------------------------------------------------------------------------------------------------------------------------
# Data: aisetup.catalog() (docs/AI.md): {hw, dir, runtime, recommended, active, space, models: [{..., assess, installed, pinned, commands}]}, the
# engine's snapshot (aiweb.engine().snapshot()) and the status of [ai] (render.ai_status). Everything in them is data (a model's name comes from a
# catalog file, a GPU's from a driver): text goes through hclean(), numbers through num(), a value that is missing is drawn '?'. The screen runs
# nothing and downloads nothing: what it does goes through the engine (render.ai_do, the web's forms), and it shows the commands to type.

AI_PANE_W = 140          # from this width up the details sit beside the list, below it otherwise
AI_ID_MAX = 64           # characters of a model id: it is a cursor, and a value in a URL
AI_NAME_MIN = 24         # the name column keeps this much (or its longest name) before another column is given up
AI_VERDICT = {  # verdict -> (label: a symbol besides the colour, SGR of the pill, web class, SGR of the text)
    "gpu": ("✔ FITS GPU", "1;42;30", "g", "32"), "partial": ("◐ GPU+CPU", "1;46;30", "c", "36"), "ram": ("✔ FITS RAM", "1;42;30", "g", "32"),
    "slow": ("! SLOW", "1;43;30", "y", "33"), "no": ("✖ TOO BIG", "1;41;37", "r", "31")}
AI_UNKNOWN = ("? UNKNOWN", "90", "d", "90")
AI_TONE = {"g": "ok", "c": "accent", "y": "warn", "r": "err", "d": "muted"}  # the class of a verdict -> the tone of its Badge and its text
AI_BACKEND = {"cuda": "CUDA", "rocm": "ROCm", "metal": "Metal", "vulkan": "Vulkan"}
AI_MARK = {"off": ("○ OFF", "muted"), "working": ("◐ WORKING", "accent"), "running": ("● ON", "ok"), "on": ("● ON", "ok"), "error": ("✖ ERROR", "err")}
AI_ACTIONS = ("toggle", "use", "delete", "delete-all", "cancel")  # AI on/off, use the model, delete it, delete all, cancel (ui.KEYMAP, scope ai)
AI_BAR = 14              # the columns of a download's bar on the console


def ai_id(x):
    return hclean(x, AI_ID_MAX) if isinstance(x, str) and x.strip() else None


def ai_mb(x):
    """Megabytes (MiB) as the usual words: 400 MB, 4.9 GB, 128 GB; '?' when it is not a number."""
    x = num(x)
    if x is None:
        return "?"
    return f"{x:.0f} MB" if x < 1024 else f"{x / 1024:.1f} GB" if x < 102400 else f"{x / 1024:.0f} GB"


def ai_size(n):
    """5000000000 -> '5.0 GB' ('nothing' for none): sizes of files, as aisetup says them ('?' when it cannot)."""
    try:
        import aisetup
        return aisetup.fmt_size(n) if n else "nothing"
    except Exception:  # noqa: BLE001
        return "?"


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


def ai_tone(verdict):
    """The tone of a verdict (and of its pill): ok, accent, warn, err; muted for what is not one."""
    return AI_TONE[AI_VERDICT.get(verdict, AI_UNKNOWN)[2]]


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


def ai_key(av, key, rows):
    """One key on the AI screen. Returns 'back' (leave it), an action for render.ai_do() ('toggle', 'use', 'delete', 'delete-all', 'cancel', and 'yes'
    for the question that is waiting) or '' (only the cursor or the details pane changed). What each key does: ui.KEYMAP, scope ai."""
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


# -- what the pieces say, as plain cleaned values (console and web share them) ----------------------------------------------------

def ai_simd(cpu):
    flags = {x.lower() for x in cpu.get("flags") if isinstance(x, str)} if isinstance(cpu.get("flags"), list) else set()
    return [n for n, ok in (("AVX2", "avx2" in flags), ("AVX-512", any(x.startswith("avx512") for x in flags)), ("NEON", "neon" in flags)) if ok], flags


def ai_cpu_name(s):
    """A processor's name without the trademarks, the clock and the core count: 'Intel(R) Core(TM) i7-10750H CPU @ 2.60GHz' -> 'Intel Core i7-10750H'."""
    s = hclean(s, 80)
    for pat in (r"\((?:R|TM|r|tm)\)", r"\s*@\s*[0-9.]+\s*[GM]Hz", r"\s+\d+-Core Processor", r"\s+CPU\b"):
        s = re.sub(pat, "", s)
    return re.sub(r"\s+", " ", s).strip()


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


def ai_spec(r, windows=False, h=None, actions=()):
    """The details of a model as a ui.Spec (the console cuts it to h lines)."""
    title, items = ai_details(r, windows)
    tone = lambda kind: AI_TONE[AI_VERDICT[kind][2]] if kind in AI_VERDICT else {"cmd": "accent", "warn": "warn", "dim": "muted"}.get(kind)  # noqa: E731
    return Spec(title, [(label, value, tone(kind), kind == "cmd") for label, value, kind in items], h, 9, actions)


def ai_space(cat):
    """(what the folder holds, what is free): '5.0 GB' or 'nothing', '120.0 GB free on that disk' or 'free space unknown'."""
    sp = dd(dd(cat).get("space"))
    free = num(sp.get("free"))
    return ai_size(sp.get("used")), (f"{ai_size(free)} free on that disk" if free else "free space unknown")


def _plen(items):
    """The columns a list of spans and bars takes on the console."""
    n = 0
    for x in items:
        if isinstance(x, Bar):
            n += x.w + (1 + len(x.value_text) if x.frac is not None and x.value_text else 0)
        else:
            n += len(getattr(x, "text", ""))
    return n


def _sp(text, tone=None, bold=False):
    return Span(text, tone, bold)


# -- the hardware ------------------------------------------------------------------------------------------------------------------

def ai_cpu_spans(hw, short=False):
    """'AMD Ryzen 7 5800X · 8 cores / 16 threads · AVX2' as spans (short: the name and the threads)."""
    cpu = dd(hw.get("cpu"))
    cores, threads = num(cpu.get("cores")), num(cpu.get("threads"))
    topo = f"{threads:.0f} threads" if threads else ""
    if cores:
        topo = f"{cores:.0f} cores" + (f" / {threads:.0f} threads" if threads and threads != cores else "")
    simd, flags = ai_simd(cpu)
    x86 = any(k in str(hw.get("arch")).lower() for k in ("x86", "amd64", "i386", "i686"))
    name = _sp(ai_cpu_name(cpu.get("model")) or "?", None, True)
    if short:
        return [name] + ([_sp(f" · {threads or cores:.0f} threads", "muted")] if threads or cores else [])
    return ([name] + ([_sp(" · " + topo, "muted")] if topo else []) + ([_sp(" · " + " ".join(simd), "muted")] if simd else [])
            + ([_sp(" · no AVX2: slow", "warn")] if x86 and flags and "avx2" not in flags and not simd else []))


def ai_memory_spans(tot, free, bw):
    """A bar of what is in use (bw 0: none), what is free of the total: '18.4 GB free of 31.2 GB'; '(free: ?)' when only the total is known."""
    if free is None:
        return ([_sp("░" * bw, "muted"), _sp("  ")] if bw else []) + [_sp(ai_mb(tot)), _sp(" (free: ?)", "muted")]
    return ([Bar(1 - free / tot, "", w=bw), _sp("  ")] if bw else []) + [_sp(f"{ai_mb(free)} free of {ai_mb(tot)}")]


def ai_ram_spans(hw, bw):
    tot, free = num(dd(hw.get("ram")).get("total_mb")), num(dd(hw.get("ram")).get("available_mb"))
    return ai_memory_spans(tot, free, bw) if tot else [_sp("?", "warn"), _sp(" could not be read", "muted")]


def ai_gpu_spans(g, bw):
    """(the GPU's name and backend, its memory) as spans: a bar of the video memory in use (bw 0: none), or 'unified memory' when it shares the RAM."""
    backend = AI_BACKEND.get(g.get("backend"))
    head = [_sp(hclean(g.get("name"), 48) or "?", None, True)] + ([_sp(" · " + backend, "muted")] if backend else [_sp(" · no usable backend: not used", "warn")])
    if g.get("unified"):
        return head, [_sp("unified memory: it shares the RAM" if bw else "unified memory", "muted")]
    tot = num(g.get("vram_mb"))
    return head, ai_memory_spans(tot, num(g.get("vram_free_mb")), bw) if tot else [_sp("?", "warn"), _sp(" video memory could not be read", "muted")]


def ai_hw_note(hw):
    return " ".join(hclean(hw.get(x), 12) for x in ("os", "arch") if hw.get(x))


def ai_hw_nodes(hw, w, k=0):
    """The HARDWARE section at level k, w columns wide: 0 everything (the bars, eight GPUs, a GPU on two lines when it does not fit on one, three
    notes), 1 one line each without bars (three GPUs) and one note, 2 the CPU and the RAM on one line (two GPUs) and no notes, 3 nothing."""
    if k >= 3:
        return []
    hw = dd(hw)
    gpus = [g for g in hw.get("gpus") if isinstance(g, dict)] if isinstance(hw.get("gpus"), list) else []
    notes = [hclean(x, 200) for x in hw.get("notes") if isinstance(x, str) and x.strip()] if isinstance(hw.get("notes"), list) else []
    bw = max(6, min(24, (w - 7) // 5))
    lab = lambda t: [_sp(" "), _sp(t.ljust(5), "muted"), _sp(" ")]  # noqa: E731
    out = [_head("HARDWARE", ai_hw_note(hw), w)]
    if k >= 2:
        out.append(Line([_sp(" ")] + ai_cpu_spans(hw, True) + [_sp("  ·  RAM ", "muted")] + ai_ram_spans(hw, 0), clip=w))
    else:
        out += [Line(lab("CPU") + ai_cpu_spans(hw), clip=w), Line(lab("RAM") + ai_ram_spans(hw, bw), clip=w)]
    keep = (8, 3, 2)[k]  # the GPUs drawn: every one when there is room
    for i, g in enumerate(gpus[:keep]):
        head, mem = ai_gpu_spans(g, bw if k == 0 else 0)  # the bars only at level 0
        one = lab("GPU" if i == 0 or k >= 2 else "") + head + [_sp("  ")] + mem
        if k >= 1 or _plen(one) <= w:
            out.append(Line(one, clip=w))
        else:
            out += [Line(lab("GPU" if i == 0 else "") + head, clip=w), Line(lab("") + mem, clip=w)]
    if len(gpus) > keep:
        out.append(Line([_sp(f"       … +{len(gpus) - keep} more GPUs", "muted")], clip=w))
    if not gpus:
        out.append(Line(lab("GPU") + [_sp("none found", "warn"), _sp(": the models run on the CPU, from RAM", "muted")], clip=w))
    if k < 2:
        nn = 3 if k == 0 else 1
        shown = notes if len(notes) <= nn else notes[:nn - 1] + [f"… +{len(notes) - nn + 1} more notes"]
        for x in shown:
            rows = textwrap.wrap(x, max(10, w - 4), break_on_hyphens=False) if k == 0 else [x]
            out.append(Line([_sp("  · " + rows[0] if len(rows[0]) + 4 <= w else "  · " + rows[0][:max(1, w - 5)] + "…", "muted")]))
            out += [Line([_sp("    " + y, "muted")]) for y in rows[1:2]]
    return out


# -- the status: what [ai] says, the model server, the model in use --------------------------------------------------------------------

def ai_status_parts(st, cat, ids):
    """The STATUS section's pieces as spans: what [ai] says (adv, with its note), the endpoint and whether it answers (ep, short, detail, and ans: both),
    the model in use (model), the runtime (run), the directory of the files (files)."""
    st, probe, rt = dd(st), dd(st).get("probe"), dd(dd(cat).get("runtime"))
    on_, sw = bool(st.get("enabled")), dd(st.get("switch"))
    note = ("  ([ai] enabled = yes)" if on_ else "  ([ai] enabled = no in config.ini)") if not sw else \
        "  ([ai] enabled = yes)" if sw.get("by") == "config" else "  (turned on from the AI page or screen)" if on_ else "  (off: the AI switch turns it on)"
    out = {"on": on_, "adv": _sp("✔ on", "ok") if on_ else _sp("· off", "muted"), "note": _sp(note, "muted"), "ep": hclean(st.get("endpoint"), 120), "detail": ""}
    if not on_ or (probe or {}).get("state") == "off":
        out["short"] = _sp("· not asked while the advisor is off", "muted")
    elif probe is None:
        out["short"] = _sp("· checking…", "muted")
    elif probe.get("state") == "answering":
        names = [x for x in probe.get("models") or [] if isinstance(x, str)]
        out["short"] = _sp("✔ answering", "ok")
        out["detail"] = plural(len(names), "model") + (": " + ", ".join(names[:3]) + ("…" if len(names) > 3 else "") if names else "")
    else:
        out["short"] = _sp("✖ not answering", "err")
        out["detail"] = hclean(probe.get("msg"), 160)
    model = hclean(st.get("model"), 80)
    out["model"] = ([_sp("●", "accent"), _sp(" "), _sp(model, None, True), _sp("  in the catalog", "muted") if model in ids else _sp("  not in the catalog", "warn")]
                    if model else [_sp("· none chosen ([ai] model is empty)", "muted")])
    out["run"] = ([_sp("✔ installed", "ok")] + ([_sp(f" ({hclean(rt.get('version'), 20)})", "muted")] if rt.get("version") else []) if rt.get("installed")
                  else [_sp("! not installed", "warn"), _sp(": setup downloads it", "muted")])
    out["files"] = hclean(dd(cat).get("dir"), 120)
    out["ans"] = [out["short"]] + ([_sp(" · " + out["detail"], "muted")] if out["detail"] else [])
    return out


def ai_status_nodes(st, cat, ids, w, k=0):
    """The STATUS section at level k, w columns wide: 0 six lines (more when the server's answer wraps), 1 two (no endpoint, runtime or files), 2 one
    without a title, 3 nothing."""
    if k >= 3:
        return []
    t = ai_status_parts(st, cat, ids)
    lab = lambda x: [_sp(" "), _sp(x.ljust(9), "muted"), _sp(" ")]  # noqa: E731
    sep = lambda: _sp(" · ", "muted")  # noqa: E731
    if k >= 2:
        return [Line([_sp(" "), _sp("status ", "muted"), t["adv"], sep(), t["short"], sep()] + t["model"], clip=w)]
    if k == 1:
        return [_head("STATUS", "", w), Line(lab("advisor") + [t["adv"]] + ([sep(), t["short"]] if t["on"] else [t["note"]]), clip=w),
                Line(lab("model") + t["model"], clip=w)]
    srv = [Line(lab("server") + t["ans"], clip=w)]
    if _plen(lab("server") + t["ans"]) > w:  # what the server said does not fit beside the verdict: under it, wrapped
        srv = [Line(lab("server") + [t["short"]], clip=w)] + [Line(lab("") + [_sp(x, "muted")], clip=w)
                                                                for x in textwrap.wrap(t["detail"], max(10, w - 11), break_on_hyphens=False)[:2]]
    return [_head("STATUS", "", w), Line(lab("advisor") + [t["adv"], t["note"]], clip=w), Line(lab("endpoint") + [_sp(t["ep"])], clip=w)] + srv \
        + [Line(lab("model") + t["model"], clip=w), Line(lab("runtime") + t["run"], clip=w)] \
        + ([Line(lab("files") + [_sp(t["files"], "muted")], clip=w)] if t["files"] else [])


# -- what is being done: the switch, a download, the answer to the last key, the folder -------------------------------------------------

def ai_work_nodes(st, cat, av, w, k=0):
    """The lines under the title: whether the AI is on and what it is doing (a download with its bar, the server starting, an error), that it is locked,
    the answer to the last key, and the folder the models are downloaded to with what it holds and what is free. At level k the folder goes first
    when the screen is small (k 1), then the lock and the answer (k 2), the state line stays (k 3). [] when the status has no engine snapshot."""
    snap = dd(st).get("snap")
    if not isinstance(snap, dict) or not isinstance(snap.get("state"), (list, tuple)):
        return []
    state, text = snap["state"]
    mark, tone = AI_MARK.get(state, ("?", "muted"))
    job, extra = snap.get("job"), []
    if state == "working" and job and job.get("total"):
        n = min(AI_BAR, round(AI_BAR * job["done"] / job["total"]))
        extra = [_sp(" "), Bar(n / AI_BAR, "", w=AI_BAR, tone="accent")]
    tail = [_sp("   (c: cancel)", "muted")] if state == "working" and not snap.get("locked") else []
    lock = [_sp("[locked by config.ini] ", "warn")] if snap.get("locked") and k >= 2 else []  # it goes up front, where a narrow screen keeps it
    out = [Line([_sp(" "), _sp(mark.ljust(10), tone, True), _sp(" ")] + lock + [_sp(hclean(text, 200))] + extra + tail, clip=w)]
    if k >= 3:
        return out
    note = av.msg if av is not None and av.msg else None
    if note is None and isinstance(snap.get("notice"), dict):
        note = ("ok" if snap["notice"].get("ok") else "err", snap["notice"].get("text"))
    if snap.get("locked") and k < 2:
        out.append(Line([_sp(" "), _sp("locked by config.ini ([ai] web_actions = no): this screen only shows", "warn")], clip=w))
    if note and note[1] and k < 3:
        out.append(Line([_sp(" "), _sp(hclean(note[1], 300), {"ok": "ok", "err": "err", "warn": "warn"}.get(note[0], "muted"))], clip=w))
    cat = dd(cat)
    if cat.get("dir") and k < 1:
        used, free = ai_space(cat)
        out.insert(1, Line([_sp(" "), _sp("folder", "muted"), _sp(" " + hclean(cat["dir"], 120)), _sp(f" · {used} downloaded · {free}", "muted")], clip=w))
    return out


def ai_counts(rows):
    """'✔ 7 fit  ! 2 slow  ✖ 2 too big' as a Line (empty: no models)."""
    n = {v: sum(1 for r in rows or [] if r["verdict"] == v) for v in AI_VERDICT}
    bits = [_sp(f"{sym} {k} {word}", tone) for k, sym, word, tone in ((n["gpu"] + n["ram"], "✔", "fit", "ok"), (n["partial"], "◐", "gpu+cpu", "accent"),
                                                                     (n["slow"], "!", "slow", "warn"), (n["no"], "✖", "too big", "err")) if k]
    return Line([x for i, b in enumerate(bits) for x in ([_sp("  ")] if i else []) + [b]])


def ai_title(rows, w):
    """'── AI  what this machine can run · 12 models · ✔ 7 fit  ! 2 slow  ✖ 2 too big': the tagline goes first when narrow, then the count (w None:
    the web, everything)."""
    counts = ai_counts(rows)
    tag, count = _sp("what this machine can run", "muted"), _sp(plural(len(rows or []), "model"), "muted")
    if w is None:
        return ui.Title("AI", [tag, count] + ([counts] if counts.spans else []))
    left = 7  # '── AI ' and the space after it
    for bits in ([tag, count, counts], [count, counts], [counts], [count]) if rows is not None else ([],):
        bits = [x for x in bits if getattr(x, "text", "") or getattr(x, "spans", None)]
        if left + _plen(bits) + 3 * max(0, len(bits) - 1) + 3 <= w:
            break
    return ui.Title("AI", bits)


# -- the models --------------------------------------------------------------------------------------------------------------------

def ai_legend(w):
    """One line: what the marks in front of a model mean, and that the speed is a guess."""
    return _joined([Line([_sp("★", "warn"), _sp(" recommended", "muted")]), Line([_sp("✓", "ok"), _sp(" installed", "muted")]),
                    Line([_sp("●", "accent"), _sp(" active", "muted")]), _sp("tok/s: rough estimate", "muted")], w, [" "], "  ·  ")


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
    """The console's heading of the table, one muted line."""
    keep, nw, pw, nn = lay
    cells = {"params": "params".ljust(pw), "size": " size".rjust(7), "need": "needs".rjust(7), "verdict": " verdict".ljust(12), "tok": "est tok/s"}
    return Line([_sp("      " + "model".ljust(nw + 2) + "  ".join(cells[k] for k in keep) + ("  notes" if nn else ""), "muted")])


def ai_badge(v):
    """The verdict's pill."""
    return Badge(AI_VERDICT.get(v, AI_UNKNOWN)[0], ai_tone(v), 10)


def ai_marks(r):
    """The marks in front of a model: recommended, installed, active (three columns)."""
    return Line([_sp("★", "warn") if r["rec"] else _sp(" "), _sp("✓", "ok") if r["installed"] else _sp(" "), _sp("●", "accent") if r["active"] else _sp(" ")])


def ai_table(rows, lay, first, count, cur, fw):
    """The console's table of the models first .. first + count, fw columns wide: the marks, the name, the columns that fit, the notes; the cursor's
    row (cur: its index in rows) solid. Nothing is padded by the Table: each cell is the width it was drawn at."""
    keep, nw, pw, nn = lay
    head = {"params": "params", "size": "size", "need": "needs", "verdict": "verdict", "tok": "est tok/s"}
    cols = [Col("marks", "", gap=2), Col("name", "model", gap=2)] + [Col(k, head[k], gap=2) for k in keep] + ([Col("notes", "notes")] if nn else [])
    if not keep:  # only the name is left: the two spaces after it were always there
        cols.append(Col("end", ""))
    body = []
    for j in range(first, min(len(rows), first + count)):
        r = rows[j]
        name = r["name"] if len(r["name"]) <= nw else r["name"][:nw - 1] + "…"
        cells = {"params": _sp(r["params"].ljust(pw), "muted"), "size": _sp(ai_mb(r["size_mb"]).rjust(7)), "need": _sp(ai_mb(r["need_mb"]).rjust(7)),
                 "verdict": ai_badge(r["verdict"]), "tok": _sp(ai_tok(r["tok"]).ljust(9))}
        row = [ai_marks(r), Span(name.ljust(nw), "muted" if r["verdict"] == "no" else None, full=r["name"])] + [cells[k] for k in keep]
        if nn:
            row.append(_sp(r["notes"][:nn - 1] + "…" if len(r["notes"]) > nn else r["notes"], "muted"))
        if not keep:
            row.append(_sp(""))
        body.append(Row(row, "sel" if j == cur else None, r["id"]))
    return Table(cols, body, fill=True, solid=fw, clip=fw)


def _group_lines(nodes, w):
    """The console's lines of components drawn as one Group (the AI screen's layout counts them so)."""
    return ansi.render(ui.Group(list(nodes)), w)[0]


def ai_lines(data, st, av, rows, w, h):
    """The AI screen's body as console lines: at most h, none wider than w. Title, HARDWARE (and STATUS beside it from 110 columns), MODELS with the
    cursor's row in reverse video, the legend, then the details (below the list, beside it from AI_PANE_W) or, without them, STATUS under
    the list. The sections lose detail from the bottom up, as the screen gets smaller, before the list loses rows. data: {cat, msg, err} as
    render.ai_data() has it, st: render.ai_status(), av: the AiView (its cursor and scroll are set here), rows: ai_rows(cat)."""
    cat = data["cat"]
    if cat is None:  # the catalog could not be read
        return _group_lines([ai_title(None, w)] + _msg("err" if data.get("err") else "info", data["msg"], w), w)[:h]
    n, hwd = len(rows), dd(cat.get("hw"))
    ai_sync(av, rows)
    sel = rows[av.idx] if rows else None
    pane = bool(av.details and sel)
    side, two, ids = pane and w >= AI_PANE_W, w >= 110, {r["id"] for r in rows}
    windows = hwd.get("os") == "windows"
    fw = int(w * 0.55) if side else w
    lay = ai_layout(rows, fw)
    full = len(_group_lines([ai_spec(sel, windows, 99)], w - fw - 3 if side else w)) if pane else 0
    levels = {}

    def level(k):  # the three sections at level k and the lines they take (each node is one line; side by side, the taller of the two)
        if k not in levels:
            work = ai_work_nodes(st, cat, av, w, k)  # the switch, the folder, the last answer: they give way last, one line at a time
            if two:
                lw = (w - 3) * 6 // 11
                hw, stn = (ai_hw_nodes(hwd, lw, k), ai_status_nodes(st, cat, ids, w - 3 - lw, k)) if k < 3 else ([], [])
                top, bottom, ntop = ([ui.Cols([(Group(hw), lw), (Group(stn), w - 3 - lw)], gap=3, once=True)] if k < 3 else []), [], max(len(hw), len(stn))
            else:
                top, bottom = ai_hw_nodes(hwd, w, k), [] if pane else ai_status_nodes(st, cat, ids, w, k)
                ntop = len(top)
            levels[k] = (work, top, bottom, ntop)
        return levels[k]

    for want in ((6, 4) if pane and not side else (8, 4)):  # first the comfortable list, then the tight one
        for k in range(4):
            work, top, bottom, ntop = level(k)
            avail = h - 1 - len(work) - ntop - len(bottom) - 3  # the title and the work lines; MODELS and its header and the legend
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
    av.rows, av.top = max(1, rows_n), ansi.scroll(av.top, av.idx, n, rows_n) if n else 0
    note = f"{av.top + 1}-{min(n, av.top + rows_n)} of {n} · best first" if n > rows_n else "best first"
    block = [_head("MODELS", note, fw), ai_header(lay)]
    if not n:
        block.append(Msg("info", "the catalog lists no model"))
    else:
        block.append(ai_table(rows, lay, av.top, rows_n, av.idx, fw))
    if side:
        nblock = 2 + (min(n, av.top + rows_n) - av.top if n else 1)  # MODELS, its header and the rows (or the line that says there are none)
        out.append(ui.Split(block, [ai_spec(sel, windows, max(nblock, min(full, avail + 2)))], fw, 0))
    else:
        out += block
    out.append(ai_legend(w))
    if pane and not side:
        out.append(ai_spec(sel, windows, max(0, avail - rows_n)))
    else:
        out += bottom
    return [ansi.clip(x, w) for x in _group_lines(out, w)[:h]]


def ai_footer(av, n, w, snap=None, enabled=None):
    """The footer of the AI screen as a component: where the cursor is, and the keys, in short words when the screen is narrow, then the least needed
    go first. The keys that act (AI on/off, use the model, delete, delete all, cancel) are there unless [ai] web_actions = no locked them (snap
    says); a question that waits is the footer (a ui.Question). enabled: render.on, which features are on."""
    if av.confirm:
        return ui.Question(av.confirm[2])
    acts = isinstance(snap, dict) and not snap.get("locked")
    pos = f"model {av.idx + 1}/{n}" if n else "no models"
    dyn = {"details": ("Enter: " + ("hide details" if av.details else "details"), "Enter: " + ("hide" if av.details else "details"))}
    extra = [(8, "questions: web page or nuc-console-ask", "")] if acts else []
    return Line([_sp(ui.footer("ai", f" {pos}   ", w, enabled, dyn, skip=() if acts else AI_ACTIONS, extra=extra), "muted")], clip=w)


# -- the web: the same pieces, nothing cut, and the buttons ------------------------------------------------------------------------------

AI_POST = "/ai/"  # the engine's endpoints (web.py do_POST): /ai/on /ai/off /ai/cancel /ai/use /ai/delete /ai/delete-all /ai/ask /ai/advise
AI_USE_TITLE = "download it if needed, start it, turn AI on"


class AiActs(object):
    """What the web's buttons need: the CSRF token and the query string of the page a form comes back to. Every Action of the screen carries both as
    hidden fields, then its own (a model id, a number of days): the fields the handlers of /ai/* have always read. The page that only shows (the
    admin locked it: [ai] web_actions = no) gets None instead and draws no form."""
    __slots__ = ("csrf", "back")

    def __init__(self, csrf, back):
        self.csrf, self.back = csrf, back

    def fields(self, *extra):
        return [("csrf", self.csrf), ("back", self.back)] + list(extra)


class AiLinks(object):
    """Where the web's links go: row(id) the page that selects that model (or, when it is the selected one, closes its details), close the page with
    no model selected, no the page without the question that waits."""
    __slots__ = ("row", "close", "no")

    def __init__(self, row, close, no):
        self.row, self.close, self.no = row, close, no


def _act(acts, action, label, extra=(), key="", tone=None, title="", disabled=False, ask=None):
    """An Action of the screen, or None where the page only shows."""
    return None if acts is None else Action(AI_POST + action, label, acts.fields(*extra), key, tone, title, disabled, ask)


def ai_use_cell(r, snap, acts):
    """The last cell of a model's row: the button that does everything for it (use this model: download it if needed, start it, turn AI on), or the
    reason there is none."""
    if not r["pinned"]:
        return _sp("not pinned yet", "muted")
    if r["verdict"] == "no":
        return _sp("too big", "muted")
    if r["active"] and snap["state"][0] in ("running", "on"):
        return _sp("● in use", "ok")
    working = bool(snap["job"] and snap["job"]["state"] == "running")
    return _act(acts, "use", "use this model", [("model", r["id"])], "", "ok", AI_USE_TITLE, working) or _sp("")


def ai_hw_kv(hw):
    """The HARDWARE section for the web: (the heading, the labelled values): the CPU, the RAM, every GPU, then the notes."""
    hw = dd(hw)
    gpus = [g for g in hw.get("gpus") if isinstance(g, dict)] if isinstance(hw.get("gpus"), list) else []
    notes = [hclean(x, 200) for x in hw.get("notes") if isinstance(x, str) and x.strip()] if isinstance(hw.get("notes"), list) else []
    pairs = [("CPU", Line(ai_cpu_spans(hw))), ("RAM", Line(ai_ram_spans(hw, 10)))]
    for g in gpus:
        head, mem = ai_gpu_spans(g, 10)
        pairs.append(("GPU", Line(head + [_sp("  ")] + mem)))
    if not gpus:
        pairs.append(("GPU", Line([_sp("none found", "warn"), _sp(": the models run on the CPU, from RAM", "muted")])))
    return Head("HARDWARE", ai_hw_note(hw)), [KV(pairs)] + [Line([_sp("· " + x, "muted")]) for x in notes]


def ai_status_kv(st, cat, ids):
    """The STATUS section for the web: the heading and the labelled values (advisor, endpoint, server, model, runtime, files)."""
    t = ai_status_parts(st, cat, ids)
    pairs = [("advisor", Line([t["adv"], t["note"]])), ("endpoint", Line([_sp(t["ep"], None, False)])), ("server", Line(t["ans"])), ("model", Line(t["model"])),
             ("runtime", Line(t["run"]))]
    if t["files"]:
        pairs.append(("files", Line([Span(t["files"], "muted", False, True)])))
    return Head("STATUS"), [KV(pairs)]


def ai_choice_line(ch, rows):
    """What turning the AI on will use, as a line (the switch is off): the model chosen before, the recommended one the first time (it asks first), or
    advice to choose. None when there is no choice to speak of."""
    if not ch:
        return None
    t = next((r for r in rows if r["id"] == ch["target"]), None)
    size = ch["size"]
    todo = "installed here" if ch["installed"] else (f"{ai_mb((size or 0) / 2 ** 20)} to download" if size else "not downloadable yet")
    if ch["model"] and t:
        return Line([_sp("turning it on uses ", "muted"), _sp(t["name"], None, True), _sp(f" ({'chosen on this page' if ch['by'] == 'page' else '[ai] model in config.ini'}; {todo})", "muted")])
    if t:
        return Line([_sp("no model is chosen yet: turning it on asks about the recommended one, ", "muted"), _sp(t["name"], None, True), _sp(f" ({todo})", "muted")])
    return Line([_sp("no model fits this machine comfortably: choose one from the list below", "muted")])


def ai_question(confirm, sel, ch, cat, rows, acts, links):
    """The question a click asked first (confirm: on, delete, delete-all), as a ui.Question, or None when there is none to ask (no such model, or the
    page only shows)."""
    t = next((r for r in rows if r["id"] == sel), None)
    if confirm == "on" and t:
        yes = _act(acts, "on", "Yes, turn it on", [("model", sel), ("confirm", "yes")], "y", "ok")
        have = (ch or {}).get("installed")
        text = f"Turn AI on with {t['name']}? " + ("It is installed here." if have else
                                                     f"It is not here yet: {ai_mb(((ch or {}).get('size') or 0) / 2 ** 20)} to download (the SHA-256 is checked), then the "
                                                     "model server starts on this machine and the advisor is turned on.")
    elif confirm == "delete" and t:
        yes = _act(acts, "delete", "Yes, delete", [("model", sel), ("confirm", "yes")], "y", "err")
        text = f"Delete the files of {t['name']} ({ai_mb(t['size_mb'])})? You can download it again later."
    elif confirm == "delete-all":
        yes = _act(acts, "delete-all", "Yes, delete everything", [("confirm", "yes")], "y", "err")
        text = f"Delete the runtime and every downloaded model ({ai_size(dd(dd(cat).get('space')).get('used'))})? You can download them again later."
    else:
        return None
    return None if acts is None else Question(text, yes, links.no)


def ai_control_nodes(snap, ch, cat, rows, sel, confirm, acts, links):
    """The top of the web screen: is the AI on, what is it doing (a download with its progress, the model server loading), the button (turn it on, turn
    it off, cancel), what turning it on will use, the folder the models go to, the answer to the last click, and the question a click asked first."""
    state, text = snap["state"]
    mark, tone = AI_MARK.get(state, ("?", "muted"))
    items = [Badge(mark, tone), _sp(text, None, True)]
    if snap["locked"]:
        pass
    elif state == "working":
        items.append(_act(acts, "cancel", "Cancel", (), "c", "err", "stop it: what was fetched is kept"))
    elif dd(snap.get("switch")).get("by") == "config":
        items.append(_sp("on by config.ini ([ai] enabled = yes): turn it off there", "muted"))
    elif dd(snap.get("switch")).get("on"):
        items.append(_act(acts, "off", "Turn AI off", (), "e", "err", "stop the model server and turn the advisor off"))
    else:
        items.append(_act(acts, "on", "Turn AI on", (), "e", "ok", "set up the model and start it"))
    out = [Controls([x for x in items if x is not None])]
    if snap["locked"]:
        out.append(ui.Notice("warn", "locked by config.ini ([ai] web_actions = no): this page only shows"))
    job = snap.get("job")
    if state == "working" and job and job.get("phase") == "downloading" and job.get("total"):
        out.append(Line([Bar(job["done"] / job["total"], f"{job['pct']}%", w=AI_BAR, tone="accent")]))
    if not snap["locked"] and state == "off":
        said = ai_choice_line(ch, rows)
        if said is not None:
            out.append(said)
    if dd(cat).get("dir"):
        used, free = ai_space(cat)
        out.append(Line([_sp("models are downloaded to ", "muted"), Span(hclean(cat["dir"], 200), None, False, True), _sp(f" · {used} downloaded · {free}", "muted")]))
    notice = snap.get("notice")
    if notice:
        out.append(ui.Notice("ok" if notice.get("ok") else "err", hclean(notice.get("text"), 300)))
    q = ai_question(confirm, sel, ch, cat, rows, acts, links) if not snap["locked"] else None
    return out + ([q] if q is not None else [])


def ai_chat_nodes(snap, chat, acts):
    """The chat under the switch: what was asked and answered (chat: the Qa of the exchanges, oldest first, built by the page from the engine's
    history), the question box, 'advice now', and why the box is asleep when it is. The box works when the AI is on and its server is not starting."""
    c = snap["chat"]
    ready = dd(snap.get("switch")).get("on") and snap["state"][0] != "working"
    asleep = not ready or bool(c["busy"])
    out = [Head("CHAT", "ask the model about this machine (it reads this machine's history; AI, check before acting)")] + list(chat)
    if acts is not None:
        out.append(_act(acts, "ask", "Ask", (), "", "ok", "", asleep, ("q", "ask: why is the disk filling up?", 500)))
        out.append(Controls([_sp("advice now:", "muted")] + [_act(acts, "advise", label, [("days", d)], "", None, "", asleep)
                                                              for d, label in ((1, "last 24 h"), (7, "last 7 days"), (30, "last 30 days"))]))
        if not ready:
            out.append(Msg("info", "the model is still starting: the box wakes up when it answers" if snap["state"][0] == "working" else "turn AI on to ask"))
    return [Group(out)]


def ai_models_web(rows, snap, sel, acts, links, windows):
    """The MODELS section for the web: every model as a row of a Table (a link: its details beside the table), the marks in words, the verdict a
    Badge, the button that uses it, the legend; and the details of the selected one (a Spec with its buttons, and the link that closes it)."""
    if not rows:
        return [Group([Head("MODELS", "best first"), Msg("info", "the catalog lists no model")])]
    cols = [Col("name", "Model"), Col("params", "Params", prio=2), Col("size", "Size", "r", 1, True), Col("need", "Needs", "r", 0, True), Col("verdict", "Verdict"),
            Col("tok", "Est tok/s", "r", 3, True), Col("notes", "Notes", prio=3)]
    if acts is not None:
        cols.append(Col("use", ""))
    body = []
    for r in rows:
        name = [_sp(r["name"], "muted" if r["verdict"] == "no" else None, True)] + [_sp("  " + word, tone) for word, tone, on in
                                                                                    (("★ recommended", "warn", r["rec"]), ("✓ installed", "ok", r["installed"]),
                                                                                     ("● active", "accent", r["active"])) if on]
        cells = [Line(name), _sp(r["params"], "muted"), _sp(ai_mb(r["size_mb"])), _sp(ai_mb(r["need_mb"])), ai_badge(r["verdict"]), _sp(ai_tok(r["tok"])),
                 _sp(r["notes"], "muted")]
        if acts is not None:
            cells.append(ai_use_cell(r, snap, acts))
        body.append(Row(cells, "sel" if r["id"] == sel else None, None, links.row(r["id"])))
    table = [Head("MODELS", "best first"), Table(cols, body, head=True), ai_legend(None)]
    chosen = next((r for r in rows if r["id"] == sel), None)
    if chosen is None:
        return [Group(table)]
    do = []
    if acts is not None:
        if chosen["pinned"] and chosen["verdict"] != "no" and not (chosen["active"] and snap["state"][0] in ("running", "on")):
            do.append(_act(acts, "use", "use this model", [("model", chosen["id"])], "u", "ok", AI_USE_TITLE, bool(snap["job"] and snap["job"]["state"] == "running")))
        if chosen["installed"]:
            do.append(_act(acts, "delete", "delete its files", [("model", chosen["id"])], "x", "err", "asks first", bool(snap["job"] and snap["job"]["state"] == "running")))
    close = ui.Seg("details", [("close details ✕", "Escape", False, links.close)])
    return [ui.Split([Group(table)], [Group([ai_spec(chosen, windows, None, [a for a in do if a is not None]), close])], 0, 0)]


def ai_model(data, st, snap, ch, rows, sel="", confirm="", acts=None, links=None, chat=()):
    """The AI screen for the web, nothing cut: the title, the switch with what it is doing and the question that waits, the chat, the hardware and
    the status side by side, the models with the details of the selected one, and what can be deleted. snap: the engine's snapshot, ch: its choice
    (None: locked), acts: the AiActs of the forms (None: the page only shows: no form, no button), links: the AiLinks, chat: [ui.Qa]."""
    cat = data["cat"]
    if cat is None:
        return [ai_title(None, None)] + _msg("err" if data.get("err") else "info", data["msg"], None)
    hwd, ids = dd(cat.get("hw")), {r["id"] for r in rows}
    out = [ai_title(rows, None), Group(ai_control_nodes(snap, ch, cat, rows, sel, confirm, acts, links))] + ai_chat_nodes(snap, chat, acts)
    hh, hb = ai_hw_kv(hwd)
    sh, sb = ai_status_kv(st, cat, ids)
    out.append(ui.Cols([(Group([hh] + hb), None), (Group([sh] + sb), None)]))
    out += ai_models_web(rows, snap, sel, acts, links, hwd.get("os") == "windows")
    used = dd(cat.get("space")).get("used")
    if acts is not None and (used or any(r["installed"] for r in rows)):
        out.append(Controls([_sp("manage:", "muted"), _act(acts, "delete-all", "delete everything", (), "X", "err", "asks first",
                                                           bool(snap["job"] and snap["job"]["state"] == "running")),
                             _sp(f"the runtime and every model ({ai_size(used)})", "muted")]))
    return out
