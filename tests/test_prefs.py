"""Tests for prefs.py, the preferences of the new interface: the cookie / ?ui= grammar, config.ini [ui], presets, precedence, export.

Pure functions only; nuc_config.load() is tested with temporary config files, and the docs against the code.
"""
import configparser
import contextlib
import io
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import nuc_config  # noqa: E402
import prefs  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
SPEC_EXAMPLE = "1.tl.dw.os.kpb_in_cp_rm.lat2_ex2_wa1_fw1_sy1_ct2_dbx"
ALL = list(nuc_config.SECTIONS)


def random_prefs(rnd):
    """A random valid partial prefs dict (what a cookie can hold)."""
    p = {}
    for field, _, codes in prefs._SCALARS:
        if rnd.random() < 0.6:
            p[field] = rnd.choice(list(codes))
    if rnd.random() < 0.6:
        p["kpis"] = rnd.sample(prefs.KPI_IDS, rnd.randint(1, prefs.MAX_KPIS))
    if rnd.random() < 0.7:
        cards = rnd.sample(ALL, rnd.randint(0, len(ALL)))
        cut = rnd.randint(0, len(cards))
        p["layout"] = [(c, rnd.randint(1, 4)) for c in cards[:cut]]
        p["hidden"] = cards[cut:]
    return p


def mutate(s, rnd):
    for _ in range(rnd.randint(1, 3)):
        i = rnd.randint(0, len(s))
        ch = rnd.choice("1._abcxz9;, =\"\u00e9\n")
        s = rnd.choice((s[:i] + ch + s[i:], s[:i] + s[i + 1:], s[:i] + ch + s[i + 1:]))
    return s


def load_ini(text):
    """nuc_config.load() of a temporary config.ini -> (cfg, what it printed on stderr)."""
    with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8") as f:  # Windows would write cp1252
        f.write(text)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            cfg = nuc_config.load(f.name)
    finally:
        os.unlink(f.name)
    return cfg, err.getvalue()


def ini_section(text, name="ui"):
    """The [ui] block as parse_ui gets it from nuc_config: the section's keys, read the way nuc_config reads the file."""
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"), strict=False)
    cp.read_string(text)
    return {k: cp.get(name, k) for k in cp.options(name)}


class Tables(unittest.TestCase):
    def test_the_codes_are_one_to_one(self):
        for name, table in (("theme", prefs.THEME_CODES), ("density", prefs.DENSITY_CODES), ("view", prefs.VIEW_CODES),
                            ("preset", prefs.PRESET_CODES), ("order", prefs.ORDER_CODES), ("kpi", prefs.KPI_CODES),
                            ("card", prefs.CARD_CODES)):
            with self.subTest(name):
                self.assertEqual(len(set(table.values())), len(table), "a code twice")
        self.assertEqual(set(prefs.THEME_CODES), set(prefs.THEMES))
        self.assertEqual(set(prefs.DENSITY_CODES), set(prefs.DENSITIES))
        self.assertEqual(set(prefs.VIEW_CODES), set(prefs.VIEWS))
        self.assertEqual(set(prefs.PRESET_CODES), set(prefs.PRESETS))
        self.assertEqual(set(prefs.ORDER_CODES), set(prefs.ORDERS))
        self.assertEqual(list(prefs.KPI_CODES), list(prefs.KPI_IDS))
        for table in (prefs.KPI_CODES, prefs.CARD_CODES):
            self.assertTrue(all(len(c) == 2 and c.isascii() and c.islower() and c.isalpha() for c in table.values()))
        for table in (prefs.THEME_CODES, prefs.DENSITY_CODES, prefs.VIEW_CODES, prefs.PRESET_CODES, prefs.ORDER_CODES):
            self.assertTrue(all(len(c) == 1 and c.isascii() and c.islower() for c in table.values()))

    def test_the_codes_are_the_ones_of_the_spec(self):
        self.assertEqual(prefs.THEME_CODES, {"auto": "a", "dark": "d", "light": "l", "high-contrast": "h"})
        self.assertEqual(prefs.DENSITY_CODES, {"wall": "w", "desk": "k", "compact": "c"})
        self.assertEqual(prefs.VIEW_CODES, {"overview": "o", "map": "m", "cpu": "c", "health": "h", "ai": "a"})
        self.assertEqual(prefs.PRESET_CODES, {"default": "n", "security": "s", "server": "v", "desktop": "d"})
        self.assertEqual(prefs.ORDER_CODES, {"severity": "s", "fixed": "f"})
        self.assertEqual(" ".join(prefs.KPI_CODES.values()), "pb in la be dl fw cp rm dk tp ld ct uh fu sh tn rx tx up hl ai")
        self.assertEqual(" ".join(prefs.KPI_IDS), "problems internet lan beyond db_lan firewall cpu ram disk temp load containers "
                         "unhealthy failed_units ssh tailnet rx tx uptime health ai")
        self.assertEqual(" ".join(prefs.CARD_CODES.values()), "at ex wa fw sy ct db bo nt se ts dd di")
        self.assertEqual((prefs.MAX_KPIS, prefs.COOKIE_MAX), (8, 256))

    def test_every_card_of_the_dashboard_has_a_code(self):
        self.assertEqual(prefs.CARDS, tuple(nuc_config.SECTIONS))
        self.assertEqual(set(prefs.CARD_CODES), set(nuc_config.SECTIONS), "a new section needs a card code in prefs.py")

    def test_ui_keys_are_the_documented_ones(self):
        self.assertEqual(prefs.UI_KEYS, ("web", "theme", "density", "start_view", "preset", "order", "kpis", "layout", "hidden"))


