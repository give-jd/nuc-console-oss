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
