"""[services] must_run, the sshd audit and [tls] watch: the DER date reader (x509min), the config checks, the collector's sections and the
problems they raise. Certificates are two throw-away self-signed ones made once with `openssl req -x509` (no key kept): one expired on
2020-01-02 (UTCTime) and one valid until 2126-09-30 (GeneralizedTime). Addresses are private ones on purpose: that is all [tls] watch takes."""
import base64
import os
import random
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # golden.py
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import cards  # noqa: E402
import collector  # noqa: E402
import exposure  # noqa: E402
import golden  # noqa: E402
import nuc_config  # noqa: E402
import problems  # noqa: E402
import x509min  # noqa: E402

EXPIRED_B64 = "MIIBVTCB/aADAgECAhR9OSxWpdEQkgULaOQq5eySxNwEHDAKBggqhkjOPQQDAjAaMRgwFgYDVQQDDA9maXh0dXJlLmludmFsaWQwHhcNMjAwMTAxMDAwMDAwWhcNMjAwMTAyMDAwMDAwWjAaMRgwFgYDVQQDDA9maXh0dXJlLmludmFsaWQwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAAQCJ9KF+/TpIVJy04sEcJ4sUilasgowbbF9zFSxc3IgC34A8T9ywy+Vn57E3VmUoEB8Gi6dDK0GYSiUz6Q5eScRoyEwHzAdBgNVHQ4EFgQUYJPoP2G9i0+xMeN2LnXiSkefNpIwCgYIKoZIzj0EAwIDRwAwRAIgcFO4o82lLEt+ij7PZ46eFIIvrYkylQkJDYttM7DAgowCIC8Wm0pOxyDpGtWy7b1NUexBbOJyYf43TpUFcPFSDl+s"
VALID_B64 = "MIIBjDCCATGgAwIBAgIUE3kC6bzazj3ZttxUlxrAXpFy8fIwCgYIKoZIzj0EAwIwGjEYMBYGA1UEAwwPZml4dHVyZS5pbnZhbGlkMCAXDTI2MTAyNDE3MjcyNVoYDzIxMjYwOTMwMTcyNzI1WjAaMRgwFgYDVQQDDA9maXh0dXJlLmludmFsaWQwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAAQCJ9KF+/TpIVJy04sEcJ4sUilasgowbbF9zFSxc3IgC34A8T9ywy+Vn57E3VmUoEB8Gi6dDK0GYSiUz6Q5eScRo1MwUTAdBgNVHQ4EFgQUYJPoP2G9i0+xMeN2LnXiSkefNpIwHwYDVR0jBBgwFoAUYJPoP2G9i0+xMeN2LnXiSkefNpIwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNJADBGAiEAkEvN8OQylOHE4YyZCbDFjtRNH0acfW5aUk5Y0EC1MmICIQDJrp+BQsklYz+zTQ/Yiecynpo2bSNkZJeXNMdW1CSBBg=="
EXPIRED, VALID = base64.b64decode(EXPIRED_B64), base64.b64decode(VALID_B64)
EXPIRED_AT, VALID_AT = 1577923200, 4946462845  # 2020-01-02 00:00:00 UTC, 2126-09-30 17:27:25 UTC
NOW = 1760000000  # 2025-10-09
SSHD_T = """port 22
addressfamily any
listenaddress 0.0.0.0:22
permitrootlogin without-password
pubkeyauthentication yes
passwordauthentication yes
permitemptypasswords no
x11forwarding yes
subsystem sftp /usr/lib/openssh/sftp-server
authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2
"""


def pem(der):
    b = base64.b64encode(der).decode()
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + "\n-----END CERTIFICATE-----\n"


