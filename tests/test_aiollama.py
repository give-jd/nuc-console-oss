"""src/aiollama.py: which build of Ollama a machine needs, unpacking it (nothing outside the folder, nothing but files, folders and links),
the names of models and Ollama's folder of models (installed, every layer there, deleting one without a server), and the few calls of its API
(errors in the server's own words, a cancel that closes the connection, never through a proxy). No network: a local http.server stands in
for the model server."""
import hashlib
import http.server
import io
import json
import os
import shutil
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tarfile
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import aiollama  # noqa: E402

POSIX = os.name == "posix"


def tgz(members):
    """members: [(name, bytes | None for a folder, mode, link target or None, kind)] -> .tgz bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data, mode, link, kind in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                t.addfile(info)
            elif kind == "sym":
                info.type, info.linkname = tarfile.SYMTYPE, link
                t.addfile(info)
            elif kind == "hard":
                info.type, info.linkname = tarfile.LNKTYPE, link
                t.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                t.addfile(info)
            else:
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def zipped(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


class BuildTests(unittest.TestCase):
    def test_the_key_of_each_machine(self):
        k = aiollama.asset_key
        self.assertEqual([k("linux", "x86_64"), k("linux", "aarch64"), k("linux", "arm64")], ["linux-amd64", "linux-arm64", "linux-arm64"])
        self.assertEqual([k("darwin", "arm64"), k("darwin", "x86_64")], ["darwin", "darwin"], "one build for both Macs")
        self.assertEqual([k("win32", "AMD64"), k("win32", "ARM64")], ["windows-amd64", "windows-arm64"])
        self.assertEqual([k("linux", "riscv64"), k("freebsd13", "amd64"), k("win32", "x86")], [None, None, None])

    def test_the_asset_and_its_url(self):
        rt = {"name": "ollama", "version": "9", "base": "https://example.invalid/v9/", "assets": {"linux-amd64": {"file": "o.tar.zst", "sha256": "a" * 64, "size": 3}}}
        self.assertEqual(aiollama.runtime_asset(rt, "linux", "x86_64"),
                         {"key": "linux-amd64", "file": "o.tar.zst", "sha256": "a" * 64, "size": 3, "url": "https://example.invalid/v9/o.tar.zst"})
        self.assertIsNone(aiollama.runtime_asset(rt, "darwin", "arm64"))
        self.assertEqual(aiollama.exe_path("/a", rt, "linux"), "/a/runtime/ollama-9/bin/ollama")
        self.assertEqual(aiollama.exe_path("/a", rt, "darwin"), "/a/runtime/ollama-9/ollama")
        self.assertEqual(aiollama.exe_path(r"C:\a", rt, "win32"), r"C:\a\runtime\ollama-9\ollama.exe")
        self.assertEqual(aiollama.archive_path("/a", rt, "linux", "x86_64"), "/a/runtime/o.tar.zst")
        self.assertIsNone(aiollama.archive_path("/a", rt, "linux", "riscv64"))


class UnpackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dest = os.path.join(self.tmp, "runtime", "ollama-9")

    def archive(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_a_tgz_with_its_folders_executables_and_links(self):
        p = self.archive("o.tgz", tgz([("bin", None, 0o755, None, "dir"), ("bin/ollama", b"#!/bin/sh\n", 0o755, None, "file"),
                                       ("lib/ollama/libx.so.1.2", b"lib", 0o644, None, "file"), ("lib/ollama/libx.so.1", None, 0o777, "libx.so.1.2", "sym"),
                                       ("lib/ollama/libx.so", None, 0o777, "../ollama/libx.so.1", "sym"), ("lib/ollama/copy", None, 0o644, "lib/ollama/libx.so.1.2", "hard"),
                                       ("lib/fifo", None, 0o644, None, "fifo")]))
        if not POSIX:
            self.skipTest("symbolic links need a privilege on Windows: the Windows build is a zip without links")
        aiollama.unpack(p, self.dest)
        with open(os.path.join(self.dest, "lib", "ollama", "libx.so"), "rb") as f:
            self.assertEqual(f.read(), b"lib", "links that stay inside are kept")
        self.assertTrue(os.path.islink(os.path.join(self.dest, "lib", "ollama", "libx.so.1")))
        self.assertEqual(os.stat(os.path.join(self.dest, "bin", "ollama")).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(os.path.join(self.dest, "lib", "ollama", "libx.so.1.2")).st_mode & 0o777, 0o644)
        self.assertFalse(os.path.islink(os.path.join(self.dest, "lib", "ollama", "copy")), "a hard link becomes a copy")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "lib", "fifo")), "a fifo is not unpacked")
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_a_zip(self):
        p = self.archive("o.zip", zipped([("ollama.exe", b"MZ"), ("lib/ollama/ggml.dll", b"dll")]))
        aiollama.unpack(p, self.dest)
        with open(os.path.join(self.dest, "lib", "ollama", "ggml.dll"), "rb") as f:
            self.assertEqual(f.read(), b"dll")
        if POSIX:
            self.assertEqual(os.stat(os.path.join(self.dest, "ollama.exe")).st_mode & 0o111, 0o111, "an .exe stays runnable")

    def test_nothing_ever_lands_outside_the_folder(self):
        for name, data in (("abs.tgz", tgz([("/etc/evil", b"x", 0o644, None, "file")])),
                           ("dots.tgz", tgz([("bin/../../evil", b"x", 0o644, None, "file")])),
                           ("drive.zip", zipped([("C:/evil", b"x")])),
                           ("dots.zip", zipped([("../evil", b"x")])),
                           ("link.tgz", tgz([("lib/l", None, 0o777, "../../../etc/passwd", "sym")])),
                           ("abslink.tgz", tgz([("lib/l", None, 0o777, "/etc/passwd", "sym")])),
                           ("ctrl.tgz", tgz([("bin/a\x1bb", b"x", 0o644, None, "file")]))):
            with self.subTest(name):
                with self.assertRaises(aiollama.SetupError) as cm:
                    aiollama.unpack(self.archive(name, data), self.dest)
                self.assertRegex(str(cm.exception), "unsafe name|leaves it")
                self.assertFalse(os.path.exists(self.dest))
                self.assertFalse(os.path.exists(self.dest + ".part"), "no half-unpacked folder")
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil")))

    def test_a_hard_link_to_nothing_unpacked_is_refused(self):
        p = self.archive("h.tgz", tgz([("lib/copy", None, 0o644, "lib/missing", "hard")]))
        with self.assertRaises(aiollama.SetupError):
            aiollama.unpack(p, self.dest)

    def test_a_broken_or_unknown_archive_is_said_and_leaves_nothing(self):
        for name, data, why in (("o.tgz", b"not gzip at all", "cannot unpack"), ("o.zip", b"PK broken", "cannot unpack"), ("o.rar", b"x", "unknown kind")):
            with self.subTest(name):
                with self.assertRaises(aiollama.SetupError) as cm:
                    aiollama.unpack(self.archive(name, data), self.dest)
                self.assertIn(why, str(cm.exception))
                self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_a_new_unpack_replaces_the_folder_and_a_cancel_leaves_the_old_one(self):
        aiollama.unpack(self.archive("a.zip", zipped([("old", b"1")])), self.dest)
        aiollama.unpack(self.archive("b.zip", zipped([("new", b"2")])), self.dest)
        self.assertEqual(os.listdir(self.dest), ["new"])
        with self.assertRaises(aiollama.Cancelled):
            aiollama.unpack(self.archive("c.zip", zipped([("x", b"3"), ("y", b"4")])), self.dest, cancel=lambda: True)
        self.assertEqual(os.listdir(self.dest), ["new"], "a cancelled unpack does not touch the whole build that is there")
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_a_tar_zst_needs_python_3_14_or_the_zstd_tool_and_says_so(self):
        with mock.patch.dict(sys.modules, {"compression": None}), mock.patch.object(aiollama.shutil, "which", return_value=None):
            with self.assertRaises(aiollama.SetupError) as cm:
                aiollama.unpack(self.archive("o.tar.zst", b"\x28\xb5\x2f\xfd"), self.dest)
        self.assertIn("install zstd", str(cm.exception))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_a_tar_zst(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as t:
            info = tarfile.TarInfo("bin/ollama")
            info.size, info.mode = 3, 0o755
            t.addfile(info, io.BytesIO(b"run"))
        try:
            from compression import zstd
            data = zstd.compress(raw.getvalue())
        except ImportError:
            tool = shutil.which("zstd", path=aiollama.UNIX_PATH)  # where unpack() looks for it, not the PATH of the tests
            if not tool:
                self.skipTest("neither Python 3.14's compression.zstd nor the zstd tool in the system's folders")
            import subprocess
            data = subprocess.run([tool, "-q", "-c"], input=raw.getvalue(), stdout=subprocess.PIPE, check=True).stdout
        aiollama.unpack(self.archive("o.tar.zst", data), self.dest)
        with open(os.path.join(self.dest, "bin", "ollama"), "rb") as f:
            self.assertEqual(f.read(), b"run")


class NameTests(unittest.TestCase):
    def test_names(self):
        p = aiollama.parse_name
        self.assertEqual(p("qwen3:8b"), ("registry.ollama.ai", "library", "qwen3", "8b"))
        self.assertEqual(p("qwen3-8b"), ("registry.ollama.ai", "library", "qwen3-8b", "latest"))
        self.assertEqual(p("user/model:q4"), ("registry.ollama.ai", "user", "model", "q4"))
        self.assertEqual(p("hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M"), ("hf.co", "unsloth", "SmolLM3-3B-GGUF", "Q4_K_M"))
        self.assertEqual(p("127.0.0.1:5000/library/tiny:1b"), ("127.0.0.1:5000", "library", "tiny", "1b"))
        for bad in ("", "a b", "../x", "x/../y:z", "a:b:c/d", "x/y/z/w:t", "-x", "x:" + "t" * 90, "x\n:y"):
            with self.subTest(bad), self.assertRaises(aiollama.SetupError):
                p(bad)
        self.assertEqual(aiollama.manifest_path("/m", "qwen3-8b", "linux"), "/m/manifests/registry.ollama.ai/library/qwen3-8b/latest")
        self.assertEqual(aiollama.blob_path("/m", "sha256:" + "a" * 64, "linux"), "/m/blobs/sha256-" + "a" * 64)
        self.assertEqual(aiollama.base_of("http://127.0.0.1:8080/v1"), "http://127.0.0.1:8080")


class ModelsFolderTests(unittest.TestCase):
    """Ollama's folder of models as nuc-console reads it (installed, every layer there) and deletes from it without a server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.models = os.path.join(self.tmp, "models")

    def blob(self, data):
        d = "sha256:" + hashlib.sha256(data).hexdigest()
        os.makedirs(os.path.join(self.models, "blobs"), exist_ok=True)
        with open(aiollama.blob_path(self.models, d), "wb") as f:
            f.write(data)
        return d, len(data)

    def manifest(self, name, *layers):
        cfg = self.blob(b"{}")
        p = aiollama.manifest_path(self.models, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump({"config": {"digest": cfg[0], "size": cfg[1]}, "layers": [{"digest": d, "size": n} for d, n in layers]}, f)

    def test_installed_and_every_layer(self):
        shared, own = self.blob(b"shared" * 10), self.blob(b"own" * 10)
        self.manifest("a", shared, own)
        self.assertTrue(aiollama.has_model(self.models, "a"))
        self.assertFalse(aiollama.has_model(self.models, "b"))
        self.assertFalse(aiollama.has_model(self.models, "../x"), "a name that is not one is never looked for")
        self.assertTrue(aiollama.blobs_ok(self.models, "a", rehash=True))
        self.assertEqual(aiollama.model_bytes(self.models, "a"), 2 + 60 + 30)
        with open(aiollama.blob_path(self.models, own[0]), "wb") as f:
            f.write(b"OWN" * 10)
        self.assertTrue(aiollama.blobs_ok(self.models, "a"), "the sizes are right")
        self.assertFalse(aiollama.blobs_ok(self.models, "a", rehash=True), "but a layer's SHA-256 is not its name")
        with open(aiollama.manifest_path(self.models, "a"), "w") as f:
            f.write("{broken")
        self.assertFalse(aiollama.blobs_ok(self.models, "a"))
        self.assertEqual(aiollama.read_manifest(aiollama.manifest_path(self.models, "a")), [])

    def test_prune_deletes_what_only_that_model_used(self):
        shared, a_own, b_own = self.blob(b"shared" * 10), self.blob(b"a-own" * 10), self.blob(b"b-own" * 10)
        self.manifest("a", shared, a_own)
        self.manifest("b", shared, b_own)
        stranger = os.path.join(self.models, "blobs", "sha256-" + "f" * 64)
        with open(stranger, "wb") as f:
            f.write(b"not known")
        os.makedirs(os.path.join(self.models, "metadata"))
        meta = os.path.join(self.models, "metadata", a_own[0].replace(":", "-") + ".json")
        with open(meta, "w") as f:
            f.write("{}")
        freed, stuck = aiollama.prune(self.models, ["a"])
        self.assertEqual(stuck, [])
        self.assertEqual(freed, 50 + 2)
        self.assertFalse(aiollama.has_model(self.models, "a"))
        self.assertFalse(os.path.exists(os.path.join(self.models, "manifests", "registry.ollama.ai", "library", "a")), "its empty folders too")
        self.assertTrue(aiollama.blobs_ok(self.models, "b", rehash=True), "b keeps the layer it shares and its config")
        self.assertFalse(os.path.exists(aiollama.blob_path(self.models, a_own[0])))
        self.assertFalse(os.path.exists(meta), "Ollama's note about that layer goes with it")
        self.assertTrue(os.path.exists(stranger), "a file nuc-console does not know of is left alone")
        self.assertEqual(aiollama.prune(self.models, ["a", "nothing"]), (0, []), "nothing to delete is not an error")

    def test_prune_says_what_it_could_not_delete(self):
        own = self.blob(b"own" * 10)
        self.manifest("a", own)
        real = os.unlink

        def unlink(p, *a):
            if p.endswith(own[0].replace(":", "-")):
                raise PermissionError(13, "Permission denied", p)
            return real(p, *a)
        with mock.patch.object(aiollama.os, "unlink", side_effect=unlink):
            freed, stuck = aiollama.prune(self.models, ["a"])
        self.assertEqual(stuck, [(own[0].replace(":", "-"), "Permission denied")])


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.seen.append(("GET", self.path, None))
        code, body = self.server.answers.get(self.path, (404, b"{}"))
        self.reply(code, body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self.server.seen.append(("POST", self.path, body))
        if self.path in self.server.streams:
            self.send_response(200)
            self.end_headers()
            for line in self.server.streams[self.path]:
                if line == "HOLD":
                    self.server.held.set()
                    self.server.release.wait(10)
                    continue
                try:
                    self.wfile.write((json.dumps(line) + "\n").encode())
                    self.wfile.flush()
                except OSError:
                    return
            return
        code, ans = self.server.answers.get(self.path, (404, b"{}"))
        self.reply(code, ans)

    do_DELETE = do_POST

    def reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.seen, self.httpd.answers, self.httpd.streams = [], {}, {}
        self.httpd.held, self.httpd.release = threading.Event(), threading.Event()
        t = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        t.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.release.set)
        self.api = aiollama.Api("http://127.0.0.1:%d" % self.httpd.server_address[1], timeout=5)

    def test_version_or_nothing(self):
        self.assertIsNone(self.api.version(), "a server that is not an Ollama")
        self.httpd.answers["/api/version"] = (200, b'{"version": "0.35.0"}')
        self.assertEqual(self.api.version(), "0.35.0")
        self.assertIsNone(aiollama.Api("http://127.0.0.1:1").version(0.5), "nothing listens")
        with self.assertRaises(aiollama.SetupError):
            aiollama.Api("https://127.0.0.1:1")

    def test_a_pull_reports_the_bytes_of_every_layer_and_ends_with_success(self):
        self.httpd.streams["/api/pull"] = [{"status": "pulling manifest"}, {"status": "pulling aaa", "digest": "sha256:a", "total": 100, "completed": 50},
                                           {"status": "pulling aaa", "digest": "sha256:a", "total": 100, "completed": 100},
                                           {"status": "pulling bbb", "digest": "sha256:b", "total": 10, "completed": 10},
                                           {"status": "verifying sha256 digest"}, {"status": "success"}]
        seen = []
        self.api.pull("qwen3:8b", lambda d, t, s: seen.append((d, t, s)))
        self.assertEqual(seen[1:4], [(50, 100, "pulling aaa"), (100, 100, "pulling aaa"), (110, 110, "pulling bbb")])
        self.assertEqual(seen[-2:], [(110, 110, "verifying sha256 digest"), (110, 110, "success")])
        self.assertEqual(self.httpd.seen[-1], ("POST", "/api/pull", {"model": "qwen3:8b", "stream": True, "insecure": False}))

    def test_errors_are_the_servers_words(self):
        self.httpd.streams["/api/pull"] = [{"status": "pulling manifest"}, {"error": "pull model manifest: file does not exist\x1b[31m"}]
        with self.assertRaises(aiollama.SetupError) as cm:
            self.api.pull("nope:1")
        self.assertEqual(str(cm.exception), "the model server says: pull model manifest: file does not exist?[31m")
        self.httpd.answers["/api/generate"] = (500, b'{"error": "model requires more system memory"}')
        with self.assertRaises(aiollama.SetupError) as cm:
            self.api.load("big")
        self.assertIn("model requires more system memory", str(cm.exception))
        self.httpd.answers["/api/copy"] = (404, b'{"error": "model \'x\' not found"}')
        with self.assertRaises(aiollama.SetupError):
            self.api.copy("x", "y")
        self.httpd.answers["/api/delete"] = (404, b'{"error": "not found"}')
        self.assertFalse(self.api.delete("x"))
        self.httpd.answers["/api/delete"] = (200, b"")
        self.assertTrue(self.api.delete("x"))
        self.httpd.answers["/api/delete"] = (500, b"oops")
        with self.assertRaises(aiollama.SetupError):
            self.api.delete("x")

    def test_a_cancel_closes_the_connection(self):
        self.httpd.streams["/api/pull"] = [{"status": "pulling manifest"}, "HOLD", {"status": "success"}]
        flag = threading.Event()
        threading.Thread(target=lambda: (self.httpd.held.wait(10), flag.set()), daemon=True).start()
        with self.assertRaises(aiollama.Cancelled):
            self.api.pull("qwen3:8b", cancel=flag.is_set)
        self.httpd.streams["/api/generate"] = ["HOLD"]
        flag.clear()
        self.httpd.held.clear()
        threading.Thread(target=lambda: (self.httpd.held.wait(10), flag.set()), daemon=True).start()
        with self.assertRaises(aiollama.Cancelled):
            self.api.load("qwen3-8b", cancel=flag.is_set)

    def test_a_server_that_goes_away_mid_answer_is_said(self):
        self.httpd.streams["/api/pull"] = [{"status": "pulling manifest"}]
        with mock.patch.object(aiollama.http.client.HTTPResponse, "readline", side_effect=ConnectionResetError(104, "reset")):
            with self.assertRaises(aiollama.SetupError) as cm:
                self.api.pull("qwen3:8b")
        self.assertIn("stopped answering", str(cm.exception))

    def test_never_through_a_proxy(self):
        self.httpd.answers["/api/version"] = (200, b'{"version": "1"}')
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"}):
            self.assertEqual(self.api.version(), "1")


class ServerEnvTests(unittest.TestCase):
    def test_the_environment_of_the_server(self):
        env = aiollama.server_env("/ai", 8080, 4096, True, {"PATH": "/usr/bin", "OLLAMA_HOST": "0.0.0.0"}, "linux")
        self.assertEqual(env["OLLAMA_HOST"], "127.0.0.1:8080", "the caller's base never decides where it listens")
        self.assertEqual(env["OLLAMA_MODELS"], "/ai/models")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(aiollama.server_argv("/x/ollama"), ["/x/ollama", "serve"])


if __name__ == "__main__":
    unittest.main()