class CookieGrammar(unittest.TestCase):
    def test_the_example_of_the_spec(self):
        p = prefs.parse_cookie(SPEC_EXAMPLE)
        self.assertEqual(p["theme"], "light")
        self.assertEqual(p["density"], "wall")
        self.assertEqual(p["order"], "severity")
        self.assertEqual(p["kpis"], ["problems", "internet", "cpu", "ram"])
        self.assertEqual(p["layout"], [("attention", 2), ("exposure", 2), ("webapps", 1), ("firewall", 1), ("system", 1), ("containers", 2)])
        self.assertEqual(p["hidden"], ["databases"])
        self.assertNotIn("preset", p)
        self.assertEqual(prefs.dump_cookie(p), SPEC_EXAMPLE, "it is already canonical")

    def test_each_scalar_code(self):
        for field, letter, codes in prefs._SCALARS:
            for value, code in codes.items():
                with self.subTest(field=field, value=value):
                    self.assertEqual(prefs.parse_cookie("1." + letter + code), {field: value})
                    self.assertEqual(prefs.dump_cookie({field: value}), "1." + letter + code)

    def test_empty_and_version_only(self):
        self.assertEqual(prefs.parse_cookie("1"), {})
        self.assertEqual(prefs.dump_cookie({}), "1")
        self.assertEqual(prefs.dump_cookie(prefs.parse_cookie("1")), "1")

    def test_a_wrong_version_or_shape_ignores_the_whole_value(self):
        for bad in ("", "2.tl", "0.tl", "10.tl", "1.", ".tl", "1..tl", "1.tl.", "tl", "1tl", "1,tl", "1;tl", "1.TL", "1.t l", " 1.tl", "1.tl ",
                    "1.tl\n", "\n1.tl", "1.t", "1.-l", "1.t-", "1.tl.t=l", '1."tl"', "1.\\tl", "1.t%6c"):
            with self.subTest(bad=bad):
                self.assertEqual(prefs.parse_cookie(bad), {})

    def test_not_a_string(self):
        for bad in (None, 1, 1.5, b"1.tl", ["1.tl"], {"theme": "light"}, object()):
            with self.subTest(bad=bad):
                self.assertEqual(prefs.parse_cookie(bad), {})

    def test_non_ascii(self):
        for bad in ("1.t\u00e9", "1.tl.d\u00e9", "\uff11.tl", "1.tl\u200b", "1.\u0442l", "1.t\u2113", "1.tl.k\u0440b"):
            with self.subTest(bad=bad):
                self.assertEqual(prefs.parse_cookie(bad), {})

    def test_at_most_eight_fields(self):
        eight = "1.tl.dw.vm.pn.os.tl.dw.vm"  # every field but k and l, and three again: eight segments
        self.assertEqual(prefs.parse_cookie(eight), {"theme": "light", "density": "wall", "start_view": "map", "preset": "default", "order": "severity"})
        self.assertEqual(prefs.parse_cookie(eight + ".tl"), {}, "nine segments: the whole value is refused")

    def test_a_segment_has_at_most_121_characters(self):
        self.assertEqual(prefs.parse_cookie("1.dw.t" + "l" * 120), {"density": "wall"}, "121 characters: well formed, but not a theme")
        self.assertEqual(prefs.parse_cookie("1.dw.t" + "l" * 121), {}, "122: the whole value is refused")

    def test_at_most_256_bytes(self):
        def padded(ms):  # one k field per m, each m times the code pb: a valid value of a length we choose
            return "1" + "".join(".k" + "_".join(["pb"] * m) for m in ms)
        self.assertEqual((len(padded([40, 40, 4])), len(padded([40, 40, 2, 2]))), (256, 257))
        self.assertEqual(prefs.parse_cookie(padded([40, 40, 4])), {"kpis": ["problems"]})
        self.assertEqual(prefs.parse_cookie(padded([40, 40, 2, 2])), {}, "257: the whole value is refused")
        self.assertEqual(prefs.parse_cookie(padded([40, 40, 4]) + "." + "tl"), {})

    def test_an_invalid_field_is_ignored_and_the_others_are_kept(self):
        self.assertEqual(prefs.parse_cookie("1.tz.dw"), {"density": "wall"})
        self.assertEqual(prefs.parse_cookie("1.tl.dz.vm"), {"theme": "light", "start_view": "map"})
        self.assertEqual(prefs.parse_cookie("1.tl.dw.kxx"), {"theme": "light", "density": "wall"})
        self.assertEqual(prefs.parse_cookie("1.zz.tl.q9"), {"theme": "light"}, "unknown field letters")
        self.assertEqual(prefs.parse_cookie("1.tll.dw"), {"density": "wall"}, "a scalar takes one character")
        self.assertEqual(prefs.parse_cookie("1.t0.dw"), {"density": "wall"})

    def test_a_field_given_twice_the_last_valid_one_wins(self):
        self.assertEqual(prefs.parse_cookie("1.tl.td"), {"theme": "dark"})
        self.assertEqual(prefs.parse_cookie("1.td.tl.dw"), {"theme": "light", "density": "wall"})
        self.assertEqual(prefs.parse_cookie("1.tl.tz"), {"theme": "light"}, "an invalid repeat does not cancel a valid field")
        self.assertEqual(prefs.parse_cookie("1.kpb.kin")["kpis"], ["internet"])
        self.assertEqual(prefs.parse_cookie("1.lat2.lex3")["layout"], [("exposure", 3)])
        self.assertEqual(prefs.parse_cookie("1.lat2_dbx.lex3"), {"layout": [("exposure", 3)], "hidden": []}, "l sets layout and hidden together")

    def test_unknown_codes_invalidate_a_list_field(self):
        for bad in ("1.kpb_zz", "1.kzz", "1.kpb_", "1.k_pb", "1.kpb__in", "1.kp", "1.kpbb", "1.k1b", "1.kpb_PB"):
            with self.subTest(bad=bad):
                self.assertNotIn("kpis", prefs.parse_cookie(bad))
        for bad in ("1.lzz", "1.lat_zz", "1.lat5", "1.lat0", "1.lat12", "1.lat2y", "1.lat2xx", "1.lat_", "1.l_at", "1.latx2", "1.lat-"):
            with self.subTest(bad=bad):
                self.assertEqual(prefs.parse_cookie(bad), {})

    def test_widths(self):
        for w in "1234":
            self.assertEqual(prefs.parse_cookie("1.lat" + w)["layout"], [("attention", int(w))])
        self.assertEqual(prefs.parse_cookie("1.lat")["layout"], [("attention", 1)], "no digit: 1")
        self.assertEqual(prefs.dump_cookie({"layout": [("attention", 1)]}), "1.lat1", "canonical: the width is always written")

    def test_hidden_items(self):
        self.assertEqual(prefs.parse_cookie("1.lat_dbx"), {"layout": [("attention", 1)], "hidden": ["databases"]})
        self.assertEqual(prefs.parse_cookie("1.lat_db2x"), {"layout": [("attention", 1)], "hidden": ["databases"], "hidden_w": {"databases": 2}},
                         "a hidden card keeps its width")
        self.assertEqual(prefs.parse_cookie("1.lat_db1x"), {"layout": [("attention", 1)], "hidden": ["databases"]}, "1 is the default: not kept")
        self.assertEqual(prefs.parse_cookie("1.ldbx"), {"hidden": ["databases"]}, "no visible card: no layout")
        self.assertEqual(prefs.parse_cookie("1.lexx"), {"hidden": ["exposure"]}, "exx = exposure + x")
        self.assertEqual(prefs.parse_cookie("1.lsex_dbx_dix")["hidden"], ["databases", "sessions", "disks"], "the order of the sections")
        self.assertEqual(prefs.dump_cookie({"layout": [("attention", 2)], "hidden": ["disks", "databases"]}), "1.lat2_dbx_dix")

    def test_a_card_or_kpi_named_twice_counts_once_the_first_mention_wins(self):
        self.assertEqual(prefs.parse_cookie("1.kpb_in_pb")["kpis"], ["problems", "internet"])
        self.assertEqual(prefs.parse_cookie("1.lat2_ex2_at3")["layout"], [("attention", 2), ("exposure", 2)])
        self.assertEqual(prefs.parse_cookie("1.lat2_atx"), {"layout": [("attention", 2)], "hidden": []})
        self.assertEqual(prefs.parse_cookie("1.latx_at2"), {"hidden": ["attention"]})

    def test_at_most_eight_kpis(self):
        eight = "_".join(list(prefs.KPI_CODES.values())[:8])
        self.assertEqual(len(prefs.parse_cookie("1.k" + eight)["kpis"]), 8)
        nine = "_".join(list(prefs.KPI_CODES.values())[:9])
        self.assertNotIn("kpis", prefs.parse_cookie("1.k" + nine))
        self.assertEqual(prefs.parse_cookie("1.tl.k" + nine), {"theme": "light"})
        self.assertEqual(len(prefs.parse_cookie("1.k" + "_".join(["pb"] * 20))["kpis"]), 1, "repeats are not counted")

    def test_all_thirteen_cards_and_eight_kpis_fit_the_cookie(self):
        p = {"theme": "high-contrast", "density": "compact", "start_view": "health", "preset": "desktop", "order": "fixed",
             "kpis": list(prefs.KPI_IDS)[:8], "layout": [(c, 4) for c in ALL], "hidden": []}
        s = prefs.dump_cookie(p)
        self.assertLessEqual(len(s), prefs.COOKIE_MAX)
        self.assertEqual(prefs.parse_cookie(s), p)
        s = prefs.dump_cookie(dict(p, layout=[], hidden=ALL))
        self.assertLessEqual(len(s), prefs.COOKIE_MAX)
        self.assertEqual(prefs.parse_cookie(s)["hidden"], ALL)

    def test_cookie_safe_characters_only(self):
        rnd = random.Random(7)
        for _ in range(300):
            s = prefs.dump_cookie(random_prefs(rnd))
            self.assertRegex(s, r"\A[a-z0-9_.]+\Z")

    def test_dump_ignores_what_is_invalid(self):
        self.assertEqual(prefs.dump_cookie({"theme": "neon", "density": 3, "kpis": ["zzz", "problems", "problems"], "layout": [("nope", 2), "disks", ("sessions", 9)],
                                            "hidden": ["nope", "boot"], "other": 1}), "1.kpb.ldi1_se4_box")
        self.assertEqual(prefs.dump_cookie(None), "1")
        self.assertEqual(prefs.dump_cookie("1.tl"), "1")
        self.assertEqual(prefs.dump_cookie({"layout": 5, "kpis": "problems", "hidden": None}), "1")


class Canonical(unittest.TestCase):
    def test_equal_prefs_equal_string(self):
        a = prefs.dump_cookie(prefs.parse_cookie("1.os.tl.dw"))
        b = prefs.dump_cookie(prefs.parse_cookie("1.dw.tl.os"))
        c = prefs.dump_cookie({"order": "severity", "theme": "light", "density": "wall"})
        self.assertEqual(a, "1.tl.dw.os")
        self.assertEqual({a, b, c}, {"1.tl.dw.os"})

    def test_fields_in_the_canonical_order(self):
        self.assertEqual(prefs.dump_cookie(prefs.parse_cookie("1.lat2_dbx.kpb.os.pn.vm.dw.tl")), "1.tl.dw.vm.pn.os.kpb.lat2_dbx")

    def test_repeats_and_hidden_order_do_not_change_the_string(self):
        self.assertEqual(prefs.dump_cookie(prefs.parse_cookie("1.td.tl")), prefs.dump_cookie(prefs.parse_cookie("1.tl")))
        self.assertEqual(prefs.dump_cookie(prefs.parse_cookie("1.ldix_dbx")), prefs.dump_cookie(prefs.parse_cookie("1.ldbx_dix")))
        self.assertEqual(prefs.dump_cookie(prefs.parse_cookie("1.kpb_pb_in")), "1.kpb_in")

    def test_the_order_of_kpis_and_layout_is_kept(self):
        self.assertEqual(prefs.dump_cookie(prefs.parse_cookie("1.kcp_pb_in")), "1.kcp_pb_in")
        self.assertNotEqual(prefs.dump_cookie(prefs.parse_cookie("1.kcp_pb")), prefs.dump_cookie(prefs.parse_cookie("1.kpb_cp")))
        self.assertEqual(prefs.dump_cookie(prefs.parse_cookie("1.lex1_at2")), "1.lex1_at2")

    def test_round_trip_of_random_prefs(self):
        rnd = random.Random(2026)
        for _ in range(500):
            p = random_prefs(rnd)
            s = prefs.dump_cookie(p)
            back = prefs.parse_cookie(s)
            self.assertEqual(prefs.dump_cookie(back), s, p)
            want = prefs._clean(p)
            if not want.get("layout") and not want.get("hidden"):
                want.pop("hidden", None)  # the l field is not written at all: nothing to say
            self.assertEqual(back, want, p)
            self.assertLessEqual(len(s), prefs.COOKIE_MAX)

    def test_a_parsed_string_is_a_fixed_point(self):
        for s in (SPEC_EXAMPLE, "1", "1.td.tl", "1.kpb_pb", "1.lat2_atx", "1.tz.dw.kxx", "2.tl", "1.lat2_ex2_dbx_dix_sex"):
            once = prefs.dump_cookie(prefs.parse_cookie(s))
            self.assertEqual(prefs.dump_cookie(prefs.parse_cookie(once)), once, s)
            self.assertEqual(prefs.parse_cookie(once), prefs.parse_cookie(s), s)


class Fuzz(unittest.TestCase):
    def check(self, s):
        p = prefs.parse_cookie(s)
        self.assertIsInstance(p, dict)
        self.assertTrue(set(p) <= set(prefs.FIELDS))
        out = prefs.dump_cookie(p)
        self.assertRegex(out, r"\A1(\.[a-z][a-z0-9_]{1,120}){0,8}\Z")
        self.assertLessEqual(len(out), prefs.COOKIE_MAX)
        self.assertEqual(prefs.dump_cookie(prefs.parse_cookie(out)), out)
        self.assertEqual(prefs.parse_cookie(out), p)
        if not prefs.parse_cookie(s):
            self.assertEqual(out, "1")
        for field in ("reset", s, s[:5], "tl", "pn", "kpb_in", "lat2_dbx"):
            new = prefs.apply_set(s, field)
            self.assertEqual(prefs.apply_set(new, "reset"), "1")
            self.assertEqual(prefs.dump_cookie(prefs.parse_cookie(new)), new)

    def test_2000_random_strings_never_raise(self):
        rnd = random.Random(20261001)
        alphabet = "1.._abcdefghijklmnopqrstuvwxyz0123456789xX;, =\"'\\%\u00e9\u0442\u200b\n\t\x00"
        for _ in range(1000):
            self.check("".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 300))))
        for _ in range(1000):  # close to valid: a valid string with a few edits
            self.check(mutate(prefs.dump_cookie(random_prefs(rnd)), rnd))

    def test_random_bytes_and_odd_values(self):
        rnd = random.Random(5)
        for _ in range(200):
            blob = bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 80)))
            self.check(blob.decode("latin-1"))
            self.check(blob.decode("utf-8", "replace"))
        for odd in (None, 5, b"1.tl", [], {}, 1.5):
            self.assertEqual(prefs.parse_cookie(odd), {})
            self.assertEqual(prefs.apply_set(odd, "tl"), "1.tl")
            self.assertEqual(prefs.apply_set("1.tl", odd), "1.tl")

    def test_random_dicts_never_raise(self):
        rnd = random.Random(11)
        junk = [None, 0, 1, True, "x", "dark", "at", "problems", [], ["problems"], [("attention", 2)], [("attention",)], [(1, 2)], [["db", "x"]], {}, {"a": 1},
                ("attention", 2), 3.5, b"b"]
        for _ in range(500):
            d = {k: rnd.choice(junk) for k in rnd.sample(list(prefs.FIELDS) + ["web", "sections", "zz"], rnd.randint(0, 8))}
            prefs.dump_cookie(d)
            prefs.visible_cards(d, rnd.choice((None, ALL, ["attention", 3, None], "ab")))
            p, src = prefs.effective(d, d, d)
            self.assertEqual(set(p) - {"hidden_w"}, set(prefs.FIELDS))
            self.assertEqual(set(src), set(prefs.FIELDS))
            prefs.export_ini(d)


