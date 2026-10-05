"""Console primitives (stdlib only, Python 3.8+): colour, widths, section rules, status lines, bars.

What every ANSI screen is drawn from. A colour that means a state is asked of ui.sgr(token); the raw codes that remain are the
ones that mean nothing but themselves (the boot stages' colours, a CPU bar's parts). Nothing here knows the size of the
terminal, the sections to hide or the data being drawn: that is render.py's.
"""
import re
import textwrap

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


_ANSI_SPLIT = re.compile("(" + ANSI.pattern + ")")


def clip(s, w):
    """Cut to w visible columns, leaving ANSI sequences intact (those right after the last column too, up to the next character)."""
    out, room = [], max(w, 0)
    for i, part in enumerate(_ANSI_SPLIT.split(s)):
        if i & 1:  # a sequence
            out.append(part)
        elif part:
            if len(part) > room:
                out.append(part[:room])
                break
            out.append(part)
            room -= len(part)
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


# the parts of a Meter (one CPU in the CPU screen) as the console has always drawn them: raw codes, they mean nothing but themselves (htop's
# meter: user green, system red, other work blue, a plain busy part cyan, I/O wait grey with its own glyph, so that colour is not all that tells it)
METER_STYLE = {"user": ("32", "█"), "system": ("31", "█"), "other": ("34", "█"), "busy": ("36", "█"), "iowait": ("90", "▒")}


def meter(parts, w):
    """A w-wide bar of consecutive segments [(percent, kind)], the rest idle."""
    out, done, acc = "", 0, 0.0
    for pct, kind in parts:
        acc += pct
        end = min(w, int(round(acc * w / 100.0)))
        if end > done:
            code, ch = METER_STYLE[kind]
            out += c(code, ch * (end - done))
            done = end
    return out + c(90, "░" * (w - done))


def cut_lines(lines, n, what="lines"):
    """The first n lines; when some are left out the last one says how many ('… +3 more lines'). what None: cut, say nothing."""
    if len(lines) <= n:
        return lines
    if what is None:
        return lines[:n]
    return lines[:max(n - 1, 0)] + ([c(ui.sgr("muted"), f" … +{len(lines) - n + 1} more {what}")] if n else [])


def sparkline(values, width, floor=1024):
    """Last `width` values as small bars; scaled to the series maximum (with a floor, so noise is not blown up)."""
    vals = list(values)[-width:]
    top = max(max(vals, default=0), floor)
    return c(ui.sgr("muted"), "▁" * (width - len(vals))) + "".join(SPARK[min(7, int(v / top * 8))] for v in vals)


