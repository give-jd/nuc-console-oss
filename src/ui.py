"""Design tokens and text helpers for every screen (stdlib only, Python 3.8+).

One table of semantic tokens ('ok', 'warn', 'err', ...) says what a colour MEANS; a theme says how a surface writes it:
ANSI_THEMES as SGR codes for the console, CSS_THEMES as colours for the web. The 'default' ANSI theme is exactly the codes the
console has always written, so drawing through sgr() changes nothing on screen. The text helpers (safe, plural, the human
formatters) hold no colour and no clock: they are what every screen and the exposure model share, so none of them has to
import render.py for it.

The components (Span, Line, Card, Kpi, Table, ...) at the end are the model every screen is built from: plain data with __slots__, no
drawing. A renderer (ansi.py for the console, htmlview.py for the web) turns them into its own text. A component that shows a value
carries a state ('ok', 'warn', 'err', 'down', 'unknown', 'info'); a value that could not be read is 'unknown' and drawn as '?', never as
fine. Text that came from the machine is cleaned (safe()) when the component is built.

Nothing here reads the configuration, the clock or the host.
"""
import math
import re
import unicodedata

# what a colour means. A symbol (✔ ✖ !) always goes with it: colour alone is never the only signal.
TOKENS = ("ok", "warn", "err", "unknown", "info", "muted", "accent", "strong", "banner_ok", "banner_err", "banner_warn", "sel",
          "accent_strong", "err_strong", "neutral")

_DEFAULT = {
    "ok": "32",                # fine: a green dot, a bar under its warning level
    "warn": "33",              # needs a look
    "err": "31",               # needs action
    "unknown": "33",           # could not be read: shown as '?', never as fine
    "info": "90",              # a note, nothing to do
    "muted": "90",             # labels, separators, what is not the point of the line
    "accent": "36",            # section rules, a filtered port: the console's own colour
    "strong": "1",             # bold
    "banner_ok": "1;7",        # the header pill when nothing is wrong
    "banner_err": "1;41;37",   # the header pill with problems
    "banner_warn": "1;43;30",  # the header pill with warnings only
    "sel": "7",                # the cursor row: reverse video
    "accent_strong": "1;36",   # section titles
    "err_strong": "1;31",      # the one thing that is public on the Internet
    "neutral": "37",           # a plain mark that is not a state (a service reachable only from this machine)
}
ANSI_THEMES = {
    "default": _DEFAULT,
    # a light terminal background: blue where the default writes cyan (cyan is unreadable on white), black where it writes white
    "light": dict(_DEFAULT, accent="34", accent_strong="1;34", neutral="30"),
    # high contrast: bold bright colours, nothing dim
    "hc": dict(_DEFAULT, ok="1;92", warn="1;93", err="1;91", unknown="1;93", info="1;97", muted="37", accent="1;96", strong="1;97",
               banner_ok="1;7", banner_err="1;41;97", banner_warn="1;103;30", accent_strong="1;96", err_strong="1;91", neutral="1;97"),
    # NO_COLOR: no colour at all, only bold and reverse video (the symbols carry the state)
    "mono": {"ok": "", "warn": "", "err": "1", "unknown": "", "info": "", "muted": "", "accent": "", "strong": "1", "banner_ok": "7",
             "banner_err": "1;7", "banner_warn": "1;7", "sel": "7", "accent_strong": "1", "err_strong": "1", "neutral": ""},
}


def sgr(token, theme="default"):
    """The SGR code (the 'm' sequence's parameters, '' = none) of a token in an ANSI theme."""
    return ANSI_THEMES[theme][token]


# the same meanings as colours for the web. dark is the palette htmlview.py writes today; light and hc are for the themes to come.
# bg/fg: the page; strong: the brightest text; muted/info: dim text; ok/warn/err/unknown/accent/link/magenta: the colours;
# border/panel/sel_bg: lines, hovered rows and the selected row; banner_*: the header pill (its background and text); rv_*: reverse video.
CSS_THEMES = {
    "dark": {
        "bg": "#0d1117", "fg": "#c9d1d9", "strong": "#f0f6fc", "muted": "#6e7681", "info": "#8b949e",
        "ok": "#3fb950", "warn": "#d29922", "err": "#ff7b72", "unknown": "#d29922", "accent": "#39c5cf", "link": "#58a6ff",
        "magenta": "#bc8cff", "border": "#30363d", "panel": "#161b22", "sel_bg": "#1f2a3a",
        "banner_err": "#da3633", "banner_err_fg": "#f0f6fc", "banner_warn": "#d29922", "banner_warn_fg": "#0d1117",
        "rv_bg": "#c9d1d9", "rv_fg": "#0d1117",
    },
    "light": {
        "bg": "#ffffff", "fg": "#1f2328", "strong": "#000000", "muted": "#57606a", "info": "#6e7781",
        "ok": "#1a7f37", "warn": "#9a6700", "err": "#cf222e", "unknown": "#9a6700", "accent": "#0969da", "link": "#0969da",
        "magenta": "#8250df", "border": "#d0d7de", "panel": "#f6f8fa", "sel_bg": "#ddf4ff",
        "banner_err": "#cf222e", "banner_err_fg": "#ffffff", "banner_warn": "#bf8700", "banner_warn_fg": "#1f2328",
        "rv_bg": "#1f2328", "rv_fg": "#ffffff",
    },
    "hc": {
        "bg": "#000000", "fg": "#ffffff", "strong": "#ffffff", "muted": "#c8c8c8", "info": "#e0e0e0",
        "ok": "#3dff6e", "warn": "#ffd400", "err": "#ff5c5c", "unknown": "#ffd400", "accent": "#00e1ff", "link": "#7ad0ff",
        "magenta": "#ff80ff", "border": "#ffffff", "panel": "#1a1a1a", "sel_bg": "#003a5c",
        "banner_err": "#ff5c5c", "banner_err_fg": "#000000", "banner_warn": "#ffd400", "banner_warn_fg": "#000000",
        "rv_bg": "#ffffff", "rv_fg": "#000000",
    },
}

THERMAL_WARN, THERMAL_ERR = 0.8, 0.9  # fractions of the maximum a sensor declares (sysfs temp*_max): where a temperature warns / errs

# ---- text helpers: no colour, no clock, no host -------------------------------------------------------------------------

CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe(s):
    """External data (comm, labels, docker stderr) is untrusted: no escapes/newlines on the physical console."""
    return CTRL.sub("?", str(s))


