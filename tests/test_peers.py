"""The fleet client (src/peers.py) and its [hosts] configuration: every rule of SECURITY.md's "The peer client" has its test here.

The peers are tests/fakepeer.py on 127.0.0.1 (port 0): programmable, misbehaving on request. The client's internal `allow_loopback`,
`resolver`, `cafile` and `limits` are the tests' only; the limits are small so that no test sleeps more than half a second. Nothing leaves
the machine, no host state is read (tests/hermetic.py)."""
import ast
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import stat
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import confedit  # noqa: E402
import confmigrate  # noqa: E402
import fakepeer  # noqa: E402
import nuc_config  # noqa: E402
import peers  # noqa: E402

POSIX = os.name == "posix"
TOKEN = "Tk9" * 8  # the secret: it must never show up anywhere
FAST = peers.Limits(op=0.3, total=0.4)
TLS = peers.Limits(op=2, total=3)
PEM = fakepeer.PEM
with open(PEM) as _f:
    LEAF = _f.read().split("-----BEGIN PRIVATE")[0]
PIN = "sha256:" + hashlib.sha256(ssl.PEM_cert_to_DER_cert(LEAF)).hexdigest()
ip = peers.ipaddress.ip_address
ABS_TOKEN = os.path.join(os.path.abspath(os.sep), "etc", "nuc-console", "peers", "b.token")  # absolute on every OS (a bare "/x" is not on Windows from Python 3.13)


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def spit(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nuc-peers-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        if POSIX:
            os.chmod(self.dir, 0o700)
        self.token = self.write_token(TOKEN)
        self.cache = os.path.join(self.dir, "cache")
        self.state = os.path.join(self.dir, "state")

    def write_token(self, text, name="b.token"):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(text + "\n")
        if POSIX:
            os.chmod(path, 0o600)
        return path

    def fake(self, **kw):
        kw.setdefault("token", TOKEN)
        p = fakepeer.FakePeer(**kw).start()
        self.addCleanup(p.stop)
        return p

    def peer(self, url, **kw):
        d = {"name": "b", "url": url, "token": self.token, "pin": "", "scope": "summary", "alert": False, "insecure": False}
        d.update(kw)
        return d

    def poll(self, fake, lim=FAST, **kw):
        peer_kw = {k: kw.pop(k) for k in ("pin", "scope", "insecure", "url") if k in kw}
        net = dict(allow_loopback=True, limits=lim)
        net.update(kw)
        return peers.poll_peer(self.peer(peer_kw.pop("url", fake.url), **peer_kw), **net)

    def no_connection(self):
        """A connection attempt fails the test: for the cases that must be refused before any socket is opened."""
        m = mock.patch.object(peers, "_connect", side_effect=AssertionError("connected"))
        m.start()
        self.addCleanup(m.stop)

    def assertClean(self, entry, *secrets):
        text = json.dumps(entry)
        for s in (TOKEN,) + secrets:
            self.assertNotIn(s, text)


# ---- [hosts] -------------------------------------------------------------------------------------------------------------------------
class Config(unittest.TestCase):
    def load(self, text, **kw):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "config.ini")
        with open(path, "w") as f:
            f.write(text)
        said = []
        return nuc_config.load(path, warn=said.append, overlay=False, **kw), said

    def test_the_grammar(self):
        cfg, said = self.load("[hosts]\nnuc-b = https://nuc-b.example.net token=%s pin=sha256:%s scope=views alert=yes\n"
                              "nas = http://100.64.1.7:8787 insecure=no   # a comment\n" % (ABS_TOKEN, "AB" * 32))
        self.assertEqual(said, [])
        b, nas = cfg["hosts"]
        self.assertEqual((b["name"], b["url"], b["token"], b["pin"], b["scope"], b["alert"], b["insecure"]),
                         ("nuc-b", "https://nuc-b.example.net", ABS_TOKEN, "sha256:" + "ab" * 32, "views", True, False))
        self.assertEqual((nas["url"], nas["scope"], nas["alert"]), ("http://100.64.1.7:8787", "summary", False))
        self.assertEqual(nas["token"], os.path.join(nuc_config.peers_token_dir(), "nas.token"))  # the default token path

    def test_the_default_token_path_and_the_cache_are_defined_per_os_in_one_place(self):
        self.assertEqual(nuc_config.peers_token_dir(), os.path.join(nuc_config.ETC_DIR, "peers"))
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_CACHE": ""}):
            with mock.patch.object(nuc_config, "PORTABLE", "/data"), mock.patch.object(nuc_config, "BASE_DIR", "/data", create=True):
                self.assertEqual(nuc_config.peers_cache_dir(), os.path.join("/data", "peers-cache"))
            with mock.patch.object(nuc_config, "PORTABLE", ""), mock.patch.object(nuc_config, "WINDOWS", False), \
                    mock.patch.object(nuc_config, "MACOS", False), mock.patch.object(nuc_config, "RUN_DIR", "/run/nuc-console"):
                self.assertEqual(nuc_config.peers_cache_dir(), "/run/nuc-console-peers")
            with mock.patch.object(nuc_config, "PORTABLE", ""), mock.patch.object(nuc_config, "WINDOWS", False), \
                    mock.patch.object(nuc_config, "MACOS", True), mock.patch.object(nuc_config, "RUN_DIR", "/var/run/nuc-console"):
                self.assertEqual(nuc_config.peers_cache_dir(), "/var/lib/nuc-console-peers-cache")  # the folder install-macos.sh makes
            with mock.patch.object(nuc_config, "PORTABLE", ""), mock.patch.object(nuc_config, "WINDOWS", True), \
                    mock.patch.object(nuc_config, "MACOS", False), mock.patch.object(nuc_config, "RUN_DIR", os.path.join("C:", "nc", "run")):
                self.assertEqual(nuc_config.peers_cache_dir(), os.path.join("C:", "nc", "run", "peers"))

    def test_a_semicolon_is_a_comment_so_the_separator_is_a_space(self):
        cfg, said = self.load("[hosts]\na = http://100.64.1.1 ; scope=views\n")
        self.assertEqual((said, cfg["hosts"][0]["scope"]), ([], "summary"))

    def test_a_malformed_row_is_ignored_with_a_message_and_never_crashes(self):
        bad = ["Bad_Name = http://x", "-a = http://x", "a" * 33 + " = http://x", "fleet = http://x", "q = \"http://x\"", "q = 'http://x'",
               "q = ftp://x", "q = http://x y", "q = http://x foo=bar", "q = http://x scope=root", "q = http://x token=relative",
               "q = http://x pin=sha256:12", "q = http://x alert=maybe", "q =", "q = http://x token=", "q = http://x token=/a\"b"]
        for row in bad:
            cfg, said = self.load("[hosts]\n%s\nok = http://100.64.0.9\n" % row)
            self.assertEqual([h["name"] for h in cfg["hosts"]], ["ok"], row)
            self.assertEqual(len(said), 1, (row, said))
            self.assertIn("[hosts]", said[0])
            self.assertNotIn(TOKEN, said[0])

    def test_the_message_never_holds_the_content_of_a_token_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        tok = os.path.join(d, "t")
        with open(tok, "w") as f:
            f.write(TOKEN)
        cfg, said = self.load("[hosts]\nq = http://x token=%s zzz=1\n" % tok)
        self.assertEqual(cfg["hosts"], [])
        self.assertNotIn(TOKEN, " ".join(said))

    def test_at_most_16_entries(self):
        cfg, said = self.load("[hosts]\n" + "".join("h%d = http://100.64.0.%d\n" % (i, i) for i in range(17)))
        self.assertEqual(len(cfg["hosts"]), 16)
        self.assertEqual(len(said), 1)
        self.assertIn("h16", said[0])

    def test_no_section_means_no_peers(self):
        self.assertEqual(self.load("[web]\nenabled = no\n")[0]["hosts"], [])
        self.assertEqual(nuc_config.load(os.path.join(hermetic.ABSENT, "none.ini"))["hosts"], [])

    def test_hosts_is_not_for_the_overlay(self):
        self.assertFalse(nuc_config.overlay_allowed("hosts", "x"))
        self.assertFalse(any(sec == "hosts" for sec, _ in nuc_config.OVERLAY_ALLOW))
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        ov = os.path.join(d, "settings.ini")
        with open(ov, "w") as f:
            f.write("[hosts]\nevil = http://100.64.0.66\n[dashboard]\nrefresh_seconds = 3\n")
        os.chmod(ov, 0o640)
        said = []
        self.assertEqual(nuc_config.read_overlay(ov, said.append), {"dashboard.refresh_seconds": "3"})
        self.assertTrue(any("hosts.evil" in s for s in said))
        cfgp = os.path.join(d, "config.ini")
        with open(cfgp, "w") as f:
            f.write("[web]\nsettings_actions = yes\n[hosts]\nreal = http://100.64.0.1\n")
        cfg = nuc_config.load(cfgp, warn=lambda s: None, overlay=ov)
        self.assertEqual([h["name"] for h in cfg["hosts"]], ["real"])  # the overlay laid its allowed key and not the peer

    def test_the_settings_page_cannot_write_hosts_even_in_a_portable_run(self):
        self.assertNotIn("hosts", confedit.BY_SECTION)
        self.assertNotIn("hosts", confedit.MAPS)
        self.assertFalse([k for k in confedit.KEYS if k.section == "hosts"])
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "config.ini")
        with open(path, "w") as f:
            f.write("[web]\nenabled = no\n")
        for form in ({"text": ["evil = http://100.64.0.66"]}, {"evil": ["http://100.64.0.66"]}, {"hosts": ["x"]}):
            with self.assertRaises(confedit.Refused):
                confedit.save(path, "hosts", form)
            with self.assertRaises(confedit.Refused):
                confedit.save_overlay(path, "hosts", form, overlay=os.path.join(d, "settings.ini"))
        with open(path) as f:
            self.assertNotIn("hosts", f.read())

    def test_migrate_carries_the_hosts_section_as_written(self):
        self.assertIn("hosts", confmigrate.KEEP)
        with open(os.path.join(ROOT, "config", "config.ini"), encoding="utf-8") as f:
            dist = f.read()
        old = "[web]\nenabled = yes\n\n[hosts]\nnuc-b = https://nuc-b.example.net token=%s alert=yes\n" % ABS_TOKEN
        new, _lost = confmigrate.build(old, dist)
        self.assertIn("nuc-b = https://nuc-b.example.net token=%s alert=yes" % ABS_TOKEN, new)
        self.assertNotIn("legacy", new.split("[hosts]")[-1].split("\n[")[0])

    def test_config_ini_documents_the_section(self):
        with open(os.path.join(ROOT, "config", "config.ini"), encoding="utf-8") as f:
            self.assertIn("[hosts]", f.read())


