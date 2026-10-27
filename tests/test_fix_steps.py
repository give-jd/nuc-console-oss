"""A Health finding's fix as steps (what it does, the exact command): the texts of every rule and OS, the finding dict, ui.Pane, the console's
drawing of the steps and the JSON a peer or the web reads. Hermetic: pure data and the demo reports, nothing of the host."""
import json
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import ansi  # noqa: E402
import demo  # noqa: E402
import health  # noqa: E402
import remotedoc  # noqa: E402
import screens  # noqa: E402
import ui  # noqa: E402
import webapi  # noqa: E402
import test_health  # noqa: E402

PLACEHOLDER = re.compile(r"<[^<>\s]+>")


def plain(lines):
    return [ansi.ANSI.sub("", x) for x in lines]


class Texts(unittest.TestCase):
    def each(self):
        for rule, per_os in health.FIXES.items():
            self.assertEqual(set(per_os), set(health.OSES), rule)
            for os_name, pairs in per_os.items():
                yield rule, os_name, pairs

    def test_every_rule_and_os_has_well_formed_steps(self):
        n = 0
        for rule, os_name, pairs in self.each():
            n += 1
            where = (rule, os_name)
            self.assertTrue(1 <= len(pairs) <= health.MAX_STEPS, where)
            for what, cmd in pairs:
                self.assertTrue(what and what == what.strip() and len(what) <= 110, (where, what))
                self.assertNotIn("`", what, where)                                   # a command never hides in the sentence
                self.assertEqual(cmd, cmd.strip(), where)
                self.assertLess(len(cmd), 141, where)
        self.assertEqual(n, 17 * 3)

    def test_template_is_true_exactly_when_a_placeholder_is_in_the_command(self):
        seen = set()
        for rule, os_name, pairs in self.each():
            for step, (what, cmd) in zip(health.steps_of(pairs), pairs):
                self.assertEqual(step, {"what": what, "cmd": cmd, "template": bool(PLACEHOLDER.search(cmd))})
                seen.add(step["template"])
        self.assertEqual(seen, {True, False})
        self.assertEqual([s["template"] for s in health.steps_of([("a", "ls"), ("b", "systemctl status <unit>"), ("c", "ls 2>/dev/null")])],
                         [False, True, False])

    def test_flat_fix_holds_every_command_and_every_sentence(self):
        for rule, os_name, pairs in self.each():
            steps = health.steps_of(pairs)
            flat = health.flat_fix(steps)
            for s in steps:
                self.assertIn(s["what"], flat)
                self.assertIn(s["cmd"], flat)
        self.assertEqual(health.flat_fix(health.steps_of([("Look", "ls -l"), ("Think", "")])), "Look: ls -l; Think")

    def test_darwin_and_windows_fall_back_to_linux_when_a_rule_has_no_text_of_its_own(self):
        fx = health._fx((("a", "ls"),))
        self.assertEqual(fx["darwin"], fx["linux"])
        self.assertEqual(fx["windows"], fx["linux"])
        fx = health._fx((("a", "ls"),), (("b", ""),))
        self.assertEqual((fx["darwin"], fx["windows"]), ((("b", ""),), (("a", "ls"),)))

    def test_commands_stay_templates_and_no_name_is_put_into_them(self):
        d = test_health.DB()
        d.event("oom", "x; rm -rf ~ `id`", n=2)
        f = test_health.get(d.report(), "oom:x; rm -rf ~ `id`")
        self.assertIn("docker update --memory 2g <name>", [s["cmd"] for s in f["steps"]])
        self.assertFalse([s for s in f["steps"] if "rm -rf" in s["cmd"] or "`id`" in s["cmd"]])