class Der(unittest.TestCase):
    def test_the_end_date_of_both_time_formats(self):
        self.assertEqual(x509min.not_after(EXPIRED), EXPIRED_AT)    # UTCTime
        self.assertEqual(x509min.not_after(VALID), VALID_AT)        # GeneralizedTime (after 2049)
        self.assertEqual(x509min.not_after(bytearray(VALID)), VALID_AT)

    def test_pem(self):
        self.assertEqual(x509min.pem_to_der("junk\n" + pem(VALID) + pem(EXPIRED)), VALID)   # the first block
        for bad in ("", "-----BEGIN CERTIFICATE-----\n!!!!\n-----END CERTIFICATE-----", "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
                    "-----BEGIN CERTIFICATE-----\n" + "A" * (x509min.MAX + 8) + "\n-----END CERTIFICATE-----", None, b"bytes"):
            self.assertIsNone(x509min.pem_to_der(bad))

    def test_what_is_not_a_certificate_is_none(self):
        for bad in (b"", b"\x30", b"\x30\x00", b"\x30\x03\x01\x01\x00", EXPIRED + b"\x00", EXPIRED[:-1], None, "text", 12, b"\x00" * 600):
            self.assertIsNone(x509min.not_after(bad), bad)

    def test_lengths_that_lie(self):
        c = bytearray(EXPIRED)
        for head in (b"\x30\x84\xff\xff\xff\xff", b"\x30\x88" + b"\xff" * 8, b"\x30\x80" + bytes(c[2:]) + b"\x00\x00",     # huge, too long a length field, indefinite
                     b"\x30\x81\x05" + bytes(c[3:]), b"\x30\x82\x00\x80" + bytes(c[4:]),                                 # not the shortest form
                     b"\x1f\x81\x01" + bytes(c[2:]), b"\x30\x82" + bytes([c[2], c[3] + 9]) + bytes(c[4:])):               # a long tag, a length past the end
            self.assertIsNone(x509min.not_after(bytes(head)), head[:8])
        # the inner elements too: a validity whose time claims more than the buffer holds
        i = EXPIRED.index(b"\x17\x0d")
        self.assertIsNone(x509min.not_after(EXPIRED[:i + 1] + b"\x7f" + EXPIRED[i + 2:]))
        self.assertIsNone(x509min.not_after(EXPIRED[:i + 1] + b"\x80" + EXPIRED[i + 2:]))

    def test_a_date_that_cannot_exist(self):
        for repl in (b"201301020000Z", b"200100020000Z", b"2001010a0000Z", b"20010102000000", b"2001020000Z"):   # month 13, day 0, a letter, no Z, short
            self.assertIsNone(x509min.not_after(EXPIRED.replace(b"200102000000Z", repl[:13].ljust(13, b"0")) if len(repl) == 13 else
                                                EXPIRED.replace(b"\x17\x0d200102000000Z", b"\x17%c" % len(repl) + repl)))

    def test_every_truncation_and_a_thousand_mutations_return_or_none(self):
        for cert in (EXPIRED, VALID):
            for n in range(len(cert)):
                self.assertIsNone(x509min.not_after(cert[:n]), n)
        rnd = random.Random(7)
        for _ in range(1500):
            b = bytearray(rnd.choice((EXPIRED, VALID)))
            for _ in range(rnd.randint(1, 6)):
                b[rnd.randrange(len(b))] = rnd.randrange(256)
            r = x509min.not_after(bytes(b))
            self.assertTrue(r is None or isinstance(r, int))
        for _ in range(300):
            self.assertIsNone(x509min.not_after(bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 200)))))


