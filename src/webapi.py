"""The data API of the web view (docs/WEB.md, "The data API"): what each screen shows, as JSON, and the stream of its changes as Server-Sent
Events. Pure functions: web.py checks the request, reads the machine and builds the components (the very ones its pages draw), and these turn
them into documents and events. Nothing here reads a file, the clock or the host.

A document is {"api": VERSION, "view": name, "rev": ..., "at": seconds since the epoch, ...the view's own fields}. rev is a hash of the view's
fields (not of "at"): the same data, the same rev, so it is the ETag of a GET and the id of an event, and a stream sends a view again only when
its rev changes. A component is {"type": its class name, field: value, ...}: the classes and fields of src/ui.py, less the widths, gaps,
heights and indents only the console draws (CONSOLE_ONLY)."""

import hashlib
import json
import math
import re

import ansi
import cards
import ui

VERSION = 1  # /api/v1: a field that changes meaning, or goes, is a new version; a new field is not
VIEWS = ("overview", "cpu", "health", "map", "ai", "events", "telegram", "summary")  # summary: the top bar and the key figures every page shows
# What only the console reads: its widths, gaps, heights, indents and clipping (htmlview.py, the web's drawing, reads none of these; a column's
# wprio and a table's titled are the web's too, so they stay).
CONSOLE_ONLY = frozenset(("w", "gap", "clip", "pad_in", "indent", "head_line", "fill", "fit", "lw", "cw", "per", "h", "once"))
EVENT_ID = re.compile(r"[0-9a-f]{16}")  # a rev: what a client may send back as Last-Event-ID
STREAM_VIEWS_MAX = 3  # views one stream may carry (a page: its view and the summary)
JSON_TYPE = "application/json; charset=utf-8"
STREAM_TYPE = "text/event-stream; charset=utf-8"

_FIELDS = {}  # (class, console) -> the names of its fields, inherited ones first, console-only ones left out (unless console)


class Doc(bytes):
    """A document as JSON (UTF-8), with its rev."""
    rev = ""


def fields(cls, console=False):
    """The fields of a component class that the API carries: every __slots__ of the class and its bases (a Notice has Msg's level and text),
    in order, less CONSOLE_ONLY (console=True keeps them: see data())."""
    names = _FIELDS.get((cls, console))
    if names is None:
        seen = []
        for k in reversed(cls.__mro__):
            seen += [n for n in getattr(k, "__slots__", ()) if n not in seen and (console or n not in CONSOLE_ONLY)]
        names = _FIELDS[cls, console] = tuple(seen)
    return names


def data(x, console=False):
    """x as plain JSON values. console=True is the console flavour (src/remotedoc.py reads both): nothing is left out or expanded (the
    widths, gaps, heights and indents stay, Cols carry their widths in "widths", Only and Cap are components); the documents the web API
    serves are never this. Without it: A component is {"type": its class name, field: value, ...}; a Bar adds its state (computed from its value and
    thresholds), Raw lines lose their colours, the children of Cols lose the console's widths. In a list, Only("console") is left out and Only("web") and Cap give their children in place, as
    the web draws them. A float that is not a number is null; a value of another kind is its text."""
    if isinstance(x, ui.Raw):
        return {"type": "Raw", "lines": [ui.safe(ansi.ANSI.sub("", str(line))) for line in x.lines]}
    if isinstance(x, ui.Component):
        out = {"type": type(x).__name__}
        for n in fields(type(x), console):
            out[n] = data(getattr(x, n), console)
        if isinstance(x, ui.Bar):
            out["state"] = x.state
        if isinstance(x, ui.Cols):  # [(node, the console's width)]: the web lays them out in a grid, whatever the width
            out["children"] = list(_items((n for n, _w in x.children), console))
            if console:
                out["widths"] = [w for _n, w in x.children]
        return out
    if isinstance(x, (list, tuple)):
        return list(_items(x, console))
    if isinstance(x, dict):
        return {str(k): data(v, console) for k, v in x.items()}
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if x is None or isinstance(x, (bool, int, str)):
        return x
    return str(x)


