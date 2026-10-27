"""A remote host's view documents, read back into the console's own components (stdlib only, Python 3.8+).

webapi.py turns the components of a screen into JSON (`webapi.data`); the poller keeps what a peer served (src/peers.py: the documents of
overview, cpu, health, map and events as opaque, size-checked JSON). This is the inverse: JSON -> the very ui.py objects that ansi.render draws,
so that a remote host's screen is drawn by the same code as a local one. Nothing here draws, reads a file, opens a socket or knows the clock.

THE DOCUMENT IS FOREIGN DATA. It comes from another machine, through a cache another account wrote, and is read inside the console's process
(the account of the terminal). The rules, all of them checked by tests/test_remotedoc.py:
  - ALLOWLIST. A node is built only when its "type" is a key of SPECS, a fixed table of classes; every field has a fixed keyword and a
    parser of its own. No name from the data is ever used as an attribute, a function, a module or a class (no getattr, eval, import, pickle),
    and the data decides nothing but values.
  - A node that is not allowed, not well typed or refused by its constructor is replaced by a visible placeholder (a Msg 'unsupported ...',
    or in a line a '?' span). Nothing raises out of decode(): a hostile document costs the reader one line.
  - CAPS. Depth (MAX_DEPTH levels of components), nodes and list items together (MAX_NODES), characters of text together (MAX_CHARS), bytes of
    a document (MAX_BYTES), one list (MAX_LIST), one text (its field's cap). Past a cap the rest is dropped and Document.cut says so.
  - TEXT. Every string goes through clean(): no control character (ESC, CSI, C1, DEL), no bidi or other format character, no combining mark,
    no character wider or narrower than one column (the console cuts lines by counting characters), no lone surrogate (it cannot be written).
    Raw lines lose their colours first. A link (href, close_href ...) is never read: the console follows none.
  - NUMBERS. A bool is not a number; NaN and infinity are not numbers; an out-of-range number is clamped to the range of its field.
  - COMPATIBILITY. A field the table does not know is ignored; a field that is missing has the constructor's default; an unknown tone, state or
    level becomes the neutral one (never a green); a document whose `api` is greater than API is not read at all (an 'incompatible' node).

Two flavours of document. The one a peer's web API serves lacks what only the console uses (widths, gaps, heights, indents and the Only and Cap
wrappers: webapi.CONSOLE_ONLY), so some things are drawn from defaults: see "Remote documents" in docs/DESIGN.md for exactly what. The console
flavour (webapi.data(x, console=True), "flavour": "console") carries all of it and is read back to objects that are equal to the originals.
"""
import json
import math
import re
import unicodedata
from functools import lru_cache

import ansi
import ui

API = 1  # the web API version this module reads (webapi.VERSION; a test keeps the two equal)
VIEWS = ("overview", "cpu", "health", "map", "events")  # never ai, telegram, settings, summary: a remote host's read-only screens
MAX_BYTES = 2 * 1024 * 1024  # a document as bytes (decode_bytes)
MAX_NODES = 20000  # components and list items of one document together
MAX_CHARS = 1500000  # characters of text of one document together
MAX_DEPTH = 32  # levels of components inside one another
MAX_LIST = 5000  # items of one list
MAX_LINES = 20000  # lines of a drawn document (render.remote_body)


class _Bad(Exception):
    """A value that is not what its field takes: the node that holds it is replaced."""


# ---- text ----------------------------------------------------------------------------------------------------------------------------
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


_BLANKS = frozenset("\u034f\u115f\u1160\u17b4\u17b5\u180e\u2800\u3164\uffa0")  # invisible fillers that are letters or marks to Unicode, not format characters


@lru_cache(maxsize=8192)
def _char(ch):
    if ch in _BLANKS:
        return "?"
    cat = unicodedata.category(ch)
    if cat == "Zs":
        return " " if unicodedata.east_asian_width(ch) not in ("W", "F") else "?"
    if cat[0] in "CZM" or unicodedata.east_asian_width(ch) in ("W", "F"):
        return "?"
    return ch


