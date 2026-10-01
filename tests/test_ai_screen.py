"""The console AI screen (render.py): the keys on the models, `--view ai` and its options, every frame inside its screen at the four sizes and
the three demo machines, the verdict pills, the details and the commands, the catalog cache, the probe of the model server, the feature switch,
the footer, the main loop, hostile names, and the advisor's cached advice in the HEALTH screen.

Hermetic: the demo catalogs (src/demo.py) or hand-made ones at a clock that stands still, a fake terminal for the main loop (no TTY), no host
state (no Sampler, no accepted.json, a fixed host name, no aisetup, no network). Every module global a test changes is put back in tearDown.
"""
import contextlib
import io
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import advisor  # noqa: E402
import demo  # noqa: E402
import render  # noqa: E402

NOW = 1_790_000_000
SIZES = ((79, 24), (120, 33), (200, 50), (226, 50))       # console sizes (--cols --rows): the layout gets cols - 1, as in once()
OSES = (None, "windows", "darwin")
ESC, BEL, CSI8 = chr(27), chr(7), chr(0x9b)               # built at runtime: the file itself stays plain text
SGR = re.compile(r"\x1b\[[0-9;]*m")                        # the only escape sequences the renderer puts inside a line
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
LINE = "\x1b[K\r\n"                                        # frame(): every line but the last ends with erase-to-end + CRLF
EVIL = ESC + "[2J" + ESC + "]0;pwn" + BEL + "evil" + CSI8 + "1m"
CJK = "\u30e1\u30e2\u5e33.exe"                             # wide characters would shift every column
FORMAT = "a\u202eb\u200bc\u2028d"                          # right-to-left override, zero width space, line separator
PILLS = {"gpu": "✔ FITS GPU", "partial": "◐ GPU+CPU", "ram": "✔ FITS RAM", "slow": "! SLOW", "no": "✖ TOO BIG"}
RENDER_GLOBALS = ("DEMO", "DEMO_OS", "DEMO_HEALTH", "MODE", "WINDOWS", "ACCEPTED_PATH", "time", "os", "sys", "signal", "shutil", "socket", "termios", "tty",
                  "Sampler", "read_keys", "snapshot", "page_overview", "ai_build", "ai_status", "ai_screen", "ai_probe_run", "health_extra_lines")


class Proxy(object):
    """A module as render (or demo) sees it, with a few attributes replaced: the real module is never touched."""

    def __init__(self, real, **over):
        self._real = real
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Stop(Exception):
    """Ends render.main()'s endless loop from inside a fake keyboard read."""


class FakeSampler(object):
    """No /proc, no hostinfo: what Sampler.sample() returns on a machine with nothing to say."""

    def sample(self):
        return {"cpu": {}, "thermal": {}, "net": {}, "sessions": None, "fs": None}


def overview_stub(*a, **kw):
    return ["   overview page (stub)"]


def model(mid, verdict="gpu", name=None, rank=1, need=5000, size=4400, **kw):
    """One model as aisetup.catalog() hands it over (docs/AI.md): the catalog's fields, assess, installed, pinned, commands."""
    assess = {"verdict": verdict, "where": {"gpu": "GPU", "partial": "GPU+CPU"}.get(verdict, "CPU"), "need_mb": need,
              "gpu_layers": {"gpu": 36, "partial": 12}.get(verdict, 0), "tok_s": None if verdict == "no" else [20, 40],
              "why": "needs %.1f GB: %s" % (need / 1024.0, verdict)}
    m = {"id": mid, "name": name or mid.title(), "license": "Apache-2.0", "params_b": 8.0, "quant": "Q4_K_M", "layers": 36, "ctx_max": 32768, "rank": rank,
         "approx_mb": size, "notes": "a note about " + mid, "assess": assess, "installed": False, "pinned": True,
         "commands": {"install": "sudo nuc-console-ai setup " + mid, "use": "sudo nuc-console-ai use " + mid, "remove": "sudo nuc-console-ai remove " + mid}}
    m.update(kw)
    return m


def catalog(change=None):
    """A catalog with one model of each verdict, an installed and active one, and an unpinned one; change(cat) edits it."""
    models = [model("m-gpu", "gpu", "Alpha GPU", 1, 5000), model("m-part", "partial", "Beta Partial", 2, 12000, 11000, active_b=2.0),
              model("m-ram", "ram", "Gamma RAM", 3, 3000, 2600, installed=True), model("m-slow", "slow", "Delta Slow", 4, 9000, 8000),
              model("m-no", "no", "Epsilon Big", 5, 40000, 38000, pinned=False)]
    cat = {"hw": demo.ai_catalog(None)["hw"], "dir": "/var/lib/nuc-console-ai", "runtime": {"installed": True, "version": "0.10.6"},
           "recommended": "m-gpu", "active": "m-ram", "models": models}
    if change:
        change(cat)
    return cat


class AiCase(unittest.TestCase):
    """The demo machine (or a hand-made catalog) at NOW, the ai feature on, nothing read from the host; all of it undone after."""

    def setUp(self):
        self.saved = {k: getattr(render, k) for k in RENDER_GLOBALS}
        self.saved_time = demo.time
        cfg = render.CFG
        self.saved_cfg = (dict(cfg["features"]), cfg["webapps"], dict(cfg["ai"]))
        self.saved_cache, self.saved_probe, self.saved_health = dict(render._AI), dict(render._AIPROBE), dict(render._HEALTH)
        render._AI.clear()
        render._AIPROBE.update(res=None, at=0.0, key=None, thread=None, started=0.0)
        self.tmp = tempfile.TemporaryDirectory()
        self.now = NOW
        self.on_sleep = self.pass_time
        clock = Proxy(time, time=lambda: self.now, sleep=lambda sec: self.on_sleep(sec),
                      strftime=lambda fmt, t=None: time.strftime(fmt, time.gmtime(self.now) if t is None else t))
        render.time = demo.time = clock
        render.socket = Proxy(render.socket, gethostname=lambda: "test-host")  # demo_defaults() renames it on the proxy only
        render.ACCEPTED_PATH = os.path.join(self.tmp.name, "accepted.json")     # missing: nothing accepted, whatever the host has
        render.DEMO, render.DEMO_OS = True, None
        cfg["features"]["ai"] = True
        self.builds = []                                                        # the (demo, os) of every ai_build(): the catalog cache
        real = render.ai_build
        render.ai_build = lambda now: (self.builds.append((render.DEMO, render.DEMO_OS)), real(now))[1]

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(render, k, v)
        demo.time = self.saved_time
        features, render.CFG["webapps"], ai = self.saved_cfg
        render.CFG["features"].clear()
        render.CFG["features"].update(features)
        render.CFG["ai"].clear()
        render.CFG["ai"].update(ai)
        render._AI.clear()
        render._AI.update(self.saved_cache)
        render._AIPROBE.update(self.saved_probe)
        render._HEALTH.clear()
        render._HEALTH.update(self.saved_health)
        render._ADVICE.clear()
        self.tmp.cleanup()

    def pass_time(self, sec):
        self.now += sec

    def seed(self, cat, status=None):
        """The catalog the screen gets: no build, whatever the demo says (and the status of the server, when given)."""
        render._AI["hit"] = {"cat": cat, "msg": "", "err": False, "at": self.now, "key": (render.DEMO, render.DEMO_OS)}
        if status is not None:
            render.ai_status = lambda wait=0.0: status

    def check_frame(self, screen, w, h, what=""):
        """Exactly h lines, none wider than w once the colours are stripped, and no control character or escape sequence but
        the renderer's own colours and line ends. -> the lines, ANSI stripped."""
        rows = screen.split(LINE)
        self.assertEqual(len(rows), h, what)
        out = [render.ANSI.sub("", x) for x in rows]
        for x in out:
            self.assertLessEqual(len(x), w, (what, x))
        self.assertIsNone(CONTROL.search(SGR.sub("", "".join(rows))), what)
        return out

    def screen(self, opts, cols, rows):
        """`render.py --once --view ai OPTS --cols cols --rows rows`, without printing: (frame, its lines ANSI stripped)."""
        s = render.ai_once(["render.py", "--once", "--view", "ai"] + list(opts), cols - 1, rows)
        return s, self.check_frame(s, cols - 1, rows, (opts, cols, rows, render.DEMO_OS))

    def frame_of(self, cols, rows, av=None):
        """One frame of the seeded catalog, drawn from ai_screen() (no Sampler, no host state, whatever render.DEMO says): its lines, ANSI stripped."""
        s, _ = render.ai_screen(render.ai_data(), [], av or render.AiView(now=NOW), cols - 1, rows)
        return self.check_frame(s, cols - 1, rows)

    def view(self, cat=None, **kw):
        """An AiView and the rows of a catalog (the hand-made one by default)."""
        rows = render.ai_rows(catalog() if cat is None else cat)
        av = render.AiView(now=NOW)
        for k, v in kw.items():
            setattr(av, k, v)
        return av, rows

    def press(self, av, rows, keys):
        """Keys as the console loop hands them to ai_key. -> what ai_key returned for each. After every key the cursor is on one model."""
        acts = []
        render.ai_sync(av, rows)                                                         # what ai_screen() does before every key
        for k in keys:
            acts.append(render.ai_key(av, k, rows))
            if rows:
                self.assertTrue(0 <= av.idx < len(rows), (k, av.idx, len(rows)))
                self.assertEqual(av.cur, rows[av.idx]["id"], k)
        return acts