def _items(xs, console=False):
    for x in xs:
        if console:
            yield data(x, True)
        elif isinstance(x, ui.Only):
            if x.surface == "web":
                for y in _items(x.children):
                    yield y
        elif isinstance(x, ui.Cap):
            for y in _items(x.children):
                yield y
        else:
            yield data(x)


def problems(pb, run=None, note=None):
    """The problems of a frame (a problems.ProblemList, or a plain list of (severity, text)): each with its id, state, severity and text, the cards
    it belongs to and, when the list has them, the catalog's title, why, fix and the command that accepts it; how many were accepted as known,
    and which, and how many were muted ([alerts] mute), and which. run(pid) -> {"label", "id"} or None (the Execute button, fixactions.item):
    every item has a `run` field, null when there is none; note: what the last Execute did, {ok, text}, or null."""
    pids = getattr(pb, "pids", None) or [None] * len(pb)
    info = getattr(pb, "info", None) or [None] * len(pb)
    items = []
    for (sev, text), pid, more in zip(pb, pids, info):
        item = {"id": pid, "state": cards.SEV_STATE.get(sev, "warn"), "severity": sev, "text": ui.safe(text),
                "cards": list(cards.PROBLEM_CARDS.get(pid, ())) if pid else [], "run": run(pid) if run and pid else None}
        if more:
            item.update(zip(("title", "why", "fix", "accept"), (ui.safe(v) if v is not None else None for v in more)))
        items.append(item)
    return {"items": items, "accepted": int(getattr(pb, "accepted", 0) or 0), "known": data(getattr(pb, "known", None) or []),
            "muted": int(getattr(pb, "muted", 0) or 0), "muted_known": data(getattr(pb, "muted_known", None) or []), "fix_note": note}


def counts(pb):
    """How many ATTENTION problems of a frame (the ones shown: accepted and muted are not in it) there are by severity word, as the status pill
    judges them: err = severity 2 or 3, warn = 1; unknown = a severity that is none of those (never counted as fine). The three add up to
    the summary's `problems`."""
    out = {"err": 0, "warn": 0, "unknown": 0}
    for sev, _text in pb or []:
        out["err" if sev in (2, 3) else "warn" if sev == 1 else "unknown"] += 1
    return out


def worst(pb, n=3):
    """The catalog titles (never the problem's own text, which can carry names and addresses) of up to n problems, the most severe first.
    A problem the catalog has no title for is left out."""
    info = getattr(pb, "info", None) or []
    rows = [(-(sev if isinstance(sev, int) else 0), i, more[0]) for i, ((sev, _t), more) in enumerate(zip(pb or [], info)) if more and more[0]]
    return [ui.safe(t) for _s, _i, t in sorted(rows)[:n]]


def rev(body):
    """The hash of a view's fields (already plain data): 16 hex digits."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:16]


def dump(obj):
    """obj as compact JSON bytes, on one line (a stream's data line): no NaN, no raw control character."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def document(view, body, at, console=False):
    """The document of `view`: its fields (body: a dict of components and values) as plain data, with the API version, the rev and the time
    the data was read. console: the console flavour (data()), marked "flavour": "console"; only the tests and src/remotedoc.py use it."""
    body = data(body, console)
    tag = rev(body)
    head = {"api": VERSION, "view": view, "rev": tag, "at": round(float(at), 3)}
    if console:
        head["flavour"] = "console"
    doc = Doc(dump(dict(head, **body)))
    doc.rev = tag
    return doc


def event(doc):
    """One Server-Sent Event: the document as the data of a message whose id is its rev."""
    return b"id: " + doc.rev.encode() + b"\ndata: " + bytes(doc) + b"\n\n"


def comment(text):
    """A comment line of a stream (a keepalive: clients ignore it)."""
    return b": " + text.encode() + b"\n\n"


def retry(ms):
    """How long a client waits before it reconnects a stream that ended."""
    return b"retry: %d\n\n" % int(ms)