class ApplySet(unittest.TestCase):
    def test_a_field_replaces_that_field(self):
        self.assertEqual(prefs.apply_set("1", "tl"), "1.tl")
        self.assertEqual(prefs.apply_set("1.tl.dw", "td"), "1.td.dw")
        self.assertEqual(prefs.apply_set("1.tl", "dw"), "1.tl.dw")
        self.assertEqual(prefs.apply_set("1.tl.dw", "vm"), "1.tl.dw.vm")
        self.assertEqual(prefs.apply_set("1.tl.dw", "of"), "1.tl.dw.of")
        self.assertEqual(prefs.apply_set("1.kpb_in", "kcp_rm_dk"), "1.kcp_rm_dk")
        self.assertEqual(prefs.apply_set("1.lat2_ex2", "lse3_dix"), "1.lse3_dix")
        self.assertEqual(prefs.apply_set("1.tl", "lex3"), "1.tl.lex3")
        self.assertEqual(prefs.apply_set("1.tl.lat2_dbx", "lex3"), "1.tl.lex3", "the layout field carries the hidden cards too")
        self.assertEqual(prefs.apply_set("1.tl.lat2_dbx", "lat2_exx"), "1.tl.lat2_exx")

    def test_the_result_is_canonical(self):
        self.assertEqual(prefs.apply_set("1.os.dw.tl", "vm"), "1.tl.dw.vm.os")
        self.assertEqual(prefs.apply_set("1.td.tl", "ta"), "1.ta")
        self.assertEqual(prefs.apply_set("1.tl", "tl"), "1.tl")
        self.assertEqual(prefs.apply_set("1.kpb_in_pb", "kin_pb_in"), "1.kin_pb")

    def test_every_value_of_every_field(self):
        for field, letter, codes in prefs._SCALARS:
            for value, code in codes.items():
                with self.subTest(field=field, value=value):
                    self.assertEqual(prefs.parse_cookie(prefs.apply_set("1.kpb", letter + code))[field], value)

    def test_reset_clears_everything(self):
        self.assertEqual(prefs.apply_set(SPEC_EXAMPLE, "reset"), "1")
        self.assertEqual(prefs.apply_set("1", "reset"), "1")
        self.assertEqual(prefs.apply_set("garbage", "reset"), "1")

    def test_an_invalid_field_changes_nothing(self):
        for bad in ("", "x", "tz", "t", "dz", "kzz", "lzz", "lat9", "reset!", "RESET", "tl.dw", "tl;", "t l", "1.tl", "\u00e9", "ml", "k" + "_".join(list(prefs.KPI_CODES.values())[:9]),
                    "t" + "l" * 200, None, 5, b"tl"):
            with self.subTest(bad=bad):
                self.assertEqual(prefs.apply_set("1.td.dw", bad), "1.td.dw")
        self.assertEqual(prefs.apply_set("2.td", "tl"), "1.tl", "a current value that is invalid counts as empty")
        self.assertEqual(prefs.apply_set("garbage", "tl"), "1.tl")
        self.assertEqual(prefs.apply_set("garbage", "tz"), "1")
        self.assertEqual(prefs.apply_set(None, "dw"), "1.dw")

    def test_choosing_a_preset_drops_the_browsers_own_kpis_and_layout(self):
        self.assertEqual(prefs.apply_set(SPEC_EXAMPLE, "ps"), "1.tl.dw.ps.os")
        self.assertEqual(prefs.apply_set("1.tl.kpb.lat2", "pd"), "1.tl.pd")
        self.assertEqual(prefs.apply_set("1.kpb_in", "pn"), "1.pn")
        # other fields leave them alone
        self.assertEqual(prefs.apply_set("1.kpb_in.lat2", "tl"), "1.tl.kpb_in.lat2")

    def test_idempotent(self):
        for f in ("tl", "dw", "vm", "pn", "of", "kpb_in", "lat2_dbx"):
            once = prefs.apply_set(SPEC_EXAMPLE, f)
            self.assertEqual(prefs.apply_set(once, f), once)


class ParseUi(unittest.TestCase):
    def test_empty_section(self):
        ui, warns = prefs.parse_ui({}, None)
        self.assertEqual(ui, {"web": "app", "sections": ALL})
        self.assertEqual(warns, [])
        self.assertEqual(prefs.parse_ui(None, None)[0], ui)
        self.assertEqual(prefs.parse_ui("not a dict", 5)[0], ui)

    def test_every_key_with_valid_values(self):
        sec = {"web": "app", "theme": "high-contrast", "density": "wall", "start_view": "health", "preset": "server", "order": "fixed",
               "kpis": "problems, internet, lan", "layout": "attention:2, exposure:2, webapps, firewall, system, containers:2", "hidden": "sessions, docker_disk"}
        ui, warns = prefs.parse_ui(sec, ALL)
        self.assertEqual(warns, [])
        self.assertEqual(ui, {"web": "app", "sections": ALL, "theme": "high-contrast", "density": "wall", "start_view": "health", "preset": "server", "order": "fixed",
                              "kpis": ["problems", "internet", "lan"],
                              "layout": [("attention", 2), ("exposure", 2), ("webapps", 1), ("firewall", 1), ("system", 1), ("containers", 2)],
                              "hidden": ["sessions", "docker_disk"]})

    def test_every_allowed_value(self):
        for key, allowed in (("web", prefs.WEB_MODES), ("theme", prefs.THEMES), ("density", prefs.DENSITIES), ("start_view", prefs.VIEWS),
                             ("preset", prefs.PRESETS), ("order", prefs.ORDERS)):
            for v in allowed:
                with self.subTest(key=key, value=v):
                    ui, warns = prefs.parse_ui({key: v}, None)
                    self.assertEqual((ui[key], warns), (v, []))

    def test_case_and_blanks(self):
        ui, warns = prefs.parse_ui({"THEME": "  Dark ", "Density": "WALL", "kpis": " Problems ,CPU ", "layout": " Attention : 2 ,Exposure", "Hidden": "Sessions"}, None)
        self.assertEqual(warns, [])
        self.assertEqual((ui["theme"], ui["density"], ui["kpis"], ui["layout"], ui["hidden"]),
                         ("dark", "wall", ["problems", "cpu"], [("attention", 2), ("exposure", 1)], ["sessions"]))

    def test_blank_values_set_nothing_except_hidden(self):
        ui, warns = prefs.parse_ui({"theme": "", "density": "  ", "start_view": "", "kpis": "", "layout": "", "preset": "", "order": "", "web": "", "hidden": ""}, None)
        self.assertEqual(warns, [])
        self.assertEqual(ui, {"web": "app", "sections": ALL, "hidden": []})

    def test_bad_values_warn_and_are_not_set(self):
        ui, warns = prefs.parse_ui({"web": "new", "theme": "neon", "density": "huge", "start_view": "x", "preset": "all", "order": "random"}, None)
        self.assertEqual(ui, {"web": "app", "sections": ALL})
        self.assertEqual(len(warns), 6)
        for key, w in zip(("web", "theme", "density", "start_view", "preset", "order"), warns):
            self.assertTrue(w.startswith("[ui] %s must be one of" % key), w)

    def test_one_bad_value_does_not_cost_the_others(self):
        ui, warns = prefs.parse_ui({"theme": "neon", "density": "wall", "kpis": "problems"}, None)
        self.assertEqual((ui["density"], ui["kpis"], "theme" in ui, len(warns)), ("wall", ["problems"], False, 1))

    def test_unknown_keys_warn(self):
        ui, warns = prefs.parse_ui({"colour": "red", "theme": "dark"}, None)
        self.assertEqual(ui["theme"], "dark")
        self.assertEqual(len(warns), 1)
        self.assertIn("unknown key 'colour'", warns[0])

    def test_kpis(self):
        ui, warns = prefs.parse_ui({"kpis": "problems, nope, cpu, problems"}, None)
        self.assertEqual(ui["kpis"], ["problems", "cpu"])
        self.assertEqual(len(warns), 2)
        self.assertIn("nope", warns[0])
        ui, warns = prefs.parse_ui({"kpis": ", ".join(prefs.KPI_IDS)}, None)
        self.assertEqual(ui["kpis"], list(prefs.KPI_IDS)[:8])
        self.assertTrue(any("at most 8" in w for w in warns))
        ui, warns = prefs.parse_ui({"kpis": "nope, nada"}, None)
        self.assertNotIn("kpis", ui)
        self.assertEqual(len(warns), 3)
        for sep in ("problems cpu ram", "problems;cpu;ram", "problems,\n  cpu,\n  ram", "problems\tcpu , ram"):
            self.assertEqual(prefs.parse_ui({"kpis": sep}, None)[0]["kpis"], ["problems", "cpu", "ram"], repr(sep))

    def test_layout(self):
        ui, warns = prefs.parse_ui({"layout": "attention:2, nope, exposure:9, webapps:0, firewall:x, system:, system, containers:3"}, None)
        self.assertEqual(ui["layout"], [("attention", 2), ("exposure", 4), ("webapps", 1), ("firewall", 1), ("system", 1), ("containers", 3)])
        text = "\n".join(warns)
        for part in ("unknown card 'nope'", "exposure:9", "webapps:0", "firewall:x", "system:", "listed twice"):
            self.assertIn(part, text)
        ui, warns = prefs.parse_ui({"layout": "nope"}, None)
        self.assertNotIn("layout", ui)
        self.assertEqual(len(warns), 2)
        self.assertEqual(prefs.parse_ui({"layout": "attention:2\n  exposure:2\n  disks"}, None)[0]["layout"], [("attention", 2), ("exposure", 2), ("disks", 1)])

    def test_hidden(self):
        ui, warns = prefs.parse_ui({"hidden": "docker_disk, nope, sessions, sessions"}, None)
        self.assertEqual(ui["hidden"], ["sessions", "docker_disk"], "in the order of the sections, once each")
        self.assertEqual(len(warns), 1)
        self.assertEqual(prefs.parse_ui({"hidden": "nope"}, None)[0]["hidden"], [], "set, and nothing valid: nothing is hidden")

    def test_a_card_in_layout_and_hidden_is_hidden(self):
        ui, warns = prefs.parse_ui({"layout": "attention:2, sessions, disks", "hidden": "sessions"}, None)
        self.assertEqual(ui["layout"], [("attention", 2), ("disks", 1)])
        self.assertEqual(ui["hidden"], ["sessions"])
        self.assertEqual(len(warns), 1)
        self.assertIn("also in hidden", warns[0])
        ui, _ = prefs.parse_ui({"layout": "sessions", "hidden": "sessions"}, None)
        self.assertNotIn("layout", ui)

    def test_sections_default_is_kept_and_completed(self):
        ui, _ = prefs.parse_ui({}, ["system", "attention"])
        self.assertEqual(ui["sections"], ["system", "attention"] + [c for c in ALL if c not in ("system", "attention")])
        ui, _ = prefs.parse_ui({}, ["system", "nope", "system", 3])
        self.assertEqual(ui["sections"], ["system"] + [c for c in ALL if c != "system"])

    def test_values_that_are_not_strings(self):
        ui, warns = prefs.parse_ui({"theme": None, "density": 5, "kpis": None, 7: "x", None: "y"}, None)
        self.assertEqual(ui, {"web": "app", "sections": ALL})
        self.assertTrue(warns)

    def test_random_sections_never_raise(self):
        rnd = random.Random(3)
        words = list(prefs.THEMES) + list(prefs.KPI_IDS) + ALL + ["", ":", "::2", "a:b:c", ",", ";;", "\n", "x" * 500, "\u00e9", "attention:99999999999999999999",
                                                                  "attention:-1", "attention:\u0661", "  ", "\x00"]
        for _ in range(500):
            sec = {rnd.choice(list(prefs.UI_KEYS) + ["zz"]): rnd.choice((" ".join(rnd.choice(words) for _ in range(rnd.randint(0, 20))),
                                                                          ",".join(rnd.choice(words) for _ in range(rnd.randint(0, 20)))))
                   for _ in range(rnd.randint(0, 9))}
            ui, warns = prefs.parse_ui(sec, rnd.sample(ALL, rnd.randint(0, 13)))
            self.assertTrue(all(isinstance(w, str) and w.startswith("[ui] ") for w in warns))
            self.assertEqual(prefs._clean(ui), {k: v for k, v in ui.items() if k in prefs.FIELDS or k == "hidden_w"}, "what it returns is already clean")
            prefs.effective(ui)


