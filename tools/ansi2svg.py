#!/usr/bin/env python3
"""Turn the ANSI output of `render.py --once --demo --color` into a crisp SVG "terminal window" (stdlib only).

    python3 src/render.py --once --demo --color --cols 130 --rows 36 | python3 tools/ansi2svg.py --title "nuc-console" > docs/img/overview.svg

Every run of text is positioned on the character grid, so the alignment does not depend on the viewer's monospace font.
"""
import re
import sys
from xml.sax.saxutils import escape

FG = {30: "#484f58", 31: "#ff7b72", 32: "#3fb950", 33: "#d29922", 34: "#58a6ff", 35: "#bc8cff", 36: "#39c5cf", 37: "#c9d1d9",
      90: "#6e7681", 91: "#ffa198", 92: "#56d364", 93: "#e3b341", 94: "#79c0ff", 95: "#d2a8ff", 96: "#56d4dd", 97: "#f0f6fc"}
DEFAULT = "#c9d1d9"
CW, LH, PAD, BAR = 8.4, 18.0, 16, 34   # cell width, line height, padding, title-bar height
SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def runs(line):
    """-> [(col, text, fg, bold)] for one line."""
    out, fg, bold, col, pos = [], DEFAULT, False, 0, 0
    line = OTHER.sub(lambda m: m.group(0) if SGR.fullmatch(m.group(0)) else "", line)
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
    tail = line[pos:]
    if tail:
        out.append((col, tail, fg, bold))
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
         f'<text x="{w / 2:.0f}" y="22" fill="#8b949e" font-family="ui-sans-serif,system-ui,sans-serif" font-size="13" text-anchor="middle">{escape(title)}</text>',
         '<g font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,\'DejaVu Sans Mono\',monospace" font-size="13.5" xml:space="preserve">']
    for i, ln in enumerate(lines):
        y = BAR + PAD + (i + 0.8) * LH
        for col, txt, fg, bold in runs(ln):
            if not txt.strip():
                continue
            o.append(f'<text x="{PAD + col * CW:.1f}" y="{y:.1f}" fill="{fg}" textLength="{len(txt) * CW:.1f}" lengthAdjust="spacingAndGlyphs"'
                     + (' font-weight="700"' if bold else "") + f'>{escape(txt)}</text>')
    o.append("</g></svg>")
    print("\n".join(o))


if __name__ == "__main__":
    main(sys.argv)
