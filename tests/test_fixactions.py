"""The Execute button of five ATTENTION problems (src/fixactions.py, POST /fix/run, systemd/50-nuc-console-fix.rules, install.sh).

Hermetic: systemctl is never run (subprocess.run is mocked and every refusal test asserts it was not called), polkit is not touched, the
server is the real handler on 127.0.0.1:0 with the demo data, the clock is injected, the installer's polkit block is run against temporary
folders with `install` stubbed."""
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import confedit  # noqa: E402
import fixactions  # noqa: E402
import nuc_config  # noqa: E402
import problems  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
import webapi  # noqa: E402

RULE_FILE = os.path.join(ROOT, "systemd", "50-nuc-console-fix.rules")
INSTALL = os.path.join(ROOT, "install.sh")
VIEWER = "v" * 24
ON = {"web": {"fix_actions": True, "token_file": "/etc/nuc-console/token"}}
NO_TOKEN = {"web": {"fix_actions": True, "token_file": ""}}
OFF = {"web": {"fix_actions": False}}
ALL = set(fixactions.TABLE)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Completed(object):
    def __init__(self, rc=0, err=b""):
        self.returncode, self.stderr = rc, err


class Table(unittest.TestCase):
    def test_exactly_the_five_ids_with_their_verbs_and_units(self):
        self.assertEqual({k: v[:2] for k, v in fixactions.TABLE.items()}, {
            "collector-net": ("restart", "nuc-console-collector.service"), "collector-boot": ("restart", "nuc-console-collector.service"),
            "stale-containers": ("restart", "nuc-console-collector.service"), "stale-net": ("restart", "nuc-console-collector.service"),
            "fleet-poller-down": ("start", "nuc-console-peers.service")})

    def test_every_id_is_in_the_catalog_and_its_fix_names_the_same_command(self):
        for pid, (verb, unit, label) in fixactions.TABLE.items():
            self.assertIn(pid, problems.BASE_CATALOG)
            self.assertIn("systemctl %s %s" % (verb, unit[:-len(".service")]), problems.BASE_CATALOG[pid][2], pid)
            self.assertEqual(label.lower(), "%s %s" % (verb, unit[:-len(".service")]))


