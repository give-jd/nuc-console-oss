#!/usr/bin/env python3
"""Render benchmark: the CPU time a frame costs, per view and size. Stdlib only.

Each view is drawn many times inside tests/golden.py's FrozenWorld (the demo, a fixed clock and a fake host: the same work on every run and every
machine), and the table says how many milliseconds of CPU (time.process_time) one frame took. The console redraws every second or two on a
box that is meant to idle, so this is the budget a refactor must not eat: compare a run with a baseline and it fails when a view got more than
1.2 times slower.

    python3 tools/bench_render.py                       # the table
    python3 tools/bench_render.py --json > before.json  # a baseline (do it on the commit before your change, on the same machine)
    python3 tools/bench_render.py --compare before.json # the ratios; exit status 1 if any is above --threshold (default 1.2)
    python3 tools/bench_render.py --only overview-200x50,web-dashboard --rounds 9

A figure is the minimum over --rounds of (CPU of a batch of frames / frames in it): the noise of a machine only adds time. A batch is long
enough (about 0.1 s) for the clock of the OS (Windows ticks every 15 ms) to measure it, and the first one is thrown away. What stays warm, as
it does in the real loop: the Health report and the AI catalog (cached for a minute and for 10 s). What is emptied before every web page: the
page cache and the graph layouts (a refresh with a new link has neither). Compare runs on one machine, not across machines or Pythons.
"""
import argparse
import gc
import importlib.util
import json
import os
import sys
import time
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH_S = 0.1        # the CPU a batch of frames should take, at least
MIN_FRAMES, MAX_FRAMES = 3, 400


def load_golden():
    """tests/golden.py, imported by its path (tools/ and tests/ are no packages): it puts src/ on the path and builds the demo's world."""
    spec = importlib.util.spec_from_file_location("golden", os.path.join(ROOT, "tests", "golden.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["golden"] = mod
    spec.loader.exec_module(mod)
    return mod


def size(w, h):
    return ["--cols", str(w), "--rows", str(h)]


# name -> ("console", the arguments of `render.py --once --demo --color`) or ("web", the query string of the page)
VIEWS = tuple(
    [(f"overview-{w}x{h}", ("console", size(w, h))) for w, h in ((79, 24), (120, 33), (200, 50), (226, 50))]
    + [("map", ("console", ["--view", "map", "--expand", "fit", "--select", "shop-api", "--details"] + size(200, 50))),
       ("cpu", ("console", ["--view", "cpu", "--select", "node", "--details"] + size(200, 50))),
       ("health", ("console", ["--view", "health", "--select", "mem-leak:node", "--details"] + size(200, 50))),
       ("ai", ("console", ["--view", "ai", "--select", "qwen3-8b", "--details"] + size(200, 50))),
       ("web-dashboard", ("web", "")),
       ("web-cpu", ("web", "view=cpu")),
       ("web-map-tree", ("web", "view=map&all=1")),
       ("web-map-graph", ("web", "view=map&as=graph")),
       ("web-health", ("web", "view=health")),
       ("web-ai", ("web", "view=ai"))])


def frame_fn(world, kind, what):
    """() -> one frame, drawn inside `world`."""
    if kind == "console":
        return lambda: world.once(what)
    srv, args = world.server, sys.modules["web"].view_params(parse_qs(what))

    def page():
        srv.cache.clear()                       # a page is built, not remembered
        getattr(srv, "layouts", {}).clear()     # the graph's layout is computed, not remembered
        return srv.page(**args)
    return page


def measure(fn, rounds):
    """(ms of CPU per frame: the fastest of `rounds` batches, frames in a batch). The noise of a machine only adds time, so the minimum is the steadiest figure."""
    t = time.perf_counter()
    fn()                                         # the first frame: imports, caches, and how long a frame takes
    n = max(MIN_FRAMES, min(MAX_FRAMES, int(BATCH_S / max(time.perf_counter() - t, 1e-4)) + 1))
    cost = []
    for _ in range(rounds + 1):                  # the first batch is thrown away
        gc.collect()
        t = time.process_time()
        for _ in range(n):
            fn()
        cost.append((time.process_time() - t) * 1000.0 / n)
    return min(cost[1:]), n


def run(golden, names, rounds, out):
    got = {}
    for name, (kind, what) in VIEWS:
        if names and name not in names:
            continue
        with golden.FrozenWorld() as world:
            ms, n = measure(frame_fn(world, kind, what), rounds)
        got[name] = {"ms": round(ms, 3), "frames": n}
        print(f"{name:<16}{ms:9.2f} ms/frame   ({n} frames a batch)", file=out, flush=True)
    return got


def compare(new, old, threshold, out, partial=False):
    """Prints the ratios now / before; 1 if some view is more than `threshold` times slower, else 0. A view only one of the runs has is
    named and left out of the verdict (partial: this run was asked for some views only, the others of the baseline are not mentioned)."""
    slow, width = [], max([len(n) for n in list(new) + list(old)] + [4])
    cell = lambda d, n: f"{d[n]['ms']:9.2f}" if n in d else f"{'-':>9}"  # noqa: E731
    print(f"{'view':<{width}}  {'before':>9}  {'now':>9}  ratio", file=out)
    for name in list(new) + ([] if partial else [n for n in old if n not in new]):
        if name not in old or name not in new:
            print(f"{name:<{width}}  {cell(old, name)}  {cell(new, name)}  " + ("(not in the baseline)" if name not in old else "(not measured now)"), file=out)
            continue
        before, now = old[name]["ms"], new[name]["ms"]
        ratio = now / before if before > 0 else 1.0
        print(f"{name:<{width}}  {before:9.2f}  {now:9.2f}  {ratio:5.2f}" + (f"  <-- more than {threshold:g}x" if ratio > threshold else ""), file=out)
        if ratio > threshold:
            slow.append(name)
    print(f"slower than {threshold:g}x: " + (", ".join(slow) if slow else "none"), file=out)
    return 1 if slow else 0


def main(argv):
    ap = argparse.ArgumentParser(description="CPU milliseconds per rendered frame, per view (the demo, in tests/golden.py's frozen world).")
    ap.add_argument("--json", action="store_true", help="print the figures as JSON on stdout (the table goes to stderr)")
    ap.add_argument("--compare", metavar="OLD.json", help="compare with a baseline made by --json: exit status 1 if a view is slower than --threshold")
    ap.add_argument("--threshold", type=float, default=1.2, help="the ratio now / before above which --compare fails (default 1.2)")
    ap.add_argument("--rounds", type=int, default=5, help="batches per view; the figure is the fastest (default 5)")
    ap.add_argument("--only", help="comma-separated views (default all: %s)" % ", ".join(n for n, _ in VIEWS))
    args = ap.parse_args(argv)
    names = [x for x in (args.only or "").split(",") if x]
    unknown = [x for x in names if x not in dict(VIEWS)]
    if unknown:
        ap.error("unknown view: " + ", ".join(unknown))
    out, rounds = (sys.stderr if args.json else sys.stdout), max(1, args.rounds)
    got = run(load_golden(), names, rounds, out)
    if args.json:
        print(json.dumps({"python": sys.version.split()[0], "platform": sys.platform, "rounds": rounds, "views": got}, indent=2, sort_keys=True))
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            old = json.load(f)["views"]
        print("", file=out)
        return compare(got, old, args.threshold, out, partial=bool(names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