def clean(s):
    """s as text that is safe to write to a terminal, one column per character: control characters, format characters (bidi), combining
    marks, wide characters and lone surrogates become '?'."""
    if s.isascii():
        return _CTRL.sub("?", s)
    return "".join(map(_char, unicodedata.normalize("NFC", s)))


# ---- the parsers: (ctx, value, depth) -> the value of a field, or _Bad -------------------------------------------------------------------
class _Ctx(object):
    __slots__ = ("nodes", "chars", "cut", "lossless", "rejected")

    def __init__(self, lossless):
        self.nodes, self.chars, self.cut, self.lossless, self.rejected = MAX_NODES, MAX_CHARS, False, lossless, 0


def text(cap=200, none="", strip_ansi=False):
    """A string of at most `cap` characters (None: `none`)."""
    def parse(c, v, d):
        if v is None:
            return none
        if not isinstance(v, str):
            raise _Bad
        v = v[:cap]
        if strip_ansi:
            v = ansi.ANSI.sub("", v)
        c.chars -= len(v)
        if c.chars < 0:
            c.cut = True
            raise _Bad
        return clean(v)
    return parse


def opt_text(cap=200):
    """A string or None."""
    return text(cap, None)


def integer(lo, hi, default=0):
    """An int in lo..hi (clamped); None: default. A bool, a string or a fraction is not one."""
    def parse(c, v, d):
        if v is None:
            return default
        if isinstance(v, float) and math.isfinite(v) and v == int(v):
            v = int(v)
        if isinstance(v, bool) or not isinstance(v, int):
            raise _Bad
        return min(max(v, lo), hi)
    return parse


def real(lo, hi, default=None):
    """A finite number in lo..hi (clamped); None, NaN and infinity: default."""
    def parse(c, v, d):
        if v is None:
            return default
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise _Bad
        if isinstance(v, float) and not math.isfinite(v):
            return default
        return float(min(max(v, lo), hi))
    return parse


def boolean(c, v, d):
    if v is None:
        return False
    if not isinstance(v, bool):
        raise _Bad
    return v


def enum(values, default):
    """One of `values`; anything else (a word a newer version has) is `default`."""
    def parse(c, v, d):
        return v if isinstance(v, str) and v in values else default
    return parse


def tone(c, v, d):
    return v if isinstance(v, str) and v in ui.TOKENS else None


def state(c, v, d):
    return v if isinstance(v, str) and v in ui.STATES else "unknown"


