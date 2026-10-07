"""nuc-console web view: the MAP, as a tree and as a graph.

The tree page (`map_nodes`, `map_native`, `map_body`, `map_row`, `details_panel`: the rows are links, the state is all in the URL, `prune`
and `universe` bound what a URL may name) and the graph page (`graph_select` picks the nodes and edges to draw, `graph_svg` and `graph_body`
draw them as one SVG with `place_labels`, `graph_doc` wraps the document; the one script of the page is graphjs.SCRIPT, which web.py sends and
pins in the CSP). Moved from web.py unchanged. Functions of a graph and of the URL parameters; the Server of web.py owns the caches and the layout
cache and calls them."""
import html
import math
import socket
import sys
import time

import graph
import htmlview
import render
import screens
from htmlview import CSS, GRAPH_CSS, MAP_CSS, sgr_class
from weburl import GZOOMS, LEVEL_CLASS, choice, gzoom, page_url

MAP_ROWS = 1000      # rows on one map page: a bigger tree is unreadable in a browser anyway (close branches, problems only)
UNIVERSE = 3000      # rows of the fully open map, to drop keys that name nothing
STATE_CLASS = {"err": "r", "down": "r", "warn": "y", "unknown": "y", "ok": "g", "info": "n"}  # htmlview.PALETTE (n: plain text)
STATE_MARK = {"down": "✖ ", "unknown": "? "}  # a symbol besides the colour
EV_CLASS = {"seen": "w B", "declared": "n", "possible": "d", "bind": "d"}  # evidence: seen bright, declared plain, possible dim
EV_OF = {glyph: ev for (_, ev), glyph in graph.ARROW.items()}


# the MAP's graph view (?view=map&as=graph): the same graph as circles and lines
GRAPH_NODES = 400    # nodes drawn on one graph page: beyond, the most relevant ones, and a note says how to see the others
ZONES = tuple(rid for rid, _, _, group in graph.ROOTS if group)  # INTERNET, LAN, TAILNET, LOCAL: the hubs (IMPACT etc. are views)
GBAD = ("err", "down", "warn", "unknown")  # needs attention, in the graph view (the colours: red, yellow)
KINDS = ("root", "port", "ct", "proc", "ext", "stack", "unit", "webapp")
STATES = ("ok", "warn", "err", "down", "unknown", "info")
ARROWS = ("seen", "declared", "possible")  # drawn with an arrowhead; 'reach' (zone -> port) and 'bind' (port -> owner) are structure
CAP_RANK = {"root": 2, "port": 3, "ct": 4, "proc": 5, "unit": 6, "webapp": 6, "stack": 7, "ext": 8}  # kept first when too many


LABEL_MAX = 24       # characters of a label under its circle (the full name is in its tooltip and the details)


def map_state(state):
    """URL parameters -> graph.State. With everything open the hand-opened list means nothing: dropped (one URL per view)."""
    st = graph.State(open=state.get("open", ()), all=state.get("all", False), shut=state.get("shut", ()), only=state.get("only", False))
    if st.all:
        st.open = set()
    return st


def state_params(st):
    return {"open": ".".join(sorted(st.open)), "shut": ".".join(sorted(st.shut)), "all": st.all, "only": st.only}


def map_here(here, st, sel, pause):
    """The map view's URL parameters, map ones first, in a fixed order: the same view is always the same URL."""
    p = {"view": "map"}
    p.update(state_params(st), sel=sel, pause=pause)
    p.update(here)
    return p


def universe(G):
    """The rows of the fully open map (at most UNIVERSE), walked once per graph (the server keeps one for r/2)."""
    if "_web_universe" not in G:
        G["_web_universe"] = graph.rows(G, graph.State(all=True), limit=UNIVERSE)
    return G["_web_universe"]


def prune(G, st):
    """Keys that name no row of the map (it changed, or a hand-made URL) are dropped: every link repeats them.
    On a map too big to walk whole, the keys of the rows this view walks are kept too: no other key changes the page."""
    if not (st.open or st.shut):
        return
    every = universe(G)
    keys = {r["key"] for r in every if r["kids"]}
    if every.truncated:
        keys |= {r["key"] for r in graph.rows(G, st, limit=MAP_ROWS) if r["kids"]}
    st.open &= keys
    st.shut &= keys