class Rows(AiCase):
    """ai_rows(): the catalog's models as plain rows, best first."""

    def test_the_models_come_best_first_and_carry_what_the_screen_draws(self):
        rows = render.ai_rows(catalog(lambda c: c["models"].reverse()))
        self.assertEqual([r["id"] for r in rows], ["m-gpu", "m-part", "m-ram", "m-slow", "m-no"])      # by rank, whatever the order of the file
        gpu, part, ram, slow, no = rows
        self.assertEqual((gpu["verdict"], part["verdict"], ram["verdict"], slow["verdict"], no["verdict"]), ("gpu", "partial", "ram", "slow", "no"))
        self.assertEqual((gpu["rec"], gpu["active"], gpu["installed"]), (True, False, False))
        self.assertEqual((ram["rec"], ram["active"], ram["installed"]), (False, True, True))
        self.assertEqual((part["params"], gpu["params"]), ("8B-A2B", "8B"))                              # a mixture of experts: the active parameters too
        self.assertEqual((gpu["need_mb"], gpu["size_mb"], gpu["tok"]), (5000.0, 4400.0, (20.0, 40.0)))
        self.assertEqual(gpu["commands"]["install"], "sudo nuc-console-ai setup m-gpu")
        self.assertFalse(no["pinned"])
        self.assertIsNone(no["tok"])

    def test_a_model_without_rank_goes_last_in_the_catalogs_order(self):
        rows = render.ai_rows(catalog(lambda c: c["models"].extend([model("z-1", rank=None), model("z-2", rank=None)])))
        self.assertEqual([r["id"] for r in rows][-2:], ["z-1", "z-2"])
        self.assertEqual(len(rows), 7)

    def test_the_size_falls_back_to_the_pinned_size_in_bytes(self):
        def pinned(c):
            m = model("pinned")
            m.pop("approx_mb")
            m["size"] = 2 * 2 ** 30                                                                      # the pinned manifest: bytes
            c["models"].append(m)
        self.assertEqual(next(r for r in render.ai_rows(catalog(pinned)) if r["id"] == "pinned")["size_mb"], 2048.0)

    def test_numbers_and_names_in_words(self):
        self.assertEqual([render.ai_mb(x) for x in (400, 1024, 5017, 12288, 130000, None, "x", float("nan"))],
                         ["400 MB", "1.0 GB", "4.9 GB", "12.0 GB", "127 GB", "?", "?", "?"])
        self.assertEqual([render.ai_params(*x) for x in ((8.2, None), (30.5, 3.3), (0.6, 0), (None, 3), ("x", None))], ["8.2B", "30.5B-A3.3B", "0.6B", "?", "?"])
        self.assertEqual([render.ai_tok(x) for x in ((40.0, 70.0), (140.0, 233.0), (2.5, 4.0), (5.0, 5.0), (0.4, 0.6), None)], ["40-70", "140-233", "2.5-4", "5", "0.4-0.6", "-"])

    def test_an_empty_or_odd_catalog_is_no_rows_not_an_error(self):
        for cat in (None, {}, [], "x", {"models": None}, {"models": "no"}, {"models": [None, 3, "x", {}, {"id": 5}, {"id": ""}, {"id": "  "}]}):
            self.assertEqual(render.ai_rows(cat), [], cat)

    def test_two_models_with_one_id_are_one_row(self):
        rows = render.ai_rows(catalog(lambda c: c["models"].append(model("m-gpu", name="Other"))))
        self.assertEqual([r["id"] for r in rows].count("m-gpu"), 1)
        self.assertEqual(rows[0]["name"], "Alpha GPU")                                                   # the first one

    def test_ids_are_cut_and_cleaned(self):
        rows = render.ai_rows(catalog(lambda c: c["models"].append(model("x" * 200 + EVIL, rank=9))))
        mine = rows[-1]["id"]
        self.assertEqual(len(mine), render.AI_ID_MAX)
        self.assertEqual(mine, "x" * 63 + "…")
        self.assertEqual(render.ai_rows(catalog(lambda c: c["models"].append(model("a\x1bb\x9bc", rank=9))))[-1]["id"], "a?b?c")
        self.assertIsNone(CONTROL.search("".join(r["id"] for r in render.ai_rows(catalog(lambda c: c["models"].append(model("e" + EVIL, rank=9)))))))


class Navigation(AiCase):
    """ai_key on a hand-made catalog: what each key does to the cursor and the details."""

    def test_the_cursor_starts_on_the_recommended_model(self):
        av, rows = self.view(catalog(lambda c: c.update(recommended="m-slow")))
        self.assertEqual(render.ai_sync(av, rows), 3)
        self.assertEqual(av.cur, "m-slow")
        av2, rows2 = self.view(catalog(lambda c: c.update(recommended=None)))
        self.assertEqual(render.ai_sync(av2, rows2), 0)                                                # none recommended: the first
        av3, none = self.view({})
        self.assertEqual((render.ai_sync(av3, none), av3.cur), (0, None))

    def test_up_and_down_move_and_stop_at_the_ends(self):
        av, rows = self.view()
        n = len(rows)
        self.assertEqual(self.press(av, rows, ["home", "up", "k"]), ["", "", ""])
        self.assertEqual(av.idx, 0)
        self.press(av, rows, ["down", "j", "down"])
        self.assertEqual((av.idx, av.cur), (3, "m-slow"))
        self.press(av, rows, ["down"] * (n + 5))
        self.assertEqual((av.idx, av.cur), (n - 1, "m-no"))                                            # the last: down stays
        self.press(av, rows, ["up"])
        self.assertEqual(av.idx, n - 2)

    def test_page_keys_move_a_page_and_stop_at_the_ends(self):
        cat = catalog(lambda c: c.update(models=[model("p-%02d" % i, rank=i) for i in range(30)], recommended="p-00"))
        av, rows = self.view(cat, rows=4)
        self.press(av, rows, ["pgdn"])
        self.assertEqual(av.idx, 3)                                                                    # a page is the rows on screen minus one
        self.press(av, rows, ["pgdn"] * 12)
        self.assertEqual(av.idx, 29)
        self.press(av, rows, ["pgup"])
        self.assertEqual(av.idx, 26)
        self.press(av, rows, ["home"])
        self.assertEqual(av.idx, 0)
        self.press(av, rows, ["end"])
        self.assertEqual(av.idx, 29)

    def test_enter_and_space_toggle_the_details(self):
        av, rows = self.view()
        self.assertFalse(av.details)
        self.assertEqual(self.press(av, rows, ["enter"]), [""])
        self.assertTrue(av.details)
        self.press(av, rows, ["space"])
        self.assertFalse(av.details)

    def test_a_esc_q_leave_the_screen_and_change_nothing(self):
        av, rows = self.view()
        self.press(av, rows, ["down", "down"])
        before = (av.idx, av.details)
        for key in ("a", "esc", "q"):
            self.assertEqual(render.ai_key(av, key, rows), "back")
        self.assertEqual((av.idx, av.details), before)

    def test_other_keys_do_nothing(self):
        av, rows = self.view()
        self.press(av, rows, ["down"])
        before = (av.idx, av.cur, av.details)
        self.assertEqual(self.press(av, rows, ["x", "tab", "btab", "left", "right", "1", "7", "c", "h", "m", "d", "w", "z"]), [""] * 13)
        self.assertEqual((av.idx, av.cur, av.details), before)                                         # h, c, m, d, w: other screens' keys
        av2, none = self.view({})
        self.assertEqual(self.press(av2, none, ["up", "down", "pgdn", "pgup", "home", "end", "enter", "enter"]), [""] * 8)
        self.assertEqual((av2.idx, av2.cur), (0, None))

    def test_the_cursor_follows_its_model_through_a_refresh(self):
        av, rows = self.view()
        self.press(av, rows, ["home", "down", "down"])
        self.assertEqual(av.cur, "m-ram")
        rows2 = render.ai_rows(catalog(lambda c: c["models"].insert(0, model("m-new", rank=0))))
        self.assertEqual(render.ai_sync(av, rows2), 3)                                                 # one more above it: the cursor moved with its model
        self.assertEqual(av.cur, "m-ram")

    def test_the_cursor_stays_in_place_when_its_model_disappears(self):
        av, rows = self.view()
        self.press(av, rows, ["home", "down", "down", "down"])
        self.assertEqual(av.cur, "m-slow")
        rows2 = [r for r in rows if r["id"] != "m-slow"]
        self.assertEqual(render.ai_sync(av, rows2), 3)                                                 # the same place, on what moved up into it
        self.assertEqual(av.cur, "m-no")
        self.assertEqual(render.ai_sync(av, rows2[:2]), 1)                                             # fewer than that: the last one
        self.assertEqual(render.ai_sync(av, []), 0)
        self.assertIsNone(av.cur)

    def test_select_finds_by_id_or_name_in_any_case(self):
        av, rows = self.view()
        self.assertTrue(render.ai_select(rows, av, "DELTA"))
        self.assertEqual(av.cur, "m-slow")
        self.assertTrue(render.ai_select(rows, av, "m-part"))
        self.assertEqual(av.idx, 1)
        self.assertFalse(render.ai_select(rows, av, "nothing like this"))
        self.assertFalse(render.ai_select(rows, av, "  "))
        self.assertEqual(av.cur, "m-part")                                                             # no match: the cursor stays

    def test_the_cursor_row_is_always_on_screen(self):
        cat = catalog(lambda c: c.update(models=[model("p-%02d" % i, ("gpu", "partial", "ram", "slow", "no")[i % 5], rank=i) for i in range(40)], recommended="p-00"))
        self.seed(cat)
        for cols, rows in SIZES:
            av, rs = self.view(cat)
            for step in range(len(rs)):
                with self.subTest(size=(cols, rows), step=step):
                    frame, _ = render.ai_screen(render.ai_data(), [], av, cols - 1, rows)
                    out = self.check_frame(frame, cols - 1, rows)
                    chosen = [render.ANSI.sub("", x) for x in frame.split(LINE) if ESC + "[7m" in x and "─" not in x]
                    self.assertEqual(len(chosen), 1)                                                   # exactly one highlighted model ...
                    self.assertIn(rs[av.idx]["name"], chosen[0])                                       # ... the selected one
                    self.assertRegex(out[-1], r"model %d/%d" % (av.idx + 1, len(rs)))
                render.ai_key(av, "down", rs)
            self.assertEqual(av.idx, len(rs) - 1)


