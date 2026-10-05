"""nuc-console web view: the style sheet of the new shell, as Python string constants (stdlib only, Python 3.8+).

The installers copy only src/*.py, so the CSS lives here and web.py serves it at /s/app.<sha8>.css (ASSETS, immutable: the name carries
the first 8 digits of the SHA-256, a changed sheet is a new URL). Nothing is built from request data.

  tokens      one table of colours per theme, on :root[data-theme=...]: dark, light, high-contrast; `auto` (or no attribute) follows
              prefers-color-scheme and prefers-contrast, and forced-colors hands the colours to the system
  densities   html[data-density=wall|desk|compact] set the base font size and the paddings; the zoom classes z50..z200 (A-/A+) scale it
  components  top bar, tabs, status pill, KPI tiles, the card grid (12 / 6 / 1 columns by container queries), card header and state chip,
              tables, bars and sparklines, footer, settings page, the layout editor (?edit=1), help (:target), stale banner, the AI page's forms
  legacy      the classes of htmlview (.r .g .y ... and the CSS of the Map, CPU, Health and AI pages) with their colours turned into the
              variables above: the existing bodies of those pages sit in the shell and follow its theme (themed())

A state is never colour alone: every state has its symbol (ui.SYMBOLS) and an unknown one is dashed.
"""
import hashlib
import re

import htmlview

GZOOMS = (50, 67, 80, 100, 125, 150, 200, 250, 300)  # the graph view's sizes (web.GZOOMS is this)
SANS = 'system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans","Helvetica Neue",Arial,sans-serif'
MONO = 'ui-monospace,"SF Mono","Cascadia Mono","JetBrains Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace'

# ---- tokens ------------------------------------------------------------------------------------------------------------------------

DARK = (("bg", "#0c0f14"), ("surface", "#131922"), ("surface-2", "#19212c"), ("line", "#253041"), ("line-2", "#37455a"),
        ("fg", "#d3dbe5"), ("fg-strong", "#f2f5f9"), ("muted", "#8693a5"), ("faint", "#5d6b7e"),
        ("accent", "#72b2ff"), ("accent-bg", "rgba(114,178,255,.14)"), ("bar", "#5d7290"),
        ("ok", "#47c15b"), ("warn", "#e3a73b"), ("err", "#f26a5e"), ("cyan", "#4fc3cf"), ("magenta", "#c79bff"),
        ("ok-bg", "rgba(71,193,91,.14)"), ("warn-bg", "rgba(227,167,59,.15)"), ("err-bg", "rgba(242,106,94,.15)"),
        ("err-solid", "#d6372d"), ("ok-solid", "#238636"), ("on-solid", "#ffffff"), ("focus", "#9cc9ff"),
        ("scrim", "rgba(3,5,8,.64)"), ("shadow", "rgba(0,0,0,.5)"), ("color-scheme", "dark"))
LIGHT = (("bg", "#eef1f5"), ("surface", "#ffffff"), ("surface-2", "#f4f6f9"), ("line", "#d5dbe4"), ("line-2", "#b4bfce"),
         ("fg", "#1c2430"), ("fg-strong", "#0a0f17"), ("muted", "#5b6677"), ("faint", "#8794a6"),
         ("accent", "#0a5fc2"), ("accent-bg", "rgba(10,95,194,.10)"), ("bar", "#8ea2bd"),
         ("ok", "#1b7f37"), ("warn", "#9b6100"), ("err", "#c42a31"), ("cyan", "#0b7285"), ("magenta", "#7c3fc0"),
         ("ok-bg", "rgba(27,127,55,.10)"), ("warn-bg", "rgba(155,97,0,.11)"), ("err-bg", "rgba(196,42,49,.09)"),
         ("err-solid", "#c42a31"), ("ok-solid", "#1b7f37"), ("on-solid", "#ffffff"), ("focus", "#0a5fc2"),
         ("scrim", "rgba(16,24,36,.42)"), ("shadow", "rgba(20,30,48,.22)"), ("color-scheme", "light"))
HIGH = (("bg", "#000000"), ("surface", "#000000"), ("surface-2", "#0e0e0e"), ("line", "#ffffff"), ("line-2", "#ffffff"),
        ("fg", "#ffffff"), ("fg-strong", "#ffffff"), ("muted", "#d6d6d6"), ("faint", "#bdbdbd"),
        ("accent", "#00e1ff"), ("accent-bg", "rgba(0,225,255,.20)"), ("bar", "#ffffff"),
        ("ok", "#3dff6e"), ("warn", "#ffd400"), ("err", "#ff5c5c"), ("cyan", "#00e1ff"), ("magenta", "#ff8cff"),
        ("ok-bg", "rgba(61,255,110,.18)"), ("warn-bg", "rgba(255,212,0,.18)"), ("err-bg", "rgba(255,92,92,.20)"),
        ("err-solid", "#ff5c5c"), ("ok-solid", "#3dff6e"), ("on-solid", "#000000"), ("focus", "#00e1ff"),
        ("scrim", "rgba(0,0,0,.8)"), ("shadow", "rgba(0,0,0,.9)"), ("color-scheme", "dark"))
THEME_TABLES = {"dark": DARK, "light": LIGHT, "high-contrast": HIGH}


def decls(table):
    return ";".join(f"{k}:{v}" if k == "color-scheme" else f"--{k}:{v}" for k, v in table)


def tokens():
    """The theme rules. Dark is the default of :root; `auto` (or no attribute) switches by the system's own settings."""
    auto = ':root:not([data-theme]),:root[data-theme="auto"]'
    return (f":root{{--sans:{SANS};--mono:{MONO};{decls(DARK)}}}"
            f"@media (prefers-color-scheme:light){{{auto}{{{decls(LIGHT)}}}}}"
            f"@media (prefers-contrast:more){{{auto}{{{decls(HIGH)}}}}}"
            + "".join(f':root[data-theme="{name}"]{{{decls(table)}}}' for name, table in THEME_TABLES.items())
            # forced colours: the system's palette, the borders and the symbols carry the meaning
            + "@media (forced-colors:active){:root{--bg:Canvas;--surface:Canvas;--surface-2:Canvas;--fg:CanvasText;--fg-strong:CanvasText;"
              "--muted:CanvasText;--faint:GrayText;--line:CanvasText;--line-2:CanvasText;--accent:LinkText;--ok:CanvasText;--warn:CanvasText;"
              "--err:CanvasText;--focus:Highlight}.card,.kpi,.chip,.status,.pl,.tag,.bt,.ib,.seg{border:1px solid CanvasText}"
              ".bar>i,.spk polyline{forced-color-adjust:none;background:Highlight;stroke:Highlight}}")


# ---- densities and zoom ------------------------------------------------------------------------------------------------------------

ZOOMS = (50, 67, 75, 90, 100, 110, 125, 150, 175, 200)  # web.ZOOMS: the A-/A+ steps (%), one class each


def density():
    return ("html{--fs:14px;--pad:.85em;--gap:.8em;--z:1}"
            'html[data-density="wall"]{--fs:clamp(18px,1.4vw,30px);--pad:1em;--gap:1em}'
            'html[data-density="desk"]{--fs:14px;--pad:.85em;--gap:.8em}'
            'html[data-density="compact"]{--fs:12.5px;--pad:.6em;--gap:.55em}'
            + "".join(f"html.z{z}{{--z:{z / 100:g}}}" for z in ZOOMS)
            + 'html[data-density="wall"] .sm{display:none}')


# ---- the shell ---------------------------------------------------------------------------------------------------------------------

BASE = """
*,*::before,*::after{box-sizing:border-box}
html{background:var(--bg);-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;container:app / inline-size;background:var(--bg);color:var(--fg);
font:calc(var(--fs) * var(--z))/1.4 var(--sans);font-variant-emoji:text}
body>*{flex:none}body>main{flex:1 0 auto}
a{color:var(--accent)}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.sr-only{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.sm{color:var(--muted);font-size:.84em}
.mut{color:var(--muted)}
.s-err{color:var(--err)}.s-warn{color:var(--warn)}.s-ok{color:var(--ok)}.s-unknown{color:var(--warn)}.s-info{color:var(--muted)}.s-down{color:var(--err)}
.sym{font-family:var(--mono);font-weight:700;display:inline-block;min-width:1.2em;text-align:center}
.s-unknown .sym,.sym.unknown{text-decoration:underline dashed;text-underline-offset:.2em;text-decoration-thickness:1px}
kbd{font:600 .8em var(--mono);padding:.08em .42em;border:1px solid var(--line-2);border-bottom-width:2px;border-radius:4px;background:var(--surface-2);color:var(--fg-strong);white-space:nowrap}
code.cmd{font:.9em var(--mono);background:var(--surface-2);border:1px solid var(--line);border-radius:4px;padding:.02em .35em;overflow-wrap:anywhere;color:var(--fg);user-select:all}
html.stale body>main,html.stale .kpis{filter:grayscale(1);opacity:.68}
html.paused .clock{color:var(--muted)}
"""