def number_or_none(c, v, d):
    """A number for a series (a gap is None); not a number: None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return float(min(max(v, -1e18), 1e18))


def _placeholder(v):
    t = v.get("type") if isinstance(v, dict) else None
    return ui.Msg("warn", "unsupported element from the remote host" + (": " + clean(t[:24]) if isinstance(t, str) else ""))


def _placeholder_inline(v):
    return ui.Span("?", "unknown")


def seq(elem, strict=False, drop=False, fallback=_placeholder, cap=MAX_LIST):
    """A list of what `elem` parses. An item that is not allowed is `fallback(item)` (strict: the whole list is refused; drop: left out).
    Past the budget of nodes the list ends, with one fallback to say it."""
    def parse(c, v, d):
        if v is None:
            return []
        if not isinstance(v, list):
            raise _Bad
        out = []
        for x in v[:cap]:
            if c.nodes <= 0:
                c.cut = True
                if not (strict or drop):
                    out.append(fallback(None))
                break
            c.nodes -= 1
            try:
                out.append(elem(c, x, d))
            except _Bad:
                c.rejected += 1
                if strict:
                    raise
                if not drop:
                    out.append(fallback(x))
        if len(v) > cap:
            c.cut = True
        return out
    return parse


def pair(first, second):
    def parse(c, v, d):
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise _Bad
        return first(c, v[0], d), second(c, v[1], d)
    return parse


def triple(first, second, third):
    def parse(c, v, d):
        if not isinstance(v, (list, tuple)) or len(v) != 3:
            raise _Bad
        return first(c, v[0], d), second(c, v[1], d), third(c, v[2], d)
    return parse


def comp(c, v, d, kinds=None):
    """The component a node describes. _Bad: not a dict, no such type (or not among `kinds`), too deep, a field of the wrong type, or the
    constructor refused it."""
    if d > MAX_DEPTH or not isinstance(v, dict):
        raise _Bad
    t = v.get("type")
    spec = SPECS.get(t) if isinstance(t, str) else None
    if spec is None or (kinds is not None and t not in kinds):
        raise _Bad
    kw = {}
    for name, parse, default in spec[1]:
        kw[name] = parse(c, v[name], d + 1) if name in v else default
    try:
        return spec[2](c, spec[0], kw)
    except _Bad:
        raise
    except Exception:  # noqa: BLE001 - a constructor refused what the parser let through (or a recursion limit): the node is refused
        raise _Bad from None


def node(c, v, d):
    return comp(c, v, d)


def inline_of(kinds):
    """One inline component (None: nothing); one that is not allowed here is a '?'."""
    def parse(c, v, d):
        if v is None:
            return None
        try:
            return comp(c, v, d, kinds)
        except _Bad:
            c.rejected += 1
            return _placeholder_inline(v)
    return parse


def one_inline(c, v, d):
    return _inline(c, v, d) or ui.Span("")


def one_of(kinds):
    """One component of those classes (None: nothing); one that is not allowed makes the node refused."""
    def parse(c, v, d):
        return None if v is None else comp(c, v, d, kinds)
    return parse


INLINE = ("Span", "Line", "Bar", "Spark", "Meter", "Series")  # what a line, a cell or a value holds (ui._inline)
_inline = inline_of(INLINE)
nodes = seq(node)  # components of any kind: one that is not allowed is a visible 'unsupported' line
inlines = seq(one_inline, fallback=_placeholder_inline)
tx, tl = text(200), text(2000)  # a label / a text
short = text(60)
key = text(120)


# ---- the table of classes ------------------------------------------------------------------------------------------------------------
SPECS = {}  # type name -> (class, ((field, parser, default when absent) ...), build(ctx, class, {field: value}) -> object)


def _make(c, cls, kw):
    return cls(**kw)


def reg(cls, *fields, build=_make):
    SPECS[cls.__name__] = (cls, fields, build)


reg(ui.Span, ("text", tl, ""), ("tone", tone, None), ("bold", boolean, False), ("mono", boolean, False), ("full", opt_text(2000), None))
reg(ui.Line, ("spans", seq(lambda c, v, d: comp(c, v, d, INLINE), fallback=_placeholder_inline), ()),
    ("clip", integer(0, 1000, None), None))
reg(ui.Raw, ("lines", seq(text(1000, strip_ansi=True), strict=True, cap=1000), ()))
reg(ui.More, ("n", integer(0, 10 ** 9), 0), ("what", tx, "more"), ("indent", integer(0, 100, None), None))
reg(ui.Card, ("id", short, ""), ("title", tx, ""), ("note", tl, ""), ("state", state, "unknown"), ("body", nodes, ()),
    ("more", one_of(("More",)), None), ("size", integer(1, 4, 1), 1), ("truncated", boolean, False))
reg(ui.Kpi, ("id", short, ""), ("label", tx, ""), ("value", tx, ""), ("unit", short, ""), ("state", state, "unknown"), ("symbol", opt_text(8), None),
    ("hint", tl, ""), ("spark", one_of(("Spark",)), None), build=lambda c, cls, kw: cls(**dict(kw, spark=kw["spark"].values if kw["spark"] else None)))
reg(ui.Col, ("key", key, ""), ("label", tx, ""), ("align", enum(("l", "r", "c"), "l"), "l"), ("prio", integer(0, 99), 0), ("num", boolean, False),
    ("w", integer(0, 100, None), None), ("gap", integer(0, 8, 1), 1), ("clip", integer(0, 500, None), None), ("pad_in", boolean, False),
    ("hkey", short, ""), ("sort", enum(("asc", "desc", "mark"), None), None), ("wprio", integer(0, 99, None), None))
reg(ui.Row, ("cells", seq(one_inline, fallback=_placeholder_inline), ()), ("tone", tone, None), ("key", opt_text(120), None))
def _bar(c, cls, kw):
    """A remote bar is never greener than the local rules would draw its number: thresholds are at most 1 (a full bar is never 'ok'), and a
    peer's 'ok' tone (it colours the fill whatever the fraction is) counts only when the fraction itself is fine."""
    kw["warn"], kw["err"] = min(kw["warn"], 1.0), min(kw["err"], 1.0)
    bar = cls(**kw)
    if bar.tone in ("ok", "banner_ok") and bar.state != "ok":
        bar.tone = None
    return bar


