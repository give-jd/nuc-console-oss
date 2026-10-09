"""[alerts] mute and [ui] kpis = none (issue 55).

A muted operational alarm is off the ATTENTION list, the problems figure and Telegram, counted as "N muted", still produced by the collectors
and listed by `nuc-console-problems`. A security alarm can never be muted: the closed list nuc_config.MUTABLE_ALERTS is checked here against
the classification of EVERY alarm id, so a new alarm has to be put on one side on purpose. The fixture is built by hand (documentation
addresses only): nothing reads the machine running the tests.
"""
import ast
import contextlib
import inspect
import io
import os
import re
import sys
import tempfile
import textwrap
import time
import unittest
from collections import Counter
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # golden.py
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import ansi  # noqa: E402
import cards  # noqa: E402
import collector  # noqa: E402
import confedit  # noqa: E402
import golden  # noqa: E402  (FrozenWorld: the default configuration, whatever another test left in render.CFG)
import htmlview  # noqa: E402
import notify  # noqa: E402
import nuc_config  # noqa: E402
import prefs  # noqa: E402
import problems  # noqa: E402
import render  # noqa: E402

# Every alarm id, on one side: the ones a person may silence (how the machine runs) and the ones never (an attack surface, or what its
# judgement reads, or the alerting itself). MUTABLE_ALERTS must be exactly the first set.
OPERATIONAL = {"journal-errors", "thermal", "throttling", "container-exited", "unhealthy-container", "collector-boot"}
SECURITY = {
    "ufw-off", "ufw-missing", "ufw-unreadable", "firewall-off", "firewall-unreadable", "firewall-policy",                   # the firewall
    "docker-bypass", "db-open-lan", "funnel-public", "over-exposed", "expose-unmatched",                                     # exposure
    "port-new", "port-changed", "port-gone", "port-compare-suspended", "baseline-missing", "baseline-unreadable",           # ports
    "collector-net", "collector-containers", "stale-net", "stale-containers", "net-sections", "config-unreadable",           # their data
    "telegram-unpaired", "telegram-failing",                                                                                 # the alerting
    "failed-units",                                                                                                          # a stopped fail2ban or sshd hides in it
    "mute-ignored",                                                                                                          # this very setting
}
SM = {"cpu": {"cpu0": 0.1}, "thermal": {"cpu": (101.0, 105.0), "nvme": (33.0, 85.85), "throttle_s": 469.0, "recent": 0, "clk": (2.6, 4.9)}}


def fx(ufw_on=True):
    """A small machine with three operational alarms (a container that exited, journal errors, a hot CPU) and, with ufw_on=False, ufw off."""
    now = time.time()
    ufw = collector.parse_ufw("Status: " + ("active" if ufw_on else "inactive") + "\nLogging: on (low)\nDefault: deny (incoming), allow (outgoing), "
                              "deny (routed)\nNew profiles: skip\n\nTo                         Action      From\n--                         ------      ----\n"
                              "22/tcp                     ALLOW IN    192.0.2.0/24\n")
    cont = {"ts": now, "containers": [{"name": "app-1", "status": "Up 3 days", "state": "running", "project": "p", "ports": [], "mem": 2 ** 20},
                                      {"name": "db-1", "status": "Exited (1) 2 hours ago", "state": "exited", "project": "p", "ports": [], "mem": None}]}
    net = {"ts": now, "errors": {}, "ufw": ufw, "docker_user": [], "f2b": {"jails": []}, "drops": {"n": 0, "src": [], "dpt": []}, "serve": [],
           "listeners": [{"proto": "tcp", "addr": "127.0.0.1", "port": 9100, "proc": "app"}]}
    boot = {"ts": now, "errors": {}, "kernel": "7.0", "btime": int(now) - 86400, "analyze": None, "blame": [], "failed": [], "enabled": [],
            "journal": {"err": 3, "warn": 5, "capped": False, "top": []}, "containers": []}
    return cont, net, boot


def ids(pb):
    return list(pb.pids)


class World(unittest.TestCase):
    ALERTS = {"mute": [], "ignored": []}
    UI = {}

    def setUp(self):
        world = golden.FrozenWorld(cfg={"spacing": 1, "details": True, "alerts": self.ALERTS, "ui": self.UI})
        world.__enter__()
        self.addCleanup(world.__exit__, None, None, None)
        self.cont, self.net, self.boot = fx()

    def pb(self, net=None):
        return problems.problems(net or self.net, self.cont, time.time(), boot=self.boot, thermal=SM["thermal"], baseline=False)

    def screen(self, w=140, h=60):
        det = []
        lines = render.page_overview(SM, self.cont, self.net, self.boot, w - 1, h - 2, details=det, now=time.time())
        return ansi.ANSI.sub("", "\n".join(lines))