# the burn-in shift of the header: one character, every ten minutes, three positions
SHIFT = "html.shift-1 .topbar{padding-left:calc(var(--pad) + 1ch)}html.shift-2 .topbar{padding-left:calc(var(--pad) + 2ch)}"

CONTROLS = """
.ib{display:inline-grid;place-items:center;min-width:2em;height:2em;padding:0 .4em;border:1px solid var(--line-2);border-radius:6px;background:var(--surface-2);color:var(--fg-strong);text-decoration:none;font-weight:700}
.ib:hover,.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn{display:inline-flex;align-items:center;gap:.4em;padding:.3em .8em;border:1px solid var(--line-2);border-radius:6px;background:var(--surface-2);color:var(--fg-strong);text-decoration:none;cursor:pointer;white-space:nowrap}
.btn.pri{background:var(--accent-bg);border-color:var(--accent);color:var(--accent);font-weight:600}
.lnk{color:var(--accent);text-decoration:underline;text-underline-offset:.15em;border-radius:3px;padding:.05em .15em}
.lnk:hover{text-decoration-thickness:2px}
button.lnk{background:none;border:0;cursor:pointer;margin-top:.4em}
.lnk[aria-pressed="true"]{color:var(--fg-strong);font-weight:600}
.lnk[aria-current="true"],span.lnk{color:var(--fg-strong);text-decoration:none;font-weight:600}
span.lnk[aria-disabled="true"]{color:var(--muted);font-weight:400;opacity:.7}
.seg{display:inline-flex;max-width:100%;flex-wrap:wrap;border:1px solid var(--line-2);border-radius:6px;background:var(--surface);overflow:hidden}
.seg a,.seg span{display:block;padding:.3em .8em;color:var(--muted);white-space:nowrap;text-decoration:none}
.seg>*+*{border-left:1px solid var(--line-2)}
.seg a:hover{color:var(--fg-strong)}
.seg [aria-current="true"]{background:var(--accent-bg);color:var(--accent);font-weight:600}
.seg [aria-disabled="true"]{opacity:.5}
"""

TOPBAR = """
.host{font:700 1em var(--mono);color:var(--fg-strong);padding-bottom:.4em}
.topbar{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;align-items:center;gap:.3em .9em;padding:.4em var(--pad) 0;background:var(--surface);border-bottom:1px solid var(--line)}
.status{font:700 .82em var(--mono);letter-spacing:.03em;padding:.22em .75em;border-radius:99px;background:var(--err-solid);color:var(--on-solid);margin-bottom:.4em;white-space:nowrap}
.status.ok{background:var(--ok-bg);color:var(--ok);box-shadow:inset 0 0 0 1px var(--ok)}
.status.warn{background:var(--warn-bg);color:var(--warn);box-shadow:inset 0 0 0 1px var(--warn)}
.status.unknown{background:var(--surface-2);color:var(--muted);box-shadow:inset 0 0 0 1px var(--line-2)}
.tabs{display:flex;gap:.1em;min-width:0;flex:1 1 auto;overflow-x:auto;scrollbar-width:none;align-self:stretch}
.tabs::-webkit-scrollbar{display:none}
.tabs a{display:inline-flex;align-items:center;gap:.45em;padding:.5em .8em;border-bottom:2px solid transparent;color:var(--muted);text-decoration:none;white-space:nowrap}
.tabs a:hover{color:var(--fg-strong)}
.tabs a kbd{font-size:.72em;padding:.02em .35em;border-bottom-width:1px;color:var(--muted)}
.tabs a[aria-current="page"]{color:var(--fg-strong);font-weight:600;border-bottom-color:var(--accent);background:var(--accent-bg)}
.tabs a[aria-current="page"] kbd{color:var(--accent);border-color:var(--accent)}
.badge{font:700 .72em var(--mono);padding:.08em .5em;border-radius:99px;background:var(--surface-2);border:1px solid var(--line-2);color:var(--fg)}
.tb-r{display:flex;align-items:center;gap:.5em;margin-left:auto;padding-bottom:.4em}
.tb-r .ib[aria-current="page"]{border-color:var(--accent);color:var(--accent)}
.clock{font:600 .95em var(--mono);font-variant-numeric:tabular-nums;color:var(--fg-strong);margin-right:.3em}
@container app (max-width:46em){
  .tabs{order:5;flex:1 0 100%;margin-inline:calc(var(--pad) * -1);padding-inline:var(--pad)}
  .tb-r{margin-left:auto;gap:.4em}
  .topbar{gap:.2em .6em}
  .host,.status,.tb-r{padding-bottom:.3em}
  .status{margin-bottom:.3em;font-size:.72em;padding:.2em .6em}
  .clock{font-size:.84em;margin-right:0}
  .tb-r .ib{min-width:1.9em;height:1.9em}
}
.stale-banner{display:flex;flex-wrap:wrap;align-items:baseline;gap:.2em .6em;margin:var(--pad) var(--pad) 0;padding:.55em .8em;border:1px dashed var(--warn);border-radius:8px;background:var(--warn-bg);color:var(--fg)}
.stale-banner[hidden]{display:none}
.stale-banner .sym{color:var(--warn);min-width:0}
"""

KPIS = """
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5em,1fr));gap:var(--gap);padding:var(--pad)}
.kpiblock>.kpis:not(:first-child){padding-top:0}
.kpi{display:flex;flex-direction:column;gap:.15em;min-width:0;text-align:left;padding:.6em .8em .65em;background:var(--surface);border:1px solid var(--line);border-radius:8px;color:inherit;text-decoration:none}
a.kpi:hover{border-color:var(--line-2)}
.kpi.st-err,.kpi.st-down{border-color:color-mix(in srgb,var(--err) 50%,var(--line))}
.kpi.st-warn{border-color:color-mix(in srgb,var(--warn) 38%,var(--line))}
.kpi.st-unknown{border-style:dashed;border-color:color-mix(in srgb,var(--warn) 60%,var(--line))}
.kpi .kl{font:600 .68em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.kpi .kv{font:700 1.65em/1.15 var(--mono);font-variant-numeric:tabular-nums;color:var(--fg-strong);display:flex;align-items:baseline;gap:.3em;white-space:nowrap}
.kpi .kv .sym{font-size:.62em;min-width:0}
.kpi .kv .u{font-size:.55em;font-weight:600;color:var(--muted)}
.kpi .ks{font-size:.76em;color:var(--muted);line-height:1.3;overflow-wrap:anywhere}
.kpi.st-unknown .kv{color:var(--warn)}
.kpi .kline{display:flex;align-items:center;gap:.6em;min-width:0}
.kpi .kline .spk{flex:1 1 3em;min-width:2.4em;max-width:7em;margin-left:auto}
.spk{display:block;height:1.5em;overflow:visible}
.spk polyline{fill:none;stroke:var(--accent);stroke-width:1.5;vector-effect:non-scaling-stroke;stroke-linejoin:round}
@container app (max-width:84.3em){.kpis{grid-template-columns:repeat(4,minmax(0,1fr))}}
@container app (max-width:45.7em){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi{padding:.45em .6em .5em}.kpi .kv{font-size:1.38em}}
"""

ROW_MAX = 200  # htmlview.ROW_MAX: the classes r2..rROW_MAX, one per number of rows a card can span (a test keeps the two equal)
ROW_CLASSES = "".join(f".card.r{n}{{grid-row:span {n}}}" for n in range(2, ROW_MAX + 1))

