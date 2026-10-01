"""nuc-console graph view: where each node of the MAP sits on the page. Pure, no I/O, no randomness.

layout() is deterministic: the same nodes and edges give the same positions whatever their order, and a node keeps roughly
its place when others come and go (its starting point is a hash of its id), so a page that refreshes does not jump.

How, in layout units where an ordinary edge wants length 1 (the page scale comes last):
1. start: every node at a point hashed from its id, in a box with the page's aspect ratio;
2. smooth: per connected component, a few rounds of "move halfway to the mean of your neighbours" (a power iteration
   towards the spectral layout), so connected nodes start near each other and the force step has little to untangle;
3. relax: Fruchterman-Reingold, one component at a time, at a low, falling temperature: every pair repels (k^2/d), edges
   attract (d^2/k), an edge whose busier end is a hub wants to be longer (the leaves of a hub get room); repulsion is
   exact up to EXACT_N nodes, beyond on a grid (near cells exact, far cells by their centre of mass and its gradient);
4. pack: the components, each in its bounding box, go side by side in rows (the row width that fills the page best, the
   tallest first), so disconnected parts never overlap and never drift off the page;
5. spread: pairs closer than a minimum page distance are pushed apart (circles and labels must not overlap);
6. fit: one scale for both axes, centred, capped so that a small graph is centred rather than blown up (the mean edge
   is at most PAGE_EDGE_MAX page units long); if the box limits it, the axis with room left is stretched a little.
Positions are complex numbers (x + yj): the repulsion on p from all q is conj(sum 1 / (p - q)), one C-level map per node.
"""
import hashlib
import math
from operator import mul, truediv

ITERATIONS = 30          # force steps
SMOOTH_ROUNDS = 10
TEMPERATURE = 0.03       # first step of the force relaxation, in edge lengths per sqrt(nodes of the component)
SPREAD_PASSES = 30
FULL_N = 400             # above this many nodes, fewer steps of each kind, so that a huge MAP stays within its time budget
EXACT_N = 120            # all-pairs repulsion up to here, the grid above (about 2x faster at 150 nodes, 4x at 600)
CELL_NODES = 3.5         # grid cells sized for this many nodes each on average over the bounding box (more on huge graphs)
HUB = 0.3                # ideal edge length 1 + HUB * (sqrt(degree of the busier end) - 1)
PACK_GAP = 0.8           # between the boxes of two components, in layout units
PAGE_EDGE_MAX = 140.0    # page units: the mean edge is at most this long, and so is a layout unit
STRETCH_MAX = 1.35       # the layout may be stretched this much along the axis that has room left
PAGE_GAP = 30.0          # wanted page distance between any two nodes (less when the page is crowded)

_ONE = 1 + 0j


