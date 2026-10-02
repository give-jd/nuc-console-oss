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
    return x if isinstance(x, (Span, Line)) else Span(x)


class Span(_Component):
    """A piece of text: its tone (a token of TOKENS, or None for the surface's own colour), bold, mono (a number or a path)."""
    __slots__ = ("text", "tone", "bold", "mono")

    def __init__(self, text, tone=None, bold=False, mono=False):
        self.text, self.tone, self.bold, self.mono = _text(text), _tone(tone), bool(bold), bool(mono)


class Line(_Component):
    """One line of Spans (strings are made Spans)."""
    __slots__ = ("spans",)

    def __init__(self, spans=()):
        self.spans = [x if isinstance(x, Span) else Span(x) for x in ([spans] if isinstance(spans, (str, Span)) else spans)]

    @property
    def text(self):
        return "".join(s.text for s in self.spans)


class Raw(_Component):
    """The lines of a block drawn the old way, ANSI included: what a card holds until it is built of components. A console draws them
    as they are; they carry no state of their own (the Card around them does)."""
    __slots__ = ("lines",)

    def __init__(self, lines=()):
        self.lines = list(lines)


class More(_Component):
    """'... +N more': what a card left out, and where all of it is (href: a web page, None on the console, which has its Details pages)."""
    __slots__ = ("n", "what", "href")

    def __init__(self, n, what="more", href=None):
        self.n, self.what, self.href = max(0, int(n)), _text(what), href

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
    """A column of a Table: key, label, align ('l' | 'r'), prio (0 = never dropped; a bigger number is dropped sooner when the room is
    short), num (the cells are numbers: mono, aligned)."""
    __slots__ = ("key", "label", "align", "prio", "num")

    def __init__(self, key, label, align="l", prio=0, num=False):
        self.key, self.label, self.align, self.prio, self.num = key, _text(label), "r" if align == "r" else "l", int(prio), bool(num)


class Row(_Component):
    """A row of a Table: one cell per column (Span, Line or str), a tone for the whole row, a stable key, a link."""
    __slots__ = ("cells", "tone", "key", "href")

    def __init__(self, cells, tone=None, key=None, href=None):
        self.cells = [_inline(c) for c in cells]
        self.tone, self.key, self.href = _tone(tone), key, href


class Table(_Component):
    """cols [Col], rows [Row] (one cell per column), groups: None or [(label, first row index)]: the rows from there on belong to it."""
    __slots__ = ("cols", "rows", "groups")

    def __init__(self, cols, rows=(), groups=None):
        self.cols, self.rows = list(cols), list(rows)
        for r in self.rows:
            if len(r.cells) != len(self.cols):
                raise ValueError(f"a row has {len(r.cells)} cells for {len(self.cols)} columns")
        self.groups = None if groups is None else [(_text(label), int(i)) for label, i in groups]


class KV(_Component):
    """Labels and values: pairs of (label, Span / Line / str) and the label column's width on the console."""
    __slots__ = ("pairs", "lw")

    def __init__(self, pairs=(), lw=13):
        self.pairs = [(_text(k), _inline(v)) for k, v in pairs]
        self.lw = int(lw)


class Bar(_Component):
    """A fraction of a whole (0..1): its state is read off the thresholds (warn, err), and a fraction that is None (it could not be
    read) is state 'unknown' with the text '?': never an empty bar that looks fine."""
    __slots__ = ("frac", "value_text", "warn", "err")

    def __init__(self, frac, value_text="", warn=0.7, err=0.9):
        f = num(frac)
        self.frac = None if f is None else min(max(f, 0.0), 1.0)
        self.value_text = "?" if self.frac is None else _text(value_text)
        self.warn, self.err = warn, err

    @property
    def state(self):
        return "unknown" if self.frac is None else "ok" if self.frac < self.warn else "warn" if self.frac < self.err else "err"


class Spark(_Component):
    """A series of numbers, oldest first; what is not a finite number is dropped."""
    __slots__ = ("values",)

    def __init__(self, values=()):
        self.values = [v for v in (num(x) for x in values) if v is not None]


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
    """Items that flow over several lines without being split (max_lines: then '... +N' ends the last one)."""
    __slots__ = ("items", "sep", "max_lines")

    def __init__(self, items=(), sep="  ·  ", max_lines=None):
        self.items, self.sep, self.max_lines = [_inline(x) for x in items], sep, max_lines


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
    """A part that is folded until asked for: summary (Line / Span / str) and body (components)."""
    __slots__ = ("summary", "body", "open")

    def __init__(self, summary, body=(), open=False):
        self.summary, self.body, self.open = _inline(summary), list(body), bool(open)