class Once(AiCase):
    """`render.py --once --view ai`: the screen at the four sizes, for the three demo machines, with and without details."""

    def test_every_frame_fills_its_screen_and_nothing_more(self):
        for os_name in OSES:
            render.DEMO_OS = os_name
            for cols, rows in SIZES:
                for opts in ([], ["--details"], ["--select", "phi", "--details"], ["--select", "gpt-oss"]):
                    with self.subTest(os=os_name, size=(cols, rows), opts=opts):
                        s, lines = self.screen(opts, cols, rows)
                        self.assertIn(" │ AI │ ", lines[0])
                        self.assertNotIn("Traceback", s)

    def test_odd_sizes_never_break_the_frame(self):
        """From a 40x10 console to a 300x80 one, either side of the widths where the layout changes (110, 140)."""
        for os_name in OSES:
            render.DEMO_OS = os_name
            data = render.ai_data()
            for cols in (24, 40, 60, 100, 109, 110, 139, 140, 190, 300):
                for rows in (5, 10, 14, 20, 40, 80):
                    for details in (False, True):
                        with self.subTest(os=os_name, size=(cols, rows), details=details):
                            av = render.AiView(now=NOW)
                            av.details = details
                            frame, _ = render.ai_screen(data, [], av, cols - 1, rows)
                            self.check_frame(frame, cols - 1, rows)

    def test_every_model_with_its_details_fits_every_size(self):
        for os_name in OSES:
            render.DEMO_OS = os_name
            render._AI.clear()
            for r in render.ai_rows(render.ai_data()["cat"]):
                for cols, rows in SIZES:
                    with self.subTest(os=os_name, model=r["id"], size=(cols, rows)):
                        s, lines = self.screen(["--select", r["id"], "--details"], cols, rows)
                        txt = "\n".join(lines)
                        self.assertIn("── DETAILS", txt)
                        self.assertRegex(txt, r"(?m)^ %s$|│  %s" % (re.escape(r["name"]), re.escape(r["name"])))   # the pane's first line: the whole name
                        self.assertIn(" verdict ", txt)
                        self.assertIn(PILLS[r["verdict"]], txt)
                        self.assertIn(" licence ", txt) if rows >= 33 else None
                        if r["commands"].get("install") and not r["installed"] and r["pinned"] and rows >= 33:
                            self.assertIn(r["commands"]["install"], txt)                               # the command, whole

    def test_the_title_counts_the_verdicts_and_gives_up_its_tagline_when_narrow(self):
        s, lines = self.screen([], 200, 50)
        self.assertRegex(lines[1], r"^── AI  what this machine can run · 12 models · ✔ 10 fit  ◐ 2 gpu\+cpu ─+$")
        render.DEMO_OS = "windows"
        s, lines = self.screen([], 120, 33)
        self.assertIn("what this machine can run · 12 models · ✔ 4 fit  ◐ 4 gpu+cpu  ! 3 slow  ✖ 1 too big", lines[1])
        s, lines = self.screen([], 79, 24)
        self.assertRegex(lines[1], r"^── AI  12 models · ✔ 4 fit  ◐ 4 gpu\+cpu  ! 3 slow  ✖ 1 too big ─+$")   # the tagline goes first
        s, lines = self.screen([], 60, 24)
        self.assertRegex(lines[1], r"^── AI  ✔ 4 fit  ◐ 4 gpu\+cpu  ! 3 slow  ✖ 1 too big ─+$")          # then the models' count
        s, lines = self.screen([], 40, 24)
        self.assertRegex(lines[1], r"^── AI  12 models ─+$")                                              # and the counts: what is left
        self.assertIn("PROBLEMS", lines[0])                                                              # the header's status is the dashboard's

    def test_the_hardware_of_each_demo_machine(self):
        s, lines = self.screen([], 120, 33)
        txt = "\n".join(lines)
        self.assertIn("── HARDWARE ", txt)
        self.assertIn("linux x86_64", txt)
        self.assertIn("AMD Ryzen 7 5800X · 8 cores / 16 threads · AVX2", txt)                           # the trademarks and the clock are not shown
        self.assertRegex(txt, r"RAM +█+░+ +21\.0 GB free of 31\.2 GB")
        self.assertIn("NVIDIA GeForce RTX 3060 · CUDA", txt)
        self.assertRegex(txt, r"11\.0 GB free of 12\.0 GB")
        render.DEMO_OS = "windows"
        s, lines = self.screen([], 200, 50)
        txt = "\n".join(lines)
        self.assertIn("Intel Core i7-10750H · 6 cores / 12 threads · AVX2", txt)
        self.assertIn("NVIDIA GeForce GTX 1650 · CUDA", txt)
        self.assertIn("4.0 GB (free: ?)", txt)                                                           # unknown is not "all free"
        self.assertIn("Intel(R) UHD Graphics · Vulkan", txt)
        self.assertIn("unified memory: it shares the RAM", txt)
        self.assertIn("· nvidia-smi not found: the free video memory could not be read", txt)           # what could not be read, said
        render.DEMO_OS = "darwin"
        s, lines = self.screen([], 120, 33)
        txt = "\n".join(lines)
        self.assertIn("Apple M2 · 8 cores · NEON", txt)
        self.assertIn("Apple M2 (10-core GPU) · Metal", txt)
        self.assertIn("unified memory: it shares the RAM", txt)
        self.assertNotIn("VRAM", txt)

    def test_the_hardware_in_odd_shapes(self):
        def lines(change, w=100, k=0):
            cat = catalog(change)
            return [render.ANSI.sub("", x) for x in render.ai_hw_lines(cat["hw"], w, k)]

        def no_gpu(c):
            c["hw"] = dict(c["hw"], gpus=[])
        self.assertIn("none found: the models run on the CPU, from RAM", "\n".join(lines(no_gpu)))

        def no_avx(c):
            c["hw"] = dict(c["hw"], cpu={"model": "Old CPU", "cores": 2, "threads": 2, "flags": ["sse2"]})
        self.assertIn("2 cores · no AVX2: slow", "\n".join(lines(no_avx)))

        def avx512(c):
            c["hw"] = dict(c["hw"], cpu={"model": "Xeon", "cores": 4, "threads": 8, "flags": ["avx2", "avx512f", "avx512bw"]})
        self.assertIn("4 cores / 8 threads · AVX2 AVX-512", "\n".join(lines(avx512)))

        def unknown(c):
            c["hw"] = {"gpus": [{"name": "Mystery", "vram_mb": None, "backend": "none"}], "ram": {}, "cpu": {}}
        txt = "\n".join(lines(unknown))
        self.assertIn("RAM   ? could not be read", txt)
        self.assertIn("Mystery · no usable backend: not used", txt)
        self.assertIn("? video memory could not be read", txt)

        def many(c):
            c["hw"] = dict(c["hw"], gpus=c["hw"]["gpus"] * 10, notes=["note %d" % i for i in range(6)])
        self.assertIn("… +2 more GPUs", "\n".join(lines(many)))                                         # eight, and the count of the others
        self.assertEqual("\n".join(lines(many)).count("NVIDIA GeForce RTX 3060"), 8)
        self.assertIn("… +7 more GPUs", "\n".join(lines(many, k=1)))
        self.assertIn("… +4 more notes", "\n".join(lines(many)))
        self.assertEqual(lines(many, k=3), [])
        for k in range(4):                                                                              # every level fits its width
            for w in (20, 40, 78, 119):
                self.assertFalse([x for x in lines(many, w, k) if len(x) > w], (k, w))
        self.assertEqual(render.ai_cpu_name("Intel(R) Core(TM) i7-10750H CPU @ 2.60GHz"), "Intel Core i7-10750H")
        self.assertEqual(render.ai_cpu_name("AMD Ryzen 7 5800X 8-Core Processor"), "AMD Ryzen 7 5800X")
        self.assertEqual(render.ai_cpu_name("Apple M2"), "Apple M2")

    def test_the_models_table_follows_the_width(self):
        txt = {c: "\n".join(self.screen([], c, 50)[1]) for c, _ in SIZES}
        for c in (79, 120, 200, 226):
            self.assertIn("── MODELS ", txt[c], c)
            self.assertIn("est tok/s", txt[c], c)                                                       # the speed is an estimate: the header says so
        self.assertNotIn("params", txt[79])                                                             # narrow: the parameters go first ...
        self.assertNotIn("notes", txt[79])                                                              # ... and the notes
        self.assertIn("params", txt[120])
        self.assertIn("notes", txt[120])
        self.assertIn("fast on CPU (MoE: only 3.3B parameters work per token)", txt[226])               # a wide screen has room for the whole note
        for c in (79, 120, 200, 226):                                                                   # STATUS beside the hardware from 110 columns
            row = next(x for x in txt[c].split("\n") if "── HARDWARE" in x)
            self.assertEqual("── STATUS" in row, c >= 110, c)

    def test_the_pills_have_a_symbol_besides_the_colour(self):
        render.DEMO_OS = "windows"
        s, lines = self.screen([], 120, 33)
        txt = "\n".join(lines)
        for verdict in ("gpu", "partial", "slow", "no"):
            self.assertIn(PILLS[verdict], txt)
        self.assertEqual(sum(PILLS["no"] in x for x in lines), 1)
        self.seed(catalog())
        for pick, shown in (("m-no", ("gpu", "partial", "ram", "slow")), ("m-gpu", ("no",))):          # the cursor's row is plain reverse video: no pill colour
            s, lines = self.screen(["--select", pick], 120, 33)
            for verdict, sgr in (("gpu", "1;42;30"), ("partial", "1;46;30"), ("ram", "1;42;30"), ("slow", "1;43;30"), ("no", "1;41;37")):
                self.assertIn(PILLS[verdict], "\n".join(lines))                                         # every verdict, with its symbol
                if verdict in shown:
                    self.assertIn(ESC + "[" + sgr + "m " + PILLS[verdict].ljust(10) + " ", s)           # and its colour

    def test_the_marks_recommended_installed_and_active(self):
        s, lines = self.screen([], 120, 33)
        row = lambda name: next(x for x in lines if name in x)  # noqa: E731
        self.assertTrue(row("Phi-4 14B").startswith(" ★ "), row("Phi-4 14B"))
        self.assertTrue(row("Qwen3 4B").startswith("  ✓● "), row("Qwen3 4B"))
        self.assertTrue(row("Qwen3 1.7B").startswith("  ✓ "), row("Qwen3 1.7B"))
        self.assertTrue(row("Qwen3 8B").startswith("      Qwen3 8B"))
        legend = next(x for x in lines if "recommended" in x and "installed" in x)
        self.assertIn("★ recommended  ·  ✓ installed  ·  ● active  ·  tok/s: rough estimate", legend)
        s, lines = self.screen([], 79, 24)
        self.assertIn("★ recommended  ·  ✓ installed  ·  ● active", "\n".join(lines))                  # the legend fits the narrow screen

    def test_select_and_details_open_the_pane_below_or_beside_the_list(self):
        s, lines = self.screen(["--select", "QWEN3 4B", "--details"], 79, 30)                           # any case, id or name
        self.assertIn("model 8/12", lines[-1].replace("  ", " "))
        i = next(j for j, x in enumerate(lines) if x.startswith("── DETAILS"))
        self.assertGreater(i, 4)                                                                         # below the list
        pane = "\n".join(lines[i:])
        for word in ("Qwen3 4B", "verdict", "✔ FITS GPU", "why", "speed", "about 61-102 tokens/s (a rough estimate, not a promise)", "where", "all 36 layers on the GPU",
                     "licence", "Apache-2.0", "state", "✓ installed · ● active: [ai] model", "remove", "sudo nuc-console-ai remove qwen3-4b"):
            self.assertIn(word, pane)
        self.assertNotIn(" install ", pane)                                                              # installed: nothing to install
        self.assertNotIn(" use ", pane)                                                                  # and active: nothing to switch to
        s, lines = self.screen(["--select", "gpt-oss", "--details"], 200, 50)                            # wide: beside the list
        row = next(x for x in lines if "── DETAILS" in x)
        self.assertGreater(row.index("── DETAILS"), 100)
        self.assertIn("── MODELS", row)
        txt = "\n".join(lines)
        self.assertIn("not pinned yet: this build cannot download it", txt)                              # no command for what cannot be downloaded
        self.assertNotIn("sudo nuc-console-ai setup gpt-oss-20b", txt)
        self.assertIn("20 of 24 layers on the GPU, the rest in RAM", txt)
        self.assertIn("── STATUS", txt)                                                                  # the sections stay
        s, lines = self.screen(["--select", "smollm"], 200, 50)                                          # a selection alone: no pane
        self.assertNotIn("DETAILS", "\n".join(lines))
        self.assertTrue([x for x in s.split(LINE) if ESC + "[7m" in x and "─" not in x])               # the cursor is still on its row
        s, lines = self.screen(["--select", "nothing like this", "--details"], 120, 33)                 # no match: the cursor stays on the recommended one
        self.assertIn("model 3/12", lines[-1].replace("  ", " "))

    def test_the_commands_of_each_state(self):
        def pane(change, mid, os_name=None, cols=200):
            render._AI.clear()
            self.seed(catalog(change))
            return "\n".join(self.screen(["--select", mid, "--details"], cols, 50)[1])
        txt = pane(None, "m-gpu")                                                                        # not installed, pinned: install
        self.assertIn("install  sudo nuc-console-ai setup m-gpu", txt)
        self.assertNotIn(" use ", txt)
        self.assertNotIn(" remove ", txt)
        self.assertIn("state    ★ recommended for this machine", txt)
        txt = pane(lambda c: c["models"][0].update(installed=True), "m-gpu")                             # installed, not active: use and remove
        self.assertNotIn("install  ", txt)
        self.assertIn("use      sudo nuc-console-ai use m-gpu", txt)
        self.assertIn("remove   sudo nuc-console-ai remove m-gpu", txt)
        txt = pane(None, "m-ram")                                                                        # installed and active: remove only
        self.assertNotIn("use      ", txt)
        self.assertIn("remove   sudo nuc-console-ai remove m-ram", txt)
        self.assertIn("✓ installed · ● active: [ai] model", txt)
        txt = pane(None, "m-no")                                                                         # not pinned
        self.assertIn("not pinned yet: this build cannot download it", txt)
        self.assertIn("✖ TOO BIG", txt)
        self.assertNotIn("setup m-no", txt)
        txt = pane(lambda c: c["models"][0].update(commands={}), "m-gpu")                                # a catalog without commands: no command, no crash
        self.assertNotIn("sudo", txt)
        self.assertIn("not pinned yet", txt)                                                             # (what cannot be typed cannot be downloaded)

    def test_the_commands_come_before_the_long_texts_so_a_small_screen_cuts_those(self):
        for opts, cols, rows in ((["--select", "phi-4", "--details"], 79, 24), (["--select", "gpt-oss", "--details"], 79, 24), (["--select", "phi-4", "--details"], 100, 22)):
            s, lines = self.screen(opts, cols, rows)
            txt = "\n".join(lines)
            self.assertIn("sudo nuc-console-ai setup phi-4" if "phi-4" in opts else "not pinned yet", txt, (opts, cols, rows))
        title, items = render.ai_details(render.ai_rows(catalog())[0])
        labels = [k for k, _, _ in items]
        self.assertEqual(labels[:5], ["verdict", "why", "speed", "state", "install"])
        self.assertLess(labels.index("install"), labels.index("needs"))
        self.assertLess(labels.index("install"), labels.index("licence"))

    def test_where_the_model_would_run_in_words(self):
        rows = {r["id"]: r for r in render.ai_rows(catalog())}
        self.assertEqual(render.ai_where(rows["m-gpu"]), "all 36 layers on the GPU")
        self.assertEqual(render.ai_where(rows["m-part"]), "12 of 36 layers on the GPU, the rest in RAM")
        self.assertEqual(render.ai_where(rows["m-ram"]), "the CPU, from RAM")
        self.assertEqual(render.ai_where(rows["m-slow"]), "the CPU, from RAM")
        self.assertEqual(render.ai_where(rows["m-no"]), "-")
        gpu = dict(rows["m-gpu"], gpu_layers=None)                                                       # a catalog without the layers: still "all on the GPU"
        self.assertEqual(render.ai_where(gpu), "all on the GPU")
        self.assertEqual(render.ai_where(dict(rows["m-part"], layers=None)), "12 layers on the GPU, the rest in RAM")

    def test_windows_commands_say_where_to_run_them(self):
        def change(c):
            c["hw"] = dict(c["hw"], os="windows")
            for m in c["models"]:
                m["commands"] = {k: v.replace("sudo ", "") for k, v in m["commands"].items()}
        self.seed(catalog(change))
        txt = "\n".join(self.screen(["--select", "m-gpu", "--details"], 200, 50)[1])
        self.assertIn("run the commands in an administrator prompt", txt)
        self.assertIn("install  nuc-console-ai setup m-gpu", txt)
        self.seed(catalog())
        self.assertNotIn("administrator prompt", "\n".join(self.screen(["--select", "m-gpu", "--details"], 200, 50)[1]))

    def test_the_why_the_speed_and_the_size_are_in_the_details(self):
        render.DEMO_OS = "windows"
        s, lines = self.screen(["--select", "qwen3-8b", "--details"], 226, 50)
        txt = "\n".join(lines)
        self.assertIn("needs 5.8 GB, the NVIDIA GeForce GTX 1650 has 3.4 GB free: 19 of 36 layers", txt)   # (the pane wraps it)
        self.assertIn("rest (2.7 GB) in RAM", txt)
        why = next(v for k, v, _ in render.ai_details(next(r for r in render.ai_rows(render.ai_data()["cat"]) if r["id"] == "qwen3-8b"))[1] if k == "why")
        self.assertEqual(why, "needs 5.8 GB, the NVIDIA GeForce GTX 1650 has 3.4 GB free: 19 of 36 layers on the GPU, the rest (2.7 GB) in RAM")
        self.assertIn("about 8-15 tokens/s (a rough estimate, not a promise)", txt)
        self.assertIn("5.8 GB of memory: the file (4.9 GB), the context and the runtime", txt)
        self.assertIn("8.2B parameters · Q4_K_M · context up to 40960 tokens", txt)
        self.assertIn("19 of 36 layers on the GPU, the rest in RAM", txt)
        self.assertIn("thinking mode: /no_think turns it off", txt)
        s, lines = self.screen(["--select", "30b", "--details"], 200, 50)
        txt = "\n".join(lines)
        self.assertIn("no estimate", txt)                                                                # too big: no speed to promise
        self.assertIn("it will not work", txt)

    def test_the_command_line_prints_the_screen_for_each_demo_os(self):
        for os_name in OSES:
            out = io.StringIO()
            args = ["render.py", "--once", "--demo", "--view", "ai", "--cols", "120", "--rows", "33"] + (["--demo-os", os_name] if os_name else [])
            with contextlib.redirect_stdout(out):
                self.assertIsNone(render.once(args))
            lines = out.getvalue().replace("\r", "").rstrip("\n").split("\n")
            self.assertEqual(len(lines), 33)
            self.assertTrue(all(len(x) <= 119 for x in lines), os_name)
            self.assertNotIn(ESC, out.getvalue())                                                        # without --color: no escape codes at all
            self.assertIn({None: "AMD Ryzen 7 5800X", "windows": "Intel Core i7-10750H", "darwin": "Apple M2 (10-core GPU)"}[os_name], out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render.once(["render.py", "--once", "--demo", "--view", "ai", "--color", "--cols", "120", "--rows", "33"])
        self.assertIn(ESC + "[", out.getvalue())                                                         # --color keeps them (tools/ansi2svg.py)

    def test_the_feature_off_the_command_line_refuses(self):
        render.CFG["features"]["ai"] = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = render.once(["render.py", "--once", "--demo", "--view", "ai"])
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertIn("[features] ai = no", err.getvalue())
        self.assertEqual(self.builds, [])                                                                # nothing was read

    def test_the_status_of_each_demo_machine(self):
        s, lines = self.screen([], 200, 50)
        txt = "\n".join(lines)
        self.assertIn("advisor   ✔ on  ([ai] enabled = yes)", txt)
        self.assertIn("endpoint  http://127.0.0.1:11434/v1", txt)
        self.assertIn("server    ✔ answering · 1 model: qwen3-4b", txt)
        self.assertIn("model     ● qwen3-4b  in the catalog", txt)
        self.assertIn("runtime   ✔ installed (0.10.6)", txt)
        self.assertIn("files     /var/lib/nuc-console-ai", txt)
        render.DEMO_OS = "windows"
        txt = "\n".join(self.screen([], 200, 50)[1])
        self.assertIn("advisor   · off  ([ai] enabled = no in config.ini)", txt)
        self.assertIn("server    · not asked while the advisor is off", txt)
        self.assertIn("model     · none chosen ([ai] model is empty)", txt)
        self.assertIn("runtime   ! not installed: setup downloads it", txt)
        render.DEMO_OS = "darwin"
        txt = "\n".join(self.screen([], 200, 50)[1])
        self.assertRegex(txt, r"server +✖ not answering\s*\n +no server on 127\.0\.0\.1:8080: start Ollama or run nuc-console-ai serve")    # under the verdict, wrapped
        txt = "\n".join(self.screen([], 226, 50)[1])
        self.assertIn("server    ✖ not answering · no server on 127.0.0.1:8080: start Ollama or run nuc-console-ai serve", txt)   # a wide column: beside it

    def test_the_status_gives_up_lines_before_the_list_loses_rows(self):
        render.DEMO_OS = "windows"
        s, lines = self.screen([], 79, 24)
        txt = "\n".join(lines)
        self.assertIn("── STATUS", txt)
        self.assertRegex(txt, r"advisor +· off")
        self.assertNotIn("endpoint ", txt)                                                               # two lines: the endpoint is on the six-line version only
        s, lines = self.screen([], 79, 14)
        txt = "\n".join(lines)
        self.assertNotIn("── HARDWARE", txt)                                                              # a small screen: the list first
        self.assertGreaterEqual(sum(1 for x in lines if "GPU" in x and ("✔" in x or "◐" in x or "!" in x)), 3)



class DataSource(AiCase):
    """ai_data(): aisetup.catalog() at most once per AI_TTL, a failure that does not escape, the demo."""

    def setUp(self):
        super().setUp()
        render.DEMO = False
        render.ai_build = self.saved["ai_build"]
        self.catalogs = []
        self.fake = types.SimpleNamespace(catalog=lambda: (self.catalogs.append(self.now), catalog())[1])
        patch = mock.patch.dict(sys.modules, {"aisetup": self.fake})
        patch.start()
        self.addCleanup(patch.stop)

    def test_once_per_ttl_whatever_the_number_of_calls(self):
        for _ in range(50):
            render.ai_data()
        self.assertEqual(len(self.catalogs), 1)
        self.now += render.AI_TTL - 1
        render.ai_data()
        self.assertEqual(len(self.catalogs), 1)
        self.now += 2                                                                                    # AI_TTL has passed
        data = render.ai_data()
        self.assertEqual(len(self.catalogs), 2)
        self.assertEqual((data["msg"], data["err"]), ("", False))
        self.assertEqual(len(render.ai_rows(data["cat"])), 5)

    def test_keys_and_frames_never_ask_for_the_catalog_again(self):
        av, rows = self.view()
        for _ in range(3):
            for k in ("down", "enter", "up", "enter", "end", "home"):
                render.ai_key(av, k, rows)
                render.ai_screen(render.ai_data(), [], av, 119, 33)
        self.assertEqual(len(self.catalogs), 1)

    def test_the_demo_is_not_aisetup(self):
        render.DEMO = True
        data = render.ai_data()
        self.assertEqual(self.catalogs, [])                                                              # the demo machine, whatever is installed
        self.assertEqual(len(render.ai_rows(data["cat"])), 12)
        render.DEMO_OS = "darwin"                                                                        # another machine: its own catalog
        self.assertEqual(render.ai_data()["cat"]["hw"]["os"], "darwin")

    def test_a_failing_catalog_is_a_message_kept_for_the_ttl(self):
        def broken():
            self.catalogs.append(1)
            raise OSError("cannot read " + ESC + "[2J")
        self.fake.catalog = broken
        data = render.ai_data()
        for _ in range(5):
            render.ai_data()
        self.assertEqual(len(self.catalogs), 1)                                                          # not retried on every key
        self.assertIsNone(data["cat"])
        self.assertTrue(data["err"])
        self.assertIn("the model catalog could not be read: OSError", data["msg"])
        self.assertNotIn(ESC, data["msg"])
        s, lines = self.screen_of(data)
        self.assertIn("the model catalog could not be read: OSError", "\n".join(lines))
        self.assertIn("no models", lines[-1])
        self.assertNotIn("0 models", lines[1])                                                           # nothing is not "0 models"
        self.now += render.AI_TTL + 1
        self.fake.catalog = lambda: catalog()
        self.assertIsNotNone(render.ai_data()["cat"])                                                    # the next ttl tries again

    def screen_of(self, data, cols=120, rows=33):
        s, _ = render.ai_screen(data, [], render.AiView(now=NOW), cols - 1, rows)
        return s, self.check_frame(s, cols - 1, rows)

    def test_a_missing_module_or_function_costs_the_screen_not_the_dashboard(self):
        with mock.patch.dict(sys.modules, {"aisetup": None}):                                           # import fails
            data = render.ai_data()
        self.assertIsNone(data["cat"])
        self.assertIn("the model catalog could not be read", data["msg"])
        render._AI.clear()
        with mock.patch.dict(sys.modules, {"aisetup": types.SimpleNamespace()}):                        # no catalog() in this build
            data = render.ai_data()
        self.assertIn("AttributeError", data["msg"])
        render._AI.clear()
        self.fake.catalog = lambda: "not a dict"
        self.assertIn("TypeError", render.ai_data()["msg"])
        for cols, rows in SIZES:
            self.screen_of(data, cols, rows)

    def test_the_header_status_is_the_dashboards(self):
        data, pb = render.ai_state(None)
        self.assertEqual(data["err"], False)
        self.assertIsInstance(pb, list)


class Probe(AiCase):
    """ai_probe(): does the model server answer? In a thread of its own, at most once a minute, never while [ai] is off."""

    def setUp(self):
        super().setUp()
        render.DEMO = False
        render.CFG["ai"].update(enabled=True, endpoint="http://127.0.0.1:11434/v1", allow_remote=False)
        self.runs, self.gate = [], threading.Event()
        self.gate.set()
        self.answer = {"state": "answering", "msg": "", "models": ["qwen3-4b"]}

        def run(ai):
            self.runs.append(dict(ai))
            self.gate.wait(5)
            return dict(self.answer)
        render.ai_probe_run = run

    def finish(self):
        t = render._AIPROBE["thread"]
        if t is not None:
            t.join(5)

    def test_the_first_call_does_not_wait_for_the_network(self):
        self.gate.clear()                                                                                # the server does not answer (yet)
        t0 = time.time()
        self.assertIsNone(render.ai_probe())                                                             # "checking…"
        self.assertLess(time.time() - t0, 1.0)
        self.assertIsNone(render.ai_probe())                                                             # still: and no second probe is started
        self.gate.set()
        self.finish()
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(render.ai_probe()["state"], "answering")

    def test_the_answer_is_cached_for_a_minute_then_asked_again_in_the_background(self):
        render.ai_probe()
        self.finish()
        for _ in range(30):
            self.assertEqual(render.ai_probe()["state"], "answering")
        self.assertEqual(len(self.runs), 1)
        self.now += render.AI_PROBE_TTL - 1
        render.ai_probe()
        self.assertEqual(len(self.runs), 1)
        self.now += 2
        self.answer = {"state": "down", "msg": "no server", "models": []}
        self.gate.clear()
        self.assertEqual(render.ai_probe()["state"], "answering")                                        # the old answer stays while the new one is on its way
        self.assertEqual(render.ai_probe()["state"], "answering")
        self.assertEqual(len(self.runs), 2)                                                              # one probe, however many frames
        self.gate.set()
        self.finish()
        self.assertEqual(render.ai_probe()["state"], "down")
        self.assertEqual(len(self.runs), 2)

    def test_wait_is_for_once_which_may_wait_a_little(self):
        self.gate.clear()
        t0 = time.time()
        self.assertIsNone(render.ai_probe(wait=0.2))
        self.assertGreaterEqual(time.time() - t0, 0.15)
        self.gate.set()
        self.finish()
        self.assertEqual(render.ai_probe(wait=1.0)["state"], "answering")

    def test_nothing_is_asked_while_the_advisor_is_off(self):
        render.CFG["ai"]["enabled"] = False
        res = render.ai_probe()
        self.assertEqual(res["state"], "off")
        self.assertIn("[ai] enabled = no", res["msg"])
        self.assertEqual(self.runs, [])
        self.assertIsNone(render._AIPROBE["thread"])                                                     # not even a thread

    def test_a_changed_endpoint_is_asked_again_and_the_old_answer_is_not_shown(self):
        render.ai_probe()
        self.finish()
        render.CFG["ai"]["endpoint"] = "http://127.0.0.1:8080/v1"
        self.gate.clear()
        self.assertIsNone(render.ai_probe())                                                             # the answer of another server is no answer
        self.gate.set()
        self.finish()
        self.assertEqual([r["endpoint"] for r in self.runs], ["http://127.0.0.1:11434/v1", "http://127.0.0.1:8080/v1"])

    def test_a_probe_stuck_for_too_long_is_given_up(self):
        self.gate.clear()
        render.ai_probe()
        self.now += render.AI_PROBE_STUCK - 1
        render.ai_probe()
        self.assertEqual(len(self.runs), 1)
        self.now += 2
        render.ai_probe()
        self.assertEqual(len(self.runs), 2)                                                              # a new one: the first is stuck in a DNS lookup
        self.gate.set()
        self.finish()

    def test_the_probe_asks_the_advisor_with_a_one_second_timeout(self):
        render.ai_probe_run = self.saved["ai_probe_run"]
        seen = {}

        def info(endpoint, allow_remote=False):
            seen["info"] = (endpoint, allow_remote)
            return {"host": "x"}

        def models(i, timeout=10.0):
            seen["timeout"] = timeout
            return ["a", "b" + ESC + "[2J", "c"]
        with mock.patch.object(advisor, "endpoint_info", info), mock.patch.object(advisor, "list_models", models):
            res = render.ai_probe_run(dict(render.CFG["ai"], allow_remote=True))
        self.assertEqual(seen, {"info": ("http://127.0.0.1:11434/v1", True), "timeout": 1.0})
        self.assertEqual((res["state"], res["models"]), ("answering", ["a", "b?[2J", "c"]))

    def test_a_server_that_does_not_answer_is_a_message(self):
        render.ai_probe_run = self.saved["ai_probe_run"]

        def refuse(i, timeout=10.0):
            raise advisor.AdvisorError("no server on 127.0.0.1:11434: start Ollama or run nuc-console-ai serve" + ESC + "[2J")
        with mock.patch.object(advisor, "endpoint_info", lambda *a, **kw: {"host": "x"}), mock.patch.object(advisor, "list_models", refuse):
            res = render.ai_probe_run(dict(render.CFG["ai"]))
        self.assertEqual(res["state"], "down")
        self.assertIn("no server on 127.0.0.1:11434", res["msg"])
        self.assertNotIn(ESC, res["msg"])

        def explode(*a, **kw):
            raise ValueError("secret details")
        with mock.patch.object(advisor, "endpoint_info", explode):
            res = render.ai_probe_run(dict(render.CFG["ai"]))
        self.assertEqual((res["state"], res["msg"]), ("down", "ValueError"))                             # anything else is only named
        with mock.patch.object(advisor, "endpoint_info", side_effect=advisor.Disabled("[ai] endpoint x is not on this machine")):
            self.assertIn("is not on this machine", render.ai_probe_run(dict(render.CFG["ai"]))["msg"])

    def test_the_status_reads_the_config_and_the_probe(self):
        render.CFG["ai"]["model"] = "qwen3-4b" + ESC + "[2J"
        render.ai_probe()
        self.finish()
        st = render.ai_status()
        self.assertEqual((st["enabled"], st["endpoint"], st["model"], st["probe"]["state"]), (True, "http://127.0.0.1:11434/v1", "qwen3-4b?[2J", "answering"))
        render.DEMO = True
        self.assertEqual(render.ai_status()["probe"]["state"], "answering")                              # the demo: its own, no thread
        render.DEMO_OS = "darwin"
        self.assertEqual(render.ai_status()["probe"]["state"], "down")
        self.assertEqual(len(self.runs), 1)

    def test_the_screen_says_checking_then_the_answer(self):
        self.seed(catalog())
        self.gate.clear()
        render.ai_status = self.saved["ai_status"]
        txt = "\n".join(self.frame_of(200, 50))
        self.assertIn("server    · checking…", txt)
        self.gate.set()
        self.finish()
        txt = "\n".join(self.frame_of(200, 50))
        self.assertIn("server    ✔ answering · 1 model: qwen3-4b", txt)
        self.assertIn("advisor   ✔ on", txt)

    def test_the_model_that_the_catalog_does_not_have_is_flagged(self):
        self.seed(catalog(), {"enabled": True, "endpoint": "http://127.0.0.1:1/v1", "model": "mystery-7b", "probe": {"state": "answering", "msg": "", "models": []}})
        txt = "\n".join(self.frame_of(200, 50))
        self.assertIn("● mystery-7b  not in the catalog", txt)
        self.assertIn("server    ✔ answering · 0 models", txt)
        self.seed(catalog(), {"enabled": True, "endpoint": "e", "model": "m-ram", "probe": {"state": "answering", "msg": "", "models": list("abcde")}})
        txt = "\n".join(self.frame_of(200, 50))
        self.assertIn("● m-ram  in the catalog", txt)
        self.assertIn("5 models: a, b, c…", txt)


class Footer(AiCase):
    def test_the_overview_offers_the_ai_screen_when_there_is_a_keyboard_and_the_feature_is_on(self):
        slide = ("Overview", 1, 1, ["x"])
        on = render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=True))
        self.assertIn("m: map   c: cpu   h: health   a: ai   console", on)                                # after the others
        self.assertNotIn("a: ai", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=False)))   # the web page: no keys
        self.assertNotIn("a: ai", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=True, aikey=False)))
        self.assertIn("a: ai", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=False, aikey=True)))
        render.CFG["features"]["ai"] = False
        self.assertNotIn("a: ai", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=True)))
        render.CFG["features"]["ai"] = True
        render.CFG["features"]["health"] = False
        self.assertIn("a: ai", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=True)))
        two = render.ANSI.sub("", render.frame(("Overview", 1, 2, ["x"]), 0, 2, 79, 24, [], keys=True)).split("\r\n")[-1]
        self.assertLessEqual(len(two), 79)
        one = render.ANSI.sub("", render.frame(slide, 0, 1, 79, 24, [], keys=True)).split("\r\n")[-1]
        self.assertIn("a: ai   console 80x24", one)                                                      # and it fits the narrowest footer

    def test_the_ai_footer_gives_up_keys_from_the_least_needed_when_narrow(self):
        av = render.AiView(now=NOW)
        seen = []
        for w in (140, 100, 78, 60, 40, 24, 12):
            text = render.ANSI.sub("", render.ai_footer(av, 12, w))
            seen.append(text)
            self.assertLessEqual(len(text), w)
        self.assertIn("PgUp/PgDn/Home/End: page", seen[0])
        self.assertNotIn("PgUp", seen[2])
        self.assertIn("a/Esc: back", seen[0])
        self.assertIn("a: back", seen[4])
        self.assertIn("model 1/12", seen[0])
        av.details = True
        self.assertIn("Enter: hide details", render.ANSI.sub("", render.ai_footer(av, 12, 140)))
        self.assertIn("no models", render.ANSI.sub("", render.ai_footer(av, 0, 140)))


