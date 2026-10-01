"""nuc-console-ai (src/aisetup.py): the manifest, the downloader, the cache directories, the server command lines, the config
helper, status, the service files. No network: downloads run against a local http.server serving fake files."""
import contextlib
import hashlib
import http.server
import inspect
import io
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import aisetup  # noqa: E402
import nuc_config  # noqa: E402


def blob(n, salt=b"x"):
    """n deterministic bytes."""
    return b"".join(hashlib.sha256(salt + str(i).encode()).digest() for i in range(n // 32 + 1))[:n]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def put(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def get(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


class FileHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.requests.append((self.path, self.headers.get("Range")))
        if self.path in srv.redirects:
            self.send_response(302)
            self.send_header("Location", srv.redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = srv.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, rng = 0, self.headers.get("Range")
        if rng and srv.honour_range:
            start = int(re.match(r"bytes=(\d+)-", rng).group(1))
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(body) - 1, len(body)))
        else:
            self.send_response(200)
        data = body[start:]
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if srv.cuts:  # send only a part, then drop the connection
            self.wfile.write(data[:srv.cuts.pop(0)])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(data)


class Server:
    """A local HTTP server in a thread; .files {path: bytes}, .requests [(path, Range)], .cuts [bytes to send before dropping]."""

    def __init__(self, files=None, honour_range=True, handler=FileHandler):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.files, self.httpd.requests, self.httpd.cuts = dict(files or {}), [], []
        self.httpd.honour_range, self.httpd.redirects = honour_range, {}
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.httpd

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def quiet():
    return contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())