class WatchConfig(unittest.TestCase):
    def test_entries(self):
        e = nuc_config.watch_entry
        self.assertEqual(e("nas = 192.168.1.20:443"), ("nas", "net", "192.168.1.20", 443))
        self.assertEqual(e("a=localhost:8443"), ("a", "net", "127.0.0.1", 8443))
        self.assertEqual(e("a=[::1]:443"), ("a", "net", "::1", 443))
        self.assertEqual(e("ts=100.64.0.7:443")[2], "100.64.0.7")
        self.assertEqual(e("ll=169.254.1.1:443")[2], "169.254.1.1")
        self.assertEqual(e("f = /etc/ssl/a.pem"), ("f", "file", "/etc/ssl/a.pem", 0))
        for bad in ("203.0.113.9:443", "web=203.0.113.9:443", "web=8.8.8.8:443", "web=example.com:443", "web=server.example.ts.net:443", "web=10.0.0.1",
                    "web=10.0.0.1:0", "web=10.0.0.1:70000", "web=10.0.0.1:x", "=10.0.0.1:443", "we b=10.0.0.1:443", "web=relative/cert.pem",
                    "web=[2001:db8::1]:443", "web=::ffff:8.8.8.8:443", "web=0.0.0.0:443", "web=172.32.0.1:443", "web=100.128.0.1:443"):
            with self.assertRaises(ValueError, msg=bad):
                e(bad)

    def test_the_private_ranges_are_the_exposure_modules(self):
        mine = {str(n) for n in nuc_config.WATCH_NETS}
        self.assertEqual(mine, {str(n) for n in exposure.PRIVATE_NETS})
        for a in ("10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.0.1", "127.0.0.1", "169.254.2.2", "100.64.0.1", "100.127.255.255", "::1",
                  "fe80::1", "fd7a:115c:a1e0::1", "8.8.8.8", "172.32.0.1", "100.63.255.255", "192.0.2.1", "2001:db8::1"):
            ok = True
            try:
                nuc_config.watch_entry("x=[%s]:1" % a if ":" in a else "x=%s:1" % a)
            except ValueError:
                ok = False
            self.assertEqual(ok, exposure.is_private_addr(a), a)

    def load(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "config.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        said = []
        return nuc_config.load(path, warn=said.append, overlay=False), said

    def test_the_defaults_are_off(self):
        cfg = nuc_config.load("/nonexistent")
        self.assertEqual((cfg["services"], cfg["tls"], cfg["watch_warnings"]), ({"must_run": []}, {"watch": [], "warn_days": 21}, []))

    def test_what_the_file_says_and_what_is_said_about_the_rest(self):
        cfg, said = self.load("[services]\nmust_run = docker, ssh;fail2ban  ,-rf, a b, docker, NetworkManager.service, bad/name, getty@tty1\n"
                              "[tls]\nwatch = nas = 10.0.0.5:443, ext = 8.8.8.8:443, dns = example.com:443, nas = 10.0.0.6:443, f = /etc/x.pem, [::1]:1\n"
                              "warn_days = 400\nfoo = 1\n")
        self.assertEqual(cfg["services"]["must_run"], ["docker", "ssh", "fail2ban", "a", "b", "NetworkManager.service", "getty@tty1"])
        self.assertEqual(cfg["tls"], {"watch": ["nas=10.0.0.5:443", "f=/etc/x.pem"], "warn_days": 365})
        text = "\n".join(said)
        for needle in ("'-rf' is not a unit name", "'bad/name'", "8.8.8.8", "only IP addresses", "'nas' is there twice", "write name =", "unknown key 'foo'"):
            self.assertIn(needle, text)
        self.assertEqual(len(cfg["watch_warnings"]), len(said))
        cfg, said = self.load("[services]\nmust_run = " + ",".join("u%d" % i for i in range(30)) + "\n")
        self.assertEqual((len(cfg["services"]["must_run"]), len(said)), (20, 10))
        cfg, said = self.load("[tls]\nwarn_days = soon\n")
        self.assertEqual((cfg["tls"]["warn_days"], len(said)), (21, 1))

    def test_the_overlay_cannot_set_them(self):
        self.assertFalse(nuc_config.overlay_allowed("services", "must_run"))
        self.assertFalse(nuc_config.overlay_allowed("tls", "watch"))


class Parsers(unittest.TestCase):
    def test_sshd_T_keeps_five_values(self):
        self.assertEqual(collector.parse_sshd_t(SSHD_T), {"permitrootlogin": "without-password", "pubkeyauthentication": "yes",
                                                         "passwordauthentication": "yes", "permitemptypasswords": "no", "x11forwarding": "yes"})
        self.assertEqual(collector.parse_sshd_t("PermitRootLogin YES\npasswordauthentication $(id)\npermitemptypasswords " + "y" * 40), {"permitrootlogin": "yes"})
        self.assertEqual(collector.parse_sshd_t("Missing privilege separation directory: /run/sshd\n"), {})

    def test_service_states(self):
        st = collector.service_state
        self.assertEqual(st("active\n"), "active")
        self.assertEqual(st("inactive\n", "LoadState=loaded\n"), "inactive")
        self.assertEqual(st("inactive\n", "LoadState=not-found\n"), "not-found")
        self.assertEqual(st("failed\n"), "failed")
        self.assertEqual(st(""), "unknown")
        self.assertEqual(st("ac tive; rm -rf"), "unknown")


class Sections(unittest.TestCase):
    """collect_boot's three new sections, driven by a fake run and fake network/file readers (nothing real is asked)."""

    def setUp(self):
        self.calls = []

    def boot(self, must_run=(), watch=(), run=None, der=None, files=None, unsupported=()):
        cfg = dict(collector.CFG, services={"must_run": list(must_run)}, tls={"watch": list(watch), "warn_days": 21})

        def fake_run(name, *args, timeout=15):
            self.calls.append((name,) + args)
            if run:
                return run(name, *args)
            raise collector.Absent(name)
        patches = [mock.patch.object(collector, "CFG", cfg), mock.patch.object(collector, "run", fake_run), mock.patch.object(collector, "LINUX", True),
                   mock.patch.object(collector, "OFF", set()), mock.patch.object(collector, "UNSUPPORTED_BOOT", unsupported),
                   mock.patch.object(collector, "boot_time", lambda: 1700000000),
                   mock.patch.object(collector, "tls_der", der or (lambda h, p, timeout=3: (_ for _ in ()).throw(OSError("no")))),
                   mock.patch.object(collector, "cert_file_der", files or (lambda p: (_ for _ in ()).throw(OSError("no"))))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return collector.collect_boot()

    def test_nothing_configured_runs_nothing_and_leaves_no_trace(self):
        b = self.boot()
        for k in ("services", "certs"):
            self.assertNotIn(k, b)
            self.assertNotIn(k, b["absent"])
        self.assertEqual([c for c in self.calls if c[0] == "systemctl" and c[1] == "is-active"], [])

    def test_services(self):
        def run(name, *a):
            if name == "systemctl" and a[0] == "is-active":
                return {"docker": (0, "active\n", ""), "fail2ban": (3, "inactive\n", ""), "gone": (3, "inactive\n", ""), "bad": (3, "failed\n", "")}[a[-1]]
            if name == "systemctl" and a[0] == "show":
                return 0, "LoadState=%s\n" % ("not-found" if a[-1] == "gone" else "loaded"), ""
            raise collector.Absent(name)
        b = self.boot(must_run=["docker", "fail2ban", "gone", "bad", "-rf", "a b"], run=run)
        self.assertEqual(b["services"], [{"unit": "docker", "state": "active"}, {"unit": "fail2ban", "state": "inactive"},
                                         {"unit": "gone", "state": "not-found"}, {"unit": "bad", "state": "failed"}])   # the two bad names never reach systemctl
        asked = [c for c in self.calls if c[0] == "systemctl" and c[1] in ("is-active", "show")]
        self.assertTrue(asked and all(c[-2] == "--" for c in asked))                                                    # a unit is never read as an option
        self.assertNotIn(("systemctl", "show", "--no-pager", "-p", "LoadState", "--", "docker"), self.calls)           # an active unit is not asked twice
        self.assertEqual(b["errors"], {})

    def test_systemctl_missing_or_timing_out_is_absent_or_unknown(self):
        self.assertIn("services", self.boot(must_run=["docker"])["absent"])
        b = self.boot(must_run=["docker"], run=lambda name, *a: (None, "", "TimeoutExpired"))
        self.assertEqual(b["services"], [{"unit": "docker", "state": "unknown"}])

    def test_sshd(self):
        b = self.boot(run=lambda name, *a: (0, SSHD_T, "") if (name, a) == ("sshd", ("-T",)) else (_ for _ in ()).throw(collector.Absent(name)))
        self.assertEqual(b["sshd"]["passwordauthentication"], "yes")
        self.assertEqual(sorted(b["sshd"]), sorted(collector.SSHD_KEYS))
        self.assertIn(("sshd", "-T"), self.calls)
        for rc, out in ((255, "Missing privilege separation directory: /run/sshd\n"), (0, ""), (None, "")):
            b = self.boot(run=lambda name, *a: (rc, out, "") if name == "sshd" else (_ for _ in ()).throw(collector.Absent(name)))
            self.assertNotIn("sshd", b)
            self.assertIn("sshd", b["absent"])       # an error or nothing is Absent, never a pass
            self.assertEqual(b["errors"], {})
        self.assertIn("sshd", self.boot()["absent"])  # no sshd at all

    def test_certs_keep_a_name_and_a_date_and_refuse_what_is_not_private(self):
        asked = []

        def der(h, p, timeout=3):
            asked.append((h, p))
            if p == 9:
                raise ssl.SSLError("handshake")
            return VALID if p == 443 else b"not a certificate"
        files = lambda path: {"/etc/a.pem": EXPIRED}.get(path) or (_ for _ in ()).throw(FileNotFoundError(path))   # noqa: E731
        b = self.boot(watch=["nas=10.0.0.5:443", "down=10.0.0.5:9", "junk=10.0.0.5:444", "file=/etc/a.pem", "gone=/etc/none.pem", "ext=8.8.8.8:443",
                             "dns=example.com:443"], der=der, files=files)
        self.assertEqual(b["certs"], [{"name": "nas", "exp": VALID_AT, "why": ""}, {"name": "down", "exp": None, "why": "unreachable"},
                                      {"name": "junk", "exp": None, "why": "unreadable"}, {"name": "file", "exp": EXPIRED_AT, "why": ""},
                                      {"name": "gone", "exp": None, "why": "unreadable"}, {"name": "ext", "exp": None, "why": "refused"},
                                      {"name": "dns", "exp": None, "why": "refused"}])
        self.assertEqual(asked, [("10.0.0.5", 443), ("10.0.0.5", 9), ("10.0.0.5", 444)])      # the collector connected to nothing else
        self.assertNotIn("CN", repr(b))

    def test_other_systems_have_no_services_and_no_sshd(self):
        b = self.boot(must_run=["docker"], watch=["a=10.0.0.5:443"], der=lambda h, p, timeout=3: VALID, unsupported=("services", "sshd"))
        self.assertEqual((b["certs"][0]["exp"], b.get("services"), b.get("sshd")), (VALID_AT, None, None))   # the certificates are read everywhere
        self.assertEqual(sorted(set(b["unsupported"]) & {"services", "sshd"}), ["services", "sshd"])


@unittest.skipUnless(shutil.which("openssl"), "no openssl to make a throw-away certificate")
class RealSocket(unittest.TestCase):
    def test_tls_der_reads_a_self_signed_server_without_trusting_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        key, crt = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
        r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key, "-out", crt,
                            "-days", "30", "-subj", "/CN=lan.invalid"], capture_output=True)
        if r.returncode:
            self.skipTest("openssl could not make a certificate")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(5)

        def serve():
            try:
                c, _ = srv.accept()
                with ctx.wrap_socket(c, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                pass
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        self.addCleanup(srv.close)
        got = collector.tls_der("127.0.0.1", srv.getsockname()[1])
        t.join(5)
        exp = x509min.not_after(got)
        self.assertTrue(abs(exp - (time.time() + 30 * 86400)) < 3600, exp)
        with open(crt) as f:
            self.assertEqual(collector.cert_file_der(crt), x509min.pem_to_der(f.read()))


class Files(unittest.TestCase):
    def test_a_file_is_a_regular_certificate_of_at_most_64_kb(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "c.pem")
        with open(p, "w") as f:
            f.write(pem(VALID))
        self.assertEqual(x509min.not_after(collector.cert_file_der(p)), VALID_AT)
        with open(p, "w") as f:
            f.write("#" * (x509min.MAX + 10) + pem(VALID))
        self.assertIsNone(collector.cert_file_der(p))
        with self.assertRaises(OSError):
            collector.cert_file_der(os.path.join(d, "none.pem"))
        if hasattr(os, "mkfifo"):
            fifo = os.path.join(d, "fifo")
            os.mkfifo(fifo)
            self.assertIsNone(collector.cert_file_der(fifo))   # a FIFO would block a read: it is not a certificate


def boot(**kw):
    return dict({"ts": NOW, "errors": {}, "kernel": "7.0", "btime": NOW - 86400, "analyze": None, "blame": [], "failed": [], "enabled": [],
                 "journal": {"err": 0, "warn": 0, "capped": False, "top": []}, "containers": []}, **kw)


def raw(b, net=None, cont=None):
    return {pid: (sev, text) for sev, text, pid in problems.problems_raw(net, cont, NOW, boot=b, baseline=False) if not pid.startswith("collector-")}


class Problems(unittest.TestCase):
    def test_nothing_configured_nothing_said(self):
        with golden.FrozenWorld():
            self.assertEqual(raw(boot()), {})
            self.assertEqual(raw(boot(sshd={"permitrootlogin": "no", "passwordauthentication": "no", "permitemptypasswords": "no"})), {})

    def test_service_down(self):
        with golden.FrozenWorld():
            r = raw(boot(services=[{"unit": "docker", "state": "active"}, {"unit": "fail2ban", "state": "inactive"}, {"unit": "x", "state": "not-found"},
                                   {"unit": "y", "state": "activating"}, {"unit": "z", "state": "unknown"}, {"unit": "w", "state": "failed"}, "junk", {"unit": 3}]))
            self.assertEqual(r["service-down"], (2, "service not running: fail2ban (inactive), x (not-found), z (unknown) … +1"))
            self.assertEqual(raw(boot(services=[{"unit": "docker", "state": "active"}, {"unit": "a", "state": "reloading"}])), {})
            self.assertEqual(raw(boot(services="nonsense")), {})
            self.assertEqual(raw(boot(failed=["bad.service"], services=[{"unit": "f", "state": "inactive"}])).keys(), {"failed-units", "service-down"})

    def test_ssh(self):
        with golden.FrozenWorld():
            r = raw(boot(sshd={"permitrootlogin": "yes", "passwordauthentication": "yes", "permitemptypasswords": "yes"}))
            self.assertEqual({k: v[0] for k, v in r.items()}, {"ssh-root-login": 1, "ssh-password-auth": 1, "ssh-empty-passwords": 2})
            self.assertEqual(raw(boot(sshd={"permitrootlogin": "prohibit-password", "passwordauthentication": "no"})), {})
            self.assertEqual(raw(boot(sshd="x")), {})

    def listeners(self, **kw):
        net = {"ts": NOW, "errors": {}, "ufw": None, "docker_user": [], "serve": [], "f2b": {"jails": []}, "drops": {"n": 0, "src": [], "dpt": []},
               "listeners": [{"proto": "tcp", "addr": "0.0.0.0", "port": 22, "proc": "sshd"}]}
        net.update(kw)
        return net

    def test_password_auth_is_an_error_when_port_22_reaches_further(self):
        b = boot(sshd={"passwordauthentication": "yes"})
        with golden.FrozenWorld():
            self.assertEqual(raw(b, self.listeners())["ssh-password-auth"][0], 1)                 # the LAN: a warning
            self.assertEqual(raw(b, None)["ssh-password-auth"][0], 1)                            # no network data: the lower one, and no crash
            funnel = self.listeners(serve=[{"port": 22, "path": "/", "target": "tcp://127.0.0.1:22", "funnel": True}])
            sev, text = raw(b, funnel)["ssh-password-auth"]
            self.assertEqual(sev, 2)
            self.assertIn("Internet", text)
        with golden.FrozenWorld(cfg={"expose": {"sshd": "LOCALE"}}):
            self.assertEqual(raw(b, self.listeners())["ssh-password-auth"][0], 2)                 # beyond what [expose] says
        with golden.FrozenWorld(cfg={"expose": {"sshd": "LAN"}}):
            self.assertEqual(raw(b, self.listeners())["ssh-password-auth"][0], 1)
        with golden.FrozenWorld():
            self.assertEqual(raw(b, self.listeners(listeners=[{"proto": "tcp", "addr": "127.0.0.1", "port": 22, "proc": "sshd"}]))["ssh-password-auth"][0], 1)

    def test_certificates(self):
        day = 86400
        b = boot(certs=[{"name": "ok", "exp": NOW + 90 * day, "why": ""}, {"name": "soon", "exp": NOW + 20 * day + 5, "why": ""},
                        {"name": "edge", "exp": NOW + 21 * day + 5, "why": ""}, {"name": "dead", "exp": NOW - 3 * day - 10, "why": ""},
                        {"name": "lost", "exp": None, "why": "unreachable"}, {"name": "tel", "exp": NOW + 1, "why": ""}, {"exp": 1}, "junk"])
        with golden.FrozenWorld():
            r = raw(b)
            self.assertEqual(r["cert-expiring"], (1, "TLS certificate expires soon: soon (20 d), tel (0 d)"))
            self.assertEqual(r["cert-expired"], (2, "TLS certificate expired: dead (3 d ago)"))
            self.assertEqual(r["cert-unreadable"], (1, "TLS certificate not read: lost"))
        with golden.FrozenWorld(cfg={"tls": {"warn_days": 30}}):
            self.assertIn("edge (21 d)", raw(b)["cert-expiring"][1])
        with golden.FrozenWorld(cfg={"tls": {"warn_days": 1}}):
            self.assertEqual(raw(b)["cert-expiring"][1], "TLS certificate expires soon: tel (0 d)")

    def test_a_config_warning_is_an_attention_item(self):
        with golden.FrozenWorld(cfg={"watch_warnings": ["[tls] watch: '8.8.8.8' is not a private, loopback or Tailscale address", "b", "c"]}):
            sev, text = raw(boot())["watch-config"]
            self.assertEqual(sev, 1)
            self.assertIn("8.8.8.8", text)
            self.assertTrue(text.endswith("… +1"))

    def test_the_decisions(self):
        new = {"service-down", "ssh-root-login", "ssh-password-auth", "ssh-empty-passwords", "cert-expiring", "cert-expired", "cert-unreadable", "watch-config"}
        self.assertEqual(new & set(nuc_config.MUTABLE_ALERTS), set())                  # none is an alarm a person may silence
        self.assertEqual(new & problems.NOT_ACCEPTABLE, set())                         # all can be accepted, with a reason
        self.assertEqual(new - set(problems.CATALOG), set())
        self.assertEqual(new - set(cards.PROBLEM_CARDS), set())
        for pid in new:
            title, why, fix = problems.CATALOG[pid]
            self.assertTrue(title and why and fix, pid)
        self.assertIn("failed-units", problems.CATALOG["service-down"][1])             # the rationale: failed-units only sees FAILED units
        self.assertIn("service-down", problems.COUNT_MATTERS)

    def test_accepting_ties_to_the_severity(self):
        with golden.FrozenWorld():
            b = boot(sshd={"passwordauthentication": "yes"})
            (sev, text), = [(s, t) for s, t, p in problems.problems_raw(None, None, NOW, boot=b, baseline=False) if p == "ssh-password-auth"]
            self.assertEqual(sev, 1)
            fp = problems.fingerprint(sev, text, "ssh-password-auth")
            self.assertNotEqual(fp, problems.fingerprint(2, text + ", and port 22 reaches the Internet or beyond config.ini", "ssh-password-auth"))


if __name__ == "__main__":
    unittest.main()
