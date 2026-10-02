"""ANSI screen -> HTML (stdlib only): shared by the web view (web.py) and the full-screen kiosk (render.py --kiosk).

Everything is escaped; only <span class> elements with fixed class names are produced. No JavaScript, ever.
"""
import html
import re

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
    return f"<span{_cls(*names)}>{_e(span.text)}</span>" if any(names) else _e(span.text)


_SPARK_W, _SPARK_H = 80, 16
_BAR_W, _BAR_H = 100, 8


def _bar_svg(b):
    if b.frac is None:
        return '<span class="st-unknown" role="img" aria-label="unknown">?</span>'
    pct = round(b.frac * 100)
    svg = (f'<svg{_cls("bar", "st-" + b.state)} viewBox="0 0 {_BAR_W} {_BAR_H}" width="{_BAR_W}" height="{_BAR_H}" role="img" '
           f'aria-label="{pct}%" preserveAspectRatio="none"><rect class="bg" x="0" y="0" width="{_BAR_W}" height="{_BAR_H}"/>'
           f'<rect class="fg" x="0" y="0" width="{round(b.frac * _BAR_W, 1):g}" height="{_BAR_H}"/></svg>')
    return svg + (f'<span class="n">{_e(b.value_text)}</span>' if b.value_text else "")


def _spark_svg(sp):
    vals = sp.values
    if not vals:
        return f'<svg class="spark" viewBox="0 0 {_SPARK_W} {_SPARK_H}" width="{_SPARK_W}" height="{_SPARK_H}" role="img" aria-label="no data"></svg>'
    top = max(max(vals), sp.floor, 1e-9)
    step = _SPARK_W / max(len(vals) - 1, 1)
    pts = " ".join(f"{i * step:.1f},{_SPARK_H - 1 - v / top * (_SPARK_H - 2):.1f}" for i, v in enumerate(vals))
    return (f'<svg class="spark" viewBox="0 0 {_SPARK_W} {_SPARK_H}" width="{_SPARK_W}" height="{_SPARK_H}" role="img" aria-label="trend">'
            f'<polyline fill="none" points="{pts}"/></svg>')


def _inline(x):
    """A Span, a Line, a Bar or a Spark as markup that sits in a line or a cell."""
    if isinstance(x, ui.Span):
        return _span(x)
    if isinstance(x, ui.Line):
        return "".join(_inline(p) for p in x.spans)
    if isinstance(x, ui.Bar):
        return _bar_svg(x)
    if isinstance(x, ui.Spark):
        return _spark_svg(x)
    return '<span class="st-unknown">?</span>'


def _table(t):
    heads = "".join(f'<th{_cls("r" if c.align == "r" else "", "n" if c.num else "", ("p%d" % c.prio) if c.prio else "")} scope="col">'
                    f"{_e(c.label)}</th>" for c in t.cols)
    marks = {i: label for label, i in (t.groups or ())}
    body = []
    for i, r in enumerate(t.rows):
        if i in marks:
            body.append(f'<tr class="grp"><th colspan="{len(t.cols)}" scope="rowgroup">{_e(marks[i])}</th></tr>')
        link = _href(r.href)
        cells = []
        for j, (c, cell) in enumerate(zip(t.cols, r.cells)):
            inner = _inline(cell)
            if j == 0 and link:
                inner = f'<a href="{_e(link)}">{inner}</a>'
            cells.append(f'<td{_cls("r" if c.align == "r" else "", "n" if c.num else "", ("p%d" % c.prio) if c.prio else "")}>{inner}</td>')
        key = f' data-key="{_e(r.key)}"' if r.key is not None else ""
        body.append(f'<tr{_cls("t-" + r.tone if r.tone else "")}{key}>{"".join(cells)}</tr>')
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
    if isinstance(node, (ui.Span, ui.Line, ui.Bar, ui.Spark)):
        return f'<p class="ln">{_inline(node)}</p>'
    if isinstance(node, ui.Msg):  # a Notice too
        notice = isinstance(node, ui.Notice)
        role = ' role="status"' if notice else ""
        return (f'<p{_cls("msg", "notice" if notice else "", "lv-" + node.level)}{role}>'
                f'<span class="sym">{_e(ui.SYMBOLS[node.level])}</span> {_e(node.text)}</p>')
    if isinstance(node, ui.KV):
        return '<dl class="kv">' + "".join(f"<dt>{_e(k)}</dt><dd>{_inline(v)}</dd>" for k, v in node.pairs) + "</dl>"
    if isinstance(node, ui.Table):
        return _table(node)
    if isinstance(node, ui.Wrap):
        lm = f' data-max-lines="{int(node.max_lines)}"' if node.max_lines else ""
        return f'<ul class="wrap"{lm}>' + "".join(f"<li>{_inline(x)}</li>" for x in node.items) + "</ul>"
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
    return '<span class="st-unknown">?</span>'


html.escape, html.unescape = _stdhtml.escape, _stdhtml.unescape
