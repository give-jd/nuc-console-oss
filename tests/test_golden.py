"""Golden outputs: every screen of the console and every page of the web view, byte for byte, as tests/golden.py renders them in demo mode
in a frozen world (a clock that stands still, one host name, the default configuration, a host that always reads the same).

A refactor of the renderer that is meant to change nothing must leave these files alone. One that changes what the user sees regenerates
them, and the diff of tests/golden/ in the pull request is the review of that change:

    NUC_GOLDEN_UPDATE=1 python3 -m unittest tests.test_golden      # rewrites the files that differ, deletes those no case owns
    git diff --stat tests/golden                                    # then look at it
"""
import copy
import os
import socket
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # `python3 -m unittest tests.test_golden` from the root finds golden.py too
import golden  # noqa: E402
import hostdata  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402

UPDATE = os.environ.get("NUC_GOLDEN_UPDATE", "") not in ("", "0")
HINT = "\n\nIf the change is intended: NUC_GOLDEN_UPDATE=1 python3 -m unittest tests.test_golden  (then review the diff of tests/golden/ in git)"


class Golden(unittest.TestCase):
    """One test per case (a generated method: test_<name>)."""


def _make(case):
    def test(self):
        got, want = case.render(), golden.read(case)
        if got == want:
            return
        if UPDATE:
            golden.write(case, got)
            print("golden: wrote " + case.filename, file=sys.stderr)
            return
        if want is None:
            self.fail(f"no golden file tests/golden/{case.filename}" + HINT)
        self.fail(f"{case.filename} is not what the renderer writes now:\n{golden.diff(want, got, case.filename)}{HINT}")
    test.__doc__ = f"tests/golden/{case.filename}: " + " ".join(case.args or (case.query or "",))
    return test


for _case in golden.CASES:
    assert not hasattr(Golden, "test_" + _case.name.replace("-", "_")), _case.name  # two names that differ only by '-' and '_'
    setattr(Golden, "test_" + _case.name.replace("-", "_"), _make(_case))


class Files(unittest.TestCase):
    def test_every_file_belongs_to_a_case_and_the_other_way_round(self):
        have = {n for n in os.listdir(golden.GOLDEN_DIR) if not n.startswith(".")}  # (a Mac's .DS_Store is not a golden file)
        want = {c.filename for c in golden.CASES}
        if UPDATE:
            for name in sorted(have - want):
                os.remove(os.path.join(golden.GOLDEN_DIR, name))
                print("golden: deleted " + name, file=sys.stderr)
            return
        self.assertEqual(sorted(have - want), [], "files no case writes: delete them (NUC_GOLDEN_UPDATE=1 does)")
        self.assertEqual(sorted(want - have), [], "cases without a file" + HINT)

    def test_the_frames_keep_their_line_ends(self):
        """The lines of a frame end in ESC[K CR LF (so do those of the dashboard page, inside its <pre>); a checkout must not turn them
        into LF (.gitattributes: tests/golden/** -text)."""
        for case in golden.CASES:
            with open(case.path, "rb") as f:
                raw = f.read()
            if case.query is None:
                self.assertIn(b"\x1b[K\r\n", raw, case.name)
                self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n") + 1, case.name)  # every line end but the last (a bare LF) is CR LF
            elif case.name.startswith("web-dashboard"):
                self.assertIn(b"\r\n", raw, case.name)

    def test_line_endings_are_pinned(self):
        with open(os.path.join(os.path.dirname(golden.HERE), ".gitattributes"), encoding="utf-8") as f:
            self.assertIn("tests/golden/** -text", f.read().splitlines())

    def test_what_is_locked(self):
        """The matrix of the work plan: a case cannot be dropped without this list saying so."""
        names = {c.name for c in golden.CASES}
        for w, h in golden.SIZES:
            for spacing in (0, 1):
                self.assertIn(f"overview-{w}x{h}-spacing{spacing}", names)
        for name in ("overview-windows-200x50", "overview-darwin-200x50", "web-dashboard", "web-dashboard-full", "web-cpu", "web-map-tree",
                     "web-map-graph", "web-health", "web-ai", "rotate-120x33-system", "rotate-226x50-boot", "details-79x24-1",
                     "map-120x33", "cpu-sort-mem-200x50", "health-none-200x50", "ai-details-120x33", "slide-health-120x33"):
            self.assertIn(name, names)