class Presets(unittest.TestCase):
    def test_default(self):
        p = prefs.preset_prefs("default")
        self.assertEqual(p["layout"], [(c, 2 if c in ("attention", "exposure") else 1) for c in nuc_config.SECTIONS])
        self.assertEqual(p["kpis"], "problems internet lan beyond cpu ram disk temp".split())
        self.assertEqual(p["hidden"], [])

    def test_security(self):
        p = prefs.preset_prefs("security")
        self.assertEqual(p["layout"], [("attention", 2), ("exposure", 2), ("firewall", 1), ("webapps", 1), ("databases", 1), ("sessions", 1), ("tailscale", 1),
                                       ("containers", 1), ("system", 1), ("boot", 1), ("disks", 1), ("network_traffic", 1), ("docker_disk", 1)])
        self.assertEqual(p["kpis"], "problems internet lan beyond containers health".split())
        self.assertEqual(p["hidden"], [])

    def test_server(self):
        p = prefs.preset_prefs("server")
        self.assertEqual(p["layout"], [("attention", 2), ("system", 2), ("containers", 1), ("databases", 1), ("disks", 1), ("docker_disk", 1), ("boot", 1),
                                       ("exposure", 2), ("webapps", 1), ("firewall", 1), ("network_traffic", 1), ("sessions", 1), ("tailscale", 1)])
        self.assertEqual(p["kpis"], "problems cpu ram disk temp containers load health".split())
        self.assertEqual(p["hidden"], [])

    def test_desktop(self):
        p = prefs.preset_prefs("desktop")
        self.assertEqual(p["layout"], [("attention", 2), ("system", 2), ("disks", 1), ("network_traffic", 1), ("sessions", 1), ("exposure", 2), ("firewall", 1),
                                       ("boot", 1)])
        self.assertEqual(p["kpis"], "problems cpu ram disk temp ai".split())
        self.assertEqual(p["hidden"], ["webapps", "containers", "databases", "tailscale", "docker_disk"], "a set: kept in the order of the sections")

    def test_every_preset_places_every_card_exactly_once(self):
        for name in prefs.PRESETS:
            with self.subTest(name):
                p = prefs.preset_prefs(name)
                shown = [c for c, _ in p["layout"]]
                self.assertEqual(sorted(shown + p["hidden"]), sorted(ALL))
                self.assertEqual(len(set(shown)), len(shown))
                self.assertTrue(0 < len(p["kpis"]) <= prefs.MAX_KPIS)
                self.assertTrue(set(p["kpis"]) <= set(prefs.KPI_IDS))
                self.assertTrue(all(1 <= w <= 4 for _, w in p["layout"]))
                self.assertEqual(prefs._clean(p), p, "a preset is canonical data")

    def test_the_default_preset_follows_dashboard_sections(self):
        p = prefs.preset_prefs("default", ["system", "attention"])
        self.assertEqual(p["layout"][:2], [("system", 1), ("attention", 2)])
        self.assertEqual([c for c, _ in p["layout"]], ["system", "attention"] + [c for c in ALL if c not in ("system", "attention")])
        for name in ("security", "server", "desktop"):
            self.assertEqual(prefs.preset_prefs(name, ["system"]), prefs.preset_prefs(name), "the other presets bring their own order")

    def test_an_unknown_preset_is_the_default(self):
        self.assertEqual(prefs.preset_prefs("nope"), prefs.preset_prefs("default"))

    def test_the_result_is_a_copy(self):
        prefs.preset_prefs("desktop")["hidden"].append("x")
        prefs.preset_prefs("desktop")["kpis"].append("x")
        self.assertEqual(prefs.preset_prefs("desktop")["kpis"], "problems cpu ram disk temp ai".split())


