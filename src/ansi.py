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


def sparkline(values, width):
    """Last `width` values as small bars; scaled to the series maximum (with a floor, so noise is not blown up)."""
    vals = list(values)[-width:]
    top = max(max(vals, default=0), 1024)
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
