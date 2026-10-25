"""The fleet poller on installed macOS and Windows hosts (docs/INSTALL.md, "Fleet poller"): the LaunchDaemon's text, install-macos.sh, and
install-windows.ps1 (the scheduled task and its ACLs). Neither a Mac nor a Windows machine is needed: the plist is parsed, the installers are
read as text, and the macOS three-outcome block runs under bash with stubs. What only a real macOS/Windows host can show (that launchd and the
Task Scheduler accept these lines, what icacls prints) is checked by the install-macos / install-windows jobs of .github/workflows/tests.yml."""
import os
import plistlib
import re
import shutil
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import nuc_config  # noqa: E402


def slurp(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def slurp_path(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class MacPlist(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "launchd", "com.nuc-console.peers.plist"), "rb") as f:
            self.pl = plistlib.load(f)

    def test_own_unprivileged_account_and_the_same_label_style(self):
        self.assertEqual(self.pl["Label"], "com.nuc-console.peers")
        self.assertEqual((self.pl["UserName"], self.pl["GroupName"]), ("_nuc-console-peers", "_nuc-console-peers"))  # not root, not the web view's
        self.assertEqual(self.pl["ProgramArguments"], ["@PYTHON@", "-E", "-B", "@DEST@/peers.py", "serve"])

    def test_outbound_only_and_restarts_on_failure_only(self):
        self.assertFalse({"Sockets", "inetdCompatibility", "WatchPaths", "QueueDirectories", "StartInterval", "EnvironmentVariables"} & set(self.pl))
        self.assertEqual(self.pl["KeepAlive"], {"SuccessfulExit": False})  # a crash comes back; exit 0 (SIGTERM) stays stopped
        self.assertEqual(self.pl["ThrottleInterval"], 30)
        self.assertTrue(self.pl["RunAtLoad"])
        self.assertEqual(self.pl["Umask"], 0o022)  # the cache files are read by the web view's account
        self.assertEqual(self.pl["StandardErrorPath"], "/var/log/nuc-console/peers.log")

    def test_the_cache_is_where_nuc_config_and_the_installer_say(self):
        src = slurp("install-macos.sh")
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_CACHE": "", "NUC_CONSOLE_PEERS_STATE": ""}), mock.patch.object(nuc_config, "PORTABLE", ""), \
                mock.patch.object(nuc_config, "WINDOWS", False), mock.patch.object(nuc_config, "MACOS", True):
            self.assertEqual(nuc_config.peers_cache_dir(), "/var/lib/nuc-console-peers-cache")
            self.assertEqual(nuc_config.peers_state_dir(), "/var/lib/nuc-console-peers")
        self.assertIn("PEERS_CACHE=/var/lib/nuc-console-peers-cache ", src)
        self.assertIn("PEERS_LIB=/var/lib/nuc-console-peers ", src)


class MacInstaller(unittest.TestCase):
    def setUp(self):
        self.src = slurp("install-macos.sh")

    def test_account_folders_and_modes(self):
        s = self.src
        self.assertIn('ensure_service_user "$PEERS_USER" "nuc-console fleet poller"', s)
        self.assertIn('dscl . -create "/Users/$SVC_USER" UserShell /usr/bin/false', s)  # no login shell (the function is shared with the web view's account)
        self.assertIn("seq 400 499", s)  # a free system uid, found per account
        self.assertIn('install -d -m 0750 -o root -g "$PEERS_USER" "$ETC/peers"', s)  # tokens: readable by the poller's group only
        self.assertIn('install -d -m 0750 -o "$PEERS_USER" -g "$PEERS_USER" "$PEERS_LIB"', s)  # state
        self.assertIn('install -d -m 0755 -o "$PEERS_USER" -g "$PEERS_USER" "$PEERS_CACHE"', s)  # cache: read by the web view and the console
        self.assertIn('touch "$LOG/peers.log" && chown "$PEERS_USER:$PEERS_USER" "$LOG/peers.log"', s)
        self.assertIn('fill "launchd/$PEERS_LABEL.plist" > "$LD/$PEERS_LABEL.plist"', s)
        self.assertIn('chown root:wheel "$LD/$PEERS_LABEL.plist" && chmod 0644 "$LD/$PEERS_LABEL.plist"', s)

    def block(self):
        a = self.src.index("peers_rc=0")
        return self.src[a:self.src.index("# ---- 4.", a)]

    def test_three_outcomes(self):
        b = self.block()
        self.assertIn('"$PY" -E "$DEST/peers.py" --needed || peers_rc=$?', b)
        self.assertRegex(b, r'if \[ "\$peers_rc" -eq 0 \]; then\n\s+launchctl enable [^\n]*\n\s+launchctl bootout [^\n]*\n\s+load system "\$LD/\$PEERS_LABEL\.plist" \|\| echo "warning')
        self.assertRegex(b, r'elif \[ "\$peers_rc" -eq 1 \]; then\n\s+launchctl bootout [^\n]*\n\s+launchctl disable [^\n]*\n\s+echo "nuc-console: the fleet poller is installed but off')
        self.assertRegex(b, r'else\n\s+echo "warning: could not tell[^\n]*left as it is" >&2\nfi')
        self.assertEqual(b.count("launchctl disable"), 1, "only the rc 1 branch disables")

    def test_the_upgrade_loop_does_not_stop_it_before_the_decision(self):
        loop = next(ln for ln in self.src.splitlines() if ln.startswith("for label in com.nuc-console.collector com.nuc-console.web com.nuc-console.notify;"))
        self.assertNotIn("peers", loop, "rc 2 leaves it running as it is")

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "bash")
    def test_the_branches_run_as_designed_with_stubs(self):
        b = self.block()
        want = {0: ["enable system/L", "bootout system/L", "load system /LD/L.plist"], 1: ["bootout system/L", "disable system/L"], 2: [], 139: []}
        for rc, calls_want in want.items():
            with tempfile.TemporaryDirectory() as d:
                py = os.path.join(d, "py")
                with open(py, "w") as f:
                    f.write("#!/bin/sh\nexit %d\n" % rc)
                os.chmod(py, 0o755)
                log = os.path.join(d, "calls")
                script = ("set -euo pipefail\nPY=%s\nDEST=/x\nETC=/etc/x\nLD=/LD\nPEERS_LABEL=L\n"
                          "launchctl() { echo \"$*\" >> %s; }\nload() { echo \"load $*\" >> %s; }\n%s\necho reached\n" % (py, log, log, b))
                r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
                calls = slurp_path(log).splitlines() if os.path.exists(log) else []
                self.assertEqual(r.returncode, 0, (rc, r.stderr))
                self.assertIn("reached", r.stdout, "set -e must not end the installer here")
                self.assertEqual(calls, calls_want, rc)
                if rc in (0, 1):
                    self.assertEqual(r.stderr, "")
                if rc not in (0, 1):
                    self.assertIn("could not tell", r.stderr)

    def test_uninstall_removes_the_job_and_keeps_account_state_and_tokens(self):
        down = self.src[self.src.index('"--uninstall" ]'):self.src.index("exit 0")]
        self.assertIn("com.nuc-console.peers", down)
        self.assertIn('"$LD"/com.nuc-console.*.plist', down)  # the plist goes with the glob
        self.assertIn("$ETC/peers", down)  # said to stay
        self.assertNotRegex(down, r"rm -[a-z]*f?r[a-z]* [^\n]*\$PEERS_LIB|rm [^\n]*\$ETC/peers", "state and tokens are not deleted")

    def test_the_update_runs_this_installer(self):
        self.assertIn("bash ./install.sh", slurp("bin", "nuc-console-update"))  # install.sh hands over to install-macos.sh: the job restarts on new code

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck_finds_nothing(self):
        r = subprocess.run(["shellcheck", "-S", "warning", os.path.join(ROOT, "install-macos.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


class WindowsInstaller(unittest.TestCase):
    def setUp(self):
        self.src = slurp("install-windows.ps1")

    def line(self, start):
        return next(ln for ln in self.src.splitlines() if ln.startswith(start))

    def test_folders_and_acls(self):
        s = self.src
        self.assertIn('New-Item -ItemType Directory -Force -Path "$Data\\peers", "$Data\\peers-state", "$Data\\run\\peers"', s)
        tokens, state, cache = (self.line('& icacls.exe "$Data\\%s" /inheritance:r' % n) for n in ("peers", "peers-state", "run\\peers"))
        for ln in (tokens, state, cache):
            self.assertIn("/inheritance:r /grant:r", ln)
            self.assertIn("'*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'", ln)  # SYSTEM and Administrators
            self.assertNotIn("S-1-1-0", ln)  # Everyone
        # tokens: the poller (NETWORK SERVICE) reads, and nobody else: not Users, not the web view's LOCAL SERVICE
        self.assertIn("'*S-1-5-20:(OI)(CI)RX'", tokens)
        self.assertNotIn("S-1-5-32-545", tokens + state)
        self.assertNotIn("S-1-5-19", tokens + state)
        self.assertIn("'*S-1-5-20:(OI)(CI)M'", state)  # state: only the poller writes (and the administrators)
        self.assertIn("'*S-1-5-20:(OI)(CI)M'", cache)  # cache: the poller writes, the web view (LOCAL SERVICE) and users read
        self.assertIn("'*S-1-5-19:(OI)(CI)RX'", cache)
        self.assertIn("'*S-1-5-32-545:(OI)(CI)RX'", cache)
        for n in ("peers", "peers-state", "run\\peers"):
            self.assertRegex(s, r'icacls\.exe "\$Data\\%s" [^\n]*\| Out-Null\nif \(\$LASTEXITCODE -ne 0\) \{ throw "icacls failed on \$Data\\%s" \}' % (re.escape(n), re.escape(n)))

    def test_the_task_is_the_notifiers_kind_as_network_service_with_the_log_option(self):
        s = self.src
        self.assertIn("Register-Service 'peers' 'peers.py' 'S-1-5-20' 'peers.log'", s)  # NETWORK SERVICE, never the web view's LOCAL SERVICE
        self.assertIn("-flags '-E -B' -sub 'serve'", s)
        self.assertIn("function Register-Service($name, $script, $sid, $log, $description, $extra = '', $flags = '-B', $sub = '')", s)
        self.assertIn('-Argument "$flags `"$App\\$script`" $sub --log `"$Data\\logs\\$log`" $extra".TrimEnd()', s)  # -E -B "app\peers.py" serve --log "...\peers.log"
        self.assertIn("-RestartCount 999", s)  # restart on failure (shared with the other tasks), and the 5-minute repetition
        # the web and notify tasks keep their old command line
        self.assertIn("Register-Service 'notify' 'notify.py' 'S-1-5-20' 'notify.log' 'nuc-console Telegram notifier: outbound only (NETWORK SERVICE)'\n", s)
        self.assertIn("'web.log' 'nuc-console web view (LOCAL SERVICE): a read-only dashboard, and the AI page has buttons' '--local'", s)

    def test_three_outcomes(self):
        s = self.src
        a = s.index("function Register-Peers")
        b = s[a:s.index("if ($mode -ne 'none') {  # at every logon")]
        self.assertIn("try { & $python -E -B (Join-Path $App 'peers.py') --needed; $peersRc = $LASTEXITCODE } catch { $peersRc = 2 } finally { $ErrorActionPreference = $old }", b)
        self.assertIn("$ErrorActionPreference = 'Continue'", b)  # a native command's stderr must not stop the script (Stop is set at the top)
        self.assertIn("$old, $peersRc = $ErrorActionPreference, 2", b)  # unknown unless the program answered
        self.assertIn("if ($peersRc -eq 0) { Register-Peers }", b)
        self.assertRegex(b, r"elseif \(\$peersRc -eq 1\) \{ Say '[^\n]*installed but off[^\n]*' \}\nelse \{\n\s+Say \"warning: could not tell[^\n]*left as it is\"\n\s+if \(\$peersTask\) \{ Register-Peers \}\n\}")
        self.assertEqual(b.count("Register-Peers"), 3, "the function and its two callers: rc 0, and rc 2 with a task from an earlier install")
        self.assertNotIn("Unregister", b)  # rc 1: Remove-Tasks already took the task of the previous install away

    def test_an_earlier_task_is_noted_before_anything_removes_the_tasks(self):
        s = self.src
        note = s.index("$peersTask = [bool](Get-ScheduledTask -TaskPath $TaskPath -TaskName 'peers'")
        self.assertLess(note, s.index("Remove-Tasks -KeepAi"))
        self.assertLess(s.index("if ($Uninstall) {"), note, "after the uninstall branch, which removes the task for good")
        self.assertLess(s.index('"$Data\\run\\peers" /inheritance:r'), s.index("# ---- 2. private Python"))

    def test_uninstall_removes_the_task_and_keeps_the_tokens(self):
        down = self.src[self.src.index("if ($Uninstall) {"):self.src.index("# ---- 1. data folder")]
        self.assertIn("Remove-Tasks", down)  # every task of \nuc-console\, the poller's included
        self.assertNotRegex(down, r"Remove-Item[^\n]*peers", "nothing of the poller's folders is removed")
        self.assertIn("the tokens of the fleet peers in $Data\\peers", down)  # said to stay

    def test_the_fleet_command_and_the_update_path_exist(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "bin", "nuc-console-fleet.cmd")))
        self.assertIn("install-windows.ps1", slurp("bin", "nuc-console-update.ps1"))  # the update runs this installer: the task is registered again


class Workflow(unittest.TestCase):
    def test_ci_checks_the_folders_and_that_no_job_is_made_without_a_peer(self):
        w = slurp(".github", "workflows", "tests.yml")
        self.assertIn("test -f /Library/LaunchDaemons/com.nuc-console.peers.plist", w)
        self.assertIn("/var/lib/nuc-console-peers-cache", w)
        self.assertRegex(w, r"-TaskName 'peers' -ErrorAction SilentlyContinue\) \{ throw \"a peers task exists with \[hosts\] empty")
        self.assertIn("$t.Principal.UserId -notmatch 'NETWORK SERVICE'", w)


if __name__ == "__main__":
    unittest.main()