# ---- SSRF ----------------------------------------------------------------------------------------------------------------------------
class Url(unittest.TestCase):
    def test_a_good_url(self):
        t = peers.parse_url("https://Peer.Test.:8443/nuc/")
        self.assertEqual((t.scheme, t.host, t.port, t.prefix, t.display), ("https", "peer.test", 8443, "/nuc", "https://peer.test:8443/nuc"))
        self.assertEqual(peers.parse_url("http://100.64.1.7").port, 80)
        self.assertEqual(peers.parse_url("https://[fd7a:115c:a1e0::1]:9").hostline, "[fd7a:115c:a1e0::1]:9")

    def test_what_is_refused(self):
        for url in ("http://user@100.64.0.1", "http://user:pw@100.64.0.1/", "http://a.test/#f", "http://a.test/?q=1", "http://a.test/?",
                    "file:///etc/passwd", "gopher://a.test", "ftp://a.test", "//a.test", "a.test", "", "http://", "http://:80",
                    "https://a.test:0", "https://a.test:65536", "https://a.test:99999", "https://a.test:", "https://a.test:-1", "https://a.test:+80",
                    "http://a.test:8o", "http://a.test/a b", "http://a.test/\t", "http://a.test/%2e%2e", "http://a.test/../x", "http://a.test//x",
                    "http://a_b.test", "http://-a.test", "http://a..test", "http://exämple.test", "http://a.test\\x", "http://[::1", "http://[fe80::1%25eth0]",
                    "http://[fe80::1%eth0]", "http://[1.2.3.4]", "http://[zz]", "http://\"a.test\"", None, 5):
            with self.assertRaises(ValueError, msg=repr(url)):
                peers.parse_url(url)

    def test_other_spellings_of_an_address_are_refused(self):
        for url in ("http://2130706433", "http://0x7f000001", "http://0x7f.1", "http://127.1", "http://127.0.1", "http://0177.0.0.1", "http://01.2.3.4",
                    "http://1.2.3.256", "http://1.2.3.4.5", "http://3232235777", "http://a.test.0x7f", "http://a.test.1", "http://0x"):
            with self.assertRaises(ValueError, msg=url):
                peers.parse_url(url)

    def test_an_ipv4_mapped_literal_parses_and_is_judged_as_the_ipv4_address(self):
        t = peers.parse_url("http://[::ffff:169.254.169.254]/")
        self.assertEqual(peers.classify(t.ip), "forbidden")
        self.assertEqual(peers.classify(peers.parse_url("http://[::ffff:100.64.0.5]").ip), "tailnet")
        self.assertEqual(peers.classify(peers.parse_url("http://[::ffff:127.0.0.1]").ip), "forbidden")


class Addresses(unittest.TestCase):
    def test_always_forbidden(self):
        for a in ("169.254.169.254", "169.254.0.1", "fe80::1", "febf::1", "224.0.0.1", "239.255.255.250", "ff02::1", "0.0.0.0", "::", "0.1.2.3",
                  "240.0.0.1", "255.255.255.255", "192.0.2.1", "198.51.100.7", "203.0.113.9", "198.18.0.1", "2001:db8::1", "::ffff:169.254.169.254",
                  "fec0::1", "64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::1", "fd00:ec2::254", "100.100.100.200", "192.0.0.192"):
            self.assertEqual(peers.classify(ip(a), allow_loopback=True), "forbidden", a)

    def test_the_classes_are_the_same_on_every_python(self):
        """A fixed table, answered by the explicit networks of peers.py (the stdlib's is_private/is_global/is_reserved changed between versions)."""
        table = (
            ("0.0.0.0", "forbidden"), ("0.255.255.255", "forbidden"), ("169.254.169.254", "forbidden"), ("224.0.0.1", "forbidden"),
            ("240.0.0.1", "forbidden"), ("255.255.255.255", "forbidden"), ("192.0.0.0", "forbidden"), ("192.0.0.192", "forbidden"),
            ("192.0.2.1", "forbidden"), ("198.51.100.1", "forbidden"), ("203.0.113.1", "forbidden"), ("198.18.0.1", "forbidden"),
            ("198.19.255.254", "forbidden"), ("192.88.99.1", "forbidden"), ("100.100.100.200", "forbidden"),
            ("::", "forbidden"), ("::2", "forbidden"), ("::1.2.3.4", "forbidden"), ("fe80::1", "forbidden"), ("ff02::1", "forbidden"),
            ("fec0::1", "forbidden"), ("2001:db8::1", "forbidden"), ("2001::1", "forbidden"), ("64:ff9b:1::1", "forbidden"), ("100::1", "forbidden"),
            ("fd00:ec2::254", "forbidden"), ("4000::1", "forbidden"), ("8000::1", "forbidden"), ("5f00::1", "forbidden"),
            ("10.0.0.1", "private"), ("172.16.0.1", "private"), ("172.31.255.255", "private"), ("192.168.0.1", "private"),
            ("fc00::1", "private"), ("fd12:3456::1", "private"),
            ("100.64.0.1", "tailnet"), ("100.127.255.255", "tailnet"), ("fd7a:115c:a1e0::1", "tailnet"),
            ("1.1.1.1", "public"), ("8.8.8.8", "public"), ("93.184.216.34", "public"), ("172.15.0.1", "public"), ("172.32.0.1", "public"),
            ("100.63.255.255", "public"), ("100.128.0.1", "public"), ("192.0.1.1", "public"), ("198.17.255.255", "public"), ("198.20.0.1", "public"),
            ("2606:4700::1111", "public"), ("2001:4860:4860::8888", "public"), ("2a00:1450::1", "public"),
        )
        for addr, kind in table:
            self.assertEqual(peers.classify(ip(addr)), kind, addr)
        for addr in ("127.0.0.1", "127.255.255.255", "::1"):
            self.assertEqual((peers.classify(ip(addr)), peers.classify(ip(addr), allow_loopback=True)), ("forbidden", "loopback"), addr)
        for addr, kind in (("::ffff:10.0.0.1", "private"), ("::ffff:169.254.169.254", "forbidden"), ("::ffff:8.8.8.8", "public"),
                           ("::ffff:100.64.0.1", "tailnet"), ("64:ff9b::808:808", "public"), ("2002:808:808::", "public")):
            self.assertEqual(peers.classify(ip(addr)), kind, addr)

    def test_loopback_only_for_the_tests(self):
        for a in ("127.0.0.1", "127.8.8.8", "::1", "::ffff:127.0.0.1"):
            self.assertEqual(peers.classify(ip(a)), "forbidden", a)
        self.assertEqual(peers.classify(ip("127.0.0.1"), allow_loopback=True), "loopback")
        self.assertEqual(peers.classify(ip("::1"), allow_loopback=True), "loopback")

    def test_private_cgnat_ula_and_public(self):
        for a, kind in (("10.1.2.3", "private"), ("172.16.0.1", "private"), ("172.31.255.1", "private"), ("192.168.1.1", "private"),
                        ("fd12:3456::1", "private"), ("100.64.0.1", "tailnet"), ("100.127.255.254", "tailnet"),
                        ("fd7a:115c:a1e0::5", "tailnet"), ("8.8.8.8", "public"), ("2606:4700::1111", "public"), ("172.32.0.1", "public"),
                        ("100.128.0.1", "public")):
            self.assertEqual(peers.classify(ip(a)), kind, a)

    def test_the_transport_policy(self):
        j = peers.judge
        self.assertEqual(j("https", ["public"]), (True, "", False))
        self.assertEqual(j("https", ["private", "tailnet"]), (True, "", False))
        self.assertEqual(j("http", ["tailnet"]), (True, "", False))
        self.assertEqual(j("http", ["tailnet", "tailnet"]), (True, "", False))
        self.assertEqual(j("http", ["public"]), (False, "plain-http", False))
        self.assertEqual(j("http", ["public"], insecure=True), (False, "plain-http", False))  # a public address is https or nothing
        self.assertEqual(j("http", ["private"]), (False, "plain-http", False))
        self.assertEqual(j("http", ["private"], insecure=True), (True, "", True))
        self.assertEqual(j("http", ["tailnet", "private"], insecure=True), (True, "", True))
        self.assertEqual(j("http", ["tailnet", "private"]), (False, "plain-http", False))
        for scheme in ("http", "https"):  # one forbidden address among good ones refuses the lot
            self.assertEqual(j(scheme, ["tailnet", "forbidden"], insecure=True), (False, "blocked-address", False))
            self.assertEqual(j(scheme, []), (False, "blocked-address", False))


