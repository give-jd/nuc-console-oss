"""The full screens' view-models (stdlib only, Python 3.8+): what each screen is made of, as the components of ui.py.

A screen of the console (CPU and Health today, later the Map and the AI) is a function of its data and of the size it is drawn at: it
returns components (ui.py) and, for a list, the numbers the live loop needs (the first row shown, how many are visible). The state a screen
keeps while it is open lives here too (CpuView, HealthView and their keys: ui.KEYMAP says what each key does). The console draws the
components with ansi.render, the web with htmlview.html: one model, two renderers.

Nothing here reads a file, the configuration, the host or the clock, and nothing draws: what a screen needs to know of the world comes in as
an argument (a small context object such as CpuCtx, the time now, the advisor's answer) or inside the data. Producers, the live loop and the
page handlers stay in render.py and web.py, which keeps shims for the names that moved (for the tests and the tools that still call them).

Layout that only the console has (how many lines the room is, which block is cut, where a table's rows end) is decided here from the size
and written into the components' console fields (ui.Cap, ui.Split, ui.Cols, the widths of a ui.Col); the web ignores them and draws every
row, because a page scrolls (a Health builder is told None for the size when the web is the reader). The console draws these screens byte
for byte as it did before they were components (tests/test_screen_cpu.py, tests/test_screen_health.py and tests/golden keep it so).

Each screen has a block below, marked `# ---- NAME`.
"""
import textwrap
import time

import ansi
import ui
from ui import (KV, THERMAL_ERR, THERMAL_WARN, Bar, Cap, Col, Finding, Grid, Group, Head, Kpi, Line, Meter, More, Msg, Only, Pane, Row, Series, Span,
                Split, Table, Tiles, Wrap, dd, dget, fmt_ago, fmt_cputime, fmt_dur, fmt_k, fmt_min, fmt_size, hclean, hcount, hnum, human, idict,
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
    cols = [Col("app", "App", w=nm, gap=2), Col("share", "Share of CPU time", gap=2), Col("avg", "Avg", w=8, gap=2, num=True), Col("trend", "Per " + ("hour" if days <= 1 else "day"), gap=2),
            Col("peak", "Peak")]
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
    cols = [Col("app", "App", w=nm, gap=2), Col("avg", "Avg", w=9, gap=2, num=True), Col("max", "Max", w=9, gap=2, num=True)]
    cols += [Col("trend", "Trend", w=11, gap=2)] if trend else []
    cols.append(Col("series", "Per " + ("hour" if days <= 1 else "day")))
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
    cols = [Col("n", "Messages", align="r", w=6, gap=2, num=True), Col("new", "New", w=3, gap=1), Col("unit", "Unit", w=ww or None, gap=1), Col("msg", "Message")]
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
    cols = [Col("mount", "Mount", w=mw, gap=1), Col("used", "Used", gap=1), Col("pct", "", gap=2, num=True), Col("full", "Full in")]
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
