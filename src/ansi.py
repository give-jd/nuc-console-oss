"""Console primitives (stdlib only, Python 3.8+): colour, widths, section rules, status lines, bars.

What every ANSI screen is drawn from. A colour that means a state is asked of ui.sgr(token); the raw codes that remain are the
ones that mean nothing but themselves (the boot stages' colours, a CPU bar's parts). Nothing here knows the size of the
terminal, the sections to hide or the data being drawn: that is render.py's.
"""
import re

import ui

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SPARK = "▁▂▃▄▅▆▇█"


def c(code, s):
    return f"\x1b[{code}m{s}\x1b[0m"


def cc(code, s):
    """c() that leaves the terminal's own colour alone when there is no code."""
    return c(code, s) if code else s


def vlen(s):
    return len(ANSI.sub("", s))


def pad(s, w):
    return s + " " * max(0, w - vlen(s))


def section(title, w, note=""):
    """Section title: '── TITLE ────────  note'. The caller always puts an empty line before it."""
    t = f" {title} "
    accent = ui.sgr("accent")
    return c(accent, "──") + c(ui.sgr("accent_strong"), t) + c(accent, "─" * max(2, w - 4 - len(t) - (len(note) + 2 if note else 0))) \
        + (c(ui.sgr("muted"), f"  {note}") if note else "")


def msg(level, text):
    """Indented status message: level err/warn/ok/info, with a symbol (colour alone is not enough)."""
    sym = {"err": "✖", "warn": "!", "ok": "✔", "info": "·"}[level]
    return f"   {c(ui.sgr(level), sym)} {text}"


def msg_wrap(level, text, w):
    """msg(), continued on the lines below (under the text) at the commas when it is wider than w: a long list is not cut."""
    lines, cur = [], ""
    for part in text.split(", "):
        if cur and len(cur) + 2 + len(part) > w - 6:  # -6: the comma that ends the line when it wraps
            lines.append(cur + ",")
            cur = part
        else:
            cur = cur + ", " + part if cur else part
    lines.append(cur)
    return [msg(level, lines[0])] + ["     " + x for x in lines[1:]]


def kv(label, value, lw=13):
    return f"   {c(ui.sgr('muted'), pad(label, lw))}{value}"


def fit_join(items, sep, w, lead="", c90=False):
    """lead + items joined by sep, dropping trailing items until it fits in w columns (at least one is kept)."""
    items = list(items)
    while len(items) > 1 and vlen(lead + sep.join(items)) > w:
        items.pop()
    text = sep.join(items)
    return lead + (c(ui.sgr("muted"), text) if c90 else text)


def clip(s, w):
    """Cut to w visible columns, leaving ANSI sequences intact."""
    out, n, i = [], 0, 0
    while i < len(s):
        m = ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
        elif n >= w:
            break
        else:
            out.append(s[i])
            n += 1
            i += 1
    return "".join(out) + "\x1b[0m"


def columns(cols, w, gap=2):
    """Puts blocks of lines side by side; each block is (lines, width)."""
    rows = max(len(x) for x, _ in cols)
    out = []
    for i in range(rows):
        out.append((" " * gap).join(pad(clip(x[i], cw), cw) if i < len(x) else " " * cw for x, cw in cols))
    return out


def bar(frac, w, warn=0.7, err=0.9):
    frac = min(max(frac, 0.0), 1.0)
    n = round(frac * w)
    level = "ok" if frac < warn else "warn" if frac < err else "err"
    return c(ui.sgr(level), "█" * n) + c(ui.sgr("muted"), "░" * (w - n))


def sparkline(values, width, floor=1024):
    """Last `width` values as small bars; scaled to the series maximum (with a floor, so noise is not blown up)."""
    vals = list(values)[-width:]
    top = max(max(vals, default=0), floor)
    return c(ui.sgr("muted"), "▁" * (width - len(vals))) + "".join(SPARK[min(7, int(v / top * 8))] for v in vals)