class World(unittest.TestCase):
    """FrozenWorld leaves nothing behind: the tests of this repository share one process and one render.CFG."""

    def snapshot(self):
        cfg = render.CFG
        return {"cfg": copy.deepcopy(dict(cfg)), "ids": {k: id(v) for k, v in cfg.items()},
                "attrs": [getattr(render, n) for n in ("MODE", "PAGES", "ROTATE_S", "REFRESH_S",
                                                        "ACCEPT_CMD", "PROBLEMS_CMD", "CMD", "KIOSK_HINT", "ACCEPTED_PATH", "DEMO", "DEMO_OS", "DEMO_HEALTH",
                                                        "time", "telegram_status")],
                "others": [socket.gethostname, os.cpu_count, nuc_config.PORTABLE, golden.demo.time, golden.graph.time, golden.web.time,
                           golden.aiweb.time, golden.aiweb._ENGINE, dict(golden.aiweb._BIND), golden.aisetup.work_dir],
                "caches": [dict(hostdata._CACHE)] + [dict(getattr(render, n)) for n in ("_HEALTH", "_ADVICE", "_AI", "_TOPO")]}

    def test_everything_is_put_back_in_place(self):
        cfg, features = render.CFG, render.CFG["features"]
        before = self.snapshot()
        with golden.FrozenWorld({"spacing": 0, "features": {"map": False}, "map_in_rotation": True}) as world:
            self.assertIs(render.CFG, cfg)                                    # the same dict, filled with the defaults and the changes
            self.assertEqual((render.CFG["spacing"], render.CFG["features"]["map"], render.CFG["map_in_rotation"]), (0, False, True))
            self.assertTrue(render.CFG["features"]["cpu"])                    # the rest of the defaults
            world.once(["--cols", "80", "--rows", "24"])                      # sets DEMO, renames the host, fills the caches
            self.assertTrue(render.DEMO)
            render.CFG["webapps"]["x"] = [1]
            render.CFG["features"]["ai"] = False
            self.assertEqual(world.page("view=health")[:4], "<!--")           # a web.Server, with its socket
        after = self.snapshot()
        self.assertEqual(after, before)
        self.assertIs(render.CFG, cfg)
        self.assertIs(render.CFG["features"], features)                       # what is inside is the same object too
        self.assertEqual(after["ids"], before["ids"])

    def test_put_back_after_an_error(self):
        before = self.snapshot()
        with self.assertRaises(ZeroDivisionError):
            with golden.FrozenWorld({"spacing": 0}):
                1 / 0
        self.assertEqual(self.snapshot(), before)

    def test_the_clock_stands_still_in_utc_and_sleep_moves_it(self):
        with golden.FrozenWorld() as world:
            t = render.time
            self.assertEqual((t.time(), t.strftime("%Y-%m-%d %H:%M:%S")), (golden.NOW, "2026-09-21 14:13:20"))
            self.assertEqual(t.strftime("%H:%M", t.localtime(0)), "00:00")     # local time is UTC, whatever TZ says
            t.sleep(0.5)
            self.assertEqual(t.time(), golden.NOW + 0.5)
            self.assertIs(golden.demo.time, t)                                 # one clock for every module of src/
            self.assertGreater(t.monotonic(), 0)                               # the rest of the module is the real one
            self.assertEqual(world.clock.now, golden.NOW + 0.5)

    def test_the_host_is_not_read(self):
        """Nothing in a frame comes from this machine: under --demo the readings are demo.sampler_data()'s, which only the clock moves."""
        with golden.FrozenWorld() as world:
            self.assertEqual(os.cpu_count(), golden.CPUS)
            self.assertEqual(socket.gethostname(), golden.HOST)
            self.assertEqual(render.ACCEPT_CMD, "sudo nuc-console-accept")
            frame = world.once(["--cols", "120", "--rows", "33"])
            self.assertIn("demo-host", frame)
            self.assertIn("14:13:20", frame)
            self.assertIn("up 5d 0h", frame)                                   # demo.UP_DEMO, not this machine's uptime
            self.assertEqual(golden.demo.sampler_data(None, golden.NOW), golden.demo.sampler_data(None, golden.NOW))

    def test_the_ai_engine_is_the_demo_one_in_a_temporary_folder(self):
        old = golden.aiweb._ENGINE
        with golden.FrozenWorld() as world:
            eng = golden.aiweb.engine()
            self.assertIsNot(eng, old)
            self.assertTrue(eng.demo)
            self.assertTrue(eng.directory.startswith(world._tmp.name))
            self.assertEqual(golden.aisetup.work_dir(), eng.directory)
            self.assertEqual(world.server.csrf, "csrf-token")
        self.assertIs(golden.aiweb._ENGINE, old)

    def test_the_sum_of_floats_is_the_old_one_on_every_python(self):
        with golden.FrozenWorld():
            self.assertEqual(render.sum([0.1] * 10), 0.9999999999999999)       # 3.12+'s builtin gives 1.0
            self.assertEqual(render.sum([1, 2, 3], 10), 16)