class MainLoop(AiCase):
    """render.main() on a fake terminal: `a` opens the AI screen, its keys move it, a/Esc/q go back, an idle one gives the monitor back.
    KEY_CHAR & co. decode the raw reads, as on a real console."""

    def run_main(self, script, keyboard=True, cols=80, rows=24):
        """script: what each keyboard read returns (raw bytes), a number of seconds that pass without a key, or (seconds, bytes):
        a key typed that long after the frame. -> (the frames drawn, ANSI stripped, each a list of lines; what tcsetattr got back)."""
        out, err, restored, todo = io.StringIO(), io.StringIO(), [], list(script)

        def next_read(fd=None, wait=None):
            if not todo:
                raise Stop()
            x = todo.pop(0)
            if isinstance(x, tuple):
                self.now += x[0]
                x = x[1]
            if isinstance(x, bytes):
                return render.decode_keys(x)
            self.now += x
            return []
        render.sys = Proxy(sys, stdin=types.SimpleNamespace(fileno=lambda: 5), stdout=out, stderr=err)
        render.os = Proxy(os, isatty=lambda fd: keyboard)
        render.termios = types.SimpleNamespace(tcgetattr=lambda fd: ["attrs", fd], TCSADRAIN=1,
                                               tcsetattr=lambda fd, when, attrs: restored.append(attrs))
        render.tty = types.SimpleNamespace(setcbreak=lambda fd: None)
        render.signal = Proxy(signal, signal=lambda *a: None)
        render.shutil = Proxy(shutil, get_terminal_size=lambda fallback=None: os.terminal_size((cols, rows)))
        render.WINDOWS, render.MODE, render.Sampler = False, "overview", FakeSampler
        render.read_keys, render.page_overview = next_read, overview_stub
        self.on_sleep = lambda sec: next_read()                                          # no keyboard: the loop sleeps between frames
        with self.assertRaises(Stop):
            render.main(["render.py"])
        text = out.getvalue()
        self.assertTrue(text.endswith("\x1b[?25h\x1b[0m"))                               # the cursor is given back
        frames = []
        for chunk in text[:-len("\x1b[?25h\x1b[0m")].split("\x1b[H")[1:]:
            chunk = chunk[:-len("\x1b[2J")] if chunk.endswith("\x1b[2J") else chunk    # cleared when the screen changes
            frames.append(self.check_frame(chunk, cols - 1, rows, len(frames)))
        return frames, restored

    @staticmethod
    def is_ai(frame):
        return " │ AI │ " in frame[0] and "── AI " in frame[1]

    @staticmethod
    def at(frame):
        m = re.search(r"model (\d+)/(\d+)", frame[-1].replace("  ", " "))
        return (int(m.group(1)), int(m.group(2))) if m else None

    def test_a_opens_the_screen_keys_move_it_and_a_esc_q_go_back(self):
        frames, restored = self.run_main([b"a", b"\x1b[B\x1bOB", b"\x1b[B", b"\r", b"\x1b[6~", b"\x1b[H", b"a", b"a", b"\x1b", b"a", b"q"])
        self.assertEqual([self.is_ai(f) for f in frames], [False, True, True, True, True, True, True, False, True, False, True, False])
        self.assertIn("a: ai", frames[0][-1])                                            # a keyboard: the footer offers it
        self.assertIn("overview page (stub)", "\n".join(frames[0]))
        self.assertEqual(self.at(frames[1]), (3, 12))                                    # opened on the recommended model (Phi-4 14B)
        self.assertEqual(self.at(frames[2]), (5, 12))                                    # ↓ (CSI) ↓ (SS3)
        self.assertEqual(self.at(frames[3]), (6, 12))
        self.assertNotIn("── DETAILS", "\n".join(frames[3]))
        self.assertIn("── DETAILS", "\n".join(frames[4]))                                # Enter
        self.assertIn("Granite 3.3 8B", "\n".join(frames[4]))
        self.assertEqual(self.at(frames[6]), (1, 12))                                    # Home
        self.assertEqual(restored, [["attrs", 5]])                                       # the terminal's settings are put back

    def test_the_catalog_is_not_read_again_within_its_ttl_but_is_after(self):
        self.run_main([b"a", b"\x1b[B", (render.REFRESH_S, b"\x1b[B"), 5, b"\x1b[B", b"\x1b"])
        self.assertEqual(self.builds, [(True, None)])
        self.builds[:] = []
        render._AI.clear()
        self.run_main([b"a", render.REFRESH_S * 4, render.AI_TTL, b"\x1b"])
        self.assertEqual(len(self.builds), 2)                                            # the screen stayed open past the ttl: a new catalog

    def test_an_idle_screen_gives_the_monitor_back_to_the_rotation(self):
        idle = render.AI_IDLE_S
        frames, _ = self.run_main([b"a", idle - 1, b"\x1b[B", idle - 1, 2])
        self.assertEqual([self.is_ai(f) for f in frames], [False, True, True, True, True, False])
        self.assertEqual(self.at(frames[4]), (4, 12))                                    # a key restarts the count

    def test_without_a_keyboard_the_screen_is_not_offered(self):
        frames, restored = self.run_main([2, 2], keyboard=False)
        self.assertEqual(len(frames), 3)
        self.assertFalse([f for f in frames if self.is_ai(f) or "a: ai" in f[-1]])
        self.assertEqual(restored, [])                                                   # no terminal settings were changed

    def test_feature_off_a_does_nothing(self):
        render.CFG["features"]["ai"] = False
        frames, restored = self.run_main([b"a", b"a", b"\x1b[B"])
        self.assertEqual(len(frames), 4)
        self.assertFalse([f for f in frames if self.is_ai(f) or "a: ai" in f[-1]])
        self.assertEqual(restored, [["attrs", 5]])
        self.assertEqual(self.builds, [])                                                # and the hardware is never read

    def test_the_other_screens_keys_are_not_taken_and_a_is_not_theirs(self):
        frames, _ = self.run_main([b"1", b"a", b"h", b"c", b"m", b"\x1b"])               # 1 jumps to a page, h/c/m do nothing inside the AI screen
        self.assertEqual([self.is_ai(f) for f in frames], [False, False, True, True, True, True, False])
        self.assertEqual(self.at(frames[2]), (3, 12))

    def test_a_does_not_open_the_ai_screen_from_inside_another_screen(self):
        render.DEMO_HEALTH = ""
        render._HEALTH.clear()
        frames, _ = self.run_main([b"h", b"a", b"\x1b", b"a", b"\x1b"])
        is_health = lambda f: " │ Health │ " in f[0]  # noqa: E731
        self.assertEqual([is_health(f) for f in frames], [False, True, True, False, False, False])
        self.assertEqual([self.is_ai(f) for f in frames], [False, False, False, False, True, False])    # the second a, from the dashboard, does
        self.assertEqual(self.builds, [(True, None)])

    def test_a_broken_catalog_is_an_error_frame_esc_still_works_and_it_retries(self):
        calls, real = [], render.ai_build

        def flaky(now):
            calls.append(self.now - NOW)
            if len(calls) == 1:
                raise ValueError("state unreadable " + ESC + "[2J")
            return real(now)
        render.ai_build = flaky                                                          # ai_data() is not guarded: the loop is
        frames, _ = self.run_main([b"a", render.REFRESH_S, b"\x1b", b"a", b"\x1b"])      # check_frame: no raw escape on screen
        error = lambda f: " │ AI │ " in f[0] and "error on the AI screen" in f[1]  # noqa: E731
        self.assertEqual([error(f) for f in frames], [False, True, False, False, False, False])
        self.assertIn("ValueError('state unreadable", frames[1][1])
        self.assertEqual(frames[1][-1].strip(), "a/Esc back")
        self.assertNotIn("ALL OK", frames[1][0])                                         # nothing could be read: never a reassuring status

    def test_a_broken_screen_does_not_say_all_ok(self):
        def broken(*a, **kw):
            raise ValueError("boom")
        render.ai_screen = broken
        frames, _ = self.run_main([b"a", b"\x1b"])
        self.assertIn("PROBLEMS", frames[0][0])
        self.assertIn("error on the AI screen", frames[1][1])
        self.assertNotIn("ALL OK", frames[1][0])

    def test_no_catalog_and_a_narrow_console(self):
        render.ai_build = lambda now: {"cat": None, "msg": "the model catalog could not be read: x", "err": True, "at": now}
        frames, _ = self.run_main([b"a", b"\x1b[B", b"\r", b"\x1b"], cols=80, rows=24)
        ai = [f for f in frames if " │ AI │ " in f[0]]
        self.assertEqual(len(ai), 3)
        for f in ai:
            self.assertIn("the model catalog could not be read: x", "\n".join(f))
            self.assertIn("no models", f[-1])