class Ssrf(Base):
    def test_a_name_that_resolves_to_the_metadata_address_is_refused_before_any_socket(self):
        self.no_connection()
        for scheme in ("http", "https"):
            e = peers.poll_peer(self.peer(scheme + "://meta.test"), resolver=lambda h, p: ["169.254.169.254"], limits=FAST)
            self.assertEqual((e["state"], e["error"]), ("unreachable", "blocked-address"))

    def test_one_bad_address_among_several_refuses_the_name(self):
        self.no_connection()
        e = peers.poll_peer(self.peer("https://multi.test"), resolver=lambda h, p: ["8.8.8.8", "fe80::1", "100.64.0.1"], limits=FAST)
        self.assertEqual(e["error"], "blocked-address")
        e = peers.poll_peer(self.peer("https://multi.test"), resolver=lambda h, p: ["8.8.8.8", "fe80::1%eth0"], limits=FAST)
        self.assertEqual(e["error"], "blocked-address")

    def test_a_literal_metadata_address_and_its_ipv4_mapped_spelling_are_refused(self):
        self.no_connection()
        for url in ("http://169.254.169.254", "https://[::ffff:169.254.169.254]", "http://[fe80::1]", "http://224.0.0.1", "http://0.0.0.0",
                    "http://[::]", "http://255.255.255.255", "http://127.0.0.1:8080", "http://[::1]"):
            e = peers.poll_peer(self.peer(url), limits=FAST)
            self.assertEqual((e["state"], e["error"]), ("unreachable", "blocked-address"), url)

    def test_invalid_urls_are_refused_before_any_socket(self):
        self.no_connection()
        for url in ("http://u@100.64.0.1", "http://2130706433", "http://0x7f.1", "file:///etc/passwd", "gopher://x.test", "https://x.test:0",
                    "https://x.test:65536"):
            e = peers.poll_peer(self.peer(url), limits=FAST)
            self.assertEqual((e["state"], e["error"], e["url_display"]), ("unreachable", "bad-url", ""), url)

    def test_loopback_is_refused_without_the_internal_flag(self):
        p = self.fake()
        e = peers.poll_peer(self.peer(p.url), limits=FAST)
        self.assertEqual(e["error"], "blocked-address")
        self.assertEqual(p.requests, [])

    def test_dns_rebinding_the_checked_address_is_the_one_connected(self):
        p = self.fake()
        calls, seen = [], []
        answers = [["100.64.1.2"], ["169.254.169.254"]]

        def resolver(host, port):
            calls.append(host)
            return answers[min(len(calls) - 1, 1)]

        def connect(addr, port, timeout):  # records where it was told to go, then reaches the fake peer instead
            seen.append((str(addr), port))
            return socket.create_connection(("127.0.0.1", p.port), timeout)
        with mock.patch.object(peers, "_connect", connect):
            e = peers.poll_peer(self.peer("http://rebind.test:%d" % p.port), resolver=resolver, limits=FAST)
        self.assertEqual(e["state"], "ok")
        self.assertEqual(calls, ["rebind.test"])  # one resolution: the name is never asked again
        self.assertEqual(seen, [("100.64.1.2", p.port)])

    def test_redirects_are_errors_and_are_not_followed_nor_given_the_token(self):
        target = self.fake(token=None)
        for mode in ("redirect-301", "redirect-302", "redirect-307"):
            p = self.fake()
            p.mode, p.target = mode, target.url + "/api/v1/summary"
            e = self.poll(p)
            self.assertEqual((e["state"], e["error"]), ("unreachable", "redirect"), mode)
            self.assertEqual(target.requests, [], mode)

    def test_no_proxy_from_the_environment_and_the_four_headers_only(self):
        proxy = socket.socket()
        proxy.bind(("127.0.0.1", 0))
        proxy.listen(1)
        proxy.settimeout(0.2)
        self.addCleanup(proxy.close)
        p = self.fake()
        env = {k: "http://127.0.0.1:%d" % proxy.getsockname()[1] for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy")}
        with mock.patch.dict(os.environ, dict(env, NO_PROXY="")):
            e = self.poll(p)
        self.assertEqual(e["state"], "ok")
        with self.assertRaises(socket.timeout):
            proxy.accept()  # nobody came
        path, headers = p.requests[0]
        self.assertEqual(path, "/api/v1/summary")
        self.assertEqual(sorted(k.lower() for k in headers), ["accept", "authorization", "host", "user-agent"])
        self.assertEqual((headers["Authorization"], headers["Accept"], headers["User-Agent"]), ("Bearer " + TOKEN, "application/json", peers.USER_AGENT))
        self.assertEqual(headers["Host"], "127.0.0.1:%d" % p.port)

    def test_the_module_has_no_other_way_out(self):
        with open(os.path.join(ROOT, "src", "peers.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | \
               {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        for banned in ("subprocess", "urllib", "web", "render", "webhttp", "webapi", "requests", "http.server"):
            self.assertNotIn(banned, mods)
        self.assertLessEqual(mods & {"nuc_config", "ui"}, {"nuc_config", "ui"})
        self.assertEqual(len(re.findall(r"\bsocket\.socket\(", slurp(os.path.join(ROOT, "src", "peers.py")))), 1)  # one place opens a connection


# ---- Limits --------------------------------------------------------------------------------------------------------------------------
class Limits(Base):
    def run_mode(self, mode, lim=FAST, **kw):
        p = self.fake(mode=mode)
        t = time.monotonic()
        e = self.poll(p, lim=lim, **kw)
        return e, time.monotonic() - t, p

    def test_slow_loris_on_the_headers_is_closed_at_the_deadline(self):
        e, took, _ = self.run_mode("slow-headers")
        self.assertEqual((e["state"], e["error"]), ("unreachable", "timeout"))
        self.assertLess(took, FAST.total + 0.4)

    def test_slow_loris_on_the_body_is_closed_at_the_deadline_though_each_byte_comes_in_time(self):
        e, took, _ = self.run_mode("slow-body")
        self.assertEqual((e["state"], e["error"]), ("unreachable", "timeout"))
        self.assertGreaterEqual(took, FAST.total - 0.05)
        self.assertLess(took, FAST.total + 0.4)

    def test_a_peer_that_connects_and_says_nothing(self):
        e, took, _ = self.run_mode("silent")
        self.assertEqual(e["error"], "timeout")
        self.assertLess(took, FAST.total + 0.4)

    def test_content_length_of_10_gb_is_refused_without_reading(self):
        e, took, _ = self.run_mode("huge-length")
        self.assertEqual(e["error"], "too-large")
        self.assertLess(took, 0.3)

    def test_chunked_without_end_stops_at_the_cap(self):
        lim = peers.Limits(op=0.3, total=2, summary_bytes=64 * 1024)
        e, took, p = self.run_mode("infinite-chunked", lim=lim)
        self.assertEqual(e["error"], "too-large")
        self.assertLess(took, 1.5)
        self.assertLess(p.sent, 4 * 1024 * 1024)  # it was not drained

    def test_the_body_cap_is_summary_256k_and_views_2m_and_applies_to_an_honest_length(self):
        self.assertEqual((peers.Limits().summary_bytes, peers.Limits().view_bytes, peers.Limits().op, peers.Limits().total),
                         (256 * 1024, 2 * 1024 * 1024, 3.0, 5.0))
        big = fakepeer.summary(host="h", worst=["x"])
        big["pad"] = "A" * (300 * 1024)
        p = self.fake()
        p.docs["summary"] = big
        self.assertEqual(self.poll(p, lim=peers.Limits(op=1, total=2))["error"], "too-large")
        small = peers.Limits(op=1, total=2, summary_bytes=100)
        self.assertEqual(self.poll(self.fake(), lim=small)["error"], "too-large")
        exact = peers.Limits(op=1, total=2, summary_bytes=len(json.dumps(fakepeer.summary(at=1.5))))
        p2 = self.fake()
        p2.docs["summary"] = fakepeer.summary(at=1.5)
        self.assertEqual(self.poll(p2, lim=exact)["state"], "stale")  # at the cap exactly is fine (an old `at`, so stale)

    def test_10_mb_string_is_too_large(self):
        self.assertEqual(self.run_mode("bigstring", lim=peers.Limits(op=2, total=3))[0]["error"], "too-large")

    def test_a_huge_header_is_an_error(self):
        e = self.run_mode("huge-header")[0]
        self.assertEqual(e["state"], "unreachable")
        self.assertNotEqual(e["error"], "")

    def test_content_type_status_and_encoding(self):
        for mode, state, error in (("html", "unreachable", "content-type"), ("gzip", "unreachable", "encoding"), ("unauthorized", "unauthorized", "unauthorized"),
                                   ("forbidden", "unauthorized", "unauthorized"), ("notfound", "incompatible", "not-found"),
                                   ("error500", "unreachable", "http-status")):
            e = self.run_mode(mode)[0]
            self.assertEqual((e["state"], e["error"]), (state, error), mode)
            self.assertNotIn("secret remote text", json.dumps(e))  # a remote body is never echoed

    def test_a_dead_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        e = peers.poll_peer(self.peer("http://127.0.0.1:%d" % port), allow_loopback=True, limits=FAST)
        self.assertEqual((e["state"], e["error"]), ("unreachable", "connect"))


# ---- Parsing -------------------------------------------------------------------------------------------------------------------------
class Parsing(Base):
    def test_a_nest_the_interpreter_may_or_may_not_survive_is_one_answer(self):
        for depth in (65, 200, 5000, 100000):  # RecursionError on some Pythons, a parsed list on others: bad-json on all
            for open_, close in ((b"[", b"]"), (b'{"a":', b"}")):
                with self.assertRaises(peers.PeerError, msg=depth) as c:
                    peers.parse_summary(open_ * depth + b"1" + close * depth)
                self.assertEqual(c.exception.reason, "bad-json", depth)
        ok = peers.parse_summary(b'{"api":1,"view":"summary","host":"h","x":' + b"[" * 63 + b"]" * 63 + b"}")  # under the limit it is a document
        self.assertEqual(ok["host"], "h")
        self.assertEqual(peers.loads(b"[" * 63 + b"]" * 63), eval("[" * 63 + "]" * 63))

    def test_bad_documents(self):
        for mode, error in (("empty", "bad-json"), ("not-json", "bad-json"), ("nested", "bad-json"), ("types", "bad-shape")):
            e = self.poll(self.fake(mode=mode))
            self.assertEqual((e["state"], e["error"], e["summary"]), ("unreachable", error, {}), mode)

    def test_other_shapes_of_json(self):
        for text in ("[]", "5", "null", '"x"', '{"api":1}', '{"api":1,"view":"cpu"}', '{"view":"summary"}', '{"api":true,"view":"summary"}',
                     '{"api":1.5,"view":"summary"}', '{"api":-1,"view":"summary"}', '{"api":1,"view":"summary","at":NaN}', '{"api":1,"view":"summary","at":Infinity}',
                     "﻿{}", '{"a":' + "9" * 40 + "}"):
            with self.assertRaises(peers.PeerError, msg=text):
                peers.parse_summary(text.encode("utf-8"))
        with self.assertRaises(peers.PeerError):
            peers.parse_summary(b"\xff\xfe{}")  # not UTF-8
        with self.assertRaises(peers.PeerError):
            peers.parse_summary("﻿{}".encode("utf-16"))

    def test_the_whitelist_keeps_only_typed_and_sized_fields(self):
        doc = fakepeer.summary(host="x" * 500, host_id="NOT-HEX", version="2.6.0\x1b[2J", os="LINUX!", status={"state": "evil", "text": 5},
                               counts={"err": -1, "warn": True, "unknown": 10 ** 12, "info": 2, "extra": 1}, worst=["a", 1, None, "b", "c", "d"],
                               stale="yes", at="now", problems=2.5, api=1)
        s = peers.parse_summary(json.dumps(doc).encode())
        self.assertEqual(s, {"host": "x" * 120, "api": 1, "counts": {"info": 2}, "worst": ["a", "b", "c"]})

    def test_hostile_text_is_cleaned_before_it_enters_the_entry(self):
        e = self.poll(self.fake(mode="hostile"))
        self.assertEqual(e["state"], "ok")
        s = e["summary"]
        self.assertNotIn("host_id", s)
        self.assertNotIn("version", s)
        self.assertNotIn("os", s)
        self.assertEqual(s["counts"], {"err": 1})
        self.assertEqual(len(s["worst"]), 3)
        for text in [s["host"], s["status_text"]] + s["worst"]:
            self.assertFalse(re.search(r"[\x00-\x1f\x7f-\x9f‮]", text), repr(text))
            self.assertLessEqual(len(text), 120)

    def test_wrong_types_never_make_it_ok(self):
        p = self.fake()
        p.docs["summary"] = fakepeer.summary(status={"state": "evil"}, counts={"err": -5, "warn": True, "unknown": 10 ** 12}, worst={"a": 1}, stale=1, at=1e308)
        e = self.poll(p)
        self.assertEqual((e["state"], e["summary"].get("counts"), e["summary"].get("worst")), ("unknown", None, None))

    def test_the_states_of_a_summary(self):
        for given, state in (("ok", "ok"), ("warn", "warn"), ("err", "err"), ("down", "down"), ("info", "info"), ("unknown", "unknown")):
            p = self.fake()
            p.docs["summary"] = fakepeer.summary(status={"state": given, "text": "t"})
            self.assertEqual(self.poll(p)["state"], state)
        p = self.fake()
        p.docs["summary"] = fakepeer.summary(status="warn")  # a bare string is read too
        self.assertEqual(self.poll(p)["state"], "warn")
        p = self.fake()
        p.docs["summary"] = {k: v for k, v in fakepeer.summary().items() if k != "status"}
        self.assertEqual(self.poll(p)["state"], "unknown")  # a missing field is "?", never ok

    def test_the_views_scope_fetches_five_documents_and_strips_csrf_and_token_keys(self):
        p = self.fake()
        e = self.poll(p, scope="views")
        self.assertEqual(sorted(e["views"]), sorted(peers.VIEWS))
        self.assertEqual([r[0] for r in p.requests], ["/api/v1/summary"] + ["/api/v1/" + v for v in peers.VIEWS])
        for name, doc in e["views"].items():
            self.assertEqual(doc["view"], name)
            self.assertNotIn("csrf", json.dumps(doc))
            self.assertNotIn("token", json.dumps(doc))
            self.assertEqual(doc["nodes"], [{"title": "x", "n": 1}])
        self.assertNotIn("ai", e["views"])
        self.assertEqual(self.poll(self.fake(), scope="summary")["views"], {})

    def test_a_view_that_is_not_what_it_claims_is_dropped_and_noted(self):
        p = self.fake()
        p.docs["cpu"] = fakepeer.view("health")
        p.docs["map"] = {"api": 1, "view": "map", "x": ["\x1b[31m" + "a" * 5000]}
        e = self.poll(p, scope="views")
        self.assertNotIn("cpu", e["views"])
        self.assertIn("view-failed:cpu", e["warn"])
        self.assertEqual(e["views"]["map"]["x"], ["?[31m" + "a" * (2000 - 5)])

    def test_the_documents_are_depth_and_size_guarded(self):
        deep = {"api": 1, "view": "cpu", "n": None}
        node = deep
        for _ in range(60):
            node["n"] = {"n": None}
            node = node["n"]
        with self.assertRaises(peers.PeerError):
            peers.parse_view("cpu", json.dumps(deep).encode())
        self.assertEqual(peers.tree({"a": [1, {"b": float("inf")}], "csrf_token": "x", "Token": 1, "TOKENIZER": 2, "ok": True}),
                         {"a": [1, {"b": None}], "ok": True})

    def test_version_skew(self):
        e = self.poll(self.fake(mode="newer"))
        self.assertEqual((e["state"], e["error"], e["summary"]), ("incompatible", "api-newer", {}))  # shown, never interpreted
        e = self.poll(self.fake(mode="old"))  # 2.5.x: only the minimum
        self.assertEqual(e["state"], "ok")
        self.assertIn("legacy-peer", e["warn"])
        for k in ("host_id", "version", "os", "counts", "worst"):
            self.assertNotIn(k, e["summary"])
        self.assertEqual((e["summary"]["host"], e["summary"]["api"], e["summary"]["problems"]), ("nuc-b", 1, 0))
        p = self.fake()
        p.docs["summary"] = dict(fakepeer.summary(), version="banana", future_field={"x": [1]})  # a version that is not semver, an extra field
        e = self.poll(p)
        self.assertEqual((e["state"], e["summary"]["version"]), ("ok", "banana"))
        self.assertNotIn("future_field", e["summary"])
        self.assertNotIn("legacy-peer", e["warn"])
        p.docs["summary"] = fakepeer.summary(counts=None)  # a peer without counts
        self.assertIn("legacy-peer", self.poll(p)["warn"])

    def test_stale_is_not_ok(self):
        e = self.poll(self.fake(mode="stale"))
        self.assertEqual(e["state"], "stale")
        self.assertIs(e["summary"]["stale"], True)


# ---- Clock ---------------------------------------------------------------------------------------------------------------------------
class Clock(Base):
    def test_skew_comes_from_the_date_header(self):
        p = self.fake()
        p.date_offset = 3600  # the peer's clock is an hour ahead, and so is its `at`
        p.docs["summary"] = fakepeer.summary(at=time.time() + 3600)
        e = self.poll(p)
        self.assertLess(abs(e["skew_s"] - 3600), 5)
        self.assertEqual(e["state"], "ok")
        self.assertIn("clock-skew", e["warn"])

    def test_a_peer_with_the_right_clock_has_no_skew_note(self):
        e = self.poll(self.fake())
        self.assertLess(abs(e["skew_s"]), 5)
        self.assertNotIn("clock-skew", e["warn"])

    def test_the_age_is_the_peers_clock_against_its_own_never_the_local_clock(self):
        p = self.fake()
        p.date_offset = 7200  # the peer's clock runs two hours ahead, and so does its `at`
        p.docs["summary"] = fakepeer.summary(at=time.time() + 7200 - 3)
        e = self.poll(p)
        self.assertEqual(e["state"], "ok")
        self.assertLess(e["age_s"], 10)

    def test_an_old_at_is_stale_and_an_at_in_the_future_does_not_crash(self):
        p = self.fake()
        p.docs["summary"] = fakepeer.summary(at=time.time() - 3600)
        e = self.poll(p)
        self.assertEqual(e["state"], "stale")
        self.assertGreater(e["age_s"], 3000)
        p.docs["summary"] = fakepeer.summary(at=time.time() + 86400)
        e = self.poll(p)
        self.assertEqual((e["state"], e["age_s"]), ("ok", 0))

    def test_no_date_header_means_no_skew(self):
        p = self.fake()
        with mock.patch.object(fakepeer.Handler, "date_time_string", lambda self, t=None: "garbage"):
            e = self.poll(p)
        self.assertEqual((e["state"], e["skew_s"], e["age_s"]), ("ok", None, None))

    def test_the_deadline_uses_the_injected_monotonic_clock(self):
        ticks = iter([0.0, 0.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0])
        e = self.poll(self.fake(), clock=lambda: next(ticks, 99.0))
        self.assertEqual((e["state"], e["error"]), ("unreachable", "timeout"))


# ---- TLS -----------------------------------------------------------------------------------------------------------------------------
class Tls(Base):
    def test_a_pinned_self_signed_certificate_is_accepted_instead_of_the_chain(self):
        e = self.poll(self.fake(tls=True), lim=TLS, pin=PIN)
        self.assertEqual((e["state"], e["error"], e["insecure"]), ("ok", "", False))

    def test_the_wrong_pin_is_untrusted_and_never_falls_back(self):
        p = self.fake(tls=True)
        e = self.poll(p, lim=TLS, pin="sha256:" + "0" * 64)
        self.assertEqual((e["state"], e["error"], e["summary"]), ("untrusted", "pin-mismatch", {}))
        self.assertEqual(p.requests, [])  # no request, so no token, went to it

    def test_a_pin_stands_in_for_the_chain_and_the_name(self):
        p = self.fake(tls=True)
        e = self.poll(p, lim=TLS, pin=PIN, resolver=lambda h, port: ["127.0.0.1"], url="https://anything.test:%d" % p.port)
        self.assertEqual(e["state"], "ok")

    def test_a_self_signed_certificate_without_a_pin_fails(self):
        p = self.fake(tls=True)
        e = self.poll(p, lim=TLS)
        self.assertEqual((e["state"], e["error"]), ("untrusted", "tls-verify"))
        self.assertEqual(p.requests, [])

    def test_the_chain_and_the_name_are_verified_with_the_cas_given(self):
        p = self.fake(tls=True)
        res = lambda h, port: ["127.0.0.1"]  # noqa: E731
        ok = self.poll(p, lim=TLS, cafile=PEM, resolver=res, url="https://peer.test:%d" % p.port)
        self.assertEqual(ok["state"], "ok")
        ok = self.poll(p, lim=TLS, cafile=PEM, url=p.url)  # the IP literal is in the certificate too
        self.assertEqual(ok["state"], "ok")
        bad = self.poll(p, lim=TLS, cafile=PEM, resolver=res, url="https://other.test:%d" % p.port)  # a name the certificate does not carry
        self.assertEqual((bad["state"], bad["error"]), ("untrusted", "tls-verify"))
        self.assertEqual(len(p.requests), 2)

    def test_https_to_a_server_that_speaks_http_is_an_error_not_a_hang(self):
        p = self.fake()
        e = self.poll(p, lim=TLS, url="https://127.0.0.1:%d" % p.port)
        self.assertEqual(e["state"], "unreachable")
        self.assertIn(e["error"], ("tls", "timeout", "connect"))

    def test_http_to_a_public_address_is_refused_before_any_socket(self):
        self.no_connection()
        for insecure in (False, True):
            e = peers.poll_peer(self.peer("http://public.test:8787", insecure=insecure), resolver=lambda h, p: ["93.184.216.34"], limits=FAST)
            self.assertEqual((e["state"], e["error"], e["insecure"]), ("unreachable", "plain-http", False))

    def test_http_to_a_private_address_needs_insecure_and_then_carries_the_flag(self):
        p = self.fake()
        seen = []

        def connect(addr, port, timeout):
            seen.append(str(addr))
            return socket.create_connection(("127.0.0.1", p.port), timeout)
        res = lambda h, port: ["10.9.8.7"]  # noqa: E731
        with mock.patch.object(peers, "_connect", connect):
            e = peers.poll_peer(self.peer("http://lan.test:%d" % p.port), resolver=res, limits=FAST)
            self.assertEqual((e["state"], e["error"], seen), ("unreachable", "plain-http", []))
            e = peers.poll_peer(self.peer("http://lan.test:%d" % p.port, insecure=True), resolver=res, limits=FAST)
        self.assertEqual((e["state"], e["insecure"], seen), ("ok", True, ["10.9.8.7"]))
        self.assertIn("insecure-transport", e["warn"])

    def test_http_to_a_tailscale_address_is_fine_and_not_flagged(self):
        p = self.fake()
        with mock.patch.object(peers, "_connect", lambda a, port, t: socket.create_connection(("127.0.0.1", p.port), t)):
            e = peers.poll_peer(self.peer("http://100.64.3.4:%d" % p.port), limits=FAST)
            e6 = peers.poll_peer(self.peer("http://[fd7a:115c:a1e0::9]:%d" % p.port), limits=FAST)
        self.assertEqual((e["state"], e["insecure"], e6["state"], e6["insecure"]), ("ok", False, "ok", False))


# ---- Secrets -------------------------------------------------------------------------------------------------------------------------
class Secrets(Base):
    def test_the_token_goes_in_the_authorization_header_and_nowhere_else(self):
        p = self.fake()
        e = self.poll(p, scope="views")
        self.assertEqual(e["state"], "ok")
        self.assertClean(e)
        for path, headers in p.requests:
            self.assertEqual([v for k, v in headers.items() if TOKEN in v], ["Bearer " + TOKEN])
            self.assertNotIn(TOKEN, path)

    def test_a_wrong_token_is_unauthorized_and_the_text_of_the_error_never_holds_it(self):
        p = self.fake(token="another-token-" + "x" * 12)
        e = self.poll(p)
        self.assertEqual((e["state"], e["error"]), ("unauthorized", "unauthorized"))
        self.assertClean(e)

    def test_the_token_file_is_read_with_care(self):
        p = self.fake()
        for name, make in (("missing", lambda d: os.path.join(d, "none")), ("short", lambda d: self.write_token("short", "s.token")),
                           ("bad-chars", lambda d: self.write_token("abc\r\nX-Evil: " + "y" * 20, "c.token")),
                           ("empty", lambda d: self.write_token("", "e.token")), ("too-big", lambda d: self.write_token("a" * 600, "big.token")),
                           ("directory", lambda d: d)):
            e = peers.poll_peer(self.peer(p.url, token=make(self.dir)), allow_loopback=True, limits=FAST)
            self.assertEqual((e["state"], e["error"]), ("unauthorized", "token-file"), name)
            self.assertClean(e)
        self.assertEqual(p.requests, [])  # no token, no request

    @unittest.skipUnless(POSIX, "mode bits, links and owners")
    def test_the_token_file_must_be_private_and_not_a_link(self):
        p = self.fake()
        loose = self.write_token(TOKEN, "loose.token")
        os.chmod(loose, 0o644)
        group = self.write_token(TOKEN, "group.token")
        os.chmod(group, 0o640)
        link = os.path.join(self.dir, "link.token")
        os.symlink(self.token, link)
        fifo = os.path.join(self.dir, "fifo.token")
        os.mkfifo(fifo)
        for name, path in (("0644", loose), ("0640", group), ("symlink", link), ("fifo", fifo)):
            e = peers.poll_peer(self.peer(p.url, token=path), allow_loopback=True, limits=FAST)
            self.assertEqual((e["state"], e["error"]), ("unauthorized", "token-file"), name)
        os.chmod(self.dir, 0o777)  # a folder others can write to: they could swap the file
        try:
            e = peers.poll_peer(self.peer(p.url), allow_loopback=True, limits=FAST)
        finally:
            os.chmod(self.dir, 0o700)
        self.assertEqual(e["error"], "token-file")
        self.assertEqual(p.requests, [])
        self.assertEqual(self.poll(p)["state"], "ok")

    @unittest.skipUnless(POSIX and hasattr(os, "geteuid"), "owners")
    def test_a_token_owned_by_someone_else_is_refused(self):
        real = os.fstat

        class St(object):
            def __init__(self, st):
                self.st = st

            def __getattr__(self, n):
                return self.st.st_uid + 12345 if n == "st_uid" else getattr(self.st, n)
        with mock.patch.object(peers.os, "fstat", lambda fd: St(real(fd))):
            with self.assertRaises(peers.PeerError):
                peers.read_secret(self.token)

    def test_nothing_in_the_cache_the_output_or_the_files_holds_the_token(self):
        p = self.fake()
        folder = os.path.join(self.dir, "cache")
        poller = peers.Poller([self.peer(p.url, scope="views")], folder, state=self.state, allow_loopback=True, limits=FAST)
        self.addCleanup(poller.pool.shutdown, False)
        poller.run_once()
        for fn in os.listdir(folder):
            with open(os.path.join(folder, fn), "rb") as f:
                self.assertNotIn(TOKEN.encode(), f.read(), fn)

    def test_no_secret_in_argv_or_processes(self):
        with open(os.path.join(ROOT, "src", "peers.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("subprocess", src)
        self.assertNotIn("os.system", src)
        self.assertNotIn("print(token", src)
        for needle in ("sys.argv",):
            self.assertEqual(src.count(needle), 1)  # main() only


# ---- The cache -----------------------------------------------------------------------------------------------------------------------
class Cache(Base):
    def entry(self, name="b", **kw):
        e = peers.new_entry(self.peer("http://100.64.0.1", name=name), time.time())
        e["state"] = "ok"
        e["summary"] = {"host": "nuc-b", "api": 1}
        e.update(kw)
        return e

    def write(self, entries):
        peers.write_cache(self.cache, entries, time.time(), keep={e["name"] for e in entries})

    def test_write_then_read(self):
        a, b = self.entry("a"), self.entry("b", state="warn", warn=["legacy-peer"])
        self.write([a, b])
        got = peers.read_cache(self.cache)
        self.assertEqual(sorted(got["peers"]), ["a", "b"])
        self.assertEqual((got["peers"]["b"]["state"], got["peers"]["b"]["warn"], got["fleet"]["worst"], got["skipped"]), ("warn", ["legacy-peer"], "warn", 0))
        for key in ("v", "name", "url_display", "ts", "state", "error", "summary", "view_index", "scope", "warn", "insecure", "identity"):
            self.assertIn(key, got["peers"]["a"])
        self.assertEqual(sorted(os.listdir(self.cache)), [".owner", "a.json", "b.json", "fleet.json"])  # no temporary file left
        if POSIX:
            for fn in os.listdir(self.cache):
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.cache, fn)).st_mode), 0o644)

    def test_the_fleet_is_as_bad_as_its_worst_peer(self):
        for states, worst in ((["ok", "ok"], "ok"), (["ok", "warn"], "warn"), (["ok", "unreachable"], "unreachable"), (["err", "unreachable"], "err"),
                              (["warn", "untrusted"], "untrusted"), (["ok", "down"], "down"), ([], "unknown"), (["ok", "stale"], "stale")):
            self.assertEqual(peers.fleet_doc([self.entry(str(i), state=s) for i, s in enumerate(states)], 1)["worst"], worst, states)

    def test_the_file_of_a_peer_no_longer_configured_goes_and_foreign_files_stay(self):
        self.write([self.entry("a"), self.entry("b")])
        for fn in ("package.json", "x.json", "b2.json"):  # not ours: a name that looks like a peer's file is not enough
            spit(os.path.join(self.cache, fn), "{}")
        if POSIX:
            os.symlink("/etc/passwd", os.path.join(self.cache, "link.json"))
        peers.write_cache(self.cache, [self.entry("a")], time.time(), keep={"a"})
        left = set(os.listdir(self.cache))
        self.assertNotIn("b.json", left)
        self.assertLessEqual({"a.json", "fleet.json", ".owner", "package.json", "x.json", "b2.json"}, left)
        self.assertEqual(slurp(os.path.join(self.cache, "package.json")), "{}")

    def test_the_atomic_write_never_leaves_half_a_file(self):
        self.write([self.entry("a")])
        before = slurp(os.path.join(self.cache, "a.json"))
        with mock.patch.object(os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                peers.write_json(self.cache, "a.json", {"x": 1})
        self.assertEqual(slurp(os.path.join(self.cache, "a.json")), before)
        self.assertEqual(sorted(os.listdir(self.cache)), [".owner", "a.json", "fleet.json"])

    def test_too_big_with_its_documents_keeps_the_summary(self):
        big = self.entry("a", views={"cpu": {"api": 1, "view": "cpu", "x": "y" * 2000}})
        with mock.patch.object(peers, "CACHE_MAX", len(json.dumps(dict(big, views={}), separators=(",", ":"))) + 300):
            self.write([big])
        got = peers.read_cache(self.cache, views=True)["peers"]["a"]
        self.assertEqual((got["views"], got["view_index"], got["summary"]["host"], "views-dropped" in got["warn"]), ({}, {}, "nuc-b", True))
        self.assertFalse(os.path.exists(os.path.join(self.cache, "a.views.json")))

    def test_the_reader_skips_what_it_cannot_trust(self):
        self.write([self.entry("a")])
        good = os.path.join(self.cache, "a.json")
        text = slurp(good)
        w = lambda name, body: spit(os.path.join(self.cache, name), body)  # noqa: E731
        w("c.json", "{not json")
        w("d.json", json.dumps(dict(json.loads(text), name="other")))  # the name is the file's
        w("e.json", json.dumps(dict(json.loads(text), name="e", state="fine")))
        w("f.json", json.dumps(dict(json.loads(text), name="f", ts="now")))
        w("g.json", json.dumps(dict(json.loads(text), name="g", error="a remote text")))
        w("h.json", json.dumps(dict(json.loads(text), name="h", scope="root")))
        w("i.json", "[" * 100000)
        w("j.json", json.dumps([1]))
        w("k.json", json.dumps(dict(json.loads(text), name="k", v=2)))
        w("fleet.json", json.dumps({"v": 1, "ts": "x", "worst": "ok"}))
        w("notes.txt", "x")
        got = peers.read_cache(self.cache)
        self.assertEqual(sorted(got["peers"]), ["a"])
        self.assertEqual((got["skipped"], got["fleet"]), (10, None))

    def test_the_reader_cleans_types_and_text_again(self):
        self.write([self.entry("a")])
        path = os.path.join(self.cache, "a.json")
        doc = json.loads(slurp(path))
        views = {"cpu": {"view": "cpu", "csrf": "z", "x": "\x1b[0m"}, "ai": {"view": "ai"}, "map": {"view": "cpu"}}
        spit(os.path.join(self.cache, "a.views.json"), json.dumps({"v": 1, "views": views}))
        doc.update(view_index={"cpu": {"size": 5, "ts": 1}, "ai": {"size": 1, "ts": 1}, "map": {"size": "x", "ts": 1}}, url_display="http://\x1b[31mevil\n", summary={"host": "\x1b]0;x\x07h", "counts": {"err": "9", "warn": 2}, "worst": [1, "w"], "state": "weird",
                                                                   "api": "1", "extra": 1}, warn=["ok", 5, "\x1b"], skew_s="x", last_ok=-4, age_s=True)
        spit(path, json.dumps(doc))
        got = peers.read_cache(self.cache, views=True)["peers"]["a"]
        self.assertEqual(got["summary"], {"host": "?]0;x?h", "counts": {"warn": 2}, "worst": ["w"]})
        self.assertEqual((got["warn"], got["skew_s"], got["last_ok"], got["age_s"]), (["ok", "?"], None, None, None))
        self.assertNotIn("\x1b", got["url_display"])
        self.assertEqual(got["views"], {"cpu": {"view": "cpu", "x": "?[0m"}})
        cheap = peers.read_cache(self.cache)["peers"]["a"]
        self.assertNotIn("views", cheap)  # not unless asked
        self.assertEqual(cheap["view_index"], {"cpu": {"size": 5, "ts": 1}})  # only the names of the known documents, typed
        self.assertEqual(peers.read_views(self.cache, "a"), {"cpu": {"view": "cpu", "x": "?[0m"}})

    def test_the_documents_live_in_their_own_file_and_the_cheap_read_never_opens_it(self):
        e = self.entry("a", views={"cpu": {"api": 1, "view": "cpu", "x": "y" * 5000}, "map": {"api": 1, "view": "map"}})
        self.write([e])
        self.assertEqual(sorted(os.listdir(self.cache)), [".owner", "a.json", "a.views.json", "fleet.json"])
        main = slurp(os.path.join(self.cache, "a.json"))
        self.assertNotIn("yyyy", main)
        self.assertLess(len(main), 1500)
        self.assertEqual(sorted(json.loads(main)["view_index"]), ["cpu", "map"])
        self.assertGreater(json.loads(main)["view_index"]["cpu"]["size"], 5000)
        opened = []
        real = peers.read_json
        with mock.patch.object(peers, "read_json", lambda path, limit: opened.append(os.path.basename(path)) or real(path, limit)):
            cheap = peers.read_cache(self.cache)
            self.assertNotIn("a.views.json", opened)
            self.assertEqual((sorted(cheap["peers"]["a"]["view_index"]), "views" in cheap["peers"]["a"], cheap["skipped"]), (["cpu", "map"], False, 0))
            full = peers.read_cache(self.cache, views=True)["peers"]["a"]
        self.assertIn("a.views.json", opened)
        self.assertEqual(sorted(full["views"]), ["cpu", "map"])
        self.assertEqual(peers.read_views(self.cache, "a")["map"], {"api": 1, "view": "map"})
        self.assertEqual(peers.read_views(self.cache, "nobody"), {})
        self.assertEqual(peers.read_views(self.cache, "../a"), {})
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.cache, "a.views.json")).st_mode), 0o644)

    def test_a_peer_without_documents_any_more_loses_its_views_file_and_a_views_file_is_not_a_peer(self):
        self.write([self.entry("a", views={"cpu": {"view": "cpu"}})])
        self.assertTrue(os.path.exists(os.path.join(self.cache, "a.views.json")))
        self.write([self.entry("a")])
        self.assertFalse(os.path.exists(os.path.join(self.cache, "a.views.json")))
        spit(os.path.join(self.cache, "zz.views.json"), "{}")
        got = peers.read_cache(self.cache)
        self.assertEqual((sorted(got["peers"]), got["skipped"]), (["a"], 0))

    def test_the_views_files_of_a_peer_no_longer_configured_go_and_foreign_ones_stay(self):
        self.write([self.entry("a", views={"cpu": {"view": "cpu"}}), self.entry("b", views={"cpu": {"view": "cpu"}})])
        spit(os.path.join(self.cache, "x.views.json"), "{}")  # not ours: fleet.json never listed x
        peers.write_cache(self.cache, [self.entry("a", views={"cpu": {"view": "cpu"}})], time.time(), keep={"a"})
        left = set(os.listdir(self.cache))
        self.assertNotIn("b.json", left)
        self.assertNotIn("b.views.json", left)
        self.assertLessEqual({"a.json", "a.views.json", "x.views.json"}, left)

    @unittest.skipUnless(POSIX, "links")
    def test_a_views_file_that_is_a_link_or_writable_by_others_is_not_read(self):
        self.write([self.entry("a", views={"cpu": {"view": "cpu"}})])
        path = os.path.join(self.cache, "a.views.json")
        os.chmod(path, 0o666)
        self.assertEqual(peers.read_views(self.cache, "a"), {})
        os.chmod(path, 0o644)
        self.assertEqual(sorted(peers.read_views(self.cache, "a")), ["cpu"])
        os.rename(path, path + ".real")
        os.symlink(path + ".real", path)
        self.assertEqual(peers.read_views(self.cache, "a"), {})
        self.assertEqual(sorted(peers.read_cache(self.cache, views=True)["peers"]["a"]["views"]), [])

    def test_an_old_entry_is_stale_never_ok(self):
        self.write([self.entry("a", ts=int(time.time()) - 500), self.entry("b", state="err", ts=int(time.time()) - 500),
                    self.entry("c", state="unreachable", ts=int(time.time()) - 500)])
        got = peers.read_cache(self.cache)
        self.assertEqual({n: e["state"] for n, e in got["peers"].items()}, {"a": "stale", "b": "err", "c": "unreachable"})
        self.assertEqual(got["peers"]["a"]["error"], "cache-old")
        self.assertEqual(peers.read_cache(self.cache, now=time.time() + 3600)["age_s"] >= 3600, True)

    def test_no_cache_at_all(self):
        got = peers.read_cache(os.path.join(self.dir, "nothing"))
        self.assertEqual((got["peers"], got["fleet"], got["age_s"]), ({}, None, None))

    @unittest.skipUnless(POSIX, "links, modes and sizes of files another account wrote")
    def test_the_reader_refuses_links_loose_modes_and_big_files(self):
        self.write([self.entry("a")])
        good = os.path.join(self.cache, "a.json")
        os.symlink(good, os.path.join(self.cache, "l.json"))
        loose = os.path.join(self.cache, "w.json")
        shutil.copy(good, loose)
        os.chmod(loose, 0o666)
        big = os.path.join(self.cache, "z.json")
        with open(big, "w") as f:
            f.write(" " * (peers.CACHE_MAX + 1))
        fifo = os.path.join(self.cache, "p.json")
        os.mkfifo(fifo)
        self.assertEqual(sorted(peers.read_cache(self.cache)["peers"]), ["a"])
        os.chmod(self.cache, 0o777)  # the folder itself writable by others: nothing in it can be trusted
        try:
            self.assertEqual(peers.read_cache(self.cache)["peers"], {})
        finally:
            os.chmod(self.cache, 0o755)

    def test_identity_is_remembered_at_the_first_contact_and_a_change_is_flagged(self):
        known = peers.Known(self.state)
        first = self.entry(summary={"host_id": "a" * 16, "host": "h"})
        self.assertTrue(known.check(first))
        known.save(self.state)
        again = peers.Known(self.state)
        same = self.entry(summary={"host_id": "a" * 16})
        self.assertEqual((again.check(same), same["identity"], same["state"]), (False, "ok", "ok"))
        other = self.entry(summary={"host_id": "b" * 16, "host": "evil"}, views={"cpu": {}})
        again.check(other)
        self.assertEqual((other["identity"], other["state"], other["error"], other["summary"], other["views"]), ("changed", "untrusted", "identity-changed", {}, {}))
        gone = self.entry(summary={"host": "downgraded"})  # an id that was there and is not any more
        again.check(gone)
        self.assertEqual((gone["identity"], gone["state"]), ("changed", "untrusted"))
        down = self.entry(state="unreachable", summary={})  # an unreachable peer says nothing about identity
        self.assertEqual((again.check(down), down["identity"]), (False, "ok"))
        legacy = self.entry("old", summary={"host": "x"})  # first contact without an id: nothing to remember
        self.assertEqual((again.check(legacy), "old" in again.ids), (False, False))
        self.assertEqual(json.loads(slurp(os.path.join(self.state, "known"))), {"v": 1, "ids": {"b": "a" * 16}})

    def test_unreachable_keeps_the_last_data_with_its_age_and_is_never_ok(self):
        p = self.fake()
        first = self.poll(p, scope="views")
        self.assertEqual(first["state"], "ok")
        p.stop()
        e = peers.poll_peer(self.peer(p.url, scope="views"), prev=first, allow_loopback=True, limits=FAST)
        self.assertEqual((e["state"], e["error"]), ("unreachable", "connect"))
        self.assertEqual((e["summary"], e["views"], e["last_ok"]), (first["summary"], first["views"], first["last_ok"]))
        self.assertIn("showing-old-data", e["warn"])