def scroll(top, i, n, rows, margin=2):
    """First row of a list of n shown, so that row i stays in sight, a few rows from the edges when there is room."""
    m = min(margin, max(0, (rows - 1) // 2))
    top = max(min(top, i - m), i + m + 1 - rows)
    return max(0, min(top, n - rows))


def hbucket(vals, n):
    """vals as at most n values: the mean of each group (None when a group has no value)."""
    if len(vals) <= n:
        return list(vals)
    step = len(vals) / float(n)
    out = []
    for i in range(n):
        g = [v for v in vals[int(i * step):int((i + 1) * step) or 1] if v is not None]
        out.append(sum(g) / len(g) if g else None)
    return out


def hspark(vals, width):
    """vals (None = not recorded) as small bars scaled to their own maximum, `width` columns; none recorded: blank."""
    vals = hbucket([ui.hnum(v, None) if v is not None else None for v in vals], width)
    top = max([v for v in vals if v is not None] or [0])
    bars = "".join(" " if v is None else SPARK[0] if top <= 0 else SPARK[min(7, int(v / top * 7.999))] for v in vals)
    return pad(bars, width)


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
    """text in the colour of a tone (a token of ui.TOKENS) and bold; plain when the theme gives it no code. An empty text with a tone is
    still written coloured (c() has always done it: the line keeps its bytes when a note turns out empty)."""
    if not text and not tone and not bold:
        return ""
    code = ui.sgr(tone) if tone else ""
    if bold:
        code = "1;" + code if code and code != "1" else "1"
    return cc(code, text)


def _bar(b):
    if b.frac is None:
        return pad(style("?", "unknown"), b.w)
    if b.tone:  # a share, not a level: the fill is the tone's whatever the fraction is
        n = round(b.frac * b.w)
        return c(ui.sgr(b.tone), "█" * n) + c(ui.sgr("muted"), "░" * (b.w - n)) + (" " + b.value_text if b.value_text else "")
    return bar(b.frac, b.w, b.warn, b.err) + (" " + b.value_text if b.value_text else "")


# the pill of a Badge as the console has always drawn a verdict: raw codes (a green, a cyan, a yellow, a red block with their text colour)
_BADGE = {"ok": "1;42;30", "accent": "1;46;30", "warn": "1;43;30", "err": "1;41;37", "muted": "90"}


def inline(x, tone=None):
    """A Span, a Line, a Bar or a Spark as the text of one line (tone: the colour of a Span that has none)."""
    if isinstance(x, ui.Span):
        return style(x.text, x.tone or tone, x.bold)
    if isinstance(x, ui.Line):
        text = "".join(inline(p, tone) for p in x.spans)
        return text if x.clip is None else clip(text, x.clip)
    if isinstance(x, ui.Bar):
        return _bar(x)
    if isinstance(x, ui.Spark):
        return sparkline(x.values, x.w, x.floor)
    if isinstance(x, ui.Meter):
        return meter(x.parts, x.w)
    if isinstance(x, ui.Series):
        return style(hspark(x.values, x.w), x.tone)
    if isinstance(x, ui.Seg):
        return " ".join(style(f" {key}:{text} ", "sel" if chosen else "muted") for text, key, chosen, _href in x.options)
    if isinstance(x, ui.Badge):
        return c(_BADGE[x.tone], " " + x.text.ljust(x.w) + " ")
    if isinstance(x, ui.Action):
        return ""  # a button of the web: the console has keys
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


def _cell(x, col, tone):
    """A cell as text; a Span of a pad_in column is padded to the column's width inside its colour first."""
    if col.pad_in and col.w is not None and isinstance(x, ui.Span):
        text = x.text.rjust(col.w) if col.align == "r" else x.text.ljust(col.w)
        x = ui.Span(text, x.tone, x.bold, x.mono)
    return inline(x, tone)


def _head_cell(t, j, last):
    """The label of column j in a table with a head_tone: padded to the column's width inside its colour (not the last column: no padding
    after it), the sorted one warn and bold with its arrow."""
    col = t.cols[j]
    label = col.label + {"desc": "▼", "asc": "▲"}.get(col.sort, "")
    if col.w is not None and not (last and col.align != "r"):
        label = label.rjust(col.w) if col.align == "r" else label.ljust(col.w)
    return style(label, "warn" if col.sort else t.head_tone, bool(col.sort))


def _aligned(col, text, last, padlast=False):
    """A table cell in its column: a w pads it (right-aligned: on the left, centred: on both sides); the last cell is left as it is
    unless padlast."""
    if col.w is not None and col.align == "r":
        return " " * (col.w - vlen(text)) + text
    if col.w is not None and col.align == "c":
        room = col.w - vlen(text)
        left = room // 2 + (room & col.w & 1) if room > 0 else 0  # str.center's own arithmetic
        return " " * left + text + " " * max(0, room - left)
    if col.w is not None and (padlast or not last):
        return pad(text, col.w)
    return text


def _fit_lines(t, w):
    """A Table with fit: every row alone loses its last cells until it is no wider than w (one cell stays); a cell with no text and no w is not there."""
    lead, lines = " " * t.indent, []
    for r in t.rows:
        items = []
        for col, cell_ in zip(t.cols, r.cells):
            text = inline(cell_, r.tone)
            if text or col.w is not None:
                items.append((_aligned(col, clip(text, col.clip) if col.clip is not None else text, False, True), col.gap))
        line = lambda: lead + "".join(x + " " * g for x, g in items[:-1]) + (items[-1][0] if items else "")  # noqa: E731
        while len(items) > 1 and vlen(line()) > w:
            items.pop()
        lines.append(line())
    return lines


def _table_lines(t, w):
    """A Table as lines: a column with a w is padded to it (right-aligned: on the left, centred: on both sides), the others are the text as
    it is; gap spaces follow every column but the last, and t.indent spaces come before the first. When a line is wider than w the columns
    with a prio go, the biggest prio first (the one on the right first among equals), until it fits or only the columns that are never
    dropped are left. A column with a clip is cut to it first (a cut cell ends the colour it was in). Groups: a muted label before its first
    row, or, titled, a blank line and a bold title with the number of its rows."""
    if t.fit:
        return _fit_lines(t, w)
    cols = list(range(len(t.cols)))
    lead = " " * t.indent
    while True:
        grid = []
        if t.head and t.head_line is None:
            grid.append(([_head_cell(t, j, n == len(cols) - 1) if t.head_tone else style(t.cols[j].label, "muted")
                          for n, j in enumerate(cols)], None))
        fill = t.fill
        grid += [([_cell(r.cells[j], t.cols[j], None if fill else r.tone) for j in cols], r.tone if fill else None) for r in t.rows]
        lines = []
        for row, tone in grid:
            parts = []
            for n, (j, text) in enumerate(zip(cols, row)):
                col, last = t.cols[j], n == len(cols) - 1
                if col.clip is not None:
                    text = clip(text, col.clip)
                text = _aligned(col, text, last)
                parts.append(text + ("" if last else " " * col.gap))
            line = lead + "".join(parts)
            if tone and t.solid is not None:  # the cursor row: its own colours go, it is as wide as the list
                line = pad(ANSI.sub("", clip(line, t.solid)), t.solid)
            elif t.clip is not None and not tone:
                line = clip(line, t.clip)
            lines.append(style(line, tone) if tone else line)
        droppable = [j for j in cols if t.cols[j].prio > 0]
        if droppable and max([vlen(x) for x in lines] + [0]) > w:
            cols.remove(max(droppable, key=lambda j: (t.cols[j].prio, j)))
            continue
        break
    head = [style(t.head_line, None, True)] if t.head_line is not None else []
    if not t.groups:
        return head + lines
    first, marks, out = (1 if t.head and t.head_line is None else 0), {i: label for label, i in t.groups}, []
    ends = sorted(i for _label, i in t.groups) + [len(t.rows)]
    for i, line in enumerate(lines):
        at = i - first
        if i >= first and at in marks:
            if t.titled:
                out += ["", style(lead + marks[at], None, True) + style(f"  ({ends[ends.index(at) + 1] - at})", "muted")]
            else:
                out.append(" " + style(marks[at], "muted"))
        out.append(line)
    return head + out


def render(node, w):
    """(lines, truncated) of a component for a console w columns wide."""
    if isinstance(node, ui.Raw):
        return list(node.lines), False
    if isinstance(node, ui.Card):
        return card_lines(node, w)
    if isinstance(node, (ui.Span, ui.Line, ui.Bar, ui.Spark, ui.Meter)):  # a Line's clip is applied by inline()
        return [inline(node)], False  # as it is: a line carries its own indent (a coloured line starts with its space)
    if isinstance(node, ui.Problem):
        return msg_wrap(node.level, node.text, w), False  # the long ones go on under their text, at the commas
    if isinstance(node, ui.RichMsg):
        return [msg(node.level, inline(node.rich))], False
    if isinstance(node, ui.Msg):  # a Notice too
        return [msg(node.level, node.text)], False
    if isinstance(node, ui.Hint):
        return [], False  # for the web
    if isinstance(node, ui.KV):
        return _kv_lines(node, w), False
    if isinstance(node, ui.Table):
        return _table_lines(node, w), False
    if isinstance(node, ui.Wrap):
        lines, hid = _wrap([inline(x) for x in node.items], w, node.indent, node.sep, node.max_lines)
        if node.lead is not None and lines:
            lead = inline(node.lead)
            lines[0] = " " + lead + " " * max(1, node.indent - 1 - vlen(lead)) + lines[0][node.indent:]
        return lines, hid
    if isinstance(node, ui.More):
        return ([" " + style(node.text, "muted")] if node.indent is None else [style(" " * node.indent + node.text, "muted")]), False
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
    if isinstance(node, ui.Details) and node.brief is not None:
        return [inline(node.brief)], False
    if isinstance(node, ui.Details):
        lines, hid = [" " + style("▾" if node.open else "▸", "muted") + " " + inline(node.summary)], False
        for child in node.body if node.open else ():
            part, h = render(child, w)
            lines += ["  " + x for x in part]
            hid = hid or h
        return lines, hid
    drawn = _DRAW.get(type(node))
    if drawn is not None:
        return drawn(node, w)
    return [" " + style("?", "unknown")], False


def _kv_lines(node, w):
    """A KV as lines. As it is: three spaces, the label padded to lw, the value. wrap: the value in as many lines as the room asks
    (the label only on the first). cols: the pairs in columns, each label and value cut to its share of the room."""
    if node.cols > 1:
        cw = (w - node.indent - 1) // node.cols
        cells = [style(pad(k, node.lw), "muted") + inline(v) for k, v in node.pairs]
        per = -(-len(cells) // node.cols)
        out = []
        for i in range(per):
            row = [cells[j * per + i] for j in range(node.cols) if j * per + i < len(cells)]
            out.append(" " * node.indent + "".join(pad(clip(x, cw), cw + 1) for x in row).rstrip())
        return out
    if node.wrap:
        out = []
        for k, v in node.pairs:
            chunks = textwrap.wrap(inline(v), max(8, w - node.lw - 1), break_on_hyphens=False) or [""]
            out += [" " * node.indent + (style(pad(k, node.lw), "muted") if j == 0 else " " * node.lw) + x for j, x in enumerate(chunks)]
        return out
    return [" " * (node.indent - 3) + kv(k, inline(v), node.lw) for k, v in node.pairs]


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


# ---- the components of the system, container, database and boot cards, drawn ------------------------------------------------------
# (ui.py: Head, Indent, Grid, Timeline, NoteTable, Flow). render() reaches them through _DRAW, filled below.

STAGE_COLORS = {"firmware": 35, "loader": 34, "kernel": 36, "initrd": 33, "userspace": 32, "main path": 36, "post boot": 32}  # the boot stages: raw codes


def _timeline_lines(t, w):
    widths = [(name, max(1, round(t.w * v / t.total))) for name, v in t.parts]
    bar_ = "   " + "".join(c(STAGE_COLORS.get(name, 37), "█" * n) for name, n in widths)
    legend, _ = _wrap([c(STAGE_COLORS.get(name, 37), "■") + f" {name} {ui.fmt_s(v)}" for name, v in t.parts], w, 3, "  ", None)
    return [bar_] + legend, False


def _notetable_lines(t, w):
    lines = _table_lines(ui.Table(t.cols, t.rows, None, t.head), w)
    out, first = [], 1 if t.head else 0
    out += lines[:first]
    for i, line in enumerate(lines[first:]):
        out.append(line)
        for note in t.notes[i]:
            out += render(note, w)[0]
    return out, False


def _flow_lines(f, w):
    items = [inline(x) for x in f.items]
    if f.cut is not None:
        items = [clip(x, f.cut) for x in items]
    lines, hid = _wrap(items, w, f.indent, f.sep, None)
    if lines and f.lead is not None:
        lines[0] = " " * (f.indent - 2) + inline(f.lead) + lines[0][f.indent - 1:]
    return lines, hid


def _indent_lines(node, w):
    out, hid = [], False
    for child in node.children:
        part, h = render(child, max(1, w - node.n))
        out += [" " * node.n + x for x in part]
        hid = hid or h
    return out, hid


def _grid_lines(g, w):
    if g.fit:
        cells = [pad(clip(inline(x), g.cw), g.cw) for x in g.items]
        return [" " * g.lead + (" " * g.gap).join(cells[i:i + g.per]).rstrip() for i in range(0, len(cells), g.per)], False
    cells = [pad(inline(x), g.cw) for x in g.items]
    return ["".join(cells[i:i + g.per]) for i in range(0, len(cells), g.per)], False


def _many(nodes, w):
    out, hid = [], False
    for child in nodes:
        part, h = render(child, w)
        out += part
        hid = hid or h
    return out, hid


def _cap_lines(node, w):
    lines, hid = _many(node.children, w)
    return cut_lines(lines, node.n, node.what), hid


def _only_lines(node, w):
    return _many(node.children, w) if node.surface == "console" else ([], False)

# ---- the components of the full screens, drawn (ui.py: Seg, Title, Series, Cols, Split, Pane, Finding, Advice) -------------------------

_PILL = {"err": ("\u2716 ERR ", "banner_err"), "warn": ("! WARN", "banner_warn"), "info": ("\u00b7 INFO", "muted")}  # a symbol besides the colour


def _legend(items, w, used):
    """The legend's items as one text, the last ones dropped while it does not fit after `used` columns (with the rule's 4 columns)."""
    cells = [style(glyph, tone) + " " + style(word, "muted") for glyph, word, tone in items]
    while cells and used + 4 + vlen("  ".join(cells)) > w:
        cells.pop()
    return "  ".join(cells)


def _title_line(t, w):
    left = style("\u2500\u2500", "accent") + style(f" {t.label} ", "accent_strong") + " "
    right = inline(t.seg) if t.seg is not None else ""
    bits, sep = [inline(p) for p in t.parts], style(" \u00b7 ", "muted")
    while len(bits) > 1 and vlen(left + sep.join(bits)) + vlen(right) + 3 > w:
        bits.pop(1)
    text = left + sep.join(bits) + " "
    if t.legend is not None and t.seg is None:
        right = _legend(t.legend.items, w, vlen(text))
    return clip(text + style("\u2500" * max(0, w - vlen(text) - vlen(right) - (1 if right else 0)), "accent") + (" " + right if right else ""), w)


def cut_to(lines, n, w):
    """lines as at most n lines: the last one, cut to w, says how many were left out."""
    if len(lines) <= n:
        return lines
    return lines[:max(0, n - 1)] + ([style(clip(f" \u2026 +{len(lines) - n + 1} more lines", w), "muted")] if n else [])


def _cols_lines(n, w):
    cols = []
    for node, cw in n.children:
        cw = w if cw is None else cw
        lines = render(node, cw)[0]
        if not n.once:
            lines = [clip(x, cw) if x else x for x in lines]  # a blank line between two parts stays blank
        cols.append((cut_to(lines, n.h, cw) if n.h is not None else lines, cw))
    if len(cols) == 1:
        return cols[0][0], False
    return columns(cols, w, gap=n.gap), False


def _split_lines(n, w):
    """left and right side by side, the rule between them; as tall as h when there is one (the right part is cut to it), else as the taller."""
    left, hl = _many(n.left, n.lw)
    right, hr = _many(n.right, max(1, w - n.lw - len(n.sep)))
    return [pad(left[i] if i < len(left) else "", n.lw) + style(n.sep, "muted") + (right[i] if i < len(right) else "")
            for i in range(max(len(left), n.h) if n.h else max(len(left), len(right)))], hl or hr


def _pane_lines(p, w):
    lw = 7
    out = [section("DETAILS", w), " " + style(p.title, p.level, True)]
    for label, body in (("what", p.what), ("facts", ""), ("fix", p.fix)):
        if label == "facts":
            rows = _wrap([f"{k} {c(1, v)}" for k, v in p.facts], w, lw + 1, "  \u00b7  ", None)[0] if p.facts else []
            out += [" " + style(label.ljust(lw), "muted") + r[lw + 1:] if i == 0 else r for i, r in enumerate(rows)]
            continue
        chunks = textwrap.wrap(body, max(8, w - lw - 1), break_on_hyphens=False) or ["?"]
        out += [(" " + style(label.ljust(lw), "muted") if i == 0 else " " * (lw + 1)) + x for i, x in enumerate(chunks)]
    if p.h is not None:
        out = cut_to(out, p.h, w)
    return [clip(x, w) for x in out], False


def finding_text(f, w):
    """' [ERR] title  text' as one line, not cut to w: the title and the text as far as they fit."""
    label, tone = _PILL[f.level]
    title = ui.hclean(f.title, max(10, w - 12))
    room = w - 11 - len(title)
    return " " + style(" " + label + " ", tone) + " " + style(title, None, True) \
        + ("  " + style(f.text if len(f.text) <= room - 2 else f.text[:room - 3] + "\u2026", "muted") if room >= 14 else "")


def _finding_row(f, w):
    """The finding's row cut to w; the cursor's row in reverse video, without its colours."""
    row = clip(finding_text(f, w), w)
    return style(pad(ANSI.sub("", row), w), "sel") if f.cursor else row


# ---- the Map's components, drawn (ui.py: Branch, Outline, Props) ----------------------------------------------------------------------

def _branch_line(b, w):
    """'tree mark body' cut to w; the cursor's row in reverse video, without its colours, padded to w."""
    line = (style(b.prefix, "muted") if b.prefix else "") + inline(b.mark) + " " + inline(b.body)
    row = clip(line, w)
    return style(pad(ANSI.sub("", row), w), "sel") if b.cursor else row


_LEVEL_TONE = {"err": "err", "warn": "warn", "ok": "ok", "info": "muted"}


def _props_lines(p, w):
    """'-- TITLE ---' and one line per item: the label (padded, muted; coloured when it marks a finding), the value wrapped under itself."""
    lw = min(max([len(k) for k, _v, _l in p.items] + [4]) + 2, 18, max(6, w // 3))
    out = [section(p.title, w)]
    for i, (label, value, level) in enumerate(p.items):
        tone, bold = _LEVEL_TONE.get(level), i == 0
        text = pad(label[:lw - 1], lw)
        head = style(text, tone, bold) if label.strip() in ("!", "·") else style(text, "muted")
        chunks = textwrap.wrap(value, max(8, w - lw), break_on_hyphens=False) or [""]
        out += [(head if j == 0 else " " * lw) + style(x, tone, bold) for j, x in enumerate(chunks)]
    if p.h is not None and len(out) > p.h:
        out = out[:max(0, p.h - 1)] + [style(f" … +{len(out) - p.h + 1} more lines", "muted")]
    return (out if p.h is None else out[:p.h]), False


def _spec_lines(p, w):
    """A Spec as lines: 'DETAILS', the title, then each item with its label; a value is wrapped at what is left of the label (a command, whole,
    is cut at the edge), in the colour of its tone."""
    lw = p.lw
    out = [section("DETAILS", w), " " + style(p.title, None, True)]
    for label, text, tone, whole in p.items:
        chunks = [text] if whole else textwrap.wrap(text, max(8, w - lw - 1), break_on_hyphens=False) or ["?"]
        for j, x in enumerate(chunks):
            out.append(" " + (style(label.ljust(lw), "muted") if j == 0 else " " * lw) + style(x, tone))
    if p.h is not None:
        out = cut_to(out, p.h, w)
    return [clip(x, w) for x in out], False


def _question_line(q, w):
    """The question in one line: bold in the warning colour, and how to answer it as far as it fits."""
    ask = " " + q.text
    return clip(style(ask, "warn", True) + style("  y: yes   any other key: no" if len(ask) + 27 <= w else "  [y/n]", "muted"), w)


_DRAW = {
    ui.Outline: lambda n, w: ([_branch_line(b, w) for b in n.rows], False),
    ui.Props: _props_lines,
    ui.Badge: lambda n, w: ([inline(n)], False),
    ui.Spec: _spec_lines,
    ui.Question: lambda n, w: ([_question_line(n, w)], False),
    ui.Action: lambda n, w: ([], False),  # the web's buttons, controls and chat: the console has keys and its own lines
    ui.Controls: lambda n, w: ([], False),
    ui.Qa: lambda n, w: ([], False),
    ui.Log: lambda n, w: ([], False),
    ui.Title: lambda n, w: ([_title_line(n, w)], False),
    ui.Seg: lambda n, w: ([inline(n)], False),
    ui.Series: lambda n, w: ([inline(n)], False),
    ui.Cols: _cols_lines,
    ui.Split: _split_lines,
    ui.Pane: _pane_lines,
    ui.Finding: lambda n, w: ([_finding_row(n, w)], False),
    ui.Advice: lambda n, w: (([section("ADVICE", w)] + list(n.lines)) if n.lines else [], False),
    ui.Head: lambda n, w: ([section(n.title, w, n.note)], False),
    ui.Indent: _indent_lines,
    ui.Grid: _grid_lines,
    ui.Timeline: _timeline_lines,
    ui.NoteTable: _notetable_lines,
    ui.Flow: _flow_lines,
    ui.Cap: _cap_lines,
    ui.Only: _only_lines,
    ui.Tiles: lambda n, w: ([], False),  # the web's key figures
}


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