GRID = """
.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:var(--gap);align-items:start;padding:0 var(--pad) var(--pad)}
.card{grid-column:span 3;min-width:0;container:card / inline-size;background:var(--surface);border:1px solid var(--line);border-radius:8px;scroll-margin-top:5em}
.card.s1{grid-column:span 3}.card.s2{grid-column:span 6}.card.s3{grid-column:span 9}.card.s4{grid-column:span 12}
/* The overview packs its cards densely: the implicit rows are a half em high (and `auto`, so a card taller than its estimate grows them
   instead of overlapping the next), each card spans the rows its estimated height needs (the server's class rN, htmlview.est_rows), the
   gap below a card is its margin, and `dense` lets a later card fill a hole an earlier, taller one left. The editor's page keeps the plain
   rows: its cards are cut to a preview and stay strictly in order. */
main.grid:not([data-edit]){grid-auto-flow:row dense;grid-auto-rows:minmax(.5em,auto);row-gap:0}
main.grid:not([data-edit]) .card{margin-bottom:var(--gap)}
@container app (max-width:84.3em){
  .grid{grid-template-columns:repeat(6,minmax(0,1fr))}
  .card.s1{grid-column:span 3}.card.s2,.card.s3,.card.s4{grid-column:span 6}
}
@container app (max-width:45.7em){
  .grid{grid-template-columns:minmax(0,1fr)}
  .card.s1,.card.s2,.card.s3,.card.s4{grid-column:auto}
  main.grid:not([data-edit]){grid-auto-flow:row}
  main.grid:not([data-edit]) .card[class]{grid-row:auto}
}
@@ROWS@@
.card:target{outline:2px solid var(--accent);outline-offset:2px}
.card.st-err,.card.st-down{border-color:color-mix(in srgb,var(--err) 55%,var(--line))}
.card.st-unknown{border-style:dashed}
.ch{display:flex;flex-wrap:wrap;align-items:baseline;gap:.15em .7em;padding:.6em var(--pad) .5em;border-bottom:1px solid var(--line)}
.ch h3{margin:0;font:650 .72em/1.3 var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-strong)}
.ch .note{color:var(--muted);font-size:.8em;min-width:0;overflow-wrap:anywhere}
.chip{margin-left:auto;align-self:center;font:700 .7em/1.4 var(--mono);letter-spacing:.03em;padding:.12em .6em;border-radius:4px;border:1px solid transparent;white-space:nowrap}
.chip.st-err,.chip.st-down{background:var(--err-bg);color:var(--err);border-color:color-mix(in srgb,var(--err) 45%,transparent)}
.chip.st-warn{background:var(--warn-bg);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.chip.st-ok{background:var(--ok-bg);color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,transparent)}
.chip.st-unknown{background:var(--warn-bg);color:var(--warn);border:1px dashed var(--warn)}
.chip.st-info{background:var(--surface-2);color:var(--muted);border-color:var(--line-2)}
.cb{padding:.7em var(--pad) .8em;min-width:0;overflow-x:auto}
.cb>*+*{margin-top:.65em}
.cb pre,pre.tty{margin:0;overflow-x:auto;font:.86em/1.3 var(--mono);font-variant-ligatures:none;white-space:pre;color:var(--fg);scrollbar-width:thin;scrollbar-color:var(--line-2) transparent;
background:linear-gradient(to right,var(--surface) 30%,transparent) left center/1.6em 100% no-repeat local,linear-gradient(to left,var(--surface) 30%,transparent) right center/1.6em 100% no-repeat local,
linear-gradient(to right,var(--line-2),transparent) left center/.5em 100% no-repeat scroll,linear-gradient(to left,var(--line-2),transparent) right center/.5em 100% no-repeat scroll}
.cb pre::-webkit-scrollbar{height:.5em}.cb pre::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:99px}
.more{color:var(--muted);font-size:.88em}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;min-width:0}
.view{padding:0 var(--pad) var(--pad);min-width:0}
.view>*+*{margin-top:var(--gap)}
.vb{display:contents}.vb>*+*{margin-top:var(--gap)}
.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:.4em 1em;padding:var(--pad) 0 0;color:var(--muted);font-size:.92em}
.toolbar .tl{display:inline-flex;align-items:center;gap:.4em}
.toolbar b{color:var(--fg-strong);font-weight:600}
.toolbar .dot{color:var(--line-2)}
.legacy{font:14px/1.25 var(--mono);font-size:.93em;min-width:0}
""".replace("@@ROWS@@", ROW_CLASSES)

TABLES = """
table{border-collapse:collapse;width:100%}
.tbl-w{overflow-x:auto;max-width:100%}
.mx th,.pt th,.mt th{font:600 .7em var(--sans);letter-spacing:.09em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.3em .5em;border-bottom:1px solid var(--line);white-space:nowrap}
.mx td,.pt td,.mt td{padding:.32em .5em;border-bottom:1px solid color-mix(in srgb,var(--line) 55%,transparent);vertical-align:baseline}
.tag{font:600 .74em var(--mono);padding:.08em .5em;border-radius:3px;border:1px solid var(--line-2);color:var(--muted);white-space:nowrap}
.tag.err{background:var(--err-bg);color:var(--err);border-color:color-mix(in srgb,var(--err) 50%,transparent)}
.tag.warn{background:var(--warn-bg);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 50%,transparent)}
.tag.ok{background:var(--ok-bg);color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,transparent)}
"""

FOOTER = """
.foot{position:sticky;bottom:0;z-index:20;display:flex;flex-wrap:wrap;align-items:center;gap:.3em .9em;padding:.5em var(--pad);background:var(--surface);border-top:1px solid var(--line);font-size:.86em;color:var(--muted)}
.foot .grp{display:inline-flex;align-items:baseline;gap:.35em;white-space:nowrap}
.foot .grp b{font-family:var(--mono);color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums;min-width:2.6em;text-align:center}
.foot .dot{color:var(--line-2)}
.foot a,.toolbar a{color:var(--accent);text-underline-offset:.15em}
.foot .lnk[aria-current="true"],.foot .lnk[aria-pressed="true"]{color:var(--fg-strong)}
.foot .upd{margin-left:auto;font-family:var(--mono);font-variant-numeric:tabular-nums}
html{scroll-padding-bottom:3em}
.view,main.grid{padding-bottom:calc(var(--pad) + 2.6em)}
@container app (max-width:45.7em){.foot{position:static}.foot .upd{margin-left:0}.view,main.grid{padding-bottom:var(--pad)}html{scroll-padding-bottom:0}}
"""

HELP = """
.ov{display:none;position:fixed;inset:0;z-index:40;background:var(--scrim);align-items:center;justify-content:center;padding:1em}
.ov:target{display:flex}
.ov .ov-bg{position:absolute;inset:0;cursor:default}
.dlg{position:relative;width:min(54em,100%);max-height:100%;overflow-y:auto;background:var(--surface);border:1px solid var(--line-2);border-radius:10px;box-shadow:0 20px 50px var(--shadow)}
.dlg>header{display:flex;align-items:center;justify-content:space-between;gap:1em;padding:.7em var(--pad);border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--surface);z-index:1}
.dlg h2{margin:0;font:650 .8em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-strong)}
.keys{display:grid;grid-template-columns:repeat(auto-fit,minmax(17em,1fr));gap:1.1em 1.6em;padding:var(--pad)}
.keys h3{margin:0 0 .4em;font:650 .7em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);padding-bottom:.3em;border-bottom:1px solid var(--line)}
.keys dl{margin:0;display:grid;grid-template-columns:8em minmax(0,1fr);gap:.35em .9em;align-items:baseline}
.keys dt{display:flex;flex-wrap:wrap;gap:.2em;align-items:baseline}
.keys dd{margin:0;color:var(--fg);min-width:0}
"""

SETTINGS = """
.settings{max-width:56em;margin:0 auto;padding:var(--pad);display:grid;gap:var(--gap);min-width:0}
.sec{display:grid;gap:1.1em;min-width:0;padding:var(--pad);background:var(--surface);border:1px solid var(--line);border-radius:8px}
.sech{margin:0;padding-bottom:.35em;border-bottom:1px solid var(--line-2);font:650 .78em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-strong)}
.fs{display:grid;gap:.4em;min-width:0}
.fs .lab,.lab{font:600 .7em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.fs .lab .src{text-transform:none;letter-spacing:0;font-weight:400;margin-left:.6em}
.hintl{margin:0;font-size:.86em;color:var(--muted)}
.kchk{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.3em .8em;margin:0;padding:0;list-style:none}
.kchk li{display:flex;align-items:center;gap:.5em;padding:.25em .4em;border-radius:5px;min-width:0}
.kchk li:hover{background:var(--surface-2)}
.kchk li>a,.kchk li>span:first-child{flex:1;color:var(--fg);text-decoration:none}
.kchk li>a:hover{color:var(--accent)}
.kchk .box{font-family:var(--mono);color:var(--muted);min-width:1.4em;text-align:center}
.kchk .on .box{color:var(--accent);font-weight:700}
.kchk .o{margin-left:auto;font:600 .72em var(--mono);color:var(--accent);min-width:1.4em;text-align:right}
.expo pre{margin:0;padding:.7em .8em;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;font:.84em/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;color:var(--fg)}
.expo code.ck-v{font:.8em var(--mono);color:var(--muted);overflow-wrap:anywhere;display:block}
.about{display:grid;grid-template-columns:6.8em minmax(0,1fr);gap:.65em .8em;margin:0;font-size:.92em}
.about dt{color:var(--muted)}
.about dd{margin:0;min-width:0;overflow-wrap:anywhere;line-height:1.6}
@container app (max-width:45.7em){.kchk{grid-template-columns:minmax(0,1fr)}.about{grid-template-columns:minmax(0,1fr);gap:.1em}.about dd{margin-bottom:.6em}}
"""