class Hostile(AiCase):
    """Names, notes, commands and texts come from catalog files, drivers and the machine: control characters and escape sequences stay inert."""

    def hostile(self):
        e, wide, fmt = EVIL, CJK, FORMAT

        def change(c):
            ms = c["models"]
            ms[0].update(id="x" * 500, name=e + wide + fmt + "<b>x</b>", notes=e * 50, license=e, quant=wide)
            ms[0]["assess"].update(why=e + " " * 300 + wide, where=e, need_mb=float("nan"), tok_s=[float("inf"), "x"], gpu_layers="many")
            ms[1].update(approx_mb="big", params_b=None, rank="first", layers=None, ctx_max=True, name=wide * 40)
            ms[1]["assess"] = None
            ms[2]["commands"] = {"install": e, "use": 5, "remove": ["x"]}
            ms[2].update(installed=False, pinned=True)
            ms[3]["assess"].update(verdict=e)
            ms.insert(3, "not a dict")
            ms.insert(4, {"id": 5})
            ms.insert(5, {"id": ms[2]["id"]})
            ms.insert(6, {"id": ""})
            c["hw"] = {"os": e, "arch": wide, "cpu": {"model": e + wide, "cores": True, "threads": float("nan"), "flags": [e, 5, None, "avx2"]},
                       "gpus": [{"name": e, "vram_mb": "x", "vram_free_mb": float("nan"), "backend": e}, "no", {"name": wide, "unified": True},
                                {"name": fmt, "vram_mb": 10 ** 12, "vram_free_mb": -5, "backend": "cuda"}] * 2,
                       "ram": {"total_mb": 0, "available_mb": 5}, "notes": [e * 5, 7, None, wide] * 3}
            c["runtime"] = {"installed": "yes", "version": e}
            c["dir"] = e
            c["recommended"] = ms[0]["id"]
            c["active"] = e
        return catalog(change)

    def test_control_characters_never_reach_the_console(self):
        self.seed(self.hostile(), {"enabled": 1, "endpoint": EVIL, "model": EVIL + CJK, "probe": {"state": "down", "msg": EVIL + CJK, "models": [EVIL, 5, None]}})
        for cols, rows in SIZES:
            for opts in ([], ["--details"], ["--select", "xxx", "--details"], ["--select", "m-ram", "--details"], ["--select", "evil", "--details"]):
                with self.subTest(size=(cols, rows), opts=opts):
                    s, lines = self.screen(opts, cols, rows)                              # check_frame: no control character, nothing too wide
                    for bad in (ESC + "[2J", ESC + "]", BEL, CSI8, "\u202e", CJK):
                        self.assertNotIn(bad, s)
        s, lines = self.screen(["--select", "x" * 60, "--details"], 200, 50)
        self.assertIn("?[2J?]0;pwn?evil?1m", "\n".join(lines))                           # visible, but plain text

    def test_every_size_and_cursor_position_of_a_hostile_catalog(self):
        cat = self.hostile()
        self.seed(cat)
        rows = render.ai_rows(cat)
        for cols, rs in SIZES + ((40, 10), (110, 24), (140, 40), (300, 80)):
            av = render.AiView(now=NOW)
            for details in (False, True):
                av.details = details
                for i in range(len(rows)):
                    with self.subTest(size=(cols, rs), details=details, model=i):
                        frame, _ = render.ai_screen(render.ai_data(), [], av, cols - 1, rs)
                        self.check_frame(frame, cols - 1, rs)
                    render.ai_key(av, "down", rows)

    def test_a_catalog_of_the_wrong_shape_is_an_empty_screen_not_a_crash(self):
        for bad in ({}, {"hw": None, "models": None}, {"hw": [], "models": [None]}, {"hw": {"gpus": "no", "notes": "no", "cpu": 3, "ram": []}, "runtime": 5},
                    {"models": [{"id": "a", "assess": {"tok_s": [1]}, "commands": 5}]}):
            render._AI.clear()
            self.seed(bad)
            for cols, rows in SIZES:
                for opts in ([], ["--details"]):
                    self.screen(opts, cols, rows)

    def test_the_text_helpers_clean_what_they_are_given(self):
        self.assertEqual(render.ai_id(None), None)
        self.assertEqual(render.ai_id(5), None)
        self.assertEqual(render.ai_id("  "), None)
        self.assertEqual(render.ai_id("a\x00b"), "a?b")
        self.assertEqual(render.ai_id("x" * 100), "x" * 63 + "…")