def plural(n, word):
    """'1 rule', '2 rules': English count + noun (regular plurals only)."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def human(nbytes):
    if nbytes is None:
        return "-"
    return f"{nbytes / 2**30:.1f}G" if nbytes >= 2**30 else f"{nbytes / 2**20:.0f}M"


def fmt_dur(sec):
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h" if d else f"{h}h {r // 60}m"


def fmt_min(sec):
    return f"{sec / 60:.1f} min" if sec >= 60 else f"{sec:.0f} s"


def fmt_ago(sec):
    sec = max(sec, 0)  # clocks out of sync must not produce "-5 s"
    return f"{sec:.0f} s" if sec < 90 else f"{sec / 60:.0f} min" if sec < 5400 else f"{sec / 3600:.0f} h"


def fmt_rate(bps):
    for unit in ("B", "kB", "MB", "GB"):
        if bps < 1000 or unit == "GB":
            return f"{bps:.0f} B/s" if unit == "B" else f"{bps:.1f} {unit}/s"
        bps /= 1000


def num(x):
    """x as a float when it is a finite number (a bool is not one), else None: what a producer hands over is data, not a promise."""
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def qf(x, spec=".0f", unit=""):
    """A number formatted, or '?' when it is not one."""
    x = num(x)
    return "?" if x is None else format(x, spec) + unit


def dget(d, *keys):
    """d[k1][k2]... or None when any step is missing or not a dict."""
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def dd(x):
    """x when it is a dict, else an empty one: a producer's section that is not what the contract says is a section with nothing in it."""
    return x if isinstance(x, dict) else {}


def idict(d):
    """{int: value} of a dict whose keys may be digits or strings (JSON keys are always strings); other keys are dropped."""
    out = {}
    for k, v in (d.items() if isinstance(d, dict) else ()):
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            pass
    return out


def fmt_size(b):
    b = num(b)
    if b is None:
        return "?"
    for unit, div in (("G", 2 ** 30), ("M", 2 ** 20), ("K", 2 ** 10)):
        if b >= div:
            v = b / div
            return (f"{v:.0f}" if v >= 10 or v == int(v) else f"{v:.1f}") + unit
    return f"{b:.0f}B"


def fmt_k(x):
    """Events per second: 842, 18.3k, 1.2M."""
    x = num(x)
    return "?" if x is None else f"{x:.0f}" if x < 1000 else f"{x / 1e3:.1f}k" if x < 1e6 else f"{x / 1e6:.1f}M"


def fmt_cputime(sec):
    """CPU seconds as htop writes them: 6:52.3, 5:03:53, 123h05m."""
    sec = num(sec)
    if sec is None:
        return "?"
    sec = max(sec, 0.0)
    if sec < 3600:
        return f"{int(sec // 60)}:{sec % 60:04.1f}"
    h, r = divmod(int(sec), 3600)
    return f"{h}:{r // 60:02d}:{r % 60:02d}" if h < 100 else f"{h}h{r // 60:02d}m"


def hclean(s, n=0):
    """Text of the report (an app, a unit, a message template: all names the history took from the machine) as one plain line: control
    and format characters, and wide characters (they would break the columns), become '?'; at most n characters (0: no limit)."""
    out = []
    for ch in safe("" if s is None else s):
        cat = unicodedata.category(ch)
        out.append(" " if cat in ("Zl", "Zp") else "?" if cat[0] == "C" or unicodedata.east_asian_width(ch) in "WF" else ch)
    t = "".join(out)
    return t[:n - 1] + "…" if n and len(t) > n else t


