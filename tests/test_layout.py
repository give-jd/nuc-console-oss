"""The code layout: one configuration dict per process, and the flat modules render.py's pieces were moved to
(ui.py, ansi.py, exposure.py): what each holds, and what they import."""
import ast
import os
import re
import sys
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import ansi  # noqa: E402
import exposure  # noqa: E402
import graph  # noqa: E402
import htmlview  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import ui  # noqa: E402


def imports_of(module):
    """The top-level names a module of src/ imports (``import x`` and ``from x import y`` anywhere in the file)."""
    with open(os.path.join(SRC, module + ".py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


class SharedConfig(unittest.TestCase):
    def test_current_is_loaded_once_and_always_the_same_object(self):
        self.assertIs(nuc_config.current(), nuc_config.current())

    def test_render_cfg_is_that_object(self):
        # the tests change render.CFG in place (~150 places): the other modules see it because it is one dict
        self.assertIs(render.CFG, nuc_config.current())
        render.CFG["spacing"], saved = 7, render.CFG["spacing"]
        try:
            self.assertEqual(nuc_config.current()["spacing"], 7)
        finally:
            render.CFG["spacing"] = saved

    def test_load_still_reads_the_file_again(self):
        a, b = nuc_config.load("/nonexistent"), nuc_config.load("/nonexistent")
        self.assertIsNot(a, b)
        self.assertIsNot(a, nuc_config.current())
        self.assertEqual(a, b)


DEFAULT_CODES = {"ok": "32", "warn": "33", "err": "31", "unknown": "33", "info": "90", "muted": "90", "accent": "36", "strong": "1",
                 "banner_ok": "1;7", "banner_err": "1;41;37", "banner_warn": "1;43;30", "sel": "7", "accent_strong": "1;36",
                 "err_strong": "1;31", "neutral": "37"}


class Tokens(unittest.TestCase):
    def test_every_theme_says_every_token(self):
        for name, theme in ui.ANSI_THEMES.items():
            self.assertEqual(set(theme), set(ui.TOKENS), name)
        self.assertEqual(set(ui.ANSI_THEMES), {"default", "light", "hc", "mono"})
        for name, theme in ui.CSS_THEMES.items():
            self.assertEqual(set(theme), set(ui.CSS_THEMES["dark"]), name)
            for key, value in theme.items():
                self.assertRegex(value, r"^#[0-9a-f]{6}$", (name, key))
        self.assertEqual(set(ui.CSS_THEMES), {"dark", "light", "hc"})

    def test_the_default_theme_is_the_codes_the_console_always_wrote(self):
        self.assertEqual(ui.ANSI_THEMES["default"], DEFAULT_CODES)
        for token, code in DEFAULT_CODES.items():
            self.assertEqual(ui.sgr(token), code)
            self.assertEqual(ui.sgr(token, "default"), code)

    def test_light_moves_the_accent_from_cyan_to_blue(self):
        light = ui.ANSI_THEMES["light"]
        self.assertEqual((light["accent"], light["accent_strong"]), ("34", "1;34"))
        self.assertEqual({k for k in light if light[k] != DEFAULT_CODES[k]}, {"accent", "accent_strong", "neutral"})

    def test_high_contrast_is_bold_and_bright_only(self):
        for token in ("ok", "warn", "err", "unknown", "accent"):
            code = ui.sgr(token, "hc")
            self.assertTrue(code.startswith("1;9"), (token, code))

    def test_mono_has_no_colour_at_all(self):
        for token, code in ui.ANSI_THEMES["mono"].items():
            self.assertTrue(set(code.split(";")) <= {"", "1", "7"}, (token, code))  # nothing but bold and reverse video

    def test_dark_is_the_palette_the_web_page_writes_today(self):
        dark = ui.CSS_THEMES["dark"]
        css = {m.group(1): m.group(2) for m in re.finditer(r"\.(\w+)\{color:(#[0-9a-f]{6})\}", htmlview.PALETTE)}
        for cls, token in (("r", "err"), ("g", "ok"), ("y", "warn"), ("b", "link"), ("m", "magenta"), ("c", "accent"), ("w", "strong"),
                           ("d", "muted"), ("k", "bg")):
            self.assertEqual(dark[token], css[cls], (cls, token))
        self.assertIn(".bR{background:%s}" % dark["banner_err"], htmlview.PALETTE)
        self.assertIn(".bY{background:%s}" % dark["banner_warn"], htmlview.PALETTE)
        self.assertEqual((dark["bg"], dark["fg"]), ("#0d1117", "#c9d1d9"))

    def test_light_and_high_contrast_values(self):
        light, hc = ui.CSS_THEMES["light"], ui.CSS_THEMES["hc"]
        self.assertEqual({k: light[k] for k in ("ok", "warn", "err", "accent", "fg", "bg", "muted")},
                         {"ok": "#1a7f37", "warn": "#9a6700", "err": "#cf222e", "accent": "#0969da", "fg": "#1f2328", "bg": "#ffffff",
                          "muted": "#57606a"})
        self.assertEqual({k: hc[k] for k in ("ok", "warn", "err", "accent", "fg", "bg")},
                         {"ok": "#3dff6e", "warn": "#ffd400", "err": "#ff5c5c", "accent": "#00e1ff", "fg": "#ffffff", "bg": "#000000"})


class Primitives(unittest.TestCase):
    """What ansi.py draws, byte for byte (the golden dumps of the whole screens say the same, at a larger scale)."""

    def test_colour_and_width(self):
        self.assertEqual(ansi.c(31, "x"), "\x1b[31mx\x1b[0m")
        self.assertEqual(ansi.c("1;36", "x"), "\x1b[1;36mx\x1b[0m")
        self.assertEqual((ansi.cc("", "x"), ansi.cc(0, "x"), ansi.cc("90", "x")), ("x", "x", "\x1b[90mx\x1b[0m"))
        self.assertEqual(ansi.vlen(ansi.c(31, "abc") + "de"), 5)
        self.assertEqual(ansi.pad(ansi.c(31, "ab"), 4), "\x1b[31mab\x1b[0m  ")
        self.assertEqual(ansi.clip(ansi.c(31, "abcdef") + "gh", 3), "\x1b[31mabc\x1b[0m")

    def test_section_and_messages(self):
        self.assertEqual(ansi.section("T", 20), "\x1b[36m──\x1b[0m\x1b[1;36m T \x1b[0m\x1b[36m" + "─" * 13 + "\x1b[0m")
        self.assertEqual(ansi.section("T", 20, "n"), "\x1b[36m──\x1b[0m\x1b[1;36m T \x1b[0m\x1b[36m" + "─" * 10 + "\x1b[0m\x1b[90m  n\x1b[0m")
        for level, sym, code in (("err", "✖", 31), ("warn", "!", 33), ("ok", "✔", 32), ("info", "·", 90)):
            self.assertEqual(ansi.msg(level, "t"), f"   \x1b[{code}m{sym}\x1b[0m t")
        self.assertRaises(KeyError, ansi.msg, "nope", "t")
        self.assertEqual(ansi.msg_wrap("warn", "aa, bb, cc", 14), [ansi.msg("warn", "aa, bb,"), "     cc"])
        self.assertEqual(ansi.kv("k", "v"), "   \x1b[90mk            \x1b[0mv")

    def test_bars_and_cells(self):
        self.assertEqual(ansi.bar(0.5, 4), "\x1b[32m██\x1b[0m\x1b[90m░░\x1b[0m")
        self.assertEqual(ansi.bar(0.8, 4), "\x1b[33m███\x1b[0m\x1b[90m░\x1b[0m")
        self.assertEqual(ansi.bar(1.5, 2), "\x1b[31m██\x1b[0m\x1b[90m\x1b[0m")
        self.assertEqual(ansi.sparkline([0, 2048], 3), "\x1b[90m▁\x1b[0m▁█")
        self.assertEqual([ansi.cell(v) for v in (0, 1, 2, 3)], ["\x1b[90m·\x1b[0m", "\x1b[33m●\x1b[0m", "\x1b[36m◐\x1b[0m", "\x1b[33m?\x1b[0m"])
        self.assertEqual(ansi.cell(1, warn=True), "\x1b[31m●\x1b[0m")
        self.assertEqual((ansi.cell(1, net=True), ansi.cell(0, net=True)), ("\x1b[1;31m●\x1b[0m", "\x1b[90m·\x1b[0m"))
        self.assertEqual((ansi.cell(1, loc=True), ansi.cell(0, loc=True)), ("\x1b[37m●\x1b[0m", "\x1b[90m·\x1b[0m"))

    def test_status_pill_codes_come_from_the_tokens(self):
        self.assertEqual(render.status_pill([]), ("✔ ALL OK", "1;7"))
        self.assertEqual(render.status_pill([(2, "x")]), ("✖ 1 PROBLEMS", "1;41;37"))
        self.assertEqual(render.status_pill([(1, "x")]), ("! 1 WARNINGS", "1;43;30"))
        self.assertEqual(render.status_pill([(3, "x")]), ("✖ EXPOSED PORTS CHANGED", "1;41;37"))


class TextHelpers(unittest.TestCase):
    def test_safe_and_plural(self):
        self.assertEqual(ui.safe("a\x1b[31mb\n\x9bc"), "a?[31mb??c")
        self.assertEqual((ui.plural(1, "rule"), ui.plural(2, "rule"), ui.plural(0, "rule")), ("1 rule", "2 rules", "0 rules"))

    def test_the_human_formatters(self):
        self.assertEqual((ui.human(None), ui.human(5 * 2 ** 20), ui.human(3 * 2 ** 30)), ("-", "5M", "3.0G"))
        self.assertEqual((ui.fmt_dur(3 * 86400 + 7200), ui.fmt_dur(3700), ui.fmt_min(30), ui.fmt_min(90)), ("3d 2h", "1h 1m", "30 s", "1.5 min"))
        self.assertEqual((ui.fmt_ago(-5), ui.fmt_ago(30), ui.fmt_ago(600), ui.fmt_ago(7200)), ("0 s", "30 s", "10 min", "2 h"))
        self.assertEqual((ui.fmt_rate(12), ui.fmt_rate(1500), ui.fmt_rate(2.5e6)), ("12 B/s", "1.5 kB/s", "2.5 MB/s"))
        self.assertEqual((ui.fmt_size(None), ui.fmt_size(512), ui.fmt_size(1536), ui.fmt_size(3 * 2 ** 30)), ("?", "512B", "1.5K", "3G"))
        self.assertEqual((ui.fmt_k(842), ui.fmt_k(18300), ui.fmt_k(1.2e6), ui.fmt_k(True)), ("842", "18.3k", "1.2M", "?"))
        self.assertEqual((ui.fmt_cputime(412.3), ui.fmt_cputime(18233), ui.fmt_cputime("x")), ("6:52.3", "5:03:53", "?"))
        self.assertEqual((ui.qf(3.14159, ".1f", "%"), ui.qf(None), ui.num(True), ui.num(float("nan"))), ("3.1%", "?", None, None))
        self.assertEqual((ui.hcount(999), ui.hcount(1500), ui.hcount(25000), ui.hcount(3e6), ui.hnum("x", None)), ("999", "1.5k", "25k", "3.0M", None))
        self.assertEqual((ui.hclean("a\x1bb\u4e2d", 0), ui.hclean("abcdef", 4)), ("a?b?", "abc…"))


class Moved(unittest.TestCase):
    """ui.py and ansi.py are the primitives: they import nothing of ours but each other."""

    def test_ui_and_ansi_import_nothing_of_ours_but_each_other(self):
        std = {"math", "re", "unicodedata"}
        self.assertEqual(imports_of("ui"), std)
        self.assertEqual(imports_of("ansi"), {"re", "textwrap", "ui"})


class ExposureModule(unittest.TestCase):
    """exposure.py is the model alone: it reads the configuration through nuc_config.current() and imports nothing of render.py."""

    def test_the_model_does_not_import_the_renderer(self):
        self.assertEqual(imports_of("exposure"), {"ipaddress", "re", "nuc_config", "ui"})
        graph_imports = imports_of("graph")
        self.assertIn("exposure", graph_imports)
        self.assertNotIn("render", graph_imports)
        self.assertFalse(hasattr(graph, "_render"))  # the callback into render.py is gone

    def test_the_map_and_the_console_use_the_same_functions(self):
        # graph.build() asks exposure for the rows, the [expose] verdicts and who is behind a row: nothing is looked up in render.py
        self.assertIs(graph.exposure, exposure)
        self.assertTrue(callable(exposure.row_owners) and callable(exposure.serve_by_port))

    def test_expose_and_webapps_are_read_from_the_shared_config_at_call_time(self):
        cfg = nuc_config.current()
        saved = cfg["expose"], cfg["webapps"]
        try:
            cfg["expose"], cfg["webapps"] = {"Shop-DB": "LOCALE", "8080": "LAN"}, {"shop": [8080]}
            self.assertEqual(exposure.expose_policy(), [("shop-db", None, "LOCALE"), ("8080", (8080, "tcp"), "LAN")])
            net = {"listeners": [{"port": 8080, "proto": "tcp", "addr": "0.0.0.0", "proc": "node"}]}
            self.assertEqual([(r["name"], r["state"], r["reach"]) for r in exposure.webapp_rows(net, None)], [("shop", "up", "LAN")])
            cfg["webapps"] = {}
            self.assertEqual([(r["name"], r["state"]) for r in exposure.webapp_rows(net, None)], [("node", "up")])
        finally:
            cfg["expose"], cfg["webapps"] = saved

    def test_the_feature_switches_are_read_from_the_shared_config_too(self):
        cur = {"22/t:LAN": {"name": "sshd", "lan": 1}, "80/t:LAN": {"name": "nginx", "lan": 1}}
        base = {"ports": {"22/t:LAN": {"name": "sshd", "lan": 1}, "80/t:LAN": {"name": "apache", "lan": 1}}}
        features = nuc_config.current()["features"]
        saved = features["containers"]
        try:
            features["containers"] = True
            self.assertEqual(exposure.baseline_diff(cur, base)[2], {"80/t:LAN": "service apache → nginx"})
            features["containers"] = False  # names cannot be resolved without the container collector: not a change
            self.assertEqual(exposure.baseline_diff(cur, base)[2], {})
        finally:
            features["containers"] = saved


if __name__ == "__main__":
    unittest.main()
