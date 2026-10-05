"""ANSI screen -> HTML (stdlib only): shared by the web view (web.py) and the full-screen kiosk (render.py --kiosk).

Everything is escaped; only <span class> elements with fixed class names are produced. No JavaScript, ever.
"""
import hashlib
import html
import re

import ui

SGR = re.compile(r"\x1b\[([0-9;]*)m")
ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
COLOR = {30: "k", 31: "r", 32: "g", 33: "y", 34: "b", 35: "m", 36: "c", 37: "w", 90: "d"}
BACKGROUND = {41: "bR", 43: "bY"}  # the header banner: red for problems, yellow for warnings
PALETTE = (".r{color:#ff7b72}.g{color:#3fb950}.y{color:#d29922}.b{color:#58a6ff}.m{color:#bc8cff}.c{color:#39c5cf}"
           ".w{color:#f0f6fc}.d{color:#6e7681}.k{color:#0d1117}.B{font-weight:700}.bR{background:#da3633}.bY{background:#d29922}"
           ".rv{background:#c9d1d9;color:#0d1117}")
CSS = ("html{background:#0d1117}body{margin:0;padding:12px;color:#c9d1d9;font:14px/1.25 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}"
       "pre{margin:0;overflow-x:auto}" + PALETTE +
       # the bar with A- / A+ and the views stays at the bottom of the window while the page scrolls
       "footer{position:sticky;bottom:0;margin-top:10px;padding:8px 0;background:#0d1117;border-top:1px solid #30363d;"
       "color:#6e7681;font:12px sans-serif}"
       "a{color:#58a6ff}@media(max-width:700px){body{font-size:10px}}")
# the web MAP page (web.py): a tree of links, its details panel on the right of a wide window and under the tree on a narrow one
MAP_CSS = (".hd{display:flex;justify-content:space-between;gap:2ch;white-space:pre;padding:0 1ch}.hd>span:first-child{overflow:hidden;"
           "text-overflow:ellipsis}.lg{margin:8px 0 2px;color:#8b949e}.nt{margin:2px 0;color:#8b949e}.n{color:#c9d1d9}"
           ".mp{display:grid;grid-template-columns:minmax(0,1fr);gap:12px 28px;margin-top:8px}.tree{min-width:0}"
           ".ro{display:grid;grid-template-columns:auto minmax(0,1fr);padding:0 4px;border-radius:3px;scroll-margin:30vh 0}"
           ".ro:hover{background:#161b22}.ro:target{outline:1px solid #30363d}.sel,.sel:hover{background:#1f2a3a;box-shadow:inset 3px 0 #58a6ff}"
           ".tr{white-space:pre;color:#6e7681}.bd{white-space:pre-wrap;overflow-wrap:anywhere}"
           ".tg{display:inline-block;width:1.6em;text-align:center;text-decoration:none}a.lb{text-decoration:none}a.lb:hover{text-decoration:underline}"
           ".dp{align-self:start;border:1px solid #30363d;border-radius:6px;padding:8px 12px;scroll-margin:8px}"
           ".dh{display:flex;justify-content:space-between;gap:2ch;margin-bottom:6px;font-weight:700}"
           ".dp table{border-collapse:collapse;width:100%}.dp th{text-align:left;font-weight:400;color:#8b949e;padding:1px 2ch 1px 0;"
           "vertical-align:top;white-space:nowrap}.dp td{padding:1px 0;overflow-wrap:anywhere}.dp th.in{padding-left:2ch}.dp tr.top td{font-weight:700}"
           ".dl{display:none}"
           "@media(min-width:1000px){.mp.two{grid-template-columns:minmax(0,1fr) minmax(340px,40%)}"
           ".dp{position:sticky;top:8px;max-height:calc(100vh - 80px);overflow:auto}}"
           "@media(max-width:999px){.mp.two .dl{display:inline}}"
           # 'tree | graph' and the other switches: the current option highlighted, the others links
           ".mv{display:inline-flex;vertical-align:middle;border:1px solid #30363d;border-radius:4px;overflow:hidden}"
           ".mv>*{padding:0 7px;line-height:18px}.mv>*+*{border-left:1px solid #30363d}.mv>b{background:#1f6feb40;color:#f0f6fc;"
           "font-weight:600}.mv>a{text-decoration:none}.mv>a:hover{background:#161b22}")
# the MAP's graph view (web.py, ?view=map&as=graph): an SVG of circles and lines, the colours of PALETTE by state, the
# evidence of an edge in its stroke (seen solid, declared dashed, same network dotted, structure thin); no inline style
GRAPH_CSS = (".gv{overflow:auto;max-height:calc(100vh - 140px);border:1px solid #21262d;border-radius:6px;min-width:0}"
             "#gsvg{display:block;height:auto;margin:0 auto}"
             ".ge line,.ll{fill:none;stroke:#484f58;stroke-width:1}"
             ".e.seen,.ll.seen{stroke:#c9d1d9;stroke-width:1.6}.e.declared,.ll.declared{stroke:#8b949e;stroke-width:1.4;stroke-dasharray:6 4}"
             ".e.possible,.ll.possible{stroke:#6e7681;stroke-width:1.5;stroke-dasharray:1 4;stroke-linecap:round}"
             ".e.bind,.ll.bind{stroke:#30363d}.e.reach,.ll.reach{stroke:#3d444d;stroke-width:1.2}.e.bad,.ll.bad{stroke:#da3633}"
             ".e.nb{stroke-width:2.2}.gv:has(.n.sel) .e:not(.nb){opacity:.2}"
             ".mk.seen{fill:#c9d1d9}.mk.declared{fill:#8b949e}.mk.possible{fill:#6e7681}"   # an arrowhead: its line's colour where known
             "@supports (fill:context-stroke){.mk path{fill:context-stroke}}.dh{flex-wrap:wrap}"
             ".n circle,.ln circle{stroke:#0d1117;stroke-width:1.5}"
             ".n.ok circle,.ln.ok circle{fill:#3fb950}.n.warn circle,.ln.warn circle{fill:#d29922}"
             ".n.err circle,.ln.err circle{fill:#ff7b72}.n.info circle,.ln.info circle{fill:#8b949e}"
             ".n.down circle,.ln.down circle{fill:#0d1117;stroke:#ff7b72;stroke-width:2.5}"     # down, unknown: a ring (nothing there)
             ".n.unknown circle,.ln.unknown circle{fill:#0d1117;stroke:#d29922;stroke-width:2.5}"
             ".n.k-ext.info circle,.ln.ext circle{fill:#0d1117;stroke:#8b949e}"                  # a remote address: hollow
             ".n.k-root circle{stroke:#c9d1d9;stroke-width:2}.n.k-stack circle{stroke:#8b949e;stroke-dasharray:3 2}"
             ".n text.ls{text-anchor:start}.n text.le{text-anchor:end}"
             ".n text{font:11px sans-serif;fill:#c9d1d9;text-anchor:middle;paint-order:stroke;stroke:#0d1117;stroke-width:3px;"
             "stroke-linejoin:round}.n.k-root text{font-weight:700;font-size:12px}"
             ".n:hover circle{stroke:#58a6ff;stroke-width:2.5}.n:hover text{fill:#f0f6fc}"
             ".n.sel circle{stroke:#58a6ff;stroke-width:3.5}.n.sel text{fill:#f0f6fc;font-weight:700}.n.dim{opacity:.35}"
             ".lk{vertical-align:middle;overflow:visible}@media(max-width:999px){.lg .dl{display:inline}}"
             # set by graphjs.SCRIPT: .js it runs, .drag a drag or pan, .hov/.hv a hovered node and its neighbours, .pin moved by hand
             ".gv.js #gsvg{cursor:grab}#gsvg.drag{cursor:grabbing}#gsvg.hov .n:not(.hv){opacity:.3}#gsvg.hov .e:not(.hv){opacity:.12}"
             ".e.hv{stroke-width:2.2}.n.pin circle{stroke:#f0f6fc;stroke-dasharray:2 2}")


