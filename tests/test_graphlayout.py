import itertools
import math
import os
import random
import statistics
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import graphlayout  # noqa: E402
from graphlayout import layout  # noqa: E402

W, H, M = 1000.0, 700.0, 40.0
MIN_DIST = 24.0            # a hub (radius 18) and a leaf (radius 6) just do not touch
EDGE_MAX = 150.0           # the ideal edge on the page is at most ~140 units


# ---- graphs ---------------------------------------------------------------------------------------------------------

def star(k, hub="hub"):
    nodes = [hub] + ["leaf%d" % i for i in range(k)]
    return nodes, [(hub, "leaf%d" % i) for i in range(k)]


def chain(k):
    return ["c%d" % i for i in range(k)], [("c%d" % i, "c%d" % (i + 1)) for i in range(k - 1)]


def hubs(ports=12, containers=20, databases=6):
    """LAN and INTERNET -> ports -> containers -> databases, as the MAP is shaped (a few ports are reached from both)."""
    nodes, edges = ["LAN", "INTERNET"], []
    for i in range(ports):
        nodes.append("port%d" % i)
        edges.append(("LAN" if i % 3 else "INTERNET", "port%d" % i))
        if i % 4 == 0:
            edges.append(("INTERNET" if i % 3 else "LAN", "port%d" % i))
    for i in range(containers):
        nodes.append("cont%d" % i)
        edges.append(("port%d" % (i % ports), "cont%d" % i))
    for i in range(databases):
        nodes.append("db%d" % i)
        edges.append(("cont%d" % (i * 3), "db%d" % i))
        edges.append(("cont%d" % (i * 3 + 1), "db%d" % i))
    return nodes, edges


def random_graph(n, extra, seed, connected=True):
    """A seeded random tree of n nodes plus `extra` random edges (a forest of ~n/3 trees when not connected)."""
    rnd = random.Random(seed)
    nodes = ["n%d" % i for i in range(n)]
    edges = set()
    for i in range(1, n):
        if connected or i % 3:
            edges.add((nodes[rnd.randrange(i)], nodes[i]))
    while len(edges) < n - 1 + extra:
        a, b = rnd.sample(nodes, 2)
        edges.add((a, b))
    return nodes, sorted(edges)


def components(sizes, seed):
    """Trees of the given sizes, side by side in one graph -> nodes, edges, [node ids of each tree]."""
    rnd = random.Random(seed)
    nodes, edges, groups = [], [], []
    for c, k in enumerate(sizes):
        ids = ["g%d_%d" % (c, i) for i in range(k)]
        nodes += ids
        groups.append(ids)
        edges += [(ids[rnd.randrange(i)], ids[i]) for i in range(1, k)]
    return nodes, edges, groups


def battery():
    """Graphs up to 150 nodes of every shape the MAP has (name, nodes, edges)."""
    out = [("star5", star(5)), ("star30", star(30)), ("star100", star(100)), ("star149", star(149)),
           ("chain3", chain(3)), ("chain40", chain(40)), ("chain120", chain(120)),
           ("hubs", hubs()), ("hubs-big", hubs(30, 60, 20)),
           ("pairs", ([x for i in range(30) for x in ("p%da" % i, "p%db" % i)], [("p%da" % i, "p%db" % i) for i in range(30)])),
           ("isolated", (["i%d" % i for i in range(60)], []))]
    for n, extra, connected in ((10, 2, True), (40, 0, True), (40, 20, True), (60, 0, False), (60, 20, True), (60, 240, True),
                                (100, 20, False), (150, 50, True), (150, 250, True), (150, 0, False)):
        for seed in (1, 2, 3):
            out.append(("random%d+%d%s/%d" % (n, extra, "" if connected else "f", seed), random_graph(n, extra, seed, connected)))
    nodes, edges, _ = components([30, 20, 12, 8, 5, 3, 2, 2, 1, 1], 4)
    out.append(("components", (nodes, edges)))
    return [(name, g[0], g[1]) for name, g in out]


# ---- measures -------------------------------------------------------------------------------------------------------

def dist(pos, a, b):
    return math.hypot(pos[a][0] - pos[b][0], pos[a][1] - pos[b][1])


def min_dist(pos):
    return min(dist(pos, a, b) for a, b in itertools.combinations(pos, 2))


def extent(pos, ids=None):
    xs = [pos[i][0] for i in (ids or pos)]
    ys = [pos[i][1] for i in (ids or pos)]
    return min(xs), min(ys), max(xs), max(ys)


