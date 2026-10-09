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
VIEWS = ("overview", "cpu", "health", "map", "ai", "telegram", "summary")  # summary: the top bar and the key figures every page shows
# What only the console reads: its widths, gaps, heights, indents and clipping (htmlview.py, the web's drawing, reads none of these; a column's
# wprio and a table's titled are the web's too, so they stay).
CONSOLE_ONLY = frozenset(("w", "gap", "clip", "pad_in", "indent", "head_line", "fill", "fit", "lw", "cw", "per", "h", "once"))
EVENT_ID = re.compile(r"[0-9a-f]{16}")  # a rev: what a client may send back as Last-Event-ID
STREAM_VIEWS_MAX = 3  # views one stream may carry (a page: its view and the summary)
JSON_TYPE = "application/json; charset=utf-8"
STREAM_TYPE = "text/event-stream; charset=utf-8"

_FIELDS = {}  # class -> the names of its fields, inherited ones first, console-only ones left out


class Doc(bytes):
    """A document as JSON (UTF-8), with its rev."""
    rev = ""


def fields(cls):
    """The fields of a component class that the API carries: every __slots__ of the class and its bases (a Notice has Msg's level and text),
    in order, less CONSOLE_ONLY."""
    names = _FIELDS.get(cls)
    if names is None:
        seen = []
        for k in reversed(cls.__mro__):
            seen += [n for n in getattr(k, "__slots__", ()) if n not in seen and n not in CONSOLE_ONLY]
        names = _FIELDS[cls] = tuple(seen)
    return names


def data(x):
    """x as plain JSON values. A component is {"type": its class name, field: value, ...}; a Bar adds its state (computed from its value and
    thresholds), Raw lines lose their colours, the children of Cols lose the console's widths. In a list, Only("console") is left out and Only("web") and Cap give their children in place, as
    the web draws them. A float that is not a number is null; a value of another kind is its text."""
    if isinstance(x, ui.Raw):
        return {"type": "Raw", "lines": [ui.safe(ansi.ANSI.sub("", str(line))) for line in x.lines]}
    if isinstance(x, ui.Component):
        out = {"type": type(x).__name__}
        for n in fields(type(x)):
            out[n] = data(getattr(x, n))
        if isinstance(x, ui.Bar):
            out["state"] = x.state
        if isinstance(x, ui.Cols):  # [(node, the console's width)]: the web lays them out in a grid, whatever the width
            out["children"] = list(_items(n for n, _w in x.children))
        return out
    if isinstance(x, (list, tuple)):
        return list(_items(x))
    if isinstance(x, dict):
        return {str(k): data(v) for k, v in x.items()}
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if x is None or isinstance(x, (bool, int, str)):
        return x
    return str(x)


def _items(xs):
    for x in xs:
        if isinstance(x, ui.Only):
            if x.surface == "web":
                for y in _items(x.children):
                    yield y
        elif isinstance(x, ui.Cap):
            for y in _items(x.children):
                yield y
        else:
            yield data(x)


def problems(pb):
    """The problems of a frame (a problems.ProblemList, or a plain list of (severity, text)): each with its id, state, severity and text, the cards
    it belongs to and, when the list has them, the catalog's title, why, fix and the command that accepts it; how many were accepted as known,
    and which."""
    pids = getattr(pb, "pids", None) or [None] * len(pb)
    info = getattr(pb, "info", None) or [None] * len(pb)
    items = []
    for (sev, text), pid, more in zip(pb, pids, info):
        item = {"id": pid, "state": cards.SEV_STATE.get(sev, "warn"), "severity": sev, "text": ui.safe(text),
                "cards": list(cards.PROBLEM_CARDS.get(pid, ())) if pid else []}
        if more:
            item.update(zip(("title", "why", "fix", "accept"), (ui.safe(v) if v is not None else None for v in more)))
        items.append(item)
    return {"items": items, "accepted": int(getattr(pb, "accepted", 0) or 0), "known": data(getattr(pb, "known", None) or [])}


def rev(body):
    """The hash of a view's fields (already plain data): 16 hex digits."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:16]


def dump(obj):
    """obj as compact JSON bytes, on one line (a stream's data line): no NaN, no raw control character."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def document(view, body, at):
    """The document of `view`: its fields (body: a dict of components and values) as plain data, with the API version, the rev and the time
    the data was read."""
    body = data(body)
    tag = rev(body)
    doc = Doc(dump(dict({"api": VERSION, "view": view, "rev": tag, "at": round(float(at), 3)}, **body)))
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