class Effective(unittest.TestCase):
    def test_nothing_set_is_the_default_preset(self):
        for args in ((), (None,), ({},), ({"web": "app", "sections": ALL}, "", None), (prefs.parse_ui({}, ALL)[0], "1", "1")):
            p, src = prefs.effective(*args)
            self.assertEqual(p, dict(prefs.DEFAULTS, **prefs.preset_prefs("default")), args)
            self.assertEqual(set(src.values()), {"default"})
            self.assertEqual(set(src), set(prefs.FIELDS))

    def test_precedence_url_over_cookie_over_config_over_preset_over_default(self):
        ui = {"theme": "dark", "density": "compact", "start_view": "map", "preset": "server", "order": "fixed"}
        p, src = prefs.effective(ui, "1.tl.dw.vc", "1.ta")
        self.assertEqual((p["theme"], src["theme"]), ("auto", "url"))
        self.assertEqual((p["density"], src["density"]), ("wall", "browser"))
        self.assertEqual((p["start_view"], src["start_view"]), ("cpu", "browser"))
        self.assertEqual((p["order"], src["order"]), ("fixed", "config.ini"))
        self.assertEqual((p["preset"], src["preset"]), ("server", "config.ini"))
        # kpis, layout and hidden come from the preset of the config
        self.assertEqual((p["kpis"], src["kpis"]), (prefs.preset_prefs("server")["kpis"], "preset"))
        self.assertEqual((p["layout"], src["layout"]), (prefs.preset_prefs("server")["layout"], "preset"))
        self.assertEqual(src["hidden"], "preset")

    def test_each_layer_alone(self):
        for layer, name in ((dict(oneshot="1.dw"), "url"), (dict(cookie="1.dw"), "browser"), (dict(cfg_ui={"density": "wall"}), "config.ini")):
            p, src = prefs.effective(**layer)
            self.assertEqual((p["density"], src["density"]), ("wall", name))
            self.assertEqual(src["theme"], "default")

    def test_the_url_beats_the_cookie_field_by_field(self):
        p, src = prefs.effective({}, "1.dc.tl.kpb", "1.dw.pn")
        self.assertEqual((p["density"], src["density"]), ("wall", "url"))
        self.assertEqual((p["theme"], src["theme"]), ("light", "browser"))
        self.assertEqual((p["kpis"], src["kpis"]), (["problems"], "browser"))
        self.assertEqual((p["preset"], src["preset"]), ("default", "url"))

    def test_strings_or_parsed_dicts(self):
        a = prefs.effective({}, "1.tl.kpb", "1.dw")
        b = prefs.effective({}, prefs.parse_cookie("1.tl.kpb"), prefs.parse_cookie("1.dw"))
        self.assertEqual(a, b)
        self.assertEqual(prefs.effective({}, "garbage", "2.tl"), prefs.effective({}))

    def test_a_cookie_that_is_invalid_changes_nothing(self):
        self.assertEqual(prefs.effective({"theme": "dark"}, "1.tl.", None), prefs.effective({"theme": "dark"}))
        self.assertEqual(prefs.effective({"theme": "dark"}, "x" * 500), prefs.effective({"theme": "dark"}))

    def test_a_chosen_preset_gives_its_layout_kpis_and_hidden(self):
        for name in ("security", "server", "desktop"):
            p, src = prefs.effective({"preset": name})
            want = prefs.preset_prefs(name)
            self.assertEqual((p["layout"], p["kpis"], p["hidden"]), (want["layout"], want["kpis"], want["hidden"]))
            self.assertEqual((src["preset"], src["layout"], src["kpis"], src["hidden"]), ("config.ini", "preset", "preset", "preset"))
        p, src = prefs.effective({}, "1.pd")
        self.assertEqual((src["preset"], src["layout"]), ("browser", "preset"))
        p, src = prefs.effective({}, None, "1.ps")
        self.assertEqual((p["preset"], src["preset"], src["kpis"]), ("security", "url", "preset"))

    def test_an_explicit_layout_or_kpis_override_the_presets(self):
        ui = {"preset": "desktop", "kpis": ["problems", "ai"], "layout": [("disks", 3), ("attention", 1)]}
        p, src = prefs.effective(ui)
        self.assertEqual((p["kpis"], src["kpis"]), (["problems", "ai"], "config.ini"))
        self.assertEqual((p["layout"], src["layout"]), ([("disks", 3), ("attention", 1)], "config.ini"))
        self.assertEqual((src["hidden"], p["hidden"]), ("preset", prefs.preset_prefs("desktop")["hidden"]), "hidden is its own field")
        # only kpis
        p, src = prefs.effective({"preset": "server", "kpis": ["ai"]})
        self.assertEqual((p["kpis"], src["kpis"], src["layout"]), (["ai"], "config.ini", "preset"))

    def test_hidden_and_layout_are_independent_fields(self):
        p, src = prefs.effective({"preset": "security", "hidden": ["sessions"]})
        self.assertEqual((p["hidden"], src["hidden"], src["layout"]), (["sessions"], "config.ini", "preset"))
        self.assertNotIn("sessions", [c for c, _ in p["layout"]], "layout never holds a hidden card")
        self.assertNotIn("sessions", [c for c, _ in prefs.visible_cards(p)])
        p, src = prefs.effective({"preset": "desktop", "hidden": []})
        self.assertEqual((p["hidden"], src["hidden"]), ([], "config.ini"), "explicitly nothing hidden beats the preset")
        self.assertEqual(len(prefs.visible_cards(p)), 13)

    def test_a_card_a_stronger_layout_names_is_not_hidden_by_a_weaker_hidden(self):
        p, src = prefs.effective({"preset": "desktop", "layout": [("containers", 2), ("attention", 1)]})
        self.assertEqual((src["layout"], src["hidden"]), ("config.ini", "preset"))
        self.assertNotIn("containers", p["hidden"])
        self.assertEqual(p["hidden"], ["webapps", "databases", "tailscale", "docker_disk"])
        self.assertEqual(prefs.visible_cards(p)[:2], [("containers", 2), ("attention", 1)])
        # the other way round: a hidden list of the config beats the preset's layout
        p, _ = prefs.effective({"preset": "security", "hidden": ["firewall"]})
        self.assertNotIn("firewall", [c for c, _ in prefs.visible_cards(p)])
        # a cookie's layout against a config.ini hidden: the cookie's l sets both, so config.ini's hidden does not apply
        p, src = prefs.effective({"hidden": ["sessions"]}, "1.lat2_ex2")
        self.assertEqual((p["hidden"], src["hidden"], src["layout"]), ([], "browser", "browser"))

    def test_a_cookie_with_only_hidden_cards_keeps_the_layout_below(self):
        p, src = prefs.effective({"preset": "server"}, "1.ldbx")
        self.assertEqual((p["hidden"], src["hidden"]), (["databases"], "browser"))
        self.assertEqual((p["layout"], src["layout"]), ([(c, w) for c, w in prefs.preset_prefs("server")["layout"] if c != "databases"], "preset"))
        self.assertNotIn("databases", [c for c, _ in prefs.visible_cards(p)])

    def test_the_cookie_layout_sets_layout_and_hidden(self):
        p, src = prefs.effective({"preset": "desktop"}, SPEC_EXAMPLE)
        self.assertEqual((src["layout"], src["hidden"], src["kpis"]), ("browser", "browser", "browser"))
        self.assertEqual(p["hidden"], ["databases"])
        self.assertEqual(src["preset"], "config.ini")

    def test_dashboard_sections_keep_working(self):
        ui, _ = prefs.parse_ui({}, ["system", "attention", "disks"])
        p, src = prefs.effective(ui)
        self.assertEqual([c for c, _ in prefs.visible_cards(p)][:3], ["system", "attention", "disks"])
        self.assertEqual(dict(prefs.visible_cards(p))["attention"], 2)
        self.assertEqual(src["layout"], "config.ini", "[dashboard] sections")
        self.assertEqual(src["kpis"], "default")
        # an explicit [ui] layout beats it
        ui, _ = prefs.parse_ui({"layout": "sessions, boot"}, ["system", "attention", "disks"])
        p, src = prefs.effective(ui)
        self.assertEqual(prefs.visible_cards(p)[:2], [("sessions", 1), ("boot", 1)])
        # and so does a cookie
        p, src = prefs.effective(ui, "1.lat2")
        self.assertEqual(prefs.visible_cards(p)[0], ("attention", 2))
        # the sections order is only for the default preset: the others bring their own
        ui, _ = prefs.parse_ui({"preset": "security"}, ["system", "attention", "disks"])
        p, src = prefs.effective(ui)
        self.assertEqual(p["layout"], prefs.preset_prefs("security")["layout"])
        self.assertEqual(src["layout"], "preset")
        ui, _ = prefs.parse_ui({"preset": "default"}, ["system", "attention", "disks"])
        self.assertEqual(prefs.effective(ui)[0]["layout"][0], ("system", 1))

    def test_a_cookie_preset_chosen_over_a_config_preset(self):
        p, src = prefs.effective({"preset": "server"}, "1.pd")
        self.assertEqual((p["preset"], src["preset"], p["kpis"]), ("desktop", "browser", prefs.preset_prefs("desktop")["kpis"]))

    def test_what_comes_back_is_a_copy(self):
        ui, _ = prefs.parse_ui({"kpis": "problems", "layout": "attention:2", "hidden": "disks"}, ALL)
        p, _ = prefs.effective(ui)
        p["kpis"].append("cpu")
        p["layout"].append(("boot", 1))
        p["hidden"].append("boot")
        self.assertEqual((ui["kpis"], ui["layout"], ui["hidden"]), (["problems"], [("attention", 2)], ["disks"]))
        q, _ = prefs.effective(ui)
        self.assertEqual((q["kpis"], q["hidden"]), (["problems"], ["disks"]))

    def test_random_layers_are_always_valid(self):
        rnd = random.Random(99)
        for _ in range(400):
            ui = dict(random_prefs(rnd), sections=rnd.sample(ALL, rnd.randint(0, 13)), web="classic")
            p, src = prefs.effective(ui, prefs.dump_cookie(random_prefs(rnd)), prefs.dump_cookie(random_prefs(rnd)))
            self.assertEqual(set(p) - {"hidden_w"}, set(prefs.FIELDS))
            self.assertEqual(prefs._clean(p).get("layout", []), p["layout"])
            self.assertTrue(set(p.get("hidden_w", {})) <= set(p["hidden"]))
            self.assertTrue(set(src.values()) <= set(prefs.SOURCES))
            self.assertTrue(0 < len(p["kpis"]) <= prefs.MAX_KPIS)
            seen = [c for c, _ in prefs.visible_cards(p)]
            self.assertEqual(len(seen), len(set(seen)))
            self.assertFalse(set(seen) & set(p["hidden"]))
            self.assertEqual(sorted(seen + [h for h in p["hidden"] if h in ALL]), sorted(ALL))


class VisibleCards(unittest.TestCase):
    def test_the_layout_in_order_with_widths(self):
        self.assertEqual(prefs.visible_cards({"layout": [("disks", 3), ("attention", 2)]})[:2], [("disks", 3), ("attention", 2)])

    def test_cards_the_layout_does_not_mention_are_appended(self):
        got = prefs.visible_cards({"layout": [("disks", 3), ("attention", 2)]})
        self.assertEqual([c for c, _ in got], ["disks", "attention"] + [c for c in ALL if c not in ("disks", "attention")])
        self.assertEqual([w for _, w in got[2:]], [1] * 11, "a new card is 1 wide")

    def test_a_card_added_by_an_upgrade_shows_up_at_the_end(self):
        old = [c for c in ALL if c != "disks"]
        got = prefs.visible_cards({"layout": [(c, 1) for c in old]}, ALL)
        self.assertEqual(got[-1], ("disks", 1))
        self.assertEqual(len(got), 13)

    def test_unknown_cards_are_dropped(self):
        got = prefs.visible_cards({"layout": [("attention", 2), ("nope", 2), ("exposure", 1)]})
        self.assertEqual([c for c, _ in got][:2], ["attention", "exposure"])
        self.assertNotIn("nope", [c for c, _ in got])
        self.assertEqual(prefs.visible_cards({"layout": [("attention", 2)]}, ["attention", "nope"]), [("attention", 2)])

    def test_hidden_cards_never_show(self):
        got = prefs.visible_cards({"layout": [("attention", 2), ("disks", 1)], "hidden": ["disks", "sessions"]})
        self.assertNotIn("disks", [c for c, _ in got])
        self.assertNotIn("sessions", [c for c, _ in got])
        self.assertEqual(len(got), 11)
        self.assertEqual(prefs.visible_cards({"hidden": ALL}), [])

    def test_a_card_whose_feature_is_off_is_not_available(self):
        avail = [c for c in ALL if c not in ("containers", "databases")]
        got = prefs.visible_cards({"layout": [("containers", 2), ("attention", 2), ("exposure", 2)]}, avail)
        self.assertEqual(got[:2], [("attention", 2), ("exposure", 2)])
        self.assertEqual([c for c, _ in got], avail)

    def test_the_order_of_available_gives_the_order_of_the_appended_cards(self):
        got = prefs.visible_cards({"layout": [("attention", 2)]}, ["disks", "attention", "boot"])
        self.assertEqual(got, [("attention", 2), ("disks", 1), ("boot", 1)])
        self.assertEqual(prefs.visible_cards({}, iter(["boot", "disks"])), [("boot", 1), ("disks", 1)], "any iterable")

    def test_empty_prefs_show_everything_in_the_default_order(self):
        self.assertEqual(prefs.visible_cards({}), [(c, 1) for c in ALL])
        self.assertEqual(prefs.visible_cards(None), [(c, 1) for c in ALL])

    def test_widths_are_held_to_one_to_four(self):
        got = dict(prefs.visible_cards({"layout": [("attention", 9), ("exposure", 0), ("webapps", -3), ("firewall", "2"), ("system", True)]}))
        self.assertEqual([got[c] for c in ("attention", "exposure", "webapps", "firewall", "system")], [4, 1, 1, 1, 1])

    def test_a_card_twice_counts_once(self):
        self.assertEqual(prefs.visible_cards({"layout": [("attention", 2), ("attention", 3)]}, ["attention"]), [("attention", 2)])

    def test_presets_through_effective(self):
        for name in prefs.PRESETS:
            got = prefs.visible_cards(prefs.effective({"preset": name})[0])
            self.assertEqual(sorted(c for c, _ in got), sorted(ALL if name != "desktop" else [c for c in ALL if c not in prefs.preset_prefs("desktop")["hidden"]]))