class Unmuted(World):
    def test_the_operational_alarms_are_all_there(self):
        pb = self.pb()
        self.assertTrue(OPERATIONAL >= {"container-exited", "journal-errors", "thermal"} and {"container-exited", "journal-errors", "thermal"} <= set(ids(pb)))
        self.assertEqual(pb.muted, 0)
        self.assertNotIn("muted", self.screen())


class Muted(World):
    ALERTS = {"mute": ["container-exited", "journal-errors"], "ignored": []}

    def test_a_muted_alarm_is_off_the_list_and_counted(self):
        pb = self.pb()
        self.assertEqual(sorted(ids(pb)), ["thermal"])
        self.assertEqual((pb.muted, sorted(x["id"] for x in pb.muted_known)), (2, ["container-exited", "journal-errors"]))
        text = self.screen()
        self.assertNotIn("exited with an error", text)
        self.assertNotIn("errors in this boot", text)
        self.assertRegex(text, r"· 2 muted \(nuc-console-problems\)")
        self.assertIn("above the", text)  # the one that is not muted is still said

    def test_the_problems_figure_leaves_them_out_and_says_so(self):
        pb = self.pb()
        ctx = cards.Ctx(problems=pb, cfg=render.CFG)
        (k,) = cards.kpis(ctx, ["problems"])
        self.assertEqual((k.value, k.hint), ("1", "2 muted"))
        everything = problems.problems_raw(self.net, self.cont, boot=self.boot, thermal=SM["thermal"], baseline=False)
        self.assertEqual(len(everything), 3)

    def test_the_collector_data_is_untouched(self):
        raw = [pid for _s, _t, pid in problems.problems_raw(self.net, self.cont, boot=self.boot, thermal=SM["thermal"], baseline=False)]
        self.assertTrue({"container-exited", "journal-errors"} <= set(raw), "only the surface is muted: the alarm is still worked out")
        self.assertEqual(self.cont["containers"][1]["state"], "exited")
        self.assertEqual(self.boot["journal"]["err"], 3)

    def test_telegram_does_not_hear_of_them(self):
        recs = problems.problem_records(self.net, self.cont, boot=self.boot, thermal=SM["thermal"], baseline=False)
        self.assertEqual(sorted(r["id"] for r in recs if r["muted"]), ["container-exited", "journal-errors"])
        keys, seen = notify.collect(recs)
        sent = {r["id"] for r in seen.values()}
        self.assertEqual(sent, {"thermal"})
        text = notify.format_message("host", notify.pick(Counter(keys), seen), (), "titles", started=sum(keys.values()))
        self.assertIn("1 open problem", text)
        self.assertNotIn("Container exited", text)
        self.assertNotIn("journal", text.lower())

    def test_nuc_console_problems_lists_them_under_muted(self):
        recs = problems.problem_records(self.net, self.cont, boot=self.boot, thermal=SM["thermal"], baseline=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(render, "current_problem_records", lambda: recs):
            render.print_problems([])
        text = out.getvalue()
        head, _, tail = text.partition("muted (2, by [alerts] mute in config.ini):")
        self.assertIn("1 problems (0 accepted, 2 muted)", head)
        self.assertIn("thermal", head)
        self.assertNotIn("container-exited", head)
        self.assertIn("container-exited", tail)
        self.assertIn("journal-errors", tail)

    def test_accepted_json_is_not_involved(self):
        pb = self.pb()
        self.assertEqual((pb.accepted, pb.known), (0, []))
        recs = problems.problem_records(self.net, self.cont, boot=self.boot, thermal=SM["thermal"], baseline=False)
        self.assertEqual([r["accepted"] for r in recs if r["muted"]], [False, False])


class NeverASecurityAlarm(World):
    """Whatever ends up in CFG, a security alarm stays: the check is made where the alarms are filtered, not only where the file is read."""
    ALERTS = {"mute": ["ufw-off", "journal-errors", "port-new", "db-open-lan"], "ignored": []}

    def test_a_security_id_in_cfg_is_not_muted(self):
        self.net["ufw"] = collector.parse_ufw("Status: inactive\n")
        pb = self.pb()
        self.assertIn("ufw-off", ids(pb))
        self.assertEqual(problems.muted_ids(), {"journal-errors"})
        self.assertNotIn("journal-errors", ids(pb))
        self.assertEqual(pb.muted, 1)
        self.assertIn("ufw off", self.screen())


class Reported(World):
    ALERTS = {"mute": ["journal-errors"], "ignored": ["port-new", "nope"]}

    def test_what_the_file_asked_that_cannot_be_is_raised_on_the_screen(self):
        pb = self.pb()
        self.assertIn("mute-ignored", ids(pb))
        text = pb[ids(pb).index("mute-ignored")][1]
        self.assertIn("'port-new'", text)
        self.assertIn("'nope'", text)
        self.assertIn("cannot be muted", self.screen())
        self.assertNotIn("journal-errors", ids(pb))  # the valid part of the line still applies


class FromTheFile(unittest.TestCase):
    def load(self, text):
        said = []
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.ini")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            cfg = nuc_config.load(path, warn=said.append)
        return cfg["alerts"], said

    def test_a_valid_list(self):
        alerts, said = self.load("[alerts]\nmute = Journal-Errors, thermal ; journal-errors\n")
        self.assertEqual((alerts["mute"], alerts["ignored"], said), (["journal-errors", "thermal"], [], []))

    def test_security_and_unknown_ids_are_ignored_and_reported_in_the_log(self):
        alerts, said = self.load("[alerts]\nmute = port-new, journal-errors, ufw-off, nope, nope\nbogus = 1\n")
        self.assertEqual((alerts["mute"], alerts["ignored"]), (["journal-errors"], ["port-new", "ufw-off", "nope"]))
        self.assertEqual(len(said), 4, said)
        for name in ("port-new", "ufw-off", "nope"):
            self.assertTrue(any(f"'{name}' cannot be muted" in s for s in said), name)
        self.assertTrue(any("unknown key 'bogus'" in s for s in said))

    def test_no_section_no_mute(self):
        self.assertEqual(self.load("[ui]\ntheme = dark\n")[0], {"mute": [], "ignored": []})
        self.assertEqual(nuc_config.load("/nonexistent")["alerts"], {"mute": [], "ignored": []})


class TheClosedList(unittest.TestCase):
    def alarm_ids(self):
        """Every id problems_raw can emit (read off its source) and every id of the catalogue."""
        found = set()
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(problems.problems_raw)))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append" and node.args \
                    and isinstance(node.args[0], ast.Tuple) and len(node.args[0].elts) == 3:
                last = node.args[0].elts[2]
                if isinstance(last, ast.Constant) and isinstance(last.value, str):
                    found.add(last.value)
        return found | set(problems.BASE_CATALOG) | set(problems.CATALOG) | set(cards.PROBLEM_CARDS)

    def test_every_alarm_is_classified_once(self):
        every = self.alarm_ids()
        self.assertGreater(len(every), 30)
        self.assertEqual(OPERATIONAL & SECURITY, set())
        self.assertEqual(sorted(every - OPERATIONAL - SECURITY), [], "an alarm nobody classified: operational (add it to MUTABLE_ALERTS) or security")
        self.assertEqual(sorted((OPERATIONAL | SECURITY) - every), [], "a classified id that no alarm uses")

    def test_the_muteable_ones_are_exactly_mutable_alerts(self):
        muteable = {pid for pid in self.alarm_ids() if pid in nuc_config.MUTABLE_ALERTS}
        self.assertEqual(muteable, OPERATIONAL)
        self.assertEqual(set(nuc_config.MUTABLE_ALERTS), OPERATIONAL)
        self.assertEqual(len(nuc_config.MUTABLE_ALERTS), len(set(nuc_config.MUTABLE_ALERTS)))
        self.assertIsInstance(nuc_config.MUTABLE_ALERTS, tuple)

    def test_no_security_id_survives_the_file_or_a_filled_cfg(self):
        for pid in sorted(SECURITY):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "config.ini")
                with open(path, "w") as f:
                    f.write("[alerts]\nmute = %s\n" % pid)
                alerts = nuc_config.load(path, warn=lambda _s: None)["alerts"]
            self.assertEqual((alerts["mute"], alerts["ignored"]), ([], [pid]), pid)
            with golden.FrozenWorld(cfg={"alerts": {"mute": [pid], "ignored": []}}):
                self.assertEqual(problems.muted_ids(), set(), pid)

    def test_the_settings_page_takes_the_same_list(self):
        key = confedit.KEY[("alerts", "mute")]
        self.assertEqual((key.kind, key.choices), (confedit.LIST, nuc_config.MUTABLE_ALERTS))
        self.assertIn("alerts", confedit.TITLES)
        self.assertEqual(confedit.check(key, "Thermal, journal-errors"), "thermal, journal-errors")
        for pid in sorted(SECURITY):
            with self.assertRaises(confedit.Refused, msg=pid):
                confedit.check(key, pid)
        with self.assertRaises(confedit.Refused):
            confedit.check(key, "nope")

    def test_every_alarm_the_catalog_names_is_explained(self):
        self.assertIn("mute-ignored", problems.CATALOG)