reg(ui.Bar, ("frac", real(0.0, 1.0), None), ("value_text", tx, ""), ("warn", real(0.0, 1.0, 0.7), 0.7), ("err", real(0.0, 1.0, 0.9), 0.9),
    ("w", integer(1, 200, 10), 10), ("tone", tone, None), ("busy", boolean, False), build=_bar)
reg(ui.Spark, ("values", seq(lambda c, v, d: number_or_none(c, v, d), drop=True, cap=2000), ()), ("w", integer(1, 200, 8), 8),
    ("floor", real(0.0, 1e18, 1024), 1024))
reg(ui.Pill, ("text", tx, ""), ("state", state, "unknown"))
reg(ui.Msg, ("level", enum(ui.LEVELS, "info"), "info"), ("text", tl, ""))
reg(ui.Notice, ("level", enum(ui.LEVELS, "info"), "info"), ("text", tl, ""))
reg(ui.Wrap, ("items", inlines, ()), ("sep", text(20, "  ·  "), "  ·  "), ("max_lines", integer(1, 1000, None), None), ("indent", integer(0, 40, 1), 1),
    ("lead", inline_of(INLINE), None), ("flat", boolean, False))
reg(ui.Group, ("children", nodes, ()), ("title", tx, ""))
reg(ui.Tree, ("rows", seq(triple(integer(0, 64), one_inline, state), strict=True), ()))
reg(ui.Details, ("summary", one_inline, None), ("body", nodes, ()), ("open", boolean, False), ("brief", inline_of(INLINE), None))
reg(ui.RichMsg, ("level", enum(ui.LEVELS, "info"), "info"), ("rich", one_of(("Line",)), None),
    build=lambda c, cls, kw: cls(kw["level"], kw["rich"] if kw["rich"] is not None else ui.Line()))
reg(ui.Problem, ("level", enum(ui.LEVELS, "warn"), "warn"), ("text", tl, ""), ("id", short, ""), ("title", tl, ""), ("why", tl, ""),
    ("fix", tl, ""), ("accept", tl, ""))
reg(ui.Accepted, ("text", tl, ""), ("id", short, ""), ("reason", tl, ""), ("when", tx, ""), ("undo", tl, ""))
reg(ui.Hint, ("label", tx, ""), ("cmd", tl, ""))
reg(ui.Head, ("title", tx, ""), ("note", tl, ""))
reg(ui.Indent, ("children", nodes, ()), ("n", integer(0, 40, 2), 2))
reg(ui.Grid, ("items", inlines, ()), ("cw", integer(1, 100, 20), 20), ("per", integer(1, 50, 1), 1), ("gap", integer(0, 20), 0),
    ("lead", integer(0, 40), 0), ("fit", boolean, False))
reg(ui.Flow, ("items", inlines, ()), ("lead", inline_of(INLINE), None), ("indent", integer(2, 40, 8), 8), ("sep", text(20, "   "), "   "),
    ("cut", integer(0, 500, None), None))