class Machine(unittest.TestCase):
    """The same bytes in another process, in another time zone, with other terminal sizes in the environment and another hash seed."""

    def run_all(self, **env):
        e = dict(os.environ, PYTHONIOENCODING="utf-8", **env)
        r = subprocess.run([sys.executable, golden.__file__], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e, encoding="utf-8",
                           errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + HINT)
        self.assertIn(f"{len(golden.CASES)} cases, 0 different", r.stdout)

    @unittest.skipIf(UPDATE, "the files are being rewritten")
    def test_east_and_a_hash_seed(self):
        self.run_all(TZ="Asia/Tokyo", PYTHONHASHSEED="1", COLUMNS="33", LINES="7", LC_ALL="C")

    @unittest.skipIf(UPDATE, "the files are being rewritten")
    def test_west_and_another_hash_seed(self):
        self.run_all(TZ="America/Los_Angeles", PYTHONHASHSEED="31337", COLUMNS="300", LINES="99")


class Helpers(unittest.TestCase):
    def test_a_diff_shows_the_escapes_and_stops_at_forty_lines(self):
        a = "".join(f"\x1b[31mline {i}\x1b[0m\x1b[K\r\n" for i in range(100))
        b = a.replace("line 5\x1b", "line five\x1b").replace("line 50", "x" * 500)
        d = golden.diff(a, b, "t.txt")
        self.assertIn("\\e[31mline five\\e[0m\\e[K\\r", d)
        self.assertNotIn("\x1b", d)
        self.assertIn("(+", d)                                                 # the long line is cut, and says by how much
        self.assertLessEqual(len(d.split("\n")), 41)
        self.assertEqual(golden.diff(a, a, "t.txt"), "")

    def test_a_web_page_is_one_tag_per_line_outside_pre_and_the_token_is_masked(self):
        page = '<p><b>a</b></p><pre><i>x</i><i>y</i>\nz</pre><input type="hidden" name="csrf" value="s3cr3t"><a>q</a>'
        text = golden.web_text(page, "default-src 'none'")
        self.assertEqual(text, "<!-- Content-Security-Policy: default-src 'none' -->\n<p>\n<b>a</b>\n</p>\n<pre><i>x</i><i>y</i>\nz</pre>\n"
                               '<input type="hidden" name="csrf" value="<csrf>">\n<a>q</a>\n')


if __name__ == "__main__":
    unittest.main()
