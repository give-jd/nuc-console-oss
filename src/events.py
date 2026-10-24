"""nuc-console events: what happened on this machine and when, newest first (the `events` table of history.db), for the EVENTS screen of
the console, the web page `?view=events`, the data API `/api/v1/events` and the CSV / JSON downloads.

Reads the history read-only (history.open_ro), like the Health screen; it records nothing (the collector does: history.py, collector.py).
Everything shown is a name or a fixed word the collector chose (never a log line); it still goes through ui.safe when a component is built.

  - parse_query(): the strict validation of the parameters of the API and the downloads (limit, since, age, kind): a bad one is a QueryError;
  - load(): the rows, or why there are none (the Health feature is off, no history yet, the file could not be read);
  - csv_text(), json_rows(): the downloads (CSV cells that a spreadsheet could take for a formula are quoted);
  - EventsView, events_key(), events_sync(): what the console screen keeps while it is open (what each key does is ui.KEYMAP's);
  - events_nodes() (the web: everything, nothing cut) and events_lines() (the console: what fits w by h): the screen as components.
"""
import csv
import io
import re
import sys
import textwrap
import time

import ansi
import history
import ui
from ui import Col, Group, Line, Msg, Pane, Row, Seg, Span, Table, Title

AGES = (("1h", 3600), ("24h", 86400), ("7d", 7 * 86400))   # the periods: keys h d w (the web: age=1h|24h|7d)
AGE_SECONDS = dict(AGES)
AGE_KEYS = ("h", "d", "w")                                   # as ui.KEYMAP says them (a test keeps the two equal)
DEFAULT_AGE = "24h"
LIMIT_DEFAULT, LIMIT_MAX = 100, 500                          # rows of one answer: the default, and the most any request can have
KINDS = history.KINDS
KIND_CYCLE = ("",) + ("problem",) + tuple(k for k in KINDS if k != "problem")  # the console's f key: all, then one kind at a time
LABEL = {"oom": "out of memory", "crash": "crash", "hang": "hang", "unexpected_shutdown": "unexpected off", "hw_error": "hardware",
         "service_failed": "service failed", "restart": "restart", "exit_error": "exit error", "throttle": "throttle", "disk_low": "disk low",
         "login_fail": "login failed", "problem": "problem"}
TONE = {"oom": "err", "unexpected_shutdown": "err", "hw_error": "err", "crash": "warn", "hang": "warn", "service_failed": "warn", "restart": "warn",
        "exit_error": "warn"}                                # the others are muted; a problem's tone is its own (row_tone)
GLYPH = {"err": "✖", "warn": "!", "ok": "✔", "muted": "·"}  # a symbol besides the colour
UNREADABLE = "the history could not be read (see the service log)"
NO_HISTORY = "no history yet: the collector starts recording when [features] health is on; events appear as they happen"
OFF = "the history is off ([features] health = no in config.ini): events are recorded only while the Health feature is on"
EMPTY = "no events in this period (the history keeps %d days)" % history.RETENTION_DAYS["events"]
FIX = {"problem": "nuc-console-problems says what it is and how to fix it (the web view: the ATTENTION card)"}
FIX_DEFAULT = "the HEALTH screen turns repeated events into findings, with what to do about them"
IDLE_S = 600  # the screen left alone this long gives the monitor back to the rotation (nobody may be at the keyboard)
MAX_SINCE = 4102444800  # 2100-01-01: a larger number is not a time anybody means


class QueryError(ValueError):
    """A parameter of a request that is not valid; the message is safe to send back (it names the parameter, never echoes the value)."""


# ---- the request -----------------------------------------------------------------------------------------------------------------

def _one(q, name):
    """The one value of a parameter, None when it is absent; a repeated one is an error (two answers to the same question)."""
    vals = q.get(name)
    if vals is None:
        return None
    if isinstance(vals, str):
        vals = [vals]
    if len(vals) != 1:
        raise QueryError("%s: give it once" % name)
    return vals[0]


