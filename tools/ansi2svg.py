#!/usr/bin/env python3
"""Turn the ANSI output of `render.py --once --demo --color` into a crisp SVG "terminal window" (stdlib only).

    python3 src/render.py --once --demo --color --cols 226 --rows 46 | python3 tools/ansi2svg.py --title "nuc-console" > docs/img/overview.svg

The character grid is respected exactly, whatever font the viewer has: bars (█ ░), sparklines (▁…█) and rules (─ │) are drawn
as rectangles, every other non-ASCII symbol (✔ ✖ ⚠ ● ·) is placed in its own cell, and ASCII text runs are anchored on the grid.
"""
import re
import sys
from xml.sax.saxutils import escape

FG = {30: "#484f58", 31: "#ff7b72", 32: "#3fb950", 33: "#d29922", 34: "#58a6ff", 35: "#bc8cff", 36: "#39c5cf", 37: "#c9d1d9",
      90: "#6e7681", 91: "#ffa198", 92: "#56d364", 93: "#e3b341", 94: "#79c0ff", 95: "#d2a8ff", 96: "#56d4dd", 97: "#f0f6fc"}
DEFAULT = "#c9d1d9"
CW, LH, PAD, BAR, FS = 8.4, 18.0, 18, 34, 14   # cell width, line height, padding, title-bar height, font size
FONT = "'DejaVu Sans Mono','Liberation Mono',Menlo,Consolas,ui-monospace,monospace"
SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]")
SPARK = "▁▂▃▄▅▆▇█"


def runs(line):
    """-> [(col, text, fg, bold)] for one line."""
    out, fg, bold, col, pos = [], DEFAULT, False, 0, 0
    line = OTHER.sub("", line)
    for m in SGR.finditer(line):
        txt = line[pos:m.start()]
        if txt:
            out.append((col, txt, fg, bold))
            col += len(txt)
        for code in (int(x) for x in (m.group(1) or "0").split(";") if x != ""):
            if code == 0:
                fg, bold = DEFAULT, False
            elif code == 1:
                bold = True
            elif code in FG:
                fg = FG[code]
        pos = m.end()
    if line[pos:]:
        out.append((col, line[pos:], fg, bold))
    return out


def cell_shapes(top, x, ch, fg):
    """SVG for one non-text cell, or None if the character is text."""
    y = top
    if ch == "█":
        return f'<rect x="{x:.1f}" y="{y + 3:.1f}" width="{CW + 0.4:.1f}" height="{LH - 5:.1f}" fill="{fg}"/>'
    if ch in "░▒▓":
        op = {"░": 0.22, "▒": 0.45, "▓": 0.7}[ch]
        return f'<rect x="{x:.1f}" y="{y + 3:.1f}" width="{CW + 0.4:.1f}" height="{LH - 5:.1f}" fill="{fg}" opacity="{op}"/>'
    if ch in SPARK[:-1]:
        hgt = (SPARK.index(ch) + 1) / 8 * (LH - 4)
        return f'<rect x="{x + 0.6:.1f}" y="{y + LH - 2 - hgt:.1f}" width="{CW - 1.2:.1f}" height="{hgt:.1f}" fill="{fg}"/>'
    if ch == "─":
        return f'<rect x="{x:.1f}" y="{y + LH / 2 + 1:.1f}" width="{CW + 0.4:.1f}" height="1.2" fill="{fg}"/>'
    if ch == "│":
        return f'<rect x="{x + CW / 2 - 0.6:.1f}" y="{y + 2:.1f}" width="1.2" height="{LH - 2:.1f}" fill="{fg}"/>'
    return None


def line_svg(i, line):
    top = BAR + PAD + i * LH
    base = top + LH * 0.75
    out = []
    for col, txt, fg, bold in runs(line):
        wt = ' font-weight="700"' if bold else ""
        buf, start = "", 0
        def flush(end_col):
            nonlocal buf, start
            s = buf.strip(" ")
            if s:
                lead = len(buf) - len(buf.lstrip(" "))
                out.append(f'<text x="{PAD + (start + lead) * CW:.1f}" y="{base:.1f}" fill="{fg}"{wt} textLength="{len(s) * CW:.1f}" '
                           f'lengthAdjust="spacing">{escape(s)}</text>')
            buf = ""
        for k, ch in enumerate(txt):
            c = col + k
            shape = cell_shapes(top, PAD + c * CW, ch, fg)
            if shape:
                flush(c)
                out.append(shape)
                start = c + 1
            elif ord(ch) > 127:          # symbol: its own cell, so a wider fallback glyph can't push the rest of the line
                flush(c)
                out.append(f'<text x="{PAD + c * CW:.1f}" y="{base:.1f}" fill="{fg}"{wt} text-anchor="start">{escape(ch)}</text>')
                start = c + 1
            else:
                if not buf:
                    start = c
                buf += ch
        flush(col + len(txt))
    return out


def main(argv):
    title = argv[argv.index("--title") + 1] if "--title" in argv else "nuc-console"
    lines = sys.stdin.read().rstrip("\n").split("\n")
    cols = max(sum(len(t) for _, t, _, _ in runs(ln)) for ln in lines)
    w, h = cols * CW + 2 * PAD, len(lines) * LH + 2 * PAD + BAR
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="{escape(title)}">',
         f'<rect width="{w:.0f}" height="{h:.0f}" rx="10" fill="#0d1117" stroke="#30363d"/>',
         f'<rect width="{w:.0f}" height="{BAR}" rx="10" fill="#161b22"/><rect y="{BAR - 10}" width="{w:.0f}" height="10" fill="#161b22"/>',
         '<circle cx="20" cy="17" r="6" fill="#ff5f56"/><circle cx="40" cy="17" r="6" fill="#ffbd2e"/><circle cx="60" cy="17" r="6" fill="#27c93f"/>',
         f'<text x="{w / 2:.0f}" y="22" fill="#8b949e" font-family="ui-sans-serif,system-ui,Helvetica,Arial,sans-serif" font-size="13" text-anchor="middle">{escape(title)}</text>',
         f'<g font-family="{FONT}" font-size="{FS}" xml:space="preserve">']
    for i, ln in enumerate(lines):
        o += line_svg(i, ln)
    o.append("</g></svg>")
    print("\n".join(o))


if __name__ == "__main__":
    main(sys.argv)