class Run(unittest.TestCase):
    def setUp(self):
        fixactions._last.clear()
        self.addCleanup(fixactions._last.clear)
        for p in (mock.patch.object(nuc_config, "LINUX", True), mock.patch.object(fixactions.shutil, "which", return_value="/usr/bin/systemctl")):
            p.start()
            self.addCleanup(p.stop)

    def go(self, pid="stale-net", active=ALL, cfg=ON, now=1000.0, result=None):
        err = io.StringIO()
        with mock.patch.object(fixactions.subprocess, "run", return_value=result or Completed()) as sub, redirect_stderr(err):
            out = fixactions.run(pid, active, cfg, now)
        return out, sub, err.getvalue()

    def test_the_happy_path_runs_one_fixed_argv_without_a_shell(self):
        (ok, note), sub, log = self.go("collector-net")
        self.assertTrue(ok)
        self.assertEqual(note, "nuc-console-collector.service restarted")
        sub.assert_called_once_with(["/usr/bin/systemctl", "--no-ask-password", "restart", "nuc-console-collector.service"], shell=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
                                   env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        self.assertEqual(log, "nuc-console web: fix collector-net: systemctl restart nuc-console-collector.service: ok\n")

    def test_the_poller_is_started_not_restarted(self):
        (ok, note), sub, _log = self.go("fleet-poller-down")
        self.assertEqual((ok, note), (True, "nuc-console-peers.service started"))
        self.assertEqual(sub.call_args[0][0][2:], ["start", "nuc-console-peers.service"])

    def test_systemctl_is_looked_up_in_the_fixed_path(self):
        with mock.patch.object(fixactions.shutil, "which", return_value="/usr/bin/systemctl") as which:
            self.go()
        which.assert_called_with("systemctl", path="/usr/bin:/bin")

    def refused(self, res, words, log_has):
        (ok, note), sub, log = res
        self.assertFalse(ok)
        self.assertIn(words, note)
        sub.assert_not_called()
        self.assertIn(log_has, log)

    def test_feature_off_or_not_a_bool_true_is_refused(self):
        for cfg in (OFF, {}, None, {"web": {}}, {"web": {"fix_actions": "yes"}}, {"web": {"fix_actions": 1}}):
            self.refused(self.go(cfg=cfg), "off", "refused(off)")

    def test_not_linux_is_refused(self):
        with mock.patch.object(nuc_config, "LINUX", False):
            self.refused(self.go(), "only available on Linux", "refused(not linux)")
            self.assertIsNone(fixactions.item("stale-net", ON))
            self.assertFalse(fixactions.available(ON))

    def test_an_id_outside_the_table_is_refused_and_never_logged(self):
        for pid in ("ssh", "", "x; reboot", "stale-net ", "STALE-NET", "$(id)", "../../etc", None, 5, ["stale-net"]):
            (ok, _n), sub, log = self.go(pid, active={pid} if isinstance(pid, str) else ALL)
            self.assertFalse(ok, pid)
            sub.assert_not_called()
            self.assertEqual(log, "nuc-console web: fix ?: refused(unknown id)\n")

    def test_a_problem_that_is_not_active_is_refused_even_if_it_is_in_the_table(self):
        for active in (set(), {"stale-containers"}, {"peer-down"}):
            self.refused(self.go("stale-net", active=active), "not active", "refused(not active)")

    def test_the_same_unit_again_within_a_minute_is_refused_whatever_the_problem(self):
        self.assertTrue(self.go("collector-net", now=1000.0)[0][0])
        self.refused(self.go("stale-net", now=1059.9), "less than a minute", "refused(rate limit)")
        self.assertTrue(self.go("stale-net", now=1060.0)[0][0])
        self.assertTrue(self.go("fleet-poller-down", now=1060.5)[0][0], "another unit has its own clock")

    def test_a_failed_attempt_counts_for_the_rate_limit(self):
        self.go("stale-net", now=1000.0, result=Completed(1, b"boom"))
        self.refused(self.go("stale-net", now=1010.0), "less than a minute", "refused(rate limit)")

    def test_one_run_at_a_time(self):
        with fixactions._lock:
            self.refused(self.go(), "another fix is running", "refused(busy)")
        self.assertTrue(self.go()[0][0], "the lock is released after a refusal and after a run")

    def test_the_lock_is_released_after_a_failure(self):
        self.go(result=Completed(1, b""))
        self.assertTrue(fixactions._lock.acquire(blocking=False))
        fixactions._lock.release()

    def test_polkit_refusal_gets_the_install_hint_never_the_raw_text(self):
        for err in (b"Failed to restart x: Access denied\n", b"Failed to restart x: Interactive authentication required.\n", b"ACCESS DENIED"):
            fixactions._last.clear()
            (ok, note), _sub, log = self.go(result=Completed(1, err))
            self.assertFalse(ok)
            self.assertEqual(note, "not permitted: the polkit rule is not installed (docs/INSTALL.md)")
            self.assertIn("failed(rc 1)", log)
            self.assertNotIn("Access", log)

    def test_any_other_failure_is_a_fixed_word_with_no_stderr_in_it(self):
        secret = b"Unit 10.1.2.3 root@host password=hunter2 not found"
        (ok, note), _s, log = self.go(result=Completed(5, secret))
        self.assertEqual((ok, note), (False, "failed (see the service log)"))
        self.assertNotIn("hunter2", note + log)
        self.assertIn("failed(rc 5)", log)

    def test_a_timeout_and_a_missing_binary_are_failures_with_fixed_notes(self):
        with mock.patch.object(fixactions.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemctl", 20, stderr=b"secret")), \
                redirect_stderr(io.StringIO()) as log:
            ok, note = fixactions.run("stale-net", ALL, ON, 1.0)
        self.assertFalse(ok)
        self.assertNotIn("secret", note + log.getvalue())
        self.assertIn("did not answer", note)
        fixactions._last.clear()
        with mock.patch.object(fixactions.subprocess, "run", side_effect=OSError("no /secret/path")), redirect_stderr(io.StringIO()) as log:
            ok, note = fixactions.run("stale-net", ALL, ON, 1.0)
        self.assertEqual((ok, note), (False, "failed: systemctl could not be run"))
        self.assertNotIn("secret", log.getvalue())
        fixactions._last.clear()
        with mock.patch.object(fixactions.shutil, "which", return_value=None), redirect_stderr(io.StringIO()):
            self.assertEqual(fixactions.run("stale-net", ALL, ON, 1.0), (False, "failed: systemctl was not found"))

    def test_key_on_without_a_token_file_refuses_everything(self):
        for cfg in (NO_TOKEN, {"web": {"fix_actions": True}}, {"web": {"fix_actions": True, "token_file": None}}):
            self.assertFalse(fixactions.available(cfg))
            self.assertIsNone(fixactions.item("stale-net", cfg))
            self.refused(self.go(cfg=cfg), "token_file", "refused(token)")

    def test_precheck_is_the_cheap_order_and_run_uses_it(self):
        self.assertIsNone(fixactions.precheck("stale-net", ON, 1000.0))
        self.assertIn("off", fixactions.precheck("stale-net", OFF, 1000.0))
        self.assertIn("no Execute", fixactions.precheck("ssh", ON, 1000.0))
        with fixactions._lock:
            self.assertIn("running", fixactions.precheck("stale-net", ON, 1000.0))
        self.go("stale-net", now=1000.0)
        self.assertIn("minute", fixactions.precheck("collector-net", ON, 1010.0))
        self.assertIsNone(fixactions.precheck("collector-net", ON, 1061.0))
        self.assertIsNone(fixactions.precheck("stale-net", ON, 1010.0) and None)

    def test_item_is_for_a_table_id_with_the_feature_on_only(self):
        self.assertEqual(fixactions.item("stale-net", ON), {"label": "Restart nuc-console-collector", "id": "stale-net"})
        self.assertEqual(fixactions.item("fleet-poller-down", ON)["label"], "Start nuc-console-peers")
        self.assertIsNone(fixactions.item("stale-net", OFF))
        self.assertIsNone(fixactions.item("peer-down", ON))
        self.assertIsNone(fixactions.item(None, ON))

    def test_the_problem_json_has_a_run_field_everywhere(self):
        pb = problems.ProblemList([(1, "network data stale"), (2, "x")])
        pb.pids, pb.info = ["stale-net", "peer-down"], [("t", "w", "f", "a"), ("t", "w", "f", "a")]
        run = lambda pid: fixactions.item(pid, ON)  # noqa: E731
        out = webapi.problems(pb, run=run, note={"ok": False, "text": "failed"})
        self.assertEqual([i["run"] for i in out["items"]], [{"label": "Restart nuc-console-collector", "id": "stale-net"}, None])
        self.assertEqual(out["fix_note"], {"ok": False, "text": "failed"})
        plain = webapi.problems(pb)
        self.assertEqual([i["run"] for i in plain["items"]], [None, None])
        self.assertIsNone(plain["fix_note"])
        self.assertIsNone(webapi.problems([(2, "something")])["items"][0]["run"])


class Polkit(unittest.TestCase):
    def test_the_committed_file_is_the_generated_text(self):
        self.assertEqual(read(RULE_FILE), fixactions.polkit_rule())

    def test_exactly_the_unit_and_verb_pairs_of_the_table_for_the_web_user(self):
        text = fixactions.polkit_rule()
        pairs = re.findall(r'action\.lookup\("unit"\) == "([^"]+)" && action\.lookup\("verb"\) == "([^"]+)"', text)
        self.assertEqual(sorted(pairs), sorted({(unit, verb) for verb, unit, _l in fixactions.TABLE.values()}))
        self.assertEqual(sorted(pairs), [("nuc-console-collector.service", "restart"), ("nuc-console-peers.service", "start")])
        self.assertEqual(text.count("action.lookup("), 4)
        self.assertEqual(re.findall(r'subject\.user == "([^"]+)"', text), [fixactions.WEB_USER])
        self.assertEqual(text.count('action.id == "org.freedesktop.systemd1.manage-units"'), 1)
        self.assertEqual(text.count("polkit.Result.YES"), 1)
        self.assertEqual(text.count("polkit.addRule"), 1)

    def test_no_wildcard_pattern_or_prefix_match_and_nothing_but_yes(self):
        code = "\n".join(l for l in fixactions.polkit_rule().splitlines() if not l.lstrip().startswith("//"))
        for bad in ("*", "RegExp", ".match", ".test(", "indexOf", "startsWith", "endsWith", "includes", "slice", "subject.isInGroup", "Result.AUTH",
                    "Result.NO", "Result.NOT_HANDLED", "action.id !=", "||  "):
            self.assertNotIn(bad, code, bad)
        self.assertNotIn("return polkit.Result.YES;\n    }\n    return", code)  # falls through: no default answer after the test

    def test_the_account_is_the_one_of_the_web_unit_and_of_the_installer(self):
        self.assertIn("User=%s\n" % fixactions.WEB_USER, read(os.path.join(ROOT, "systemd", "nuc-console-web.service")))
        self.assertRegex(read(INSTALL), r"useradd [^\n]* %s\n" % re.escape(fixactions.WEB_USER))

    def test_the_units_it_names_exist_in_the_package(self):
        for _verb, unit, _l in fixactions.TABLE.values():
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "systemd", unit)), unit)

    def test_the_web_unit_keeps_its_sandbox(self):
        unit = read(os.path.join(ROOT, "systemd", "nuc-console-web.service"))
        for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "CapabilityBoundingSet=\n"):
            self.assertIn(line, unit)