def parse_query(q):
    """q: {name: [values]} as urllib.parse.parse_qs gives it -> {'limit', 'since' (epoch seconds, 0 = everything kept), 'kind' ('' = all),
    'age' ('' or one of AGES: since is then taken from the clock)}. With neither since nor age the period is the last 24 hours (the page's, the
    app's and the API's alike); since=0 is everything kept. Only these four are read; each is checked as strictly as the next:
    limit 1..LIMIT_MAX in digits, since in digits up to MAX_SINCE, age one of 1h 24h 7d (not together with since), kind one of history.KINDS.
    Anything else is a QueryError (an empty value included): the API never guesses what was meant."""
    out = {"limit": LIMIT_DEFAULT, "since": 0, "kind": "", "age": "" if "since" in q else DEFAULT_AGE}
    raw = _one(q, "limit")
    if raw is not None:
        if not re.fullmatch(r"[0-9]{1,4}", raw) or not 1 <= int(raw) <= LIMIT_MAX:
            raise QueryError("limit: a number from 1 to %d" % LIMIT_MAX)
        out["limit"] = int(raw)
    raw = _one(q, "since")
    if raw is not None:
        if not re.fullmatch(r"[0-9]{1,10}", raw) or int(raw) > MAX_SINCE:
            raise QueryError("since: seconds since the epoch, as digits")
        out["since"] = int(raw)
    raw = _one(q, "age")
    if raw is not None:
        if raw not in AGE_SECONDS:
            raise QueryError("age: one of " + ", ".join(a for a, _s in AGES))
        if "since" in q:
            raise QueryError("age and since: give one of them")
        out["age"] = raw
    raw = _one(q, "kind")
    if raw is not None:
        if raw not in KINDS:
            raise QueryError("kind: one of " + ", ".join(KINDS))
        out["kind"] = raw
    return out


def since_of(params, now):
    """The first second the answer covers: since, else now less the age, else 0."""
    return params.get("since") or (int(now) - AGE_SECONDS[params["age"]] if params.get("age") else 0)


# ---- the data --------------------------------------------------------------------------------------------------------------------

def select(conn, kind, since, limit):
    """(rows, total): the newest `limit` events from `since` on (of one kind, or of all), and how many there are in all."""
    where, args = "WHERE ts >= ?" + (" AND kind = ?" if kind else ""), [int(since)] + ([kind] if kind else [])
    rows = conn.execute("SELECT id, ts, kind, subject, detail, source, COALESCE(n, 1) FROM events %s ORDER BY ts DESC, id DESC LIMIT ?" % where,
                        args + [int(limit)]).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM events " + where, args).fetchone()[0]
    return [{"id": i, "ts": int(ts), "kind": history.clean(k, 24), "subject": history.clean(s, history.SUBJECT_MAX),
             "detail": history.clean(d, history.DETAIL_MAX), "source": history.clean(src, 16), "n": int(n)} for i, ts, k, s, d, src, n in rows], int(total)


def answer(state, params, now, rows=(), total=0, msg=""):
    """The dict every surface reads: state ('ok' | 'off' | 'none' | 'error'), msg (why there are no rows), the parameters it answers, rows."""
    return {"state": state, "msg": msg, "kind": params["kind"], "age": params["age"], "since": since_of(params, now), "limit": params["limit"],
            "rows": list(rows), "total": total, "at": now}


def load(params, now=None, enabled=True, path=None):
    """The events for params (parse_query's): answer(). enabled False: the Health feature is off, so is the history (nothing is read). A missing
    history is 'none', an unreadable one 'error' (with the reason): never an empty list that looks like a quiet machine."""
    now = time.time() if now is None else now
    if not enabled:
        return answer("off", params, now, msg=OFF)
    try:
        conn = history.open_ro(path)
        if conn is None:
            return answer("none", params, now, msg=NO_HISTORY)
        try:
            rows, total = select(conn, params["kind"], since_of(params, now), params["limit"])
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - say so, and keep the dashboard
        print("nuc-console events: the history could not be read:", repr(e)[:200], file=sys.stderr)  # the reason goes to the journal, not to a page
        return answer("error", params, now, msg=UNREADABLE)
    return answer("ok", params, now, rows, total)


# ---- the downloads ---------------------------------------------------------------------------------------------------------------

def iso(ts):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def csv_cell(v):
    """A CSV cell that no spreadsheet takes for a formula: a text that starts with = + - @ (or a tab or a carriage return, which some
    programs skip before reading it) gets a quote in front of it. Numbers are written as they are."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    s = ui.safe(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_text(rows):
    """The rows as CSV (RFC 4180: CRLF lines, quotes where needed), with a header line."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["time_utc", "ts", "kind", "subject", "detail", "count", "source"])
    for r in rows:
        w.writerow([csv_cell(iso(r["ts"])), r["ts"], csv_cell(r["kind"]), csv_cell(r["subject"]), csv_cell(r["detail"]), r["n"], csv_cell(r["source"])])
    return buf.getvalue()


def json_rows(data):
    """The answer as the plain document the API and the JSON download carry: the filters it answers and the events, newest first."""
    return {"state": data["state"], "msg": data["msg"], "kind": data["kind"], "age": data["age"], "since": data["since"], "limit": data["limit"],
            "total": data["total"], "count": len(data["rows"]), "events": [dict(r, time=iso(r["ts"])) for r in data["rows"]]}


# ---- the rows as cells -----------------------------------------------------------------------------------------------------------