# ---- Failure -------------------------------------------------------------------------------------------------------------------------
class Failure(Base):
    def test_a_dead_peer_does_not_delay_the_others(self):
        good = [self.fake() for _ in range(4)]
        dead = self.fake(mode="silent")
        cfg = [self.peer(g.url, name="g%d" % i) for i, g in enumerate(good)] + [self.peer(dead.url, name="dead")]
        poller = peers.Poller(cfg, self.cache, state=self.state, allow_loopback=True, limits=FAST)
        self.addCleanup(poller.pool.shutdown, False)
        t = time.monotonic()
        entries = poller.run_once()
        took = time.monotonic() - t
        self.assertEqual([e["state"] for e in entries], ["ok"] * 4 + ["unreachable"])
        self.assertEqual(entries[-1]["error"], "timeout")
        self.assertLess(took, FAST.total + 0.4)  # parallel: not 4 x 0.4 s
        got = peers.read_cache(self.cache)
        self.assertEqual(got["fleet"]["worst"], "unreachable")  # the fleet is as bad as its worst

    def test_at_most_four_requests_at_a_time(self):
        lock, state = threading.Lock(), {"now": 0, "max": 0}

        def slow(peer, prev, wall, **net):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return dict(peers.new_entry(peer, 1), state="ok")
        cfg = [{"name": "p%d" % i, "url": "http://100.64.0.1", "scope": "summary"} for i in range(10)]
        poller = peers.Poller(cfg, self.cache, state=self.state, poll=slow)
        self.addCleanup(poller.pool.shutdown, False)
        poller.run_once()
        self.assertEqual(state["max"], 4)
        self.assertEqual(peers.WORKERS, 4)

    def poller_with(self, results, t):
        cfg = [{"name": "p", "url": "http://100.64.0.1", "scope": "summary"}]
        it = iter(results)

        def poll(peer, prev, wall, **net):
            return dict(peers.new_entry(peer, 1), state=next(it))
        poller = peers.Poller(cfg, self.cache, state=self.state, poll=poll, clock=lambda: t[0], jitter=lambda: 1.0)
        self.addCleanup(poller.pool.shutdown, False)
        return poller

    def settle(self, poller):
        for f in list(poller.running.values()):
            f.exception()
        poller.step()

    def test_the_backoff_doubles_from_5_seconds_to_5_minutes_and_resets_on_success(self):
        t = [0.0]
        poller = self.poller_with(["unreachable"] * 9 + ["ok", "ok"], t)
        waits = []
        for _ in range(9):
            poller.step()
            self.settle(poller)
            waits.append(poller.next["p"] - t[0])
            t[0] = poller.next["p"]
        self.assertEqual(waits, [5, 10, 20, 40, 80, 160, 300, 300, 300])
        poller.step()
        self.settle(poller)
        self.assertEqual(poller.next["p"] - t[0], peers.INTERVAL)  # back to the base interval
        self.assertEqual(poller.fails["p"], 0)

    def test_nothing_is_polled_before_it_is_due_and_the_interval_has_a_floor(self):
        t = [0.0]
        poller = self.poller_with(["ok"] * 5, t)
        poller.step()
        self.settle(poller)
        t[0] = 14.9
        poller.step()
        self.assertEqual(poller.running, {})
        t[0] = 15.0
        poller.step()
        self.assertEqual(list(poller.running), ["p"])
        self.assertEqual(peers.Poller([], self.cache, state=self.state, interval=1).interval, peers.INTERVAL_MIN)
        self.assertEqual(peers.Poller([], self.cache, state=self.state, interval=40).interval, 40)

    def test_the_jitter_is_applied(self):
        poller = peers.Poller([{"name": "p"}], self.cache, state=self.state, jitter=lambda: 1.2)
        self.addCleanup(poller.pool.shutdown, False)
        self.assertAlmostEqual(poller.delay("p", {"state": "unreachable"}), 6.0)
        self.assertAlmostEqual(poller.delay("p", {"state": "ok"}), peers.INTERVAL * 1.2)

    def test_an_untrusted_peer_backs_off_too_and_a_crash_is_not_ok(self):
        t = [0.0]
        cfg = [{"name": "p", "url": "http://100.64.0.1", "scope": "summary"}]

        def boom(peer, prev, wall, **net):
            raise RuntimeError("secret " + TOKEN)
        poller = peers.Poller(cfg, self.cache, state=self.state, poll=boom, clock=lambda: t[0], jitter=lambda: 1.0)
        self.addCleanup(poller.pool.shutdown, False)
        poller.step()
        self.settle(poller)
        e = poller.entries["p"]
        self.assertEqual((e["state"], e["error"]), ("unreachable", "internal"))
        self.assertClean(e)
        self.assertEqual(poller.next["p"], 5)

    def test_identity_change_is_written_to_the_cache_by_the_poller(self):
        p = self.fake()
        cfg = [self.peer(p.url)]
        poller = peers.Poller(cfg, self.cache, state=self.state, allow_loopback=True, limits=FAST)
        self.addCleanup(poller.pool.shutdown, False)
        self.assertEqual(poller.run_once()[0]["state"], "ok")
        p.docs["summary"] = fakepeer.summary(host_id="f" * 16)
        again = peers.Poller(cfg, self.cache, state=self.state, allow_loopback=True, limits=FAST)  # a new process: it reads the known file
        self.addCleanup(again.pool.shutdown, False)
        e = again.run_once()[0]
        self.assertEqual((e["state"], e["identity"], e["summary"]), ("untrusted", "changed", {}))
        self.assertEqual(peers.read_cache(self.cache)["peers"]["b"]["identity"], "changed")