def layout(nodes, edges, width=1000.0, height=700.0, margin=40.0):
    """nodes: ids; edges: (id, id) pairs (direction ignored, unknown ids skipped) -> {id: (x, y)} inside the margins."""
    ids = sorted(set(nodes))
    n = len(ids)
    cx, cy = width / 2.0, height / 2.0
    if n < 2:
        return {nid: (cx, cy) for nid in ids}
    index = {nid: i for i, nid in enumerate(ids)}
    pairs = set()
    for a, b in edges:
        ia, ib = index.get(a), index.get(b)
        if ia is not None and ib is not None and ia != ib:
            pairs.add((ia, ib) if ia < ib else (ib, ia))
    pairs = sorted(pairs)
    nbrs = [[] for _ in range(n)]
    for a, b in pairs:
        nbrs[a].append(b)
        nbrs[b].append(a)

    aw, ah = max(0.0, width - 2.0 * margin), max(0.0, height - 2.0 * margin)
    sx = math.sqrt(min(max(aw / ah, 0.25), 4.0)) if aw > 0 and ah > 0 else 1.0
    sy = 1.0 / sx                     # sx / sy is the page's aspect ratio
    side = 1.4 * math.sqrt(n)
    P = []
    for nid in ids:
        h = hashlib.sha256(str(nid).encode("utf-8", "surrogatepass")).digest()
        u, v = int.from_bytes(h[:4], "big") / 4294967296.0, int.from_bytes(h[4:8], "big") / 4294967296.0
        P.append(complex((u - 0.5) * side * sx, (v - 0.5) * side * sy))
    work = min(1.0, FULL_N / n)
    comps = _components(nbrs)
    _smooth(P, comps, nbrs, sx, sy, max(8, int(SMOOTH_ROUNDS * math.sqrt(work))))
    _relax_components(P, pairs, nbrs, comps, max(2, int(ITERATIONS * work)))
    _pack(P, comps, aw, ah)

    s = _scale(P, aw, ah, pairs)
    if aw > 0 and ah > 0:
        gap = min(PAGE_GAP, 0.45 * math.sqrt(aw * ah / n))
        for _ in range(3):            # spreading can widen the layout, so shrink the scale, so spread a little more
            _separate(P, gap / s * 1.02, max(3, int(SPREAD_PASSES * work)))
            s2 = _scale(P, aw, ah, pairs)
            done, s = s2 >= s * 0.98, s2
            if done:
                break
    xs = [p.real for p in P]
    ys = [p.imag for p in P]
    mx, my = (max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0
    kx, ky = 1.0, 1.0
    if aw > 0 and ah > 0 and max(xs) > min(xs) and max(ys) > min(ys):
        fx, fy = (max(xs) - min(xs)) * s / aw, (max(ys) - min(ys)) * s / ah     # how much of the box each axis fills
        if max(fx, fy) > 0.999:                       # the box limits the scale (not the cap): use the other axis too
            k = min(STRETCH_MAX, max(fx, fy) / min(fx, fy))
            kx, ky = (k, 1.0) if fx < fy else (1.0, k)
    out = {}
    for nid, x, y in zip(ids, xs, ys):
        x = min(max(cx + (x - mx) * s * kx, margin), width - margin) if aw > 0 else cx
        y = min(max(cy + (y - my) * s * ky, margin), height - margin) if ah > 0 else cy
        out[nid] = (x, y)
    return out


def _components(nbrs):
    """Connected components as sorted index lists, the largest first (ties: the one with the smallest index)."""
    seen = [False] * len(nbrs)
    comps = []
    for s in range(len(nbrs)):
        if seen[s]:
            continue
        seen[s] = True
        comp = [s]
        for i in comp:
            for j in nbrs[i]:
                if not seen[j]:
                    seen[j] = True
                    comp.append(j)
        comps.append(sorted(comp))
    comps.sort(key=lambda c: (-len(c), c[0]))
    return comps


def _smooth(P, comps, nbrs, sx, sy, rounds):
    """Replace each component's hashed start by a few rounds of degree-normalised power iteration (Koren's spectral
    layout, not run to convergence): x and y are kept centred, D-orthogonal to each other and of unit D-norm. The
    result depends smoothly on the graph and on the hashed start, which also fixes its sign and orientation."""
    for comp in comps:
        m = len(comp)
        if m == 2:                    # an edge of ordinary length, in the direction of the hashed pair
            a, b = comp
            c, d = (P[a] + P[b]) / 2.0, P[b] - P[a]
            u = d / abs(d) if abs(d) > 0 else _ONE
            P[a], P[b] = c - u / 2.0, c + u / 2.0
        if m < 3:
            continue
        loc = {g: k for k, g in enumerate(comp)}
        nb = [[loc[j] for j in nbrs[g]] for g in comp]
        d = [float(len(v)) for v in nb]
        dsum = sum(d)
        rng = range(m)
        x = [P[g].real for g in comp]
        y = [P[g].imag for g in comp]
        centre = sum([P[g] for g in comp]) / m
        for _ in range(rounds):
            x = [(x[k] + sum([x[j] for j in nb[k]]) / d[k]) * 0.5 for k in rng]
            y = [(y[k] + sum([y[j] for j in nb[k]]) / d[k]) * 0.5 for k in rng]
            mx = sum(map(mul, d, x)) / dsum
            my = sum(map(mul, d, y)) / dsum
            x = [v - mx for v in x]
            y = [v - my for v in y]
            dx = list(map(mul, d, x))
            xx = sum(map(mul, dx, x))
            if xx > 0:
                f = sum(map(mul, dx, y)) / xx
                y = [y[k] - f * x[k] for k in rng]
                f = math.sqrt(dsum / xx)
                x = [v * f for v in x]
            yy = sum(map(mul, map(mul, d, y), y))
            if yy > 0:
                f = math.sqrt(dsum / yy)
                y = [v * f for v in y]
        r = 0.5 * math.sqrt(m)
        for k, g in enumerate(comp):
            e = math.sqrt(x[k] * x[k] + y[k] * y[k])
            f = r if e <= 2.0 else r * 2.0 / e   # a long dangling chain must not start far away
            P[g] = centre + complex(x[k] * f * sx, y[k] * f * sy)


def _relax_components(P, pairs, nbrs, comps, iters):
    """Relax each component (two or more nodes) on its own; P is updated in place."""
    where = [None] * len(P)
    for c, comp in enumerate(comps):
        for k, g in enumerate(comp):
            where[g] = (c, k)
    springs = [[] for _ in comps]
    for a, b in pairs:
        (c, ka), (_, kb) = where[a], where[b]
        ideal = 1.0 + HUB * (math.sqrt(max(len(nbrs[a]), len(nbrs[b]))) - 1.0)
        springs[c].append((ka, kb, 1.0 / (ideal * ideal * ideal)))   # d^2 / ideal^3 balances 1 / d at d = ideal
    for comp, sp in zip(comps, springs):
        if len(comp) > 1:
            Q = [P[g] for g in comp]
            _relax(Q, sp, iters, TEMPERATURE * math.sqrt(len(comp)))
            for g, q in zip(comp, Q):
                P[g] = q


def _relax(P, springs, iters, t0):
    """Fruchterman-Reingold, Jacobi steps, each node moving at most the temperature, which falls linearly."""
    repel = _repel_exact if len(P) <= EXACT_N else _repel_grid
    for it in range(iters):
        t = t0 + (0.01 - t0) * it / iters
        F = repel(P)
        for a, b, c in springs:
            d = P[b] - P[a]
            f = d * (abs(d) * c)
            F[a] += f
            F[b] -= f
        for i, f in enumerate(F):
            m = abs(f)
            if m > t:
                f *= t / m
            P[i] += f


def _pack(P, comps, aw, ah):
    """Move the components (each as a whole) side by side: shelves of boxes, the tallest first, the shelf width that
    gives the largest scale on the page; a single component stays where it is."""
    if len(comps) < 2:
        return
    boxes = []
    for comp in comps:
        xs = [P[g].real for g in comp]
        ys = [P[g].imag for g in comp]
        boxes.append((min(xs), min(ys), max(xs) - min(xs) + PACK_GAP, max(ys) - min(ys) + PACK_GAP))
    order = sorted(range(len(comps)), key=lambda c: (-boxes[c][3], -boxes[c][2], c))
    widest = max(b[2] for b in boxes)
    total = sum(b[2] for b in boxes)
    best = None
    for k in range(9):
        spots, bw, bh = _shelves(order, boxes, widest * (total / widest) ** (k / 8.0))
        score = min(aw / bw, ah / bh) if aw > 0 and ah > 0 else -bw * bh
        if best is None or score > best[0] * (1.0 + 1e-9):
            best = (score, spots)
    for comp, (x0, y0, _, _), (x, y) in zip(comps, boxes, best[1]):
        shift = complex(x - x0 + PACK_GAP / 2.0, y - y0 + PACK_GAP / 2.0)
        for g in comp:
            P[g] += shift


def _shelves(order, boxes, width):
    """Fill rows up to the given width with the boxes in the given order -> (top-left corner of each box, width, height
    of the whole); the boxes of a row are centred on it, the rows on each other."""
    rows = []
    row, used, high = [], 0.0, 0.0
    for c in order:
        w, h = boxes[c][2], boxes[c][3]
        if row and used + w > width:
            rows.append((row, used, high))
            row, used, high = [], 0.0, 0.0
        row.append(c)
        used += w
        high = max(high, h)
    rows.append((row, used, high))
    total_w = max(r[1] for r in rows)
    total_h = sum(r[2] for r in rows)
    spots = [None] * len(boxes)
    y = 0.0
    for row, used, high in rows:
        x = (total_w - used) / 2.0
        for c in row:
            spots[c] = (x, y + (high - boxes[c][3]) / 2.0)
            x += boxes[c][2]
        y += high
    return spots, total_w, total_h


def _repel_exact(P):
    """Repulsion 1/d between all pairs: conj(sum over q != p of 1 / (p - q)) for each p."""
    out = []
    for i, p in enumerate(P):
        try:
            s = sum(map(_ONE.__truediv__, map(p.__sub__, P[:i] + P[i + 1:])))
        except ZeroDivisionError:
            s = _repel_slow(P, i, [j for j in range(len(P)) if j != i])
        out.append(s.conjugate())
    return out


def _repel_slow(P, i, js):
    """The sum for node i over nodes js, when some sit on the same point: those push apart along x, in index order."""
    p = P[i]
    s = 0j
    for j in js:
        q = P[j]
        s += 1 / (p - q) if p != q else complex(1e3 if i < j else -1e3, 0.0)
    return s


def _repel_grid(P):
    """The same, with nodes binned into cells: the 3x3 cells around a node exactly, every other cell as its node count
    at its centre of mass, that far field taken at the node's cell centre plus its gradient (first-order expansion)."""
    n = len(P)
    xs = [p.real for p in P]
    ys = [p.imag for p in P]
    x0, y0 = min(xs), min(ys)
    cell = math.sqrt(max((max(xs) - x0) * (max(ys) - y0), 1e-12) * max(CELL_NODES, math.sqrt(n) / 12.0) / n)
    inv = 1.0 / cell
    cells = {}                        # insertion order follows the node order: deterministic
    for i in range(n):
        key = (int((xs[i] - x0) * inv), int((ys[i] - y0) * inv))
        if key in cells:
            cells[key].append(i)
        else:
            cells[key] = [i]
    keys = list(cells)
    where = {key: k for k, key in enumerate(keys)}
    mass = [float(len(cells[key])) for key in keys]
    com = [sum([P[i] for i in cells[key]]) / len(cells[key]) for key in keys]
    out = [0j] * n
    for k, (gx, gy) in enumerate(keys):
        z0 = complex(x0 + (gx + 0.5) * cell, y0 + (gy + 0.5) * cell)
        near, near_ids, saved = [], [], []
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                b = where.get((gx + ox, gy + oy))
                if b is not None:
                    saved.append((b, mass[b], com[b]))
                    mass[b] = 0.0     # the near cells are left out of the far field...
                    if b != k:
                        near_ids.extend(cells[keys[b]])
                        near.extend([P[i] for i in cells[keys[b]]])
        com[k] = z0 + cell            # ...and the own cell's centre of mass may sit on z0: move it (its mass is 0)
        dz = list(map(z0.__sub__, com))
        far = sum(map(truediv, mass, dz))
        slope = sum(map(truediv, mass, map(mul, dz, dz)))
        for b, m, c in saved:
            mass[b], com[b] = m, c
        members = cells[keys[k]]
        own = [P[i] for i in members]
        for t, i in enumerate(members):
            p = P[i]
            try:
                s = sum(map(_ONE.__truediv__, map(p.__sub__, near + own[:t] + own[t + 1:])))
            except ZeroDivisionError:
                s = _repel_slow(P, i, near_ids + members[:t] + members[t + 1:])
            out[i] = (far - slope * (p - z0) + s).conjugate()
    return out


def _separate(P, dmin, passes):
    """Push apart, pair by pair, nodes closer than dmin (a grid of dmin cells finds the pairs)."""
    d2 = dmin * dmin
    inv = 1.0 / dmin
    goal = dmin * 1.01
    for _ in range(passes):
        grid = {}
        for i, p in enumerate(P):
            key = (math.floor(p.real * inv), math.floor(p.imag * inv))
            if key in grid:
                grid[key].append(i)
            else:
                grid[key] = [i]
        moved = False
        for (gx, gy), members in grid.items():
            for ox, oy in ((0, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):   # each pair of neighbouring cells once
                other = members if ox == oy == 0 else grid.get((gx + ox, gy + oy))
                if not other:
                    continue
                for t, i in enumerate(members):
                    for j in (other[t + 1:] if other is members else other):
                        d = P[j] - P[i]
                        q = d.real * d.real + d.imag * d.imag
                        if q < d2:
                            dist = math.sqrt(q)
                            push = d * ((goal - dist) * 0.5 / dist) if dist > 0 else complex(goal * 0.5, 0.0)
                            P[i] -= push
                            P[j] += push
                            moved = True
        if not moved:
            return


def _scale(P, aw, ah, pairs):
    """Page units per layout unit: the layout fills the box on one axis, unless that would blow a small graph up (then
    the mean edge, in the page's units, is at most PAGE_EDGE_MAX)."""
    xs = [p.real for p in P]
    ys = [p.imag for p in P]
    bw, bh = max(xs) - min(xs), max(ys) - min(ys)
    s = PAGE_EDGE_MAX
    if bw > 0:
        s = min(s, aw / bw)
    if bh > 0:
        s = min(s, ah / bh)
    if pairs:
        mean = sum([abs(P[b] - P[a]) for a, b in pairs]) / len(pairs)
        if mean > 0:
            s = min(s, PAGE_EDGE_MAX / mean)
    return s