reg(ui.Meter, ("parts", seq(pair(real(0.0, 1000.0, 0.0), enum(ui.METER_KINDS, "other")), strict=True, cap=50), ()), ("w", integer(1, 200, 10), 10))
reg(ui.Cap, ("children", nodes, ()), ("n", integer(0, 100000), 0), ("what", text(40, None), "lines"))
reg(ui.Only, ("surface", enum(("console", "web"), "web"), "web"), ("children", nodes, ()))
reg(ui.Tiles, ("items", seq(lambda c, v, d: comp(c, v, d, ("Kpi",)), strict=True, cap=200), ()))
reg(ui.Seg, ("label", tx, ""), ("options", seq(lambda c, v, d: _option(c, v, d), strict=True, cap=50), ()))
reg(ui.Legend, ("items", seq(triple(text(20), text(40), tone), strict=True, cap=50), ()))
reg(ui.Title, ("label", tx, ""), ("parts", seq(one_inline, fallback=_placeholder_inline), ()), ("seg", one_of(("Seg",)), None),
    ("legend", one_of(("Legend",)), None), ("segs", seq(lambda c, v, d: comp(c, v, d, ("Seg",)), strict=True, cap=10), ()))
reg(ui.Series, ("values", seq(number_or_none, strict=True, cap=2000), ()), ("w", integer(1, 200, 10), 10), ("tone", tone, "accent"),
    build=lambda c, cls, kw: cls(**dict(kw, tone=kw["tone"] or "accent")))
reg(ui.Pane, ("level", enum(("err", "warn", "info"), "info"), "info"), ("title", tx, ""), ("what", tl, ""),
    ("facts", seq(pair(tx, tl), strict=True, cap=200), ()), ("fix", tl, ""), ("h", integer(0, 1000, None), None),
    ("steps", seq(triple(tl, tl, boolean), strict=True, cap=20), ()))
reg(ui.Finding, ("id", short, ""), ("level", enum(("err", "warn", "info"), "info"), "info"), ("title", tx, ""), ("text", tl, ""),
    ("detail", one_of(("Pane",)), None), ("cursor", boolean, False), ("open", boolean, False))
reg(ui.Advice, ("head", tl, ""), ("paras", seq(seq(text(2000), strict=True, cap=200), strict=True, cap=200), ()),
    ("notes", seq(pair(short, tl), strict=True, cap=50), ()), ("kind", enum(("advice", "shared", "error", "none"), "advice"), "advice"),
    ("lines", seq(text(1000, strip_ansi=True), strict=True, cap=200), ()))
reg(ui.Branch, ("key", key, ""), ("depth", integer(0, 64), 0), ("mark", one_inline, None), ("body", one_inline, None), ("state", state, "info"),
    ("cursor", boolean, False), ("prefix", text(200), ""), ("tip", tl, ""), ("after", one_of(("Props",)), None))
reg(ui.Outline, ("rows", seq(lambda c, v, d: comp(c, v, d, ("Branch",)), strict=True), ()))
reg(ui.Props, ("title", tx, ""), ("items", seq(triple(tx, tl, enum(("err", "warn", "ok", "info"), "")), strict=True, cap=500), ()),
    ("h", integer(0, 1000, None), None))


def _option(c, v, d):  # a Seg's option [text, key, chosen, href]: the link is never read
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise _Bad
    return text(40)(c, v[0], d), text(8)(c, v[1], d), boolean(c, v[2], d), None


def _fit(c, cls, kw):
    """A Table / NoteTable: when no column has a width (the API flavour drops them), each one is as wide as its widest cell, so that the
    columns line up."""
    cols, rows = kw["cols"], kw["rows"]
    if cols and all(col.w is None for col in cols) and not c.lossless:
        head = kw.get("head") or kw.get("head_line") is not None
        for j, col in enumerate(cols):
            wide = ansi.vlen(col.label) + (1 if col.sort in ("asc", "desc") else 0) if head else 0
            for r in rows:
                wide = max(wide, ansi.vlen(ansi.inline(r.cells[j], r.tone)))
            col.w = min(wide, 60)
    return cls(**kw)


def _groups(c, v, d):  # [(label, first row)] or None (not grouped): an empty list is a different thing
    return None if v is None else seq(pair(tx, integer(0, 10 ** 6)), strict=True, cap=500)(c, v, d)


TABLE_FIELDS = (("cols", seq(lambda c, v, d: comp(c, v, d, ("Col",)), strict=True, cap=50), ()),
                ("rows", seq(lambda c, v, d: comp(c, v, d, ("Row",)), strict=True), ()))