# the web CPU page (web.py): a process row is a link that looks like the text around it
CPU_CSS = "a.pr{color:inherit;text-decoration:none}a.pr:hover{background:#161b22}"
# the web HEALTH page (web.py): level pills on the findings rows (the rows and the details panel reuse MAP_CSS)
HEALTH_CSS = (".pl{display:inline-block;min-width:6ch;padding:0 .6ch;border-radius:3px;text-align:center;font-weight:700;white-space:pre}"
              ".pl.r{background:#da3633;color:#fff}.pl.y{background:#d29922;color:#0d1117}.pl.d{color:#6e7681;font-weight:400}"
              ".ht{margin:8px 0 0}.hs{margin:10px 0 2px;color:#8b949e}.hn{margin:6px 0 0;white-space:pre-wrap}"
              # the advisor's ADVICE block under the findings (advisor.html): its own words, set apart from the rules' findings
              ".advice{margin:10px 0 0;padding:4px 10px;border-left:3px solid #39c5cf}.advice p{margin:3px 0;white-space:pre-wrap;overflow-wrap:anywhere}"
              ".advice-head,.advice-cites,.advice-tools{color:#8b949e}.advice-head{font-weight:700}.advice-error{border-left-color:#d29922}")
# the web AI page (web.py): the models as a table of links (the details panel reuses MAP_CSS, the pills HEALTH_CSS), a verdict pill besides its symbol
AI_CSS = (".pl.g{background:#3fb950;color:#0d1117}.pl.c{background:#39c5cf;color:#0d1117}"
          ".mw{overflow-x:auto}.mt{border-collapse:collapse;width:100%}.mt th{text-align:left;font-weight:400;color:#8b949e;padding:2px 2ch 2px 0;white-space:nowrap}"
          ".mt td{padding:1px 2ch 1px 0;white-space:nowrap}.mt .r{text-align:right}.mt .mk{white-space:pre;padding-left:4px}.mt .pl{min-width:11ch}"
          ".mt tbody tr:hover{background:#161b22}.mt tr.sel,.mt tr.sel:hover{background:#1f2a3a;box-shadow:inset 3px 0 #58a6ff}.mt .no a{color:#8b949e}"
          ".mt .nn div{max-width:44ch;overflow:hidden;text-overflow:ellipsis;color:#8b949e}.cmd{background:#161b22;padding:1px 6px;border-radius:3px;user-select:all;overflow-wrap:anywhere}"
          "@media(max-width:1399px){.mp.two .nn{display:none}}@media(max-width:699px){.mt .nn,.mt .pm,.mt .sz{display:none}}"
          # the AI switch, its progress, the chat and the buttons (forms: web.py ai_control_html / ai_chat_html)
          ".ctl,.chat{margin:10px 0;padding:8px 12px;border:1px solid #30363d;border-radius:6px}.ctl .row{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;margin:2px 0}"
          ".pl.big{font-size:1.15em;padding:2px 14px}.ctl .st{font-weight:700;overflow-wrap:anywhere}.ctl .note{margin:4px 0}.note.ok{color:#3fb950}.note.bad{color:#ff7b72}"
          ".ctl .dir{color:#8b949e}.ctl progress{width:min(48ch,100%);height:14px}"
          "form.f{display:inline;margin:0}.bt{font:inherit;cursor:pointer;display:inline-block;text-decoration:none;color:#c9d1d9;background:#21262d;border:1px solid #30363d;"
          "border-radius:6px;padding:2px 12px}.bt:hover{background:#30363d}.bt.on,.bt.use{background:#238636;border-color:#2ea043;color:#fff}"
          ".bt.off,.bt.del,.bt.stop{background:#8e1519;border-color:#da3633;color:#fff}.bt.use{padding:0 8px}.bt:disabled{opacity:.4;cursor:not-allowed}"
          ".cf{margin:6px 0;padding:6px 10px;border-left:3px solid #d29922;background:#161b22}.cf .bt{margin-left:6px}"
          ".chat .q{margin:2px 0}.chat .qa{margin:8px 0}input.q{font:inherit;color:#c9d1d9;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:3px 8px;width:min(70ch,100%)}"
          "input.q:disabled{opacity:.5}.chat .adv{margin:6px 0}.mg{margin:10px 0;color:#8b949e}.mt .ac{padding-left:1ch}")


def to_html(text):
    """ANSI text -> HTML spans (colour, background, reverse, bold)."""
    out, fg, bg, rev, bold, pos = [], "", "", False, False, 0
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-ln-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)  # cursor/erase/OSC sequences (anything but SGR 'm')

    def span(s):
        if not s:
            return ""
        names = " ".join(x for x in (fg, bg, "rv" if rev else "", "B" if bold else "") if x)
        return f'<span class="{names}">{html.escape(s)}</span>' if names else html.escape(s)
    for m in SGR.finditer(text):
        out.append(span(text[pos:m.start()]))
        for code in (int(x) for x in (m.group(1) or "0").split(";") if x):
            if code == 0:
                fg, bg, rev, bold = "", "", False, False
            elif code == 1:
                bold = True
            elif code == 7:
                rev = True
            elif code in COLOR:
                fg = COLOR[code]
            elif code in BACKGROUND:
                bg = BACKGROUND[code]
        pos = m.end()
    out.append(span(ESC.sub("", text[pos:])))
    return "".join(out)


def sgr_class(code):
    """'1;41;37' -> 'w bR B': an SGR code (render.status_pill's) as the class names to_html would give its text."""
    codes = [int(x) for x in str(code).split(";") if x.isdigit()]
    names = [COLOR[x] for x in codes if x in COLOR] + [BACKGROUND[x] for x in codes if x in BACKGROUND]
    return " ".join(names + (["rv"] if 7 in codes else []) + (["B"] if 1 in codes else []))


def fit_css(cols, rows=0):
    """The text fills the window width (and the height when rows is given): the same grid as a console on any screen."""
    width = f"calc(98vw / {cols * 0.61:.1f})"
    size = f"min({width}, calc(93vh / {rows * 1.2:.1f}))" if rows else width
    return (f"body{{padding:1vh 1vw}}pre{{font-size:{size};line-height:1.2;overflow:hidden}}"
            + ("html,body{height:100%;overflow:hidden}" if rows else ""))


def kiosk_page(screen, cols, rows, refresh, title):
    """A self-refreshing page whose font is sized so that exactly `cols` x `rows` characters fill the window
    (monospace advance ~0.6 em, line height 1.2 em): the same grid as a text console, on any monitor."""
    font = f"min(calc(98vw / {cols * 0.61:.1f}), calc(98vh / {rows * 1.2:.1f}))"
    css = ("html,body{margin:0;height:100%;overflow:hidden;background:#0d1117;cursor:none}"
           f"pre{{margin:0;padding:1vh 1vw;color:#c9d1d9;font-family:ui-monospace,'Cascadia Mono',Consolas,Menlo,monospace;"
           f"font-size:{font};line-height:1.2;white-space:pre}}" + PALETTE)
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta http-equiv="refresh" content="{int(refresh)}">'
            f"<title>{html.escape(title)} · nuc-console</title><style>{css}</style><pre>{to_html(screen.replace(chr(13), ''))}</pre></html>")