# the layout editor (?edit=1, src/webjs.py BUILDER_JS): the bar above the grid, and the buttons of each card. The handles show only on a card that
# BUILDER_JS has made focusable (tabindex), so that without the script there is no handle that does nothing; the cards' bodies are previews.
EDITOR = """
.ebar{display:grid;gap:.5em;margin:0 var(--pad) var(--gap);padding:var(--pad);background:var(--surface);border:1px solid var(--accent);border-radius:8px}
.ebar .eh{display:flex;flex-wrap:wrap;align-items:baseline;gap:.3em 1.4em}
.ebar h2{margin:0 auto 0 0;font:650 .9em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-strong)}
.ebar p{margin:0}
.ectl{display:flex;align-items:center;flex-wrap:wrap;gap:.35em;padding:.45em var(--pad);border-bottom:1px solid var(--line);background:var(--surface-2)}
.eb,.grip{display:inline-grid;place-items:center;min-width:2em;height:2em;padding:0 .5em;border:1px solid var(--line-2);border-radius:6px;background:var(--surface);color:var(--fg-strong);text-decoration:none;font-weight:700;line-height:1}
.eb:hover,.grip:hover{border-color:var(--accent);color:var(--accent)}
.grip{display:none;cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none}
.grip.rz{margin-left:auto;cursor:ew-resize}
.card[tabindex] .grip{display:inline-grid}
.wv{min-width:2.4em;text-align:center;font:600 .8em var(--mono);color:var(--muted)}
.card.s1 .wv::before{content:"1/4"}.card.s2 .wv::before{content:"2/4"}.card.s3 .wv::before{content:"3/4"}.card.s4 .wv::before{content:"4/4"}
.eb-hide .t-show{display:none}
.card.off .eb-hide .t-hide{display:none}.card.off .eb-hide .t-show{display:inline}
.card.off{border-style:dashed;background:transparent}
.card.off .ch h3,.card.off .ch .note{opacity:.6}
.card.off .ch h3::after{content:" \\00b7 hidden";text-transform:none;letter-spacing:0;font-weight:400}
.card.off .cb,.card.off .ectl>:not(.eb-hide){display:none}
.card.off .ectl{border-bottom:0;background:transparent}
.grid[data-edit] .cb{max-height:9em;overflow:hidden;position:relative}
.grid[data-edit] .cb::after{content:"";position:absolute;left:0;right:0;bottom:0;height:2.5em;background:linear-gradient(transparent,var(--surface))}
.card.drag{opacity:.75;outline:2px dashed var(--accent);outline-offset:2px}
.card[data-grab]{outline:3px solid var(--accent);outline-offset:2px}
.grid[data-edit] .card:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
@media (pointer:coarse){.eb,.grip{min-width:2.75em;height:2.75em}}
"""

# the components htmlview.html draws (ui.Span/Line/Table/KV/Msg/Wrap/More/Group/Pill/Tree/Details/Bar/Spark): every class it emits is here.
# Tones are t-<token>; a state is st-<state> or lv-<level>. Columns carry p1..p3 (ui.Col.prio): a bigger number is dropped sooner, by the
# width of the card (container queries on the card, not on the page). The classes r / b / n of these elements share their names with the ANSI
# colours (.r red, .b blue): the ANSI text lives in a <pre>, and the rules for it are repeated there with a higher weight.
COMPONENTS = """
.cb p{margin:0}
.cb .ln{overflow-wrap:anywhere}
.t-ok{color:var(--ok)}.t-warn{color:var(--warn)}.t-err{color:var(--err)}.t-unknown{color:var(--warn)}.t-info,.t-muted,.t-neutral{color:var(--muted)}
.t-accent{color:var(--accent)}.t-accent-strong{color:var(--accent);font-weight:700}.t-err-strong{color:var(--err);font-weight:700}.t-strong{color:var(--fg-strong);font-weight:700}
.t-banner-ok{background:var(--ok-bg);color:var(--ok);font-weight:700;padding:0 .4em;border-radius:3px}
.t-banner-err{background:var(--err-solid);color:var(--on-solid);font-weight:700;padding:0 .4em;border-radius:3px}
.t-banner-warn{background:var(--warn-bg);color:var(--warn);font-weight:700;padding:0 .4em;border-radius:3px}
.t-sel{background:var(--accent-bg);color:var(--fg-strong)}
.cb span.b{color:inherit;font-weight:700}.cb pre span.b{color:var(--accent);font-weight:400}
.cb span.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.cb span.n{font-family:var(--mono);font-variant-numeric:tabular-nums;color:inherit}
.st-unknown{color:var(--warn)}span.st-unknown{text-decoration:underline dashed;text-underline-offset:.2em;text-decoration-thickness:1px}
.msg{display:grid;grid-template-columns:1.4em minmax(0,1fr);gap:.4em;align-items:baseline;overflow-wrap:anywhere}
.msg .sym{align-self:start}
.msg.lv-ok .sym{color:var(--ok)}.msg.lv-warn .sym{color:var(--warn)}.msg.lv-err .sym{color:var(--err)}.msg.lv-info{color:var(--muted)}
.msg.lv-warn,.msg.lv-err{color:var(--fg-strong)}
.msg.notice{padding:.45em .7em;border:1px solid var(--line-2);border-radius:6px;background:var(--surface-2)}
.msg.notice.lv-warn{border-color:color-mix(in srgb,var(--warn) 50%,transparent);background:var(--warn-bg)}
.msg.notice.lv-err{border-color:color-mix(in srgb,var(--err) 50%,transparent);background:var(--err-bg)}
dl.kv{display:grid;grid-template-columns:auto minmax(0,1fr);gap:.2em .8em;margin:0;font-size:.92em}
dl.kv dt{color:var(--muted)}dl.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
table.tbl{font-size:.9em}
table.tbl th{font:600 .72em var(--sans);letter-spacing:.09em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.3em .5em;border-bottom:1px solid var(--line);white-space:nowrap}
table.tbl td{padding:.32em .5em;border-bottom:1px solid color-mix(in srgb,var(--line) 55%,transparent);vertical-align:baseline}
table.tbl tbody tr:last-child td{border-bottom:0}
table.tbl th:first-child,table.tbl td:first-child{padding-left:0}
table.tbl th:last-child,table.tbl td:last-child{padding-right:0}
.cb td.r,.cb th.r{color:inherit;text-align:right}
.cb th.r{color:var(--muted)}
td.n,th.n{font-family:var(--mono);font-variant-numeric:tabular-nums}
td.n,td.r{white-space:nowrap}
table.tbl td.n:has(>svg.bar){white-space:normal}
table.tbl td.n>svg.bar+span.n{display:inline-block;white-space:nowrap}
table.tbl td svg{vertical-align:middle}
table.tbl tr.grp th{padding-top:.8em;font:600 .86em var(--sans);letter-spacing:0;text-transform:none;color:var(--fg-strong);border-bottom:1px solid var(--line-2)}
table.tbl tr.t-err td{background:var(--err-bg)}
table.tbl tr.t-warn td{background:var(--warn-bg)}
table.tbl td a{color:var(--accent)}
svg.bar{display:inline-block;width:min(100%,6.5em);height:.6em;vertical-align:middle;margin-right:.5em;border-radius:2px;overflow:hidden}
svg.bar .bg{fill:color-mix(in srgb,var(--fg) 13%,transparent)}
svg.bar .fg{fill:var(--bar)}
svg.bar.st-warn .fg{fill:var(--warn)}svg.bar.st-err .fg{fill:var(--err)}svg.bar.st-ok .fg{fill:var(--ok)}
svg.spark{display:inline-block;width:4.5em;height:1.1em;vertical-align:middle;overflow:visible}
svg.spark polyline{fill:none;stroke:var(--accent);stroke-width:1.5;vector-effect:non-scaling-stroke;stroke-linejoin:round}
ul.wrap,ul.tree{list-style:none;margin:0;padding:0}
ul.wrap{display:flex;flex-wrap:wrap;gap:.2em 1em}
ul.tree li{padding:.1em 0;overflow-wrap:anywhere}
ul.tree li[data-depth="1"]{padding-left:1.4em}ul.tree li[data-depth="2"]{padding-left:2.8em}ul.tree li[data-depth="3"]{padding-left:4.2em}
section.grp>h3{margin:0 0 .4em;font:650 .7em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);padding-bottom:.3em;border-bottom:1px solid var(--line)}
section.grp>*+*{margin-top:.5em}
.pill{display:inline-block;font:700 .78em/1.5 var(--mono);padding:.05em .7em;border-radius:99px;border:1px solid var(--line-2);color:var(--muted);white-space:nowrap}
.pill.st-ok{background:var(--ok-bg);color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,transparent)}
.pill.st-warn,.pill.st-unknown{background:var(--warn-bg);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.pill.st-err,.pill.st-down{background:var(--err-bg);color:var(--err);border-color:color-mix(in srgb,var(--err) 45%,transparent)}
details.more>summary,details.dt>summary{cursor:pointer;width:fit-content;border-radius:3px}
details.more>summary{color:var(--muted)}
details.more>summary:hover{color:var(--accent)}
details.more p{margin-top:.3em}
details.dt{border-top:1px dashed var(--line-2);padding-top:.5em}
details.dt>summary{color:var(--accent);font-size:.92em}
details.dt[open]>summary{margin-bottom:.5em}
details.dt>*+*{margin-top:.5em}
.cb .grp .ln+.ln{margin-top:.2em}
@container card (max-width:40em){th.p3,td.p3{display:none}}
@container card (max-width:30em){th.p2,td.p2{display:none}}
@container card (max-width:22em){th.p1,td.p1{display:none}}
html[data-density="wall"] details.dt{display:none}
"""