class ExportIni(unittest.TestCase):
    def roundtrip(self, p):
        text = prefs.export_ini(p)
        ui, warns = prefs.parse_ui(ini_section(text), ALL)
        self.assertEqual(warns, [], text)
        return text, prefs.effective(ui)[0]

    def test_the_block(self):
        p, _ = prefs.effective({"theme": "dark", "preset": "desktop"})
        text = prefs.export_ini(p)
        self.assertTrue(text.startswith("[ui]\n"))
        self.assertTrue(text.endswith("\n"))
        for line in ("theme = dark", "density = desk", "start_view = overview", "preset = desktop", "order = severity",
                     "kpis = problems, cpu, ram, disk, temp, ai",
                     "layout = attention:2, system:2, disks, network_traffic, sessions, exposure:2, firewall, boot",
                     "hidden = webapps, containers, databases, tailscale, docker_disk"):
            self.assertIn(line + "\n", text)
        self.assertNotIn("web =", text)
        self.assertEqual(prefs.export_ini(dict(p, web="app")).splitlines()[1], "web = app")

    def test_round_trip_of_every_preset(self):
        for name in prefs.PRESETS:
            with self.subTest(name):
                p, _ = prefs.effective({"preset": name})
                _, back = self.roundtrip(p)
                self.assertEqual(back, p)

    def test_round_trip_of_what_a_browser_can_hold(self):
        for cookie in (SPEC_EXAMPLE, "1", "1.tl", "1.kpb", "1.ldbx", "1.lat2_ex2", "1.pd.lat_exx", "1.ps.dw.kai.lsex3_dix_atx",
                       "1.lat1_ex1_wa1_fw1_sy1_ct1_db1_bo1_nt1_se1_ts1_dd1_di1", "1.lat_ex_wa_fw_sy_ct_db_bo_nt_se_ts_dd_dix"):
            for base in ({}, {"preset": "desktop"}, {"preset": "server", "theme": "dark", "hidden": ["sessions"]}):
                with self.subTest(cookie=cookie, base=sorted(base)):
                    p, _ = prefs.effective(dict(base), cookie)
                    text, back = self.roundtrip(p)
                    self.assertEqual(back, p, text)

    def test_round_trip_when_the_preset_hides_and_the_layout_does_not(self):
        p, _ = prefs.effective({"preset": "desktop"}, "1.lat2_ex2_ct2_db1")  # the cookie names cards that desktop hides, and hides none itself
        self.assertEqual(p["hidden"], [])
        text, back = self.roundtrip(p)
        self.assertIn("\nhidden =\n", text)
        self.assertEqual(back, p)

    def test_round_trip_of_random_prefs(self):
        rnd = random.Random(8)
        for _ in range(300):
            ui, _ = prefs.parse_ui({}, rnd.sample(ALL, rnd.randint(0, 13)))
            p, _ = prefs.effective(dict(ui, preset=rnd.choice(prefs.PRESETS)), prefs.dump_cookie(random_prefs(rnd)))
            _, back = self.roundtrip(p)
            self.assertEqual(back, p)

    def test_what_is_not_valid_is_not_written(self):
        text = prefs.export_ini({"theme": "neon", "kpis": ["nope"], "layout": [("nope", 1)], "web": "weird", "hidden": ["nope"]})
        self.assertEqual(text, "[ui]\nhidden =\n")
        self.assertEqual(prefs.export_ini(None), "[ui]\n")

    def test_widths_are_written_only_above_one(self):
        self.assertIn("layout = attention:2, exposure, webapps:4\n", prefs.export_ini({"layout": [("attention", 2), ("exposure", 1), ("webapps", 4)]}))

    def test_the_text_parses_with_the_strict_parser_too(self):
        p, _ = prefs.effective({"preset": "server"}, SPEC_EXAMPLE)
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))  # strict: a key twice is an error
        cp.read_string(prefs.export_ini(p))
        self.assertEqual(list(cp["ui"]), ["theme", "density", "start_view", "preset", "order", "kpis", "layout", "hidden"])


class LoadConfig(unittest.TestCase):
    def test_no_file_no_section(self):
        cfg = nuc_config.load("/nonexistent/config.ini")
        self.assertEqual(cfg["ui"], {"web": "app", "sections": ALL})
        cfg, err = load_ini("[features]\nmap = no\n")
        self.assertEqual((cfg["ui"], err), ({"web": "app", "sections": ALL}, ""))
        cfg, err = load_ini("[ui]\n")
        self.assertEqual((cfg["ui"], err), ({"web": "app", "sections": ALL}, ""))

    def test_a_valid_section(self):
        cfg, err = load_ini("[ui]\nweb = app\ntheme = dark\ndensity = wall\nstart_view = map\npreset = security\norder = fixed\n"
                            "kpis = problems, internet, lan   # three\nlayout = attention:2, exposure:2, webapps\nhidden = sessions ; the rest\n")
        self.assertEqual(err, "")
        self.assertEqual(cfg["ui"], {"web": "app", "sections": ALL, "theme": "dark", "density": "wall", "start_view": "map", "preset": "security", "order": "fixed",
                                     "kpis": ["problems", "internet", "lan"], "layout": [("attention", 2), ("exposure", 2), ("webapps", 1)], "hidden": ["sessions"]})
        p, src = prefs.effective(cfg["ui"])
        self.assertEqual((p["theme"], src["theme"], p["kpis"], src["kpis"]), ("dark", "config.ini", ["problems", "internet", "lan"], "config.ini"))

    def test_the_example_of_the_plan(self):
        cfg, err = load_ini("[ui]\nweb = classic            # classic | app\ntheme = auto             # auto | dark | light | high-contrast\n"
                            "density = desk           # wall | desk | compact\nstart_view = overview    # overview | map | cpu | health | ai\n"
                            "preset = default         # default | security | server | desktop\norder = severity        # severity | fixed\n"
                            "kpis = problems, internet, lan, cpu, ram, disk, temp\n"
                            "layout = attention:2, exposure:2, webapps, firewall, system, containers:2\nhidden = sessions, docker_disk\n")
        self.assertEqual(err, "")
        p, _ = prefs.effective(cfg["ui"])
        self.assertEqual(p["hidden"], ["sessions", "docker_disk"])
        self.assertEqual(dict(prefs.visible_cards(p))["containers"], 2)
        self.assertNotIn("sessions", [c for c, _ in prefs.visible_cards(p)])

    def test_broken_values_go_to_stderr_and_the_rest_is_kept(self):
        cfg, err = load_ini("[dashboard]\nrotate_seconds = 20\n[ui]\ntheme = neon\ndensity = wall\nkpis = problems, nope\nlayout = attention:9, nope, exposure:x\n"
                            "hidden = nope\nweb = maybe\ncolour = red\n")
        self.assertEqual(cfg["rotate_seconds"], 20, "the other sections are not touched")
        self.assertEqual(cfg["ui"], {"web": "app", "sections": ALL, "density": "wall", "kpis": ["problems"], "layout": [("attention", 4), ("exposure", 1)],
                                     "hidden": []})
        lines = err.strip().splitlines()
        self.assertTrue(all(ln.startswith("nuc-console: ") and "[ui]" in ln for ln in lines), err)
        for part in ("theme must be one of", "unknown name 'nope'", "attention:9", "exposure:x", "unknown card 'nope'", "web must be one of", "unknown key 'colour'"):
            self.assertIn(part, err)

    def test_every_value_wrong_still_loads(self):
        cfg, err = load_ini("[ui]\ntheme = 1\ndensity = 2\nstart_view = 3\npreset = 4\norder = 5\nkpis = 6\nlayout = 7\nhidden = 8\nweb = 9\n")
        self.assertEqual(cfg["ui"], {"web": "app", "sections": ALL, "hidden": []})
        self.assertGreaterEqual(len(err.strip().splitlines()), 9)

    def test_the_section_order_comes_from_dashboard_sections(self):
        cfg, err = load_ini("[dashboard]\nsections = system, attention, nope\n[ui]\ntheme = light\n")
        self.assertEqual(cfg["ui"]["sections"], cfg["sections"])
        self.assertEqual(cfg["ui"]["sections"][:2], ["system", "attention"])
        self.assertIn("unknown name 'nope'", err)
        self.assertEqual(err.count("nope"), 1, "warned once, by [dashboard]")
        cfg, _ = load_ini("[dashboard]\nsections = system, attention\n")  # no [ui] at all
        self.assertEqual(cfg["ui"]["sections"][:2], ["system", "attention"])
        p, src = prefs.effective(cfg["ui"])
        self.assertEqual([c for c, _ in prefs.visible_cards(p)][:2], ["system", "attention"])

    def test_default_section_keys_are_not_ours(self):
        cfg, err = load_ini("[DEFAULT]\ntheme = dark\nlayout = disks\n[ui]\ndensity = wall\n")
        self.assertEqual(cfg["ui"], {"web": "app", "sections": ALL, "density": "wall"})
        self.assertEqual(err, "")

    def test_a_file_that_cannot_be_read_has_the_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.ini")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("[ui]\n:broken = 1\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cfg = nuc_config.load(bad)
        self.assertEqual(cfg["ui"], {"web": "app", "sections": ALL})
        self.assertTrue(cfg["config_error"])

    def test_a_bug_in_prefs_never_stops_the_dashboard(self):
        with mock.patch.object(prefs, "parse_ui", side_effect=RuntimeError("boom")):
            cfg, err = load_ini("[features]\nmap = no\n[ui]\ntheme = dark\n")
        self.assertFalse(cfg["features"]["map"])
        self.assertEqual(cfg["ui"], {"web": "app", "sections": ALL})
        self.assertIn("[ui] ignored: boom", err)

    def test_prefs_is_stdlib_only_and_has_no_import_cycle(self):
        src = os.path.join(ROOT, "src")
        for code in ("import prefs; print(prefs.CARDS[0])",
                     "import nuc_config; print(nuc_config.load('/nonexistent')['ui']['web'])",
                     "import prefs, nuc_config; print(nuc_config.load('/nonexistent')['ui']['sections'][0])"):
            out = subprocess.run([sys.executable, "-c", code], cwd=src, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
        with tempfile.TemporaryDirectory() as d:  # the installers' way in: nuc_config.py run as a script
            ini = os.path.join(d, "config.ini")
            with open(ini, "w", encoding="utf-8") as f:
                f.write("[ui]\nweb = app\n")
            env = dict(os.environ, NUC_CONSOLE_CONFIG=ini)
            out = subprocess.run([sys.executable, os.path.join(src, "nuc_config.py"), "--get", "ui", "web"], capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual((out.returncode, out.stdout.strip(), out.stderr), (0, "app", ""))
        with open(os.path.join(src, "prefs.py"), encoding="utf-8") as f:
            imports = re.findall(r"(?m)^(?:import|from)\s+(\w+)", f.read())
        self.assertEqual(sorted(imports), ["nuc_config", "re"])


class Docs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "docs", "CONFIGURATION.md"), encoding="utf-8") as f:
            doc = f.read()
        cls.doc = doc[doc.index("## `[ui]`"):].split("\n## ")[0]
        with open(os.path.join(ROOT, "config", "config.ini"), encoding="utf-8") as f:
            cls.ini = f.read()

    def test_every_key_and_value_is_documented(self):
        for key in prefs.UI_KEYS:
            self.assertIn("| `%s` |" % key, self.doc, key)
        words = (prefs.WEB_MODES + prefs.THEMES + prefs.DENSITIES + prefs.VIEWS + prefs.PRESETS + prefs.ORDERS + prefs.KPI_IDS + prefs.CARDS)
        for w in words:
            self.assertIn("`%s`" % w, self.doc, w)

    def test_the_documented_defaults_are_the_codes(self):
        rows = {m.group(1): m.group(2) for m in re.finditer(r"(?m)^\| `([a-z_]+)` \| `?([^|`]*)`? \|", self.doc)}
        self.assertEqual(rows["web"], "app")
        for key in ("theme", "density", "start_view", "preset", "order"):
            self.assertEqual(rows[key], prefs.DEFAULTS[key], key)
        self.assertIn("up to %d" % prefs.MAX_KPIS, self.doc)

    def test_the_presets_are_documented_as_coded(self):
        for name in prefs.PRESETS:
            p = prefs.preset_prefs(name)
            rows = [ln for ln in self.doc.splitlines() if ln.startswith("| `%s` |" % name)]
            self.assertEqual(len(rows), 1, name)
            row = rows[0]
            self.assertIn(", ".join(p["kpis"]), row, name)
            self.assertIn(", ".join(c if w == 1 else "%s:%d" % (c, w) for c, w in p["layout"]), row, name)
            for c in p["hidden"]:
                self.assertIn(c, row)

    def test_it_says_who_reads_it(self):
        low = self.doc.lower()
        self.assertIn("the console uses this section", low)  # the console reads [ui]
        self.assertIn("is the default and reads this", low)  # the web shell is the default interface and reads [ui]...
        self.assertIn("web = classic", low)  # ...the classic pages (kept for one release) ignore it

    def test_the_shipped_config_has_a_commented_ui_block_that_works(self):
        block = self.ini[self.ini.index("\n[ui]\n") + 1:].split("\n[")[0]
        ui, warns = prefs.parse_ui(ini_section(block), ALL)
        self.assertEqual((ui, warns), ({"web": "app", "sections": ALL}, []), "shipped as a commented example")
        example = re.sub(r"(?m)^# (%s)(\s*=)" % "|".join(prefs.UI_KEYS), r"\1\2", block)
        self.assertNotEqual(example, block)
        ui, warns = prefs.parse_ui(ini_section(example), ALL)
        self.assertEqual(warns, [])
        self.assertEqual(set(ui) - {"sections"}, set(prefs.UI_KEYS), "every key of [ui] has an example line")
        p, src = prefs.effective(ui)
        for field in ("theme", "density", "start_view", "preset", "order"):
            self.assertEqual(p[field], prefs.DEFAULTS[field], "the example shows the defaults")
        self.assertEqual(ui["web"], "app")
        self.assertLessEqual(len(ui["kpis"]), prefs.MAX_KPIS)

    def test_the_shipped_config_is_strict_and_loads(self):
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
        cp.read_string(self.ini)
        self.assertIn("ui", cp.sections())
        cfg, err = load_ini(self.ini)
        self.assertEqual((cfg["ui"], err), ({"web": "app", "sections": ALL}, ""))