# ======================================================================================================================================
# The components as HTML: html(node)
# ======================================================================================================================================
# The web's drawing of the ui components (ui.py): semantic HTML with classes, no inline style and no script. Every text is escaped; a
# class is built only from a token or a state (letters, digits and '-'), a link only when it is a path, a query or http(s).
#
#   tone     t-ok t-warn t-err t-unknown t-info t-muted t-accent t-strong ...   (a token of ui.TOKENS, '_' written '-')
#   state    st-ok st-warn st-err st-down st-unknown st-info                    (a card, a Pill, a Kpi, a Bar, a table row's cell)
#   level    lv-ok lv-warn lv-err lv-info                                       (a Msg)
#   b, mono  bold text, a number or a path
#   n        a cell of numbers (<td class="n">), r right-aligned, p<N> a column that goes sooner the bigger N is (container queries)
#   card     <article class="card st-warn s2" data-card="id"> with <header><h2>, the note and the state's word
#
# A Bar and a Spark are SVG with attributes (viewBox, width, points): the CSS colours them by the state class of the element. A More is a
# <details>. The console's widths (Col.w, Bar.w) are not the web's: the CSS sizes the columns and the bars.
#
# The module's `html` name was the standard library module until this function took it: its escape() and unescape() are kept on it, so that
# `html.escape(...)` in the code above and in the shell keeps working.

import ui  # noqa: E402 - the components this section draws

_stdhtml = html
_SAFE_CLASS = re.compile(r"[^a-z0-9-]")
_SAFE_HREF = re.compile(r"^(?:/(?!/)|[?#]|https?://)[^\s\\\"'<>]*$")  # a path, a query, an anchor or http(s): not //host, not a backslash


def _e(text):
    return _stdhtml.escape(ui.safe(text), quote=True)  # control characters are '?' too: escaping alone leaves an ESC in a data- attribute


def _cls(*names):
    """A class attribute from names ('' and None are left out; anything but a-z 0-9 and '-' is turned into '-')."""
    out = [_SAFE_CLASS.sub("-", str(n).lower().replace("_", "-")) for n in names if n]
    return f' class="{" ".join(out)}"' if out else ""


def _href(url):
    return str(url) if url and _SAFE_HREF.match(str(url)) else None


def _span(span):
    names = [("t-" + span.tone) if span.tone else "", "b" if span.bold else "", "mono" if span.mono else ""]
    text = span.text if span.full is None else span.full  # the web has the room the console had not
    return f"<span{_cls(*names)}>{_e(text)}</span>" if any(names) else _e(text)


_SPARK_W, _SPARK_H = 80, 16
_BAR_W, _BAR_H = 100, 8


def _bar_svg(b, value=True):
    if b.busy:  # work without a known total: a bar that moves (CSS only), with what is known
        return (f'<svg{_cls("bar", "busy", "t-" + b.tone if b.tone else "")} viewBox="0 0 {_BAR_W} {_BAR_H}" width="{_BAR_W}" height="{_BAR_H}" role="progressbar" '
                f'aria-label="in progress" preserveAspectRatio="none"><rect class="bg" x="0" y="0" width="{_BAR_W}" height="{_BAR_H}"/>'
                f'<rect class="fg" x="0" y="0" width="35" height="{_BAR_H}"/></svg>' + (f'<span class="n">{_e(b.value_text)}</span>' if b.value_text and value else ""))
    if b.frac is None:
        return '<span class="st-unknown" role="img" aria-label="unknown">?</span>'
    pct = round(b.frac * 100)
    svg = (f'<svg{_cls("bar", "st-" + b.state, "t-" + b.tone if b.tone else "")} viewBox="0 0 {_BAR_W} {_BAR_H}" width="{_BAR_W}" height="{_BAR_H}" role="img" '
           f'aria-label="{pct}%" preserveAspectRatio="none"><rect class="bg" x="0" y="0" width="{_BAR_W}" height="{_BAR_H}"/>'
           f'<rect class="fg" x="0" y="0" width="{round(b.frac * _BAR_W, 1):g}" height="{_BAR_H}"/></svg>')
    return svg + (f'<span class="n">{_e(b.value_text)}</span>' if b.value_text and value else "")


def _spark_svg(sp):
    vals = sp.values
    if not vals:
        return f'<svg class="spark" viewBox="0 0 {_SPARK_W} {_SPARK_H}" width="{_SPARK_W}" height="{_SPARK_H}" role="img" aria-label="no data"></svg>'
    top = max(max(vals), sp.floor, 1e-9)
    step = _SPARK_W / max(len(vals) - 1, 1)
    pts = " ".join(f"{i * step:.1f},{_SPARK_H - 1 - v / top * (_SPARK_H - 2):.1f}" for i, v in enumerate(vals))
    return (f'<svg class="spark" viewBox="0 0 {_SPARK_W} {_SPARK_H}" width="{_SPARK_W}" height="{_SPARK_H}" role="img" aria-label="trend">'
            f'<polyline fill="none" points="{pts}"/></svg>')


def _meter_svg(m):
    x, rects, said = 0.0, [], []
    for pct, kind in m.parts:
        w = min(pct, _BAR_W - x)
        if w > 0:
            rects.append(f'<rect{_cls("m-" + kind)} x="{x:.1f}" y="0" width="{w:.1f}" height="{_BAR_H}"/>')
        x += max(w, 0.0)
        said.append(f"{kind} {pct:.0f}%")
    return (f'<svg class="meter" viewBox="0 0 {_BAR_W} {_BAR_H}" width="{_BAR_W}" height="{_BAR_H}" role="img" aria-label="{_e(", ".join(said) or "idle")}" '
            f'preserveAspectRatio="none"><rect class="bg" x="0" y="0" width="{_BAR_W}" height="{_BAR_H}"/>{"".join(rects)}</svg>')


_SERIES_H = 16


def _series_svg(sr):
    """Bars, one per value, scaled to the series' own maximum; a gap (None) is a gap. (ui.Series)"""
    n = len(sr.values)
    width = max(4 * n - 1, 1)
    top = max([v for v in sr.values if v is not None] or [0])
    bars = []
    for i, v in enumerate(sr.values):
        if v is not None:
            h = max(1.0, v / top * (_SERIES_H - 2)) if top > 0 else 1.0
            bars.append(f'<rect x="{4 * i}" y="{_SERIES_H - h:.1f}" width="3" height="{h:.1f}"/>')
    label = "trend" if bars else "no data"
    return (f'<svg{_cls("series", "t-" + sr.tone if sr.tone else "")} viewBox="0 0 {width} {_SERIES_H}" width="{width}" height="{_SERIES_H}" role="img" '
            f'aria-label="{label}" preserveAspectRatio="none">{"".join(bars)}</svg>')


def _inline(x):
    """A Span, a Line, a Bar, a Meter, a Spark, a Series, a Badge or an Action as markup that sits in a line or a cell."""
    if isinstance(x, ui.Span):
        return _span(x)
    if isinstance(x, ui.Line):
        return "".join(_inline(p) for p in x.spans)
    if isinstance(x, ui.Bar):
        return _bar_svg(x)
    if isinstance(x, ui.Spark):
        return _spark_svg(x)
    if isinstance(x, ui.Meter):
        return _meter_svg(x)
    if isinstance(x, ui.Series):
        return _series_svg(x)
    if isinstance(x, ui.Badge):
        return _badge_html(x)
    if isinstance(x, ui.Action):
        return _action_html(x)
    return '<span class="st-unknown">?</span>'


