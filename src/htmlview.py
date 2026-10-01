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