def edge_vs_pair_ratio(pos, edges):
    """Mean length of the edges over the mean distance of the pairs of nodes that are not joined."""
    joined = set(frozenset(e) for e in edges)
    on = [dist(pos, a, b) for a, b in edges]
    off = [dist(pos, a, b) for a, b in itertools.combinations(pos, 2) if frozenset((a, b)) not in joined]
    return statistics.mean(on) / statistics.mean(off)


def median_move(before, after):
    return statistics.median(dist2(before[k], after[k]) for k in after if k in before)


def dist2(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


class LayoutTest(unittest.TestCase):
    def assertInBox(self, pos, w=W, h=H, m=M, label=""):
        for nid, (x, y) in pos.items():
            self.assertTrue(math.isfinite(x) and math.isfinite(y), "%s: %r is not finite" % (label, nid))
            self.assertTrue(m <= x <= w - m and m <= y <= h - m,
                            "%s: %r at %.1f,%.1f is outside the margins of %gx%g" % (label, nid, x, y, w, h))


class EdgeCases(LayoutTest):
    def test_nothing(self):
        self.assertEqual(layout([], []), {})
        self.assertEqual(layout([], [("a", "b")]), {})
        self.assertEqual(layout(iter(()), iter(())), {})

    def test_one_node_is_the_centre(self):
        self.assertEqual(layout(["a"], []), {"a": (W / 2, H / 2)})
        self.assertEqual(layout(["a"], [("a", "a"), ("a", "zzz")], 400, 300, 10), {"a": (200.0, 150.0)})

    def test_two_nodes_are_symmetric_around_the_centre(self):
        for edges in ([], [("a", "b")]):
            for w, h, m in ((W, H, M), (400.0, 900.0, 40.0), (300.0, 300.0, 100.0)):
                pos = layout(["a", "b"], edges, w, h, m)
                (ax, ay), (bx, by) = pos["a"], pos["b"]
                self.assertAlmostEqual((ax + bx) / 2, w / 2, places=6)
                self.assertAlmostEqual((ay + by) / 2, h / 2, places=6)
                self.assertGreater(dist(pos, "a", "b"), 20.0)
                self.assertLessEqual(dist(pos, "a", "b"), EDGE_MAX)
                self.assertInBox(pos, w, h, m)

    def test_duplicate_nodes_count_once(self):
        nodes, edges = hubs()
        self.assertEqual(layout(nodes + nodes[::-1] + ["LAN"], edges), layout(nodes, edges))

    def test_self_loops_unknown_ids_and_duplicate_edges_are_skipped(self):
        nodes, edges = hubs()
        noisy = edges + [(a, a) for a in nodes[:5]] + [("LAN", "nobody"), ("ghost", "port1"), ("x", "y")]
        noisy += edges[:7] + [(b, a) for a, b in edges[3:11]]
        self.assertEqual(layout(nodes, noisy), layout(nodes, edges))

    def test_edge_direction_is_ignored(self):
        nodes, edges = random_graph(30, 10, 5)
        self.assertEqual(layout(nodes, [(b, a) for a, b in edges]), layout(nodes, edges))

    def test_any_iterable(self):
        nodes, edges = hubs()
        self.assertEqual(layout((n for n in nodes), (e for e in edges)), layout(nodes, edges))
        self.assertEqual(layout(set(nodes), frozenset(edges)), layout(nodes, edges))
        self.assertEqual(layout(tuple(nodes), [list(e) for e in edges]), layout(nodes, edges))

    def test_unusual_ids(self):
        nodes = ["", " ", "a", "A", "café", "☃", "x" * 500, "line\nbreak", "lone\ud800surrogate", "0", "00"]
        edges = list(zip(nodes, nodes[1:]))
        pos = layout(nodes, edges)
        self.assertEqual(sorted(pos), sorted(nodes))
        self.assertInBox(pos)
        self.assertGreaterEqual(min_dist(pos), MIN_DIST)

    def test_no_room_means_the_centre(self):
        pos = layout(["a", "b", "c"], [("a", "b"), ("b", "c")], 100.0, 100.0, 50.0)
        self.assertEqual(set(pos.values()), {(50.0, 50.0)})
        pos = layout(["a", "b", "c"], [("a", "b")], 100.0, 100.0, 80.0)      # a negative room is none
        self.assertEqual(set(pos.values()), {(50.0, 50.0)})

    def test_no_margin(self):
        nodes, edges = hubs()
        pos = layout(nodes, edges, 500.0, 500.0, 0.0)
        self.assertInBox(pos, 500.0, 500.0, 0.0)
        self.assertGreater(extent(pos)[2] - extent(pos)[0], 400.0)

    def test_isolated_nodes_spread_out(self):
        nodes = ["i%d" % i for i in range(40)]
        pos = layout(nodes, [])
        self.assertInBox(pos)
        self.assertGreaterEqual(min_dist(pos), MIN_DIST)


class Contract(LayoutTest):
    def test_every_point_is_inside_the_margins(self):
        boxes = ((W, H, M), (400.0, 900.0, 40.0), (1600.0, 500.0, 40.0), (300.0, 300.0, 40.0), (W, H, 0.0), (200.0, 200.0, 90.0),
                 (W, H, 300.0), (80.0, 700.0, 40.0))
        for name, nodes, edges in battery()[::4]:
            for w, h, m in boxes:
                pos = layout(nodes, edges, w, h, m)
                self.assertEqual(set(pos), set(nodes), name)
                self.assertInBox(pos, w, h, m, "%s %gx%g" % (name, w, h))
                for p in pos.values():
                    self.assertIsInstance(p, tuple)
                    self.assertEqual(len(p), 2)

    def test_same_input_same_output_bit_for_bit(self):
        for name, nodes, edges in battery()[2::4]:
            self.assertEqual(layout(nodes, edges), layout(nodes, edges), name)

    def test_the_order_of_nodes_and_edges_does_not_matter(self):
        rnd = random.Random(7)
        for name, nodes, edges in battery()[1::3]:
            want = layout(nodes, edges)
            for _ in range(2):
                n2, e2 = list(nodes), [(b, a) if rnd.random() < 0.5 else (a, b) for a, b in edges]
                rnd.shuffle(n2)
                rnd.shuffle(e2)
                self.assertEqual(layout(n2, e2), want, name)

    def test_no_dependence_on_the_hash_seed_or_the_process(self):
        code = ("import sys; sys.path.insert(0, %r); import graphlayout; "
                "nodes = ['n%%d' %% i for i in range(150)] + ['LAN', 'caf\\u00e9']; "
                "edges = [(nodes[i], nodes[(i * 7 + 1) %% 150]) for i in range(150)] + [('LAN', nodes[i]) for i in range(0, 150, 9)]; "
                "pos = graphlayout.layout(nodes, edges); print(sorted(pos.items()))") % os.path.join(os.path.dirname(__file__), "..", "src")
        outs = set()
        for seed in ("0", "1", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            r = subprocess.run([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            outs.add(r.stdout)
        self.assertEqual(len(outs), 1)

    def test_the_global_random_state_is_left_alone(self):
        random.seed(99)
        state = random.getstate()
        layout(*hubs())
        self.assertEqual(random.getstate(), state)

    def test_the_inputs_are_not_modified(self):
        nodes, edges = hubs()
        n2, e2 = list(nodes), list(edges)
        layout(nodes, edges)
        self.assertEqual((nodes, edges), (n2, e2))

    def test_the_result_is_a_fresh_dict_of_plain_floats(self):
        pos = layout(*hubs())
        for x, y in pos.values():
            self.assertIs(type(x), float)
            self.assertIs(type(y), float)
        self.assertIsNot(layout(*hubs()), pos)


class Stability(LayoutTest):
    """A node keeps roughly its place when others come and go (its starting point is a hash of its id)."""

    def moves(self, nodes, edges, leaf):
        """Median displacement of the others when `leaf` is taken away, and when a newcomer is joined to it."""
        pos = layout(nodes, edges)
        without = layout([x for x in nodes if x != leaf], [e for e in edges if leaf not in e])
        self.assertEqual(set(without), set(nodes) - {leaf})
        extra = layout(nodes + ["newcomer"], edges + [(leaf, "newcomer")])
        self.assertEqual(set(extra), set(nodes) | {"newcomer"})
        return median_move(pos, without), median_move(pos, extra)

    def leaf_of(self, nodes, edges, k):
        degree = {x: sum(x in e for e in edges) for x in nodes}
        leaves = [x for x in nodes if degree[x] == 1]
        self.assertTrue(leaves)
        return leaves[k % len(leaves)]

    def test_removing_one_leaf_of_a_40_node_graph(self):
        for extra, seeds in ((0, (1, 2, 3, 5, 6, 8, 9, 10)), (5, (2, 5, 6, 8, 10))):
            for seed in seeds:
                nodes, edges = random_graph(40, extra, seed)
                for move in self.moves(nodes, edges, self.leaf_of(nodes, edges, seed)):
                    self.assertLess(move, 0.10 * W, "40 nodes, %d loops, seed %d" % (extra, seed))

    def test_removing_one_leaf_of_the_hub_graph(self):
        """The shape of the MAP: a few cycles through the hubs, so the layout can settle in another way; less steady."""
        nodes, edges = hubs(8, 20, 4)
        self.assertEqual(len(nodes), 34)
        moves = [m for drop in ("cont19", "cont7", "db3", "db0", "cont14", "cont11") for m in self.moves(nodes, edges, drop)]
        self.assertLess(statistics.median(moves), 0.07 * W)
        self.assertLess(max(moves), 0.15 * W)

    def test_an_isolated_newcomer_leaves_the_others_where_they_are(self):
        nodes, edges = hubs()
        self.assertLess(median_move(layout(nodes, edges), layout(nodes + ["newcomer"], edges)), 0.10 * W)


class Quality(LayoutTest):
    def test_nodes_never_sit_closer_than_the_circles_need(self):
        for name, nodes, edges in battery():
            pos = layout(nodes, edges)
            self.assertGreaterEqual(min_dist(pos), MIN_DIST, name)

    def test_nodes_are_spread_in_a_crowded_box(self):
        nodes, edges = random_graph(150, 100, 3)
        pos = layout(nodes, edges, 600.0, 450.0, 30.0)
        self.assertGreaterEqual(min_dist(pos), 15.0)

    def test_joined_nodes_are_closer_than_unjoined_ones(self):
        for seed in (1, 2, 3, 4):
            for n, extra, connected in ((40, 0, True), (40, 20, True), (60, 0, False), (100, 20, False)):
                nodes, edges = random_graph(n, extra, seed, connected)
                ratio = edge_vs_pair_ratio(layout(nodes, edges), edges)
                self.assertLess(ratio, 0.6, "%d+%d seed %d" % (n, extra, seed))
        nodes, edges = random_graph(60, 240, 1)           # a dense graph has less to show
        self.assertLess(edge_vs_pair_ratio(layout(nodes, edges), edges), 0.85)
        for nodes, edges in (chain(40), hubs(), hubs(30, 60, 20)):
            self.assertLess(edge_vs_pair_ratio(layout(nodes, edges), edges), 0.6)
        nodes, edges = star(30)                            # all the edges meet in one node: every leaf is a ring away from it
        self.assertLess(edge_vs_pair_ratio(layout(nodes, edges), edges), 0.85)

    def test_a_chain_is_laid_along_itself(self):
        nodes, edges = chain(40)
        pos = layout(nodes, edges)
        self.assertEqual(len(set(pos.values())), 40)
        for a, b in edges:
            self.assertLess(dist(pos, a, b), 4 * statistics.mean(dist(pos, a, b) for a, b in edges))
        self.assertLess(dist(pos, "c0", "c1"), dist(pos, "c0", "c39"))
        self.assertLess(dist(pos, "c0", "c1") * 2, dist(pos, "c0", "c20"))

    def test_the_leaves_of_a_hub_are_not_crushed(self):
        for k in (5, 30, 100):
            nodes, edges = star(k)
            pos = layout(nodes, edges)
            near = min(dist(pos, "hub", "leaf%d" % i) for i in range(k))
            self.assertGreaterEqual(near, MIN_DIST * 1.5, "star %d" % k)

    def test_the_hubs_of_the_map_get_room_for_their_neighbours(self):
        for nodes, edges in (hubs(), hubs(30, 60, 20)):
            pos = layout(nodes, edges)
            hub_edges = [dist(pos, a, b) for a, b in edges if a in ("LAN", "INTERNET")]
            other = [dist(pos, a, b) for a, b in edges if a not in ("LAN", "INTERNET")]
            self.assertGreater(statistics.mean(hub_edges), 1.3 * statistics.mean(other))
            near = min(dist(pos, h, x) for h in ("LAN", "INTERNET") for x in nodes if x != h)
            self.assertGreaterEqual(near, 30.0)

    def test_a_busier_hub_wants_longer_edges(self):
        """In a graph of one big and one small star, both too big to be capped, the big star's spokes are the longer."""
        nodes = ["big", "small"] + ["b%d" % i for i in range(60)] + ["s%d" % i for i in range(6)]
        edges = [("big", "b%d" % i) for i in range(60)] + [("small", "s%d" % i) for i in range(6)] + [("big", "small")]
        pos = layout(nodes, edges)
        big = statistics.mean(dist(pos, "big", "b%d" % i) for i in range(60))
        small = statistics.mean(dist(pos, "small", "s%d" % i) for i in range(6))
        self.assertGreater(big, small)

    def test_disconnected_components_do_not_overlap(self):
        for seed in range(1, 6):
            nodes, edges, groups = components([5 + (seed * 7 + i * 5) % 15 for i in range(5)] + [1, 1, 2], seed)
            pos = layout(nodes, edges)
            self.assertInBox(pos)
            boxes = [extent(pos, g) for g in groups]
            for (a, b), (c, d) in itertools.combinations(zip(groups, boxes), 2):
                wide = min(b[2], d[2]) - max(b[0], d[0])
                high = min(b[3], d[3]) - max(b[1], d[1])
                self.assertFalse(wide > 5.0 and high > 5.0, "seed %d: %s... and %s... overlap by %.0fx%.0f" % (seed, a[0], c[0], wide, high))

    def test_components_stay_together(self):
        nodes, edges, groups = components([40, 25, 12, 8, 5], 3)
        pos = layout(nodes, edges)
        centre = [(statistics.mean(pos[i][0] for i in g), statistics.mean(pos[i][1] for i in g)) for g in groups]
        for g, c in zip(groups, centre):
            for i in g:
                mine = dist2(pos[i], c)
                others = min(dist2(pos[i], o) for o in centre)
                self.assertLessEqual(others, mine + 1e-9)
                self.assertLess(mine, 480.0)
        # and the page is shared: the big one does not take all of it
        big = extent(pos, groups[0])
        self.assertLess((big[2] - big[0]) * (big[3] - big[1]), 0.8 * (W - 2 * M) * (H - 2 * M))

    def test_many_small_components(self):
        nodes = ["p%d_%d" % (i, j) for i in range(100) for j in (0, 1)] + ["i%d" % i for i in range(60)] + ["hub"] + ["l%d" % i for i in range(20)]
        edges = [("p%d_0" % i, "p%d_1" % i) for i in range(100)] + [("hub", "l%d" % i) for i in range(20)]
        pos = layout(nodes, edges)
        self.assertInBox(pos)
        self.assertGreaterEqual(min_dist(pos), 15.0)
        self.assertLess(statistics.mean(dist(pos, a, b) for a, b in edges[:100]), 60.0)

    def test_a_line_of_three_is_not_blown_up_into_a_diagonal(self):
        pos = layout(["a", "b", "c"], [("a", "b"), ("b", "c")])
        x0, y0, x1, y1 = extent(pos)
        self.assertLessEqual(max(x1 - x0, y1 - y0), 2 * EDGE_MAX)
        self.assertLessEqual(dist(pos, "a", "b"), EDGE_MAX)
        self.assertLessEqual(dist(pos, "b", "c"), EDGE_MAX)
        self.assertGreater(dist(pos, "a", "c"), 1.5 * dist(pos, "a", "b") * 0.99 - 1)      # a line, not a triangle
        self.assertAlmostEqual((x0 + x1) / 2, W / 2, places=6)
        self.assertAlmostEqual((y0 + y1) / 2, H / 2, places=6)

    def test_small_graphs_are_centred_not_blown_up(self):
        for nodes, edges in (star(5), chain(4), hubs(2, 2, 0), random_graph(8, 1, 1), (["a", "b", "c"], [])):
            pos = layout(nodes, edges)
            x0, y0, x1, y1 = extent(pos)
            self.assertAlmostEqual((x0 + x1) / 2, W / 2, places=6)
            self.assertAlmostEqual((y0 + y1) / 2, H / 2, places=6)
            self.assertLess(x1 - x0, 0.7 * (W - 2 * M))
            self.assertLess(y1 - y0, 0.9 * (H - 2 * M))
            if edges:
                self.assertLessEqual(statistics.mean(dist(pos, a, b) for a, b in edges), EDGE_MAX)

    def test_a_big_graph_uses_the_whole_box(self):
        for name, nodes, edges in battery():
            if len(nodes) < 40 or name.startswith(("pairs", "isolated", "star")):
                continue
            x0, y0, x1, y1 = extent(layout(nodes, edges))
            fill_x, fill_y = (x1 - x0) / (W - 2 * M), (y1 - y0) / (H - 2 * M)
            self.assertGreater(max(fill_x, fill_y), 0.5 if "+240" in name else 0.99, name)   # (a dense graph hits the edge cap)
            self.assertGreater(min(fill_x, fill_y), 0.5, name)
            self.assertAlmostEqual((x0 + x1) / 2, W / 2, delta=1.0)
            self.assertAlmostEqual((y0 + y1) / 2, H / 2, delta=1.0)

    def test_the_layout_follows_the_shape_of_the_page(self):
        nodes, edges = random_graph(60, 20, 3)
        wide = extent(layout(nodes, edges, 1600.0, 500.0, 40.0))
        tall = extent(layout(nodes, edges, 400.0, 900.0, 40.0))
        self.assertGreater((wide[2] - wide[0]) / (wide[3] - wide[1]), 1.2)
        self.assertGreater((tall[3] - tall[1]) / (tall[2] - tall[0]), 1.2)

    def test_a_graph_with_two_hubs_is_not_a_hairball_at_the_centre(self):
        """The nodes use the page: the middle of the box is not where everything is."""
        nodes, edges = hubs(30, 60, 20)
        pos = layout(nodes, edges)
        out = sum(1 for p in pos.values() if dist2(p, (W / 2, H / 2)) > 150.0)
        self.assertGreater(out, 0.7 * len(nodes))


class Performance(LayoutTest):
    """Bounds are generous (slow CI runners); the layout takes ~20 ms for 60 nodes, ~170 ms for 300 nodes on a laptop."""

    def timed(self, nodes, edges, limit, label, runs=2):
        best = None
        for _ in range(runs):
            t = time.perf_counter()
            pos = layout(nodes, edges)
            took = time.perf_counter() - t
            best = took if best is None else min(best, took)
        self.assertLess(best, limit, "%s took %.2fs" % (label, best))
        self.assertEqual(len(pos), len(set(nodes)))
        self.assertInBox(pos, label=label)
        return pos

    def test_60_nodes(self):
        self.timed(*random_graph(60, 30, 1), limit=1.0, label="60 nodes")

    def test_150_nodes(self):
        self.timed(*random_graph(150, 70, 1), limit=2.0, label="150 nodes")

    def test_300_nodes_600_edges(self):
        self.timed(*random_graph(300, 301, 1), limit=3.0, label="300 nodes")

    def test_600_nodes(self):
        self.timed(*random_graph(600, 400, 1), limit=4.0, label="600 nodes", runs=1)

    def test_1000_nodes(self):
        self.timed(*random_graph(1000, 500, 1), limit=6.0, label="1000 nodes", runs=1)

    def test_a_big_star_and_a_dense_graph(self):
        self.timed(*star(500), limit=4.0, label="star of 500", runs=1)
        self.timed(*random_graph(300, 2700, 2), limit=4.0, label="300 nodes, 3000 edges", runs=1)

    def test_many_tiny_components(self):
        nodes = ["p%d_%d" % (i, j) for i in range(200) for j in (0, 1)] + ["i%d" % i for i in range(300)]
        edges = [("p%d_0" % i, "p%d_1" % i) for i in range(200)]
        self.timed(nodes, edges, limit=3.0, label="200 pairs, 300 isolated")


class Internals(unittest.TestCase):
    def test_the_grid_repulsion_matches_the_exact_one(self):
        rnd = random.Random(3)
        P = [complex(rnd.gauss(0, 8), rnd.gauss(0, 6)) for _ in range(300)]
        exact = graphlayout._repel_exact(P)
        grid = graphlayout._repel_grid(P)
        err = [abs(a - b) / max(abs(a), 1e-9) for a, b in zip(exact, grid)]
        self.assertLess(statistics.median(err), 0.05)
        self.assertLess(max(abs(a - b) for a, b in zip(exact, grid)), 0.5 * max(abs(a) for a in exact))

    def test_nodes_on_the_same_point_are_pushed_apart(self):
        for f in (graphlayout._repel_exact, graphlayout._repel_grid):
            P = [0j, 0j, 0j, 1 + 1j]
            out = f(P)
            self.assertTrue(all(math.isfinite(z.real) and math.isfinite(z.imag) for z in out))
            self.assertEqual(len(set(out[:3])), 3)

    def test_nodes_on_the_same_point_end_up_apart(self):
        P = [0j] * 8 + [3 + 3j]
        graphlayout._separate(P, 1.0, 30)
        self.assertGreaterEqual(min(abs(a - b) for a, b in itertools.combinations(P, 2)), 0.9)


if __name__ == "__main__":
    unittest.main()
