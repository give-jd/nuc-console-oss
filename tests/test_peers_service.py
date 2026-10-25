"""The fleet poller as a service (docs/INSTALL.md, "Fleet poller"): the systemd unit's text, install.sh, the serve loop with an injected clock,
and the portable run (run.sh starts and stops it only when [hosts] has a peer). No systemd, no network: a peer in these tests is a URL whose
token file is missing, so the poll ends before any socket exists."""
import os
import re
import shutil
import signal
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import io
import json
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import nuc_config  # noqa: E402
import peers  # noqa: E402
from test_portable import tree_copy  # noqa: E402

WINDOWS = sys.platform == "win32"


def slurp(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def slurp_path(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def directives(name):
    out = {}
    for ln in slurp("systemd", name).splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln and not ln.startswith("["):
            k, v = ln.split("=", 1)
            out.setdefault(k, []).append(v)
    return out


class UnitText(unittest.TestCase):
    def setUp(self):
        self.u = directives("nuc-console-peers.service")

    def one(self, k):
        self.assertEqual(len(self.u.get(k, [])), 1, k)
        return self.u[k][0]

    def test_its_own_account_and_the_exact_command(self):
        self.assertEqual((self.one("User"), self.one("Group")), ("nuc-console-peers", "nuc-console-peers"))
        self.assertEqual(self.one("ExecStart"), "/usr/bin/python3 -E -B /opt/nuc-console/peers.py serve")
        self.assertNotIn("ExecStartPre", self.u)
        self.assertNotIn("ExecStartPost", self.u)
        self.assertEqual(self.one("Restart"), "on-failure")
        self.assertRegex(self.one("RestartSec"), r"^[0-9]+$")

    def test_folders(self):
        self.assertEqual((self.one("RuntimeDirectory"), self.one("RuntimeDirectoryMode")), ("nuc-console-peers", "0755"))
        self.assertEqual((self.one("StateDirectory"), self.one("StateDirectoryMode")), ("nuc-console-peers", "0750"))
        self.assertEqual(self.one("ReadWritePaths").split(), ["/run/nuc-console-peers", "/var/lib/nuc-console-peers"])
        self.assertEqual(self.one("ReadOnlyPaths"), "/etc/nuc-console")

    def test_hardening_is_not_weaker_than_the_notifiers(self):
        notify = directives("nuc-console-notify.service")
        for k, v in notify.items():
            if k in ("NoNewPrivileges", "ProtectSystem", "ProtectHome", "PrivateTmp", "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules",
                     "ProtectControlGroups", "ProtectClock", "ProtectHostname", "RestrictNamespaces", "RestrictSUIDSGID", "LockPersonality", "CapabilityBoundingSet"):
                self.assertEqual(self.u.get(k), v, k)
        self.assertEqual(self.u.get("TasksMax"), notify["TasksMax"])
        self.assertEqual(self.u.get("MemoryMax"), ["1G"], "16 peers x 5 documents x ~5.7 parsed: the arithmetic is in the unit's comment")
        self.assertIn("5.7", slurp("systemd", "nuc-console-peers.service"))
        for k in ("MemoryDenyWriteExecute", "RestrictRealtime", "ProtectKernelLogs"):
            self.assertEqual(self.u.get(k), ["yes"], k)
        self.assertEqual(self.u.get("RestrictAddressFamilies"), ["AF_INET AF_INET6 AF_UNIX"], "AF_UNIX for nss-resolve, nscd, sssd")
        self.assertEqual(self.u["SystemCallFilter"], ["@system-service", "~@privileged @resources"])
        self.assertEqual(self.u["CapabilityBoundingSet"], [""])

    def test_no_listener_and_nothing_that_widens_the_sandbox(self):
        text = slurp("systemd", "nuc-console-peers.service")
        for bad in ("ListenStream", "Sockets=", "AmbientCapabilities=CAP", "PrivateNetwork=no", "SupplementaryGroups", "User=root", "PermissionsStartOnly",
                    "ReadWritePaths=/etc", "ReadWritePaths=-/etc", "BindPaths", "Environment=", "EnvironmentFile"):
            self.assertNotIn(bad, text, bad)

    def test_the_token_folder_is_what_read_secret_accepts(self):
        """The unit and install.sh say: folder 0750 root:nuc-console-peers (not writable by group/other), tokens 0600 owned by the poller's account."""
        src = slurp("install.sh")
        self.assertIn("install -d -m 0750 -o root -g nuc-console-peers /etc/nuc-console/peers", src)
        self.assertIn("install -d -m 0750 -o nuc-console-peers -g nuc-console-peers /var/lib/nuc-console-peers", src)
        doc = slurp("docs", "INSTALL.md")
        self.assertIn("install -o nuc-console-peers -g nuc-console-peers -m 0600", doc)


class InstallScript(unittest.TestCase):
    def setUp(self):
        self.src = slurp("install.sh")

    def test_account_made_only_when_missing(self):
        self.assertIn("id nuc-console-peers >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin nuc-console-peers", self.src)

    def test_the_unit_is_installed_by_the_glob_and_named_python_is_rewritten(self):
        self.assertIn("install -m 0644 systemd/*.service /etc/systemd/system/", self.src)
        self.assertIn("/etc/systemd/system/nuc-console*.service", self.src)  # the sed that swaps the system Python for the archive's

    def block(self):
        a = self.src.index("peers_rc=0")
        return self.src[a:self.src.index("systemctl restart nuc-console.service", a)]

    def test_three_outcomes_enable_and_restart_disable_or_leave_alone(self):
        b = self.block()
        self.assertIn('"$PY" -E "$DEST/peers.py" --needed || peers_rc=$?', b)
        self.assertRegex(b, r'if \[ "\$peers_rc" -eq 0 \]; then\n\s+\{ systemctl enable nuc-console-peers\.service && systemctl restart nuc-console-peers\.service; \} \|\| echo "warning')
        self.assertRegex(b, r'elif \[ "\$peers_rc" -eq 1 \]; then\n\s+systemctl disable --now nuc-console-peers\.service')
        self.assertIn("systemctl enable --now nuc-console-peers", b)  # and it says how
        self.assertRegex(b, r'else\n\s+echo "warning: could not tell[^\n]*left as it is" >&2\nfi')
        self.assertEqual(b.count("disable"), 1, "only the rc 1 branch disables")
        self.assertLess(self.src.index("systemctl daemon-reload\nsystemctl enable nuc-console-collector"), self.src.index(b), "units are read before they are started")

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "bash")
    def test_the_branches_run_as_designed_with_a_stub_interpreter_and_systemctl(self):
        b = self.block()
        for rc, want in ((0, ["enable nuc-console-peers.service", "restart nuc-console-peers.service"]), (1, ["disable --now nuc-console-peers.service"]),
                         (2, []), (139, []), (0, None)):
            with tempfile.TemporaryDirectory() as d:
                py = os.path.join(d, "py")
                with open(py, "w") as f:
                    f.write("#!/bin/sh\nexit %d\n" % rc)
                os.chmod(py, 0o755)
                log = os.path.join(d, "calls")
                fail = "restart" if want is None else ""
                script = ("set -euo pipefail\nPY=%s\nDEST=/x\nsystemctl() { echo \"$*\" >> %s; case \"$*\" in *%s*) return 1;; esac; }\n%s\necho reached\n"
                          % (py, log, fail or "NEVERMATCH", b))
                r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
                calls = slurp_path(log).splitlines() if os.path.exists(log) else []
                self.assertEqual(r.returncode, 0, (rc, r.stderr))
                self.assertIn("reached", r.stdout, "set -e must not end the installer here")
                if want is not None:
                    self.assertEqual(calls, want, rc)
                if rc not in (0, 1):
                    self.assertIn("could not tell", r.stderr)
                    self.assertEqual(calls, [], "state left alone")
                if want is None:
                    self.assertIn("did not start", r.stderr)

    def test_uninstall_stops_and_removes_the_unit(self):
        down = self.src[self.src.index('"--uninstall" ]'):self.src.index("exit 0")]
        self.assertIn("nuc-console-peers.service nuc-console-notify.service; do", down)
        self.assertIn("/etc/systemd/system/nuc-console-peers.service", down)

    def test_the_update_runs_this_installer(self):
        self.assertIn("bash ./install.sh", slurp("bin", "nuc-console-update"))  # so a running poller restarts on the new code

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck_finds_nothing_new(self):
        r = subprocess.run(["shellcheck", "-S", "warning", os.path.join(ROOT, "install.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_needed_says_whether_hosts_has_a_peer(self):
        out = []
        self.assertEqual(peers.main(["--needed"], out=out.append, cfg={"hosts": [{"name": "a"}]}), 0)
        self.assertEqual(peers.main(["--needed"], out=out.append, cfg={"hosts": []}), 1)
        self.assertEqual(out, [])


class FakeStop(object):
    """A stop event that is a clock: wait(t) is a tick; the script runs one action per tick, then stop is set."""

    def __init__(self, script):
        self.script, self.i, self.waits, self.flag = list(script), 0, [], False

    def is_set(self):
        return self.flag

    def wait(self, t):
        self.waits.append(t)
        if self.i < len(self.script):
            self.script[self.i]()
        else:
            self.flag = True
        self.i += 1
        return self.flag


class FakePoller(object):
    made = []

    def __init__(self, hosts, folder, state=None, **net):
        self.hosts, self.folder, self.state, self.steps, self.closed = hosts, folder, state, 0, False
        self.raise_on_step = None
        self.pool = mock.Mock()
        FakePoller.made.append(self)

    def step(self):
        self.steps += 1
        if self.raise_on_step:
            raise self.raise_on_step


class Serve(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nuc-serve-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cache, self.state = os.path.join(self.dir, "cache"), os.path.join(self.dir, "state")
        env = mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_CACHE": self.cache, "NUC_CONSOLE_PEERS_STATE": self.state})
        env.start()
        self.addCleanup(env.stop)
        FakePoller.made = []
        self.cfg = os.path.join(self.dir, "config.ini")
        self.hosts = []
        self.err = io.StringIO()
        e = mock.patch.object(sys, "stderr", self.err)
        e.start()
        self.addCleanup(e.stop)

    def touch(self, n=1):
        with open(self.cfg, "w") as f:
            f.write("# %d\n" % n)
        os.utime(self.cfg, (n, n + 1000 * n))  # a different mtime each time

    def run_loop(self, script, flag=None):
        stop = FakeStop(script)
        rc = peers.serve(stop, flag or threading.Event(), load=lambda: {"hosts": self.hosts}, path=self.cfg, make=FakePoller)
        self.assertEqual(rc, 0)
        return stop

    def test_no_peers_means_no_poller_and_only_waiting(self):
        self.touch()
        stop = self.run_loop([lambda: None] * 5)
        self.assertEqual(FakePoller.made, [])
        self.assertEqual(len(stop.waits), 6)  # it waited each time: no busy loop
        self.assertTrue(all(w > 0 for w in stop.waits))
        self.assertEqual(self.err.getvalue().count("idle: no peers"), 1, "said once")

    def test_a_missing_config_file_is_not_a_crash(self):
        self.run_loop([lambda: None] * 2)
        self.assertEqual(FakePoller.made, [])
        self.assertNotIn("round failed", self.err.getvalue())

    def test_peers_start_one_poller_stepped_every_tick(self):
        self.hosts = [{"name": "lab"}]
        self.touch()
        self.run_loop([lambda: None] * 3)
        self.assertEqual(len(FakePoller.made), 1)
        p = FakePoller.made[0]
        self.assertEqual((p.steps, p.folder, p.state), (4, self.cache, self.state))
        p.pool.shutdown.assert_called_once()

    def test_a_changed_config_file_is_reloaded_an_unchanged_one_is_not(self):
        self.hosts = [{"name": "lab"}]
        self.touch(1)

        def edit():
            self.hosts = [{"name": "lab"}, {"name": "b"}]
            self.touch(2)
        self.run_loop([lambda: None, edit, lambda: None, lambda: None])
        self.assertEqual([len(p.hosts) for p in FakePoller.made], [1, 2])
        FakePoller.made[0].pool.shutdown.assert_called_once()  # the old pool is released

    def test_sighup_rereads_even_when_the_file_looks_the_same(self):
        self.hosts = [{"name": "lab"}]
        self.touch()
        flag = threading.Event()

        def hup():
            self.hosts = [{"name": "other"}]
            flag.set()
        self.run_loop([lambda: None, hup, lambda: None], flag)
        self.assertEqual([p.hosts[0]["name"] for p in FakePoller.made], ["lab", "other"])
        self.assertFalse(flag.is_set())

    def test_same_hosts_after_a_reload_keep_the_poller_and_its_backoff(self):
        self.hosts = [{"name": "lab"}]
        self.touch(1)
        self.run_loop([lambda: self.touch(2), lambda: None])
        self.assertEqual(len(FakePoller.made), 1)

    def test_emptied_hosts_stop_the_poller_and_clear_the_cache(self):
        self.hosts = [{"name": "lab"}]
        self.touch(1)
        os.makedirs(self.cache)
        peers.write_cache(self.cache, [peers.new_entry({"name": "lab", "url": "http://100.64.0.1", "scope": "summary", "alert": False, "insecure": False}, 1)], 1)
        self.assertTrue(os.path.exists(os.path.join(self.cache, "lab.json")))

        def empty():
            self.hosts = []
            self.touch(2)
        self.run_loop([lambda: None, empty, lambda: None])
        self.assertFalse(os.path.exists(os.path.join(self.cache, "lab.json")))
        FakePoller.made[0].pool.shutdown.assert_called_once()
        self.assertEqual(len(FakePoller.made), 1)

    def test_an_exception_in_a_round_is_logged_once_and_the_loop_goes_on(self):
        self.hosts = [{"name": "lab"}]
        self.touch()

        def boom():
            FakePoller.made[0].raise_on_step = RuntimeError("secret-token-abc /etc/nuc-console/peers/x.token")
        stop = self.run_loop([lambda: None, boom, lambda: None, lambda: None, lambda: None])
        self.assertEqual(len(stop.waits), 6)  # it survived to the end
        self.assertEqual(self.err.getvalue().count("round failed"), 1)
        self.assertNotIn("secret", self.err.getvalue())
        self.assertNotIn("/etc", self.err.getvalue())
        self.assertGreater(FakePoller.made[0].steps, 4)

    def test_a_loader_that_raises_is_survived_too(self):
        self.touch()
        stop = FakeStop([lambda: None, lambda: self.touch(2), lambda: None])
        peers.serve(stop, threading.Event(), load=mock.Mock(side_effect=ValueError("/etc/nuc-console/config.ini line 3")), path=self.cfg, make=FakePoller)
        self.assertEqual(len(stop.waits), 4)
        self.assertEqual(self.err.getvalue().count("round failed"), 1)

    def test_a_real_poller_with_a_peer_that_has_no_token_file_writes_the_cache_and_never_touches_the_network(self):
        peer = {"name": "lab", "url": "http://127.0.0.1:1", "token": os.path.join(self.dir, "missing.token"), "pin": "", "scope": "summary",
                "alert": False, "insecure": False}
        self.hosts = [peer]
        self.touch()
        stop = threading.Event()
        flag = threading.Event()
        t = threading.Thread(target=peers.serve, args=(stop, flag), kwargs=dict(load=lambda: {"hosts": self.hosts}, path=self.cfg, tick=0.02))
        t.start()
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(os.path.join(self.cache, "lab.json")):
            time.sleep(0.02)
        stop.set()
        t.join(10)
        self.assertFalse(t.is_alive())
        entry = peers.read_cache(self.cache)["peers"]["lab"]
        self.assertEqual((entry["state"], entry["error"]), ("unauthorized", "token-file"))


class Signals(unittest.TestCase):
    def test_serve_main_registers_what_the_os_has_and_needs_no_sighup(self):
        seen = {}
        with mock.patch.object(signal, "signal", lambda s, f: seen.setdefault(s, f)), mock.patch.object(peers, "serve", lambda stop, flag, **kw: (stop, flag)):
            if hasattr(signal, "SIGHUP"):
                saved = signal.SIGHUP
                self.addCleanup(setattr, signal, "SIGHUP", saved)
                del signal.SIGHUP  # as on Windows
            stop, flag = peers.serve_main()
        self.assertIn(signal.SIGTERM, seen)
        self.assertIn(signal.SIGINT, seen)
        seen[signal.SIGTERM](signal.SIGTERM, None)
        self.assertTrue(stop.is_set())
        self.assertFalse(flag.is_set())

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "no SIGHUP here")
    def test_sighup_sets_the_reload_flag(self):
        seen = {}
        with mock.patch.object(signal, "signal", lambda s, f: seen.setdefault(s, f)), mock.patch.object(peers, "serve", lambda stop, flag, **kw: (stop, flag)):
            stop, flag = peers.serve_main()
        seen[signal.SIGHUP](signal.SIGHUP, None)
        self.assertTrue(flag.is_set())
        self.assertFalse(stop.is_set())

    def test_scrub_env_runs_first_and_the_command_line_cannot_open_loopback(self):
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/x"}), mock.patch.object(peers, "serve", lambda *a, **k: 0):
            self.assertEqual(peers.serve_main(), 0)
            self.assertNotIn("SSL_CERT_FILE", os.environ)
        src = slurp("src", "peers.py")
        self.assertNotIn("allow_loopback", src[src.index("def serve("):src.index("def main(")].replace("`allow_loopback`", ""))

    def test_main_dispatch_takes_only_serve_and_serve_log(self):
        out = []
        with mock.patch.object(peers, "serve_main", lambda net=None: 7):
            self.assertEqual(peers.main(["serve"], out=out.append), 7)
            self.assertEqual(peers.main(["serve", "extra"], out=out.append, cfg={"hosts": []}), 2)
            self.assertEqual(peers.main(["serve", "--allow-loopback"], out=out.append, cfg={"hosts": []}), 2)


class Needed(unittest.TestCase):
    """peers.py --needed: 0 a peer is configured, 1 none, 2 unknown (the callers then leave things as they are)."""

    def run_main(self, **kw):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            return peers.main(["--needed"], out=lambda _l: None, **kw), err.getvalue()

    def test_zero_one_two(self):
        self.assertEqual(self.run_main(cfg={"hosts": [{"name": "a"}], "config_error": ""})[0], 0)
        self.assertEqual(self.run_main(cfg={"hosts": [], "config_error": ""})[0], 1)
        rc, err = self.run_main(cfg={"hosts": [], "config_error": "cannot open /x"})
        self.assertEqual((rc, err.strip()), (2, "nuc-console-peers: config unreadable"))

    def test_a_crash_is_two_not_one(self):
        with mock.patch.object(nuc_config, "load", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_main()[0], 2)

    def test_a_garbage_or_unreadable_config_file_is_two_and_a_missing_one_is_one(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.ini")
            with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": path}):
                self.assertEqual(self.run_main()[0], 1)  # no file: defaults, no peers
                with open(path, "w") as f:
                    f.write("[hosts\nlab = http://100.64.0.1\n")
                self.assertEqual(self.run_main()[0], 2)
                with open(path, "w") as f:
                    f.write("[hosts]\nlab = http://100.64.0.1:8080\n")
                self.assertEqual(self.run_main()[0], 0)
                if hasattr(os, "geteuid") and os.geteuid() != 0:
                    os.chmod(path, 0)
                    self.assertEqual(self.run_main()[0], 2)


class ServeConfigErrors(Serve):
    """M2: a config that cannot be read at this moment changes nothing; only a good one with an empty [hosts] clears the cache."""

    def good(self):
        with open(self.cfg, "w") as f:
            f.write("[hosts]\nlab = http://100.64.0.1:8080\n")

    def serve_real_loader(self, script):
        stop = FakeStop(script)
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": self.cfg}):
            peers.serve(stop, threading.Event(), path=self.cfg, make=FakePoller)
        return stop

    def cache_with_lab(self):
        os.makedirs(self.cache)
        peers.write_cache(self.cache, [peers.new_entry({"name": "lab", "url": "http://100.64.0.1", "scope": "summary", "alert": False, "insecure": False}, 1)], 1)

    def test_an_injected_error_keeps_the_poller_and_the_cache_and_is_logged_once(self):
        self.cache_with_lab()
        answers = iter([{"hosts": [{"name": "lab"}], "config_error": ""}, {"hosts": [], "config_error": "cannot open"}, {"hosts": [], "config_error": "cannot open"},
                        {"hosts": [{"name": "lab"}], "config_error": ""}])
        self.touch(1)
        stop = FakeStop([lambda: self.touch(2), lambda: self.touch(3), lambda: self.touch(4), lambda: None])
        peers.serve(stop, threading.Event(), load=lambda: next(answers), path=self.cfg, make=FakePoller)
        self.assertEqual(len(FakePoller.made), 1, "never rebuilt: same hosts before and after")
        FakePoller.made[0].pool.shutdown.assert_called_once()  # only the final stop
        self.assertTrue(os.path.exists(os.path.join(self.cache, "lab.json")))
        self.assertEqual(self.err.getvalue().count("config unreadable"), 1)

    def test_a_garbage_or_truncated_file_after_a_good_one(self):
        self.good()
        self.cache_with_lab()
        for bad in ("[hosts\nlab = http://100.64.0.1\n", "\x00\x01garbage", "[hosts]\nlab ="[:3]):
            FakePoller.made = []
            self.good()
            os.utime(self.cfg, (1, 1))

            def spoil(bad=bad):
                with open(self.cfg, "w") as f:
                    f.write(bad)
                os.utime(self.cfg, (2, 2))
            self.serve_real_loader([lambda: None, spoil, lambda: None, lambda: None])
            self.assertEqual(len(FakePoller.made), 1, repr(bad))
            self.assertTrue(os.path.exists(os.path.join(self.cache, "lab.json")), repr(bad))

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() != 0, "root reads a mode-000 file")
    def test_a_file_that_became_unreadable(self):
        self.good()
        self.cache_with_lab()

        def lock():
            os.chmod(self.cfg, 0)
            os.utime(self.cfg, (5, 5))
        self.serve_real_loader([lambda: None, lock, lambda: None])
        self.assertEqual(len(FakePoller.made), 1)
        self.assertTrue(os.path.exists(os.path.join(self.cache, "lab.json")))

    def test_a_good_file_with_an_empty_list_does_clear_the_cache(self):
        self.good()
        self.cache_with_lab()

        def empty():
            with open(self.cfg, "w") as f:
                f.write("[features]\nmap = yes\n")
            os.utime(self.cfg, (9, 9))
        self.serve_real_loader([lambda: None, empty, lambda: None])
        self.assertFalse(os.path.exists(os.path.join(self.cache, "lab.json")))

    def test_a_loader_that_raises_is_retried_on_the_next_tick(self):
        answers = [ValueError("x"), {"hosts": [{"name": "lab"}], "config_error": ""}]

        def load():
            a = answers.pop(0) if answers else {"hosts": [{"name": "lab"}], "config_error": ""}
            if isinstance(a, Exception):
                raise a
            return a
        self.touch()
        peers.serve(FakeStop([lambda: None, lambda: None]), threading.Event(), load=load, path=self.cfg, make=FakePoller)
        self.assertEqual(len(FakePoller.made), 1, "the file did not change, yet the second try succeeded")


class RetainedMemory(unittest.TestCase):
    """M3: the poller keeps no documents of its peers in memory, and reads one file when it needs the last good state."""

    def setUp(self):
        import fakepeer
        self.dir = tempfile.mkdtemp(prefix="nuc-mem-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cache, self.state = os.path.join(self.dir, "cache"), os.path.join(self.dir, "state")
        self.tokfile = os.path.join(self.dir, "t.token")
        with open(self.tokfile, "w") as f:
            f.write("Tk9" * 8)
        os.chmod(self.tokfile, 0o600)
        if os.name == "posix":
            os.chmod(self.dir, 0o700)
        self.fake = fakepeer.FakePeer(token="Tk9" * 8)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        big = {"api": 1, "view": "x", "rows": [{"a": "z" * 40, "n": i} for i in range(4000)]}
        for name in peers.VIEWS:
            self.fake.docs[name] = dict(big, view=name)
        self.peer = {"name": "p", "url": self.fake.url, "token": self.tokfile, "pin": "", "scope": "views", "alert": False, "insecure": False}

    def poller(self):
        return peers.Poller([self.peer], self.cache, state=self.state, allow_loopback=True, limits=peers.Limits(op=2, total=4))

    def test_retained_entries_are_small_and_the_documents_are_on_disk(self):
        p = self.poller()
        e = p.run_once()[0]
        p.pool.shutdown(wait=False)
        self.assertEqual(e["state"], "ok", e)
        self.assertEqual(e["views"], {})
        self.assertEqual(sorted(e["view_index"]), sorted(peers.VIEWS))
        self.assertLess(len(json.dumps(p.entries["p"])), 3000)
        on_disk = os.path.getsize(os.path.join(self.cache, "p.views.json"))
        self.assertGreater(on_disk, 500000)
        self.assertLess(os.path.getsize(os.path.join(self.cache, "p.json")), 3000)
        self.assertEqual(sorted(peers.read_views(self.cache, "p")), sorted(peers.VIEWS))

    def test_the_last_good_documents_survive_an_unreachable_round(self):
        p = self.poller()
        p.run_once()
        self.fake.stop()
        e = p.run_once()[0]
        p.pool.shutdown(wait=False)
        self.assertIn("showing-old-data", e["warn"])
        self.assertEqual(sorted(peers.read_views(self.cache, "p")), sorted(peers.VIEWS), "the file still has them")

    def test_last_reads_one_peer_not_the_folder(self):
        p = self.poller()
        p.run_once()
        p.pool.shutdown(wait=False)
        opened = []
        real = peers.read_json
        with mock.patch.object(peers, "read_json", lambda path, limit: opened.append(os.path.basename(path)) or real(path, limit)), \
                mock.patch.object(os, "listdir", side_effect=AssertionError("the folder is not listed")):
            got = p.last("p")
            self.assertIsNone(p.last("../x"))
        self.assertEqual(sorted(opened), ["p.json", "p.views.json"])
        self.assertEqual(sorted(got["views"]), sorted(peers.VIEWS))


class Closing(unittest.TestCase):
    def test_pending_polls_are_cancelled_the_one_in_flight_is_left_to_end(self):
        from concurrent.futures import Future
        pending, flying = Future(), Future()
        flying.set_running_or_notify_cancel()
        poller = mock.Mock(running={"a": pending, "b": flying})
        peers._close(poller)
        self.assertTrue(pending.cancelled())
        self.assertFalse(flying.cancelled())
        poller.pool.shutdown.assert_called_once_with(wait=False)


class LogFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nuc-log-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name in ("stdout", "stderr"):
            m = mock.patch.object(sys, name, getattr(sys, name))
            m.start()
            self.addCleanup(m.stop)
        self.addCleanup(lambda: sys.stderr.close() if sys.stderr not in (sys.__stderr__,) and hasattr(sys.stderr, "name") and isinstance(sys.stderr.name, int) else None)

    def test_creates_only_the_last_folder_and_a_private_file(self):
        log = os.path.join(self.dir, "logs", "peers.log")
        peers._log_to(log)
        print("hello", file=sys.stderr)
        sys.stderr.flush()
        self.assertEqual(slurp_path(log), "hello\n")
        if os.name == "posix":
            self.assertEqual(os.stat(log).st_mode & 0o777, 0o600)
        with self.assertRaises(OSError):
            peers._log_to(os.path.join(self.dir, "no", "parent", "peers.log"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "no")))

    @unittest.skipUnless(hasattr(os, "symlink") and hasattr(os, "O_NOFOLLOW"), "links")
    def test_a_link_in_place_of_the_log_is_refused(self):
        target = os.path.join(self.dir, "victim")
        with open(target, "w") as f:
            f.write("keep")
        link = os.path.join(self.dir, "peers.log")
        os.symlink(target, link)
        with self.assertRaises(OSError):
            peers._log_to(link)
        self.assertEqual(slurp_path(target), "keep")

    def test_serve_log_failure_is_a_clean_exit_code(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(peers.main(["serve", "--log", os.path.join(self.dir, "no", "x", "p.log")], out=lambda _l: None), 1)
        self.assertEqual(err.getvalue().strip(), "nuc-console-peers: cannot open the log")


@unittest.skipIf(WINDOWS, "run.sh: Linux and macOS")
class PortableRunSh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(os.path.realpath(self.tmp.name), "nuc-console")
        os.mkdir(self.dir)
        tree_copy(self.dir)
        self.env = dict(os.environ, PYTHON=sys.executable, HOME=self.tmp.name)
        for k in list(self.env):
            if k.startswith("NUC_CONSOLE_"):
                del self.env[k]
        self.data = os.path.join(self.dir, "data")

    def procs(self, root):
        rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True).stdout.splitlines()
        parsed = [(int(r.split(None, 2)[0]), int(r.split(None, 2)[1]), r.split(None, 2)[2] if len(r.split(None, 2)) > 2 else "") for r in rows if r.strip()]
        found, todo = {}, [root]
        while todo:
            cur = todo.pop()
            for p, pp, args in parsed:
                if pp == cur and p not in found:
                    found[p] = args
                    todo.append(p)
        return found

    def run_it(self, config):
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "config.ini"), "w") as f:
            f.write(config)
        p = subprocess.Popen([os.path.join(self.dir, "run.sh"), "--web", "--no-open"], cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        self.addCleanup(self.reap, p)
        weblog = os.path.join(self.data, "logs", "web.log")
        deadline = time.time() + 90
        while time.time() < deadline and not (os.path.exists(weblog) and "web view on" in slurp_path(weblog)):
            self.assertIsNone(p.poll(), "run.sh ended early")
            time.sleep(0.2)
        time.sleep(0.5)  # the poller starts before the web view: it is there by now
        return p

    def reap(self, p):
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
        if p.stdout:
            p.stdout.close()

    def alive(self, pid):
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(r) and not r.startswith("Z")

    def serving(self, kids):
        return [pid for pid, args in kids.items() if "peers.py serve" in args]

    def test_started_with_a_peer_stopped_with_the_launcher_and_not_started_without_one(self):
        p = self.run_it("[hosts]\nlab = http://127.0.0.1:1\n")
        kids = self.procs(p.pid)
        poller = self.serving(kids)
        self.assertEqual(len(poller), 1, kids)
        self.assertIn("-E", kids[poller[0]])
        cache = os.path.join(self.data, "peers-cache", "lab.json")
        deadline = time.time() + 30
        while time.time() < deadline and not os.path.exists(cache):
            time.sleep(0.1)
        self.assertTrue(os.path.exists(cache))
        self.assertTrue(os.path.isdir(os.path.join(self.data, "peers-cache")))
        os.kill(p.pid, signal.SIGTERM)
        p.wait(timeout=30)
        deadline = time.time() + 10
        while time.time() < deadline and any(self.alive(k) for k in kids):
            time.sleep(0.1)
        self.assertEqual([k for k in kids if self.alive(k)], [], "orphans left")
        # the same folder, no peers: nothing is started, and the log of the first run is not touched by a second poller
        p2 = self.run_it("[features]\nmap = yes\n")
        self.assertEqual(self.serving(self.procs(p2.pid)), [])
        os.kill(p2.pid, signal.SIGTERM)
        p2.wait(timeout=30)


@unittest.skipIf(WINDOWS, "run.sh: Linux and macOS")
class PortableUnknown(PortableRunSh):
    def test_an_unreadable_config_starts_nothing_and_says_so(self):
        p = self.run_it("[hosts\nlab = http://127.0.0.1:1\n")
        self.assertEqual(self.serving(self.procs(p.pid)), [])
        os.kill(p.pid, signal.SIGTERM)
        out = p.communicate(timeout=30)[0]
        self.assertIn("the fleet poller was not started: could not tell", out)

    test_started_with_a_peer_stopped_with_the_launcher_and_not_started_without_one = None


class LaunchersAndDocs(unittest.TestCase):
    def test_run_sh_does_not_let_the_environment_move_the_peers_folders(self):
        line = next(ln for ln in slurp("run.sh").splitlines() if ln.startswith("unset NUC_CONSOLE_"))
        self.assertIn("NUC_CONSOLE_PEERS_CACHE", line)
        self.assertIn("NUC_CONSOLE_PEERS_STATE", line)

    def test_the_launchers_warn_visibly_on_an_unknown_answer(self):
        sh, ps = slurp("run.sh"), slurp("run.ps1")
        self.assertNotIn("--needed 2>/dev/null", sh)
        self.assertIn('elif [ "$peers_rc" -ne 1 ]; then', sh)
        self.assertIn("was not started: could not tell", sh)
        self.assertNotIn("--needed 2>$null", ps)
        self.assertIn("-ne 0 -and $rc -ne 1", ps)

    def test_run_ps1_starts_the_poller_only_with_a_peer(self):
        s = slurp("run.ps1")
        i = s.index("peers.py') --needed")
        j = s.index("'serve', '--log'")
        self.assertLess(i, j)
        self.assertIn("if ($needed)", s[i:j])
        self.assertIn("Start-Child (Join-Path $Src 'peers.py')", s[i:j + 120])

    def test_run_sh_starts_it_in_the_background_inside_the_cleanup_list(self):
        s = slurp("run.sh")
        i = s.index('peers.py" --needed')
        block = s[i:s.index("# the baseline of the port alarms")]
        self.assertIn('peers.py" serve >>"$LOGS/peers.log" 2>&1 &', block)
        self.assertIn('PIDS="$PIDS $!"', block)

    def test_the_serve_log_option_exists_for_windows(self):
        with mock.patch.object(peers, "_log_to") as log, mock.patch.object(peers, "serve_main", lambda net=None: 0):
            self.assertEqual(peers.main(["serve", "--log", "x.log"], out=lambda _l: None), 0)
        log.assert_called_once_with("x.log")

    def test_docs_say_it_all(self):
        install = slurp("docs", "INSTALL.md")
        self.assertIn("## Fleet poller", install)
        self.assertRegex(install, r"sudo -u nuc-console-peers nuc-console-fleet test \w+")
        self.assertIn("not yet installed as a service", install)  # macOS and Windows: step 8
        for doc in ("README.md", "docs/CONFIGURATION.md", "docs/WEB.md"):
            self.assertIn("nuc-console-peers", slurp(*doc.split("/")), doc)
        self.assertIn("nuc-console-peers", slurp("SECURITY.md"))
        self.assertNotRegex(slurp("docs", "CONFIGURATION.md"), r"(?<!-u )(?<!-u nuc-console-peers )sudo nuc-console-fleet")
        changelog = slurp("CHANGELOG.md")
        self.assertIn("nuc-console-peers", changelog[:changelog.index("## [2.6.0]")])


if __name__ == "__main__":
    unittest.main()
