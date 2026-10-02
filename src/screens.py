"""The full screens' view-models (stdlib only, Python 3.8+): what each screen is made of, as components.

A screen of the console (the CPU, later the Health, the Map and the AI) is a function of its data and of the size it is drawn at: it
returns components (ui.py) and the numbers the live loop needs (the first row shown, how many are visible). The console draws them with
ansi.render, the web with htmlview.html: one model, two renderers. Nothing here reads the host, the configuration or the clock; what the
screen needs to know about them comes in as a small context object (CpuCtx) or inside the data. Producers, the live loop and the page
handlers stay in render.py and web.py.

Layout that only the console has (how many lines the room is, which block is cut, where the table's rows end) is decided here from the
size and written into the components' console fields (ui.Cap, ui.Split, the widths of a ui.Col); the web ignores those and draws every
row, because a page scrolls. The console draws these screens byte for byte as it did before they were components
(tests/test_screen_cpu.py and tests/golden keep it so).

Each screen has a block below, marked `# ---- NAME`.
"""
import time

import ansi
import ui
from ui import (THERMAL_ERR, THERMAL_WARN, Bar, Cap, Col, Group, Grid, Head, Kpi, KV, Line, Meter, More, Msg, Only, Row, Span, Split, Table,
                Tiles, Wrap, dd, dget, fmt_ago, fmt_cputime, fmt_dur, fmt_k, fmt_min, fmt_size, idict, num, qf, safe)


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