# the cards that are built of components (cards.py, web.shell_card): what the console draws as columns of text, drawn as a dashboard. The
# classes below are the ones of htmlview's own markup that COMPONENTS does not style: a problem (prob, pr, pid, why, fix, how, accept, known),
# the label / bar / value lines (bl, lb, bx), a note under a row (tr.sub), a sub-heading (h3.sub), an indent (ind), a flow, a core grid, the boot
# timeline (tl, tp, legend, sw) and the centred cells of a matrix. `c` is the name of an ANSI colour too: it is scoped to the cell.
CARDS = """
.cb td.c,.cb th.c{color:inherit;text-align:center;padding-inline:.35em}
.cb th.c{color:var(--muted)}
td.c{white-space:nowrap}
td.c span{font-size:1.2em;line-height:1}
table.tbl td{overflow-wrap:break-word}
table.tbl th.n{font-family:var(--sans)}
.cb>p.ln{font-size:.94em}
.cb>*+p{margin-top:.55em}
.cb>*+p.hint{margin-top:.2em}
.cb>p.ln>span.b:first-child:not(:last-child){display:inline-block;margin-right:.6em}
.cb>p.ln>span.b:first-child:not(:last-child)+span{font-size:.72em}
.cb>p.ln>span.b:first-child{display:block;margin-top:.5em;font:650 .72em/1.4 var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
table.tbl tbody tr:not(.grp):not(.sub):hover>td{background:color-mix(in srgb,var(--fg) 4%,transparent)}
table.tbl tr.grp th .n{color:var(--muted);font-weight:400}
table.tbl tbody tr.sub>td{padding:0 0 .5em 1.7em;color:var(--muted);font-size:.9em;white-space:normal}
table.tbl tbody tr:has(+tr.sub)>td{border-bottom:0}
p.flow{display:flex;flex-wrap:wrap;gap:.05em .9em;margin:0}
p.flow .lead{color:var(--muted)}
p.flow .fi{min-width:0;overflow-wrap:anywhere}
h3.sub{margin:.9em 0 0;padding-bottom:.3em;border-bottom:1px solid var(--line);font:650 .7em/1.4 var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
h3.sub .note{font-weight:400;letter-spacing:0;text-transform:none;margin-left:.5em}
.cb>h3.sub{margin-top:1.1em}
.ind{min-width:0;padding-left:.7em;border-left:2px solid var(--line)}
.ind>*+*{margin-top:.5em}
.hint{color:var(--muted);font-size:.86em}
.hint code.cmd{font-size:.95em}
.cb .prob+.prob,.cb .prob+.known,.cb .known+.known{margin-top:0}
.msg.prob,.msg.known{padding:.5em 0;border-top:1px solid var(--line);row-gap:.15em}
.cb>.msg.prob:first-child,.cb>.msg.known:first-child,.cb>.dt>.msg.known:first-of-type{border-top:0;padding-top:0}
.msg.prob>:not(.sym),.msg.known>:not(.sym){grid-column:2;min-width:0}
.prob .pr{font-weight:560}
code.pid{font:400 .76em/1.5 var(--mono);padding:.04em .55em;margin-left:.4em;border:1px solid var(--line-2);border-radius:99px;background:var(--surface-2);color:var(--muted);white-space:nowrap;vertical-align:.08em}
.why{color:var(--muted);font-size:.9em;line-height:1.35}
.msg.prob.lv-err .why,.msg.prob.lv-warn .why{color:var(--muted)}
details.fix>summary{cursor:pointer;width:fit-content;color:var(--accent);font-size:.84em;font-weight:600;border-radius:3px}
details.fix[open]>summary{margin-bottom:.3em}
.how,.accept{color:var(--muted)}
details.fix>.d{display:block;margin-top:.25em;font-size:.88em;overflow-wrap:anywhere}
details.fix code.cmd{display:inline-block;max-width:100%;vertical-align:top;user-select:all}
.known .why code.cmd{font-size:.95em}
ul.wrap{gap:.3em .4em}
ul.wrap li{min-width:0;padding:.02em .55em;border:1px solid var(--line);border-radius:5px;background:var(--surface-2);overflow-wrap:anywhere}
ul.wrap.flat{gap:.2em 1.4em;font-variant-numeric:tabular-nums}
ul.wrap.flat li{padding:0;border:0;border-radius:0;background:none}
ul.wrap.flat+ul.wrap.flat{margin-top:.3em;color:var(--muted);font-size:.88em}
section.grp>p.ln{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 .6em}
.bl{display:grid;grid-template-columns:3.4em minmax(3em,1fr) 7.6em;align-items:center;gap:.05em .6em;min-width:0}
.bl+.bl{margin-top:.5em}
.bl .lb{color:var(--muted);font-size:.9em;font-weight:600;white-space:nowrap}
.bl svg.bar{display:block;width:100%;height:.6em;margin:0}
.bl .n{text-align:right;white-space:nowrap;line-height:1.2}
.bl .bx{grid-column:2 / -1;color:var(--muted);font-size:.86em;overflow-wrap:anywhere}
ul.grid{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(4.8em,1fr));gap:.15em .8em}
ul.grid li{min-width:0}
ul.grid li.bl{grid-template-columns:1.4em minmax(.8em,1fr) 2.2em;gap:0 .3em;font-size:.82em}
ul.grid li.bl+li.bl{margin-top:0}
ul.grid li.bl .lb{font:600 .86em var(--mono);text-align:right;font-variant-numeric:tabular-nums}
ul.grid li.bl svg.bar{height:.5em}
figure.timeline{margin:0;min-width:0}
svg.tl{display:block;width:100%;height:.9em;border-radius:3px;overflow:hidden;background:var(--surface-2)}
rect.tp{fill:var(--bar)}
rect.tp-firmware,ul.legend .tp-firmware .sw{fill:var(--magenta);background:var(--magenta)}
rect.tp-loader,ul.legend .tp-loader .sw{fill:var(--cyan);background:var(--cyan)}
rect.tp-kernel,ul.legend .tp-kernel .sw{fill:var(--accent);background:var(--accent)}
rect.tp-initrd,ul.legend .tp-initrd .sw{fill:color-mix(in srgb,var(--accent) 45%,var(--surface));background:color-mix(in srgb,var(--accent) 45%,var(--surface))}
rect.tp-main-path,ul.legend .tp-main-path .sw{fill:var(--accent);background:var(--accent)}
rect.tp-post-boot,ul.legend .tp-post-boot .sw{fill:var(--bar);background:var(--bar)}
rect.tp-userspace,ul.legend .tp-userspace .sw{fill:var(--bar);background:var(--bar)}
rect.tp{stroke:var(--surface);stroke-width:.4}
ul.legend{list-style:none;margin:.45em 0 0;padding:0;display:flex;flex-wrap:wrap;gap:.15em 1em;color:var(--muted);font-size:.86em}
ul.legend li{white-space:nowrap}
ul.legend .sw{display:inline-block;width:.7em;height:.7em;margin-right:.2em;border-radius:2px;vertical-align:-.05em;background:var(--bar)}
ul.legend .n{color:var(--fg)}
html[data-density="compact"] .prob .why,html[data-density="compact"] .prob .fix,html[data-density="compact"] .hint,
html[data-density="wall"] .prob .why,html[data-density="wall"] .prob .fix,html[data-density="wall"] .hint,html[data-density="wall"] code.pid{display:none}
html[data-density="wall"] ul.grid{display:none}
/* a wall has no mouse: what can only be clicked is not drawn (the "+N more" keeps its count, as plain text), and a link in a card is text */
html[data-density="wall"] .card p.more,html[data-density="wall"] details.more p,html[data-density="wall"] details.fix{display:none}
html[data-density="wall"] details.more>summary{pointer-events:none;list-style:none}
html[data-density="wall"] details.more>summary::-webkit-details-marker{display:none}
html[data-density="wall"] .cb a{pointer-events:none;color:inherit;text-decoration:none}
"""