def cell(v, warn=False, net=False, loc=False):
    """One cell of the exposure matrix: 0 closed, 1 open, 2 filtered by source, 3 unknown (warn: a database; net: the Internet column;
    loc: the 'this machine' column, where open is not an alarm)."""
    if loc:  # local is not an alarm: neutral colour
        return c(ui.sgr("neutral"), "●") if v else c(ui.sgr("muted"), "·")
    if v == 3:
        return c(ui.sgr("unknown"), "?")
    if net:
        return c(ui.sgr("err_strong"), "●") if v else c(ui.sgr("muted"), "·")
    if v == 1:
        return c(ui.sgr("err" if warn else "warn"), "●")
    return c(ui.sgr("accent"), "◐") if v == 2 else c(ui.sgr("muted"), "·")


def _cut(s, a, b):
    """The part of s between visible columns a and b (b None: to the end) with its colours, as (text, columns taken). The colour in force
    at column a is carried over, so a slice from the middle of a coloured run keeps it."""
    out, n, i, sgr = [], 0, 0, ""
    while i < len(s):
        m = ANSI.match(s, i)
        if m:
            seq = m.group()
            if n < a:
                sgr = "" if seq in ("\x1b[0m", "\x1b[m") else sgr + seq if seq.endswith("m") else sgr
            elif b is None or n < b:
                out.append(seq)
            i = m.end()
            continue
        if n >= a and (b is None or n < b):
            if not out and sgr:
                out.append(sgr)
            out.append(s[i])
        n += 1
        i += 1
    return "".join(out), max(0, min(n, b if b is not None else n) - a)