# ---- The command ---------------------------------------------------------------------------------------------------------------------
class Command(Base):
    NET = dict(allow_loopback=True, limits=FAST)

    def cli(self, argv, hosts):
        out = []
        with mock.patch.object(nuc_config, "peers_cache_dir", lambda: self.cache), mock.patch.object(nuc_config, "peers_state_dir", lambda: self.state):
            code = peers.main(argv, out=out.append, cfg={"hosts": hosts}, net=self.NET)
        return code, "\n".join(out)

    def test_usage(self):
        for argv in ([], ["nope"], ["test"], ["list", "x"], ["once", "x"], ["test", "a", "b"], ["pair"], ["forget"], ["forget", "a", "b"]):
            self.assertEqual(self.cli(argv, [self.peer("http://100.64.0.1")])[0], 2, argv)

    def test_no_peers(self):
        self.assertEqual(self.cli(["list"], [])[0], 0)
        self.assertEqual(self.cli(["once"], [])[0], 1)

    def test_list_shows_configured_peers_and_the_last_cached_state_without_a_token(self):
        cfg = [self.peer("http://100.64.0.1:8787", name="a", insecure=True), self.peer("not a url", name="b", alert=True)]
        code, text = self.cli(["list"], cfg)
        self.assertEqual(code, 0)
        self.assertIn("a  http://100.64.0.1:8787  scope=summary insecure  last: no cache", text)
        self.assertIn("(invalid URL)", text)
        peers.write_cache(self.cache, [peers.new_entry(cfg[0], time.time())], time.time())
        self.assertIn("last: unknown", self.cli(["list"], cfg)[1])
        self.assertNotIn(TOKEN, text)

    def test_test_polls_once_and_says_why_in_words(self):
        p = self.fake()
        code, text = self.cli(["test", "b"], [self.peer(p.url)])
        self.assertEqual(code, 0)
        self.assertIn("b  OK", text)
        self.assertIn("host nuc-b", text)
        self.assertNotIn(TOKEN, text)
        p.mode = "silent"
        code, text = self.cli(["test", "b"], [self.peer(p.url)])
        self.assertEqual(code, 1)
        self.assertIn("UNREACHABLE", text)
        self.assertIn(peers.REASONS["timeout"], text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(self.cli(["test", "nobody"], [self.peer(p.url)])[0], 2)
        self.assertEqual(os.path.exists(self.cache), False)  # test writes nothing

    def test_once_writes_the_cache(self):
        p = self.fake()
        code, text = self.cli(["once"], [self.peer(p.url)])
        self.assertEqual(code, 0)
        self.assertEqual(peers.read_cache(self.cache)["peers"]["b"]["state"], "ok")
        self.assertNotIn(TOKEN, text)

    def test_once_says_when_it_cannot_write(self):
        blocker = os.path.join(self.dir, "file")
        open(blocker, "w").close()
        out = []
        with mock.patch.object(nuc_config, "peers_cache_dir", lambda: os.path.join(blocker, "sub")):
            code = peers.main(["once"], out=out.append, cfg={"hosts": [self.peer("http://100.64.0.1")]})
        self.assertEqual(code, 1)
        self.assertIn("cannot write the cache", "\n".join(out))

    def test_an_identity_change_shows_in_test_without_being_remembered(self):
        p = self.fake()
        known = peers.Known(self.state)
        known.ids["b"] = "e" * 16
        known.save(self.state)
        code, text = self.cli(["test", "b"], [self.peer(p.url)])
        self.assertEqual(code, 1)
        self.assertIn("host_id changed", text)


# ---- The files around it -------------------------------------------------------------------------------------------------------------
class Files(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8", newline="") as f:
            return f.read()

    def test_the_command_and_its_cmd(self):
        sh, cmd = self.read("bin/nuc-console-fleet"), self.read("bin/nuc-console-fleet.cmd")
        self.assertTrue(sh.startswith("#!/bin/sh\n"))
        self.assertIn(r"app\peers.py", cmd)
        self.assertIn("python.exe", cmd)
        if POSIX:
            self.assertTrue(os.stat(os.path.join(ROOT, "bin", "nuc-console-fleet")).st_mode & stat.S_IXUSR)
        names = ("NUC_CONSOLE_HOME", "NUC_CONSOLE_CONFIG", "NUC_CONSOLE_PEERS_CACHE", "NUC_CONSOLE_PEERS_STATE", "NUC_CONSOLE_SETTINGS",
                 "NUC_CONSOLE_NOTIFY_DIR", "NUC_CONSOLE_HOST_ID", "SSL_CERT_FILE", "SSL_CERT_DIR")
        self.assertIn("/usr/bin/python3 -E -B /opt/nuc-console/peers.py", sh)
        for n in names:  # the wrapper clears what could redirect it, like nuc-console-config
            self.assertIn("-u " + n + " ", sh)
            self.assertIn("set " + n + "=\r\n", cmd)
        self.assertIn("python.exe\" -E -B ", cmd)
        used = set(re.findall(r"NUC_CONSOLE_[A-Z_]+", self.read("src/nuc_config.py") + self.read("src/peers.py")))
        self.assertLessEqual({u for u in used if u.startswith("NUC_CONSOLE_")} - {"NUC_CONSOLE_"}, set(names))  # every variable the client reads is cleared
        self.assertIn("bin/nuc-console-fleet text eol=lf", self.read(".gitattributes"))
        self.assertNotIn(TOKEN, sh + cmd)

    def test_the_installers_and_the_lint_list_carry_it(self):
        s = self.read("install.sh")
        self.assertIn("install -m 0755 bin/nuc-console-fleet /usr/local/bin/", s)
        self.assertIn("/usr/local/bin/nuc-console-fleet", s[s.index('"--uninstall" ]'):s.index("exit 0")])
        self.assertRegex(s, r"/usr/local/bin/nuc-console-\{[^}]*fleet[^}]*\}")
        mac = self.read("install-macos.sh")
        self.assertGreaterEqual(mac.count("nuc-console-fleet"), 3)
        self.assertIn("bin/nuc-console-fleet", self.read(".github/workflows/tests.yml"))
        self.assertIn(r"bin\*.cmd", self.read("install-windows.ps1"))

    def test_the_docs_describe_it(self):
        self.assertIn("[hosts]", self.read("docs/CONFIGURATION.md"))
        self.assertIn("Fleet cache", self.read("docs/WEB.md"))
        self.assertIn("The peer client", self.read("SECURITY.md"))
        self.assertIn("nuc-console-fleet", self.read("README.md"))
        self.assertIn("nuc-console-fleet", self.read("CHANGELOG.md").split("## [2.5.2]")[0])


# ---- The findings of the independent review ----------------------------------------------------------------------------------------------
class Review(Base):
    def test_m1_an_ipv6_that_embeds_a_tailnet_address_is_https_only(self):
        for a in ("64:ff9b::6440:101", "2002:6440:101::", "2002:6440:101:1::7"):
            self.assertEqual(peers.classify(ip(a)), "public", a)  # the range of the address the socket goes to
            self.assertEqual(peers.judge("http", [peers.classify(ip(a))]), (False, "plain-http", False), a)
            self.assertEqual(peers.judge("http", [peers.classify(ip(a))], insecure=True), (False, "plain-http", False), a)
            self.assertEqual(peers.judge("https", [peers.classify(ip(a))])[0], True, a)
        for a in ("64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::", "64:ff9b::7f00:1"):
            self.assertEqual(peers.classify(ip(a)), "forbidden", a)  # what it embeds can still forbid it
        for a, kind in (("100.64.1.2", "tailnet"), ("fd7a:115c:a1e0::7", "tailnet"), ("::ffff:100.64.1.2", "tailnet"), ("::ffff:10.0.0.1", "private")):
            self.assertEqual(peers.classify(ip(a)), kind, a)
            self.assertEqual(peers.judge("http", [kind])[0], kind == "tailnet")
        self.no_connection()
        for url in ("http://[64:ff9b::6440:101]", "http://[2002:6440:101::]"):
            e = peers.poll_peer(self.peer(url), limits=FAST)
            self.assertEqual((e["state"], e["error"]), ("unreachable", "plain-http"), url)

    def test_m2_the_poller_deletes_only_what_it_made(self):
        os.makedirs(self.cache)
        spit(os.path.join(self.cache, "package.json"), "{}")
        with self.assertRaises(OSError) as c:  # a folder with files and no marker is not ours: nothing is written or removed
            peers.write_cache(self.cache, [], time.time(), keep=set())
        self.assertIn("not ours", str(c.exception))
        self.assertEqual(os.listdir(self.cache), ["package.json"])
        # a folder that is empty is taken (the service manager made it), and a marker that is not ours is refused
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty)
        peers.write_cache(empty, [], time.time(), keep=set())
        self.assertIn(peers.OWNER, os.listdir(empty))
        spit(os.path.join(empty, peers.OWNER), "something else")
        with self.assertRaises(OSError):
            peers.write_cache(empty, [], time.time(), keep=set())

    @unittest.skipUnless(POSIX, "links")
    def test_m2_a_cache_folder_that_is_a_link_is_refused(self):
        real = os.path.join(self.dir, "real")
        os.makedirs(real)
        os.symlink(real, self.cache)
        with self.assertRaises(OSError):
            peers.write_cache(self.cache, [], time.time(), keep=set())
        self.assertEqual(os.listdir(real), [])

    def test_l1_a_failing_cache_does_not_stop_the_poller_and_is_said_once(self):
        t = [0.0]
        cfg = [{"name": "p", "url": "http://100.64.0.1", "scope": "summary"}]
        poller = peers.Poller(cfg, self.cache, state=self.state, poll=lambda peer, prev, wall, **n: dict(peers.new_entry(peer, 1), state="ok"),
                              clock=lambda: t[0], jitter=lambda: 1.0)
        self.addCleanup(poller.pool.shutdown, False)
        err = io.StringIO()
        with mock.patch.object(peers, "write_cache", side_effect=OSError(28, "No space left on device /secret/path")), \
                mock.patch.object(sys, "stderr", err):
            for _ in range(3):
                poller.step()
                for f in list(poller.running.values()):
                    f.exception()
                poller.step()
                t[0] += 100
        self.assertEqual(err.getvalue().count("cannot write the cache"), 1)  # once per kind
        self.assertNotIn("/secret/path", err.getvalue())
        self.assertEqual(poller.entries["p"]["state"], "ok")  # it kept going
        stop = threading.Event()
        stop.set()
        poller.run_forever(stop)

    def test_l1_write_json_survives_an_unremovable_temporary_file(self):
        os.makedirs(self.cache)
        real = os.remove

        def remove(path):
            if path.endswith(".tmp"):
                raise PermissionError(13, "no")
            return real(path)
        with mock.patch.object(os, "remove", remove):
            peers.write_json(self.cache, "a.json", {"x": 1})
        self.assertEqual(json.loads(slurp(os.path.join(self.cache, "a.json"))), {"x": 1})

    def test_l2_a_pin_on_an_http_url_is_a_config_error(self):
        c = Config()
        c.addCleanup = self.addCleanup
        cfg, said = c.load("[hosts]\nbad = http://100.64.0.1 pin=sha256:%s\ngood = https://x.test pin=sha256:%s\n" % ("ab" * 32, "cd" * 32))
        self.assertEqual([h["name"] for h in cfg["hosts"]], ["good"])
        self.assertEqual(len(said), 1)
        self.assertIn("bad:", said[0])
        self.assertNotIn("ab" * 8, said[0])
        self.no_connection()  # and the client refuses it too if it is handed one
        e = peers.poll_peer(self.peer("http://100.64.0.1", pin="sha256:" + "ab" * 32), limits=FAST)
        self.assertEqual((e["state"], e["error"]), ("unreachable", "bad-url"))

    def test_l3_fewer_bytes_than_announced_is_an_error(self):
        p = self.fake()

        class Short(fakepeer.Handler):
            def m_normal(self):
                body = b'{"api":1,"view":"summary"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body) + 50))
                self.end_headers()
                self.wfile.write(body)
        p.server.RequestHandlerClass = Short
        e = self.poll(p)
        self.assertEqual((e["state"], e["error"]), ("unreachable", "truncated"))

    def test_l3_a_content_length_that_is_not_a_number_is_a_bad_shape(self):
        for value in ("abc", "\u00b2", "-5", "1e3", "12 34", ""):
            p = self.fake()

            class Odd(fakepeer.Handler):
                def m_normal(self, value=value):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", value)
                    self.end_headers()
                    self.wfile.write(b"{}")
            p.server.RequestHandlerClass = Odd
            e = self.poll(p)
            self.assertEqual(e["state"], "unreachable", value)
            self.assertIn(e["error"], ("bad-shape", "bad-json"), value)
            self.assertNotEqual(e["error"], "connect", value)

    def test_l4_lookups_outstanding_are_bounded(self):
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        block = lambda h, port: (stuck.wait(5), ["100.64.0.1"])[1]  # noqa: E731
        for _ in range(4):
            with self.assertRaises(peers.PeerError):
                peers._resolve("x.test", 80, block, 0.05)  # times out, the thread stays
        t = time.monotonic()
        with self.assertRaises(peers.PeerError) as c:
            peers._resolve("y.test", 80, block, 2.0)
        self.assertEqual(c.exception.reason, "resolve")  # no fifth thread: refused at once
        self.assertLess(time.monotonic() - t, 0.5)
        stuck.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                self.assertEqual(peers._resolve("z.test", 80, lambda h, port: ["100.64.0.1"], 1.0), ["100.64.0.1"])
                break
            except peers.PeerError:
                time.sleep(0.02)
        else:
            self.fail("the lookups never came back")

    def test_l5_the_trust_anchors_do_not_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/evil/ca.pem", "SSL_CERT_DIR": "/evil"}):
            peers.main(["list"], out=lambda s: None, cfg={"hosts": []})
            self.assertNotIn("SSL_CERT_FILE", os.environ)
            self.assertNotIn("SSL_CERT_DIR", os.environ)
        # and an explicit cafile still works (the tests' own)
        self.assertEqual(self.poll(self.fake(tls=True), lim=TLS, cafile=PEM)["state"], "ok")

    def test_l6_the_windows_note_is_in_the_security_page(self):
        text = slurp(os.path.join(ROOT, "SECURITY.md"))
        self.assertIn("folder ACLs", text.split("## The peer client")[1].split("\n## ")[0])

    def test_l8_more_secret_looking_keys_are_stripped_from_documents(self):
        doc = {"api": 1, "view": "cpu", "ok": 1, "Secret": 1, "dbPassword": 2, "passwd": 3, "Set-Cookie": 4, "Authorization": 5, "api_key": 6, "apiKey": 7,
               "api-key": 8, "x": {"csrfToken": 1, "client_secret": 2, "keep": 3}, "list": [{"password": 1, "n": 1}]}
        got = peers.parse_view("cpu", json.dumps(doc).encode())
        self.assertEqual(got, {"api": 1, "view": "cpu", "ok": 1, "x": {"keep": 3}, "list": [{"n": 1}]})

    def test_l7_the_known_file_lives_in_its_own_persistent_state_folder(self):
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_STATE": ""}):
            with mock.patch.object(nuc_config, "PORTABLE", "/data"), mock.patch.object(nuc_config, "BASE_DIR", "/data", create=True):
                self.assertEqual(nuc_config.peers_state_dir(), os.path.join("/data", "peers-state"))
            with mock.patch.object(nuc_config, "PORTABLE", ""), mock.patch.object(nuc_config, "WINDOWS", False):
                self.assertEqual(nuc_config.peers_state_dir(), "/var/lib/nuc-console-peers")
            with mock.patch.object(nuc_config, "PORTABLE", ""), mock.patch.object(nuc_config, "WINDOWS", True), \
                    mock.patch.object(nuc_config, "BASE_DIR", os.path.join("C:", "nc"), create=True):
                self.assertEqual(nuc_config.peers_state_dir(), os.path.join("C:", "nc", "peers-state"))
        self.assertNotEqual(nuc_config.peers_state_dir(), nuc_config.peers_cache_dir())
        p = self.fake()
        poller = peers.Poller([self.peer(p.url)], self.cache, state=self.state, allow_loopback=True, limits=FAST)
        self.addCleanup(poller.pool.shutdown, False)
        poller.run_once()
        self.assertTrue(os.path.isfile(os.path.join(self.state, "known")))
        self.assertNotIn("known", os.listdir(self.cache))  # a cache wiped at reboot no longer forgets who answered first

    def test_l7_forget_removes_one_identity_and_nothing_else(self):
        known = peers.Known(self.state)
        known.ids.update(b="a" * 16, c="c" * 16)
        known.save(self.state)
        with mock.patch.object(nuc_config, "peers_cache_dir", lambda: self.cache), mock.patch.object(nuc_config, "peers_state_dir", lambda: self.state):
            out = []
            self.assertEqual(peers.main(["forget", "b"], out=out.append, cfg={"hosts": []}), 0)
            self.assertIn("forgotten", out[0])
            self.assertEqual(peers.Known(self.state).ids, {"c": "c" * 16})
            self.assertIn("no identity", self.cli_out(["forget", "b"]))
            self.assertEqual(peers.main(["forget", "../x"], out=out.append, cfg={"hosts": []}), 2)
        # the replaced machine is accepted after the forget: the next poll remembers the new id
        p = self.fake()
        p.docs["summary"] = fakepeer.summary(host_id="e" * 16)
        known = peers.Known(self.state)
        known.ids["b"] = "a" * 16
        known.save(self.state)
        poller = peers.Poller([self.peer(p.url)], self.cache, state=self.state, allow_loopback=True, limits=FAST)
        self.addCleanup(poller.pool.shutdown, False)
        self.assertEqual(poller.run_once()[0]["state"], "untrusted")
        known = peers.Known(self.state)
        known.forget("b")
        known.save(self.state)
        again = peers.Poller([self.peer(p.url)], self.cache, state=self.state, allow_loopback=True, limits=FAST)
        self.addCleanup(again.pool.shutdown, False)
        self.assertEqual(again.run_once()[0]["state"], "ok")
        self.assertEqual(peers.Known(self.state).ids["b"], "e" * 16)

    def cli_out(self, argv):
        out = []
        peers.main(argv, out=out.append, cfg={"hosts": []})
        return "\n".join(out)

    def test_m2_main_says_the_folder_is_not_ours(self):
        os.makedirs(self.cache)
        spit(os.path.join(self.cache, "package.json"), "{}")
        out = []
        with mock.patch.object(nuc_config, "peers_cache_dir", lambda: self.cache), mock.patch.object(nuc_config, "peers_state_dir", lambda: self.state):
            code = peers.main(["once"], out=out.append, cfg={"hosts": [self.peer("http://100.64.0.1")]}, net=dict(limits=FAST))
        self.assertEqual(code, 1)
        self.assertIn("not ours", "\n".join(out))
        self.assertEqual(os.listdir(self.cache), ["package.json"])


if __name__ == "__main__":
    unittest.main()