# the full screens built of components (screens.py): a page of panels (ui.Group), key figures (ui.Tiles), meters, a list whose rows are links
SCREENS = """
.scr{display:grid;gap:var(--gap);min-width:0}
.scr section.grp{container:panel / inline-size;min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:var(--pad)}
.scr section.grp>*+*{margin-top:.6em}
.scr p{margin:0}
.scr table.tbl td{white-space:nowrap}
.scr h3 .note{margin-left:.8em;font-weight:400;letter-spacing:0;text-transform:none;color:var(--muted)}
.scr .tiles{padding:0;grid-template-columns:repeat(auto-fit,minmax(8em,1fr))}
.scr .tiles .kpi{display:grid;grid-template-columns:auto minmax(0,1fr);grid-template-areas:"s l" "n u";align-items:baseline;column-gap:.4em;padding:.5em .7em .55em}
.scr .tiles .kpi .sym{grid-area:s;font-size:.7em;min-width:0}
.scr .tiles .kpi .lbl{grid-area:l;font:600 .68em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.scr .tiles .kpi .n{grid-area:n;font:700 1.5em/1.15 var(--mono);font-variant-numeric:tabular-nums;color:var(--fg-strong)}
.scr .tiles .kpi .unit{grid-area:u;font-size:.8em;font-weight:600;color:var(--muted)}
.scr .tiles .kpi.st-unknown .n{color:var(--warn)}
.scr ul.wrap li.lead{color:var(--muted)}
.scr ul.grid{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(19em,1fr));gap:.2em 1.6em;font:.88em var(--mono);font-variant-numeric:tabular-nums;white-space:pre}
.scr ul.grid li{min-width:0;overflow:hidden}
svg.meter{display:inline-block;width:8em;height:.6em;vertical-align:middle;border-radius:2px;overflow:hidden}
svg.meter .bg{fill:color-mix(in srgb,var(--fg) 13%,transparent)}
svg.meter .m-user{fill:var(--ok)}svg.meter .m-system{fill:var(--err)}svg.meter .m-other{fill:var(--accent)}svg.meter .m-busy{fill:var(--cyan)}svg.meter .m-iowait{fill:var(--faint)}
.scr p.ln svg.meter{width:min(100%,22em)}
.scr p.ln{font-variant-numeric:tabular-nums}
.scr table.tbl{font-size:.9em}
.scr table.tbl th a{color:inherit;text-decoration:none}
.scr table.tbl th a:hover{color:var(--accent)}
.scr table.tbl th.sorted,.scr table.tbl th.sorted a{color:var(--warn)}
.scr table.tbl th[aria-sort="descending"] a::after{content:" ▼"}
.scr table.tbl th[aria-sort="ascending"] a::after{content:" ▲"}
.scr table.tbl th.r,.scr table.tbl td.r{text-align:right}
.scr table.tbl tr{position:relative}
.scr table.tbl tr[data-row]{cursor:pointer}
.scr table.tbl tr[data-row] td:first-child a{color:inherit;text-decoration:none}
.scr table.tbl tr[data-row] td:first-child a::after{content:"";position:absolute;inset:0}
.scr table.tbl tr[data-row]:hover td,.scr table.tbl tr[data-row]:focus-within td{background:var(--surface-2)}
.scr table.tbl tr.t-sel td{background:var(--accent-bg);color:var(--fg-strong)}
.scr table.tbl td:last-child{width:100%;white-space:normal;overflow-wrap:anywhere}
.scr section.grp>p.ln+p.ln{margin-top:.2em}
.scr .split{display:grid;grid-template-columns:minmax(0,1fr);gap:var(--gap)}
.scr .split>div{min-width:0;display:grid;gap:var(--gap);align-content:start}
@container app (min-width:60em){.scr .split{grid-template-columns:minmax(0,1.7fr) minmax(18em,1fr)}}
@container panel (max-width:44em){.scr th.p3,.scr td.p3{display:none}}
@container panel (max-width:34em){.scr th.p2,.scr td.p2{display:none}}
@container panel (max-width:24em){.scr th.p1,.scr td.p1{display:none}}
"""

# the Health screen drawn from components (screens.health_model; htmlview: Title, Seg, Finding, Pane, Advice, Cols, Series): everything under .hv
HEALTH_VIEW = """
.hv{display:grid;gap:var(--gap);min-width:0}
.hv>*{min-width:0}
.hv>.ln,.hv>.msg{margin:0}
.hv .st{display:flex;flex-wrap:wrap;align-items:center;gap:.4em 1.2em;padding-top:var(--pad)}
.hv .st h2{margin:0;font:650 .8em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-strong)}
.hv .bits{display:flex;flex-wrap:wrap;align-items:baseline;gap:.2em 1.1em;margin:0;min-width:0;flex:1 1 18em;color:var(--muted)}
.hv .bit{white-space:normal;overflow-wrap:anywhere}
.hv section.grp{min-width:0;overflow-x:auto}
.hv .st .seg{margin-left:auto}
.hv>section.grp,.hv .col{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:var(--pad);min-width:0;overflow-x:auto}
.hv section.grp>*+*{margin-top:0}
.hv h3.sub{margin:1.2em 0 .5em;padding-bottom:.3em;border-bottom:1px solid var(--line);font:650 .72em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-strong)}
.hv h3.sub:first-child{margin-top:0}
.hv h3.sub .note{margin-left:.8em;font-weight:400;letter-spacing:0;text-transform:none;color:var(--muted)}
.hv .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,30em),1fr));gap:var(--gap);align-items:start}
.hv .col>section.grp>.ln,.hv .col>section.grp>.msg{margin:.2em 0}
.hv .fd{border-top:1px solid color-mix(in srgb,var(--line) 55%,transparent)}
.hv h3.sub+.fd,.hv h3.sub+.msg+.fd{border-top:0}
.fd details>summary{display:flex;flex-wrap:wrap;align-items:baseline;gap:.15em .65em;padding:.5em .3em;border-radius:4px;cursor:pointer;list-style:none}
.fd details>summary::-webkit-details-marker{display:none}
.fd details>summary::before{content:"\\25B8";color:var(--muted);width:.9em;flex:none;text-align:center}
.fd details[open]>summary::before{content:"\\25BE"}
.fd details>summary:hover{background:var(--surface-2)}
.fd .ft{color:var(--fg-strong);font-weight:600}
.fd .fx{flex:1 1 16em;min-width:0;color:var(--muted)}
.fd details[open]{background:var(--accent-bg);border-radius:6px}
.fd .tag{min-width:5.2em;text-align:center}
.fd .tag.info{background:var(--surface-2)}
dl.pane{display:grid;grid-template-columns:4.5em minmax(0,1fr);gap:.3em .9em;margin:.1em .6em .8em 2.2em;font-size:.92em}
dl.pane dt{color:var(--muted)}
dl.pane dd{margin:0;min-width:0;overflow-wrap:anywhere}
ul.facts{display:flex;flex-wrap:wrap;gap:.2em 1.4em;margin:0;padding:0;list-style:none}
ul.facts .k{color:var(--muted)}
.hv .advice{padding:.75em 1em;background:var(--surface-2);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:6px}
.hv .advice p{margin:.35em 0;overflow-wrap:anywhere}
.hv .advice-head{margin:0 0 .5em;color:var(--accent);font:600 .86em var(--mono)}
.hv .advice-error{border-left-color:var(--warn)}
.hv .advice-error .advice-head{color:var(--warn)}
.hv .advice-none,.hv .advice-shared{border-left-style:dashed}
.hv .advice-cites,.hv .advice-tools{color:var(--muted);font-size:.86em}
.hv table.tbl{table-layout:auto}
.hv td.n,.hv td.r{text-align:right}
.hv .col{container:panel / inline-size}
.hv table.tbl td:last-child{white-space:normal;overflow-wrap:anywhere}
@container panel (max-width:44em){.hv th.p3,.hv td.p3{display:none}}
@container panel (max-width:34em){.hv th.p2,.hv td.p2{display:none}}
@container panel (max-width:24em){.hv th.p1,.hv td.p1{display:none}}
svg.bar.t-accent .fg{fill:var(--accent)}
svg.bar.busy .fg{fill:var(--accent);animation:barbusy 1.4s ease-in-out infinite alternate}
@keyframes barbusy{from{transform:translateX(0)}to{transform:translateX(65px)}}
@media (prefers-reduced-motion:reduce){svg.bar.busy .fg{animation:none}}
svg.series{display:inline-block;width:min(100%,9em);height:1.1em;vertical-align:middle;color:var(--accent)}
svg.series rect{fill:currentColor}
.hv .bit .t-banner-err{font-size:.9em}
@container app (max-width:45.7em){.hv .st .seg{margin-left:0}dl.pane{margin-left:.4em;grid-template-columns:minmax(0,1fr)}dl.pane dd{margin-bottom:.3em}}
"""

