"""[posture] (opt-in security posture alarms): the closed set of checks, the registry reads (a stand-in winreg), the Apple tools' wording, the
section the root collector writes and the `posture` problem with its fingerprint. Nothing here touches the host's registry or tools."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # golden.py
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import cards  # noqa: E402
import collect_darwin  # noqa: E402
import collect_windows  # noqa: E402
import collector  # noqa: E402
import golden  # noqa: E402
import nuc_config  # noqa: E402
import problems  # noqa: E402

NOW = 1760000000


class FakeWinreg:
    """The part of winreg the collector uses: values = {(key, name): int | OSError | (type, value)}; a missing pair is FileNotFoundError."""
    HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_64KEY, REG_DWORD = "HKLM", 1, 2, 4

    def __init__(self, values):
        self.values, self.opened = values, []
        self.keys = {k for k, _ in values}

    def OpenKey(self, root, key, reserved, access):
        self.opened.append((root, key, access))
        if key not in self.keys:
            raise FileNotFoundError(key)
        if isinstance(self.values.get((key, "")), Exception):
            raise self.values[(key, "")]
        return mock.MagicMock(__enter__=lambda s: key, __exit__=lambda *a: False)

    def QueryValueEx(self, key, name):
        v = self.values.get((key, name), FileNotFoundError())
        if isinstance(v, Exception):
            raise v
        return v if isinstance(v, tuple) else (v, self.REG_DWORD)


def reg(keys=(), **kw):
    """keys: extra HKLM keys that exist with no value (or, with ("path", ""): an Exception makes the key itself unreadable)."""
    ks = collect_windows.POSTURE_KEYS
    w = FakeWinreg({ks[k]: v for k, v in kw.items()})
    w.keys |= {ks[k][0] for k in keys}
    return w


class Windows(unittest.TestCase):
    def test_each_check(self):
        want = ("secure-boot", "rdp-nla", "smb1")
        self.assertEqual(collect_windows.posture(reg(**{"secure-boot": 1, "rdp-deny": 1, "smb1": 0}), want), {"secure-boot": "ok", "rdp-nla": "ok", "smb1": "ok"})
        self.assertEqual(collect_windows.posture(reg(**{"secure-boot": 0, "rdp-deny": 0, "rdp-nla": 0, "smb1": 1}), want),
                         {"secure-boot": "fail", "rdp-nla": "fail", "smb1": "fail"})
        self.assertEqual(collect_windows.posture(reg(**{"rdp-deny": 0, "rdp-nla": 1}), want), {"secure-boot": "unknown", "rdp-nla": "ok", "smb1": "ok"})

    def test_unreadable_is_unknown_never_ok(self):
        w = reg(**{"secure-boot": PermissionError(), "rdp-deny": 0, "rdp-nla": ("x", 1), "smb1": 7})
        self.assertEqual(collect_windows.posture(w, ("secure-boot", "rdp-nla", "smb1")), {"secure-boot": "unknown", "rdp-nla": "unknown", "smb1": "unknown"})
        self.assertEqual(collect_windows.posture(reg(**{"rdp-nla": 0}), ("rdp-nla",)), {"rdp-nla": "unknown"})  # RDP setting missing: nothing to judge

    def test_rdp_policy_and_local_every_combination(self):
        P = collect_windows.POSTURE_KEYS
        R = lambda **kw: collect_windows.posture(reg(**kw), ("rdp-nla",))["rdp-nla"]   # noqa: E731
        # policy absent: the local keys decide
        self.assertEqual([R(), R(**{"rdp-deny": 1}), R(**{"rdp-deny": 0}), R(**{"rdp-deny": 0, "rdp-nla": 0}), R(**{"rdp-deny": 0, "rdp-nla": 1})],
                         ["unknown", "ok", "unknown", "fail", "ok"])
        # policy present wins, per value, over a local value that says the opposite
        self.assertEqual(R(**{"rdp-deny-policy": 1, "rdp-deny": 0, "rdp-nla": 0}), "ok")              # policy denies RDP
        self.assertEqual(R(**{"rdp-deny-policy": 0, "rdp-deny": 1, "rdp-nla": 0}), "fail")            # policy enables it, local NLA 0
        self.assertEqual(R(**{"rdp-deny-policy": 0, "rdp-nla-policy": 1, "rdp-deny": 1, "rdp-nla": 0}), "ok")
        self.assertEqual(R(**{"rdp-deny-policy": 0, "rdp-nla-policy": 0, "rdp-nla": 1}), "fail")      # policy NLA 0 beats local 1
        self.assertEqual(R(**{"rdp-deny-policy": 0, "rdp-nla-policy": 0}), "fail")                    # policy only, no local keys
        self.assertEqual(R(**{"rdp-deny-policy": 0}), "unknown")                                       # enabled, NLA unknown
        self.assertEqual(R(**{"rdp-nla-policy": 0}), "unknown")                                        # only NLA from the policy, deny unknown
        self.assertEqual(R(**{"rdp-nla-policy": 0, "rdp-deny": 0}), "fail")                           # mixed sources, both readable
        # policy key unreadable (access denied on the key or on a value): unknown, whatever the local keys say
        w = reg(**{"rdp-deny": 1})
        w.keys.add(P["rdp-deny-policy"][0])
        w.values[(P["rdp-deny-policy"][0], "")] = PermissionError()
        self.assertEqual(collect_windows.posture(w, ("rdp-nla",)), {"rdp-nla": "unknown"})
        self.assertEqual(R(**{"rdp-deny-policy": PermissionError(), "rdp-deny": 1}), "unknown")
        self.assertEqual(R(**{"rdp-deny-policy": 0, "rdp-nla-policy": PermissionError(), "rdp-nla": 1}), "unknown")

    def test_smb1_value_then_driver(self):
        S = lambda keys=(), **kw: collect_windows.posture(reg(keys=keys, **kw), ("smb1",))["smb1"]   # noqa: E731
        self.assertEqual([S(smb1=0), S(smb1=1), S(smb1=5)], ["ok", "fail", "unknown"])
        self.assertEqual(S(**{"smb1": 0, "smb1-driver": 3}), "ok")                  # an explicit 0 wins over the driver
        self.assertEqual(S(), "ok")                                                  # no value, no mrxsmb10 key: no SMB1 server
        self.assertEqual([S(**{"smb1-driver": n}) for n in (0, 1, 2, 3, 4)], ["fail", "fail", "fail", "fail", "ok"])
        self.assertEqual(S(**{"smb1-driver": 9}), "unknown")
        self.assertEqual(S(keys=("smb1-driver",)), "unknown")                       # key there, Start missing: ambiguous
        self.assertEqual(S(**{"smb1-driver": ("x", 1)}), "unknown")                 # not a DWORD
        self.assertEqual(S(**{"smb1-driver": PermissionError()}), "unknown")
        self.assertEqual(S(smb1=PermissionError()), "unknown")

    def test_64_bit_view_and_only_the_asked_keys(self):
        w = reg(**{"secure-boot": 1})
        collect_windows.posture(w, ("secure-boot",))
        self.assertEqual([(r, a) for r, _k, a in w.opened], [("HKLM", 3)])  # KEY_READ | KEY_WOW64_64KEY
        self.assertEqual(collect_windows.posture(w, ()), {})


class Mac(unittest.TestCase):
    def test_wordings(self):
        P = collect_darwin.parse_posture
        self.assertEqual([P("filevault", t) for t in ("FileVault is On.\n", "FileVault is Off.\n", "FileVault is On.\nEncryption in progress: Percent completed = 12", "filevault IS off")],
                         ["ok", "fail", "ok", "fail"])
        self.assertEqual([P("sip", t) for t in ("System Integrity Protection status: enabled.\n", "System Integrity Protection status: disabled.",
                                                "System Integrity Protection status: enabled (Custom Configuration).", "System Integrity Protection status: unknown (Custom Configuration).")],
                         ["ok", "fail", "unknown", "unknown"])
        self.assertEqual([P("gatekeeper", t) for t in ("assessments enabled\n", "assessments disabled", "assessment enabled")], ["ok", "fail", "ok"])

    def test_unrecognised_is_unknown(self):
        for check in collect_darwin.POSTURE_TOOLS:
            for text in ("", "Nenhum", "usage: tool", "Error: not allowed", None):
                self.assertEqual(collect_darwin.parse_posture(check, text), "unknown", (check, text))

    def test_running_the_tools(self):
        calls = []

        def run(name, *args, timeout=15):
            calls.append((name,) + args)
            if name == "spctl":
                raise collector.Absent(name)
            if name == "csrutil":
                return None, "", ""                       # timeout
            return 0, "FileVault is Off.\n", ""
        got = collect_darwin.posture(run, ("filevault", "sip", "gatekeeper"))
        self.assertEqual(got, {"filevault": "fail", "sip": "unknown", "gatekeeper": "unknown"})
        self.assertEqual(calls, [("fdesetup", "status"), ("csrutil", "status"), ("spctl", "--status")])  # fixed argv, nothing from the config


class Config(unittest.TestCase):
    def load(self, text):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        path = os.path.join(d, "config.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": path}), mock.patch("sys.stderr"):
            return nuc_config.load(path)["posture"]

    def test_default_is_off(self):
        self.assertEqual(self.load(""), {"enabled": False, "checks": []})
        self.assertEqual(self.load("[posture]\nchecks = sip\n"), {"enabled": False, "checks": ["sip"]})

    def test_enabled_and_closed_set(self):
        got = self.load("[posture]\nenabled = yes\nchecks = SIP, smb1, bitlocker, nonsense, sip; filevault\n")
        self.assertEqual(got, {"enabled": True, "checks": ["sip", "smb1", "filevault"]})
        self.assertEqual(self.load("[posture]\nenabled = maybe\n")["enabled"], False)

    def test_a_bad_name_is_sanitised_in_the_message(self):
        import tempfile, shutil
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "config.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[posture]\nchecks = bad\x1b[2Jname\n")
        said = []
        with mock.patch("sys.stderr") as err:
            err.write.side_effect = said.append
            nuc_config.load(path)
        text = "".join(said)
        self.assertIn("[posture] checks", text)
        self.assertNotIn("\x1b", text)

    def test_which_checks_run_where(self):
        W = nuc_config.posture_wanted
        self.assertEqual(W([], "windows"), ("secure-boot", "rdp-nla", "smb1"))
        self.assertEqual(W(["sip", "smb1"], "windows"), ("smb1",))
        self.assertEqual(W([], "darwin"), ("filevault", "sip", "gatekeeper"))
        self.assertEqual(W(["smb1"], "darwin"), ())
        self.assertEqual(W([], "linux"), ())

    def test_overlay_cannot_set_it_and_the_page_knows_it(self):
        self.assertFalse(nuc_config.overlay_allowed("posture", "enabled"))
        self.assertFalse(nuc_config.overlay_allowed("posture", "checks"))
        import confedit
        self.assertEqual({(k.section, k.name, k.applies) for k in confedit.BY_SECTION["posture"]}, {("posture", "enabled", "start"), ("posture", "checks", "start")})


class Section(unittest.TestCase):
    """collect_boot writes the section only when enabled, and says 'unsupported' on Linux."""

    def boot(self, os_name, enabled, **extra):
        cfg = {"posture": {"enabled": enabled, "checks": []}}
        patches = [mock.patch.object(collector, "CFG", dict(collector.CFG, **cfg)), mock.patch.object(collector, "OS_NAME", os_name),
                   mock.patch.object(collector, "UNSUPPORTED_BOOT", collector.UNSUPPORTED_BOOT_BY_OS.get(os_name, ())),
                   mock.patch.object(collector, "WINDOWS", os_name == "windows"), mock.patch.object(collector, "LINUX", os_name == "linux"),
                   mock.patch.object(collector, "boot_time", lambda: 1), mock.patch.object(collector, "run", mock.Mock(side_effect=collector.Absent("x")))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_windows_and_macos_write_the_section(self):
        self.boot("windows", True)
        w = reg(**{"secure-boot": 0, "rdp-deny": 1, "smb1": 0})
        with mock.patch.dict(sys.modules, {"winreg": w}), mock.patch.object(collector, "cwin", collect_windows, create=True):
            d = collector.collect_boot()
        self.assertEqual(d["posture"], {"secure-boot": "fail", "rdp-nla": "ok", "smb1": "ok"})
        self.assertNotIn("posture", d["errors"])
        self.boot("darwin", True)
        collector.WINDOWS = False
        with mock.patch.object(collector, "cmac", mock.Mock(posture=lambda run, wanted: {c: "ok" for c in wanted}, daemons=lambda run: ([], [])), create=True):
            d = collector.collect_boot()
        self.assertEqual(d["posture"], {"filevault": "ok", "sip": "ok", "gatekeeper": "ok"})

    def test_off_collects_nothing(self):
        self.boot("linux", False)
        d = collector.collect_boot()
        self.assertNotIn("posture", d)
        self.assertNotIn("posture", d.get("unsupported", []))

    def test_linux_on_is_unsupported_not_an_error(self):
        self.boot("linux", True)
        d = collector.collect_boot()
        self.assertIn("posture", d["unsupported"])
        self.assertNotIn("posture", d["errors"])
        self.assertEqual(collector.UNSUPPORTED_BOOT_BY_OS["linux"], ("posture",))
        self.assertNotIn("posture", collector.UNSUPPORTED_BOOT_BY_OS["windows"] + collector.UNSUPPORTED_BOOT_BY_OS["darwin"])


def boot(**kw):
    return dict({"ts": NOW, "errors": {}, "kernel": "7.0", "btime": NOW - 86400, "analyze": None, "blame": [], "failed": [], "enabled": [],
                 "journal": {"err": 0, "warn": 0, "capped": False, "top": []}, "containers": []}, **kw)


def raw(b):
    return {pid: (sev, text) for sev, text, pid in problems.problems_raw(None, None, NOW, boot=b, baseline=False) if not pid.startswith("collector-")}


ON = {"posture": {"enabled": True, "checks": []}}


class Problem(unittest.TestCase):
    def test_off_says_nothing_even_with_data(self):
        with golden.FrozenWorld():
            self.assertEqual(raw(boot(posture={"sip": "fail"})), {})

    def test_text_and_unknown(self):
        with golden.FrozenWorld(cfg=ON):
            M = lambda **p: boot(os="darwin", posture=p)   # noqa: E731
            self.assertEqual(raw(M(filevault="ok", sip="ok")), {})
            self.assertEqual(raw(M(sip="fail", filevault="fail"))["posture"], (1, "security posture: FileVault off, SIP off"))
            self.assertEqual(raw(M(sip="fail", filevault="unknown"))["posture"], (1, "security posture: SIP off"))   # one unknown among others: quiet
            self.assertEqual(raw(M(sip="unknown", filevault="unknown"))["posture"], (1, "posture checks could not be read"))
            self.assertEqual(raw(M(bogus="fail", sip="weird")), {})
            self.assertEqual(raw(M()), {})
            self.assertEqual(raw(boot(os="darwin", posture="x")), {})
            self.assertEqual(raw(boot(os="windows", posture={"smb1": "fail", "secure-boot": "fail", "rdp-nla": "fail"}))["posture"][1],
                             "security posture: RDP without NLA, Secure Boot off, SMB1 enabled")

    def test_only_the_checks_of_the_hosts_own_os(self):
        with golden.FrozenWorld(cfg=ON):
            self.assertEqual(raw(boot(os="windows", posture={"filevault": "fail", "sip": "fail"})), {})      # a foreign shape says nothing
            self.assertEqual(raw(boot(os="darwin", posture={"smb1": "fail"})), {})
            self.assertEqual(raw(boot(os="linux", posture={"sip": "fail", "smb1": "fail"})), {})
            self.assertEqual(raw(boot(os="windows", posture={"smb1": "fail", "sip": "fail"}))["posture"], (1, "security posture: SMB1 enabled"))

    def test_the_decisions(self):
        self.assertNotIn("posture", nuc_config.MUTABLE_ALERTS)
        self.assertNotIn("posture", problems.NOT_ACCEPTABLE)
        self.assertIn("posture", cards.PROBLEM_CARDS)
        for cat in (problems.BASE_CATALOG, problems.OS_CATALOG["windows"], problems.OS_CATALOG["darwin"]):
            title, why, fix = cat["posture"]
            self.assertTrue(title and why and fix)
            self.assertIn("accept it with a reason", why)
        self.assertIn("Secure Boot", problems.OS_CATALOG["windows"]["posture"][2])
        self.assertIn("fdesetup", problems.OS_CATALOG["darwin"]["posture"][2])

    def test_accepting_one_name_does_not_hide_another(self):
        def left(post, acc):
            b = boot(posture=post)
            with mock.patch.object(problems, "load_accepted", lambda: acc):
                pb = problems.problems(None, None, NOW, boot=b, baseline=False)
                return [t for (s, t), p in zip(pb, pb.pids) if p == "posture"]
        with golden.FrozenWorld(cfg=ON):
            (rec,) = [r for r in problems.problem_records(None, None, NOW, boot=boot(posture={"secure-boot": "fail"}), baseline=False) if r["id"] == "posture"]
            acc = {"posture": {"fp": rec["fingerprint"], "reason": "kiosk"}}
            self.assertEqual(left({"secure-boot": "fail"}, acc), [])
            self.assertEqual(left({"secure-boot": "ok"}, acc), [])
            self.assertEqual(len(left({"secure-boot": "fail", "smb1": "fail"}, acc)), 1)     # a second failing name comes back
            self.assertEqual(len(left({"smb1": "fail"}, acc)), 1)
            self.assertTrue(rec["acceptable"])
            self.assertNotEqual(rec["fingerprint"], problems.fingerprint(1, "security posture: Secure Boot off, SMB1 enabled", "posture"))   # names are the tie


if __name__ == "__main__":
    unittest.main()