reg(ui.Table, *(TABLE_FIELDS + (("groups", _groups, None), ("head", boolean, False),
                                ("indent", integer(0, 40, 1), 1), ("head_line", opt_text(200), None), ("titled", boolean, False),
                                ("fill", boolean, False), ("head_tone", tone, None), ("solid", integer(0, 1000, None), None),
                                ("fit", boolean, False), ("clip", integer(0, 1000, None), None))), build=_fit)
reg(ui.NoteTable, *(TABLE_FIELDS + (("notes", seq(nodes, strict=True), ()), ("head", boolean, False))), build=_fit)
reg(ui.KV, ("pairs", seq(pair(tx, one_inline), strict=True), ()), ("lw", integer(0, 60, 13), 13), ("indent", integer(0, 40, 3), 3),
    ("wrap", boolean, False), ("cols", integer(1, 8, 1), 1))


def _cols(c, cls, kw):
    """Parts side by side. The API flavour has no widths: the parts are stacked (one under the other) instead."""
    kids, widths = kw["children"], kw["widths"]
    if not widths:
        return ui.Group(kids)
    widths = (widths + [None] * len(kids))[:len(kids)]
    return cls([(k, w) for k, w in zip(kids, widths)], kw["gap"], kw["h"], kw["once"])


reg(ui.Cols, ("children", nodes, ()), ("widths", seq(integer(0, 300, None), cap=50), ()), ("gap", integer(0, 20, 3), 3),
    ("h", integer(0, 1000, None), None), ("once", boolean, False), build=_cols)


def _split(c, cls, kw):
    """Two parts with a rule between them. The API flavour has no width for the left one: they are stacked."""
    if kw["lw"] is None:
        return ui.Group(kw["left"] + kw["right"])
    return cls(kw["left"], kw["right"], kw["lw"], kw["h"] or 0, kw["sep"])


reg(ui.Split, ("left", nodes, ()), ("right", nodes, ()), ("lw", integer(0, 200, None), None), ("h", integer(0, 1000, 0), 0),
    ("sep", text(8, " \u2502 "), " \u2502 "), build=_split)


def _timeline(c, cls, kw):
    total = kw["total"]
    return cls([(n, min(v, total)) for n, v in kw["parts"]], total, kw["w"])  # a part is never more than the whole (it is drawn in proportion)


reg(ui.Timeline, ("parts", seq(pair(tx, real(0.0, 1e9, 0.0)), strict=True, cap=50), ()), ("total", real(0.001, 1e9, 1.0), 1.0),
    ("w", integer(1, 200, 40), 40), build=_timeline)
ALLOWED = tuple(sorted(SPECS))  # the allowlist, for the docs and the tests
# not allowed on purpose (the AI screen's and the web's own): Badge, Action, Controls, Question, Spec, Qa, Prompt, Log. They are 'unsupported'.


# ---- documents -----------------------------------------------------------------------------------------------------------------------
class Document(object):
    """A decoded view document. ok: it can be drawn; notice: why not ('' | 'invalid' | 'incompatible' | 'view'); nodes: the components of
    cpu, health, map and events (of any view: the placeholder lines when not ok); cards, layout [(id, columns)], kpis, host, status
    (state, text) and stale: the overview's; cut: some caps were hit (part of the document was left out); rejected: how many nodes were
    replaced."""
    __slots__ = ("ok", "notice", "view", "api", "at", "rev", "flavour", "host", "status", "stale", "kpis", "cards", "layout", "nodes", "cut",
                 "rejected")

    def __init__(self):
        self.ok, self.notice, self.view, self.api, self.at, self.rev, self.flavour = False, "", "", 0, None, "", "api"
        self.host, self.status, self.stale, self.kpis, self.cards, self.layout, self.nodes = "", ("unknown", ""), False, [], [], [], []
        self.cut, self.rejected = False, 0


def _refuse(notice, level, words, doc=None):
    d = doc or Document()
    d.ok, d.notice, d.nodes, d.cards, d.layout, d.kpis = False, notice, [ui.Msg(level, words)], [], [], []
    return d