def overlay(lines, box, w=None, top=None, left=None):
    """The frame's lines with the box (lines, ANSI allowed) drawn over their middle: the rest of the frame stays around it. Each box line
    replaces as many columns as it is wide; a frame line shorter than the box is padded. w: the frame's width (default: the widest
    line). top/left: where the box goes (default: centred). The box is cut when it is taller or wider than the frame."""
    lines = list(lines)
    w = w or max([vlen(x) for x in lines] + [1])
    box = [x for x in box][:len(lines)]
    bw = min(w, max([vlen(x) for x in box] + [0]))
    top = max(0, (len(lines) - len(box)) // 2) if top is None else top
    left = max(0, (w - bw) // 2) if left is None else left
    for j, row in enumerate(box):
        i = top + j
        if i >= len(lines):
            break
        line = lines[i]
        head, took = _cut(line, 0, left)
        head += " " * (left - took)
        row, took = _cut(row, 0, bw)
        row += " " * (bw - took)
        tail, _ = _cut(line, left + bw, None)
        lines[i] = head + "\x1b[0m" + row + "\x1b[0m" + tail
    return lines


# ---- the components, drawn --------------------------------------------------------------------------------------------------------
# render(node, w) -> (lines, truncated): the console's text for a ui component, in the format the sections have always had (a block
# is indented one column, a Msg three, a Line is as it is, as before). truncated is True when the drawing itself left items out ('... +N' of a Wrap), which
# the caller (render.page_overview) tells the Details pages. A value that has no state is drawn '?'; a node that is not a component is
# drawn '?' too, never fine and never an exception.

_STATE_TONE = {"ok": "ok", "warn": "warn", "err": "err", "down": "err", "unknown": "unknown", "info": "info"}


def style(text, tone=None, bold=False):
    """text in the colour of a tone (a token of ui.TOKENS) and bold; plain when the theme gives it no code."""
    if not text:
        return ""
    code = ui.sgr(tone) if tone else ""
    if bold:
        code = "1;" + code if code and code != "1" else "1"
    return cc(code, text)


def _bar(b):
    if b.frac is None:
        return pad(style("?", "unknown"), b.w)
    return bar(b.frac, b.w, b.warn, b.err) + (" " + b.value_text if b.value_text else "")


def inline(x, tone=None):
    """A Span, a Line, a Bar or a Spark as the text of one line (tone: the colour of a Span that has none)."""
    if isinstance(x, ui.Span):
        return style(x.text, x.tone or tone, x.bold)
    if isinstance(x, ui.Line):
        return "".join(inline(p, tone) for p in x.spans)
    if isinstance(x, ui.Bar):
        return _bar(x)
    if isinstance(x, ui.Spark):
        return sparkline(x.values, x.w, x.floor)
    return style("?", "unknown")


def _wrap(items, w, indent, sep, max_lines):
    """The items over several lines without splitting one; beyond max_lines the last line ends with '… +N' (render.wrap_items)."""
    rows, cur = [], []
    for it in items:
        if cur and indent + vlen(sep.join(cur)) + len(sep) + vlen(it) > w:
            rows.append(cur)
            cur = []
        cur.append(it)
    rows.append(cur)
    rows = [r for r in rows if r]
    if not max_lines or len(rows) <= max_lines:
        return [" " * indent + sep.join(r) for r in rows], False
    rows, hidden = rows[:max_lines], sum(len(r) for r in rows[max_lines:])
    while len(rows[-1]) > 1 and indent + vlen(sep.join(rows[-1])) + 8 > w:  # 8 = "  … +NNN"
        rows[-1].pop()
        hidden += 1
    return [" " * indent + sep.join(r) for r in rows[:-1]] + [" " * indent + sep.join(rows[-1]) + style(f"  … +{hidden}", "muted")], True


def _table_lines(t, w):
    """A Table as lines: a column with a w is padded to it (right-aligned: on the left), the others are the text as it is; gap spaces
    follow every column but the last. When a line is wider than w the columns with a prio go, the biggest prio first (the one on the
    right first among equals), until it fits or only the columns that are never dropped are left."""
    cols = list(range(len(t.cols)))
    while True:
        grid = []
        if t.head:
            grid.append([style(t.cols[j].label, "muted") for j in cols])
        grid += [[inline(r.cells[j], r.tone) for j in cols] for r in t.rows]
        lines = []
        for row in grid:
            parts = []
            for n, (j, text) in enumerate(zip(cols, row)):
                col, last = t.cols[j], n == len(cols) - 1
                if col.w is not None and col.align == "r":
                    text = " " * (col.w - vlen(text)) + text
                elif col.w is not None and not last:
                    text = pad(text, col.w)
                parts.append(text + ("" if last else " " * col.gap))
            lines.append(" " + "".join(parts))
        droppable = [j for j in cols if t.cols[j].prio > 0]
        if droppable and max([vlen(x) for x in lines] + [0]) > w:
            cols.remove(max(droppable, key=lambda j: (t.cols[j].prio, j)))
            continue
        break
    if not t.groups:
        return lines
    first, marks, out = (1 if t.head else 0), {i: label for label, i in t.groups}, []
    for i, line in enumerate(lines):
        if i >= first and (i - first) in marks:
            out.append(" " + style(marks[i - first], "muted"))
        out.append(line)
    return out


def render(node, w):
    """(lines, truncated) of a component for a console w columns wide."""
    if isinstance(node, ui.Raw):
        return list(node.lines), False
    if isinstance(node, ui.Card):
        return card_lines(node, w)
    if isinstance(node, (ui.Span, ui.Line, ui.Bar, ui.Spark)):
        return [inline(node)], False  # as it is: a line carries its own indent (a coloured line starts with its space)
    if isinstance(node, ui.Msg):  # a Notice too
        return [msg(node.level, node.text)], False
    if isinstance(node, ui.KV):
        return [kv(k, inline(v), node.lw) for k, v in node.pairs], False
    if isinstance(node, ui.Table):
        return _table_lines(node, w), False
    if isinstance(node, ui.Wrap):
        return _wrap([inline(x) for x in node.items], w, node.indent, node.sep, node.max_lines)
    if isinstance(node, ui.More):
        return [" " + style(node.text, "muted")], False
    if isinstance(node, ui.Group):
        lines, hid = ([" " + style(node.title, "accent_strong")] if node.title else []), False
        for child in node.children:
            part, h = render(child, w)
            lines += part
            hid = hid or h
        return lines, hid
    if isinstance(node, ui.Pill):
        return [" " + style(f"{ui.SYMBOLS[node.state]} {node.text}", _STATE_TONE[node.state], True)], False
    if isinstance(node, ui.Kpi):
        return [" " + style(node.symbol, _STATE_TONE[node.state]) + f" {node.label} {node.value}{node.unit}"], False
    if isinstance(node, ui.Tree):
        return [" " + "  " * d + style(ui.SYMBOLS[st], _STATE_TONE[st]) + " " + inline(x) for d, x, st in node.rows], False
    if isinstance(node, ui.Details):
        lines, hid = [" " + style("▾" if node.open else "▸", "muted") + " " + inline(node.summary)], False
        for child in node.body if node.open else ():
            part, h = render(child, w)
            lines += ["  " + x for x in part]
            hid = hid or h
        return lines, hid
    return [" " + style("?", "unknown")], False


def card_lines(card, w):
    """(lines, truncated) of a card: the section's title line (the note in it), then its body. A card whose body starts with Raw
    carries its own title (the lines of a section not yet built of components)."""
    if card.body and isinstance(card.body[0], ui.Raw):
        lines, hid = [], False
    else:
        lines, hid = [section(card.title, w, card.note)], False
    for part in card.body:
        got, h = render(part, w)
        lines += got
        hid = hid or h
    return lines, hid or card.truncated


# ---- the theme of a whole frame ------------------------------------------------------------------------------------------------
# The screens are written with the default theme's codes (ui.sgr(token), or a raw code that means a state: 31 32 33 90 ...). A theme is
# applied to the finished frame: every SGR sequence is looked up as the default theme's code of a token and written with the theme's
# own code for it. A code that is no token's (a CPU bar's parts, a boot stage) goes through the theme's per-colour table.

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_COLOUR = re.compile(r"(?:3[0-7]|4[0-7]|9[0-7]|10[0-7])$")
_PER_COLOUR = {
    "light": {"36": "34", "96": "94", "37": "30", "97": "30", "1;36": "1;34"},
    "hc": {"31": "1;91", "32": "1;92", "33": "1;93", "34": "1;94", "35": "1;95", "36": "1;96", "37": "1;97", "90": "37", "2": ""},
}
_TABLES = {}


def _table(theme):
    """{default code: the theme's code} for the tokens, the more common meaning first where two tokens share a code (90: muted)."""
    t = _TABLES.get(theme)
    if t is None:
        t = _TABLES[theme] = {}
        base = ui.ANSI_THEMES["default"]
        for token in ("muted", "ok", "warn", "err", "accent", "strong", "banner_ok", "banner_err", "banner_warn", "sel", "accent_strong",
                      "err_strong", "neutral", "unknown", "info"):
            t.setdefault(base[token], ui.ANSI_THEMES[theme][token])
    return t


def _param(theme, params):
    """The parameters of one sequence in a theme ('' = the sequence goes)."""
    if params in ("", "0"):
        return params
    table = _table(theme)
    if params in table:
        return table[params]
    parts, out, i = params.split(";"), [], 0
    while i < len(parts):
        p = parts[i]
        if theme == "mono" and p in ("38", "48") and i + 1 < len(parts):  # 38;5;N / 38;2;R;G;B: an extended colour
            i += 3 if parts[i + 1] == "5" else 5
            continue
        if theme == "mono":
            if not _COLOUR.match(p):
                out.append(p)
        else:
            out.append(_PER_COLOUR.get(theme, {}).get(p, p))
        i += 1
    res = []
    for x in ";".join(out).split(";"):
        if x and not (x == "1" and "1" in res):
            res.append(x)
    return ";".join(res)


def retheme(text, theme):
    """text (a frame, ANSI) written for the 'default' theme, in another ANSI theme of ui.ANSI_THEMES: light, hc or mono (no colour at all).
    Sequences that end up empty are dropped; the default theme returns text as it is."""
    if theme == "default" or theme not in ui.ANSI_THEMES:
        return text

    def one(m):
        p = _param(theme, m.group(1))
        return "\x1b[" + p + "m" if p or m.group(1) in ("", "0") else ""
    return _SGR.sub(one, text)