def map_head(pb, host):
    """The MAP's header line: host, time, and the problems pill coloured as on the console."""
    text, code = render.status_pill(pb)
    return (f'<div class="hd {sgr_class(code)}"><span> {html.escape(host)} │ MAP │ {time.strftime("%H:%M:%S")}</span>'
            f'<span>{html.escape(text)} </span></div>')


def map_body(G, pb, st, sel, here, host, found=None):
    """Header, legend, notes, the tree (one row per line, every row a link) and the details of the selected row, as HTML.
    Everything from the graph is escaped: labels are container and process names. found: gets the selected row's node."""
    esc = html.escape
    out = [map_head(pb, host)]
    legend = []
    for item in graph.LEGEND.split("  "):
        glyph, _, word = item.partition(" ")
        legend.append(f'<span class="{EV_CLASS.get(EV_OF.get(glyph), "")}">{esc(glyph)}</span> {esc(word)}' if glyph in EV_OF else esc(item))
    n = graph.counts(G)
    out.append(f'<div class="lg">{n["nodes"]} nodes · {n["edges"]} links · '
               + (f'<span class="r">{n["problems"]} need attention</span>' if n["problems"] else "nothing needs attention")
               + f' │ {"  ".join(legend)}  <span class="r">✖ down</span>  <span class="y">? unknown</span>  ▸ ▾ open/close · a name: its details</div>')
    out += [f'<div class="nt">· {esc(x)}</div>' for x in G.get("notes") or []]
    rs = graph.rows(G, st, limit=MAP_ROWS)
    tree = [map_row(G, row, st, sel, here) for row in rs]
    if rs.truncated:
        tree.append(f'<div class="nt">… more than {MAP_ROWS} rows: close some branches, or show problems only</div>')
    if not rs:
        tree.append('<div class="nt">nothing to show yet' + (": see the notes above" if G.get("notes") else "") + '</div>')
    panel = ""
    if sel:
        i = graph.find(rs, sel)
        row = rs[i] if i is not None else None
        if row is None:  # its branch is closed: still the same row of the fully open map
            every = universe(G)
            j = graph.find(every, sel)
            row = every[j] if j is not None else None
        panel = details_panel(G, row["node"] if row else None, st, sel, here)
        if found is not None and row:
            found.update(node=row["node"], kind=G["nodes"].get(row["node"], {}).get("kind"))
    out.append(f'<main class="mp{" two" if panel else ""}"><div class="tree">{"".join(tree)}</div>{panel}</main>')
    return "".join(out)


def map_nodes(G, st, sel, here, base, pause, found=None):
    """The Map screen's components for the web (screens.map_web): the title with its choices, the tree's rows (links) and the details of the selected
    row. here: the map's URL parameters, base: the page's own; found gets the selected row's node. The page (map_native) and the data API draw these."""
    rs = graph.rows(G, st, limit=MAP_ROWS)
    row = None
    if sel:
        i = graph.find(rs, sel)
        row = rs[i] if i is not None else None
        if row is None:  # its branch is closed: still the same row of the fully open map
            every = universe(G)
            j = graph.find(every, sel)
            row = every[j] if j is not None else None
    nid = row["node"] if row else None
    mine = {} if found is None else found
    if row:
        mine.update(node=nid, kind=G["nodes"].get(nid, {}).get("kind"))
    state = lambda **kw: page_url(here, **kw)  # noqa: E731
    links = screens.MapLinks(
        lambda r: map_url(here, st, "" if r["key"] == sel else r["key"], ""),
        lambda r: map_url(here, st.toggled(r), sel, ""),
        map_url(here, st, "", ""),
        state(**state_params(graph.State(all=True, only=st.only))),
        state(**state_params(graph.State(only=st.only))),
        lambda only: state(only=only),
        graph_url(mine, st.only, pause, base))
    return screens.map_web(G, rs, st, sel, nid, links, MAP_ROWS, rs.truncated)