def call(fn, *a, **kw):
    """Runs fn with stdout/stderr captured -> (result, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        r = fn(*a, **kw)
    return r, out.getvalue(), err.getvalue()


def fake_manifest(base, runtime_bytes, model_bytes, name="tiny"):
    runtime = {"name": "llamafile", "version": "0.0", "license": "Apache-2.0", "url": base + "/llamafile-0.0",
               "sha256": sha(runtime_bytes), "size": len(runtime_bytes), "args": []}
    model = {"id": name, "name": "Tiny test model", "license": "MIT", "ram_mb": 100, "repo": "test/tiny-GGUF", "file": name + ".gguf",
             "revision": "a" * 40, "sha256": sha(model_bytes), "size": len(model_bytes)}
    return runtime, model


class ManifestTests(unittest.TestCase):
    def test_models(self):
        ids = [m["id"] for m in aisetup.MODELS]
        self.assertEqual(len(ids), len(set(ids)), "ids must be unique")
        self.assertIn(aisetup.DEFAULT_MODEL, ids)
        self.assertGreaterEqual(len(ids), 3)
        for m in aisetup.MODELS:
            with self.subTest(m["id"]):
                self.assertTrue(re.fullmatch(r"[a-z0-9][a-z0-9.-]*", m["id"]))
                self.assertIn(m["license"], aisetup.ALLOWED_LICENSES)
                self.assertIsInstance(m["ram_mb"], int)
                self.assertTrue(re.fullmatch(r"[\w.-]+/[\w.-]+", m["repo"]))
                self.assertTrue(m["file"].endswith(".gguf") and "Q4_K_M" in m["file"], "Q4_K_M GGUF files only")
                self.assertNotIn("url", m, "the URL is built from repo, commit and file: nothing else can be fetched")
                bad = aisetup.missing_pins(m, True)  # either pinned completely, or not at all: never half
                self.assertTrue(bad == [] or set(bad) == {"revision", "sha256", "size"}, bad)
                url = aisetup.model_url(m)
                if bad:
                    self.assertIsNone(url)
                else:
                    self.assertRegex(url, r"^https://huggingface\.co/[\w.-]+/[\w.-]+/resolve/[0-9a-f]{40}/[^/]+\.gguf$")
                    self.assertRegex(m["sha256"], r"^[0-9a-f]{64}$")
                    self.assertGreater(m["size"], 10 ** 8)

    def test_runtime(self):
        r = aisetup.RUNTIME
        self.assertIn(r["license"], aisetup.ALLOWED_LICENSES)
        self.assertEqual(r["url"], "https://github.com/Mozilla-Ocho/llamafile/releases/download/%s/llamafile-%s" % (r["version"], r["version"]))
        bad = aisetup.missing_pins(r, False)
        self.assertTrue(bad == [] or set(bad) == {"sha256", "size"}, bad)
        self.assertTrue(all(isinstance(a, str) for a in r["args"]))

    def test_missing_pins_checks_formats(self):
        m = dict(aisetup.MODELS[0], revision="main", sha256="abc", size=5)
        self.assertEqual(len(aisetup.missing_pins(m, True)), 2)
        self.assertEqual(aisetup.missing_pins(dict(m, revision="b" * 40, sha256="c" * 64), True), [])

    def test_default_and_ram(self):
        self.assertEqual(aisetup.pick_default(None)["id"], aisetup.DEFAULT_MODEL)
        self.assertEqual(aisetup.pick_default(16000)["id"], aisetup.MODELS[0]["id"])
        self.assertLessEqual(aisetup.pick_default(4096)["ram_mb"], 4096 * 0.85)
        self.assertEqual(aisetup.pick_default(500)["ram_mb"], min(m["ram_mb"] for m in aisetup.MODELS))

    def test_shipped_endpoint_is_the_config_default(self):
        self.assertEqual(aisetup.SHIPPED_ENDPOINT, nuc_config.load("/nonexistent")["ai"]["endpoint"])

    def test_real_manifest_unpinned_refuses_to_download(self):
        if not aisetup.missing_pins(aisetup.RUNTIME, False) and not any(aisetup.missing_pins(m, True) for m in aisetup.MODELS):
            self.skipTest("everything is pinned")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(urllib_request(), "urlopen", side_effect=AssertionError("network")):
            args = aisetup.build_parser().parse_args(["setup", "--dir", d, "--yes", "--no-config"])
            rc, out, err = call(aisetup.cmd_setup, args)
            self.assertEqual(os.listdir(d), [])
        self.assertEqual(rc, 1)
        self.assertIn("not pinned", err)


def urllib_request():
    import urllib.request
    return urllib.request


class SourceRulesTests(unittest.TestCase):
    def setUp(self):
        self.src = get(os.path.join(ROOT, "src", "aisetup.py"))

    def test_no_shell_no_wide_bind(self):
        self.assertNotRegex(self.src, r"shell\s*=\s*True")
        self.assertNotIn("os.system", self.src)
        self.assertNotIn("0.0.0.0", self.src)
        self.assertNotIn("http://", re.sub(r'"http://%s:%d/v1"|SHIPPED_ENDPOINT = "http://127.0.0.1:11434/v1"|#.*|""".*?"""', "", self.src, flags=re.S)
                         .replace("http://127.0.0.1", "").replace("http://schemas.microsoft.com/windows/2004/02/mit/task", "")
                         .replace("http://www.apple.com", ""), "downloads and APIs are https only")

    def test_ascii_output(self):
        self.assertTrue(self.src.isascii(), "plain ASCII: Windows consoles")

    def test_host_is_not_a_parameter(self):
        self.assertNotIn("host", inspect.signature(aisetup.serve_argv).parameters)


class PlacesTests(unittest.TestCase):
    def test_default_dir(self):
        d = aisetup.default_dir
        self.assertEqual(d("linux", {}, 0, "/root"), "/var/lib/nuc-console/ai")
        self.assertEqual(d("linux", {}, 1000, "/home/u"), "/home/u/.local/share/nuc-console/ai")
        self.assertEqual(d("linux", {"XDG_DATA_HOME": "/data/u"}, 1000, "/home/u"), "/data/u/nuc-console/ai")
        self.assertEqual(d("darwin", {}, 0, "/var/root"), "/Library/Application Support/nuc-console/ai")
        self.assertEqual(d("darwin", {}, 501, "/Users/u"), "/Users/u/Library/Application Support/nuc-console/ai")
        self.assertEqual(d("win32", {"ProgramData": r"D:\PD"}, None, None), r"D:\PD\nuc-console\ai")
        self.assertEqual(d("win32", {}, None, None), r"C:\ProgramData\nuc-console\ai")
        for plat in ("linux", "darwin", "win32"):  # NUC_CONSOLE_HOME wins everywhere
            self.assertTrue(d(plat, {"NUC_CONSOLE_HOME": "/opt/h", "ProgramData": "X"}, 0, "/").replace("\\", "/").endswith("/opt/h/ai".replace("/opt/h", "/opt/h")))
        self.assertEqual(d("win32", {"NUC_CONSOLE_HOME": r"E:\h"}, None, None), r"E:\h\ai")

    def test_runtime_and_model_paths(self):
        self.assertEqual(aisetup.runtime_path("/c", plat="linux"), "/c/runtime/llamafile-%s" % aisetup.RUNTIME["version"])
        self.assertEqual(aisetup.runtime_path("/c", plat="darwin"), "/c/runtime/llamafile-%s" % aisetup.RUNTIME["version"])
        self.assertEqual(aisetup.runtime_path(r"C:\c", plat="win32"), r"C:\c\runtime\llamafile-%s.exe" % aisetup.RUNTIME["version"])
        m = aisetup.MODELS[0]
        self.assertEqual(aisetup.model_path("/c", m, "linux"), "/c/models/" + m["file"])

    @unittest.skipUnless(os.name == "posix", "Unix permissions")
    def test_ensure_dirs_modes(self):
        with tempfile.TemporaryDirectory() as t:
            os.chmod(t, 0o700)  # an existing directory (a home) is left alone
            old = os.umask(0o077)
            try:
                aisetup.ensure_dirs(os.path.join(t, "a", "b", "ai"))
            finally:
                os.umask(old)
            self.assertEqual(os.stat(t).st_mode & 0o777, 0o700)
            for sub in ("a", "a/b", "a/b/ai", "a/b/ai/runtime", "a/b/ai/models"):
                self.assertEqual(os.stat(os.path.join(t, sub)).st_mode & 0o777, 0o755, sub)
            aisetup.ensure_dirs(os.path.join(t, "a", "b", "ai"))  # again: no error

    def test_threads_leave_cores_free(self):
        self.assertEqual(aisetup.default_threads(1), 1)
        self.assertEqual(aisetup.default_threads(2), 1)
        self.assertEqual(aisetup.default_threads(4), 2)
        self.assertEqual(aisetup.default_threads(16), 14)

    def test_parse_meminfo(self):
        m = aisetup.parse_meminfo("MemTotal:       16487476 kB\nMemFree:  1 kB\nMemAvailable:   15000000 kB\nHugePages_Total:       0\n")
        self.assertEqual(m["MemTotal"], 16487476 * 1024)
        self.assertEqual(m["MemAvailable"], 15000000 * 1024)


class ServeCommandTests(unittest.TestCase):
    def check_common(self, argv):
        self.assertTrue(all(isinstance(a, str) for a in argv), "an argument list of strings: no shell involved")
        i = argv.index("--host")
        self.assertEqual(argv[i + 1], "127.0.0.1")
        self.assertEqual(argv.count("--host"), 1)
        self.assertEqual(argv[argv.index("--port") + 1], "8080")
        self.assertIn("--server", argv)
        self.assertIn("--nobrowser", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "2")
        self.assertEqual(argv[argv.index("-c") + 1], str(aisetup.DEFAULT_CTX))
        self.assertEqual(argv[argv.index("-a") + 1], "qwen3-4b")
        self.assertTrue(argv[argv.index("-m") + 1].endswith(aisetup.MODELS[0]["file"]))
        for a in aisetup.RUNTIME["args"]:
            self.assertIn(a, argv)

    def test_linux_and_mac_go_through_sh(self):
        for plat, d in (("linux", "/var/lib/nuc-console/ai"), ("darwin", "/Library/Application Support/nuc-console/ai")):
            with self.subTest(plat):
                argv = aisetup.serve_argv(d, aisetup.MODELS[0], 8080, 2, plat=plat)
                self.assertEqual(argv[0], "/bin/sh")
                self.assertEqual(argv[1], d + "/runtime/llamafile-" + aisetup.RUNTIME["version"])
                self.check_common(argv)

    def test_windows_runs_the_exe(self):
        argv = aisetup.serve_argv(r"C:\ProgramData\nuc-console\ai", aisetup.MODELS[0], 8080, 2, plat="win32")
        self.assertTrue(argv[0].endswith(".exe"), argv[0])
        self.assertNotIn("/bin/sh", argv)
        self.assertEqual(argv[0], r"C:\ProgramData\nuc-console\ai\runtime\llamafile-%s.exe" % aisetup.RUNTIME["version"])
        self.check_common(argv)

    def test_port_and_threads_are_numbers_in_the_command(self):
        argv = aisetup.serve_argv("/c", aisetup.MODELS[0], "9090", "3", 8192, plat="linux")
        self.assertEqual(argv[argv.index("--port") + 1], "9090")
        self.assertEqual(argv[argv.index("-t") + 1], "3")
        self.assertEqual(argv[argv.index("-c") + 1], "8192")
        with self.assertRaises(ValueError):
            aisetup.serve_argv("/c", aisetup.MODELS[0], "8080; reboot", 2, plat="linux")

    def test_unix_exec_with_nice(self):
        with mock.patch.object(aisetup, "lower_priority") as low, mock.patch.object(os, "execv") as ex, \
                mock.patch.object(aisetup, "_is_win", return_value=False):
            aisetup.run_server(["/bin/sh", "/x/llamafile", "--server"])
        low.assert_called_once_with()
        ex.assert_called_once_with("/bin/sh", ["/bin/sh", "/x/llamafile", "--server"])

    @unittest.skipIf(sys.platform == "win32", "Unix priorities")
    def test_lower_priority_sets_nice_10_not_more(self):
        calls = []
        with mock.patch.object(os, "getpriority", return_value=0), mock.patch.object(os, "setpriority", side_effect=lambda *a: calls.append(a)):
            aisetup.lower_priority()
        self.assertEqual(calls, [(os.PRIO_PROCESS, 0, 10)])
        calls.clear()
        with mock.patch.object(os, "getpriority", return_value=10), mock.patch.object(os, "setpriority", side_effect=lambda *a: calls.append(a)):
            aisetup.lower_priority()
        self.assertEqual(calls, [], "already nice 10 (systemd Nice=): not 20")

    def test_windows_runs_below_normal(self):
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch.object(aisetup, "_is_win", return_value=True), mock.patch.object(subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, create=True), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            self.assertEqual(aisetup.run_server([r"C:\x\llamafile.exe", "--server"]), 0)
        self.assertTrue(popen.call_args[1]["creationflags"] & 0x4000)
        self.assertEqual(popen.call_args[0][0], [r"C:\x\llamafile.exe", "--server"])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = blob(300_000)
        self.dest = os.path.join(self.tmp.name, "models", "m.gguf")

    def dl(self, srv, path="/m.gguf", data=None, **kw):
        data = self.data if data is None else data
        kw.setdefault("backoff", 0)
        return aisetup.download(srv_base(srv) + path, self.dest, sha(data), len(data), allow_loopback_http=True, **kw)

    def test_download_verifies_and_renames(self):
        with Server({"/m.gguf": self.data}) as srv:
            self.assertEqual(self.dl(srv, mode=0o644), "downloaded")
        self.assertEqual(get(self.dest, "rb"), self.data)
        self.assertFalse(os.path.exists(self.dest + ".part"))
        if os.name == "posix":
            self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o644)

    def test_executable_mode(self):
        with Server({"/m.gguf": self.data}) as srv:
            self.dl(srv, mode=0o755)
        if os.name == "posix":
            self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o755)

    def test_present_file_with_right_hash_is_not_downloaded_again(self):
        os.makedirs(os.path.dirname(self.dest))
        put(self.dest, self.data)
        with Server({"/m.gguf": self.data}) as srv:
            self.assertEqual(self.dl(srv), "cached")
            self.assertEqual(self.dl(srv), "cached")
            self.assertEqual(srv.requests, [])

    def test_present_file_with_wrong_content_is_replaced(self):
        os.makedirs(os.path.dirname(self.dest))
        put(self.dest, b"\0" * len(self.data))
        with Server({"/m.gguf": self.data}) as srv:
            self.assertEqual(self.dl(srv), "downloaded")
        self.assertEqual(get(self.dest, "rb"), self.data)

    def test_resume_from_partial_file(self):
        os.makedirs(os.path.dirname(self.dest))
        put(self.dest + ".part", self.data[:100_000])
        with Server({"/m.gguf": self.data}) as srv:
            self.assertEqual(self.dl(srv), "downloaded")
            self.assertEqual(srv.requests, [("/m.gguf", "bytes=100000-")], "only the missing part is requested")
        self.assertEqual(get(self.dest, "rb"), self.data)

    def test_resume_after_a_dropped_connection(self):
        with Server({"/m.gguf": self.data}) as srv:
            srv.cuts = [120_000]
            self.assertEqual(self.dl(srv), "downloaded")
            self.assertEqual([r for _p, r in srv.requests], [None, "bytes=120000-"])
        self.assertEqual(get(self.dest, "rb"), self.data)

    def test_server_without_range_support_restarts_cleanly(self):
        os.makedirs(os.path.dirname(self.dest))
        put(self.dest + ".part", self.data[:50_000])
        with Server({"/m.gguf": self.data}, honour_range=False) as srv:
            self.assertEqual(self.dl(srv), "downloaded")
        self.assertEqual(get(self.dest, "rb"), self.data, "no duplicated or missing bytes")

    def test_oversized_partial_file_is_discarded(self):
        os.makedirs(os.path.dirname(self.dest))
        put(self.dest + ".part", self.data + b"extra")
        with Server({"/m.gguf": self.data}) as srv:
            self.assertEqual(self.dl(srv), "downloaded")
        self.assertEqual(get(self.dest, "rb"), self.data)

    def test_wrong_hash_is_refused_and_deleted(self):
        evil = bytes([self.data[0] ^ 1]) + self.data[1:]  # same size, one bit different
        with Server({"/m.gguf": evil}) as srv:
            with self.assertRaises(aisetup.SetupError) as cm:
                self.dl(srv)
        self.assertIn("SHA-256", str(cm.exception))
        self.assertFalse(os.path.exists(self.dest), "nothing installed")
        self.assertFalse(os.path.exists(self.dest + ".part"), "the bad download is deleted")

    def test_wrong_hash_does_not_touch_an_existing_good_file(self):
        os.makedirs(os.path.dirname(self.dest))
        put(self.dest, self.data)
        evil = blob(len(self.data), b"evil")
        with Server({"/m.gguf": evil}) as srv:
            # a different pin: the good file does not match it, the download does not either
            with self.assertRaises(aisetup.SetupError):
                aisetup.download(srv_base(srv) + "/m.gguf", self.dest, sha(b"other"), len(self.data), allow_loopback_http=True, backoff=0)
        self.assertEqual(get(self.dest, "rb"), self.data)

    def test_more_bytes_than_pinned_is_refused(self):
        with Server({"/m.gguf": self.data + b"tail"}) as srv:
            with self.assertRaises(aisetup.SetupError):
                self.dl(srv)
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_http_404_is_not_retried(self):
        with Server({}) as srv:
            with self.assertRaises(aisetup.SetupError) as cm:
                self.dl(srv, retries=3)
            self.assertEqual(len(srv.requests), 1)
        self.assertIn("404", str(cm.exception))

    def test_unreachable_server_keeps_resumable_state_and_says_so(self):
        with self.assertRaises(aisetup.SetupError) as cm:
            aisetup.download("http://127.0.0.1:%d/m.gguf" % free_port(), self.dest, sha(self.data), len(self.data),
                             allow_loopback_http=True, retries=1, backoff=0)
        self.assertIn("incomplete", str(cm.exception))

    def test_unpinned_values_refuse(self):
        for h, n in ((None, 5), ("a" * 64, None), ("zz", 5)):
            with self.assertRaises(aisetup.SetupError):
                aisetup.download("https://example.com/x", self.dest, h, n)

    def test_https_only(self):
        for url in ("http://example.com/m.gguf", "ftp://example.com/m", "file:///etc/passwd", "//example.com/m", "http://127.0.0.1/m"):
            with self.subTest(url), self.assertRaises(aisetup.SetupError):
                aisetup.check_url(url)  # plain http is refused even for loopback in production
        aisetup.check_url("https://huggingface.co/a/b/resolve/" + "a" * 40 + "/f.gguf")
        aisetup.check_url("http://127.0.0.1:1/x", allow_loopback_http=True)
        with self.assertRaises(aisetup.SetupError):
            aisetup.check_url("http://203.0.113.5/x", allow_loopback_http=True)

    def test_redirect_to_plain_http_is_refused(self):
        with Server({}) as srv:
            srv.redirects["/m.gguf"] = "http://203.0.113.5/m.gguf"
            with self.assertRaises(aisetup.SetupError) as cm:
                self.dl(srv)
        self.assertIn("HTTPS", str(cm.exception))
        self.assertFalse(os.path.exists(self.dest))

    def test_redirect_to_the_same_kind_of_url_is_followed_with_range(self):
        os.makedirs(os.path.dirname(self.dest))
        put(self.dest + ".part", self.data[:1000])
        with Server({"/cdn/m.gguf": self.data}) as srv:
            srv.redirects["/m.gguf"] = srv_base(srv) + "/cdn/m.gguf"
            self.assertEqual(self.dl(srv), "downloaded")
            self.assertEqual(srv.requests[-1], ("/cdn/m.gguf", "bytes=1000-"), "Range survives the redirect")


def srv_base(httpd):
    return "http://127.0.0.1:%d" % httpd.server_address[1]


class StampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        self.bytes = blob(5000)
        self.runtime, self.model = fake_manifest("http://127.0.0.1:1", b"r", self.bytes)
        self.path = aisetup.model_path(self.d, self.model)
        os.makedirs(os.path.dirname(self.path))
        put(self.path, self.bytes)

    def test_verified_by_stamp_and_by_hash(self):
        self.assertFalse(aisetup.is_verified(self.d, self.path, self.model), "no stamp yet")
        self.assertTrue(aisetup.is_verified(self.d, self.path, self.model, rehash=True))
        aisetup.record(self.d, self.path, self.model["sha256"])
        self.assertTrue(aisetup.is_verified(self.d, self.path, self.model))

    def test_changed_file_loses_its_stamp(self):
        aisetup.record(self.d, self.path, self.model["sha256"])
        with open(self.path, "r+b") as f:
            f.write(b"X")
        os.utime(self.path, ns=(1, 1))
        self.assertFalse(aisetup.is_verified(self.d, self.path, self.model))
        self.assertFalse(aisetup.is_verified(self.d, self.path, self.model, rehash=True))

    def test_unpinned_entry_is_never_verified(self):
        unpinned = dict(self.model, sha256=None, revision=None, size=None)
        aisetup.record(self.d, self.path, self.model["sha256"])
        self.assertFalse(aisetup.is_verified(self.d, self.path, unpinned))

    def test_broken_stamp_file_is_just_empty(self):
        put(aisetup.stamp_path(self.d), "{not json")
        self.assertEqual(aisetup.load_stamp(self.d), {})


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")
        self.cfg = os.path.join(self.tmp.name, "config.ini")
        self.rt_bytes, self.m_bytes = blob(20_000, b"rt"), blob(200_000, b"model")
        patcher = mock.patch.object(aisetup, "memory_mb", return_value=(16000, 12000))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_setup(self, srv, *extra, runtime=None, model=None, ask=None):
        runtime, model = fake_manifest(srv_base(srv), self.rt_bytes, self.m_bytes) if runtime is None else (runtime, model)
        args = aisetup.build_parser().parse_args(["setup", "--dir", self.d, "--config", self.cfg, "--port", "18080", "--model", "tiny"] + list(extra))
        with mock.patch.object(aisetup, "model_url", lambda m: srv_base(srv) + "/" + m["file"]):
            kw = {"ask": ask} if ask else {}
            return call(aisetup.cmd_setup, args, runtime=runtime, models=[model], allow_loopback_http=True, **kw)

    def files(self, srv):
        return {"/llamafile-0.0": self.rt_bytes, "/tiny.gguf": self.m_bytes}

    def test_setup_downloads_once_and_never_again(self):
        put(self.cfg, "# my settings\n[features]\nhealth = yes\n\n[ai]\n# local model\nenabled = no\nendpoint = http://127.0.0.1:11434/v1\nmodel =\n")
        with Server(self.files(None)) as srv:
            rc, out, err = self.run_setup(srv, "--yes")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(len(srv.requests), 2)
            runtime_file = aisetup.runtime_path(self.d, {"url": srv_base(srv) + "/llamafile-0.0"})
            self.assertEqual(get(runtime_file, "rb"), self.rt_bytes)
            self.assertEqual(get(os.path.join(self.d, "models", "tiny.gguf"), "rb"), self.m_bytes)
            if os.name == "posix":
                self.assertEqual(os.stat(runtime_file).st_mode & 0o777, 0o755)
                self.assertEqual(os.stat(os.path.join(self.d, "models", "tiny.gguf")).st_mode & 0o777, 0o644)
            self.assertEqual(sorted(aisetup.load_stamp(self.d)), ["llamafile-0.0", "tiny.gguf"])
            rc, out, err = self.run_setup(srv, "--yes")  # second run
            self.assertEqual(rc, 0)
            self.assertEqual(len(srv.requests), 2, "no request at all the second time")
            self.assertIn("not downloaded again", out)
        cfg = get(self.cfg)
        self.assertIn("# my settings", cfg)
        self.assertIn("# local model", cfg)
        self.assertIn("endpoint = http://127.0.0.1:18080/v1", cfg)
        self.assertIn("model = tiny", cfg)
        self.assertIn("enabled = no", cfg, "the advisor is not switched on behind the admin's back")
        self.assertEqual(nuc_config.load(self.cfg)["features"]["health"], True)

    def test_setup_shows_the_choice(self):
        with Server(self.files(None)) as srv:
            rc, out, err = self.run_setup(srv, "--yes", "--no-config")
        self.assertIn("tiny", out)
        self.assertIn("MIT", out)
        self.assertIn("llamafile 0.0", out)
        self.assertFalse(os.path.exists(self.cfg), "--no-config")

    def test_without_consent_nothing_is_downloaded(self):
        with Server(self.files(None)) as srv, mock.patch.object(sys, "stdin", io.StringIO("")):
            rc, out, err = self.run_setup(srv)  # no --yes, stdin is not a terminal
            self.assertEqual(rc, 1)
            self.assertEqual(srv.requests, [])
        self.assertIn("--yes", err)
        self.assertFalse(os.path.exists(os.path.join(self.d, "models", "tiny.gguf")))

    def test_unpinned_manifest_refuses_and_lists_what_is_missing(self):
        with Server(self.files(None)) as srv:
            runtime, model = fake_manifest(srv_base(srv), self.rt_bytes, self.m_bytes)
            runtime["sha256"], model["revision"], model["sha256"] = None, None, None
            rc, out, err = self.run_setup(srv, "--yes", runtime=runtime, model=model)
            self.assertEqual(rc, 1)
            self.assertEqual(srv.requests, [])
        self.assertIn("runtime: not pinned: sha256", err)
        self.assertIn("tiny: not pinned: revision, sha256", err)
        self.assertFalse(os.path.exists(self.d), "not even the directory")

    def test_corrupt_download_installs_nothing(self):
        with Server({"/llamafile-0.0": self.rt_bytes, "/tiny.gguf": bytes(len(self.m_bytes))}) as srv:
            with self.assertRaises(aisetup.SetupError) as cm:
                self.run_setup(srv, "--yes")
        self.assertIn("SHA-256", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.d, "models", "tiny.gguf")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "models", "tiny.gguf.part")))

    def test_not_enough_disk_space(self):
        with Server(self.files(None)) as srv, mock.patch.object(aisetup, "free_bytes", return_value=1000):
            runtime, model = fake_manifest(srv_base(srv), self.rt_bytes, self.m_bytes)
            args = aisetup.build_parser().parse_args(["setup", "--dir", self.d, "--model", "tiny", "--yes"])
            with self.assertRaises(aisetup.SetupError) as cm:
                call(aisetup.cmd_setup, args, runtime=runtime, models=[model], allow_loopback_http=True)
            self.assertEqual(srv.requests, [])
        self.assertIn("disk space", str(cm.exception))

    def test_unknown_model(self):
        args = aisetup.build_parser().parse_args(["setup", "--dir", self.d, "--model", "gpt-9"])
        with self.assertRaises(aisetup.SetupError) as cm:
            call(aisetup.cmd_setup, args)
        self.assertIn("qwen3-4b", str(cm.exception))

    def test_interrupted_download_resumes_in_the_next_run(self):
        with Server(self.files(None)) as srv:
            srv.cuts = [20_000] * 10  # the runtime fits in one cut; the connection then dies again and again on the model: this run gives up
            runtime, model = fake_manifest(srv_base(srv), self.rt_bytes, self.m_bytes)
            with mock.patch.object(aisetup, "model_url", lambda m: srv_base(srv) + "/" + m["file"]), \
                    mock.patch.object(aisetup.time, "sleep"):
                args = aisetup.build_parser().parse_args(["setup", "--dir", self.d, "--model", "tiny", "--yes", "--no-config"])
                with self.assertRaises(aisetup.SetupError):
                    call(aisetup.cmd_setup, args, runtime=runtime, models=[model], allow_loopback_http=True)
            part = os.path.join(self.d, "models", "tiny.gguf.part")
            self.assertGreater(os.path.getsize(part), 0, "what arrived is kept")
            srv.cuts = []
            rc, out, err = self.run_setup(srv, "--yes", "--no-config")
            self.assertEqual(rc, 0, err)
            self.assertTrue(any(r and r.startswith("bytes=") for _p, r in srv.requests), "resumed with Range")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = os.path.join(self.tmp.name, "config.ini")

    def test_write_keeps_everything_else(self):
        text = "# top\n[dashboard]\nmode = rotate   # keep\n\n[ai]\n# comment\nenabled = yes\nendpoint = http://127.0.0.1:11434/v1\nmodel =\ntimeout_s = 60\n\n[web]\nenabled = no\n"
        put(self.cfg, text)
        aisetup.write_config(self.cfg, "http://127.0.0.1:8080/v1", "qwen3-4b")
        new = get(self.cfg)
        self.assertEqual(new, text.replace("endpoint = http://127.0.0.1:11434/v1", "endpoint = http://127.0.0.1:8080/v1").replace("model =\n", "model = qwen3-4b\n"))
        ai = nuc_config.load(self.cfg)["ai"]
        self.assertEqual((ai["endpoint"], ai["model"], ai["enabled"], ai["timeout_s"]), ("http://127.0.0.1:8080/v1", "qwen3-4b", True, 60))

    def test_write_creates_missing_section_and_file(self):
        aisetup.write_config(self.cfg, "http://127.0.0.1:8080/v1", "m")
        ai = nuc_config.load(self.cfg)["ai"]
        self.assertEqual((ai["endpoint"], ai["model"]), ("http://127.0.0.1:8080/v1", "m"))

    def test_yes_does_not_replace_a_server_the_admin_chose(self):
        text = "[ai]\nenabled = yes\nendpoint = http://127.0.0.1:11500/v1\nmodel = llama3\n"
        put(self.cfg, text)
        _r, out, _e = call(aisetup.offer_config, self.cfg, "http://127.0.0.1:8080/v1", "qwen3-4b", True)
        self.assertEqual(get(self.cfg), text)
        self.assertIn("left as it is", out)

    def test_prompt_can_replace_and_can_decline(self):
        text = "[ai]\nenabled = yes\nendpoint = http://127.0.0.1:11500/v1\nmodel = llama3\n"
        put(self.cfg, text)
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch.object(sys, "stdin", tty):
            call(aisetup.offer_config, self.cfg, "http://127.0.0.1:8080/v1", "qwen3-4b", False, lambda q: "n")
            self.assertEqual(get(self.cfg), text)
            _r, out, _e = call(aisetup.offer_config, self.cfg, "http://127.0.0.1:8080/v1", "qwen3-4b", False, lambda q: "y")
        self.assertIn("model = qwen3-4b", get(self.cfg))
        self.assertIn("updated", out)

    def test_unwritable_config_prints_the_lines(self):
        _r, out, _e = call(aisetup.offer_config, os.path.join(self.tmp.name, "no", "such", "dir", "config.ini"), "http://127.0.0.1:8080/v1", "m", True)
        self.assertIn("endpoint = http://127.0.0.1:8080/v1", out)
        self.assertIn("Set by hand", out)

    def test_already_configured_is_quiet(self):
        aisetup.write_config(self.cfg, "http://127.0.0.1:8080/v1", "m")
        before = get(self.cfg)
        _r, out, _e = call(aisetup.offer_config, self.cfg, "http://127.0.0.1:8080/v1", "m", True)
        self.assertEqual(get(self.cfg), before)
        self.assertIn("already has", out)


class ModelsHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        code, body = self.server.answer
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")
        self.cfg = os.path.join(self.tmp.name, "config.ini")
        self.runtime, self.model = fake_manifest("http://127.0.0.1:1", blob(1000, b"r"), blob(5000, b"m"))

    def install(self, stamp=True):
        aisetup.ensure_dirs(self.d)
        for path, entry, content in ((aisetup.runtime_path(self.d, self.runtime), self.runtime, blob(1000, b"r")),
                                     (aisetup.model_path(self.d, self.model), self.model, blob(5000, b"m"))):
            put(path, content)
            if stamp:
                aisetup.record(self.d, path, entry["sha256"])

    def status(self, endpoint, *extra, ini=None):
        put(self.cfg, ini if ini is not None else "[ai]\nenabled = yes\nendpoint = %s\nmodel = tiny\n" % endpoint)
        args = aisetup.build_parser().parse_args(["status", "--dir", self.d, "--config", self.cfg] + list(extra))
        return call(aisetup.cmd_status, args, runtime=self.runtime, models=[self.model])

    def models_server(self, answer):
        s = Server(handler=ModelsHandler)
        httpd = s.__enter__()
        httpd.answer = answer
        self.addCleanup(s.__exit__)
        return s, httpd

    def test_ready(self):
        self.install()
        s, httpd = self.models_server((200, json.dumps({"object": "list", "data": [{"id": "tiny", "object": "model"}]}).encode()))
        rc, out, err = self.status(s.base + "/v1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(httpd.requests, ["/v1/models"])
        self.assertIn("installed, SHA-256 verified", out)
        self.assertIn("answering, /v1/models lists: tiny", out)
        self.assertIn("model = tiny", out)

    def test_installed_but_server_down(self):
        self.install()
        rc, out, err = self.status("http://127.0.0.1:%d/v1" % free_port())
        self.assertEqual(rc, 3)
        self.assertIn("not answering", out)
        self.assertIn("nuc-console-ai serve", out)

    def test_nothing_installed_and_server_down(self):
        rc, out, err = self.status("http://127.0.0.1:%d/v1" % free_port())
        self.assertEqual(rc, 1)
        self.assertIn("not installed", out)
        self.assertIn("nuc-console-ai setup", out)

    def test_external_server_is_fine_without_our_files(self):
        s, _h = self.models_server((200, b'{"data": [{"id": "tiny"}]}'))
        rc, out, err = self.status(s.base + "/v1")
        self.assertEqual(rc, 0)
        self.assertIn("not installed", out)

    def test_configured_model_missing_from_the_server(self):
        s, _h = self.models_server((200, b'{"data": [{"id": "other"}]}'))
        rc, out, err = self.status(s.base + "/v1")
        self.assertEqual(rc, 3)
        self.assertIn("model = tiny is not in that list", out)

    def test_bad_answers(self):
        for answer, why in (((200, b"<html>"), "not JSON"), ((200, b'{"models": []}'), "no model list"), ((500, b"{}"), "HTTP 500"),
                            ((200, b"[1, 2]"), "no model list")):
            with self.subTest(why):
                s, _h = self.models_server(answer)
                rc, out, err = self.status(s.base + "/v1")
                self.assertEqual(rc, 1)
                self.assertIn(why, out)

    def test_server_text_is_sanitised(self):
        s, _h = self.models_server((200, json.dumps({"data": [{"id": "evil\x1b[31m\x07name"}]}).encode()))
        rc, out, err = self.status(s.base + "/v1", ini="[ai]\nendpoint = %s/v1\n" % s.base)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("evil?[31m?name", out)

    def test_remote_endpoint_is_not_even_probed_without_allow_remote(self):
        self.install()
        with mock.patch.object(aisetup, "probe", side_effect=AssertionError("must not connect")):
            rc, out, err = self.status("http://192.0.2.7:8080/v1")
        self.assertEqual(rc, 3)
        self.assertIn("allow_remote = no", out)

    def test_endpoint_option_probes_another_url(self):
        s, httpd = self.models_server((200, b'{"data": [{"id": "tiny"}]}'))
        rc, out, err = self.status("http://127.0.0.1:1/v1", "--endpoint", s.base + "/v1")
        self.assertEqual(rc, 0)
        self.assertEqual(httpd.requests, ["/v1/models"])

    def test_file_without_stamp_or_changed_is_not_verified(self):
        self.install(stamp=False)
        rc, out, err = self.status("http://127.0.0.1:%d/v1" % free_port())
        self.assertIn("NOT verified", out)
        self.assertEqual(rc, 1, "files that were never verified do not count as installed")
        rc, out, err = self.status("http://127.0.0.1:%d/v1" % free_port(), "--verify")
        self.assertIn("installed, SHA-256 verified", out, "--verify hashes the files itself")
        self.assertEqual(rc, 3)

    def test_unpinned_entries_are_reported_as_such(self):
        rt, md = dict(self.runtime, sha256=None, size=None), dict(self.model, revision=None, sha256=None, size=None)
        put(self.cfg, "")
        args = aisetup.build_parser().parse_args(["status", "--dir", self.d, "--config", self.cfg, "--endpoint", "http://127.0.0.1:%d/v1" % free_port()])
        rc, out, err = call(aisetup.cmd_status, args, runtime=rt, models=[md])
        self.assertEqual(rc, 1)
        self.assertIn("not pinned in this build", out)

    def test_loopback_probe_ignores_proxy_variables(self):
        s, httpd = self.models_server((200, b'{"data": []}'))
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}):
            ok, ids, why = aisetup.probe(s.base + "/v1")
        self.assertTrue(ok, why)
        self.assertEqual(ids, [])

    def test_probe_refuses_other_schemes(self):
        ok, ids, why = aisetup.probe("file:///etc/passwd")
        self.assertFalse(ok)


class ServeCommandFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")
        self.runtime, self.model = fake_manifest("http://127.0.0.1:1", blob(1000, b"r"), blob(5000, b"m"))
        self.cfg = os.path.join(self.tmp.name, "config.ini")

    def install(self):
        aisetup.ensure_dirs(self.d)
        for path, entry, content in ((aisetup.runtime_path(self.d, self.runtime), self.runtime, blob(1000, b"r")),
                                     (aisetup.model_path(self.d, self.model), self.model, blob(5000, b"m"))):
            put(path, content)
            aisetup.record(self.d, path, entry["sha256"])

    def parse(self, *extra):
        return aisetup.build_parser().parse_args(["serve", "--dir", self.d, "--config", self.cfg] + list(extra))

    def test_serve_refuses_when_nothing_is_installed(self):
        with self.assertRaises(aisetup.SetupError) as cm:
            call(aisetup.cmd_serve, self.parse(), runtime=self.runtime, models=[self.model])
        self.assertIn("setup", str(cm.exception))

    def test_dry_run_prints_the_command(self):
        self.install()
        rc, out, err = call(aisetup.cmd_serve, self.parse("--dry-run", "--port", "9191", "--threads", "3"), runtime=self.runtime, models=[self.model])
        self.assertEqual(rc, 0)
        words = out.split() if os.name == "posix" else out.replace('"', "").split()
        self.assertIn("127.0.0.1", words)
        self.assertEqual(words[words.index("--port") + 1], "9191")
        self.assertEqual(words[words.index("-t") + 1], "3")
        self.assertNotIn("0.0.0.0", out)

    def test_serve_starts_the_server_with_the_command(self):
        self.install()
        with mock.patch.object(aisetup, "run_server", return_value=0) as rs:
            rc, out, err = call(aisetup.cmd_serve, self.parse("--port", "9191"), runtime=self.runtime, models=[self.model])
        self.assertEqual(rc, 0)
        argv = rs.call_args[0][0]
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertIn("http://127.0.0.1:9191/v1", out)

    def test_configured_model_is_preferred_among_installed(self):
        self.install()
        put(self.cfg, "[ai]\nmodel = tiny\n")
        m = aisetup.choose_model(self.parse(), self.d, [self.model], self.runtime, "tiny")
        self.assertEqual(m["id"], "tiny")

    def test_port_must_be_a_port(self):
        for bad in ("0", "70000", "http", "8080; reboot"):
            with self.subTest(bad), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.parse("--port", bad)


class ServiceTemplateTests(unittest.TestCase):
    def test_systemd_unit(self):
        argv = ["/usr/bin/python3", "-B", "/opt/nuc-console/aisetup.py", "serve", "--dir", "/var/lib/nuc-console/ai", "--model", "qwen3-4b", "--port", "8080"]
        unit = aisetup.systemd_unit(argv, 3600)
        self.assertIn("ExecStart=/usr/bin/python3 -B /opt/nuc-console/aisetup.py serve --dir /var/lib/nuc-console/ai --model qwen3-4b --port 8080\n", unit)
        for line in ("User=nuc-console-ai", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                     "CapabilityBoundingSet=", "Nice=10", "StateDirectory=nuc-console-ai", "WantedBy=multi-user.target",
                     "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", "MemoryMax=5400M"):
            self.assertIn(line + "\n", unit)
        self.assertNotIn("User=root", unit)
        import configparser
        cp = configparser.RawConfigParser(strict=False)
        cp.optionxform = str
        cp.read_string(unit)
        self.assertEqual(sorted(cp.sections()), ["Install", "Service", "Unit"])

    def test_systemd_quoting(self):
        self.assertEqual(aisetup.systemd_quote("/var/lib/a-b_c.d"), "/var/lib/a-b_c.d")
        self.assertEqual(aisetup.systemd_quote("/my dir/100%/$HOME"), '"/my dir/100%%/$$HOME"')
        self.assertEqual(aisetup.systemd_quote('a"b'), '"a\\"b"')
        unit = aisetup.systemd_unit(["/usr/bin/python3", "x y", "--dir", "/a b/c"])
        self.assertIn('ExecStart=/usr/bin/python3 "x y" --dir "/a b/c"\n', unit)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not installed")
    def test_systemd_analyze_accepts_the_unit(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nuc-console-ai.service")
            put(p, aisetup.systemd_unit(["/bin/true", "serve"], 2000))
            r = subprocess.run(["systemd-analyze", "verify", "--man=no", p], capture_output=True, text=True)
        bad = [ln for ln in (r.stdout + r.stderr).splitlines() if re.search(r"Unknown (key|section)|Invalid|Failed to parse|Unknown lvalue", ln)]
        self.assertEqual(bad, [])

    def test_launchd_plist(self):
        p = plistlib.loads(aisetup.launchd_plist(["/usr/bin/python3", "-B", "/opt/nuc-console/aisetup.py", "serve"], "/var/db/nuc-console-ai", "/var/log/nuc-console/ai.log"))
        self.assertEqual(p["Label"], "com.nuc-console.ai")
        self.assertEqual(p["UserName"], "_nuc-console-ai")
        self.assertEqual(p["Nice"], 10)
        self.assertEqual(p["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(p["ProgramArguments"][-1], "serve")
        self.assertEqual(p["EnvironmentVariables"]["HOME"], "/var/db/nuc-console-ai")

    def test_task_xml(self):
        cmd = r"C:\Program Files\nuc-console\python\pythonw.exe"
        args = r'-B "C:\Program Files\nuc-console\app\aisetup.py" serve --dir "C:\ProgramData\a & b\ai"'
        xml = aisetup.task_xml(cmd, args)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "task.xml")
            with open(p, "w", encoding="utf-16") as f:  # the encoding the Task Scheduler expects
                f.write(xml)
            root = ET.parse(p).getroot()
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find("t:Principals/t:Principal/t:UserId", ns).text, "S-1-5-19", "LOCAL SERVICE")
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", ns).text, "LeastPrivilege")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Command", ns).text, cmd)
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", ns).text, args, "& is escaped and read back unchanged")
        self.assertEqual(root.find("t:Settings/t:Priority", ns).text, "7", "below normal")
        self.assertIsNotNone(root.find("t:Triggers/t:BootTrigger", ns))

    def test_service_argv_is_explicit(self):
        m = aisetup.MODELS[0]
        argv = aisetup.service_argv("/var/lib/nuc-console/ai", m, 8080, 2, 4096, "linux", python="/usr/bin/python3", script="/opt/nuc-console/aisetup.py")
        self.assertEqual(argv, ["/usr/bin/python3", "-B", "/opt/nuc-console/aisetup.py", "serve", "--dir", "/var/lib/nuc-console/ai", "--model", m["id"],
                                "--port", "8080", "--threads", "2", "--ctx", "4096"])
        w = aisetup.service_argv(r"C:\PD\nuc-console\ai", m, 8080, 2, 4096, "win32", python=r"C:\py\python.exe", script=r"C:\app\aisetup.py")
        self.assertEqual(w[-2], "--log")
        self.assertTrue(w[-1].endswith("ai.log"))

    @unittest.skipUnless(os.name == "posix", "Unix permissions")
    def test_readable_by_all(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            f = os.path.join(d, "m.gguf")
            put(f, "x")
            os.chmod(f, 0o644)
            self.assertTrue(aisetup._readable_by_all(f))
            os.chmod(f, 0o600)
            self.assertFalse(aisetup._readable_by_all(f))
            os.chmod(f, 0o644)
            os.chmod(d, 0o700)
            self.assertFalse(aisetup._readable_by_all(f))
            os.chmod(d, 0o755)

    def test_install_needs_root(self):
        m = aisetup.MODELS[0]
        with mock.patch.object(aisetup, "is_root", return_value=False), self.assertRaises(aisetup.SetupError) as cm:
            aisetup.install_service("/var/lib/nuc-console/ai", m, 8080, 2, 4096, "linux")
        self.assertIn("root", str(cm.exception))
        with mock.patch.object(aisetup, "is_root", return_value=False), self.assertRaises(aisetup.SetupError):
            aisetup.remove_service("linux")

    @unittest.skipUnless(os.name == "posix", "Unix permissions")
    def test_install_linux_steps(self):
        """The Linux path end to end with every system call replaced: what would be written and run."""
        m, ran, wrote = aisetup.MODELS[0], [], {}
        with tempfile.TemporaryDirectory() as d:
            for sub in ("runtime", "models"):
                os.makedirs(os.path.join(d, sub), mode=0o755)
            os.chmod(d, 0o755)
            for p in (aisetup.runtime_path(d, plat="linux"), aisetup.model_path(d, m, "linux")):
                put(p, "x")
                os.chmod(p, 0o644)
            with mock.patch.object(aisetup, "is_root", return_value=True), mock.patch.object(aisetup, "_linux_user") as user, \
                    mock.patch.object(aisetup, "_write_root_file", side_effect=lambda p, data, mode=0o644: wrote.update({p: data})), \
                    mock.patch.object(aisetup, "tool", side_effect=lambda n, plat=None: "/usr/bin/" + n), \
                    mock.patch.object(aisetup, "run", side_effect=lambda argv, check=True: ran.append(argv)), \
                    mock.patch.object(aisetup, "_is_win", return_value=False), mock.patch.object(aisetup, "_is_mac", return_value=False):
                _r, out, _e = call(aisetup.install_service, d, m, 8080, 2, 4096, "linux")
            argv = aisetup.service_argv(d, m, 8080, 2, 4096, "linux")
        user.assert_called_once_with()
        self.assertEqual(list(wrote), ["/etc/systemd/system/nuc-console-ai.service"])
        self.assertIn("ExecStart=%s -B %s serve --dir %s" % (argv[0], argv[2], d), wrote["/etc/systemd/system/nuc-console-ai.service"])
        self.assertEqual(ran, [["/usr/bin/systemctl", "daemon-reload"], ["/usr/bin/systemctl", "enable", "nuc-console-ai.service"],
                               ["/usr/bin/systemctl", "restart", "nuc-console-ai.service"]])
        self.assertIn("127.0.0.1", out)


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")
        self.runtime, self.model = fake_manifest("http://127.0.0.1:1", b"r" * 100, b"m" * 500)
        self.other = dict(self.model, id="other", file="other.gguf")
        aisetup.ensure_dirs(self.d)
        for p in (aisetup.runtime_path(self.d, self.runtime), aisetup.model_path(self.d, self.model), aisetup.model_path(self.d, self.other),
                  aisetup.model_path(self.d, self.model) + ".part", os.path.join(self.d, "models", "mine.gguf")):
            put(p, b"x" * 10)
        aisetup.record(self.d, aisetup.model_path(self.d, self.model), "0" * 64)

    def remove(self, *extra, ask=None):
        args = aisetup.build_parser().parse_args(["remove", "--dir", self.d] + list(extra))
        return call(aisetup.cmd_remove, args, runtime=self.runtime, models=[self.model, self.other], **({"ask": ask} if ask else {}))

    def test_one_model(self):
        rc, out, err = self.remove("--model", "tiny", "--yes")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.model)))
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.model) + ".part"))
        self.assertTrue(os.path.exists(aisetup.model_path(self.d, self.other)))
        self.assertTrue(os.path.exists(aisetup.runtime_path(self.d, self.runtime)))
        self.assertNotIn("tiny.gguf", aisetup.load_stamp(self.d))

    def test_everything_but_only_what_we_downloaded(self):
        rc, out, err = self.remove("--yes")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(aisetup.runtime_path(self.d, self.runtime)))
        self.assertTrue(os.path.exists(os.path.join(self.d, "models", "mine.gguf")), "a file we do not know is never deleted")

    def test_declined(self):
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            rc, out, err = self.remove("--model", "tiny")
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(aisetup.model_path(self.d, self.model)))

    def test_nothing_to_remove(self):
        shutil.rmtree(self.d)
        rc, out, err = self.remove("--yes")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to remove", out)


class PinsTests(unittest.TestCase):
    def test_hub(self):
        info = {"sha": "a" * 40, "cardData": {"license": "apache-2.0"}}
        tree = [{"type": "file", "path": "README.md", "size": 5}, {"type": "file", "path": "m-Q4_K_M.gguf", "oid": "b" * 40, "size": 25,
                                                               "lfs": {"oid": "c" * 64, "size": 2497280256, "pointerSize": 135}}]
        self.assertEqual(aisetup.pin_from_hub(info, tree, "m-Q4_K_M.gguf"), {"revision": "a" * 40, "sha256": "c" * 64, "size": 2497280256, "license": "apache-2.0"})
        for bad_info, bad_tree, path in (({}, tree, "m-Q4_K_M.gguf"), (info, tree, "missing.gguf"), (info, tree, "README.md"), (info, {}, "x")):
            with self.subTest(path), self.assertRaises(aisetup.SetupError):
                aisetup.pin_from_hub(bad_info, bad_tree, path)

    def test_release(self):
        url = "https://github.com/o/r/releases/download/1.0/r-1.0"
        rel = {"tag_name": "1.0", "assets": [{"name": "x", "browser_download_url": "https://x/y", "size": 1},
                                             {"name": "r-1.0", "browser_download_url": url, "size": 42, "digest": "sha256:" + "d" * 64}]}
        self.assertEqual(aisetup.pin_from_release(rel, url), {"sha256": "d" * 64, "size": 42})
        rel["assets"][1].pop("digest")
        with self.assertRaises(aisetup.SetupError):
            aisetup.pin_from_release(rel, url)
        with self.assertRaises(aisetup.SetupError):
            aisetup.pin_from_release({"assets": []}, url)


class ScriptsTests(unittest.TestCase):
    sh = os.path.join(ROOT, "bin", "nuc-console-ai")
    cmd = os.path.join(ROOT, "bin", "nuc-console-ai.cmd")

    def test_sh_wrapper(self):
        text = get(self.sh)
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn('/usr/bin/python3 /opt/nuc-console/aisetup.py "$@"', text, "the form install-macos.sh rewrites with sed")
        self.assertNotIn("\r", text)
        if os.name == "posix":
            self.assertTrue(os.access(self.sh, os.X_OK))
            self.assertEqual(subprocess.run(["sh", "-n", self.sh]).returncode, 0)

    def test_sh_wrapper_clears_redirecting_variables(self):
        self.assertIn("-u NUC_CONSOLE_HOME", get(self.sh))
        self.assertIn("-u NUC_CONSOLE_CONFIG", get(self.sh))

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck(self):
        self.assertEqual(subprocess.run(["shellcheck", "-S", "warning", self.sh]).returncode, 0)

    def test_cmd_wrapper(self):
        raw = get(self.cmd, "rb")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "CRLF line endings")
        text = raw.decode("ascii")
        self.assertIn(r'"%~dp0..\python\python.exe" -B "%~dp0..\app\aisetup.py" %*', text)
        self.assertIn("set NUC_CONSOLE_HOME=", text)
        self.assertIn("set NUC_CONSOLE_CONFIG=", text)

    def test_gitattributes_keeps_lf(self):
        self.assertIn("bin/nuc-console-ai text eol=lf", get(os.path.join(ROOT, ".gitattributes")))


class MainTests(unittest.TestCase):
    def test_usage_error_is_exit_2(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            aisetup.main(["nuc-console-ai"])
        self.assertEqual(cm.exception.code, 2)

    def test_expected_failures_are_one_line_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, err = call(aisetup.main, ["nuc-console-ai", "serve", "--dir", d, "--config", os.path.join(d, "c.ini")])
        self.assertEqual(rc, 1)
        self.assertEqual([ln for ln in err.splitlines() if ln.startswith("nuc-console-ai:")], [err.strip().splitlines()[-1]])
        self.assertIn("setup", err)

    def test_help_mentions_every_command(self):
        text = aisetup.build_parser().format_help()
        for word in ("setup", "serve", "status", "remove"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