class Findings(unittest.TestCase):
    def test_a_finding_of_the_rules_carries_steps_and_the_flat_fix(self):
        d = test_health.DB()
        d.event("oom", "worker", n=2)
        f = test_health.get(d.report(), "oom:worker")
        self.assertEqual(set(f), {"id", "level", "title", "text", "fix", "steps", "facts", "subject"})
        self.assertEqual(f["steps"], health.steps_of(health.FIXES["oom"]["linux"]))
        self.assertEqual(f["fix"], health.flat_fix(f["steps"]))
        json.dumps(f)

    def test_a_demo_finding_of_every_os_carries_steps_and_the_flat_fix(self):
        for os_name in ("linux", "windows", "darwin"):
            found = demo.health_report(os_name, 30)["findings"]
            self.assertTrue(found, os_name)
            for f in found:
                self.assertTrue(f["steps"], f["id"])
                self.assertEqual(f["fix"], health.flat_fix(f["steps"]), f["id"])
                rule = f["id"].split(":")[0]
                if f["id"] != "crash-loop:Spooler":                                   # the demo says what the real rule says
                    self.assertEqual(f["steps"], health.steps_of(health.FIXES[rule][os_name]), (os_name, f["id"]))
        spooler = [f for f in demo.health_report("windows", 30)["findings"] if f["id"] == "crash-loop:Spooler"][0]
        self.assertEqual(spooler["steps"][0], {"what": "Check the service", "cmd": "Get-Service <name>", "template": True})

    def test_the_screen_reads_steps_from_a_finding_and_ignores_what_is_not_one(self):
        f = {"steps": [{"what": "Look", "cmd": "ls <dir>", "template": True}, {"what": "Think", "cmd": "", "template": False},
                       {"what": 5, "cmd": "x"}, "junk", None, {"what": "no cmd"}, {"what": "w", "cmd": "c", "template": "yes"}]}
        self.assertEqual(screens.health_steps(f), [("Look", "ls <dir>", True), ("Think", "", False), ("w", "c", False)])
        for bad in ({}, {"steps": None}, {"steps": "ls"}, {"steps": {"what": "a"}}):
            self.assertEqual(screens.health_steps(bad), [])
        self.assertEqual(len(screens.health_steps({"steps": [{"what": "a", "cmd": "b"}] * 50})), 12)
        pane = screens.health_pane(dict(f, id="a:b", level="warn", title="t", text="x", fix="the flat one", facts={}), 80, None)
        self.assertEqual((pane.fix, pane.steps[0]), ("the flat one", ("Look", "ls <dir>", True)))


class PaneDrawing(unittest.TestCase):
    STEPS = [("See what uses the CPU", "top", False), ("Look at the service", "systemctl status <unit>", True), ("Check the log", "", False)]

    def pane(self, **kw):
        return ui.Pane("warn", "title", "what happened", [("n", 3)], "flat fix text", **kw)

    def test_a_pane_without_steps_is_as_before(self):
        p = self.pane()
        self.assertEqual(p.steps, [])
        lines = plain(ansi.render(p, 60)[0])
        self.assertIn("fix    flat fix text", lines[-1])
        self.assertFalse([x for x in lines if "$ " in x])

    def test_steps_are_numbered_with_the_command_on_its_own_line(self):
        lines = plain(ansi.render(self.pane(steps=self.STEPS), 60)[0])
        i = next(j for j, x in enumerate(lines) if x.startswith(" fix "))
        self.assertEqual(lines[i], " fix    1. See what uses the CPU")
        self.assertEqual(lines[i + 1], "           $ top")
        self.assertEqual(lines[i + 2], "        2. Look at the service")
        self.assertEqual(lines[i + 3], "           $ systemctl status <unit>")            # a template shows its placeholder as is
        self.assertEqual(lines[i + 4], "        3. Check the log")
        self.assertEqual(len(lines), i + 5)
        self.assertNotIn("flat fix text", "\n".join(lines))

    def test_long_steps_wrap_and_every_line_fits(self):
        long = [("A very long sentence that says what this step does and why it matters here", "journalctl -k -p err | grep -iE 'mce|edac|nvme'", False)]
        lines = plain(ansi.render(self.pane(steps=long), 40)[0])
        self.assertTrue(all(len(x) <= 40 for x in lines), lines)
        self.assertTrue([x for x in lines if "$ " in x])

    def test_h_cuts_the_pane_and_says_how_many_lines_are_left_out(self):
        full = plain(ansi.render(self.pane(steps=self.STEPS), 60)[0])
        cut = plain(ansi.render(self.pane(steps=self.STEPS, h=len(full) - 3), 60)[0])
        self.assertEqual(len(cut), len(full) - 3)
        self.assertRegex(cut[-1], r"\+\d+ more lines?")
        self.assertEqual(cut[:-1], full[:len(cut) - 1])

    def test_a_console_document_round_trips_the_steps(self):
        p = self.pane(steps=self.STEPS)
        doc = webapi.data(p, console=True)
        self.assertEqual(doc["steps"], [list(s) for s in self.STEPS])
        got = remotedoc.decode(json.loads(json.dumps({"api": 1, "view": "health", "flavour": "console", "nodes": [doc]})))
        self.assertTrue(got.ok)
        self.assertEqual(got.nodes[0], p)
        old = dict(doc)
        del old["steps"]                                                                  # a peer of an older version has no steps
        got = remotedoc.decode({"api": 1, "view": "health", "flavour": "console", "nodes": [old]})
        self.assertEqual((got.nodes[0].steps, got.nodes[0].fix), ([], "flat fix text"))

    def test_the_web_json_carries_steps_and_keeps_fix(self):
        out = webapi.data(self.pane(steps=self.STEPS))
        self.assertEqual((out["fix"], out["steps"]), ("flat fix text", [list(s) for s in self.STEPS]))
        out = webapi.data(self.pane())
        self.assertEqual((out["fix"], out["steps"]), ("flat fix text", []))


if __name__ == "__main__":
    unittest.main()