def _wprio(c):
    p = c.prio if c.wprio is None else c.wprio
    return ("p%d" % p) if p else ""


def _barline(line):
    """The inside of a line that has a bar: what comes before it (the label), the bar, its value, and what else the line says (under the bar),
    each in its own box, so that the page lines the bars of a card up. The value is the bar's text up to its first run of two spaces. None for
    a line without a bar."""
    at = next((i for i, x in enumerate(line.spans) if isinstance(x, ui.Bar)), None)
    if at is None:
        return None
    pre, bar, post = line.spans[:at], line.spans[at], line.spans[at + 1:]
    label = "".join(_inline(x) for x in pre).strip()
    if bar.frac is None:
        return f'<span class="lb">{label}</span>{_inline(bar)}'
    value, _, rest = (bar.value_text or "").strip().partition("  ")
    extra = (_e(rest.strip()) + " " + "".join(_inline(x) for x in post)).strip()
    return (f'<span class="lb">{label}</span>{_bar_svg(bar, False)}<span class="n">{_e(value)}</span>'
            + (f'<span class="bx">{extra}</span>' if extra else ""))


def _th(c):
    """The head of a column: its label, a link when the rows can be sorted by it (data-key: the keys of the keymap that do the same)."""
    link, label = _href(c.href), _e(c.label)
    if link:
        label = f'<a href="{_e(link)}"' + (f' data-key="{_e(c.hkey)}"' if c.hkey else "") + f">{label}</a>"
    aria = {"desc": ' aria-sort="descending"', "asc": ' aria-sort="ascending"'}.get(c.sort, "")
    cls = _cls(c.align if c.align != "l" else "", "n" if c.num else "", _wprio(c), "sorted" if c.sort else "")
    return f'<th{cls}{aria} scope="col">{label}</th>'


def _table(t, notes=None):
    heads = "".join(_th(c) for c in t.cols)
    marks = {i: label for label, i in (t.groups or ())}
    ends = sorted(marks) + [len(t.rows)]
    body = []
    for i, r in enumerate(t.rows):
        if i in marks:
            count = f' <span class="n">({ends[ends.index(i) + 1] - i})</span>' if t.titled else ""
            body.append(f'<tr class="grp"><th colspan="{len(t.cols)}" scope="rowgroup">{_e(marks[i])}{count}</th></tr>')
        link = _href(r.href)
        cells = []
        for j, (c, cell) in enumerate(zip(t.cols, r.cells)):
            inner = _inline(cell)
            if j == 0 and link:
                inner = f'<a href="{_e(link)}">{inner}</a>'
            cells.append(f'<td{_cls(c.align if c.align != "l" else "", "n" if c.num else "", _wprio(c))}>{inner}</td>')
        key = f' data-key="{_e(r.key)}"' if r.key is not None else ""
        key += (" data-row" if link else "") + (' aria-current="true"' if r.tone == "sel" else "")  # a row of a list; the one the cursor is on
        body.append(f'<tr{_cls("t-" + r.tone if r.tone else "")}{key}>{"".join(cells)}</tr>')
        if notes and notes[i]:  # NoteTable: what belongs to the row, in a row of its own under it
            body.append(f'<tr class="sub"><td colspan="{len(t.cols)}">{"".join(html(x) for x in notes[i])}</td></tr>')
    colgroup = "<colgroup>" + "".join(f"<col{_cls('c-' + c.key)}>" for c in t.cols) + "</colgroup>"
    return f'<table class="tbl">{colgroup}<thead><tr>{heads}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def html(node):
    """The markup of a component (a Card is an <article>; anything that is not a component is drawn '?')."""
    if isinstance(node, ui.Raw):
        return f'<pre class="raw">{to_html(chr(10).join(node.lines))}</pre>'
    if isinstance(node, ui.Card):
        head = (f'<header><h2>{_e(node.title)}</h2>' + (f'<span class="note">{_e(node.note)}</span>' if node.note else "")
                + f'<span{_cls("state", "st-" + node.state)}>{_e(ui.SYMBOLS[node.state])} {_e(node.state)}</span></header>')
        attrs = f' data-card="{_e(node.id)}"' + (" data-truncated" if node.truncated else "")
        return f'<article{_cls("card", "st-" + node.state, "s%d" % node.size)} id="card-{_e(node.id)}"{attrs}>{head}{"".join(html(p) for p in node.body)}</article>'
    if isinstance(node, ui.Line) and not node.spans:
        return ""  # a blank line is for the console
    if isinstance(node, ui.Line) and _barline(node) is not None:
        return f'<p class="ln bl">{_barline(node)}</p>'
    if isinstance(node, (ui.Span, ui.Line, ui.Bar, ui.Spark, ui.Meter)):
        return f'<p class="ln">{_inline(node)}</p>'
    if isinstance(node, ui.Problem):
        # what the console has no room for: the id (a chip after the text), why it matters (under it), the fix and how to accept it (behind a
        # disclosure, which the compact and wall densities do not draw)
        chip = f' <code class="pid" title="{_e(node.title)}">{_e(node.id)}</code>' if node.id else ""
        why = f'<span class="d why">{_e(node.why)}</span>' if node.id and node.why else ""
        how = "".join(x for x in (
            f'<span class="d how">fix: <code class="cmd">{_e(node.fix)}</code></span>' if node.fix else "",
            f'<span class="d accept">accept if known: <code class="cmd">{_e(node.accept)}</code></span>' if node.accept else ""))
        fix = f'<details class="fix" data-k="fix-{_e(node.id)}"><summary>fix</summary>{how}</details>' if how else ""
        return (f'<div{_cls("msg", "prob", "lv-" + node.level)} data-problem="{_e(node.id)}">'
                f'<span class="sym">{_e(ui.SYMBOLS[node.level])}</span><span class="pr">{_e(node.text)}{chip}</span>{why}{fix}</div>')
    if isinstance(node, ui.Accepted):
        when = f" · accepted {_e(node.when)}" if node.when else ""
        undo = f' · undo: <code class="cmd">{_e(node.undo)}</code>' if node.undo else ""
        return (f'<div{_cls("msg", "known", "lv-info")} data-problem="{_e(node.id)}"><span class="sym">{_e(ui.SYMBOLS["info"])}</span>'
                f'<span class="pr">{_e(node.text)} <code class="pid">{_e(node.id)}</code></span>'
                f'<span class="d why">reason: “{_e(node.reason)}”{when}{undo}</span></div>')
    if isinstance(node, ui.Msg):  # a Notice too
        notice = isinstance(node, ui.Notice)
        role = ' role="status"' if notice else ""
        text = _inline(node.rich) if isinstance(node, ui.RichMsg) else _e(node.text)
        return (f'<p{_cls("msg", "notice" if notice else "", "lv-" + node.level)}{role}>'
                f'<span class="sym">{_e(ui.SYMBOLS[node.level])}</span> {text}</p>')
    if isinstance(node, ui.Hint):
        return f'<p class="hint d">{_e(node.label)}: <code class="cmd">{_e(node.cmd)}</code></p>'
    if isinstance(node, ui.KV):
        return '<dl class="kv">' + "".join(f"<dt>{_e(k)}</dt><dd>{_inline(v)}</dd>" for k, v in node.pairs) + "</dl>"
    if isinstance(node, ui.Table):
        return _table(node)
    if isinstance(node, ui.Wrap):
        lm = f' data-max-lines="{int(node.max_lines)}"' if node.max_lines else ""
        lead = f'<li class="lead">{_inline(node.lead)}</li>' if node.lead is not None else ""
        return f'<ul class="wrap{" flat" if node.flat else ""}"{lm}>' + lead + "".join(f"<li>{_inline(x)}</li>" for x in node.items) + "</ul>"
    if isinstance(node, ui.More):
        link = _href(node.href)
        return (f'<details class="more"><summary>{_e(node.text)}</summary>'
                + (f'<p><a href="{_e(link)}">show all</a></p>' if link else "") + "</details>")
    if isinstance(node, ui.Group):
        return ('<section class="grp">' + (f"<h3>{_e(node.title)}</h3>" if node.title else "") + "".join(html(c) for c in node.children) + "</section>")
    if isinstance(node, ui.Pill):
        return f'<span{_cls("pill", "st-" + node.state)}>{_e(ui.SYMBOLS[node.state])} {_e(node.text)}</span>'
    if isinstance(node, ui.Kpi):
        spark = _spark_svg(node.spark) if node.spark is not None else ""
        return (f'<div{_cls("kpi", "st-" + node.state)} data-kpi="{_e(node.id)}" title="{_e(node.hint)}"><span class="sym">{_e(node.symbol)}</span>'
                f'<span class="lbl">{_e(node.label)}</span><span class="n">{_e(node.value)}</span><span class="unit">{_e(node.unit)}</span>{spark}</div>')
    if isinstance(node, ui.Tree):
        return ('<ul class="tree">' + "".join(f'<li{_cls("st-" + st)} data-depth="{int(d)}"><span class="sym">{_e(ui.SYMBOLS[st])}</span> {_inline(x)}</li>'
                                              for d, x, st in node.rows) + "</ul>")
    if isinstance(node, ui.Details):
        return (f'<details class="dt"{" open" if node.open else ""}><summary>{_inline(node.summary)}</summary>'
                + "".join(html(c) for c in node.body) + "</details>")
    drawn = _HTML.get(type(node))
    if drawn is not None:
        return drawn(node)
    return '<span class="st-unknown">?</span>'