def hnum(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v and abs(v) != float("inf") else default


def hcount(n):
    n = hnum(n)
    return f"{n:.0f}" if n < 1000 else f"{n / 1000:.1f}k" if n < 10000 else f"{n / 1000:.0f}k" if n < 1e6 else f"{n / 1e6:.1f}M"


# ---- components: what a screen is made of ---------------------------------------------------------------------------------
# Plain classes with __slots__ (a frame builds hundreds), equal when their fields are. The constructors clean what the machine wrote
# and refuse what has no meaning (a state that does not exist); they hold no colour: a tone is a token of TOKENS, a state is one of STATES.

STATES = ("ok", "warn", "err", "down", "unknown", "info")
SYMBOLS = {"ok": "✔", "warn": "!", "err": "✖", "down": "✖", "unknown": "?", "info": "·"}  # the mark that goes with a state besides its colour
LEVELS = ("ok", "warn", "err", "info")  # what a Msg / Notice says (the console's msg())
_SEVERITY = {"ok": 0, "info": 0, "unknown": 1, "warn": 2, "err": 3, "down": 3}


def check_state(state):
    """state, when it is one of STATES; ValueError otherwise (there is no default: a value without a state does not exist)."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}: one of {', '.join(STATES)}")
    return state


def worst(states):
    """The most serious of some states ('ok' for none): err and down, then warn, then unknown, then ok and info."""
    best = "ok"
    for s in states:
        if _SEVERITY[check_state(s)] > _SEVERITY[best]:
            best = s
    return best


class _Component(object):
    __slots__ = ()

    def _fields(self):
        return tuple(getattr(self, n) for n in self.__slots__)

    def __eq__(self, other):
        return type(other) is type(self) and self._fields() == other._fields()

    __hash__ = None

    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, ", ".join("%s=%r" % (n, getattr(self, n)) for n in self.__slots__))


def _text(x):
    return safe("" if x is None else x)


def _tone(tone):
    if tone is not None and tone not in TOKENS:
        raise ValueError(f"unknown tone {tone!r}")
    return tone


def _inline(x):
    """What fits in a line or a table cell: a Span, a Line, a Bar, a Spark, a Meter or a Series; anything else is text."""
    return x if isinstance(x, (Span, Line, Bar, Spark, Meter, Series)) else Span(x)


class Span(_Component):
    """A piece of text: its tone (a token of TOKENS, or None for the surface's own colour), bold, mono (a number or a path). full: the
    whole text when `text` is what fits a console column (cut with '…'): the web has room and draws full, the console draws text."""
    __slots__ = ("text", "tone", "bold", "mono", "full")

    def __init__(self, text, tone=None, bold=False, mono=False, full=None):
        self.text, self.tone, self.bold, self.mono = _text(text), _tone(tone), bool(bold), bool(mono)
        self.full = None if full is None else _text(full)


class Line(_Component):
    """One line of Spans (strings are made Spans); a Bar, a Spark, a Series or another Line may sit in it between the text. No spans at all is
    a blank line on the console and nothing on the web. clip: the console cuts the line to that many columns, wherever it sits (None: as it
    is); the web ignores it."""
    __slots__ = ("spans", "clip")

    def __init__(self, spans=(), clip=None):
        self.spans = [x if isinstance(x, (Span, Bar, Spark, Meter, Series, Line)) else Span(x)
                      for x in ([spans] if isinstance(spans, (str, Span, Bar, Spark, Meter, Series, Line)) else spans)]
        self.clip = None if clip is None else int(clip)

    @property
    def text(self):
        return "".join(getattr(s, "text", "") for s in self.spans)


class Raw(_Component):
    """The lines of a block drawn the old way, ANSI included: what a card holds until it is built of components. A console draws them
    as they are; they carry no state of their own (the Card around them does)."""
    __slots__ = ("lines",)

    def __init__(self, lines=()):
        self.lines = list(lines)


class More(_Component):
    """'... +N more': what a card left out, and where all of it is (href: a web page, None on the console, which has its Details pages).
    indent: the columns the console puts before it, inside its colour (None: one, outside it); the web ignores it."""
    __slots__ = ("n", "what", "href", "indent")

    def __init__(self, n, what="more", href=None, indent=None):
        self.n, self.what, self.href = max(0, int(n)), _text(what), href
        self.indent = None if indent is None else int(indent)

    @property
    def text(self):
        return f"… +{self.n} {self.what}"


class Card(_Component):
    """One section of the overview: id (one of nuc_config.SECTIONS), title, note (the title's remark), state, body (components),
    more (a More, when it left items out), size (columns wide on the web, 1-4) and truncated (it hid items on the console)."""
    __slots__ = ("id", "title", "note", "state", "body", "more", "size", "truncated")

    def __init__(self, id, title, note, state, body, more=None, size=1, truncated=False):
        self.id, self.title, self.note, self.state = id, _text(title), _text(note), check_state(state)
        self.body, self.more, self.truncated = list(body), more, bool(truncated)
        self.size = size if isinstance(size, int) and not isinstance(size, bool) and 1 <= size <= 4 else 1

    @property
    def lines(self):
        """The ANSI lines of the Raw parts of the body, in order (what a console that has not been taught the other components draws)."""
        return [x for part in self.body if isinstance(part, Raw) for x in part.lines]


class Kpi(_Component):
    """One figure of the row under the header: label, value and unit as text, its state and the symbol that goes with it. A value that
    is not known is state 'unknown' and value '?' (whatever was passed), never fine. spark: a series of numbers, or None."""
    __slots__ = ("id", "label", "value", "unit", "state", "symbol", "hint", "spark")

    def __init__(self, id, label, value, unit, state, symbol=None, hint="", spark=None):
        self.id, self.label, self.state = id, _text(label), check_state(state)
        self.value, self.unit = ("?", "") if state == "unknown" else (_text(value), _text(unit))
        self.symbol = SYMBOLS[state] if symbol is None else _text(symbol)
        self.hint = _text(hint)
        self.spark = None if spark is None else Spark(spark)


class Col(_Component):
    """A column of a Table: key, label, align ('l' | 'r' | 'c' centred), prio (0 = never dropped; a bigger number is dropped sooner when
    the room is short), num (the cells are numbers: mono, aligned). w, gap and clip are the console's: w is the columns the cell is
    padded to (None: the text as it is, no padding), gap the spaces after the column, clip the columns the cell is cut to (None: not
    cut), pad_in (the padding of a Span cell is inside its colour: a coloured cell is as wide as the column). The web ignores all four.
    wprio: the web's own priority (None: prio), by the width of the card it is drawn in: the console's prio is tuned to its columns, a
    card of the grid has its own sizes.
    A column the rows can be sorted by has href (the link of its label; None: not a link), hkey (the keys that do the same, as the
    keymap writes them: 'p P') and, on the one the rows are in the order of, sort: 'desc' or 'asc' (the console writes an arrow after
    the label), or 'mark' for a second column that belongs to the same order (no arrow, no aria-sort)."""
    __slots__ = ("key", "label", "align", "prio", "num", "w", "gap", "clip", "pad_in", "href", "hkey", "sort", "wprio")

    def __init__(self, key, label, align="l", prio=0, num=False, w=None, gap=1, clip=None, pad_in=False, href=None, hkey="", sort=None, wprio=None):
        self.key, self.label, self.align, self.prio, self.num = key, _text(label), align if align in ("r", "c") else "l", int(prio), bool(num)
        self.wprio = None if wprio is None else int(wprio)
        self.w, self.gap = None if w is None else int(w), int(gap)
        self.clip = None if clip is None else int(clip)
        self.pad_in, self.href, self.hkey = bool(pad_in), href, _text(hkey)
        if sort not in (None, "asc", "desc", "mark"):
            raise ValueError(f"unknown sort {sort!r}: asc, desc or mark")
        self.sort = sort


class Row(_Component):
    """A row of a Table: one cell per column (Span, Line or str), a tone for the whole row, a stable key, a link."""
    __slots__ = ("cells", "tone", "key", "href")

    def __init__(self, cells, tone=None, key=None, href=None):
        self.cells = [_inline(c) for c in cells]
        self.tone, self.key, self.href = _tone(tone), key, href


class Table(_Component):
    """cols [Col], rows [Row] (one cell per column), groups: None or [(label, first row index)]: the rows from there on belong to it.
    head: the console draws the labels as a first row (the web always has them in <thead>).
    The rest is the console's own (the web ignores it): indent (the columns before the first one), head_line (the header drawn bold, as this
    text, in place of the labels), titled (each group is a bold title with its row count after a blank line; the web adds the count),
    fill (a row with a tone is drawn in that colour from end to end, the spaces between the cells too), head_tone (the labels are drawn in
    that tone, the one of a sorted column warn and bold, with its arrow: the labels are padded inside their colour) and solid (a row with a
    tone is cut to that many columns, stripped of the colours of its cells and padded to them: the cursor row of a list) and fit (each row on
    its own: a row wider than the console loses its last cells one by one, at least one stays, instead of the columns with a prio going from
    every row; a cell with no text and no w is not there at all, its gap neither, and a cell with a w is padded to it even when it is the last
    one).
    A row with a link (Row.href) is a row of a list: the web marks it data-row, and the one with the tone 'sel' aria-current."""
    __slots__ = ("cols", "rows", "groups", "head", "indent", "head_line", "titled", "fill", "head_tone", "solid", "fit")

    def __init__(self, cols, rows=(), groups=None, head=False, indent=1, head_line=None, titled=False, fill=False, head_tone=None, solid=None, fit=False):
        self.head = bool(head) or head_line is not None
        self.indent, self.head_line, self.titled, self.fill = int(indent), None if head_line is None else _text(head_line), bool(titled), bool(fill)
        self.head_tone, self.solid = _tone(head_tone), None if solid is None else int(solid)
        self.fit = bool(fit)
        self.cols, self.rows = list(cols), list(rows)
        for r in self.rows:
            if len(r.cells) != len(self.cols):
                raise ValueError(f"a row has {len(r.cells)} cells for {len(self.cols)} columns")
        self.groups = None if groups is None else [(_text(label), int(i)) for label, i in groups]


class KV(_Component):
    """Labels and values: pairs of (label, Span / Line / str) and the label column's width on the console. The rest is the console's (the
    web ignores it): indent (the columns before the label), wrap (a long value goes on under itself at the width of the room; the values
    are plain text) and cols (the pairs in that many columns, filled one after the other, each cut to its share of the room)."""
    __slots__ = ("pairs", "lw", "indent", "wrap", "cols")

    def __init__(self, pairs=(), lw=13, indent=3, wrap=False, cols=1):
        self.pairs = [(_text(k), _inline(v)) for k, v in pairs]
        self.lw, self.indent, self.wrap, self.cols = int(lw), int(indent), bool(wrap), max(1, int(cols))


class Bar(_Component):
    """A fraction of a whole (0..1): its state is read off the thresholds (warn, err), and a fraction that is None (it could not be
    read) is state 'unknown' with the text '?': never an empty bar that looks fine. w: the console's width in columns (the web sizes it
    in CSS). tone: a token that colours the fill whatever the fraction is (a share of a total, not a level); None follows the state."""
    __slots__ = ("frac", "value_text", "warn", "err", "w", "tone")

    def __init__(self, frac, value_text="", warn=0.7, err=0.9, w=10, tone=None):
        self.tone = _tone(tone)  # None: the colour follows the state; a token: a bar that is not a level (the slowest units, a share)
        self.w = max(1, int(w))
        f = num(frac)
        self.frac = None if f is None else min(max(f, 0.0), 1.0)
        self.value_text = "?" if self.frac is None else _text(value_text)
        self.warn, self.err = warn, err

    @property
    def state(self):
        return "unknown" if self.frac is None else "ok" if self.frac < self.warn else "warn" if self.frac < self.err else "err"


class Spark(_Component):
    """A series of numbers, oldest first; what is not a finite number is dropped. w: the last w values the console draws; floor: the
    top of the scale is never below it, so that noise is not blown up (the default is a rate in bytes per second)."""
    __slots__ = ("values", "w", "floor")

    def __init__(self, values=(), w=8, floor=1024):
        self.values = [v for v in (num(x) for x in values) if v is not None]
        self.w, self.floor = max(1, int(w)), floor


class Pill(_Component):
    """A short label with a state: 'ALL OK', 'N PROBLEMS'."""
    __slots__ = ("text", "state")

    def __init__(self, text, state):
        self.text, self.state = _text(text), check_state(state)


class Msg(_Component):
    """A status line: level 'ok' | 'warn' | 'err' | 'info' (the symbol goes with it) and its text."""
    __slots__ = ("level", "text")

    def __init__(self, level, text):
        if level not in LEVELS:
            raise ValueError(f"unknown level {level!r}: one of {', '.join(LEVELS)}")
        self.level, self.text = level, _text(text)


class Notice(Msg):
    """A message about the screen, not about a thing in it (stale data, a lock): the shape of a Msg."""
    __slots__ = ()


class Wrap(_Component):
    """Items that flow over several lines without being split (max_lines: then '... +N' ends the last one; indent: the console's).
    lead: a label (a Span) before the first item: the console puts it in the indent of the first line (one column in), the web before the list.
    flat: the web draws the items as plain text side by side (counters, a legend) instead of as chips; the console ignores it."""
    __slots__ = ("items", "sep", "max_lines", "indent", "lead", "flat")

    def __init__(self, items=(), sep="  ·  ", max_lines=None, indent=1, lead=None, flat=False):
        self.items, self.sep, self.max_lines, self.indent = [_inline(x) for x in items], sep, max_lines, int(indent)
        self.lead = None if lead is None else _inline(lead)
        self.flat = bool(flat)


class Group(_Component):
    """Children drawn together under an optional title."""
    __slots__ = ("children", "title")

    def __init__(self, children=(), title=""):
        self.children, self.title = list(children), _text(title)


class Tree(_Component):
    """Rows of (depth, Span / Line / str, state): a tree as a list, each node with its state."""
    __slots__ = ("rows",)

    def __init__(self, rows=()):
        self.rows = [(int(d), _inline(x), check_state(s)) for d, x, s in rows]


class Details(_Component):
    """A part that is folded until asked for: summary (Line / Span / str) and body (components). brief: what the console draws in its
    place (a Line / Span / str, as it is) when it has one line to say it in (None: the usual '▸ summary', and the body when open)."""
    __slots__ = ("summary", "body", "open", "brief")

    def __init__(self, summary, body=(), open=False, brief=None):
        self.summary, self.body, self.open = _inline(summary), list(body), bool(open)
        self.brief = None if brief is None else _inline(brief)


class RichMsg(Msg):
    """A Msg whose text has its own emphasis: rich (a Line of Spans), the same words as text. Both surfaces draw rich; text is its plain
    form, for whoever has no use for the emphasis."""
    __slots__ = ("rich",)

    def __init__(self, level, rich):
        rich = rich if isinstance(rich, Line) else Line(rich)
        Msg.__init__(self, level, rich.text)
        self.rich = rich

    def _fields(self):
        return (self.level, self.text, self.rich)


class Problem(Msg):
    """One problem of ATTENTION, drawn as a Msg (the console wraps a long text at its commas under it). The rest is for the web, which has the
    room: id (its stable name, 'db-open-lan'), title (what it is), why (why it matters), fix (how to handle it: the command in the words of
    this OS and of this install) and accept (the command that accepts it as known, '' when it cannot be: a port change is accepted with the
    baseline, and that command is in fix)."""
    __slots__ = ("id", "title", "why", "fix", "accept")

    def __init__(self, level, text, id="", title="", why="", fix="", accept=""):
        Msg.__init__(self, level, text)
        self.id, self.title, self.why, self.fix, self.accept = _text(id), _text(title), _text(why), _text(fix), _text(accept)

    def _fields(self):
        return (self.level, self.text, self.id, self.title, self.why, self.fix, self.accept)


class Accepted(Msg):
    """A problem that was accepted as known (level 'info'): id, the reason it was given, when (text: '3 h ago', '' when it is not known) and
    undo (the command that forgets it). The console never draws one (it says how many there are, ATTENTION's Details has the line)."""
    __slots__ = ("id", "reason", "when", "undo")

    def __init__(self, text, id="", reason="", when="", undo=""):
        Msg.__init__(self, "info", text)
        self.id, self.reason, self.when, self.undo = _text(id), _text(reason), _text(when), _text(undo)

    def _fields(self):
        return (self.level, self.text, self.id, self.reason, self.when, self.undo)


class Hint(_Component):
    """A command the reader may run, and what it does: the web draws 'label: <command>'; the console draws nothing (it has its own keys
    and its footer)."""
    __slots__ = ("label", "cmd")

    def __init__(self, label, cmd):
        self.label, self.cmd = _text(label), _text(cmd)


# ---- components for the system, container, database and boot cards ----------------------------------------------------------------
# (cards.py: system_card, containers_card, databases_card, boot_card; ansi.py and htmlview.py draw them in their own blocks)

def fmt_s(x):
    """Seconds as the boot blocks write them: '12.3s'."""
    return f"{x:.1f}s"


class Head(_Component):
    """The heading of a part of a card (the console's section rule, a <h3> on the web): title and an optional note."""
    __slots__ = ("title", "note")

    def __init__(self, title, note=""):
        self.title, self.note = _text(title), _text(note)


class Indent(_Component):
    """Children the console draws n columns further in (the web ignores it: its CSS indents)."""
    __slots__ = ("children", "n")

    def __init__(self, children=(), n=2):
        self.children, self.n = list(children), max(0, int(n))


class Grid(_Component):
    """Items of one line each (a Line with a Bar: one CPU core) in columns: the console pads each to cw columns and puts per of them on
    a line; the web lays them out in a CSS grid. fit (the console's): each item is cut to cw columns too, lead spaces come before a line,
    gap spaces between its items, and the blanks at its end are dropped."""
    __slots__ = ("items", "cw", "per", "gap", "lead", "fit")

    def __init__(self, items=(), cw=20, per=1, gap=0, lead=0, fit=False):
        self.items, self.cw, self.per = [_inline(x) for x in items], max(1, int(cw)), max(1, int(per))
        self.gap, self.lead, self.fit = max(0, int(gap)), max(0, int(lead)), bool(fit)


class Timeline(_Component):
    """Stacked segments of a whole: parts [(name, seconds)] drawn side by side in proportion (the stages of a boot), then a legend.
    total: the whole in seconds (what the segments are a share of); w: the console's width in columns."""
    __slots__ = ("parts", "total", "w")

    def __init__(self, parts=(), total=1.0, w=40):
        self.parts = [(_text(n), v) for n, v in ((n, num(v)) for n, v in parts) if v is not None]
        self.total, self.w = max(num(total) or 0.0, 0.001), max(1, int(w))


class NoteTable(_Component):
    """A Table whose rows may have lines under them: notes[i] is the list of components (a Line, a Flow) that belong to row i. The
    console puts them right under the row, the web in a row of their own that spans the table."""
    __slots__ = ("cols", "rows", "notes", "head")

    def __init__(self, cols, rows=(), notes=None, head=False):
        self.cols, self.rows, self.head = list(cols), list(rows), bool(head)
        for r in self.rows:
            if len(r.cells) != len(self.cols):
                raise ValueError(f"a row has {len(r.cells)} cells for {len(self.cols)} columns")
        self.notes = [list(x) for x in (notes or [])] + [[] for _ in range(len(self.rows) - len(notes or []))]


class Flow(_Component):
    """Facts that flow over several lines without being split, after a lead mark (a Span such as '→') on the first line. cut: the console
    cuts each fact to that many columns."""
    __slots__ = ("items", "lead", "indent", "sep", "cut")

    def __init__(self, items=(), lead=None, indent=8, sep="   ", cut=None):
        self.items, self.lead = [_inline(x) for x in items], None if lead is None else _inline(lead)
        self.indent, self.sep, self.cut = max(2, int(indent)), sep, None if cut is None else int(cut)


# ---- components of the full screens (screens.py) -----------------------------------------------------------------------------------

METER_KINDS = ("user", "system", "other", "busy", "iowait")


class Meter(_Component):
    """A whole in parts drawn side by side, the rest idle (one CPU: user, system, other work, I/O wait): parts [(percent, kind)] with
    kind one of METER_KINDS, in the order they are drawn; w: the console's width in columns (the web sizes it in CSS). No parts at all is
    an idle meter: what could not be read is drawn '?' next to it by whoever builds the line."""
    __slots__ = ("parts", "w")

    def __init__(self, parts=(), w=10):
        self.parts = []
        for pct, kind in parts:
            if kind not in METER_KINDS:
                raise ValueError(f"unknown kind {kind!r}: one of {', '.join(METER_KINDS)}")
            self.parts.append((max(num(pct) or 0.0, 0.0), kind))
        self.w = max(1, int(w))


class Cap(_Component):
    """Children that take at most n lines of the console: when they are more, the last line says how many were left out ('... +3 more
    <what>'); what None cuts without saying. The web draws every child: it has the room."""
    __slots__ = ("children", "n", "what")

    def __init__(self, children=(), n=0, what="lines"):
        self.children, self.n, self.what = list(children), max(0, int(n)), None if what is None else _text(what)


class Only(_Component):
    """Children one surface draws and the other does not: surface 'console' or 'web' (the key figures of the web have the header lines of
    the console as their counterpart)."""
    __slots__ = ("surface", "children")

    def __init__(self, surface, children=()):
        if surface not in ("console", "web"):
            raise ValueError(f"unknown surface {surface!r}: console or web")
        self.surface, self.children = surface, list(children)


class Tiles(_Component):
    """A row of key figures (Kpi) of a screen: the web draws them as tiles, the console nothing (it writes the same facts as text)."""
    __slots__ = ("items",)

    def __init__(self, items=()):
        self.items = list(items)

# ---- components of the full screens (screens.py: the Health screen) ------------------------------------------------------------------
# What a screen has that a card has not: a heading with a choice of periods, a list of findings that open, the advisor's answer, parts side
# by side. The console fields (w, h, clip, fit ...) are the console's own and the web ignores them, as in the components above.

class Seg(_Component):
    """A choice between a few options, one of them chosen (a period): options [(text, key, chosen, href)]. key is the key that picks the
    option (the keymap's), href where the web goes (None: no link). The console draws ' d:24h ' for each, the chosen one in reverse video;
    the web a group of links with data-key and aria-current."""
    __slots__ = ("label", "options")

    def __init__(self, label, options=()):
        self.label = _text(label)
        self.options = [(_text(t), _text(k), bool(c), None if h is None else str(h)) for t, k, c, h in options]


class Title(_Component):
    """The heading of a screen: its label, parts (Span / Line: what it is about, and the figures) and seg (a Seg, or None). The console draws
    '-- LABEL  part · part ------ seg' on one line and, when it is too wide for the console, drops the parts from the second on, one by one,
    last of them first, until it fits (the first one always stays).
    legend (a Legend, or None) goes where the console puts the seg when there is none: right-aligned, its last items dropped first while the
    line is too wide. segs: more choices after seg (the web draws all of them, in a row; the console only draws seg)."""
    __slots__ = ("label", "parts", "seg", "legend", "segs")

    def __init__(self, label, parts=(), seg=None, legend=None, segs=()):
        self.label, self.parts, self.seg = _text(label), [_inline(p) for p in parts], seg
        self.legend, self.segs = legend, list(segs)


class Legend(_Component):
    """What the signs of a drawing mean: items [(glyph, word, tone)], the glyph drawn in the tone (None: the surface's own colour) and the
    word muted. The console writes 'glyph word' for each, two spaces between."""
    __slots__ = ("items",)

    def __init__(self, items=()):
        self.items = [(_text(g), _text(w), _tone(t)) for g, w, t in items]


class Series(_Component):
    """Values over time, oldest first, None where nothing was recorded (a gap, not a zero), scaled to their own maximum: the console draws
    them as small bars w columns wide (the mean of each group when there are more values than columns), the web as an SVG of bars. tone:
    the token that colours it."""
    __slots__ = ("values", "w", "tone")

    def __init__(self, values=(), w=10, tone="accent"):
        self.values = [None if v is None else hnum(v, None) for v in values]
        self.w, self.tone = max(1, int(w)), _tone(tone)


class Cols(_Component):
    """Parts side by side: children [(node, w)]. The console draws each in its w columns (every line cut to them), gap spaces between, and a
    column that is longer than h lines is cut there (the last line says how many were left out; h None: no cut). The web lays the children
    out in a grid, in the order given, whatever the w."""
    __slots__ = ("children", "gap", "h")

    def __init__(self, children=(), gap=3, h=None):
        self.children, self.gap, self.h = [(n, None if w is None else int(w)) for n, w in children], int(gap), None if h is None else int(h)


class Split(_Component):
    """Two parts with a rule between them, the console's side by side: left and right [nodes]; left is drawn lw columns wide, sep stands
    between the two; with h the two are as tall as h lines (the left made as tall, the right cut to it), without as the taller of them. The web
    draws both, one after the other (its CSS puts them next to each other when there is room, and stacks them in a narrow window)."""
    __slots__ = ("left", "right", "lw", "h", "sep")

    def __init__(self, left=(), right=(), lw=40, h=0, sep=" \u2502 "):
        self.left, self.right, self.lw, self.h, self.sep = list(left), list(right), int(lw), int(h), sep


class Pane(_Component):
    """Everything known about one finding: level (err, warn, info), title, what (the text), facts [(name, value)] and fix. h: the console
    cuts it to that many lines (the last says how many were left out; None: not cut)."""
    __slots__ = ("level", "title", "what", "facts", "fix", "h")

    def __init__(self, level, title, what, facts=(), fix="", h=None):
        self.level, self.title, self.what, self.fix = check_level(level), _text(title), _text(what), _text(fix)
        self.facts, self.h = [(_text(k), _text(v)) for k, v in facts], None if h is None else int(h)


class Finding(_Component):
    """One finding of a report: id (stable), level (err, warn, info), title, text (one line of what it is) and detail (a Pane, or None).
    cursor: the console draws the row in reverse video (the keyboard's cursor); open: the web has its details open. The console's row is
    ' [LEVEL] title  text', the text as far as it fits."""
    __slots__ = ("id", "level", "title", "text", "detail", "cursor", "open")

    def __init__(self, id, level, title, text, detail=None, cursor=False, open=False):
        self.id, self.level, self.title, self.text = _text(id), check_level(level), _text(title), _text(text)
        self.detail, self.cursor, self.open = detail, bool(cursor), bool(open)


class Advice(_Component):
    """The advisor's answer under the findings: head (its first line), paras (paragraphs, each a list of lines), notes ([(kind, text)]:
    'cites' and 'tools'), kind ('advice', 'shared' for an older answer, 'error', 'none' when there is none yet). The console draws lines
    (ANSI text, the advisor's own wrapping and cut) under a section rule; the web draws the parts as a highlighted block."""
    __slots__ = ("head", "paras", "notes", "kind", "lines")

    def __init__(self, head="", paras=(), notes=(), kind="advice", lines=()):
        self.head, self.paras = _text(head), [[_text(x) for x in p] for p in paras]
        self.notes, self.kind, self.lines = [(_text(k), _text(t)) for k, t in notes], kind if kind in ("advice", "shared", "error", "none") else "advice", list(lines)


# ---- components of the Map screen (screens.py): an outline of rows that open and close, and what is known of one of them ------------------

class Branch(_Component):
    """One row of an Outline: key (stable: the same path is the same key at every refresh), depth, mark (a Span: the sign that says the row
    opens, is open, is a leaf, or repeats one above), body (a Line: what the row says, from its arrow to its note), state (the node's: its
    symbol and class on the web), href (where the web goes to select the row; None: no link) and mark_href (to open or close it; None: the
    mark is no link). cursor: the keyboard's row (reverse video on the console, aria-current on the web). prefix: the console's tree glyphs
    before the mark (the web has the depth); tip: what the mark's link says on the web."""
    __slots__ = ("key", "depth", "mark", "body", "state", "href", "mark_href", "cursor", "prefix", "tip")

    def __init__(self, key, depth, mark, body, state="info", href=None, mark_href=None, cursor=False, prefix="", tip=""):
        self.key, self.depth, self.mark, self.body = _text(key), int(depth), _inline(mark), _inline(body)
        self.state, self.href, self.mark_href = check_state(state), href, mark_href
        self.cursor, self.prefix, self.tip = bool(cursor), _text(prefix), _text(tip)


class Outline(_Component):
    """Rows (Branch) of a tree drawn as a list, one line each: the console cuts every row to its width (the cursor's row is padded to it, in
    reverse video), the web draws a list whose rows are links, deeper ones indented by their depth."""
    __slots__ = ("rows",)

    def __init__(self, rows=()):
        self.rows = list(rows)


class Props(_Component):
    """Everything known about one thing: title and items [(label, value, level)], level one of err, warn, ok, info or '' (a label that starts with
    spaces belongs to the one above; the first item is the thing itself, in bold). h: the console cuts it to that many lines (the last says how
    many were left out; None: not cut). close: where the web goes to close it (None: no link)."""
    __slots__ = ("title", "items", "h", "close")

    def __init__(self, title, items=(), h=None, close=None):
        self.title = _text(title)
        self.items = [(_text(k), _text(v), lv if lv in ("err", "warn", "ok", "info") else "") for k, v, lv in items]
        self.h, self.close = None if h is None else int(h), close


def check_level(level):
    """level, when it is err, warn or info (what a finding has); ValueError otherwise."""
    if level not in ("err", "warn", "info"):
        raise ValueError(f"unknown level {level!r}: one of err, warn, info")
    return level


# ---- the keymap: the ONE table that drives the console's key dispatch, every footer and the `?` help overlay ----------------------
#
# Key names are the ones render.decode_keys / render.win_key produce: up down left right pgup pgdn home end tab btab enter esc space,
# or the character typed. Letters are local to a screen; digits and symbols are global.
#
# A row is (keys, scope, action, label, prio) plus how it is shown: `show` (the keys as the footer and the help print them), `short`
# (the label when the footer is narrow: None = the same, "" = none, the row is dropped first), `feat` (the [features] switch that has
# to be on, or "portable" for a portable console). prio: 0 = the last footer item to go when the width is short, a larger number goes
# before; None = not in the footers, only in the help.

class Key(object):
    """One row of KEYMAP (read only by convention)."""
    __slots__ = ("keys", "scope", "action", "label", "prio", "show", "short", "feat")

    def __init__(self, keys, scope, action, label, prio, show="", short=None, feat=""):
        self.keys, self.scope, self.action, self.label, self.prio, self.show, self.short, self.feat = keys, scope, action, label, prio, show, short, feat

    def __repr__(self):
        return f"Key({self.keys!r}, {self.scope!r}, {self.action!r})"


def _k(keys, scope, action, label, prio, show="", short=None, feat=""):
    return Key(tuple(keys.split()) if isinstance(keys, str) else tuple(keys), scope, action, label, prio, show, short, feat)


# scopes: global (every screen), overview, list (map cpu health ai), then one per screen; editor is the web layout editor (documented
# only: the console never dispatches it)
SCOPES = ("global", "overview", "list", "map", "cpu", "health", "ai", "editor")
LIST_SCREENS = ("map", "cpu", "health", "ai")
# the screens 1-5 open: (name, [features] switch that has to be on or "", the title in the help)
SCREENS = (("overview", "", "Overview"), ("map", "map", "Map"), ("cpu", "cpu", "CPU"), ("health", "health", "Health"), ("ai", "ai", "AI"))
HOLD_ALIAS = {"m": "map", "c": "cpu", "h": "health", "a": "ai"}  # the overview's old letters: they still open the screen

KEYMAP = (
    # -- every screen
    _k("1 2 3 4 5", "global", "screen", "screens", 6, "1-5"),
    _k("esc", "global", "back", "back", 0, "Esc"),
    _k("q", "global", "back", "back", None, "q"),
    _k("?", "global", "help", "help", 1, "?"),
    _k("r", "global", "redraw", "redraw now", None, "r"),
    _k("Z", "global", "pause", "pause/resume the redraw", None, "Z"),
    _k("tab", "global", "next", "next screen", None, "Tab"),
    _k("btab", "global", "prev", "previous screen", None, "Shift+Tab"),
    # -- the overview
    _k("left pgup", "overview", "slide-prev", "slide", 2, "←→", short="←→: slide"),
    _k("right pgdn", "overview", "slide-next", "next slide", None, "→"),
    _k("m", "overview", "open-map", "open the Map", None, "m", feat="map"),
    _k("c", "overview", "open-cpu", "open the CPU screen", None, "c", feat="cpu"),
    _k("h", "overview", "open-health", "open the Health screen", None, "h", feat="health"),
    _k("a", "overview", "open-ai", "open the AI screen", None, "a", feat="ai"),
    # -- every list (map, cpu, health, ai)
    _k("up down j k", "list", "move", "move", 2, "↑↓"),
    _k("pgup pgdn home end", "list", "page", "page", 9, "PgUp/PgDn/Home/End", short=""),
    _k("enter space", "list", "details", "details", 4, "Enter", short="details"),
    # -- map
    _k("left h", "map", "close", "close/open", 3, "←→", short="open"),
    _k("right l", "map", "open", "open", None, "→ l"),
    _k("e", "map", "expand", "expand/collapse all", 7, "e/c", short="all"),
    _k("c", "map", "collapse", "collapse all", None, "c"),
    _k("p", "map", "problems", "problems only", 5, "p", short="problems"),
    # -- cpu (upper case too: P and p are the same key)
    _k("p m t n u P M T N U", "cpu", "sort", "sort", 3, "p m t n u"),
    # -- health
    _k("d w m", "health", "period", "24h/7d/30d", 3, "d/w/m", short="period"),
    # -- ai (the keys that act are there unless [ai] web_actions = no locked them)
    _k("e", "ai", "toggle", "AI on/off", 3, "e", short="on/off"),
    _k("u", "ai", "use", "use model", 3.1, "u", short="use"),
    _k("x", "ai", "delete", "delete", 5, "x", short="del"),
    _k("X", "ai", "delete-all", "delete all", 6, "X", short="all"),
    _k("c", "ai", "cancel", "cancel", 5.1, "c"),
    _k("y n", "ai", "answer", "answer a question", None, "y/n"),
    # -- the web layout editor (?edit=1): documented here, drawn by the page
    _k("space", "editor", "grab", "grab or drop a card", None, "Space"),
    _k("up down left right", "editor", "nudge", "move it", None, "←↑↓→"),
    _k("+ -", "editor", "size", "bigger/smaller", None, "+ -"),
    _k("x", "editor", "hide", "hide the card", None, "x"),
    _k("esc", "editor", "cancel", "let go", None, "Esc"),
)

PRIO_LAST = 99  # the footer's own items (the console size, a note): they go before any key


def chain(scope):
    """The scopes a screen's keys come from, most specific first."""
    if scope in ("global", "editor"):  # the editor is a modal state of the web page: its Esc is its own
        return [scope]
    return [scope] + (["list"] if scope in LIST_SCREENS else []) + ["global"]


def rows(scope, enabled=None):
    """The rows in force on a screen, in the order a footer shows them (its list's, its own, the global ones; table order within each). enabled: a function
    (feature name) -> bool that gates the rows with a `feat` (default: all on)."""
    ok = enabled or (lambda f: True)
    order = (["list"] if scope in LIST_SCREENS else []) + chain(scope)[:1] + (["global"] if scope not in ("global", "editor") else [])
    return [r for s in order for r in KEYMAP if r.scope == s and (not r.feat or r.feat == "portable" or ok(r.feat))]


def lookup(scope, key):
    """The row a key does on a screen, or None: the screen's own row first, then its list's, then the global one."""
    for s in chain(scope):
        for r in KEYMAP:
            if r.scope == s and key in r.keys:
                return r
    return None


def action(scope, key, enabled=None):
    """The action a key does on a screen ('' = none). A row whose feature is off does nothing, as if it was not there."""
    r = lookup(scope, key)
    if r is None or (r.feat and r.feat != "portable" and enabled and not enabled(r.feat)):
        return ""
    return r.action


def screen_keys(enabled=None):
    """[(digit, screen name)] of the screens a digit opens now: a disabled feature has no digit's action and is not shown."""
    ok = enabled or (lambda f: True)
    return [(str(i + 1), name) for i, (name, feat, _t) in enumerate(SCREENS) if not feat or ok(feat)]


def screens_show(enabled=None):
    """'1-5', or '1 3-5' when a screen is off: the digits that do something."""
    nums, runs, i = [int(d) for d, _n in screen_keys(enabled)], [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        runs.append(str(nums[i]) if i == j else " ".join(map(str, nums[i:j + 1])) if j == i + 1 else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return " ".join(runs)


def fit(lead, items, w):
    """The footer's plain text: lead + the items, each (prio, long, short), in their order. When it does not fit w columns the short
    forms are used (an item without one goes), then the items with the largest prio go one by one (the last of equals first).
    The footer's own items (the console size...) come with a large prio (PRIO_LAST): they are the first to go. Returns the text; one
    item is always kept."""
    items = list(items)
    text = lead + "   ".join(a for _p, a, _b in items)
    keys = [(p, i, a, b if b is not None else a) for i, (p, a, b) in enumerate(items)]
    if len(text) <= w:
        return text
    keys = [k for k in keys if k[3]]
    while keys:
        text = lead.rstrip() + "  " + "  ".join(k[3] for k in keys)
        if len(text) <= w or len(keys) == 1:
            return text
        keys.remove(max(keys, key=lambda k: (k[0], k[1])))
    return lead.rstrip()


def footer_items(scope, enabled=None, dyn=None, skip=()):
    """[(prio, long, short)] of a screen's keys for fit(): the rows with a prio, as 'show: label'. dyn: {action: (long, short) | None}
    replaces a row's whole text (the label that changes, e.g. 'hide details') or hides it (None); skip: actions left out."""
    dyn = dyn or {}
    out = []
    for r in rows(scope, enabled):
        if r.prio is None or r.action in skip:
            continue
        if r.action == "screen":
            show, long_, short = screens_show(enabled), r.label, None
        else:
            show, long_, short = r.show, r.label, r.short
        if r.action in dyn:
            if dyn[r.action] is None:
                continue
            text = dyn[r.action]
            out.append((r.prio, text[0], text[1] if len(text) > 1 else None))
            continue
        out.append((r.prio, f"{show}: {long_}", None if short is None else (f"{show}: {short}" if short else "")))
    return out


def footer(scope, lead, w, enabled=None, dyn=None, skip=(), extra=()):
    """A screen's footer, plain text of at most w columns (the caller colours it). extra: more (prio, long, short) items."""
    return fit(lead, footer_items(scope, enabled, dyn, skip) + list(extra), w)


HELP_PAIR = {"next": ("Tab/Shift+Tab", "next/previous screen"), "slide-prev": ("←→ PgUp/PgDn", "previous/next slide"),
             "close": ("←→ h l", "close (or the parent)/open"), "expand": ("e c", "expand/collapse all"), "delete": ("x X", "delete/delete all")}
HELP_MERGED = ("prev", "slide-next", "open", "collapse", "delete-all")  # said by the line of their pair
GROUP_TITLE = {"global": "Everywhere", "list": "Lists", "ai": "AI", "cpu": "CPU"}


def help_rows(scope, enabled=None, portable=False, paused=False):
    """[(group title, [(keys shown, what it does)])] for the `?` overlay: the screen's own keys first, then the global ones."""
    ok = enabled or (lambda f: True)
    names = {n: t for n, _f, t in SCREENS}
    groups = []
    for s in chain(scope):
        items = []
        for r in KEYMAP:
            if r.scope != s or (r.feat and r.feat != "portable" and not ok(r.feat)):
                continue
            show, what = r.show, r.label
            if r.action in HELP_MERGED:  # one line for a pair of keys that do the opposite
                continue
            if r.action in HELP_PAIR:
                show, what = HELP_PAIR[r.action]
            elif r.action == "screen":
                show, what = screens_show(ok), ", ".join(names[n] for _d, n in screen_keys(ok))
            elif r.action == "back" and r.keys == ("esc",):
                what = "close details or help, else back" if scope != "overview" else "close the help"
            elif r.action == "back":  # q
                if scope == "overview":
                    if not portable:
                        continue
                    what = "quit"
                else:
                    what = "like Esc"
            elif r.action == "pause":
                what = "resume the redraw" if paused else "pause the redraw"
            elif r.action == "help":
                what = "this help"
            elif r.action.startswith("open-"):
                what += " (as " + {"m": "2", "c": "3", "h": "4", "a": "5"}[r.keys[0]] + ")"
            items.append((show, what))
        if items:
            groups.append((GROUP_TITLE.get(s, s.capitalize()), items))
    return groups
