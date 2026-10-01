"""Design tokens and text helpers for every screen (stdlib only, Python 3.8+).

One table of semantic tokens ('ok', 'warn', 'err', ...) says what a colour MEANS; a theme says how a surface writes it:
ANSI_THEMES as SGR codes for the console, CSS_THEMES as colours for the web. The 'default' ANSI theme is exactly the codes the
console has always written, so drawing through sgr() changes nothing on screen. The text helpers (safe, plural, the human
formatters) hold no colour and no clock: they are what every screen and the exposure model share, so none of them has to
import render.py for it.

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