# the Map screen drawn from components (screens.map_web; htmlview: Title, Legend, Seg, Outline, Props): everything under .mapv (and .scr, for the panels)
MAP_VIEW = """
.mapv>*{min-width:0}
.mapv .st{display:flex;flex-wrap:wrap;align-items:center;gap:.4em 1.2em;padding-top:var(--pad)}
.mapv .st h2{margin:0;font:650 .8em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-strong)}
.mapv .bits{display:flex;flex-wrap:wrap;align-items:baseline;gap:.2em 1.1em;margin:0;min-width:0;flex:1 1 18em;color:var(--muted)}
.mapv .bit{white-space:nowrap}
.mapv ul.lgd{order:9;flex:1 1 100%;margin:0;font:.84em var(--mono)}
.mapv ul.lgd li{padding:0}
.mapv .st .seg:first-of-type{margin-left:auto}
.mapv .split>div>section.grp{overflow-x:auto}
.mapv .split{align-items:start}
.mapv li.ob-d{display:none}
@container app (min-width:60em){.mapv .split{grid-template-columns:minmax(0,1.5fr) minmax(20em,1fr)}
.mapv .sp-r{position:sticky;top:var(--stick,3.2em);max-height:calc(100vh - var(--stick,3.2em) - 3em);overflow-y:auto}}
@container app (max-width:59.99em){.mapv .split:has(li.ob-d) .sp-r{display:none}
.mapv li.ob-d{display:block;margin:.2em 0 .5em 1.6em;padding:.5em .7em;background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:6px}
.mapv li.ob-d .props{font-size:.9em}}
ul.ol{list-style:none;margin:0;padding:0;font:.92em/1.45 var(--mono)}
.ob{display:flex;align-items:baseline;gap:.1em;padding:.1em .3em;border-radius:4px}
DEPTHS
.ob .tg{flex:none;width:1.6em;text-align:center;color:var(--accent);text-decoration:none}
.ob .tg.d{color:var(--muted)}
a.tg:hover{color:var(--fg-strong)}
.ob .oa{flex:1 1 auto;min-width:0;color:inherit;text-decoration:none;overflow-wrap:anywhere}
.ob:hover,.ob:focus-within{background:var(--surface-2)}
.ob.sel{background:var(--accent-bg)}
.ob.sel .oa{color:var(--fg-strong)}
.ob .sym{min-width:1.1em}
.ob.st-ok .sym{color:var(--ok)}.ob.st-warn .sym,.ob.st-unknown .sym{color:var(--warn)}.ob.st-err .sym,.ob.st-down .sym{color:var(--err)}.ob.st-info .sym{color:var(--muted)}
.props{font-size:.92em;min-width:0}
.props .ph{display:flex;align-items:baseline;justify-content:space-between;gap:1em;border-bottom:1px solid var(--line);margin-bottom:.5em}
.props .ph h3.sub{margin:0;border:0;padding-bottom:.3em}
.props dl{display:grid;grid-template-columns:minmax(6em,max-content) minmax(0,1fr);gap:.25em 1em;margin:0}
.props dt{color:var(--muted);font-family:var(--mono)}
.props dt.in{padding-left:1.2em}
.props dd{margin:0;min-width:0;overflow-wrap:anywhere;font-family:var(--mono)}
.props dd.top{font-weight:700;color:var(--fg-strong)}
.props dd.lv-err{color:var(--err)}.props dd.lv-warn{color:var(--warn)}.props dd.lv-ok{color:var(--ok)}.props dd.lv-info{color:var(--muted)}
@container app (max-width:45.7em){.props dl{grid-template-columns:minmax(0,1fr);gap:0}.props dd{margin-bottom:.4em}}
"""
MAP_VIEW = MAP_VIEW.replace("DEPTHS", "".join('.ob[data-depth="%d"]{padding-left:%.1fem}' % (d, 0.3 + 1.3 * d) for d in range(1, 13)))

# the AI screen drawn from components (screens.ai_model; htmlview: Badge, Action, Controls, Question, Spec, Qa, Log, Title, Cols, Split, Table): everything under
# .av, which is also .scr (the panels, the table rows, the split). The buttons are forms that post to /ai/*: their look is here, their rules are the server's.
AI_VIEW = """
.av{display:grid;gap:var(--gap);min-width:0}
.av>*{min-width:0}
.av .st{display:flex;flex-wrap:wrap;align-items:center;gap:.4em 1.2em;padding-top:var(--pad)}
.av .st h2{margin:0;font:650 .8em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-strong)}
.av .bits{display:flex;flex-wrap:wrap;align-items:baseline;gap:.2em 1.1em;margin:0;min-width:0;flex:1 1 18em;color:var(--muted)}
.av .bit{white-space:nowrap}
.av h3.sub{margin:0 0 .6em;padding-bottom:.3em;border-bottom:1px solid var(--line);font:650 .72em var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--fg-strong)}
.av h3.sub .note{margin-left:.8em;font-weight:400;letter-spacing:0;text-transform:none;color:var(--muted)}
.av .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,26em),1fr));gap:var(--gap);align-items:start}
.av .col>section.grp{height:100%}
.av .controls{display:flex;flex-wrap:wrap;align-items:center;gap:.5em 1em}
.av .controls .ci{min-width:0;overflow-wrap:anywhere}
.av .controls .tag{font-size:.9em;padding:.14em .9em}
.av .tag.accent{background:var(--accent-bg);color:var(--accent);border-color:color-mix(in srgb,var(--accent) 45%,transparent)}
.av form.f{display:inline-flex;flex-wrap:wrap;align-items:center;gap:.5em;margin:0}
.av .bt{font:inherit;cursor:pointer;padding:.25em .9em;border:1px solid var(--line-2);border-radius:6px;background:var(--surface-2);color:var(--fg-strong)}
.av .bt:hover{border-color:var(--accent);color:var(--accent)}
.av .bt.bt-ok{background:var(--ok-bg);border-color:color-mix(in srgb,var(--ok) 45%,transparent);color:var(--ok);font-weight:600}
.av .bt.bt-ok:hover{border-color:var(--ok);color:var(--ok)}
.av .bt.bt-err{background:var(--err-bg);border-color:color-mix(in srgb,var(--err) 45%,transparent);color:var(--err)}
.av .bt.bt-err:hover{background:var(--err-solid);border-color:var(--err-solid);color:var(--on-solid)}
.av .bt:disabled,.av .bt:disabled:hover{opacity:.55;cursor:not-allowed;border-color:var(--line);background:var(--surface-2);color:var(--muted)}
.av table.tbl .bt{padding:.05em .7em;font-size:.92em;position:relative;z-index:1}
.av input.q{font:inherit;width:min(42em,100%);padding:.3em .7em;border:1px solid var(--line-2);border-radius:6px;background:var(--surface-2);color:var(--fg-strong)}
.av input.q:disabled{opacity:.55}
.av .log{display:flex;flex-direction:column-reverse;max-height:min(30em,55vh);overflow-y:auto;overscroll-behavior:contain;margin:0 0 .7em;padding:0 .7em;border:1px solid var(--line);border-radius:6px;background:var(--bg)}
.av .log-in{padding:.2em 0}
.av .qa{margin:.5em 0}
.av .qa .q{margin:0 0 .35em}
.av .pend{color:var(--muted)}
.av .advice{padding:.75em 1em;background:var(--surface-2);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:6px}
.av .advice p{margin:.35em 0;overflow-wrap:anywhere}
.av .advice-head{margin:0 0 .5em;color:var(--accent);font:600 .86em var(--mono)}
.av .advice-error{border-left-color:var(--warn)}
.av .advice-error .advice-head{color:var(--warn)}
.av .advice-shared{border-left-style:dashed}
.av .advice-cites,.av .advice-tools{color:var(--muted);font-size:.86em}
.av .ask{display:flex;flex-wrap:wrap;align-items:center;gap:.5em 1em;padding:.6em .8em;border:1px solid var(--warn);border-left-width:3px;border-radius:6px;background:var(--warn-bg)}
.av .ask p{flex:1 1 20em;min-width:0;margin:0;color:var(--fg-strong);overflow-wrap:anywhere}
.av .ask-b{display:flex;align-items:center;gap:.5em}
.av .spec-t{margin:0 0 .5em;font-weight:700;color:var(--fg-strong)}
dl.spec-dl{display:grid;grid-template-columns:5.5em minmax(0,1fr);gap:.3em .9em;margin:0;font-size:.92em}
dl.spec-dl dt{color:var(--muted)}
dl.spec-dl dd{margin:0;min-width:0;overflow-wrap:anywhere}
dl.spec-dl dd.do{display:flex;flex-wrap:wrap;gap:.4em}
.av .spec+.seg{margin-top:.6em}
.av table.tbl{table-layout:auto}
.av table.tbl td,.av table.tbl td:last-child{width:auto;white-space:nowrap}
.av table.tbl td.p3:not(.n){white-space:normal;min-width:14em}
.av table.tbl td:first-child{white-space:normal;min-width:9em}
.av .bt{white-space:nowrap}
.av section.grp{overflow-x:auto}
@container app (max-width:45.7em){.av .st .seg{margin-left:0}dl.spec-dl{grid-template-columns:minmax(0,1fr)}dl.spec-dl dd{margin-bottom:.3em}}
"""