# ---- the components of the system, container, database and boot cards (ui.py: Head, Indent, Grid, Timeline, NoteTable, Flow) -----------
#   <h3 class="sub">     Head          <div class="ind">   Indent        <ul class="grid"><li>   Grid (one core a cell)
#   <figure class="timeline"><svg class="tl"><rect class="tp tp-NAME" x width>   Timeline, then <ul class="legend">
#   <tr class="sub">     the notes of a NoteTable row, spanning the table      <p class="flow"><span class="lead">   Flow

_TL_W, _TL_H = 100, 8


def _timeline_html(tl):
    x, rects = 0.0, []
    for name, v in tl.parts:
        w = min(max(v / tl.total * _TL_W, 0.0), _TL_W - x)
        rects.append(f'<rect{_cls("tp", "tp-" + name)} x="{x:.1f}" y="0" width="{w:.1f}" height="{_TL_H}"/>')
        x += w
    svg = (f'<svg class="tl" viewBox="0 0 {_TL_W} {_TL_H}" width="{_TL_W}" height="{_TL_H}" role="img" aria-label="boot timeline" '
           f'preserveAspectRatio="none">{"".join(rects)}</svg>')
    legend = "".join(f'<li{_cls("tp-" + name)}><span class="sw"></span> {_e(name)} <span class="n">{_e(ui.fmt_s(v))}</span></li>' for name, v in tl.parts)
    return f'<figure class="timeline">{svg}<ul class="legend">{legend}</ul></figure>'


def _grid_item(x):
    bl = _barline(x) if isinstance(x, ui.Line) else None
    return f'<li class="bl">{bl}</li>' if bl is not None else f"<li>{_inline(x)}</li>"


def _flow_html(f):
    lead = f'<span class="lead">{_inline(f.lead)}</span> ' if f.lead is not None else ""
    return f'<p class="flow">{lead}' + "".join(f'<span class="fi">{_inline(x)}</span>' for x in f.items) + "</p>"


def _tiles_html(t):
    return '<div class="kpis tiles">' + "".join(html(k) for k in t.items) + "</div>"


# ---- the components of the full screens (ui.py: Seg, Title, Series, Cols, Split, Pane, Finding, Advice) ------------------------------
#   <header class="st"><h2>      Title (its parts in <span class="bit">, then the Seg)        <div class="seg">   Seg: links with data-key
#   <ul class="ol"><li class="ob st-STATE" data-depth><a class="tg">, <a class="oa" data-row data-k aria-current>   Outline (a Branch each)
#   <aside class="props"><div class="ph">, <dl><dt><dd class="lv-LEVEL">   Props          <ul class="legend lgd">   Legend (in a Title)
#   <div class="cols"><div class="col">   Cols       <div class="split">   Split       <svg class="series"><rect>   Series
#   <div class="fd lv-LEVEL" data-k><details data-k><summary>   Finding, its Pane inside as <dl class="pane">
#   <div class="advice [advice-shared|advice-error|advice-none]">   Advice: <p class="advice-head">, paragraphs, <p class="advice-cites|advice-tools">

def _seg_html(sg):
    return seg(sg.label, [(text, _href(href), chosen, f' data-key="{_e(key)}" title="key {_e(key)}"' if key else "")
                          for text, key, chosen, href in sg.options])


def _legend_html(lg):
    return ('<ul class="legend lgd">' + "".join(f'<li><span{_cls("t-" + tone if tone else "")}>{_e(glyph)}</span> {_e(word)}</li>' for glyph, word, tone in lg.items) + "</ul>")


def _title_html(t):
    bits = "".join(f'<span class="bit">{_inline(p)}</span>' for p in t.parts)
    segs = ([t.seg] if t.seg is not None else []) + list(t.segs)
    return (f'<header class="st"><h2>{_e(t.label)}</h2><p class="bits">{bits}</p>{_legend_html(t.legend) if t.legend is not None else ""}'
            + "".join(_seg_html(sg) for sg in segs) + "</header>")


_BRANCH_MAX = 12  # the deepest a tree goes (graph.MAX_DEPTH): a deeper row is drawn as deep as that


def _branch_html(b):
    """A row of an Outline: the mark (a link that opens or closes, or plain), then the row's own link that selects it (data-row: the keys move
    over these), the state's symbol first."""
    d = max(0, min(b.depth, _BRANCH_MAX))
    k = _e(b.key)
    link, tog = _href(b.href), _href(b.mark_href)
    mark = (f'<a class="tg" href="{_e(tog)}" title="{_e(b.tip)}" data-k="t-{k}">{_inline(b.mark)}</a>' if tog
            else f'<span class="tg d" title="{_e(b.tip)}">{_inline(b.mark)}</span>')
    inner = f'<span class="sym">{_e(ui.SYMBOLS[b.state])}</span> {_inline(b.body)}'
    cur = ' aria-current="true"' if b.cursor else ""
    row = (f'<a class="oa" href="{_e(link)}" data-row data-k="o-{k}"{cur}>{inner}</a>' if link else f'<span class="oa"{cur}>{inner}</span>')
    near = f'<li class="ob-d">{html(b.after)}</li>' if b.after is not None else ""  # the details under the row: shown by CSS where there is no pane beside the tree
    return f'<li{_cls("ob", "st-" + b.state, "sel" if b.cursor else "")} data-depth="{d}">{mark}{row}</li>{near}'