class KpisNone(unittest.TestCase):
    def test_the_file_says_none(self):
        ui, warns = prefs.parse_ui({"kpis": " None "}, None)
        self.assertEqual((ui["kpis"], warns), ([], []))
        self.assertEqual(prefs.effective(ui)[0]["kpis"], [])
        self.assertEqual(prefs.effective({})[0]["kpis"], prefs.preset_prefs("default")["kpis"])
        ui, warns = prefs.parse_ui({"kpis": ""}, None)
        self.assertNotIn("kpis", ui)  # blank still sets nothing
        ui, warns = prefs.parse_ui({"kpis": "none, cpu"}, None)
        self.assertEqual(ui["kpis"], ["cpu"])
        self.assertEqual(len(warns), 1)

    def test_a_cookie_cannot_say_it_and_export_reads_back(self):
        self.assertEqual(prefs.dump_cookie({"kpis": []}), "1")
        self.assertIn("kpis = none\n", prefs.export_ini({"kpis": []}))
        ui, _ = prefs.parse_ui({"kpis": "none"}, None)
        text = prefs.export_ini(prefs.effective(ui)[0])
        self.assertIn("kpis = none\n", text)

    def test_the_settings_page_writes_none(self):
        key = confedit.KEY[("ui", "kpis")]
        self.assertEqual(confedit.check(key, "none"), "none")
        cfg = nuc_config.load("/nonexistent")
        cfg["ui"] = prefs.parse_ui({"kpis": "none"}, None)[0]
        self.assertEqual(confedit.value(cfg, key), "none")
        items, drop = confedit.changes(nuc_config.load("/nonexistent"), "ui", {"kpis": ["none"]})
        self.assertEqual((items, drop), ([("kpis", "none")], []))