def kind_label(kind):
    return LABEL.get(kind, ui.safe(kind))


def row_tone(r):
    """The tone of an event: a problem is as bad as its severity while it is open and fine once it cleared; the other kinds have theirs."""
    if r["kind"] == "problem":
        d = r["detail"]
        return "ok" if d.startswith("cleared") else "warn" if "(warning)" in d or d == "flapping" else "err"
    return TONE.get(r["kind"], "muted")


def what(r):
    return r["detail"] + (" ×%d" % r["n"] if r["n"] > 1 else "")


def kind_cell(r, width=None):
    tone = row_tone(r)
    text = GLYPH[tone] + " " + kind_label(r["kind"])
    return Span(text.ljust(width) if width else text, tone, full=text)


def utc(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def local(ts, fmt="%m-%d %H:%M:%S"):
    try:
        return time.strftime(fmt, time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


# ---- the screen ------------------------------------------------------------------------------------------------------------------

class EventsView(object):
    """The interactive EVENTS screen: the period and the kind shown, the selected event (its id survives refreshes; its index is where the
    cursor stays when it is gone), the scroll position, the details pane, when it was opened and last touched."""

    def __init__(self, age=DEFAULT_AGE, kind="", now=None):
        self.age, self.kind, self.cur, self.idx, self.top, self.details, self.rows = age, kind, None, 0, 0, False, 10
        self.opened = self.touched = now or time.time()

    def params(self):
        """The request this view makes: the newest LIMIT_MAX events of the period (and of the kind)."""
        return {"limit": LIMIT_MAX, "since": 0, "kind": self.kind, "age": self.age}


def events_sync(ev, rows):
    """The cursor back on its event: by id, else the same index (clamped). Returns the index."""
    i = next((j for j, r in enumerate(rows) if r["id"] == ev.cur), None) if ev.cur is not None else None
    ev.idx = i if i is not None else max(0, min(ev.idx, len(rows) - 1))
    ev.cur = rows[ev.idx]["id"] if rows else None
    return ev.idx


def events_key(ev, key, rows):
    """One key on the EVENTS screen (what each key does: ui.KEYMAP, scope events). Returns 'back' (Esc, or q when no details pane is open),
    'filter' (another period or kind: the rows are asked for again) or '' (only the cursor or the details pane changed)."""
    act = ui.action("events", key)
    if act == "back":
        if ev.details:
            ev.details = False
            return ""
        return "back"
    if act == "age":
        old, ev.age = ev.age, AGES[AGE_KEYS.index(key)][0]
        return "filter" if old != ev.age else ""
    if act == "kind":
        i = KIND_CYCLE.index(ev.kind) if ev.kind in KIND_CYCLE else 0
        ev.kind = KIND_CYCLE[(i + 1) % len(KIND_CYCLE)]
        return "filter"
    if act == "details":
        ev.details = not ev.details
        return ""
    if not rows or act not in ("move", "page"):
        return ""
    i, page = events_sync(ev, rows), max(1, ev.rows - 1)
    i = {"up": i - 1, "k": i - 1, "down": i + 1, "j": i + 1, "pgup": i - page, "pgdn": i + page, "home": 0, "end": len(rows) - 1}.get(key, i)
    ev.idx = max(0, min(i, len(rows) - 1))
    ev.cur = rows[ev.idx]["id"]
    return ""


def _msg(level, text, w):
    """A message that wraps at the console's width (the explanations are long); w None: the web, one line."""
    if w is None:
        return [Msg(level, text)]
    rows = textwrap.wrap(text, max(10, w - 5)) or [""]
    return [Msg(level, rows[0])] + [Line([Span("     " + x)]) for x in rows[1:]]


def period_text(data):
    age = data["age"]
    if age:
        return "last " + {"1h": "hour", "24h": "24 hours", "7d": "7 days"}[age]
    return "since " + utc(data["since"]) + " UTC" if data["since"] else "everything kept"


def next_kind_key(kind):
    """The kind whose option carries the key f on the web: the one after the chosen, as the console's f moves to it."""
    return KIND_CYCLE[(KIND_CYCLE.index(kind) + 1) % len(KIND_CYCLE)] if kind in KIND_CYCLE else ""


def events_title(data, age, kind, href=None, selector=True):
    """'-- EVENTS  last 24 hours · 12 events · crash ------ h:1h d:24h w:7d': the period (a Seg), how many there are, the kind filter. The web also
    gets the kinds as a second choice and the downloads as a third (href(age=, kind=, format=) -> the address; None: the console)."""
    n, total = len(data["rows"]), data["total"]
    parts = [Span(period_text(data), "muted")]
    if data["state"] == "ok":
        parts.append(Span("%d events" % total if n == total else "newest %d of %d" % (n, total), "muted"))
    if kind:
        parts.append(Span("kind: " + kind_label(kind), "muted"))
    seg = Seg("period", [(lab, key, lab == age, href(age=lab) if href else None) for (lab, _s), key in zip(AGES, AGE_KEYS)]) if selector else None
    segs = []
    if href:
        nxt = next_kind_key(kind)
        segs.append(Seg("kind", [(("all" if not k else kind_label(k)), "f" if k == nxt else "", k == kind, href(kind=k)) for k in KIND_CYCLE]))
        if data["state"] == "ok":
            segs.append(Seg("download", [("download CSV", "", False, href(format="csv")), ("download JSON", "", False, href(format="json"))]))
    return Title("EVENTS", parts, seg, segs=segs)


def _table(rows, w, cur=None):
    """The events as a Table. w None: the web (the full text of every cell, UTC times; a phone stacks the cells, see webcss). A console w: local
    times, the kind and the subject padded to their columns, what happened cut to the room that is left, the cursor's row (index cur) solid."""
    if w is None:
        cols = [Col("when", "When (UTC)", num=True), Col("kind", "Kind"), Col("subject", "Subject"), Col("what", "What happened")]
        body = [Row([Span(utc(r["ts"])), kind_cell(r), Span(r["subject"]), Span(what(r))]) for r in rows]
        return Table(cols, body)
    sw = max(8, min(24, max([len(r["subject"]) for r in rows] + [7]), (w - 40) // 2))
    kw = max(len(GLYPH["ok"] + " " + kind_label(k)) for k in {r["kind"] for r in rows}) if rows else 8
    room = max(0, w - 1 - 14 - 1 - kw - 1 - sw - 1)
    cols = [Col("when", "WHEN", w=14, gap=1), Col("kind", "KIND", w=kw, gap=1), Col("subject", "SUBJECT", w=sw, gap=1), Col("what", "WHAT HAPPENED")]
    body = []
    for j, r in enumerate(rows):
        subj, text = r["subject"], what(r)
        body.append(Row([Span(local(r["ts"])), kind_cell(r, kw), Span(subj if len(subj) <= sw else subj[:sw - 1] + "…", full=subj),
                         Span(text if len(text) <= room else text[:max(room - 1, 0)] + "…", "muted", full=text)], "sel" if j == cur else None))
    return Table(cols, body, head=True, indent=1, fill=True, head_tone="accent_strong", solid=w, clip=w)


def _empty(data, w):
    if data["state"] != "ok":
        return _msg("err" if data["state"] == "error" else "info", data["msg"], w)
    return _msg("info", EMPTY, w)


def events_nodes(data, age, kind, href):
    """The whole screen for the web, nothing cut: the title with the periods, kinds and downloads as links, then the table (or why there is none)."""
    out = [events_title(data, age, kind, href)]
    if data["state"] != "ok" or not data["rows"]:
        return out + _empty(data, None)
    return out + [_table(data["rows"], None)]


def details_pane(r, h):
    """The details of an event: what it says in full, when (local and UTC), how many times, who saw it."""
    tone = row_tone(r)
    level = {"err": "err", "warn": "warn"}.get(tone, "info")
    facts = [("local", local(r["ts"], "%Y-%m-%d %H:%M:%S")), ("utc", utc(r["ts"])), ("count", str(r["n"])), ("source", r["source"] or "?")]
    return Pane(level, kind_label(r["kind"]) + " · " + r["subject"], r["detail"] or "?", facts, FIX.get(r["kind"], FIX_DEFAULT), h)


def events_lines(data, ev, w, h):
    """The EVENTS screen's body as console lines: at most h, none wider than w. Title, then the rows in view (the cursor's in reverse), with the
    details of the selected one under them when Enter opened them."""
    rows = data["rows"]
    out = [events_title(data, ev.age, ev.kind)]
    if data["state"] != "ok" or not rows:
        ev.rows = max(1, h - 1)
        return ansi.render(Group(out + _empty(data, w)), w)[0][:h]
    events_sync(ev, rows)
    pane = ev.details
    pane_h = min(9, max(5, h // 3)) if pane and h >= 14 else 0
    room = max(1, h - 1 - 1 - pane_h)  # the title, the column heads, the pane
    ev.rows = room
    ev.top = ansi.scroll(ev.top, ev.idx, len(rows), room)
    shown = rows[ev.top:ev.top + room]
    out.append(_table(shown, w, ev.idx - ev.top))
    if pane_h:
        out.append(details_pane(rows[ev.idx], pane_h))
    return [ansi.clip(x, w) for x in ansi.render(Group(out), w)[0][:h]]


def footer_pos(ev, n):
    return "event %d/%d" % (ev.idx + 1, n) if n else "no events"