def _props_html(p):
    close = _href(p.close)
    head = f'<div class="ph"><h3 class="sub">{_e(p.title)}</h3>' + (f'<a href="{_e(close)}">close ✕</a>' if close else "") + "</div>"
    rows = "".join(f'<dt{_cls("in" if raw.startswith("  ") else "")}>{_e(raw.strip())}</dt><dd{_cls("lv-" + lv if lv else "", "top" if i == 0 else "")}>{_e(v)}</dd>'
                   for i, (raw, v, lv) in enumerate(p.items))
    return f'<aside class="props">{head}<dl>{rows}</dl></aside>'


def _pane_html(p):
    facts = ('<dt>facts</dt><dd><ul class="facts">' + "".join(f'<li><span class="k">{_e(k)}</span> <b>{_e(v)}</b></li>' for k, v in p.facts)
             + "</ul></dd>") if p.facts else ""
    return f'<dl{_cls("pane", "lv-" + p.level)}><dt>what</dt><dd>{_e(p.what)}</dd>{facts}<dt>fix</dt><dd>{_e(p.fix)}</dd></dl>'


_TAG = {"err": "\u2716 ERR", "warn": "! WARN", "info": "\u00b7 INFO"}


def _finding_html(f):
    key = f"f-{f.id}"
    summary = (f'<summary><span class="tag {_e(f.level)}">{_TAG[f.level]}</span> <strong class="ft">{_e(f.title)}</strong> '
               f'<span class="fx">{_e(f.text)}</span></summary>')
    body = _pane_html(f.detail) if f.detail is not None else ""
    return (f'<div{_cls("fd", "lv-" + f.level)} data-k="{_e(key)}"><details data-k="{_e(key + "-d")}"{" open" if f.open else ""}>{summary}{body}</details></div>')


def _advice_html(a):
    if not (a.head or a.paras or a.notes):
        return ""
    paras = "".join("<p>" + "<br>".join(_e(x) for x in p) + "</p>" for p in a.paras)
    notes = "".join(f'<p{_cls("advice-" + k)}>{_e(t)}</p>' for k, t in a.notes)
    return f'<div{_cls("advice", "advice-" + a.kind if a.kind != "advice" else "")}><p class="advice-head">{_e(a.head)}</p>{paras}{notes}</div>'


# ---- the components of the AI screen (ui.py: Badge, Action, Controls, Question, Spec, Qa) ------------------------------------------------
#   <span class="tag ok|warn|err|accent">     Badge (a verdict: its symbol is in the text)
#   <form class="f" method="post" action="/ai/..">   Action: hidden inputs, an optional <input class="q" name>, <button class="bt bt-TONE" data-key>
#   <div class="controls"><div class="ci">   Controls          <div class="ask" role="group">   Question: <p>, the yes form, <a class="btn" data-key="n">No
#   <section class="spec"><dl class="spec-dl">   Spec: values in <code class="cmd"> when whole, in a tone class otherwise, a 'do' row of Actions
#   <div class="qa"><p class="q">   Qa: the question, then the answer as an Advice block or the waiting line
#   <div class="log" role="log"><div class="log-in">   Log: the chat's exchanges in a box that scrolls, its end in sight

_BADGE_CLASS = {"ok": "ok", "warn": "warn", "err": "err", "accent": "accent", "muted": ""}
_SAFE_PATH = re.compile(r"^/[a-z][a-z0-9/-]*$")  # an Action posts to a path of this server, never to another address


def _badge_html(b):
    return f'<span{_cls("tag", _BADGE_CLASS[b.tone])}>{_e(b.text)}</span>'


