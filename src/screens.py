"""The full screens' view-models (stdlib only, Python 3.8+): what each screen shows, as the components of ui.py.

The screens (Health, CPU, Map, AI) read their data in render.py and web.py; the part in between lives here: the state a screen keeps
while it is open (HealthView and its keys: ui.KEYMAP says what each key does), and the builders that turn a report into components.
A builder is told the console's width and height, or None for both when the web is the reader: the console builds what fits (the sections
at the fullest level that fits, the findings that are in view), the web everything. The components are drawn by ansi.py (the console) and
htmlview.py (the web), so that one model is what both show.

Nothing here reads a file, the configuration or the host, and nothing draws: the clock comes in as an argument (now) and so does the
advisor's answer. render.py keeps shims for the names that moved (for the tests and the tools that still call them there).

One block per screen, each under a `# ---- NAME` rule.
"""
import textwrap
import time

import ansi
import ui
from ui import Bar, Col, Finding, Head, Line, More, Msg, Pane, Row, Series, Span, Table, hclean, hcount, hnum, human, plural


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