class KpisNoneOnTheConsole(World):
    UI = {"kpis": []}

    def test_no_line_and_the_row_goes_to_the_body(self):
        for h in (24, 30, 50):
            self.assertFalse(render.kpi_on(h), h)
            self.assertEqual(render.body_rows(h), h - 2, h)
        lines = ansi.ANSI.sub("", render.frame(("Overview", 1, 1, ["body"]), 0, 1, 139, 50, problems.ProblemList())).split("\r\n")
        self.assertEqual(len(lines), 50)
        self.assertNotIn("Problems", lines[1])
        self.assertTrue(lines[1].startswith("body"))

    def test_the_default_still_has_it(self):
        render.CFG["ui"].pop("kpis", None)
        self.assertTrue(render.kpi_on(50))
        self.assertEqual(render.body_rows(50), 47)


class KpisNoneOnTheWeb(World):
    UI = {"kpis": []}

    def test_the_block_is_there_and_empty(self):
        self.assertNotIn('class="kpis"', htmlview.kpis_block([]))
        self.assertIn('data-card="__kpis"', htmlview.kpis_block([]))
        self.assertIn('class="kpis"', htmlview.kpis_block(["<a>x</a>"]))

    def test_the_shell_and_the_classic_page_draw_no_row(self):
        with golden.FrozenWorld(cfg={"ui": {"kpis": []}}) as world:
            shell, classic = world.page(""), world.page("app=0")
            app = str(world.server.app_page("1", {}))  # the live app: its #cfg carries the key figures to draw
        for page in (shell, classic):
            self.assertNotIn('aria-label="Key figures"', page)
            self.assertNotRegex(page, r'class="kpi[ "]')
        self.assertIn('"kpis":[],', app.replace(" ", ""))

    def test_the_app_script_draws_no_row_for_an_empty_list(self):
        import appjs
        self.assertIn("tiles.length ?", inspect.getsource(appjs))


if __name__ == "__main__":
    unittest.main()