# the ANSI text of the cards that are not built of components yet (htmlview.to_html: <span class="g B">): the colours of the theme
ANSI = """
.r{color:var(--err)}.g{color:var(--ok)}.y{color:var(--warn)}.b{color:var(--accent)}.m{color:var(--magenta)}.c{color:var(--cyan)}
.w{color:var(--fg-strong)}.d{color:var(--muted)}.k{color:var(--bg)}.B{font-weight:700}
.bR{background:var(--err-solid);color:var(--on-solid)}.bY{background:var(--warn);color:var(--bg)}.rv{background:var(--fg);color:var(--bg)}
.n{color:var(--fg)}
"""

# htmlview's own colours (the dark palette of the classic pages) -> the variables: the Map, CPU, Health and AI bodies are reused as they are
REMAP = {
    "#0d1117": "var(--bg)", "#c9d1d9": "var(--fg)", "#f0f6fc": "var(--fg-strong)", "#8b949e": "var(--muted)", "#6e7681": "var(--faint)",
    "#484f58": "var(--faint)", "#3d444d": "var(--line-2)", "#30363d": "var(--line-2)", "#21262d": "var(--surface-2)",
    "#161b22": "var(--surface-2)", "#1f2a3a": "var(--accent-bg)", "#1f6feb40": "var(--accent-bg)", "#58a6ff": "var(--accent)",
    "#3fb950": "var(--ok)", "#d29922": "var(--warn)", "#ff7b72": "var(--err)", "#da3633": "var(--err-solid)", "#39c5cf": "var(--cyan)",
    "#bc8cff": "var(--magenta)", "#238636": "var(--ok-solid)", "#2ea043": "var(--ok-solid)", "#8e1519": "var(--err-solid)",
    "#fff": "var(--on-solid)",
}
HEX = re.compile(r"(?<=[:\s,(])#[0-9a-fA-F]{3,8}\b")


def themed(css):
    """css with the colours of the classic palette replaced by the theme's variables; any other colour is left as it is."""
    return HEX.sub(lambda m: REMAP.get(m.group(0).lower(), m.group(0)), css)


LEGACY_SOURCES = (htmlview.MAP_CSS, htmlview.CPU_CSS, htmlview.HEALTH_CSS, htmlview.AI_CSS, htmlview.GRAPH_CSS)

# what the shell changes of those pages: no header of their own (the top bar says it), the pills and buttons of the new look, the AI page's
# switch, chat, confirmation and progress as panels
LEGACY_FIX = """
.legacy .hd{display:none}
.legacy .lg,.legacy .nt,.legacy .hs{font-family:var(--sans)}
.legacy a{color:var(--accent)}
.legacy a.pr,.legacy a.lb{color:inherit}
.legacy .ro:hover,.legacy a.pr:hover{background:var(--surface-2)}
.legacy .dp{background:var(--surface);border-radius:8px}
.legacy .mv{border-color:var(--line-2);background:var(--surface)}
.legacy .mv>b{background:var(--accent-bg);color:var(--accent)}
.pl{border:1px solid var(--line-2);border-radius:4px;font-family:var(--mono);font-size:.8em;line-height:1.5;color:var(--muted)}
.pl.r{background:var(--err-bg);color:var(--err);border-color:color-mix(in srgb,var(--err) 50%,transparent)}
.pl.y{background:var(--warn-bg);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 50%,transparent)}
.pl.g{background:var(--ok-bg);color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,transparent)}
.pl.c{background:var(--accent-bg);color:var(--accent);border-color:color-mix(in srgb,var(--accent) 45%,transparent)}
.pl.d{background:var(--surface-2);color:var(--muted);font-weight:700}
.pl.big{font-size:.95em;padding:.1em .9em}
.ctl,.chat{background:var(--surface);border-radius:8px;margin:var(--gap) 0}
.ctl .row{font-family:var(--sans)}
.ctl .note,.cf{border-radius:6px;border:1px solid var(--line-2);background:var(--surface-2);padding:.45em .7em}
.ctl .note.ok{border-color:color-mix(in srgb,var(--ok) 45%,transparent);background:var(--ok-bg);color:var(--fg)}
.ctl .note.bad{border-color:color-mix(in srgb,var(--warn) 50%,transparent);background:var(--warn-bg);color:var(--fg)}
.cf{display:flex;flex-wrap:wrap;align-items:center;gap:.4em .8em;border-color:var(--warn);background:var(--warn-bg)}
.cf>span{flex:1 1 18em;min-width:0}.cf .bt{margin-left:0}
.bt{background:var(--surface-2);border:1px solid var(--line-2);color:var(--fg-strong);border-radius:6px;padding:.2em .8em;font-family:var(--sans)}
.bt:hover{border-color:var(--accent);color:var(--accent);background:var(--surface-2)}
.bt.on,.bt.use{background:var(--accent-bg);border-color:var(--accent);color:var(--accent);font-weight:600}
.bt.off,.bt.del,.bt.stop{background:var(--err-bg);border-color:var(--err);color:var(--err)}
.bt.off:hover,.bt.del:hover,.bt.stop:hover{background:var(--err-solid);color:var(--on-solid)}
.bt:disabled,.bt:disabled:hover{opacity:.55;cursor:not-allowed;border-color:var(--line);color:var(--muted);background:var(--surface-2)}
progress{appearance:none;-webkit-appearance:none;width:min(26em,100%);height:.8em;border:1px solid var(--line-2);border-radius:99px;background:var(--surface-2);overflow:hidden;vertical-align:middle}
progress::-webkit-progress-bar{background:var(--surface-2)}
progress::-webkit-progress-value{background:var(--accent)}
progress::-moz-progress-bar{background:var(--accent)}
.legacy input.q{background:var(--surface-2);color:var(--fg-strong);border-color:var(--line-2);font-family:var(--sans)}
.legacy .advice{background:var(--surface-2);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:6px;font-family:var(--sans)}
.legacy .advice-head{color:var(--accent);font-family:var(--mono)}
.legacy .advice-error{border-left-color:var(--warn)}
.legacy .cmd{background:var(--surface-2);border:1px solid var(--line);border-radius:4px}
.gv{background:var(--surface)}
"""


def graph_zoom_css():
    """The graph's size per zoom step: a class of <html> (gzNNN; gzfit, the default, is the window's width) instead of an inline style."""
    return ".gzfit #gsvg{width:100%;max-height:calc(100vh - 150px)}" + "".join(".gz%d #gsvg{width:%d%%}" % (z, z) for z in GZOOMS if z != 100)


def build():
    """The whole sheet, once."""
    legacy = themed("".join(LEGACY_SOURCES))
    return "".join((tokens(), density(), BASE, SHIFT, CONTROLS, TOPBAR, KPIS, GRID, TABLES, FOOTER, HELP, SETTINGS, EDITOR, COMPONENTS, CARDS, SCREENS, HEALTH_VIEW, MAP_VIEW, AI_VIEW, graph_zoom_css(), ANSI, legacy, LEGACY_FIX))


CSS = build()
ASSET_TYPE = "text/css; charset=utf-8"
# name -> (the sheet's bytes, content type, SHA-256 in hex): served at /s/<name>.<sha8>.css, immutable (the name changes with the text)
ASSETS = {"app": (CSS.encode("utf-8"), ASSET_TYPE, hashlib.sha256(CSS.encode("utf-8")).hexdigest())}


def asset_path(name):
    """The URL an asset is served at: the first 8 hex digits of its SHA-256 are in the name, so a changed sheet is a new URL."""
    return "/s/%s.%s.css" % (name, ASSETS[name][2][:8])