def _status(c, v):
    if not isinstance(v, dict):
        return "unknown", ""
    try:
        return state(c, v.get("state"), 0), text(80)(c, v.get("text"), 0)
    except _Bad:
        return "unknown", ""


def _layout(c, v):
    out = []
    for x in (v if isinstance(v, list) else [])[:64]:
        if isinstance(x, dict) and isinstance(x.get("id"), str):
            try:
                out.append((short(c, x["id"], 0), integer(1, 4, 1)(c, x.get("w"), 0) if x.get("w") is not None else 1))
            except _Bad:
                pass
    return out


def _card_fallback(v):
    return ui.Card("?", "unsupported", "", "unknown", [_placeholder(v)])


def decode(doc):
    """doc (what json.loads gave: the dict of a view document) -> a Document. Never raises."""
    try:
        return _decode(doc)
    except Exception:  # noqa: BLE001 - whatever the data did, the reader gets a line, not a traceback
        return _refuse("invalid", "err", "the remote host's document could not be read")


def _decode(doc):
    if not isinstance(doc, dict):
        return _refuse("invalid", "err", "the remote host's document could not be read")
    api, view = doc.get("api"), doc.get("view")
    d = Document()
    if isinstance(api, bool) or not isinstance(api, int) or api < 1:
        return _refuse("invalid", "err", "the remote host's document has no API version")
    d.api = min(api, 10 ** 6)
    if api > API:
        return _refuse("incompatible", "warn", "incompatible: the remote host speaks web API %d, this console reads up to %d: update this console"
                       % (d.api, API), d)
    if not isinstance(view, str) or view not in VIEWS:
        return _refuse("view", "warn", "this view is not available remotely", d)
    d.view, c = view, _Ctx(doc.get("flavour") == "console")
    d.flavour = "console" if c.lossless else "api"
    at = doc.get("at")
    d.at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) and 0 <= at < 1e11 else None
    rev = doc.get("rev")
    d.rev = clean(rev[:32]) if isinstance(rev, str) else ""
    if view == "overview":
        d.host = _try(text(80), c, doc.get("host"), "")
        d.status, d.stale = _status(c, doc.get("status")), doc.get("stale") is True
        d.kpis = _try(seq(lambda c_, v, d_: comp(c_, v, d_, ("Kpi",)), drop=True, cap=64), c, doc.get("kpis"), [])
        d.cards = _try(seq(lambda c_, v, d_: comp(c_, v, d_, ("Card",)), fallback=_card_fallback, cap=64), c, doc.get("cards"), [])
        d.layout = _layout(c, doc.get("layout"))
    else:
        d.nodes = _try(nodes, c, doc.get("nodes"), [])
    d.ok, d.cut, d.rejected = True, c.cut, c.rejected
    return d


def _try(parse, c, v, default):
    try:
        return parse(c, v, 0)
    except _Bad:
        c.rejected += 1
        return default


def _no_constant(_name):
    raise ValueError("NaN and Infinity are not JSON")


def _int_text(s):
    if len(s) > 20:
        raise ValueError("number too long")
    return int(s)


def decode_bytes(raw):
    """The bytes of a document -> a Document: at most MAX_BYTES, strict JSON (no NaN, no number of more than 20 digits), nesting that the parser
    refuses is 'invalid'. Never raises."""
    try:
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BYTES:
            return _refuse("invalid", "err", "the remote host's document is too big or not text")
        return decode(json.loads(bytes(raw).decode("utf-8"), parse_constant=_no_constant, parse_int=_int_text))
    except (ValueError, RecursionError, MemoryError):
        return _refuse("invalid", "err", "the remote host's document could not be read")


def kpis(doc):
    """The key figures (ui.Kpi) of a document that has them (a summary or an overview): at most 64; none when it has not. Never raises."""
    try:
        c = _Ctx(isinstance(doc, dict) and doc.get("flavour") == "console")
        return _try(seq(lambda c_, v, d_: comp(c_, v, d_, ("Kpi",)), drop=True, cap=64), c, doc.get("kpis") if isinstance(doc, dict) else None, [])
    except Exception:  # noqa: BLE001
        return []