def map_native(G, st, sel, here, base, pause, found=None):
    """The Map screen of the shell, drawn from components (screens.map_web, the model the console draws too): the title with its choices (expand
    all, collapse all, problems only: links with their keys, and the graph view), the tree as a list whose rows are links (a row selects it, its
    mark opens or closes the branch) and the details of the selected row beside it. here: the map's URL parameters, base: the page's own (the
    graph view keeps those). found: gets the selected row's node, as map_body does."""
    try:
        return '<div class="scr mapv">' + "".join(htmlview.html(n) for n in map_nodes(G, st, sel, here, base, pause, found)) + "</div>"
    except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
        print("nuc-console web: map render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
        return '<p class="sm">the map could not be drawn (see the service log)</p>'


def map_url(here, st, sel, anchor):
    return page_url(here, sel=sel, **state_params(st)) + (f"#r-{anchor}" if anchor else "")


def map_row(G, row, st, sel, here):
    """One row: tree glyphs, the toggle link (▸ opens, ▾ closes, ↻ already on this path), the arrow coloured by evidence,
    the name (a link that selects it) coloured by state, the port's owner in bold, the qualifier dim, the worst finding."""
    esc, k = html.escape, row["key"]
    p = graph.parts(G, row)
    if row["kids"] and not row["cycle"]:
        tip = "close" if row["open"] else f"open: {row['kids']} below"
        out = [f'<a class="tg" title="{tip}" href="{esc(map_url(here, st.toggled(row), sel, k))}">{esc(p["toggle"])}</a>']
    else:
        tip = "already shown above on this path" if row["cycle"] else "nothing below"
        out = [f'<span class="tg d" title="{tip}">{esc(p["toggle"])}</span>']
    if p["arrow"]:
        out.append(f'<span class="{EV_CLASS.get(p.get("ev"), "")}">{esc(p["arrow"])}</span> ')
    label = STATE_MARK.get(p["state"], "") + p["label"]
    out.append(f'<a class="lb {STATE_CLASS.get(p["state"], "n")}" href="{esc(map_url(here, st, k, k))}">{esc(label)}</a>')
    if p.get("port"):
        out.append(f' <span class="d">{esc(p["port"])}</span>')
    if p.get("owner"):
        out.append(f' <b class="{"y" if p["owner"] == "?" else "n"}">{esc(p["owner"])}</b>')
    if p.get("sub"):
        out.append(f'  <span class="d">{esc(p["sub"])}</span>')
    if p.get("note"):
        lv = next((lv for lv, t in G["nodes"][row["node"]].get("findings") or [] if t == p["note"]), "")  # else: when it was seen
        out.append(f'  <span class="{LEVEL_CLASS.get(lv, "d") if lv != "ok" else "d"}">{esc(p["note"])}</span>')
    if k == sel:
        out.append(' <a class="dl" href="#details">details ↓</a>')
    return (f'<div class="ro{" sel" if k == sel else ""}" id="r-{esc(k)}"><span class="tr">{esc(p["tree"])}</span>'
            f'<span class="bd">{"".join(out)}</span></div>')


def details_panel(G, nid, st, sel, here, close=None, tools=""):
    """Everything graph.details knows about the selected node, as a table; 'close' keeps the page on the row.
    The graph view gives its own close URL, and the links to the node's neighbourhood (tools: HTML)."""
    esc, trs = html.escape, []
    for i, (label, value, level) in enumerate(graph.details(G, nid)):
        top, indent = ' class="top"' if i == 0 else "", ' class="in"' if label.startswith("  ") else ""
        trs.append(f'<tr{top}><th{indent}>{esc(label.strip())}</th><td class="{LEVEL_CLASS.get(level, "")}">{esc(str(value))}</td></tr>')
    close = map_url(here, st, "", sel) if close is None else close
    return (f'<aside class="dp" id="details"><div class="dh"><span>DETAILS</span>{tools}'
            f'<a href="{esc(close)}">close ✕</a></div><table>{"".join(trs)}</table></aside>')


def mode_switch(tree, graph_):
    """'tree | graph' on the MAP page: the URL of the other mode, None for the current one."""
    return choice([("tree", tree), ("graph", graph_)])


def zoom_steps(link, z):
    """'− 100% +' for the drawing's size (z=)."""
    cur = z or 100
    i = GZOOMS.index(cur)
    return ((link("−", z=gzoom(GZOOMS[i - 1])) if i else "−") + f" {cur}% "
            + (link("+", z=gzoom(GZOOMS[i + 1])) if i + 1 < len(GZOOMS) else "+"))


def graph_here(here, sel="", local=0, only=False, stacks=False, ext="", z=0, pause=False):
    """The graph view's URL parameters in a fixed order (one URL per view), sel last: a node's link is the view plus its key."""
    p = {"view": "map", "as": "graph", "only": only, "stacks": stacks, "ext": ext, "z": z, "pause": pause}
    p.update(here)
    p.update(local=local, sel=sel)
    return p


def graph_url(found, only, pause, here):
    """The graph view of a tree view: the selected row's node selected (a project turns the stacks on), same filter and pace."""
    nid, kind = found.get("node"), found.get("kind")
    sel = graph.path_key((nid,)) if nid and (kind != "root" or nid in ZONES) else ""
    return page_url(graph_here(here, sel, 0, only, bool(sel) and kind == "stack", "", 0, pause))


def tree_url(G, nid, only, pause, here):
    """The tree view of a graph view: the selected node's first row in the fully open tree, selected, its branch opened."""
    st = graph.State(only=only)
    every = universe(G) if nid is not None else []
    j = next((i for i, row in enumerate(every) if row["node"] == nid), None)
    if j is None:
        return page_url(map_here(here, st, "", pause))
    depth = every[j]["depth"]
    for row in reversed(every[:j]):  # its ancestors: the nearest row above it at each smaller depth (roots are open anyway)
        if depth == 0:
            break
        if row["depth"] < depth:
            depth = row["depth"]
            if depth:
                st.open.add(row["key"])
    key = every[j]["key"]
    return page_url(map_here(here, st, key, pause)) + "#r-" + key


def reachable(edges, start, back=False):
    """start and every node a path of these edges leads to from it (back: every node from which a path leads to it)."""
    nxt = {}
    for e in edges:
        a, b = (e["dst"], e["src"]) if back else (e["src"], e["dst"])
        nxt.setdefault(a, []).append(b)
    out, todo = set(start), list(start)
    while todo:
        for x in nxt.get(todo.pop(), ()):
            if x not in out:
                out.add(x)
                todo.append(x)
    return out


def problems(edges, ids, bad):
    """'Problems only': the nodes that need attention, and those on a path from a zone to one of them (as the tree's
    problems only: a client address connected to a front door is not on that path)."""
    return bad | (reachable(edges, [z for z in ZONES if z in ids]) & reachable(edges, bad, back=True))


def around(edges, start, hops):
    """start and the nodes at most `hops` edges away from it, whichever way the edges point (Obsidian's local graph)."""
    near = {}
    for e in edges:
        near.setdefault(e["src"], set()).add(e["dst"])
        near.setdefault(e["dst"], set()).add(e["src"])
    out, frontier = {start}, [start]
    for _ in range(hops):
        nxt = [y for x in frontier for y in sorted(near.get(x, ())) if y not in out]
        out.update(nxt)
        frontier = nxt
    return out


def graph_select(G, sel="", local=0, only=False, stacks=False, ext=True):
    """What the graph view draws: {ids, edges, keys {id: key}, sel (id or None), bad (ids), hidden (count)}.

    Every node but the IMPACT/STACKS/OUTBOUND views (their nodes are drawn anyway), compose projects with stacks, remote
    addresses unless ext is false; only: problems() (what needs attention and the paths from a zone to it); local=1|2 with
    a selected node: it and its neighbours within that many hops. At most GRAPH_NODES: the selected node, the problems and
    their paths, zones, ports, containers, processes, then remote addresses seen last. sel: the key of a node that passes
    the filters, else None. The edges are every edge of the graph between two drawn nodes."""
    nodes, keys, taken = G["nodes"], {}, set()
    for nid in sorted(nodes):
        kind = nodes[nid]["kind"]
        if (kind == "root" and nid not in ZONES) or (kind == "stack" and not stacks) or (kind == "ext" and not ext):
            continue
        k = graph.path_key((nid,))
        if k not in taken:  # two ids with one key (40 bits): the first one only, so that a key names one node
            taken.add(k)
            keys[nid] = k
    ids = set(keys)
    within = lambda ids: [e for e in G["edges"] if e["src"] in ids and e["dst"] in ids]  # noqa: E731
    edges = within(ids)
    bad = {x for x in ids if nodes[x]["state"] in GBAD}
    if only:
        ids = problems(edges, ids, bad)
        edges = within(ids)
    cur = next((x for x in sorted(ids) if keys[x] == sel), None) if sel else None
    if cur is not None and local in (1, 2):
        ids = around(edges, cur, local)
        edges = within(ids)
    hidden = max(0, len(ids) - GRAPH_NODES)
    if hidden:
        lead = problems(edges, ids, bad & ids)
        last = {}  # remote address -> when it was last seen connected
        for e in edges:
            t = G["now"] if e.get("n") else (e.get("last") or 0)
            for x in (e["src"], e["dst"]):
                if nodes[x]["kind"] == "ext":
                    last[x] = max(last.get(x, 0), t)
        ids = set(sorted(ids, key=lambda x: (x != cur, x not in lead, CAP_RANK.get(nodes[x]["kind"], 9), -last.get(x, 0), x))[:GRAPH_NODES])
        edges = within(ids)
    return {"ids": ids, "edges": edges, "keys": {x: keys[x] for x in ids}, "sel": cur, "bad": bad & ids, "hidden": hidden}


def canvas(n):
    """The drawing's size in SVG units for n nodes: 1000 x 700 up to 60, growing toward 2000 x 1400 at GRAPH_NODES."""
    w = 1000 if n <= 60 else min(2000, 1000 + (n - 60) * 1000 // (GRAPH_NODES - 60))
    w = int(round(w / 50.0)) * 50
    return w, int(w * 0.7)


def f1(v):
    """A coordinate: a plain decimal, at most one digit after the point."""
    s = "%.1f" % v
    return s[:-2] if s.endswith(".0") else s


def spot(p, w, h):
    """A layout position, kept inside the drawing (room for the label under the circle); the centre if it is not a number."""
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        x, y = w / 2.0, h / 2.0
    if not (math.isfinite(x) and math.isfinite(y)):
        x, y = w / 2.0, h / 2.0
    return min(max(x, 20.0), w - 20.0), min(max(y, 20.0), h - 34.0)


MARKERS = "".join(f'<marker id="ah-{ev}" class="mk {ev}" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" '
                  f'markerUnits="userSpaceOnUse" orient="auto"><path d="M0,0L10,5L0,10z"/></marker>' for ev in ARROWS)
ARROW_GAP = 4    # between an arrowhead's tip and the circle it points at (graphjs.SCRIPT keeps the same when a node moves)


def graph_svg(G, v, pos, w, h, z, gh):
    """The SVG of the drawing, per the DOM contract with graphjs.SCRIPT: edges first (endpoints on the circles' edges),
    then nodes; every node a link selecting it; sel / nb (its neighbours, its edges) / dim (the others) when one is
    selected. Every text from the graph is escaped (quotes too) and its control characters replaced."""
    nodes, keys, cur, bad, esc = G["nodes"], v["keys"], v["sel"], v["bad"], html.escape
    deg = {}
    for e in v["edges"]:
        for x in (e["src"], e["dst"]):
            deg[x] = deg.get(x, 0) + 1
    rad = {x: 18.0 if nodes[x]["kind"] == "root" else min(14.0, 6 + 1.6 * math.sqrt(deg.get(x, 0))) for x in v["ids"]}
    xy = {x: spot(pos.get(x), w, h) for x in v["ids"]}
    near = {e["dst"] if e["src"] == cur else e["src"] for e in v["edges"] if cur in (e["src"], e["dst"])} - {cur} if cur else set()
    lines = []
    for e in sorted(v["edges"], key=lambda e: (cur in (e["src"], e["dst"]), e["dst"] in bad, graph.EV_RANK.get(e["ev"], 0),
                                               keys[e["src"]], keys[e["dst"]])):
        a, b = e["src"], e["dst"]
        ev = "reach" if e["ev"] == "bind" and a in ZONES else e["ev"] if e["ev"] in ARROWS else "bind"
        (x1, y1), (x2, y2) = xy[a], xy[b]
        ra, rb, d = rad[a], rad[b] + (ARROW_GAP if ev in ARROWS else 0), math.hypot(x2 - x1, y2 - y1)
        if d > ra + rb + 1:  # else the circles touch: centre to centre, under them
            ux, uy = (x2 - x1) / d, (y2 - y1) / d
            x1, y1, x2, y2 = x1 + ux * ra, y1 + uy * ra, x2 - ux * rb, y2 - uy * rb
        cls = "e " + ev + (" bad" if (a in bad and b in bad) or (e["ev"] in ("seen", "declared") and b in bad) else "") \
            + (" nb" if cur in (a, b) else "")
        lines.append(f'<line class="{cls}" data-a="{keys[a]}" data-b="{keys[b]}" x1="{f1(x1)}" y1="{f1(y1)}" x2="{f1(x2)}" '
                     f'y2="{f1(y2)}"' + (f' marker-end="url(#ah-{ev})"' if ev in ARROWS else "") + "/>")
    texts = {}
    for x in v["ids"]:
        label = graph.safe(nodes[x]["label"])
        state = nodes[x]["state"] if nodes[x]["state"] in STATES else "unknown"
        texts[x] = STATE_MARK.get(state, "") + (label if len(label) <= LABEL_MAX else label[:LABEL_MAX - 1] + "…")
    spots = place_labels(sorted(v["ids"], key=lambda x: (nodes[x]["kind"] != "root", -deg.get(x, 0), keys[x])), xy, rad, texts)
    circles = []
    for x in sorted(v["ids"], key=lambda x: (x == cur, x in near, x in bad, nodes[x]["kind"] == "root", keys[x])):
        n, k, (cx, cy), r = nodes[x], keys[x], xy[x], rad[x]
        state = n["state"] if n["state"] in STATES else "unknown"
        kind = n["kind"] if n["kind"] in KINDS else "other"
        label, sub = graph.safe(n["label"]), graph.safe(n.get("sub") or "")
        lx, ly, anchor = spots[x]
        cls = f"n {state} k-{kind}" + (" sel" if x == cur else " nb" if x in near else " dim" if cur else "")
        circles.append(f'<a class="{cls}" id="n-{k}" data-k="{k}" href="{esc(page_url(gh, sel=k))}">'
                       f'<circle cx="{f1(cx)}" cy="{f1(cy)}" r="{f1(r)}"/><text class="lb{anchor}" x="{f1(lx)}" y="{f1(ly)}">'
                       f'{esc(texts[x])}</text><title>{esc(label + (" · " + sub if sub else ""))}</title></a>')
    zz = z or 100
    return (f'<svg id="gsvg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{round(w * zz / 100)}" '
            f'height="{round(h * zz / 100)}" data-w="{w}" data-h="{h}"><defs>{MARKERS}</defs><g id="gvp">'
            f'<g class="ge">{"".join(lines)}</g><g class="gn">{"".join(circles)}</g></g></svg>')


LABEL_CHAR_W, LABEL_H = 6.4, 12.0  # 11px sans-serif (GRAPH_CSS): average character width and line height, in SVG units


def place_labels(order, xy, rad, texts):
    """{id: (x, y, class suffix)}: each label under its circle (the default), else above, right or left of it, whichever
    first overlaps no circle and no label placed before it (order: the most important nodes first); the least bad when all
    do. Deterministic, so a refresh does not move labels around. The script keeps each label's offset from its circle."""
    boxes = [(cx - rad[x], cy - rad[x], cx + rad[x], cy + rad[x]) for x, (cx, cy) in xy.items()]
    out = []

    def overlap(b):
        return sum(max(0.0, min(b[2], o[2]) - max(b[0], o[0])) * max(0.0, min(b[3], o[3]) - max(b[1], o[1]))
                   for o in boxes + out)
    spots = {}
    for x in order:
        (cx, cy), r, w = xy[x], rad[x], len(texts[x]) * LABEL_CHAR_W
        cands = ((cx, cy + r + 12, "", (cx - w / 2, cy + r + 2, cx + w / 2, cy + r + 2 + LABEL_H)),      # below
                 (cx, cy - r - 5, "", (cx - w / 2, cy - r - 4 - LABEL_H, cx + w / 2, cy - r - 4)),        # above
                 (cx + r + 4, cy + 4, " ls", (cx + r + 4, cy - LABEL_H / 2, cx + r + 4 + w, cy + LABEL_H / 2)),  # right
                 (cx - r - 4, cy + 4, " le", (cx - r - 4 - w, cy - LABEL_H / 2, cx - r - 4, cy + LABEL_H / 2)))  # left
        own = (cx - r, cy - r, cx + r, cy + r)
        boxes.remove(own)  # a label never collides with its own circle
        best = min(cands, key=lambda c: (overlap(c[3]) > 0, overlap(c[3]), cands.index(c)))
        boxes.append(own)
        spots[x] = best[:3]
        out.append(best[3])
    return spots


def legend_line(ev, text):
    return (f'<svg class="lk" viewBox="0 0 30 10" width="30" height="10" aria-hidden="true"><line class="ll {ev}" x1="1" y1="5" '
            f'x2="{27 if ev in ARROWS else 29}" y2="5"' + (f' marker-end="url(#ah-{ev})"' if ev in ARROWS else "") + f'/></svg> {text}')


def legend_dot(state, text):
    return (f'<svg class="lk" viewBox="0 0 14 14" width="14" height="14" aria-hidden="true"><g class="ln {state}">'
            f'<circle cx="7" cy="7" r="5"/></g></svg> {text}')


GRAPH_LEGEND = ("  ".join([legend_line("seen", "seen"), legend_line("declared", "declared"), legend_line("possible", "same network"),
                           legend_line("reach", "zone → port → service"), legend_line("reach bad", "leads to a problem")])
                + " │ " + "  ".join([legend_dot("ok", "ok"), legend_dot("warn", "attention"), legend_dot("err", "failing"),
                                     legend_dot("down", "✖ down"), legend_dot("unknown", "? unknown"), legend_dot("info", "info"),
                                     legend_dot("info ext", "remote address")]))


def graph_body(G, pb, v, pos, w, h, gh, r, host):
    """Header, legend, notes, the drawing and the details of the selected node, as HTML."""
    esc, n = html.escape, graph.counts(G)
    out = [map_head(pb, host)]
    links = sum(e["ev"] in ARROWS for e in v["edges"])
    out.append(f'<div class="lg">{len(v["ids"])} nodes · {links} links · '
               + (f'<span class="r">{n["problems"]} need attention</span>' if n["problems"] else "nothing needs attention")
               + f' │ {GRAPH_LEGEND} · a node: its details' + (' <a class="dl" href="#details">details ↓</a>' if v["sel"] else "") + "</div>")
    out += [f'<div class="nt">· {esc(graph.safe(x))}</div>' for x in G.get("notes") or []]
    if v["hidden"]:
        out.append(f'<div class="nt">+{v["hidden"]} more not drawn: show problems only, or select a node and its local graph</div>')
    if not v["ids"]:
        out.append('<div class="nt">' + ("nothing needs attention" if gh["only"] else "nothing to show yet"
                                         + (": see the notes above" if G.get("notes") else "")) + "</div>")
    panel = ""
    if v["sel"]:
        local = gh["local"]
        tools = choice([("local 1", None if local == 1 else page_url(gh, local=1)), ("local 2", None if local == 2 else page_url(gh, local=2)),
                        ("whole graph", page_url(gh, local=0) if local else None)])
        panel = details_panel(G, v["sel"], None, gh["sel"], gh, close=page_url(gh, local=0, sel=""), tools=tools)
    paused = "1" if gh["pause"] else "0"
    out.append(f'<main class="mp{" two" if panel else ""}"><div class="gv" id="gv" data-refresh="{r}" data-paused="{paused}" '
               f'data-state="{esc(page_url(gh, sel=""))}">{graph_svg(G, v, pos, w, h, gh["z"], gh)}</div>{panel}</main>')
    return "".join(out)


def graph_doc(r, zoom, z, body, foot, refresh, script):
    """The graph page: with a script, the meta refresh only for browsers that run none (the script reloads the page itself);
    the drawing fills the column at z=100 (no scrolling), z% of it otherwise (the box scrolls)."""
    meta = f'<meta http-equiv="refresh" content="{r}">'
    size = f"#gsvg{{width:{z}%}}" if z else "#gsvg{width:100%;max-height:calc(100vh - 150px)}"
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            + ((f"<noscript>{meta}</noscript>" if script else meta) if refresh else "")
            + f'<title>{html.escape(socket.gethostname())} · map · nuc-console</title><style>{CSS}{MAP_CSS}{GRAPH_CSS}'
            + "body{font-size:%.1fpx}" % (14 * zoom / 100) + size + f'</style>{body}<footer>{" · ".join(foot)}</footer>'
            + (f"<script>{script}</script>" if script else "") + "</html>")