@unittest.skipUnless(sys.platform.startswith("linux"), "the installer's block runs on Linux (bash, GNU env)")
class Installer(unittest.TestCase):
    """The block of install.sh that installs or removes the rule, run against temporary folders (`install` stubbed: the real one wants root)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rules = os.path.join(self.tmp.name, "rules.d")
        self.conf = os.path.join(self.tmp.name, "config.ini")
        src = read(INSTALL)
        self.block = re.search(r"(?ms)^if \[ -d /etc/polkit-1/rules\.d \].*?^fi$", src).group(0)

    def run_block(self, ini, folder=True, existing=False):
        if folder:
            os.makedirs(self.rules, exist_ok=True)
        rule = os.path.join(self.rules, "50-nuc-console-fix.rules")
        if existing:
            write(rule, "old")
        if ini is not None:
            write(self.conf, ini)
        block = self.block.replace("/etc/polkit-1/rules.d", self.rules).replace("/etc/nuc-console/config.ini", self.conf)
        script = ('install() { local a=("$@"); cp "${a[-2]}" "${a[-1]}"; }\nFIXRULE=%s\nPY=%s\nDEST=%s\ncd %s\n%s\n'
                  % (rule, sys.executable, os.path.join(ROOT, "src"), ROOT, block))
        r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return os.path.exists(rule), r.stdout

    def test_installed_only_with_the_key_on_and_the_folder_there(self):
        on, out = self.run_block("[web]\nfix_actions = yes\ntoken_file = /etc/nuc-console/token\n")
        self.assertTrue(on)
        self.assertIn("ON", out)
        self.assertEqual(read(os.path.join(self.rules, "50-nuc-console-fix.rules")), fixactions.polkit_rule())

    def test_removed_when_the_key_is_off_missing_or_not_a_bool(self):
        for ini in ("[web]\nfix_actions = yes\n", "[web]\nfix_actions = yes\ntoken_file =\n", "[web]\nfix_actions = no\ntoken_file = /t\n", "[web]\nfix_actions = no\n", "[web]\nenabled = yes\n", "[web]\nfix_actions = maybe\n", "[web]\nfix_actions = \n", "", None):
            on, out = self.run_block(ini, existing=True)
            self.assertFalse(on, ini)
            self.assertIn("off", out)
        self.assertIn("token_file", out)

    def test_an_unreadable_or_broken_config_removes_it(self):
        on, _out = self.run_block("[web\nfix_actions = yes\n", existing=True)
        self.assertFalse(on)

    def test_no_polkit_folder_installs_nothing_and_creates_nothing(self):
        on, out = self.run_block("[web]\nfix_actions = yes\ntoken_file = /etc/nuc-console/token\n", folder=False)
        self.assertFalse(on)
        self.assertFalse(os.path.exists(self.rules))
        self.assertIn("off", out)

    def test_structure_root_owned_0644_and_the_uninstall_removes_it(self):
        src = read(INSTALL)
        self.assertIn('install -m 0644 -o root -g root systemd/50-nuc-console-fix.rules "$FIXRULE"', src)
        self.assertIn("FIXRULE=/etc/polkit-1/rules.d/50-nuc-console-fix.rules", src)
        un = src[src.index('"--uninstall"'):src.index("exit 0")]
        self.assertIn('rm -f "$FIXRULE"', un)
        self.assertLess(src.index("src/*.py"), src.index("fixactions.py"), "the module the block runs is installed first")
        for other in ("install-macos.sh", "install-windows.ps1"):
            self.assertNotIn("polkit", read(os.path.join(ROOT, other)))


class Config(unittest.TestCase):
    def load(self, ini, overlay=False):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.ini")
            write(p, ini)
            return nuc_config.load(p, warn=lambda line: None, overlay=overlay)["web"]["fix_actions"]

    def test_off_by_default_and_a_typo_is_a_lock(self):
        self.assertIs(nuc_config.load("/nonexistent")["web"]["fix_actions"], False)
        self.assertIs(self.load("[web]\nenabled = yes\n"), False)
        for word in ("maybe", "", "2", "ye", "enable"):
            self.assertIs(self.load("[web]\nfix_actions = %s\n" % word), False, word)
        for word in ("yes", "true", "on", "1"):
            self.assertIs(self.load("[web]\nfix_actions = %s\n" % word), True, word)

    def test_the_shipped_config_says_no(self):
        self.assertRegex(read(os.path.join(ROOT, "config", "config.ini")), r"(?m)^fix_actions = no\b")

    def test_the_overlay_cannot_turn_it_on(self):
        self.assertFalse(nuc_config.overlay_allowed("web", "fix_actions"))
        with tempfile.TemporaryDirectory() as d:
            ov = os.path.join(d, "settings.ini")
            write(ov, "[web]\nfix_actions = yes\n")
            os.chmod(ov, 0o640)
            self.assertFalse(nuc_config.read_overlay(ov, lambda line: None).get("web.fix_actions"))
            self.assertIs(nuc_config.load("/nonexistent", overlay=ov)["web"]["fix_actions"], False)

    def test_the_settings_page_cannot_change_it(self):
        key = confedit.KEY[("web", "fix_actions")]
        self.assertEqual((key.kind, key.default, key.applies), (confedit.BOOL, "no", confedit.LOCK))
        self.assertFalse(key.editable())
        self.assertFalse(key.editable(overlay=True))

    def test_documented(self):
        self.assertIn("| `fix_actions` | `no` |", read(os.path.join(ROOT, "docs", "CONFIGURATION.md")))


class Endpoint(unittest.TestCase):
    """POST /fix/run on the real handler. systemctl is a mock that must not be called by any refusal."""

    @classmethod
    def setUpClass(cls):
        cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
        cls.srv = web.Server(("127.0.0.1", 0), cfg, VIEWER, demo=True)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.web_cfg = dict(render.CFG["web"])
        cls.expose, cls.webapps, cls.hostname = render.CFG["expose"], render.CFG["webapps"], socket.gethostname  # the demo frame changes them

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        render.CFG["expose"], render.CFG["webapps"], socket.gethostname = cls.expose, cls.webapps, cls.hostname
        render.CFG["web"] = cls.web_cfg
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        fixactions._last.clear()
        self.addCleanup(fixactions._last.clear)
        render.CFG["web"] = dict(self.web_cfg, fix_actions=True, token_file="/etc/nuc-console/token")
        self.addCleanup(lambda: render.CFG.__setitem__("web", self.web_cfg))
        self.active = [(1, "stale-net")]
        self.sub = mock.patch.object(fixactions.subprocess, "run", return_value=Completed()).start()
        self.frame = mock.patch.object(self.srv, "read_frame", side_effect=lambda: mock.Mock(problem_ids=lambda: self.active))
        for p in (mock.patch.object(nuc_config, "LINUX", True), mock.patch.object(fixactions.shutil, "which", return_value="/usr/bin/systemctl"),
                  self.frame):
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.srv.fix_note = None

    def post(self, fields=None, csrf=True, headers=None, path="/fix/run", body=None, token=VIEWER):
        data = dict(fields if fields is not None else {"id": "stale-net"})
        if csrf:
            data["csrf"] = self.srv.csrf if csrf is True else csrf
        h = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:%d" % self.port}
        if token:
            h["Authorization"] = "Bearer " + token
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", path, urlencode(data) if body is None else body, h)
        r = c.getresponse()
        out = (r.status, {k.title(): v for k, v in r.getheaders()}, r.read().decode("utf-8", "replace"))
        c.close()
        return out

    def refused(self, res, code):
        self.assertEqual(res[0], code, res)
        self.sub.assert_not_called()

    def test_the_happy_path_runs_systemctl_once_and_redirects_back_with_a_note(self):
        with redirect_stderr(io.StringIO()) as log:
            st, h, _b = self.post({"id": "stale-net", "back": "view=cpu&sort=mem&zoom=125"})
        self.assertEqual(st, 303)
        self.assertEqual(h["Location"], "/?view=cpu&zoom=125&sort=mem")
        self.sub.assert_called_once()
        self.assertEqual(self.sub.call_args[0][0], ["/usr/bin/systemctl", "--no-ask-password", "restart", "nuc-console-collector.service"])
        self.assertIn("fix stale-net: systemctl restart nuc-console-collector.service: ok", log.getvalue())
        self.assertEqual(self.srv.fix_notice(), {"ok": True, "text": "nuc-console-collector.service restarted"})

    def test_the_live_app_gets_json_and_the_note_is_in_the_overview_document(self):
        st, _h, body = self.post({"id": "stale-net", "to": "app", "back": "view="}, headers={"Accept": "application/json"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["to"], "/app")
        self.frame.stop()  # the real frame, with the demo's data
        doc = self.srv.api_overview(2)["problems"]
        self.assertEqual(doc["fix_note"]["ok"], True)

    def test_the_overview_document_draws_run_only_while_the_key_is_on(self):
        self.frame.stop()
        doc = self.srv.api_overview(2)["problems"]
        self.assertTrue(all("run" in i for i in doc["items"]))
        self.assertTrue(all(i["run"] is None or i["run"]["id"] in fixactions.TABLE for i in doc["items"]))
        render.CFG["web"] = dict(self.web_cfg, fix_actions=False)
        self.assertTrue(all(i["run"] is None for i in self.srv.api_overview(2)["problems"]["items"]))

    def test_the_redirect_never_comes_from_the_request(self):
        for back in ("//evil.example/x", "http://evil.example/", "view=cpu&next=http://evil.example", "\r\nLocation: http://evil.example", "%0d%0aX: y"):
            with redirect_stderr(io.StringIO()):
                fixactions._last.clear()
                st, h, _b = self.post({"id": "stale-net", "back": back})
            self.assertEqual(st, 303, back)
            self.assertTrue(h["Location"].startswith("/") and not h["Location"].startswith("//"), h["Location"])
            self.assertNotIn("evil", h["Location"])
            self.assertNotIn("\n", h["Location"])

    def test_refusals_never_run_systemctl(self):
        self.refused(self.post(token=None), 401)
        self.refused(self.post(token="x" * 24), 401)
        self.refused(self.post(csrf=False), 403)
        self.refused(self.post(csrf="wrong"), 403)
        self.refused(self.post(headers={"Origin": "http://evil.example"}), 403)
        self.refused(self.post(headers={"Origin": None, "Referer": "http://evil.example/x"}), 403)
        self.refused(self.post(headers={"Sec-Fetch-Site": "cross-site"}), 403)
        self.refused(self.post(headers={"Content-Type": "text/plain"}), 415)
        self.refused(self.post(body="id=stale-net&csrf=%s&pad=%s" % (self.srv.csrf, "a" * 5000)), 413)
        self.refused(self.post(path="/fix/run?id=stale-net"), 405)
        self.refused(self.post(path="/fix/"), 404)
        self.refused(self.post(path="/fix/restart"), 404)
        self.refused(self.post(path="/fix"), 405)

    def test_unknown_or_hostile_ids_are_a_400(self):
        for bad in ("", "ssh", "x; reboot", "$(id)", "stale-net;", "stale-net\n", "a" * 500, "peer-down", "../stale-net", "STALE-NET"):
            self.refused(self.post({"id": bad}), 400)
        self.refused(self.post({}), 400)

    def test_a_problem_that_is_not_active_now_is_refused_with_a_note_and_nothing_runs(self):
        self.active = [(1, "peer-down")]
        with redirect_stderr(io.StringIO()) as log:
            st, _h, _b = self.post({"id": "stale-net"})
        self.assertEqual(st, 303)
        self.sub.assert_not_called()
        self.assertEqual(self.srv.fix_notice()["ok"], False)
        self.assertIn("refused(not active)", log.getvalue())

    def test_an_unreadable_state_means_nothing_is_active(self):
        with mock.patch.object(self.srv, "read_frame", side_effect=RuntimeError("secret detail")), redirect_stderr(io.StringIO()) as log:
            st, _h, _b = self.post()
        self.assertEqual(st, 303)
        self.sub.assert_not_called()
        self.assertNotIn("secret detail", self.srv.fix_notice()["text"])

    def test_the_second_click_within_a_minute_is_refused(self):
        self.active = [(1, "stale-net"), (1, "collector-net")]
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.post()[0], 303)
            self.assertEqual(self.post({"id": "collector-net"})[0], 303)
        self.assertEqual(self.sub.call_count, 1)
        self.assertIn("less than a minute", self.srv.fix_notice()["text"])

    def test_key_on_without_a_token_file_is_a_403_and_nothing_runs(self):
        render.CFG["web"] = dict(self.web_cfg, fix_actions=True, token_file="")
        self.refused(self.post(), 403)
        self.frame.stop()
        self.assertTrue(all(i["run"] is None for i in self.srv.api_overview(2)["problems"]["items"]))

    def test_a_refused_or_rate_limited_click_never_builds_the_frame(self):
        rf = self.srv.read_frame
        with redirect_stderr(io.StringIO()):
            self.post({"id": "stale-net"})
            rf.reset_mock()
            self.post({"id": "stale-net"})  # rate limited
            self.post({"id": "collector-net"})  # same unit
            with fixactions._lock:
                self.post({"id": "fleet-poller-down"})  # busy
            with mock.patch.object(nuc_config, "LINUX", False):
                self.post({"id": "fleet-poller-down"})
        rf.assert_not_called()
        self.post({"id": "x;y"})
        rf.assert_not_called()
        self.assertEqual(self.sub.call_count, 1)

    def test_feature_off_is_a_403_and_nothing_runs(self):
        render.CFG["web"] = dict(self.web_cfg, fix_actions=False)
        self.refused(self.post(), 403)

    def test_not_linux_is_a_403_and_nothing_runs(self):
        with mock.patch.object(nuc_config, "LINUX", False):
            self.refused(self.post(), 403)

    def test_a_get_is_405_and_a_peer_token_never_reaches_it(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/fix/run", headers={"Authorization": "Bearer " + VIEWER})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 405)
        self.sub.assert_not_called()

    def test_the_form_gets_no_fix_area_on_other_posts(self):
        self.assertIn("fix", __import__("webhttp").POST_AREAS)
        self.assertEqual(__import__("webhttp").POST_AREAS["fix"], ("run",))


class PeerDocs(unittest.TestCase):
    """A peer token (either scope) is never handed run or fix_note, even with the key on; the viewer is."""

    def test_the_overview_for_a_peer_has_run_null_and_no_note(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "peer.tokens")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("a summary %s\nb views %s\n" % ("s" * 40, "w" * 40))
            cfg = dict(nuc_config.load()["web"], refresh_seconds=2, peer_tokens_file=path)
            srv = web.Server(("127.0.0.1", 0), cfg, VIEWER, demo=True)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            saved, expose, webapps, hostname = dict(render.CFG["web"]), render.CFG["expose"], render.CFG["webapps"], socket.gethostname
            render.CFG["web"] = dict(saved, fix_actions=True, token_file="/etc/nuc-console/token")
            srv.fix_note = {"ok": True, "text": "x", "at": __import__("time").time()}
            frame = srv.read_frame()  # the demo's real frame, with one problem of ours
            frame.problems = problems.ProblemList([(1, "network data stale")])
            frame.problems.pids, frame.problems.info = ["stale-net"], [("t", "w", "f", "a")]
            try:
                with mock.patch.object(nuc_config, "LINUX", True), mock.patch.object(srv, "shell_frame", return_value=frame):
                    def get(tok):
                        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
                        c.request("GET", "/api/v1/overview", headers={"Authorization": "Bearer " + tok})
                        r = c.getresponse()
                        body = r.read().decode()
                        c.close()
                        return r.status, body
                    st, body = get(VIEWER)
                    self.assertEqual(st, 200)
                    doc = json.loads(body)["problems"]
                    self.assertEqual(doc["items"][0]["run"]["id"], "stale-net")
                    self.assertEqual(doc["fix_note"]["text"], "x")
                    st, body = get("w" * 40)  # views scope reads the overview
                    self.assertEqual(st, 200)
                    doc = json.loads(body)["problems"]
                    self.assertIsNone(doc["items"][0]["run"])
                    self.assertIsNone(doc["fix_note"])
                    self.assertNotIn("Restart nuc-console", body)
                    self.assertEqual(get("s" * 40)[0], 403)  # summary scope cannot read it at all
                    self.assertEqual(json.loads(get(VIEWER)[1])["problems"]["items"][0]["run"]["id"], "stale-net", "the viewer's cached copy is its own")
            finally:
                render.CFG["web"] = saved
                render.CFG["expose"], render.CFG["webapps"], socket.gethostname = expose, webapps, hostname
                render.DEMO = False
                srv.shutdown()
                srv.server_close()


class PeerScope(unittest.TestCase):
    """A peer token (any scope) is a 403 on /fix/run: it is stopped before the form is read, whatever the feature says."""

    def test_peer_tokens_get_403_and_systemctl_is_never_called(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "peer.tokens")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("a summary %s\nb views %s\n" % ("s" * 40, "w" * 40))
            cfg = dict(nuc_config.load()["web"], refresh_seconds=2, peer_tokens_file=path)
            srv = web.Server(("127.0.0.1", 0), cfg, VIEWER, demo=True)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            saved, expose, webapps, hostname = dict(render.CFG["web"]), render.CFG["expose"], render.CFG["webapps"], socket.gethostname
            render.CFG["web"] = dict(saved, fix_actions=True, token_file="/etc/nuc-console/token")
            try:
                with mock.patch.object(nuc_config, "LINUX", True), mock.patch.object(fixactions.subprocess, "run") as sub:
                    for tok in ("s" * 40, "w" * 40):
                        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
                        body = urlencode({"id": "stale-net", "csrf": srv.csrf})
                        c.request("POST", "/fix/run", body, {"Authorization": "Bearer " + tok, "Content-Type": "application/x-www-form-urlencoded",
                                                             "Origin": "http://127.0.0.1:%d" % srv.server_address[1]})
                        r = c.getresponse()
                        data = r.read().decode()
                        c.close()
                        self.assertEqual(r.status, 403, tok)
                        self.assertNotIn(srv.csrf, data)
                    sub.assert_not_called()
            finally:
                render.CFG["web"] = saved
                render.CFG["expose"], render.CFG["webapps"], socket.gethostname = expose, webapps, hostname
                render.DEMO = False
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