class LayoutEditor(unittest.TestCase):
    """The pure steps of the layout editor (prefs.move, resize, hide, show, reset) and the `?set=e...` field that applies one to a cookie."""
    LAY = {"layout": [("attention", 2), ("exposure", 2), ("webapps", 1), ("firewall", 1)], "hidden": ["boot", "disks"]}

    def ids(self, lay):
        return [c for c, _ in lay["layout"]]

    def test_move_one_place_and_stay_at_the_ends(self):
        self.assertEqual(self.ids(prefs.move(self.LAY, "webapps", -1)), ["attention", "webapps", "exposure", "firewall"])
        self.assertEqual(self.ids(prefs.move(self.LAY, "webapps", 1)), ["attention", "exposure", "firewall", "webapps"])
        self.assertEqual(self.ids(prefs.move(self.LAY, "attention", -1)), self.ids(self.LAY))
        self.assertEqual(self.ids(prefs.move(self.LAY, "firewall", 1)), self.ids(self.LAY))
        self.assertEqual(self.ids(prefs.move(self.LAY, "attention", 99)), ["exposure", "webapps", "firewall", "attention"])
        self.assertEqual(prefs.move(self.LAY, "webapps", -1)["hidden"], ["boot", "disks"])
        self.assertEqual(dict(prefs.move(self.LAY, "firewall", -2)["layout"])["firewall"], 1)  # the width goes with the card

    def test_resize_is_held_to_one_to_four(self):
        w = lambda lay, c: dict(lay["layout"])[c]  # noqa: E731
        self.assertEqual(w(prefs.resize(self.LAY, "webapps", 1), "webapps"), 2)
        self.assertEqual(w(prefs.resize(self.LAY, "webapps", -1), "webapps"), 1)
        self.assertEqual(w(prefs.resize(self.LAY, "attention", 5), "attention"), 4)
        self.assertEqual(w(prefs.resize(self.LAY, "attention", -5), "attention"), 1)
        self.assertEqual(self.ids(prefs.resize(self.LAY, "webapps", 1)), self.ids(self.LAY))

    def test_hide_and_show(self):
        h = prefs.hide(self.LAY, "exposure")
        self.assertEqual(self.ids(h), ["attention", "webapps", "firewall"])
        self.assertEqual(h["hidden"], ["exposure", "boot", "disks"])  # the order of the sections: the hidden list is a set
        s = prefs.show(h, "boot")
        self.assertEqual(s["layout"][-1], ("boot", 1))  # last, 1 wide
        self.assertEqual(s["hidden"], ["exposure", "disks"])
        self.assertEqual(prefs.hide(self.LAY, "boot"), prefs._out(*prefs._lay(self.LAY)))  # already hidden: nothing
        self.assertEqual(prefs.show(self.LAY, "webapps"), prefs._out(*prefs._lay(self.LAY)))  # not hidden: nothing

    def test_a_hidden_card_keeps_its_width_and_show_restores_it(self):
        lay = prefs.resize(prefs.resize(self.LAY, "webapps", 1), "webapps", 1)
        h = prefs.hide(lay, "webapps")
        self.assertEqual(h["hidden_w"], {"webapps": 3})
        self.assertNotIn("hidden_w", prefs.hide(self.LAY, "webapps"), "1 wide: nothing to keep")
        s = prefs.show(h, "webapps")
        self.assertEqual(s["layout"][-1], ("webapps", 3))
        self.assertNotIn("hidden_w", s)
        cur = prefs.apply_edit(prefs.apply_edit(prefs.apply_edit(prefs.apply_edit("1", "egwa"), "egwa"), "ehwa"), "eufw")
        self.assertIn("wa3x", cur)
        eff, _ = prefs.effective(None, cur)
        self.assertEqual((eff["hidden"], eff["hidden_w"]), (["webapps"], {"webapps": 3}))
        self.assertIn("hidden = webapps:3\n", prefs.export_ini(eff))
        back = prefs.apply_edit(cur, "ewwa")
        self.assertNotIn("wa3x", back)
        self.assertEqual(dict(prefs.parse_cookie(back)["layout"])["webapps"], 3)
        self.assertEqual(prefs.apply_edit("1.lat2_wa3x", "ewat"), "1.lat2_wa3x", "a card that is not hidden: nothing")

    def test_a_hidden_width_is_dropped_with_its_card_and_never_outlives_it(self):
        cur = "1.lat2_wa3x_dbx"
        self.assertEqual(prefs.dump_cookie(prefs.parse_cookie(cur)), cur)
        self.assertEqual(prefs.dump_cookie({"layout": [("attention", 1)], "hidden": ["webapps"], "hidden_w": {"webapps": 9, "disks": 2, "x": 2}}),
                         "1.lat1_wa4x", "held to 1-4, only for the hidden cards")
        self.assertEqual(prefs.apply_edit(cur, "ereset"), "1")
        self.assertEqual(prefs.apply_set(cur, "pv"), "1.pv", "a preset drops the layout and the hidden cards, and their widths")

    def test_widths_of_hidden_cards_round_trip_and_stay_within_the_limit(self):
        rnd = random.Random(21)
        for _ in range(300):
            p = random_prefs(rnd)
            if "hidden" in p:
                p["hidden_w"] = {c: rnd.randint(1, 4) for c in p["hidden"] if rnd.random() < 0.7}
            s = prefs.dump_cookie(p)
            if len(s) <= prefs.COOKIE_MAX:
                got = prefs.parse_cookie(s)
                self.assertEqual(prefs.dump_cookie(got), s)
                want = {c: w for c, w in p.get("hidden_w", {}).items() if w > 1}
                self.assertEqual(got.get("hidden_w", {}), want)
        worst = prefs.dump_cookie({"theme": "high-contrast", "density": "compact", "start_view": "overview", "preset": "security", "order": "fixed",
                                   "kpis": list(prefs.KPI_IDS)[:8], "layout": [(c, 4) for c in ALL[:1]], "hidden": ALL[1:],
                                   "hidden_w": {c: 4 for c in ALL[1:]}})
        self.assertLessEqual(len(worst), prefs.COOKIE_MAX)

    def test_every_edit_step_keeps_the_cookie_valid_and_short_with_hidden_widths(self):
        rnd = random.Random(8)
        ck = "1.tl.dw.vo.pv.os.kpb_in_la_be_dl_fw_cp_rm"
        for _ in range(300):
            ck = prefs.apply_edit(ck, prefs.edit_field(rnd.choice(list(prefs.EDIT_OPS)), rnd.choice(ALL)))
            self.assertLessEqual(len(ck), prefs.COOKIE_MAX)
            got = prefs.parse_cookie(ck)
            self.assertTrue(set(got.get("hidden_w", {})) <= set(got.get("hidden", [])))
            self.assertEqual(prefs.dump_cookie(got), ck)

    def test_config_hidden_takes_a_width(self):
        ui, warns = prefs.parse_ui({"hidden": "sessions:3, docker_disk, boot:9, disks:x, webapps:1"}, ALL)
        self.assertEqual(ui["hidden"], ["webapps", "boot", "sessions", "docker_disk", "disks"])
        self.assertEqual(ui["hidden_w"], {"sessions": 3, "boot": 4})
        self.assertEqual(len(warns), 2)
        eff, _ = prefs.effective(ui)
        self.assertEqual(eff["hidden_w"], {"sessions": 3, "boot": 4})
        text = prefs.export_ini(eff)
        self.assertIn("hidden = webapps, boot:4, sessions:3, docker_disk, disks\n", text)
        again, warns = prefs.parse_ui(ini_section(text), ALL)
        self.assertEqual((warns, again["hidden"], again["hidden_w"]), ([], eff["hidden"], eff["hidden_w"]))

    def test_unknown_ids_and_bad_numbers_change_nothing(self):
        same = prefs._out(*prefs._lay(self.LAY))
        for card in ("nope", "", None, 3, ("a", "b"), "__top"):
            self.assertEqual(prefs.move(self.LAY, card, 1), same)
            self.assertEqual(prefs.resize(self.LAY, card, 1), same)
            self.assertEqual(prefs.hide(self.LAY, card), same)
            self.assertEqual(prefs.show(self.LAY, card), same)
        for delta in (None, "1", 1.5, True):
            self.assertEqual(prefs.move(self.LAY, "webapps", delta), same)
            self.assertEqual(prefs.resize(self.LAY, "webapps", delta), same)
        self.assertEqual(prefs.move({}, "webapps", 1), {"layout": [], "hidden": []})
        self.assertEqual(prefs.move("junk", "webapps", 1), {"layout": [], "hidden": []})

    def test_the_steps_do_not_change_what_they_were_given(self):
        before = repr(self.LAY)
        for f in (lambda: prefs.move(self.LAY, "webapps", -1), lambda: prefs.resize(self.LAY, "webapps", 1), lambda: prefs.hide(self.LAY, "webapps"),
                  lambda: prefs.show(self.LAY, "boot")):
            f()
        self.assertEqual(repr(self.LAY), before)

    def test_reset_is_no_layout_at_all(self):
        self.assertEqual(prefs.reset(self.LAY), {})
        self.assertEqual(prefs.reset(), {})

    def test_layout_of_is_what_the_overview_shows_and_what_is_hidden(self):
        eff, _ = prefs.effective(None, "1.lat2_ex1_wax_dbx")
        lay = prefs.layout_of(eff, ["attention", "exposure", "webapps", "databases", "boot"])
        self.assertEqual(lay["layout"], [("attention", 2), ("exposure", 1), ("boot", 1)])  # databases is hidden, boot was left out: last
        self.assertEqual(lay["hidden"], ["webapps", "databases"])
        self.assertEqual(prefs.layout_of(eff, ["attention"])["hidden"], ["webapps", "databases"])  # a hidden card stays hidden

    def test_the_field_of_a_step(self):
        for op in prefs.EDIT_OPS:
            for card in ALL:
                f = prefs.edit_field(op, card)
                self.assertRegex(f, r"^e[udsghw][a-z]{2}$")
                self.assertEqual(prefs.edit_parts(f), (op, card))
                self.assertRegex(f, prefs._FIELD_RE.pattern)  # it is a legal ?set= value
        self.assertEqual(prefs.edit_parts("ereset"), ("r", None))
        for bad in ("", "e", "euzz", "exat", "euat1", "Euat", "ereset1", "reset", "tl", "euat.euat", None, 5, ["euat"]):
            self.assertIsNone(prefs.edit_parts(bad), bad)

    def test_a_step_applied_to_a_cookie_keeps_everything_but_the_layout(self):
        cur = "1.tl.dw.os.kpb_in_cp"
        out = prefs.apply_edit(cur, "euwa")  # the first step fixes the whole layout of the preset
        got = prefs.parse_cookie(out)
        self.assertEqual({k: v for k, v in got.items() if k not in ("layout", "hidden")}, {k: v for k, v in prefs.parse_cookie(cur).items()})
        self.assertEqual(len(got["layout"]), len(ALL))
        self.assertEqual(got["layout"][1][0], "webapps")  # up from 3rd to 2nd
        self.assertEqual(got["hidden"], [])
        again = prefs.apply_edit(out, "ehwa")
        self.assertEqual(prefs.parse_cookie(again)["hidden"], ["webapps"])
        self.assertEqual(prefs.parse_cookie(prefs.apply_edit(again, "ewwa"))["hidden"], [])
        self.assertEqual(prefs.apply_edit(prefs.apply_edit(out, "egat"), "esat"), out)  # wider, narrower: back
        self.assertEqual(prefs.apply_edit("1.tl.lat2_ex2_wa1", "ereset"), "1.tl")
        self.assertEqual(prefs.apply_edit("1.lat2", "ereset"), "1")

    def test_a_step_that_changes_nothing_leaves_the_cookie_alone(self):
        for field in ("euat", "esfw", "ehzz", "euzz", "junk", "", "ewat"):
            self.assertEqual(prefs.apply_edit("1.tl", field), "1.tl", field)  # the first card up, the narrowest narrower, unknown, not hidden
        self.assertEqual(prefs.apply_edit("junk", "ehwa"), prefs.apply_edit("1", "ehwa"))  # an invalid cookie is no cookie

    def test_a_card_whose_feature_is_off_is_not_edited(self):
        avail = [c for c in ALL if c != "webapps"]
        self.assertEqual(prefs.apply_edit("1.tl", "ehwa", None, avail), "1.tl")
        self.assertEqual(prefs.apply_edit("1.tl", "euwa", None, avail), "1.tl")
        got = prefs.parse_cookie(prefs.apply_edit("1.tl", "ehfw", None, avail))
        self.assertEqual(got["hidden"], ["firewall"])
        self.assertNotIn("webapps", [c for c, _ in got["layout"]])  # it is in no list: the layout holds only the cards that exist

    def test_the_presets_hidden_cards_become_the_cookies(self):
        got = prefs.parse_cookie(prefs.apply_edit("1.pd", "ehat"))
        self.assertEqual(set(got["hidden"]), {"attention", "containers", "databases", "docker_disk", "webapps", "tailscale"})
        self.assertEqual(got["preset"], "desktop")  # the preset stays; its layout is now the reader's own

    def test_the_cookie_stays_valid_and_short_after_any_run_of_steps(self):
        rnd = random.Random(21)
        cur = "1.tl.dw.vo.pv.os.kpb_in_la_be_dl_fw_cp_rm"  # all the other fields at their longest
        for _ in range(400):
            op = rnd.choice(list(prefs.EDIT_OPS))
            cur = prefs.apply_edit(cur, prefs.edit_field(op, rnd.choice(ALL)))
            self.assertLessEqual(len(cur), prefs.COOKIE_MAX)
            self.assertEqual(prefs.dump_cookie(prefs.parse_cookie(cur)), cur)  # canonical
            got = prefs.parse_cookie(cur)
            seen = [c for c, _ in got.get("layout", ())] + got["hidden"]
            self.assertEqual(len(seen), len(set(seen)))  # a card is in one list, once
            self.assertEqual(sorted(seen), sorted(ALL))
        worst = prefs.dump_cookie({"theme": "high-contrast", "density": "compact", "start_view": "overview", "preset": "security", "order": "fixed",
                                   "kpis": list(prefs.KPI_IDS)[:8], "layout": [(c, 4) for c in ALL[:7]], "hidden": ALL[7:]})
        self.assertLessEqual(len(worst), prefs.COOKIE_MAX)

    def test_an_edited_layout_exports_as_layout_and_hidden(self):
        cur = prefs.apply_edit(prefs.apply_edit("1", "euwa"), "ehdb")
        eff, _ = prefs.effective(None, cur)
        text = prefs.export_ini(eff)
        self.assertIn("layout = attention:2, webapps, exposure:2", text)
        self.assertIn("hidden = databases", text)
        ui, warns = prefs.parse_ui(ini_section(text), ALL)
        self.assertEqual(warns, [])
        self.assertEqual((ui["layout"], ui["hidden"]), (eff["layout"], eff["hidden"]))

    def test_a_layout_of_the_readers_own_fixes_the_order(self):
        for cookie, ui, custom in (("", "", False), ("1.tl", "", False), ("1.lat2_ex2", "", True), ("1.dbx", "", False), ("", "1.lat2", True)):
            _, src = prefs.effective(None, cookie, ui)
            self.assertEqual(prefs.custom_layout(src), custom, (cookie, ui))
        # a layout from config.ini is a stated layout too: fixed order, unless order = severity is set by someone
        cfg = {"layout": [("attention", 1)], "sections": ALL}
        eff, src = prefs.effective(cfg, "")
        self.assertTrue(prefs.custom_layout(src, eff["order"]))
        eff, src = prefs.effective(dict(cfg, order="severity"), "")
        self.assertFalse(prefs.custom_layout(src, eff["order"]), "order = severity written in config.ini")
        eff, src = prefs.effective(cfg, "1.os")
        self.assertFalse(prefs.custom_layout(src, eff["order"]), "By severity chosen in the browser")
        eff, src = prefs.effective(cfg, "1.lat2.os")
        self.assertFalse(prefs.custom_layout(src, eff["order"]), "...whatever the layout's source")
        eff, src = prefs.effective(cfg, "1.of")
        self.assertTrue(prefs.custom_layout(src, eff["order"]))
        eff, src = prefs.effective(None, "1.ps")  # a preset's own layout is not a stated one
        self.assertFalse(prefs.custom_layout(src, eff["order"]))
        eff, src = prefs.effective(None, "1.lat2")  # the layout alone: the default order is severity, but not an explicit one
        self.assertTrue(prefs.custom_layout(src, eff["order"]))
        self.assertTrue(prefs.custom_layout(src), "one argument: the layout alone decides")


if __name__ == "__main__":
    unittest.main()