def _action_html(a):
    if not _SAFE_PATH.match(a.action):
        return ""
    hidden = "".join(f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">' for k, v in a.fields)
    off = " disabled" if a.disabled else ""
    ask = ""
    if a.ask is not None:
        name, hint, most = a.ask
        ask = f'<input class="q" type="text" name="{_e(name)}" maxlength="{int(most)}" placeholder="{_e(hint)}" aria-label="{_e(hint)}" autocomplete="off"{off}> '
    attrs = ' type="submit"' + (f' title="{_e(a.title)}"' if a.title else "") + (f' data-key="{_e(a.key)}"' if a.key else "") + off
    return (f'<form class="f" method="post" action="{_e(a.action)}">{hidden}{ask}<button{_cls("bt", "bt-" + a.tone if a.tone else "")}{attrs}>{_e(a.label)}</button></form>')


def _controls_html(c):
    return '<div class="controls">' + "".join(f'<div class="ci">{_inline(x)}</div>' for x in c.items) + "</div>"


def _question_html(q):
    yes = _action_html(q.yes) if q.yes is not None else ""
    link = _href(q.no_href)
    no = f'<a class="btn" href="{_e(link)}" data-key="n">No</a>' if link else ""
    return f'<div class="ask" role="group" aria-label="question"><p>{_e(q.text)}</p><div class="ask-b">{yes}{no}</div></div>'


def _spec_html(sp):
    rows = []
    for label, text, tone, whole in sp.items:
        value = f'<code class="cmd">{_e(text)}</code>' if whole else f'<span{_cls("t-" + tone)}>{_e(text)}</span>' if tone else _e(text)
        rows.append(f"<dt>{_e(label)}</dt><dd>{value}</dd>")
    if sp.actions:
        rows.append('<dt>do</dt><dd class="do">' + "".join(_action_html(a) for a in sp.actions) + "</dd>")
    return f'<section class="spec"><h3 class="sub">DETAILS</h3><p class="spec-t">{_e(sp.title)}</p><dl class="spec-dl">{"".join(rows)}</dl></section>'


def _qa_html(q):
    wait = ('<p class="pend">the model is writing the answer (a small model on a slow CPU may need a minute; this page updates by itself)</p>'
            if q.pending and q.answer is None else "")
    return (f'<div class="qa"><p class="q"><strong>{"advice" if q.kind == "advice" else "you"}:</strong> {_e(q.q)}</p>'
            + (html(q.answer) if q.answer is not None else "") + wait + "</div>")


_HTML = {
    ui.Badge: lambda n: f'<p class="ln">{_badge_html(n)}</p>',
    ui.Action: _action_html,
    ui.Controls: _controls_html,
    ui.Question: _question_html,
    ui.Spec: _spec_html,
    ui.Qa: _qa_html,
    ui.Log: lambda n: (f'<div class="log" role="log" aria-label="{_e(n.label)}"><div class="log-in">' + "".join(html(c) for c in n.children)
                       + "</div></div>"),
    ui.Title: _title_html,
    ui.Seg: _seg_html,
    ui.Legend: _legend_html,
    ui.Outline: lambda n: '<ul class="ol">' + "".join(_branch_html(b) for b in n.rows) + "</ul>",
    ui.Props: _props_html,
    ui.Series: _series_svg,
    ui.Cols: lambda n: '<div class="cols">' + "".join(f'<div class="col">{html(c)}</div>' for c, _w in n.children) + "</div>",
    ui.Pane: _pane_html,
    ui.Finding: _finding_html,
    ui.Advice: _advice_html,
    ui.Head: lambda n: f'<h3 class="sub">{_e(n.title)}' + (f' <span class="note">{_e(n.note)}</span>' if n.note else "") + "</h3>",
    ui.Indent: lambda n: '<div class="ind">' + "".join(html(c) for c in n.children) + "</div>",
    ui.Grid: lambda n: '<ul class="grid">' + "".join(_grid_item(x) for x in n.items) + "</ul>",
    ui.Timeline: _timeline_html,
    ui.NoteTable: lambda n: _table(ui.Table(n.cols, n.rows, None, n.head), n.notes),
    ui.Flow: _flow_html,
    ui.Cap: lambda n: "".join(html(c) for c in n.children),
    ui.Split: lambda n: ('<div class="split"><div class="sp-l">' + "".join(html(c) for c in n.left) + '</div><div class="sp-r">'
                         + "".join(html(c) for c in n.right) + "</div></div>"),
    ui.Only: lambda n: "".join(html(c) for c in n.children) if n.surface == "web" else "",
    ui.Tiles: _tiles_html,
}


html.escape, html.unescape = _stdhtml.escape, _stdhtml.unescape

# ======================================================================================================================================
# The shell (web.py shell pages; webcss.py styles them): markup builders, pure functions of the data they are given. Every control is a
# link or a form, every text is escaped here, and what goes inside a block (data-card) uses only the tags and attributes of
# webjs.FRAG_TAGS / FRAG_ATTRS, so that a later fragment poll can take it over. No script, no inline style.
# ======================================================================================================================================
STATE_WORD = {"ok": "ok", "warn": "warn", "err": "err", "down": "down", "unknown": "unknown", "info": "info"}


def esc(text):
    """Text or an attribute value, escaped."""
    return html.escape(str(text), quote=True)


def rev(*parts):
    """The revision of a block: it changes when, and only when, the block's HTML does."""
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace") + b"\0")
    return h.hexdigest()[:10]


def state_symbol(state):
    return f'<span class="sym s-{esc(state)}" aria-hidden="true">{esc(ui.SYMBOLS.get(state, "?"))}</span>'


def state_chip(state):
    """The chip of a card header: the symbol and the word, the colour only a third cue."""
    return f'<span class="chip st-{esc(state)}">{esc(ui.SYMBOLS.get(state, "?"))} {esc(STATE_WORD.get(state, state))}</span>'


def block(tag, card, cls, inner, extra=""):
    """An element the page is made of: data-card says which, data-rev changes with its content (the fragment poll swaps by it)."""
    return f'<{tag} class="{esc(cls)}" data-card="{esc(card)}" data-rev="{rev(cls, extra, inner)}"{extra}>{inner}</{tag}>'


def tab(key, label, href, current, badge=""):
    return (f'<a href="{esc(href)}" data-key="{esc(key)}"' + (' aria-current="page"' if current else "")
            + f'><kbd>{esc(key)}</kbd>{esc(label)}' + (f' <span class="badge">{badge}</span>' if badge else "") + "</a>")


def topbar(host, pill_state, pill_text, tabs, clock, help_href, settings_href, settings_current):
    """The top bar block (data-card="__top"): host, the status pill, the tabs (already built by tab()), the clock, ? and the settings."""
    inner = (f'<span class="host">{esc(host)}</span><span class="status {esc(pill_state)}" role="status">{esc(pill_text)}</span>'
             f'<nav class="tabs" aria-label="Screens">{"".join(tabs)}</nav><div class="tb-r"><time class="clock">{esc(clock)}</time>'
             f'<a class="ib" href="{esc(help_href)}" data-key="?" aria-label="Help: keyboard shortcuts" title="Help (?)">?</a>'
             f'<a class="ib" href="{esc(settings_href)}" aria-label="Settings" title="Settings"'
             + (' aria-current="page"' if settings_current else "") + ">\u2699\ufe0e</a></div>")
    return block("header", "__top", "topbar", inner)


def spark_svg(values):
    """A sparkline as an SVG polyline (attributes only), None for fewer than two points."""
    vals = [float(v) for v in values][-60:]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pts = " ".join(f"{i * 100.0 / (len(vals) - 1):.1f},{22.0 - (v - lo) * 20.0 / span:.1f}" for i, v in enumerate(vals))
    return f'<svg class="spk" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><polyline points="{pts}"/></svg>'


def kpi_tile(k, href=""):
    """One KPI as a tile (a link to its card or view when href is given). An unknown value is '?' without a symbol, dashed."""
    sym = "" if k.state == "unknown" else state_symbol(k.state)
    spark = spark_svg(k.spark.values) if k.spark is not None else ""
    value = f'<span class="kv">{sym}{esc(k.value)}' + (f'<span class="u">{esc(k.unit)}</span>' if k.unit else "") + "</span>"
    line = f'<span class="kline">{value}{spark}</span>' if spark else value
    tag, attrs = ("a", f' href="{esc(href)}"') if href else ("div", "")
    return (f'<{tag} class="kpi st-{esc(k.state)}" role="listitem" data-state="{esc(k.state)}"{attrs}><span class="kl">{esc(k.label)}</span>{line}'
            + (f'<span class="ks">{esc(k.hint)}</span>' if k.hint else "") + f"</{tag}>")


def kpis_block(tiles, banner=""):
    """The KPI row block (data-card="__kpis"); the banner (a collector that is not running...) goes above the tiles."""
    return block("div", "__kpis", "kpiblock", banner + f'<div class="kpis" role="list" aria-label="Key figures">{"".join(tiles)}</div>')


def banner(sym, bold, small=""):
    return f'<div class="stale-banner" role="status"><span class="sym">{esc(sym)}</span> <b>{esc(bold)}</b>' + (f' <span class="sm">{small}</span>' if small else "") + "</div>"


ROW_UNIT = 0.5  # em: the grid's implicit rows (webcss.GRID); a card spans as many as its estimated height needs
ROW_MAX = 200  # the style sheet has one class per span, r1..rROW_MAX (webcss.row_classes): a taller card spans no more (its rows grow with it)
CHARS_PER_COL = 44  # characters of text that fit one line of a card per grid quarter at the narrowest width a quarter gets: wrapping is never underestimated
_BLOCK_END = re.compile(r"</(?:tr|p|li|div|h3|h4|pre|summary|details|dd|dt|dl|table|ul|ol|section)>|<br\s*/?>")
_CELL_END = re.compile(r"</t[dh]>")
_MARKUP = re.compile(r"<[^>]*>")


def _flow(m):
    """The items of a chip list (ul.wrap) or of a grid of small cells (ul.grid) run together in a line: marked so that they are one text, and weighted."""
    return "<q>" + m.group(2).replace("</li>", " ").replace("<li", "<i") + "</q>"


_WALL_HIDDEN = re.compile(r'<details class="(?:fix|dt)".*?</details>|<ul class="grid".*?</ul>|<(\w+) class="[^"]*\b(?:why|hint|sm)\b[^"]*"[^>]*>.*?</\1>', re.S)  # as the style sheet hides them
_FLOWS = re.compile(r'<ul class="(wrap|grid)"[^>]*>(.*?)</ul>', re.S)


def est_rows(inner, size, note="", wall=False):
    """How many ROW_UNIT rows a card of this body needs, as a cheap estimate for the grid's dense packing (the server cannot measure): the
    text is cut into lines at the ends of the block elements, a line longer than the card is wide wraps, a table row costs a little more than
    a line, chips (a ul.wrap) run together and cost more per line, and the header and the paddings are added. The rows are `auto` tracks:
    a card that turns out taller than its span only makes its rows taller (it never overlaps the next card), one that is shorter leaves a
    hole of the difference, so the weights aim at the real height and the estimate is rounded up."""
    if wall:  # the wall's page is a 6-column grid (its text is large) and hides what only a reader needs: fixes, hints, the small print, lists of cells
        size = min(4, size * 2)
        inner = _WALL_HIDDEN.sub("", inner)
    cpl = max(12, CHARS_PER_COL * max(1, min(4, size)))
    chips = 0.0
    def run(m):
        nonlocal chips
        text = html.unescape(_MARKUP.sub("", m.group(1))).strip()
        chips += 1.9 * (-(-int(len(text) * 1.35) // cpl))
        return "\n"
    flowed = _FLOWS.sub(_flow, inner)
    flowed = re.sub(r"<q>(.*?)</q>", run, flowed, flags=re.S)
    text = _CELL_END.sub(" ", flowed)
    em = 3.6 + chips  # header, the padding of the body, the gap below the card
    for line in _MARKUP.sub("", _BLOCK_END.sub("\n", text)).split("\n"):
        line = html.unescape(line).strip()
        if line:
            em += 1.3 * (-(-len(line) // cpl))
    em += 0.7 * flowed.count("<tr") + 0.4 * flowed.count("<li")  # a table row or a list item has its own padding
    em += 0.9 * (flowed.count(' class="msg') + flowed.count(" bl\"")) + 1.2 * flowed.count('class="bx"')  # a message, a bar line and the note under it
    if len(note) > cpl:
        em += 0.4 * (-(-len(note) // cpl) - 1)
    return max(1, min(ROW_MAX, int(-(-em // ROW_UNIT))))


def card_article(card, title, note, state, size, inner, more="", tools="", off=False, rows=0):
    """A card of the grid: width class sN, state class st-..., the header (title, note, state chip) and the body (inner is built by the caller).
    tools: the layout editor's buttons (built by the page): they come between the header and the body, and the body is then inert (a
    preview: nothing in it can be clicked or focused while the cards are being arranged). off: the card is hidden in the layout (the editor lists it dimmed).
    rows: the estimated height in grid rows (est_rows): the class rN makes the card span that many, so that the overview packs without holes."""
    body = f'<div class="ch"><h3>{esc(title)}</h3>' + (f'<span class="note">{esc(note)}</span>' if note else "") + f"{state_chip(state)}</div>" + tools
    body += f'<div class="cb"{" inert" if tools else ""}>{inner}' + (f'<p class="more">{more}</p>' if more else "") + "</div>"
    return block("article", card, f"card s{size} st-{state}" + (" off" if off else "") + (f" r{rows}" if rows else ""), body, f' id="c-{esc(card)}" data-state="{esc(state)}"' + (f' data-title="{esc(title)}"' if tools else ""))


TITLE_LINE = re.compile(r"\s*\u2500{2} (.+?) \u2500{2,}(?:  (.*))?")  # ansi.section(): '\u2500\u2500 TITLE \u2500\u2500\u2500\u2500  note'


def plain(line):
    return ESC.sub("", SGR.sub("", line))


def ansi_card(lines):
    """(the note of the section's title line, the card body) for the lines of a card that is not built of components yet (ANSI, as the console
    draws them): the title line (the card's header says it) and the blank lines above the text are left out, the rest are colour spans in a <pre>."""
    lines, note = [x.rstrip("\r") for x in lines], ""
    while lines and not plain(lines[0]).strip():
        lines.pop(0)
    m = TITLE_LINE.fullmatch(plain(lines[0])) if lines else None
    if m:
        note = (m.group(2) or "").strip()
        lines.pop(0)
    while lines and not plain(lines[0]).strip():
        lines.pop(0)
    return note, '<pre class="tty" tabindex="0">' + to_html("\n".join(lines)) + "</pre>"


def seg(label, options):
    """A group of links like a segmented control: options [(text, href or None, current, attrs)]. The chosen one is marked aria-current and is
    not a link; an option with no href is shown dimmed (aria-disabled). attrs: extra attributes of a link (data-set, data-theme...)."""
    out = []
    for text, href, current, attrs in options:
        if current:
            out.append(f'<span aria-current="true">{esc(text)}</span>')
        elif href is None:
            out.append(f'<span aria-disabled="true">{esc(text)}</span>')
        else:
            out.append(f'<a href="{esc(href)}"{attrs}>{esc(text)}</a>')
    return f'<div class="seg" role="group" aria-label="{esc(label)}">{"".join(out)}</div>'


def keys_html(shown):
    """'Tab/Shift+Tab', '1-5', '\u2190\u2192 PgUp/PgDn': each key in a <kbd>."""
    return " ".join(f"<kbd>{esc(k)}</kbd>" for k in shown.split())


def help_dialog(groups, close_href="#"):
    """#help: the keys of the screen (ui.help_rows), shown by :target (no script): a link to #help opens it, a link to # closes it."""
    cols = "".join(f"<section><h3>{esc(title)}</h3><dl>" + "".join(f"<dt>{keys_html(k)}</dt><dd>{esc(what)}</dd>" for k, what in items) + "</dl></section>"
                   for title, items in groups)
    return (f'<section class="ov" id="help" role="dialog" aria-modal="true" aria-labelledby="help-title"><a class="ov-bg" href="{esc(close_href)}" '
            f'tabindex="-1" aria-label="Close help"></a><div class="dlg"><header><h2 id="help-title">Keyboard</h2><a class="ib" href="{esc(close_href)}" '
            f'data-key="? Escape" aria-label="Close help">\u2715</a></header><div class="keys">{cols}</div></div></section>')


def foot(groups, updated):
    """The footer: the groups (strings), a dot between two, and when it was drawn."""
    return ('<footer class="foot">' + '<span class="dot">\u00b7</span>'.join(groups) + f'<span class="upd">updated {esc(updated)}</span></footer>')


def shell_doc(title, css_href, body, theme="auto", density="desk", zoom=100, shift=0, kiosk=False, prefs_src="config", prefs="", refresh=0,
              extra_class="", script=(), paused=False):
    """The whole page. <html> carries what the style sheet and the scripts read: data-theme, data-density, the classes zNNN and shift-N (paused when
    the redraw is paused), data-prefs-src and data-prefs. refresh: the meta refresh of the page (0 = none); script: the inline scripts, in order
    (one text, or a list; none: the page has no script and its meta refresh is plain)."""
    scripts = [script] if isinstance(script, str) else list(script)
    cls = f"z{int(zoom)} shift-{int(shift)}" + (" paused" if paused else "") + (f" {esc(extra_class)}" if extra_class else "")
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    if meta and any(scripts):
        meta = f"<noscript>{meta}</noscript>"  # a page with scripts refreshes itself: the meta is for browsers without them
    return ('<!doctype html>'
            f'<html lang="en" data-theme="{esc(theme)}" data-density="{esc(density)}" class="{cls}" data-prefs-src="{esc(prefs_src)}"'
            + (f' data-prefs="{esc(prefs)}"' if prefs else "") + (" data-kiosk" if kiosk else "") + ">"
            f'<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark light">'
            f'{meta}<title>{esc(title)}</title><link rel="stylesheet" href="{esc(css_href)}">'
            + f"</head><body>{body}" + "".join(f"<script>{x}</script>" for x in scripts if x)
            + "</body></html>")