class Advisor(AiCase):
    """The advisor's CACHED answer in the HEALTH screen: ADVICE under the findings, never a generation, every text cleaned."""

    RESULT = {"text": "1. restart shop-worker-1 with a memory limit [oom:shop-worker-1]\n2. look at the disk /data\n" + "more words " * 60,
              "model": "qwen3-4b", "at": NOW, "cites": ["oom:shop-worker-1"]}

    def setUp(self):
        super().setUp()
        self.rep = demo.health_report(None, 7, NOW)
        render.CFG["ai"].update(enabled=True, endpoint="http://127.0.0.1:11434/v1", allow_remote=False)
        self.calls = []
        render._ADVICE.clear()
        patch = mock.patch.object(advisor, "try_advise", self.try_advise)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(advisor, "available", lambda cfg: (bool(cfg["ai"]["enabled"]), "[ai] enabled = no in config.ini"))
        patch.start()
        self.addCleanup(patch.stop)
        self.cached = dict(self.RESULT)

    def try_advise(self, report, cfg, cached_only=False):
        self.calls.append((report["period"]["days"], cached_only))
        return self.cached

    def test_off_there_is_no_block(self):
        render.CFG["ai"]["enabled"] = False
        self.assertEqual(render.health_extra_lines(self.rep, 120), [])
        self.assertEqual(self.calls, [])                                                                 # and the advisor is not even asked

    def test_the_cached_answer_is_the_block_and_the_model_is_never_asked(self):
        lines = render.health_extra_lines(self.rep, 100)
        plain = [render.ANSI.sub("", x) for x in lines]
        self.assertLessEqual(len(lines), render.ADVICE_LINES)
        self.assertEqual(plain[0], " ADVICE (AI, qwen3-4b) — check before acting")                       # who wrote it, and the warning
        self.assertEqual(plain[1], " 1. restart shop-worker-1 with a memory limit [oom:shop-worker-1]")
        self.assertRegex(plain[-1], r"^ … \+\d+ more lines: nuc-console-ask --advise$")
        self.assertTrue(all(len(x) <= 100 for x in plain))
        self.assertEqual(self.calls, [(7, True)])                                                        # cached_only=True: a screen never waits for a generation
        with mock.patch.object(advisor, "advise", side_effect=AssertionError("a generation on a request path")):
            render._ADVICE.clear()
            render.health_extra_lines(self.rep, 100)

    def test_nothing_cached_yet_is_a_hint_how_to_ask(self):
        self.cached = None
        lines = render.health_extra_lines(self.rep, 100)
        plain = " ".join(render.ANSI.sub("", x).strip() for x in lines)
        self.assertIn("no advice yet: nuc-console-ask --advise asks the local model", plain)
        self.assertLessEqual(len(lines), 3)
        self.assertTrue(all(ESC + "[90m" in x for x in lines))

    def test_an_error_result_is_shown_as_the_advisor_words_it(self):
        self.cached = {"error": "the model server is busy" + ESC + "[2J", "busy": True}
        plain = " ".join(render.ANSI.sub("", x) for x in render.health_extra_lines(self.rep, 100))
        self.assertIn("ADVICE (AI) — not available: the model server is busy", plain)
        self.assertNotIn(ESC + "[2J", plain)

    def test_the_answer_is_looked_up_once_per_report_for_a_while(self):
        for _ in range(20):
            render.health_extra_lines(self.rep, 100)
        self.assertEqual(len(self.calls), 1)                                                             # a key or a frame does not read the cache file
        self.now += render.ADVICE_TTL + 1
        render.health_extra_lines(self.rep, 100)
        self.assertEqual(len(self.calls), 2)
        render.health_extra_lines(dict(self.rep), 100)                                                   # another report (the next minute's): asked again
        self.assertEqual(len(self.calls), 3)

    def test_a_broken_advisor_is_no_advice_and_no_crash(self):
        with mock.patch.object(advisor, "try_advise", side_effect=RuntimeError("boom")):
            self.assertEqual(render.health_extra_lines(self.rep, 100), [])
        render._ADVICE.clear()
        with mock.patch.dict(sys.modules, {"advisor": None}):                                           # not even importable
            self.assertEqual(render.health_extra_lines(self.rep, 100), [])
            self.assertEqual(render.health_advice(self.rep), (False, None))

    def test_hostile_text_from_the_model_is_inert(self):
        self.cached = dict(self.RESULT, text=EVIL + " " + CJK + " " + FORMAT + "\n" + "x" * 600, model=EVIL, cites=[EVIL])
        for w in (30, 78, 119, 199):
            render._ADVICE.clear()
            lines = render.health_extra_lines(self.rep, w)
            plain = "".join(render.ANSI.sub("", x) for x in lines)
            self.assertIsNone(CONTROL.search(SGR.sub("", "".join(lines))), w)
            for bad in (ESC, BEL, CSI8, "\u202e", CJK):
                self.assertNotIn(bad, SGR.sub("", "".join(lines)))
            self.assertIn("evil", plain)

    def test_advice_goes_under_the_findings_and_over_the_sections_of_the_health_screen(self):
        render.DEMO_HEALTH = ""
        render._HEALTH.clear()
        for cols, rows in SIZES:
            with self.subTest(size=(cols, rows)):
                s = render.health_once(["render.py", "--once", "--view", "health"], cols - 1, rows)
                lines = [render.ANSI.sub("", x) for x in s.split(LINE)]
                self.assertEqual(len(lines), rows)
                self.assertTrue(all(len(x) <= cols - 1 for x in lines))
                txt = "\n".join(lines)
                self.assertLess(txt.index("── FINDINGS"), txt.index("── ADVICE"))
                self.assertIn("ADVICE (AI, qwen3-4b) — check before acting", txt)
                if rows >= 33:
                    self.assertLess(txt.index("── ADVICE"), txt.index("── TOP CPU"))
        render._HEALTH.clear()


if __name__ == "__main__":
    unittest.main()
