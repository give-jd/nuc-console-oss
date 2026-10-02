"""nuc-console-ai (src/aisetup.py, src/aiollama.py): the pins, the downloader, unpacking the server's build, the cache directories, the
server's command and environment, the config helper, status, the service files, and the choice of models by what the machine can run
(models, setup of several, use, the GPU decision, catalog). No network: downloads run against a local http.server serving fake files, and
the model server is the fake Ollama of fakeollama.py (pulls from its fake registry, on loopback). The hardware advice (src/aihw.py) is
replaced by FakeAihw, written from its contract, so these tests depend neither on that module nor on the machine they run on."""
import argparse
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
import aiollama  # noqa: E402
import aisetup  # noqa: E402
import nuc_config  # noqa: E402
sys.path.insert(0, HERE)
import fakeollama as fo  # noqa: E402


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


def popen_any(argv, **kw):
    """subprocess.Popen, except that on Windows the fake ollama.exe (Python source: fakeollama.py) is run by this interpreter."""
    if os.name != "posix" and argv and argv[0].lower().endswith("ollama.exe"):
        argv = [sys.executable] + list(argv)
    return subprocess.Popen(argv, **kw)


class FakeAihw:
    """A stand-in for src/aihw.py, written from the contract (verdicts gpu / partial / ram / slow / no, recommend, cached). aisetup imports
    aihw lazily, so a test puts this in sys.modules. The numbers are simplified: need = weights + 0.6 GB of KV cache + 0.3 GB."""

    def __init__(self, hw):
        self.hw = hw

    def cached(self, max_age=300):
        return self.hw

    def need_mb(self, model, ctx=4096):
        return int((model.get("approx_mb") or (model.get("size") or 0) // 10 ** 6) + ctx * 0.15 + 300)

    def assess(self, model, hw, ctx=4096):
        need, layers = self.need_mb(model, ctx), model.get("layers") or 32
        ram, free = (hw.get("ram") or {}).get("total_mb"), (hw.get("ram") or {}).get("available_mb")
        gpu = next((g for g in hw.get("gpus") or [] if g.get("backend") not in (None, "none")), None)
        active = (model.get("active_b") or model.get("params_b") or 1.0) / (model.get("params_b") or 1.0)

        def res(verdict, where, n, bandwidth):
            tps = bandwidth * 1000.0 / max(1.0, (model.get("approx_mb") or 100) * active)
            return {"verdict": verdict, "where": where, "need_mb": need, "gpu_layers": n, "why": "%s needs %d MB: %s" % (model["id"], need, verdict),
                    "tok_s": [round(tps * 0.6), round(tps)] if bandwidth else None}
        if gpu and gpu.get("unified"):
            if ram and need <= ram * 0.65 and need <= free:
                return res("gpu", "GPU", layers, 100)
        elif gpu and gpu.get("vram_free_mb"):
            if gpu["vram_free_mb"] >= need * 1.1:
                return res("gpu", "GPU", layers, 300)
            share = gpu["vram_free_mb"] / float(need)
            if share >= 0.25 and need * (1 - share) <= (ram or 0) * 0.5:
                return res("partial", "GPU+CPU", int(layers * share), 120)
        if ram is None:
            return res("slow", "CPU", 0, 0)
        if need <= ram * 0.5 and need <= free - 1000:
            return res("ram", "CPU", 0, 30)
        return res("slow" if need <= ram * 0.85 else "no", "CPU", 0, 20)

    def recommend(self, models, hw):
        rated = [(m, self.assess(m, hw)["verdict"]) for m in models]  # models are ordered best first
        for wanted in (("gpu", "ram"), ("partial",)):
            for m, v in rated:
                if v in wanted:
                    return m["id"]
        fits = [m for m, v in rated if v != "no"]
        return min(fits, key=lambda m: m.get("approx_mb") or 0)["id"] if fits else None


def hw_of(ram_mb, available_mb=None, gpus=(), os_name="linux", arch="x86_64", notes=()):
    return {"os": os_name, "arch": arch, "cpu": {"model": "Test CPU 9000", "cores": 8, "threads": 16, "flags": ["avx2", "avx512f"]},
            "ram": {"total_mb": ram_mb, "available_mb": available_mb}, "gpus": list(gpus), "notes": list(notes)}


def gpu_of(name, vram_mb, free_mb, vendor="nvidia", backend="cuda", unified=False):
    return {"vendor": vendor, "name": name, "vram_mb": vram_mb, "vram_free_mb": free_mb, "unified": unified, "backend": backend, "source": "test"}


HW_BIG = hw_of(65536, 60000, [gpu_of("NVIDIA GeForce RTX 4090", 24576, 22000)])  # a big NVIDIA GPU
HW_SMALL_GPU = hw_of(16384, 11000, [gpu_of("NVIDIA GeForce GTX 1650", 4096, 3500)], "windows")
HW_APPLE = hw_of(16384, 11000, [gpu_of("Apple M2", None, None, "apple", "metal", True)], "darwin", "arm64")  # 16 GB unified memory
HW_CPU8 = hw_of(8192, 5500, notes=["nvidia-smi not found"])  # 8 GB, no GPU
HW_UNKNOWN = hw_of(None, None, notes=["/proc/meminfo could not be read"])  # nothing known about the memory
ALL_HW = {"big nvidia": HW_BIG, "small gpu": HW_SMALL_GPU, "apple 16 GB": HW_APPLE, "8 GB cpu": HW_CPU8, "unknown ram": HW_UNKNOWN}


def advice(testcase, hw):
    """The hardware advice with FakeAihw and the machine `hw` for the rest of the test."""
    patcher = mock.patch.dict(sys.modules, {"aihw": FakeAihw(hw)})
    patcher.start()
    testcase.addCleanup(patcher.stop)


def fake_catalog(base="http://127.0.0.1:1"):
    """-> (runtime, [huge, big, mid, small]): models of known sizes (the fake advice needs ~31, ~10, ~3.4 and ~1.3 GB); each weighs a few KB in
    the fake registry."""
    runtime = fo.fake_runtime(base)[0]
    models = []
    for rank, (name, approx) in enumerate((("huge", 30000), ("big", 9000), ("mid", 2500), ("small", 400)), 1):
        models.append(fo.fake_model(name, 5000 + rank, rank, approx_mb=approx, ram_mb=approx + 800, params_b=approx / 600.0))
    return runtime, models


def install_files(d, runtime, models, only=None):
    """The server's build and the models of fake_catalog on disk, like a finished setup."""
    fo.install_runtime(d, runtime)
    for m in models:
        if only is None or m["id"] in only:
            fo.install_model(d, m)


Q4B = aisetup.find_model("qwen3-4b")


class ManifestTests(unittest.TestCase):
    def test_models(self):
        ids = [m["id"] for m in aisetup.MODELS]
        self.assertEqual(len(ids), len(set(ids)), "ids must be unique")
        self.assertIn(aisetup.DEFAULT_MODEL, ids)
        self.assertGreaterEqual(len(ids), 8, "a catalog to choose from: several sizes")
        self.assertEqual([m["rank"] for m in aisetup.MODELS], list(range(1, len(ids) + 1)), "ordered best first, ranks 1..n")
        names = [m["ollama"] for m in aisetup.MODELS]
        self.assertEqual(len(names), len(set(names)), "one model of the library per entry")
        for m in aisetup.MODELS:
            with self.subTest(m["id"]):
                self.assertTrue(re.fullmatch(r"[a-z0-9][a-z0-9.-]*", m["id"]))
                self.assertTrue(aiollama.NAME.fullmatch(m["id"]), "the id is what the server is asked for: a plain model name")
                self.assertIn(m["license"], aisetup.ALLOWED_LICENSES)
                self.assertIsInstance(m["ram_mb"], int)
                self.assertIn(m["quant"], ("Q4_K_M", "MXFP4"))
                self.assertGreater(m["params_b"], 0.1)
                self.assertTrue(10 <= m["layers"] <= 100, m["layers"])
                self.assertGreaterEqual(m["ctx_max"], aisetup.DEFAULT_CTX)
                self.assertTrue(m["notes"] and "\n" not in m["notes"] and m["notes"].isascii(), "one line of ASCII")
                # a typo in a size would silently turn a verdict around: Q4_K_M is about 0.5-0.8 GB per billion parameters
                self.assertTrue(m["params_b"] * 500 <= m["approx_mb"] <= m["params_b"] * 900, (m["params_b"], m["approx_mb"]))
                self.assertTrue(300 <= m["ram_mb"] - m["approx_mb"] <= 1200, "the server needs the file plus a KV cache and some overhead")
                if "active_b" in m:  # a mixture of experts reads fewer parameters per token than it holds
                    self.assertLess(m["active_b"], m["params_b"])
                self.assertEqual(aisetup.missing_pins(m, True), [], "every model names what to pull")
                host, ns, model, tag = aiollama.parse_name(m["ollama"])
                self.assertIn(host, ("registry.ollama.ai", "hf.co"), "the Ollama library, or a GGUF repository it pulls from")
                self.assertNotEqual(model, m["id"], "the library's name is dropped after the copy: it must not be the id itself")
                for k in ("repo", "file", "revision", "sha256", "url"):
                    self.assertNotIn(k, m, "nothing is fetched but through the server")
                if m.get("size") is not None:
                    self.assertTrue(isinstance(m["size"], int) and m["size"] > 10 ** 8, m["size"])
                    self.assertTrue(0.7 <= m["size"] / (m["approx_mb"] * 1e6) <= 1.3, "approx_mb is near the registry's size")

    def test_a_model_that_cannot_work_is_refused_before_anything_else(self):
        advice(self, HW_CPU8)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(urllib_request(), "urlopen", side_effect=AssertionError("network")):
            args = aisetup.build_parser().parse_args(["setup", "qwen3-30b-a3b", "--dir", d, "--yes", "--no-config"])
            with self.assertRaises(aisetup.SetupError) as cm:
                call(aisetup.cmd_setup, args)
            self.assertEqual(os.listdir(d), [])
        self.assertIn("qwen3-30b-a3b will not work on this machine", str(cm.exception))

    def test_runtime(self):
        r = aisetup.RUNTIME
        self.assertEqual(r["name"], "ollama")
        self.assertIn(r["license"], aisetup.ALLOWED_LICENSES)
        self.assertEqual(r["base"], "https://github.com/ollama/ollama/releases/download/v%s/" % r["version"])
        self.assertEqual(set(r["assets"]), {"linux-amd64", "linux-arm64", "darwin", "windows-amd64", "windows-arm64"},
                         "a build for every system and processor nuc-console's archives are made for")
        for key, a in r["assets"].items():
            with self.subTest(key):
                self.assertRegex(a["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(isinstance(a["size"], int) and a["size"] > 10 ** 7)
                self.assertEqual(a["file"], "ollama-%s%s" % (key, ".zip" if key.startswith("windows") else ".tgz" if key == "darwin" else ".tar.zst"))
        for plat, machine in (("linux", "x86_64"), ("linux", "aarch64"), ("darwin", "arm64"), ("darwin", "x86_64"), ("win32", "AMD64"), ("win32", "ARM64")):
            with self.subTest(plat=plat, machine=machine):
                self.assertEqual(aisetup.missing_pins(r, False, plat, machine), [])
                self.assertTrue(aiollama.runtime_asset(r, plat, machine)["url"].startswith(r["base"] + "ollama-"))

    def test_missing_pins_checks_formats(self):
        rt = {"name": "ollama", "version": "1", "base": "https://example.invalid/", "assets": {"linux-amd64": {"file": "f.tgz", "sha256": "abc", "size": 5}}}
        self.assertEqual(aisetup.missing_pins(rt, False, "linux", "x86_64"), ["sha256 (not 64 hex)"])
        self.assertEqual(aisetup.missing_pins(dict(rt, assets={"linux-amd64": {"file": "f.tgz", "sha256": "c" * 64, "size": 5}}), False, "linux", "x86_64"), [])
        self.assertEqual(aisetup.missing_pins(dict(rt, assets={"linux-amd64": {"file": "f.tgz", "sha256": None, "size": None}}), False, "linux", "x86_64"),
                         ["sha256", "size"])
        self.assertEqual(aisetup.missing_pins(rt, False, "linux", "riscv64"), ["a build for this system (linux riscv64)"])
        self.assertEqual(aisetup.missing_pins(rt, False, "win32", "AMD64"), ["a build for this system (windows-amd64)"])
        m = dict(aisetup.MODELS[0])
        self.assertEqual(aisetup.missing_pins(dict(m, ollama=None), True), ["ollama (the name to pull)"])
        self.assertEqual(aisetup.missing_pins(dict(m, ollama="a b:c"), True), ["ollama (not a model name)"])
        self.assertEqual(aisetup.missing_pins(dict(m, ollama="../x:y"), True), ["ollama (not a model name)"])

    def test_moe_models_are_marked(self):
        moe = {m["id"] for m in aisetup.MODELS if "active_b" in m}
        self.assertEqual(moe, {"qwen3-30b-a3b", "gpt-oss-20b"})

    def test_default_and_ram(self):  # without advice: the best model that takes at most half the RAM
        with mock.patch.dict(sys.modules, {"aihw": None}):
            self.assertEqual(aisetup.pick_default(None)["id"], aisetup.DEFAULT_MODEL)
            for total in (16000, 8000, 4096):
                self.assertEqual(aisetup.pick_default(total), next(m for m in aisetup.MODELS if m["ram_mb"] <= total * 0.5), total)
            self.assertEqual(aisetup.pick_default(500)["ram_mb"], min(m["ram_mb"] for m in aisetup.MODELS))
            self.assertEqual(aisetup.pick_default(16000, hw=HW_BIG)["id"], aisetup.pick_default(16000)["id"], "no aihw: the RAM rule")

    def test_default_follows_the_advice_when_there_is_one(self):
        for name, hw in ALL_HW.items():
            with self.subTest(name), mock.patch.dict(sys.modules, {"aihw": FakeAihw(hw)}):
                want = FakeAihw(hw).recommend(aisetup.MODELS, hw)
                self.assertEqual(aisetup.pick_default(None, hw=hw)["id"], want or min(aisetup.MODELS, key=lambda m: m["ram_mb"])["id"])
        with mock.patch.dict(sys.modules, {"aihw": FakeAihw(HW_BIG)}):
            self.assertEqual(aisetup.pick_default(None, hw=HW_BIG)["id"], "qwen3-30b-a3b", "a 24 GB GPU holds the best one")
            tiny = hw_of(512, 300)
            self.assertEqual(aisetup.pick_default(512, hw=tiny)["id"], "qwen3-0.6b", "nothing fits: the smallest (setup then refuses it)")

    def test_shipped_endpoint_is_the_config_default(self):
        self.assertEqual(aisetup.SHIPPED_ENDPOINT, nuc_config.load("/nonexistent")["ai"]["endpoint"])

    def test_an_unpinned_entry_refuses_to_download(self):
        advice(self, hw_of(16384, 12000))
        unpinned = dict(aisetup.RUNTIME, assets={})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(urllib_request(), "urlopen", side_effect=AssertionError("network")):
            args = aisetup.build_parser().parse_args(["setup", "qwen3-4b", "--dir", d, "--yes", "--no-config"])
            rc, out, err = call(aisetup.cmd_setup, args, runtime=unpinned)
            self.assertEqual(os.listdir(d), [])
        self.assertEqual(rc, 1)
        self.assertIn("the server: not pinned: a build for this system", err)


def urllib_request():
    import urllib.request
    return urllib.request


class SourceRulesTests(unittest.TestCase):
    def setUp(self):
        self.src = get(os.path.join(ROOT, "src", "aisetup.py"))

    def test_no_shell_no_wide_bind(self):
        for name in ("aisetup.py", "aiollama.py"):
            with self.subTest(name):
                src = get(os.path.join(ROOT, "src", name))
                self.assertNotRegex(src, r"shell\s*=\s*True")
                self.assertNotIn("os.system", src)
                self.assertNotIn("0.0.0.0", src)
                self.assertNotIn("http://", re.sub(r'"http://%s:%d"|"http://%s:%d/v1"|SHIPPED_ENDPOINT = "http://127.0.0.1:11434/v1"|#.*|""".*?"""', "", src, flags=re.S)
                                 .replace("http://127.0.0.1", "").replace("http://schemas.microsoft.com/windows/2004/02/mit/task", "")
                                 .replace("http://www.apple.com", "").replace('"not an http:// server', ""), "downloads and APIs are https only")

    def test_ascii_output(self):
        self.assertTrue(self.src.isascii(), "plain ASCII: Windows consoles")
        self.assertTrue(get(os.path.join(ROOT, "src", "aiollama.py")).isascii())

    def test_host_is_not_a_parameter(self):
        self.assertNotIn("host", inspect.signature(aisetup.serve_argv).parameters)
        self.assertNotIn("host", inspect.signature(aisetup.serve_env).parameters)
        self.assertNotIn("host", inspect.signature(aiollama.server_env).parameters)


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
        v = aisetup.RUNTIME["version"]
        self.assertEqual(aisetup.runtime_path("/c", plat="linux"), "/c/runtime/ollama-%s/bin/ollama" % v)
        self.assertEqual(aisetup.runtime_path("/c", plat="darwin"), "/c/runtime/ollama-%s/ollama" % v)
        self.assertEqual(aisetup.runtime_path(r"C:\c", plat="win32"), r"C:\c\runtime\ollama-%s\ollama.exe" % v)
        self.assertEqual(aisetup.archive_path("/c", plat="linux", machine="aarch64"), "/c/runtime/ollama-linux-arm64.tar.zst")
        self.assertEqual(aisetup.archive_path(r"C:\c", plat="win32", machine="ARM64"), r"C:\c\runtime\ollama-windows-arm64.zip")
        self.assertIsNone(aisetup.archive_path("/c", plat="linux", machine="riscv64"))
        self.assertEqual(aisetup.model_path("/c", Q4B, "linux"), "/c/models/manifests/registry.ollama.ai/library/qwen3-4b/latest")
        self.assertEqual(aisetup.model_path(r"C:\c", Q4B, "win32"), r"C:\c\models\manifests\registry.ollama.ai\library\qwen3-4b\latest")

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

    def test_parse_meminfo(self):
        m = aisetup.parse_meminfo("MemTotal:       16487476 kB\nMemFree:  1 kB\nMemAvailable:   15000000 kB\nHugePages_Total:       0\n")
        self.assertEqual(m["MemTotal"], 16487476 * 1024)
        self.assertEqual(m["MemAvailable"], 15000000 * 1024)


class FolderTests(unittest.TestCase):
    """The folder the files go to, said clearly: what it holds, what is free, who owns the folders `setup` makes in it, and which folder the
    screens work in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")

    def test_dir_space_counts_the_runtime_and_the_models_partial_downloads_too(self):
        os.makedirs(os.path.join(self.d, "runtime", "ollama-0.0", "bin"))
        os.makedirs(os.path.join(self.d, "models", "blobs"))
        put(os.path.join(self.d, "runtime", "ollama-0.0.tgz"), b"a" * 700)
        put(os.path.join(self.d, "runtime", "ollama-0.0", "bin", "ollama"), b"r" * 1000)
        put(os.path.join(self.d, "models", "blobs", "sha256-" + "a" * 64), b"m" * 4000)
        put(os.path.join(self.d, "models", "blobs", "sha256-" + "b" * 64 + "-partial"), b"p" * 500)
        put(os.path.join(self.d, "verified.json"), "{}")                      # not a download
        put(os.path.join(self.d, "web.json"), "{}")
        if os.name == "posix":
            os.symlink("ollama", os.path.join(self.d, "runtime", "ollama-0.0", "bin", "link"))  # a link is not counted twice
        sp = aisetup.dir_space(self.d)
        self.assertEqual(sp["used"], 6200)
        self.assertIsInstance(sp["free"], int)
        self.assertGreater(sp["free"], 0)

    def test_dir_space_of_a_folder_that_does_not_exist_yet(self):
        sp = aisetup.dir_space(os.path.join(self.d, "not", "there"))
        self.assertEqual(sp["used"], 0)
        self.assertGreater(sp["free"], 0, "the free space is that of the disk the folder will be on")
        self.assertFalse(os.path.exists(self.d), "looking creates nothing")

    def test_dir_space_never_raises(self):
        with mock.patch.object(aisetup, "free_bytes", side_effect=OSError("gone")), mock.patch.object(aisetup.os, "scandir", side_effect=OSError("gone")):
            self.assertEqual(aisetup.dir_space(self.d), {"used": 0, "free": None})

    def test_space_text_in_plain_words(self):
        self.assertEqual(aisetup.space_text({"used": 5 * 10 ** 9, "free": 120 * 10 ** 9}), "5.0 GB downloaded, 120.0 GB free on that disk")
        self.assertEqual(aisetup.space_text({"used": 0, "free": 800 * 10 ** 6}), "nothing downloaded, 800 MB free on that disk")
        self.assertEqual(aisetup.space_text({"used": 1, "free": None}), "1 KB downloaded, free space unknown on that disk")
        self.assertEqual(aisetup.space_text(None), "nothing downloaded, free space unknown on that disk")

    def test_the_catalog_carries_the_folder_and_its_space(self):
        runtime, models = fake_catalog()
        install_files(self.d, runtime, models, only=("small",))
        c = aisetup.catalog({}, self.d, models=models, runtime=runtime, cfg={"ai": {"model": ""}})
        self.assertEqual(c["dir"], self.d)
        self.assertEqual(c["space"]["used"], aisetup.du(os.path.join(self.d, "runtime")) + aisetup.du(os.path.join(self.d, "models")))
        self.assertGreater(c["space"]["used"], 5004 + fo.runtime_archive()[1].__len__(), "the archive, the unpacked build and the model")
        self.assertEqual(c["runtime"], {"installed": True, "name": "Ollama", "version": fo.VERSION})

    @unittest.skipUnless(hasattr(os, "geteuid"), "Unix ownership")
    def test_root_hands_a_new_folder_to_the_owner_of_the_folder_it_is_in(self):
        like, new, calls = os.path.join(self.tmp.name, "like"), os.path.join(self.tmp.name, "new"), []
        owner = lambda uid: type("St", (), {"st_uid": uid, "st_gid": uid + 1})()  # noqa: E731
        with mock.patch.object(aisetup.os, "geteuid", return_value=0), mock.patch.object(aisetup.os, "chown", side_effect=lambda *a: calls.append(a)):
            with mock.patch.object(aisetup.os, "stat", return_value=owner(4242)):
                aisetup.adopt(new, like)
            self.assertEqual(calls, [(new, 4242, 4243)])
            calls.clear()
            with mock.patch.object(aisetup.os, "stat", return_value=owner(0)):
                aisetup.adopt(new, like)
            self.assertEqual(calls, [], "a folder root owns: nothing to hand over")
            with mock.patch.object(aisetup.os, "stat", side_effect=OSError("gone")):
                aisetup.adopt(new, like)
            self.assertEqual(calls, [], "no error either")
        with mock.patch.object(aisetup.os, "geteuid", return_value=1000), mock.patch.object(aisetup.os, "chown", side_effect=lambda *a: calls.append(a)), \
                mock.patch.object(aisetup.os, "stat", return_value=owner(4242)):
            aisetup.adopt(new, like)
        self.assertEqual(calls, [], "anyone but root: nothing")

    @unittest.skipUnless(hasattr(os, "geteuid"), "Unix ownership")
    def test_ensure_dirs_adopts_top_down_so_that_the_new_folders_belong_to_the_web_account(self):
        base, calls = os.path.join(self.tmp.name, "lib"), []
        os.makedirs(base)
        with mock.patch.object(aisetup, "adopt", side_effect=lambda path, like: calls.append((path, like))):
            aisetup.ensure_dirs(os.path.join(base, "ai"))
        ai = os.path.join(base, "ai")
        self.assertEqual(calls, [(ai, base), (os.path.join(ai, "runtime"), ai), (os.path.join(ai, "models"), ai)],
                         "each one from the one it is in, outermost first (only the folders that were created)")
        calls.clear()
        with mock.patch.object(aisetup, "adopt", side_effect=lambda path, like: calls.append((path, like))):
            aisetup.ensure_dirs(ai)
        self.assertEqual(calls, [], "folders that exist are never touched")

    def test_work_dir_is_the_system_wide_folder_when_this_account_can_write_there(self):
        system, own = os.path.join(self.tmp.name, "sys"), os.path.join(self.tmp.name, "own")

        def fake_default(plat=None, env=None, euid=None, home=None):
            return system if euid == 0 else own
        with mock.patch.object(aisetup, "default_dir", side_effect=fake_default):
            self.assertEqual(aisetup.work_dir(), own, "no system-wide folder: the account's own")
            os.makedirs(own)
            os.makedirs(system)
            os.chmod(system, 0o755)  # made by this account, in its temporary folder: it can write there, root or not, on every system
            self.assertEqual(aisetup.work_dir(), system, "a system-wide folder this account can write to comes first")
            with mock.patch.object(aisetup.os, "access", return_value=False):
                self.assertEqual(aisetup.work_dir(), own, "a folder this account cannot write to is not one to download into")
            put(aisetup.stamp_path(own), "{}")
            self.assertEqual(aisetup.work_dir(), own, "models installed in the account's own folder are not hidden by an empty system-wide one")
            put(aisetup.stamp_path(system), "{}")
            with mock.patch.object(aisetup.os, "access", return_value=False):
                self.assertEqual(aisetup.work_dir(), system, "what the administrator installed is shown, writable or not")
        with mock.patch.object(aisetup.os, "access", side_effect=AssertionError("os.access is not asked on Windows")):
            self.assertEqual(aisetup.work_dir("win32"), aisetup.default_dir("win32", euid=0),
                             "Windows has one folder for every account (ProgramData's): os.access, which does not read its ACL, is never asked")

    def test_work_dir_is_find_dir_with_nuc_console_home(self):
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_HOME": self.tmp.name}):
            self.assertEqual(aisetup.work_dir(), os.path.join(self.tmp.name, "ai"))
            self.assertEqual(aisetup.work_dir(), aisetup.find_dir())


class ServeCommandTests(unittest.TestCase):
    def check_env(self, env, d, port="8080"):
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()), "an environment of strings")
        self.assertEqual(env["OLLAMA_HOST"], "127.0.0.1:" + port)
        self.assertEqual(env["OLLAMA_MODELS"], os.path.join(d, "models") if os.path.sep in d or d.startswith("/") else env["OLLAMA_MODELS"])
        self.assertEqual(env["OLLAMA_CONTEXT_LENGTH"], str(aisetup.DEFAULT_CTX))
        self.assertEqual((env["OLLAMA_MAX_LOADED_MODELS"], env["OLLAMA_NUM_PARALLEL"]), ("1", "1"))
        self.assertEqual(env["OLLAMA_NO_CLOUD"], "1", "no cloud model: nothing leaves the machine")
        self.assertEqual(env["OLLAMA_NOPRUNE"], "1", "a cancelled pull resumes; the other process may be using the folder")

    def test_linux_and_mac_run_the_executable_with_its_environment(self):
        for plat, d in (("linux", "/var/lib/nuc-console/ai"), ("darwin", "/Library/Application Support/nuc-console/ai")):
            with self.subTest(plat):
                argv = aisetup.serve_argv(d, plat=plat)
                self.assertEqual(argv, [aisetup.runtime_path(d, plat=plat), "serve"])
                env = aisetup.serve_env(d, 8080, home="/h", plat=plat)
                self.check_env(env, d)
                self.assertEqual(env["OLLAMA_MODELS"], d + "/models")
                self.assertEqual((env["PATH"], env["HOME"], env["TMPDIR"]), (aisetup.UNIX_PATH, "/h", "/h"))

    def test_windows_runs_the_exe_with_the_system_folders(self):
        d = r"C:\ProgramData\nuc-console\ai"
        argv = aisetup.serve_argv(d, plat="win32")
        self.assertEqual(argv, [r"C:\ProgramData\nuc-console\ai\runtime\ollama-%s\ollama.exe" % aisetup.RUNTIME["version"], "serve"])
        with mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files", "OLLAMA_HOST": "0.0.0.0:1", "OLLAMA_ORIGINS": "*",
                                          "SECRET": "x"}):
            env = aisetup.serve_env(d, 8080, home=r"C:\h", plat="win32")
        self.check_env(env, d)
        self.assertEqual(env["OLLAMA_MODELS"], d + r"\models")
        self.assertEqual(env["PATH"], r"C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem")
        self.assertEqual((env["USERPROFILE"], env["ProgramFiles"]), (r"C:\h", r"C:\Program Files"))
        self.assertNotIn("SECRET", env, "a small fixed environment, never the caller's")
        self.assertNotIn("OLLAMA_ORIGINS", env)

    def test_the_callers_ollama_variables_never_reach_the_server(self):
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODELS": "/elsewhere", "OLLAMA_ORIGINS": "*", "HTTPS_PROXY": "http://p"}):
            env = aisetup.serve_env("/c", 9090, home="/h", plat="linux")
        self.assertEqual(env["OLLAMA_HOST"], "127.0.0.1:9090")
        self.assertEqual(env["OLLAMA_MODELS"], "/c/models")
        self.assertNotIn("OLLAMA_ORIGINS", env)
        self.assertNotIn("HTTPS_PROXY", env)

    def test_port_and_context_are_numbers_in_the_environment(self):
        env = aisetup.serve_env("/c", "9090", 8192, home="/h", plat="linux")
        self.assertEqual((env["OLLAMA_HOST"], env["OLLAMA_CONTEXT_LENGTH"]), ("127.0.0.1:9090", "8192"))
        with self.assertRaises(ValueError):
            aisetup.serve_env("/c", "8080; reboot", home="/h", plat="linux")

    def test_cpu_only_hides_every_gpu(self):
        env = aisetup.serve_env("/c", 8080, gpu=False, home="/h", plat="linux")
        for k in ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES"):
            self.assertEqual(env[k], "-1", k)
        env = aisetup.serve_env("/c", 8080, gpu=True, home="/h", plat="linux")
        self.assertFalse(set(aiollama.GPU_HIDE) & set(env), "the GPU allowed: nothing hidden")

    def test_unix_exec_with_nice_and_the_environment(self):
        with mock.patch.object(aisetup, "lower_priority") as low, mock.patch.object(os, "execve", create=True) as ex, \
                mock.patch.object(aisetup, "_is_win", return_value=False):
            aisetup.run_server(["/x/ollama", "serve"], {"OLLAMA_HOST": "127.0.0.1:8080"})
        low.assert_called_once_with()
        ex.assert_called_once_with("/x/ollama", ["/x/ollama", "serve"], {"OLLAMA_HOST": "127.0.0.1:8080"})

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
            self.assertEqual(aisetup.run_server([r"C:\x\ollama.exe", "serve"], {"OLLAMA_HOST": "127.0.0.1:8080"}), 0)
        self.assertTrue(popen.call_args[1]["creationflags"] & 0x4000)
        self.assertEqual(popen.call_args[0][0], [r"C:\x\ollama.exe", "serve"])
        self.assertEqual(popen.call_args[1]["env"], {"OLLAMA_HOST": "127.0.0.1:8080"})


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

    def test_cancel_stops_at_the_next_chunk_keeps_the_partial_file_and_a_later_download_resumes_it(self):
        data, seen = blob(2_500_000, b"c"), []
        with Server({"/m.gguf": data}) as srv:
            with self.assertRaises(aisetup.Cancelled):
                self.dl(srv, data=data, progress=lambda done, total: seen.append(done), cancel=lambda: len(seen) >= 1)
            self.assertFalse(os.path.exists(self.dest), "nothing is installed")
            part = os.path.getsize(self.dest + ".part")
            self.assertTrue(1 << 20 <= part < len(data), part)
            self.assertEqual(self.dl(srv, data=data), "downloaded")
            self.assertEqual(srv.requests[-1][1], "bytes=%d-" % part, "the second one starts where the first stopped")
        self.assertEqual(get(self.dest, "rb"), data)

    def test_cancel_before_the_start_does_not_even_connect(self):
        with Server({"/m.gguf": self.data}) as srv:
            with self.assertRaises(aisetup.Cancelled):
                self.dl(srv, cancel=lambda: True)
            self.assertEqual(srv.requests, [])

    def test_cancel_is_looked_at_while_waiting_to_try_again(self):
        slept = []
        with Server({"/m.gguf": self.data}) as srv:
            srv.cuts = [100, 100, 100, 100, 100]  # every answer is cut short: the download keeps trying
            with mock.patch.object(aisetup.time, "sleep", side_effect=lambda s: slept.append(s)):
                with self.assertRaises(aisetup.Cancelled):
                    self.dl(srv, backoff=5.0, cancel=lambda: len(slept) >= 2)
        self.assertLessEqual(max(slept), 0.2, "in slices of a fifth of a second, not one long sleep")
        self.assertEqual(len(srv.requests), 1, "no second attempt after the cancel")

    def test_without_cancel_the_wait_is_one_sleep(self):
        slept = []
        with Server({"/m.gguf": self.data}) as srv:
            srv.cuts = [100]
            with mock.patch.object(aisetup.time, "sleep", side_effect=lambda s: slept.append(s)):
                self.assertEqual(self.dl(srv, backoff=5.0), "downloaded")
        self.assertEqual(slept, [5.0])

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
        self.path = os.path.join(self.d, "runtime", "ollama-fake.tgz")
        os.makedirs(os.path.dirname(self.path))
        put(self.path, self.bytes)

    def test_verified_by_stamp_and_by_hash(self):
        self.assertFalse(aisetup.is_verified(self.d, self.path, sha(self.bytes), len(self.bytes)), "no stamp yet")
        self.assertTrue(aisetup.is_verified(self.d, self.path, sha(self.bytes), len(self.bytes), rehash=True))
        aisetup.record(self.d, self.path, sha(self.bytes))
        self.assertTrue(aisetup.is_verified(self.d, self.path, sha(self.bytes), len(self.bytes)))
        self.assertFalse(aisetup.is_verified(self.d, self.path, "0" * 64, len(self.bytes)), "another pin")

    def test_changed_file_loses_its_stamp(self):
        aisetup.record(self.d, self.path, sha(self.bytes))
        with open(self.path, "r+b") as f:
            f.write(b"X")
        os.utime(self.path, ns=(1, 1))
        self.assertFalse(aisetup.is_verified(self.d, self.path, sha(self.bytes), len(self.bytes)))
        self.assertFalse(aisetup.is_verified(self.d, self.path, sha(self.bytes), len(self.bytes), rehash=True))

    def test_broken_stamp_file_is_just_empty(self):
        put(aisetup.stamp_path(self.d), "{not json")
        self.assertEqual(aisetup.load_stamp(self.d), {})

    def test_the_runtime_is_ready_when_it_was_unpacked_from_the_pinned_archive(self):
        rt = fo.fake_runtime("http://127.0.0.1:1")[0]
        self.assertFalse(aisetup.runtime_ready(self.d, rt))
        fo.install_runtime(self.d, rt)
        self.assertTrue(aisetup.runtime_ready(self.d, rt))
        self.assertTrue(aisetup.runtime_ready(self.d, rt, rehash=True), "the archive is kept and hashes to its pin")
        self.assertFalse(aisetup.runtime_ready(self.d, dict(rt, assets={fo.KEY: dict(rt["assets"][fo.KEY], sha256="0" * 64)})),
                         "unpacked from another archive than the one pinned now")
        self.assertFalse(aisetup.runtime_ready(self.d, dict(rt, assets={})), "no build for this system")
        os.unlink(aisetup.runtime_path(self.d, rt))
        self.assertFalse(aisetup.runtime_ready(self.d, rt), "the executable is gone")

    def test_a_changed_archive_fails_the_rehash_only(self):
        rt = fo.fake_runtime("http://127.0.0.1:1")[0]
        fo.install_runtime(self.d, rt)
        arch = aisetup.archive_path(self.d, rt)
        with open(arch, "r+b") as f:
            f.write(b"X")
        self.assertTrue(aisetup.runtime_ready(self.d, rt), "the stamp says it was unpacked from the pinned archive")
        self.assertFalse(aisetup.runtime_ready(self.d, rt, rehash=True))
        os.unlink(arch)
        self.assertFalse(aisetup.runtime_ready(self.d, rt, rehash=True), "no archive to check: `serve --install-service` refuses")

    def test_a_model_is_ready_when_its_manifest_and_every_layer_are_there(self):
        m = fo.fake_model("tiny", 5000)
        self.assertFalse(aisetup.model_ready(self.d, m))
        fo.install_model(self.d, m)
        self.assertTrue(aisetup.model_ready(self.d, m))
        self.assertTrue(aisetup.model_ready(self.d, m, rehash=True))
        self.assertFalse(aisetup.model_ready(self.d, dict(m, ollama=None)), "an unpinned entry is never installed")
        layers = aiollama.read_manifest(aisetup.model_path(self.d, m))
        blob_file = aiollama.blob_path(aiollama.models_dir(self.d), layers[1][0])
        with open(blob_file, "r+b") as f:
            f.write(b"X")  # same size, another content
        self.assertTrue(aisetup.model_ready(self.d, m), "without rehash only the sizes are looked at")
        self.assertFalse(aisetup.model_ready(self.d, m, rehash=True), "a layer whose SHA-256 is not its name")
        os.unlink(blob_file)
        self.assertFalse(aisetup.model_ready(self.d, m), "a missing layer")


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")
        self.cfg = os.path.join(self.tmp.name, "config.ini")
        self.archive = fo.runtime_archive()
        self.model = fo.fake_model("tiny", 200_000)
        patcher = mock.patch.object(aisetup, "memory_mb", return_value=(16000, 12000))
        patcher.start()
        self.addCleanup(patcher.stop)
        advice(self, hw_of(16000, 12000))

    def runtime(self, srv):
        return fo.fake_runtime(srv_base(srv), self.archive)[0]

    def files(self):
        return {"/" + self.archive[0]: self.archive[1]}

    def run_setup(self, srv, *extra, runtime=None, models=None, ask=None, registry=True):
        models = models or [self.model]
        if registry:
            fo.write_control(self.d, registry=fo.registry_for(models))
        args = aisetup.build_parser().parse_args(["setup", "--dir", self.d, "--config", self.cfg, "--port", "18080"] + list(extra))
        kw = {"ask": ask} if ask else {}
        return call(aisetup.cmd_setup, args, runtime=runtime or self.runtime(srv), models=models, allow_loopback_http=True, popen=popen_any, **kw)

    def pulls(self):
        return [r["body"]["model"] for r in fo.requests_seen(self.d) if r["path"] == "/api/pull"]

    def test_setup_downloads_once_and_never_again(self):
        put(self.cfg, "# my settings\n[features]\nhealth = yes\n\n[ai]\n# local model\nenabled = no\nendpoint = http://127.0.0.1:11434/v1\nmodel =\n")
        with Server(self.files()) as srv:
            rt = self.runtime(srv)
            rc, out, err = self.run_setup(srv, "tiny", "--yes")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(len(srv.requests), 1, "the server's archive, once")
            self.assertEqual(self.pulls(), ["tiny:test"], "the model, pulled once through the server")
            self.assertTrue(aisetup.runtime_ready(self.d, rt, rehash=True))
            self.assertTrue(aisetup.model_ready(self.d, self.model, rehash=True))
            if os.name == "posix":
                self.assertEqual(os.stat(aisetup.runtime_path(self.d, rt)).st_mode & 0o777, 0o755)
            self.assertEqual(sorted(aisetup.load_stamp(self.d)), sorted([self.archive[0], aisetup.runtime_key(rt)]))
            ids = [r["body"] for r in fo.requests_seen(self.d) if r["path"] in ("/api/copy", "/api/delete")]
            self.assertEqual(ids, [{"source": "tiny:test", "destination": "tiny"}, {"model": "tiny:test"}], "named by its id, the library's name dropped")
            rc, out, err = self.run_setup(srv, "tiny", "--yes")  # second run
            self.assertEqual(rc, 0)
            self.assertEqual(len(srv.requests), 1, "no request at all the second time")
            self.assertEqual(self.pulls(), ["tiny:test"], "and no server started to pull")
            self.assertIn("not downloaded again", out)
            self.assertIn("server : Ollama %s (MIT), installed" % fo.VERSION, out)
        cfg = get(self.cfg)
        self.assertIn("# my settings", cfg)
        self.assertIn("# local model", cfg)
        self.assertIn("endpoint = http://127.0.0.1:18080/v1", cfg)
        self.assertIn("model = tiny", cfg)
        self.assertIn("enabled = no", cfg, "the advisor is not switched on behind the admin's back")
        self.assertEqual(nuc_config.load(self.cfg)["features"]["health"], True)

    def test_the_server_started_to_pull_is_stopped(self):
        started = []

        def popen(argv, **kw):
            p = popen_any(argv, **kw)
            started.append(p)
            return p
        with Server(self.files()) as srv:
            fo.write_control(self.d, registry=fo.registry_for([self.model]))
            args = aisetup.build_parser().parse_args(["setup", "tiny", "--dir", self.d, "--yes", "--no-config"])
            rc, out, err = call(aisetup.cmd_setup, args, runtime=self.runtime(srv), models=[self.model], allow_loopback_http=True, popen=popen)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll(), "nothing keeps running after setup")
        env = started[0].args if isinstance(started[0].args, list) else []
        self.assertEqual(env[-1], "serve")

    def test_setup_shows_the_choice(self):
        with Server(self.files()) as srv:
            rc, out, err = self.run_setup(srv, "tiny", "--yes", "--no-config")
        self.assertIn("tiny", out)
        self.assertIn("MIT", out)
        self.assertIn("Ollama %s" % fo.VERSION, out)
        self.assertFalse(os.path.exists(self.cfg), "--no-config")

    def test_without_consent_nothing_is_downloaded(self):
        with Server(self.files()) as srv, mock.patch.object(sys, "stdin", io.StringIO("")):
            rc, out, err = self.run_setup(srv, "tiny", registry=False)  # no --yes, stdin is not a terminal
            self.assertEqual(rc, 1)
            self.assertEqual(srv.requests, [])
        self.assertIn("--yes", err)
        self.assertFalse(os.path.exists(self.d))

    def test_unpinned_entries_refuse_and_list_what_is_missing(self):
        with Server(self.files()) as srv:
            rt = dict(self.runtime(srv), assets={})
            rc, out, err = self.run_setup(srv, "tiny", "--yes", runtime=rt, models=[dict(self.model, ollama=None)], registry=False)
            self.assertEqual(rc, 1)
            self.assertEqual(srv.requests, [])
        self.assertIn("the server: not pinned: a build for this system", err)
        self.assertIn("tiny: not pinned: ollama (the name to pull)", err)
        self.assertFalse(os.path.exists(self.d), "not even the directory")

    def test_corrupt_download_installs_nothing(self):
        with Server({"/" + self.archive[0]: bytes(len(self.archive[1]))}) as srv:
            with self.assertRaises(aisetup.SetupError) as cm:
                self.run_setup(srv, "tiny", "--yes")
        self.assertIn("SHA-256", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.d, "runtime", self.archive[0])))
        self.assertFalse(os.path.exists(os.path.join(self.d, "runtime", self.archive[0] + ".part")))
        self.assertFalse(os.path.exists(aiollama.runtime_dir(self.d, fo.fake_runtime("http://x")[0])), "nothing unpacked")

    def test_a_pull_the_server_refuses_is_said_and_nothing_is_left_running(self):
        with Server(self.files()) as srv:
            fo.write_control(self.d, pull_error="pull model manifest: file does not exist")
            with self.assertRaises(aisetup.SetupError) as cm:
                self.run_setup(srv, "tiny", "--yes", registry=False)
        self.assertIn("the model server says: pull model manifest: file does not exist", str(cm.exception))
        self.assertFalse(aisetup.model_ready(self.d, self.model))
        self.assertTrue(aisetup.runtime_ready(self.d, self.runtime(srv)), "the server is installed: the next run only pulls")

    def test_a_server_that_does_not_start_is_said(self):
        with Server(self.files()) as srv:
            fo.write_control(self.d, die="boom: cannot start")
            with self.assertRaises(aisetup.SetupError) as cm:
                self.run_setup(srv, "tiny", "--yes", registry=False)
        self.assertIn("the model server stopped at once (exit status 3)", str(cm.exception))
        self.assertIn("boom: cannot start", str(cm.exception))

    def test_not_enough_disk_space(self):
        with Server(self.files()) as srv, mock.patch.object(aisetup, "free_bytes", return_value=1000):
            with self.assertRaises(aisetup.SetupError) as cm:
                self.run_setup(srv, "tiny", "--yes", registry=False)
            self.assertEqual(srv.requests, [])
        self.assertIn("disk space", str(cm.exception))

    def test_unknown_model(self):
        args = aisetup.build_parser().parse_args(["setup", "--dir", self.d, "--model", "gpt-9"])
        with self.assertRaises(aisetup.SetupError) as cm:
            call(aisetup.cmd_setup, args)
        self.assertIn("qwen3-4b", str(cm.exception))

    def test_interrupted_download_resumes_in_the_next_run(self):
        big = fo.runtime_archive(fo.runtime_exe() + b"\n#" + blob(60_000, b"pad").hex().encode())  # an archive that takes more than one cut
        with Server({"/" + big[0]: big[1]}) as srv:
            srv.cuts = [5000] * 10  # the connection dies again and again: this run gives up
            rt = fo.fake_runtime(srv_base(srv), big)[0]
            with mock.patch.object(aisetup.time, "sleep"):
                with self.assertRaises(aisetup.SetupError):
                    self.run_setup(srv, "tiny", "--yes", "--no-config", runtime=rt)
            part = os.path.join(self.d, "runtime", big[0] + ".part")
            self.assertGreater(os.path.getsize(part), 0, "what arrived is kept")
            srv.cuts = []
            rc, out, err = self.run_setup(srv, "tiny", "--yes", "--no-config", runtime=rt)
            self.assertEqual(rc, 0, err)
            self.assertTrue(any(r and r.startswith("bytes=") for _p, r in srv.requests), "resumed with Range")

    # --- several models, the recommended one, what will not work or will be slow ---------------------------------------------------
    def many(self, approx=(("a", 400), ("b", 2500))):
        """-> [models] of fake models (name, approx_mb), each a few KB in the fake registry."""
        return [fo.fake_model(name, 30_000 + rank, rank, approx_mb=mb, ram_mb=mb + 800) for rank, (name, mb) in enumerate(approx, 1)]

    def setup_many(self, srv, models, *argv, ask=None):
        return self.run_setup(srv, *argv, models=models, ask=ask)

    def installed(self, models):
        return sorted(m["id"] for m in models if aisetup.model_ready(self.d, m))

    def test_several_models_in_one_run(self):
        models = self.many((("a", 400), ("b", 2500), ("c", 1000)))
        with Server(self.files()) as srv:
            rc, out, err = self.setup_many(srv, models, "b", "a", "--yes")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(len(srv.requests), 1, "the server once")
            self.assertEqual(sorted(self.pulls()), ["a:test", "b:test"], "each model once")
            self.assertEqual(self.installed(models), ["a", "b"], "only what was asked for")
            rc, out, err = self.setup_many(srv, models, "a", "b", "--yes")  # again, in another order
            self.assertEqual(rc, 0)
            self.assertEqual((len(srv.requests), len(self.pulls())), (1, 2), "nothing is downloaded twice")
        self.assertEqual(out.count("not downloaded again"), 3)
        self.assertIn("model = b", get(self.cfg), "the first model named is the one [ai] points at")
        self.assertIn("serves every installed model", out)

    def test_repeated_names_are_one_download(self):
        models = self.many()
        with Server(self.files()) as srv:
            rc, out, err = self.setup_many(srv, models, "a", "a", "--model", "a", "--yes", "--no-config")
            self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.pulls(), ["a:test"])

    def test_an_unknown_name_among_several_downloads_nothing(self):
        models = self.many()
        with Server(self.files()) as srv:
            with self.assertRaises(aisetup.SetupError) as cm:
                self.run_setup(srv, "a", "nope", "--yes", models=models, registry=False)
            self.assertEqual(srv.requests, [])
        self.assertIn("nope", str(cm.exception))
        self.assertFalse(os.path.exists(self.d))

    def test_no_model_named_installs_the_recommended_one(self):
        models = self.many((("huge", 30000), ("fits", 2500), ("small", 400)))  # on 16 GB: no, ram, ram
        with Server(self.files()) as srv:
            rc, out, err = self.setup_many(srv, models, "--yes", "--no-config")
            self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.installed(models), ["fits"], "the best-ranked model that fits")
        self.assertIn("recommended for this machine", out)

    def test_no_model_named_and_nothing_fits_is_refused_with_the_reason(self):
        models = self.many((("huge", 30000), ("big", 20000)))
        with Server(self.files()) as srv:
            with self.assertRaises(aisetup.SetupError) as cm:
                self.run_setup(srv, "--yes", models=models, registry=False)
            self.assertEqual(srv.requests, [])
        self.assertIn("will not work on this machine", str(cm.exception))
        self.assertIn("--force", str(cm.exception))

    def test_a_model_that_will_not_work_is_refused_unless_forced(self):
        models = self.many((("huge", 30000), ("small", 400)))
        with Server(self.files()) as srv:
            with self.assertRaises(aisetup.SetupError) as cm:
                self.run_setup(srv, "small", "huge", "--yes", models=models, registry=False)
            self.assertEqual(srv.requests, [], "not even the model that would have been fine")
            self.assertIn("huge will not work on this machine", str(cm.exception))
            self.assertNotIn("small will not", str(cm.exception))
            rc, out, err = self.setup_many(srv, models, "huge", "--yes", "--force", "--no-config")
            self.assertEqual(rc, 0, out + err)
        self.assertIn("will not work on this machine, installing it anyway", out)
        self.assertEqual(self.installed(models), ["huge"])

    def test_a_model_that_will_slow_the_pc_down_is_installed_with_a_warning(self):
        models = self.many((("heavy", 9000),))  # needs ~9.9 GB of 16: more than half, less than 85 %
        with Server(self.files()) as srv:
            rc, out, err = self.setup_many(srv, models, "heavy", "--yes", "--no-config")
            self.assertEqual(rc, 0, out + err)
        self.assertIn("slow down a lot", out)
        self.assertIn("SLOW", out)
        self.assertEqual(self.installed(models), ["heavy"])

    def test_without_advice_the_plain_ram_rule_still_warns(self):
        models = self.many((("a", 400),))
        with mock.patch.dict(sys.modules, {"aihw": None}), mock.patch.object(aisetup, "memory_mb", return_value=(500, 400)), Server(self.files()) as srv:
            rc, out, err = self.setup_many(srv, models, "a", "--yes", "--no-config")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("swap or fail", out)

    def test_the_model_option_is_still_accepted(self):
        models = self.many()
        with Server(self.files()) as srv:
            rc, out, err = self.setup_many(srv, models, "--model", "b", "--yes", "--no-config")
            self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.installed(models), ["b"])

    def test_one_unpinned_model_among_several_refuses_all(self):
        models = self.many()
        models[1] = dict(models[1], ollama=None)
        with Server(self.files()) as srv:
            rc, out, err = self.run_setup(srv, "a", "b", "--yes", models=models, registry=False)
            self.assertEqual((rc, srv.requests), (1, []))
        self.assertIn("b: not pinned: ollama (the name to pull)", err)
        self.assertNotIn("a: not pinned", err)


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

    def test_gpu_key(self):
        err = io.StringIO()
        self.assertEqual(nuc_config.load("/nonexistent")["ai"]["gpu"], "auto", "the default: the GPU when the model fits there")
        for text, want in (("", "auto"), ("gpu =\n", "auto"), ("gpu = auto\n", "auto"), ("gpu = AUTO\n", "auto"), ("gpu = no\n", "no"), ("gpu = No\n", "no"),
                           ("gpu = off\n", "no"), ("gpu = false\n", "no"), ("gpu = cpu\n", "no"), ("gpu = yes\n", "auto")):
            with self.subTest(text), contextlib.redirect_stderr(err):
                put(self.cfg, "[ai]\nenabled = no\n" + text)
                self.assertEqual(nuc_config.load(self.cfg)["ai"]["gpu"], want)
        self.assertEqual(err.getvalue(), "", "valid spellings are quiet")
        put(self.cfg, "[ai]\ngpu = nvidia\nmodel = m\n")
        with contextlib.redirect_stderr(err):
            ai = nuc_config.load(self.cfg)["ai"]
        self.assertEqual((ai["gpu"], ai["model"]), ("auto", "m"), "a bad value keeps the default and does not touch the other keys")
        self.assertIn("[ai] gpu must be auto or no", err.getvalue())
        put(self.cfg, "[web]\nenabled = no\n")
        self.assertEqual(nuc_config.load(self.cfg)["ai"]["gpu"], "auto", "no [ai] section")

    def test_web_actions_key(self):
        self.assertIs(nuc_config.load("/nonexistent")["ai"]["web_actions"], True, "the default: the AI page and screen may act")
        err = io.StringIO()
        for text, want in (("", True), ("web_actions = yes\n", True), ("web_actions = no\n", False), ("web_actions = off\n", False), ("web_actions = 0\n", False),
                           ("web_actions = True\n", True)):
            with self.subTest(text), contextlib.redirect_stderr(err):
                put(self.cfg, "[ai]\nenabled = no\n" + text)
                self.assertIs(nuc_config.load(self.cfg)["ai"]["web_actions"], want)
        self.assertEqual(err.getvalue(), "")
        put(self.cfg, "[ai]\nweb_actions = maybe\nmodel = m\n")
        with contextlib.redirect_stderr(err):
            ai = nuc_config.load(self.cfg)["ai"]
        self.assertEqual((ai["web_actions"], ai["model"]), (True, "m"), "a bad value keeps the default and does not touch the other keys")
        self.assertIn("[ai] web_actions is not a boolean", err.getvalue())

    def test_set_key_writes_gpu_like_the_other_keys(self):
        put(self.cfg, "[ai]\n# comment\ngpu = auto\nmodel =\n")
        nuc_config.set_key(self.cfg, "ai", "gpu", "no")
        self.assertEqual(get(self.cfg), "[ai]\n# comment\ngpu = no\nmodel =\n")

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
        self.runtime, self.model = fo.fake_runtime("http://127.0.0.1:1")[0], fo.fake_model("tiny", 5000)

    def install(self, stamp=True):
        fo.install_runtime(self.d, self.runtime)
        fo.install_model(self.d, self.model)
        if not stamp:
            aisetup.forget(self.d, aisetup.runtime_key(self.runtime))

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
        self.assertIn("server    : Ollama %s: installed" % fo.VERSION, out)
        self.assertRegex(out, r"model     : tiny +Tiny test model +installed  \(active\)")
        self.assertIn("answering, /v1/models lists: tiny", out)
        self.assertIn("model = tiny", out)

    def test_an_ollama_with_no_model_yet_and_tags_are_read(self):
        s, _h = self.models_server((200, b'{"object": "list", "data": null}'))
        self.assertEqual(aisetup.probe(s.base + "/v1"), (True, [], ""), "Ollama answers null when it has no model")
        s, _h = self.models_server((200, b'{"data": [{"id": "tiny:latest"}, {"id": "qwen3:8b"}]}'))
        self.assertEqual(aisetup.probe(s.base + "/v1")[1], ["tiny", "qwen3:8b"], "the default tag is not part of the name [ai] model uses")
        rc, out, err = self.status(s.base + "/v1")
        self.assertEqual(rc, 0, out)

    def test_the_directory_line_says_what_is_in_it_and_what_is_free(self):
        self.install()
        rc, out, err = self.status("http://127.0.0.1:%d/v1" % free_port())
        line = next(ln for ln in out.splitlines() if ln.startswith("  directory :"))
        self.assertIn(self.d, line)
        self.assertIn("(%s downloaded, " % aisetup.fmt_size(aisetup.dir_space(self.d)["used"]), line)
        self.assertRegex(line, r"downloaded, [\d.]+ (GB|MB) free on that disk\)$")
        rc, out, err = self.status("http://127.0.0.1:%d/v1" % free_port(), "--dir", os.path.join(self.tmp.name, "empty"))
        self.assertRegex(next(ln for ln in out.splitlines() if ln.startswith("  directory :")), r"\(nothing downloaded, ")

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

    def test_without_a_directory_argument_it_looks_where_the_administrator_installed(self):
        """`sudo nuc-console-ai setup` puts the files in the system-wide folder; an unprivileged `status` must look there, like `models`."""
        self.install()  # in self.d, which stands for the system-wide folder
        mine = os.path.join(self.tmp.name, "mine")
        put(self.cfg, "[ai]\nendpoint = http://127.0.0.1:%d/v1\nmodel = tiny\n" % free_port())
        args = aisetup.build_parser().parse_args(["status", "--config", self.cfg])
        self.assertIsNone(args.dir)
        with mock.patch.object(aisetup, "find_dir", return_value=self.d) as fd, mock.patch.object(aisetup, "default_dir", return_value=mine):
            rc, out, err = call(aisetup.cmd_status, args, runtime=self.runtime, models=[self.model])
        fd.assert_called()
        self.assertIn("directory : %s" % self.d, out)
        self.assertIn("server    : Ollama %s: installed" % fo.VERSION, out)
        self.assertEqual(rc, 3, "installed, the server is not running")
        args = aisetup.build_parser().parse_args(["status", "--config", self.cfg, "--dir", mine])
        with mock.patch.object(aisetup, "find_dir", return_value=self.d):
            rc, out, err = call(aisetup.cmd_status, args, runtime=self.runtime, models=[self.model])
        self.assertIn("directory : %s" % mine, out, "an explicit --dir is kept")
        self.assertEqual(rc, 1)

    def test_endpoint_option_probes_another_url(self):
        s, httpd = self.models_server((200, b'{"data": [{"id": "tiny"}]}'))
        rc, out, err = self.status("http://127.0.0.1:1/v1", "--endpoint", s.base + "/v1")
        self.assertEqual(rc, 0)
        self.assertEqual(httpd.requests, ["/v1/models"])

    def test_a_build_not_unpacked_from_the_pinned_archive_does_not_count(self):
        self.install(stamp=False)
        rc, out, err = self.status("http://127.0.0.1:%d/v1" % free_port())
        self.assertIn("present but NOT verified", out)
        self.assertEqual(rc, 1, "a server that was never unpacked from the pinned archive does not count as installed")

    def test_verify_hashes_the_archive_and_every_layer(self):
        self.install()
        rc, out, err = self.status("http://127.0.0.1:%d/v1" % free_port(), "--verify")
        self.assertIn("installed, its archive SHA-256 verified", out)
        self.assertIn("installed, every layer SHA-256 verified", out)
        self.assertEqual(rc, 3)
        layers = aiollama.read_manifest(aisetup.model_path(self.d, self.model))
        with open(aiollama.blob_path(aiollama.models_dir(self.d), layers[1][0]), "r+b") as f:
            f.write(b"X")
        rc, out, err = self.status("http://127.0.0.1:%d/v1" % free_port(), "--verify")
        self.assertIn("present but INCOMPLETE (run nuc-console-ai setup tiny again)", out)

    def test_unpinned_entries_are_reported_as_such(self):
        rt = dict(self.runtime, assets={})
        put(self.cfg, "")
        args = aisetup.build_parser().parse_args(["status", "--dir", self.d, "--config", self.cfg, "--endpoint", "http://127.0.0.1:%d/v1" % free_port()])
        rc, out, err = call(aisetup.cmd_status, args, runtime=rt, models=[self.model])
        self.assertEqual(rc, 1)
        self.assertIn("not pinned for this system in this build", out)

    def test_only_installed_models_are_listed_and_the_active_one_is_marked(self):
        self.install()
        other = fo.fake_model("other")
        put(self.cfg, "[ai]\nenabled = yes\nendpoint = http://127.0.0.1:%d/v1\nmodel = tiny\n" % free_port())
        args = aisetup.build_parser().parse_args(["status", "--dir", self.d, "--config", self.cfg])
        rc, out, err = call(aisetup.cmd_status, args, runtime=self.runtime, models=[self.model, other, fo.fake_model("o2")])
        self.assertRegex(out, r"model     : tiny .*installed  \(active\)")
        self.assertNotIn("model     : other", out, "a catalog of a dozen models would bury the one that is there")
        self.assertIn("2 more in the catalog, not installed (nuc-console-ai models)", out)

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
        self.runtime, self.model = fo.fake_runtime("http://127.0.0.1:1")[0], fo.fake_model("tiny", 5000)
        self.cfg = os.path.join(self.tmp.name, "config.ini")
        advice(self, HW_CPU8)

    def install(self):
        fo.install_runtime(self.d, self.runtime)
        fo.install_model(self.d, self.model)

    def parse(self, *extra):
        return aisetup.build_parser().parse_args(["serve", "--dir", self.d, "--config", self.cfg] + list(extra))

    def test_install_service_checks_the_files_again_the_stamp_is_not_enough(self):
        """The web account may write the AI folder (the AI page downloads there): it can change the unpacked build or a layer, and the stamp
        would still say 'installed'. The service runs those files as another account: the archive is hashed against its pin and unpacked again,
        and every layer of the model is hashed against its name, first."""
        self.install()
        calls = []
        with mock.patch.object(aisetup, "install_service", side_effect=lambda *a, **k: calls.append((a, k))):
            rc, out, _e = call(aisetup.cmd_serve, self.parse("--install-service"), runtime=self.runtime, models=[self.model], hw=HW_CPU8)
        self.assertEqual((rc, len(calls)), (0, 1))
        self.assertIn("checking the SHA-256 of the files the service will run", out)
        exe = aisetup.runtime_path(self.d, self.runtime)
        put(exe, b"#!/bin/sh\necho changed\n")  # the unpacked build changed by the web account
        with mock.patch.object(aisetup, "install_service"):
            call(aisetup.cmd_serve, self.parse("--install-service"), runtime=self.runtime, models=[self.model], hw=HW_CPU8)
        self.assertEqual(get(exe, "rb"), fo.runtime_exe(), "unpacked again from the archive that hashes to its pin")
        for which in ("the server's archive", "model tiny"):
            with self.subTest(which):
                self.install()
                if "archive" in which:
                    path = aisetup.archive_path(self.d, self.runtime)
                else:
                    path = aiollama.blob_path(aiollama.models_dir(self.d), aiollama.read_manifest(aisetup.model_path(self.d, self.model))[1][0])
                st = os.stat(path)
                put(path, b"#" * st.st_size)                         # the same size,
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # the same date: the stamp cannot tell
                calls.clear()
                with mock.patch.object(aisetup, "install_service", side_effect=lambda *a, **k: calls.append(a)):
                    with self.assertRaises(aisetup.SetupError) as cm:
                        call(aisetup.cmd_serve, self.parse("--install-service"), runtime=self.runtime, models=[self.model], hw=HW_CPU8)
                self.assertEqual(calls, [], "nothing was installed")
                self.assertIn(which, str(cm.exception))
                self.assertIn("nothing was installed", str(cm.exception))

    def test_a_foreground_serve_does_not_hash_again(self):
        self.install()
        with mock.patch.object(aisetup, "sha256_file", side_effect=AssertionError("serve trusts the stamp")), \
                mock.patch.object(aiollama.hashlib, "sha256", side_effect=AssertionError("serve trusts the sizes")), \
                mock.patch.object(aisetup, "run_server", return_value=0):
            rc, out, _e = call(aisetup.cmd_serve, self.parse(), runtime=self.runtime, models=[self.model], hw=HW_CPU8)
        self.assertEqual(rc, 0)

    def test_serve_refuses_when_nothing_is_installed(self):
        with self.assertRaises(aisetup.SetupError) as cm:
            call(aisetup.cmd_serve, self.parse(), runtime=self.runtime, models=[self.model])
        self.assertIn("setup", str(cm.exception))

    def test_dry_run_prints_the_command_and_its_settings(self):
        self.install()
        rc, out, err = call(aisetup.cmd_serve, self.parse("--dry-run", "--port", "9191", "--threads", "3"), runtime=self.runtime, models=[self.model])
        self.assertEqual(rc, 0)
        self.assertIn("OLLAMA_HOST=127.0.0.1:9191", out)
        self.assertIn("OLLAMA_NO_CLOUD=1", out)
        self.assertIn("OLLAMA_MODELS=", out)
        self.assertTrue(out.rstrip().endswith("serve"), out)
        self.assertNotIn("0.0.0.0", out)

    def test_serve_starts_the_server_with_its_environment(self):
        self.install()
        with mock.patch.object(aisetup, "run_server", return_value=0) as rs:
            rc, out, err = call(aisetup.cmd_serve, self.parse("--port", "9191"), runtime=self.runtime, models=[self.model])
        self.assertEqual(rc, 0)
        argv, env = rs.call_args[0]
        self.assertEqual(argv, [aisetup.runtime_path(self.d, self.runtime), "serve"])
        self.assertEqual(env["OLLAMA_HOST"], "127.0.0.1:9191")
        self.assertEqual(env["OLLAMA_MODELS"], aiollama.models_dir(self.d))
        self.assertIn("http://127.0.0.1:9191/v1", out)
        self.assertIn("the advisor asks tiny", out)

    def test_configured_model_is_preferred_among_installed(self):
        self.install()
        put(self.cfg, "[ai]\nmodel = tiny\n")
        m = aisetup.choose_model(self.parse(), self.d, [self.model], self.runtime, "tiny")
        self.assertEqual(m["id"], "tiny")

    def test_port_must_be_a_port(self):
        for bad in ("0", "70000", "http", "8080; reboot"):
            with self.subTest(bad), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.parse("--port", bad)


class ModelsCommandTests(unittest.TestCase):
    """`nuc-console-ai models`: the hardware found, one row per model with its verdict, speed, installed / active / recommended."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")
        self.cfg = os.path.join(self.tmp.name, "config.ini")
        self.runtime, self.models = fake_catalog()

    def models_cmd(self, hw, models=None, ini=""):
        put(self.cfg, ini)
        args = aisetup.build_parser().parse_args(["models", "--dir", self.d, "--config", self.cfg])
        with mock.patch.dict(sys.modules, {"aihw": FakeAihw(hw)}):
            return call(aisetup.cmd_models, args, runtime=self.runtime, models=self.models if models is None else models, hw=hw)

    def row(self, out, model_id):
        return next(ln for ln in out.splitlines() if ln[2:].startswith(model_id + " "))

    def test_big_nvidia_gpu(self):
        rc, out, err = self.models_cmd(HW_BIG)
        self.assertEqual(rc, 0)
        self.assertIn("RTX 4090, 24.0 GB, 21.5 GB free, cuda", out)
        self.assertIn("RAM      : 64.0 GB, 58.6 GB free", out)
        self.assertIn("Test CPU 9000 (8 cores, 16 threads) avx2 avx512", out)
        self.assertRegex(self.row(out, "huge"), r"GPU\+CPU")  # 31 GB do not fit 22 GB of VRAM: partly in RAM
        for name in ("big", "mid", "small"):
            self.assertRegex(self.row(out, name), r"  GPU  ")
        self.assertTrue(self.row(out, "big").startswith("*"), "the best-ranked model that fits entirely is recommended")
        self.assertEqual(sum(ln.startswith("*") and ln[2:3] != "r" for ln in out.splitlines()), 1, "exactly one row is starred")
        self.assertIn("recommended for this machine: big: big needs", out)

    def test_small_gpu_on_windows(self):
        rc, out, err = self.models_cmd(HW_SMALL_GPU)
        self.assertIn("GTX 1650, 4.0 GB, 3.4 GB free, cuda", out)
        self.assertRegex(self.row(out, "huge"), "TOO BIG")
        self.assertRegex(self.row(out, "big"), "GPU\\+CPU")
        self.assertRegex(self.row(out, "small"), "  GPU  ")
        self.assertTrue(self.row(out, "small").startswith("*"), "big only fits in part: the recommendation is what fits entirely")

    def test_apple_unified_memory(self):
        rc, out, err = self.models_cmd(HW_APPLE)
        self.assertIn("GPU      : Apple M2, unified memory (it uses the RAM), metal", out)
        self.assertRegex(self.row(out, "huge"), "TOO BIG")
        self.assertRegex(self.row(out, "big"), "  GPU  ")
        self.assertTrue(self.row(out, "big").startswith("*"))

    def test_8gb_without_gpu(self):
        rc, out, err = self.models_cmd(HW_CPU8)
        self.assertIn("GPU      : none found: the CPU does the work", out)
        self.assertIn("note     : nvidia-smi not found", out, "what could not be read is said")
        self.assertRegex(self.row(out, "huge"), "TOO BIG")
        self.assertRegex(self.row(out, "big"), "TOO BIG")
        self.assertRegex(self.row(out, "mid"), "  RAM  ")
        self.assertTrue(self.row(out, "mid").startswith("*"))

    def test_unknown_ram_does_not_guess(self):
        rc, out, err = self.models_cmd(HW_UNKNOWN)
        self.assertEqual(rc, 0)
        self.assertIn("RAM      : unknown", out)
        self.assertIn("note     : /proc/meminfo could not be read", out)
        for name in ("huge", "big", "mid", "small"):
            self.assertNotRegex(self.row(out, name), "TOO BIG", "nothing is called too big on a guess")
        self.assertRegex(self.row(out, "mid"), r"SLOW\s+\?\s", "no speed estimate without a memory reading")

    def test_no_advice_at_all(self):
        with mock.patch.dict(sys.modules, {"aihw": None}):
            args = aisetup.build_parser().parse_args(["models", "--dir", self.d, "--config", self.cfg])
            put(self.cfg, "")
            rc, out, err = call(aisetup.cmd_models, args, runtime=self.runtime, models=self.models, hw={})
        self.assertEqual(rc, 0)
        self.assertIn("not detected", out)
        self.assertIn("could not be assessed", out)
        self.assertEqual(len(out.splitlines()) > 8, True)

    def test_a_speed_below_10_keeps_its_decimal(self):
        real = aisetup.assess_model
        for tok_s, want in (([0.5, 3.2], "0.5-3.2"), ([8.4, 17], "8.4-17"), ([120, 340], "120-340"), ([4.0, 4.0], "4.0"), ([12, 12], "12")):
            with self.subTest(tok_s), mock.patch.object(aisetup, "assess_model", side_effect=lambda m, hw, ctx=aisetup.DEFAULT_CTX: dict(real(m, hw, ctx), tok_s=list(tok_s))):
                rc, out, err = self.models_cmd(HW_CPU8)
                self.assertRegex(self.row(out, "mid"), r"\s%s\s" % re.escape(want))
                header = next(ln for ln in out.splitlines() if "TOK/S" in ln)
                self.assertEqual(self.row(out, "mid").index("not installed"), header.index("STATE"), "the columns stay aligned")

    def test_every_verdict_has_a_label_and_a_speed_where_there_is_one(self):
        seen = set()
        for hw in (HW_BIG, HW_SMALL_GPU, HW_APPLE, HW_CPU8, HW_UNKNOWN):
            rc, out, err = self.models_cmd(hw)
            for m in self.models:
                row = self.row(out, m["id"])
                fits = re.search(r"\s(GPU\+CPU|GPU|RAM|SLOW|TOO BIG)\s+(\S+)\s{2}", row)
                self.assertTrue(fits, row)
                seen.add(fits.group(1))
                if fits.group(2) != "?":  # below 10 tokens/s a decimal, from 10 on whole numbers
                    self.assertRegex(fits.group(2), r"^(\d\.\d|\d{2,})(-(\d\.\d|\d{2,}))?$", row)
        self.assertEqual(seen, {"GPU+CPU", "GPU", "RAM", "SLOW", "TOO BIG"})

    def test_without_advice_every_row_is_neutral(self):
        with mock.patch.dict(sys.modules, {"aihw": None}):
            args = aisetup.build_parser().parse_args(["models", "--dir", self.d, "--config", self.cfg])
            put(self.cfg, "")
            rc, out, err = call(aisetup.cmd_models, args, runtime=self.runtime, models=self.models, hw={})
        for m in self.models:
            row = self.row(out, m["id"])
            self.assertRegex(row, r"~\d+(\.\d)? (GB|MB)\s+\?\s+\?\s+(not pinned yet|not installed)$", "a neutral ? for the verdict and the speed: nothing is guessed")
            self.assertFalse(row.startswith("*"))
        self.assertNotRegex(out, r"TOO BIG\s+\?|SLOW\s+\?")

    def test_needs_agree_with_the_sentence_that_explains_the_verdict(self):
        """The NEEDS column and the "needs 5.7 GB" of the recommendation are one number, in the same unit as the RAM line."""
        real = aisetup.assess_model
        with mock.patch.object(aisetup, "assess_model", side_effect=lambda m, hw, ctx=aisetup.DEFAULT_CTX: dict(real(m, hw, ctx), need_mb=5888, why="needs 5.8 GB")):
            rc, out, err = self.models_cmd(HW_CPU8)
        self.assertRegex(self.row(out, "mid"), r"~5\.8 GB")
        self.assertEqual(aisetup.fmt_mem(5888), "5.8 GB")
        self.assertEqual(aisetup.fmt_mem(700), "700 MB")
        self.assertEqual(aisetup.fmt_mem(None), "?")

    def test_installed_active_and_unpinned(self):
        install_files(self.d, self.runtime, self.models, only=("mid",))
        models = [dict(m, ollama=None, size=None) if m["id"] == "huge" else m for m in self.models]
        rc, out, err = self.models_cmd(HW_CPU8, models=models, ini="[ai]\nmodel = mid\n")
        self.assertRegex(self.row(out, "mid"), r"installed, ACTIVE$")
        self.assertRegex(self.row(out, "small"), r"not installed$")
        self.assertRegex(self.row(out, "huge"), r"not pinned yet$")
        self.assertRegex(self.row(out, "mid"), r"\s5 KB\s", "a pinned model shows its real size")
        self.assertRegex(self.row(out, "huge"), r"~30\.0 GB", "an unpinned one the approximate size, marked with ~")
        rc, out, err = self.models_cmd(HW_CPU8, ini="[ai]\nmodel = small\n")
        self.assertRegex(self.row(out, "mid"), r"installed$")
        self.assertRegex(self.row(out, "small"), r"not installed$", "active but not installed: not shown as installed")

    def test_it_only_reads_so_an_unprivileged_user_can_run_it(self):
        install_files(self.d, self.runtime, self.models)
        stamp = get(aisetup.stamp_path(self.d))
        boom = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("models must only read"))  # noqa: E731
        with mock.patch.object(aisetup, "is_root", side_effect=boom), mock.patch.object(aisetup, "ensure_dirs", side_effect=boom), \
                mock.patch.object(aisetup, "record", side_effect=boom), mock.patch.object(aisetup, "forget", side_effect=boom), \
                mock.patch.object(aisetup, "download", side_effect=boom), mock.patch.object(aisetup, "sha256_file", side_effect=boom), \
                mock.patch.object(nuc_config, "set_key", side_effect=boom), mock.patch.object(os, "makedirs", side_effect=boom), \
                mock.patch.object(subprocess, "run", side_effect=boom), mock.patch.object(subprocess, "Popen", side_effect=boom):
            rc, out, err = self.models_cmd(HW_BIG)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(sorted(os.listdir(self.d)), ["models", "runtime", "verified.json"])
        self.assertEqual(get(aisetup.stamp_path(self.d)), stamp, "the stamp file is read, never written")
        self.assertRegex(self.row(out, "big"), "installed$")

    def test_without_a_directory_argument_it_looks_where_the_administrator_installed(self):
        system = os.path.join(self.tmp.name, "system-ai")
        install_files(system, self.runtime, self.models, only=("mid",))
        put(self.cfg, "")
        args = aisetup.build_parser().parse_args(["models", "--config", self.cfg])
        with mock.patch.object(aisetup, "find_dir", return_value=system), mock.patch.dict(sys.modules, {"aihw": FakeAihw(HW_CPU8)}):
            rc, out, err = call(aisetup.cmd_models, args, runtime=self.runtime, models=self.models, hw=HW_CPU8)
        self.assertRegex(self.row(out, "mid"), "installed")
        self.assertRegex(self.row(out, "small"), "not installed")

    def test_the_folder_the_files_go_to_is_said_with_what_it_holds_and_what_is_free(self):
        install_files(self.d, self.runtime, self.models, only=("mid",))
        rc, out, err = self.models_cmd(HW_CPU8)
        line = next(ln for ln in out.splitlines() if ln.startswith("  Models   :"))
        self.assertIn(self.d, line)
        self.assertRegex(line, r"\(\d+ KB downloaded, [\d.]+ (GB|MB) free on that disk\)$")
        self.assertLess(out.index(line), out.index("ID  "), "before the table: it is part of the machine")

    def test_commands_are_shown_and_output_is_ascii(self):
        rc, out, err = self.models_cmd(HW_BIG)
        how = (lambda c: "nuc-console-ai %s ID (in an administrator prompt)" % c) if sys.platform == "win32" else "sudo nuc-console-ai {} ID".format
        self.assertIn(how("setup"), out)
        self.assertIn(how("use"), out)
        self.assertTrue(out.isascii(), "Windows consoles")

    def test_the_real_catalog_on_every_machine(self):
        for name, hw in ALL_HW.items():
            with self.subTest(name):
                rc, out, err = self.models_cmd(hw, models=aisetup.MODELS)
                self.assertEqual(rc, 0)
                for m in aisetup.MODELS:
                    self.assertIn(m["id"], out)
                self.assertTrue(out.isascii())
                # measured with the system-wide folder of this OS in place of this test's temporary one, whose length is the runner's
                # (C:\Users\RUNNER~1\AppData\Local\Temp\tmp... on Windows, /var/folders/../T/tmp... on macOS, /tmp/tmp... on Linux)
                real = aisetup.default_dir(env={}, euid=0)
                self.assertLess(max(len(ln.replace(self.d, real)) for ln in out.splitlines()), 110, "fits a terminal")


class FormatTests(unittest.TestCase):
    def test_speed_text(self):
        t = lambda lo_hi: aisetup.speed_text({"tok_s": lo_hi})  # noqa: E731
        self.assertEqual(t([0.5, 3.2]), "0.5-3.2", "a range below 10 keeps its decimal: 0-3 would be wrong")
        self.assertEqual(t([8.4, 17]), "8.4-17")
        self.assertEqual(t([9.96, 41.2]), "10-41")
        self.assertEqual(t([37, 82]), "37-82")
        self.assertEqual(t([384, 1024]), "384-1024")
        self.assertEqual(t([4, 4]), "4.0")
        self.assertEqual(t([20, 20]), "20")
        self.assertEqual(t([0, 0.04]), "0.1", "never 0.0: it runs")
        for none in (None, [], {}, "x", [1], ["a", "b"], [float("nan"), 3], [1, float("inf")]):
            self.assertEqual(aisetup.speed_text({"tok_s": none}), "?", none)
        self.assertEqual(aisetup.speed_text(None), "?")
        self.assertEqual(aisetup.speed_text({}), "?")

    def test_gpu_text(self):
        self.assertEqual(aisetup.gpu_text(False), "CPU only")
        self.assertEqual(aisetup.gpu_text(True), "on the GPU when one holds the model (Ollama decides), else the CPU")


class RealAdviceTests(unittest.TestCase):
    """With the real aihw: the advice still says where a model fits, but the server decides for itself how many layers go to the GPU (it
    measures the free video memory every time it loads a model): serve and a service install only allow or hide the GPU."""

    def setUp(self):
        import aihw
        self.aihw = aihw
        patcher = mock.patch.dict(sys.modules, {"aihw": aihw})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.m = aisetup.find_model("qwen3-8b")  # 36 layers

    def test_the_advice_is_advice_and_the_gpu_is_allowed_whatever_it_says(self):
        self.assertEqual(self.aihw.assess(self.m, HW_BIG)["verdict"], "gpu")
        self.assertEqual(self.aihw.assess(self.m, HW_SMALL_GPU)["verdict"], "partial")
        for cfg_gpu in ("auto", "yes", ""):
            self.assertEqual(aisetup.gpu_plan(cfg_gpu), (True, ""))
        self.assertEqual(aisetup.gpu_plan("no"), (False, "[ai] gpu = no: CPU only"))

    def test_serve_and_a_service_install_say_where_it_runs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d, cfg = os.path.join(tmp.name, "ai"), os.path.join(tmp.name, "config.ini")
        runtime, models = fake_catalog()
        install_files(d, runtime, models)
        put(cfg, "")
        args = aisetup.build_parser().parse_args(["serve", "--dir", d, "--config", cfg, "--model", "mid"])
        with mock.patch.object(aisetup, "run_server", return_value=0) as rs:
            rc, out, err = call(aisetup.cmd_serve, args, runtime=runtime, models=models, hw=HW_BIG)
        self.assertIn("on the GPU when one holds the model (Ollama decides)", out)
        self.assertFalse(set(aiollama.GPU_HIDE) & set(rs.call_args[0][1]))
        service = aisetup.service_argv("/c", models[2], 8080, 4096, "linux", python="/usr/bin/python3", script="/opt/aisetup.py")
        self.assertNotIn("--cpu", service)
        service = aisetup.service_argv("/c", models[2], 8080, 4096, "linux", python="/usr/bin/python3", script="/opt/aisetup.py", gpu=False)
        self.assertEqual(service[-1], "--cpu")
        self.assertTrue(aisetup.build_parser().parse_args(service[3:]).cpu)


class CatalogTests(unittest.TestCase):
    """catalog(): what the screens show. Never downloads, never hashes, never raises."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")
        self.runtime, self.models = fake_catalog()
        self.cfg = {"ai": {"model": "mid"}}

    def cat(self, hw, **kw):
        kw.setdefault("cfg", self.cfg)
        with mock.patch.dict(sys.modules, {"aihw": FakeAihw(hw)}):
            return aisetup.catalog(hw, self.d, models=self.models, runtime=self.runtime, **kw)

    def test_shape(self):
        install_files(self.d, self.runtime, self.models, only=("mid", "small"))
        c = self.cat(HW_CPU8)
        self.assertEqual(set(c), {"hw", "dir", "space", "runtime", "recommended", "active", "models"})
        self.assertEqual((c["hw"], c["dir"]), (HW_CPU8, self.d))
        self.assertEqual(c["runtime"], {"installed": True, "name": "Ollama", "version": fo.VERSION})
        self.assertEqual((c["recommended"], c["active"]), ("mid", "mid"))
        self.assertEqual([m["id"] for m in c["models"]], ["huge", "big", "mid", "small"], "the catalog's order: best first")
        for m in c["models"]:
            for key in ("id", "name", "license", "rank", "approx_mb", "notes", "layers", "params_b", "ctx_max"):
                self.assertIn(key, m, "the model's own fields are kept")
            self.assertEqual(set(m["assess"]), {"verdict", "where", "need_mb", "gpu_layers", "tok_s", "why"})
            self.assertEqual(set(m["commands"]), {"install", "use", "remove"})
            self.assertTrue(m["pinned"])
        self.assertEqual({m["id"]: m["installed"] for m in c["models"]}, {"huge": False, "big": False, "mid": True, "small": True})
        self.assertEqual([m["assess"]["verdict"] for m in c["models"]], ["no", "no", "ram", "ram"])
        json.dumps(c)  # plain data: the web view can serialise it

    def test_runtime_not_installed_and_unpinned_model(self):
        self.models[0] = dict(self.models[0], ollama=None, size=None)
        c = self.cat(HW_CPU8)
        self.assertEqual(c["runtime"]["installed"], False)
        self.assertEqual([m["pinned"] for m in c["models"]], [False, True, True, True])

    def test_commands_per_system(self):
        c = self.cat(HW_BIG, plat="linux")
        self.assertEqual(c["models"][2]["commands"], {"install": "sudo nuc-console-ai setup mid", "use": "sudo nuc-console-ai use mid",
                                                      "remove": "sudo nuc-console-ai remove mid"})
        w = self.cat(HW_BIG, plat="win32")
        self.assertEqual(w["models"][2]["commands"]["install"], "nuc-console-ai setup mid (in an administrator prompt)")
        for c in w["models"]:
            self.assertNotIn("sudo", " ".join(c["commands"].values()))
        self.assertTrue(self.cat(HW_APPLE, plat="darwin")["models"][0]["commands"]["use"].startswith("sudo "))

    def test_every_machine(self):
        want = {"big nvidia": "big", "small gpu": "small", "apple 16 GB": "big", "8 GB cpu": "mid", "unknown ram": "small"}
        for name, hw in ALL_HW.items():
            with self.subTest(name):
                self.assertEqual(self.cat(hw)["recommended"], want[name])
        self.assertEqual(self.cat(HW_BIG)["models"][0]["assess"]["verdict"], "partial")
        self.assertGreater(self.cat(HW_BIG)["models"][0]["assess"]["gpu_layers"], 0)
        self.assertEqual(self.cat(HW_UNKNOWN)["models"][2]["assess"]["tok_s"], None)

    def test_nothing_fits(self):
        c = self.cat(hw_of(512, 300))
        self.assertIsNone(c["recommended"])
        self.assertEqual({m["assess"]["verdict"] for m in c["models"]}, {"no"})

    def test_never_downloads_never_hashes(self):
        install_files(self.d, self.runtime, self.models)
        with mock.patch.object(urllib_request(), "urlopen", side_effect=AssertionError("network")), \
                mock.patch.object(aisetup, "sha256_file", side_effect=AssertionError("hashing")), \
                mock.patch.object(aiollama.hashlib, "sha256", side_effect=AssertionError("hashing")), \
                mock.patch.object(aisetup, "download", side_effect=AssertionError("download")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no server is started")):
            c = self.cat(HW_BIG)
        self.assertTrue(all(m["installed"] for m in c["models"]), "the stamp and the manifests are what count")

    def test_a_model_with_a_missing_layer_is_not_installed(self):
        install_files(self.d, self.runtime, self.models)
        layers = aiollama.read_manifest(aisetup.model_path(self.d, self.models[2]))
        os.unlink(aiollama.blob_path(aiollama.models_dir(self.d), layers[1][0]))
        self.assertEqual({m["id"]: m["installed"] for m in self.cat(HW_BIG)["models"]}["mid"], False)

    def test_the_hardware_argument_wins_over_the_detected_one(self):
        with mock.patch.dict(sys.modules, {"aihw": FakeAihw(HW_CPU8)}):  # the machine running this has 8 GB, the demo shows another
            c = aisetup.catalog(HW_BIG, self.d, models=self.models, runtime=self.runtime, cfg=self.cfg)
            self.assertEqual(c["hw"], HW_BIG)
            self.assertEqual(c["models"][1]["assess"]["verdict"], "gpu")
            self.assertEqual(aisetup.catalog(None, self.d, models=self.models, runtime=self.runtime, cfg=self.cfg)["hw"], HW_CPU8)

    def test_without_aihw_it_still_answers(self):
        with mock.patch.dict(sys.modules, {"aihw": None}):
            c = aisetup.catalog(None, self.d, models=self.models, runtime=self.runtime, cfg=self.cfg)
        self.assertEqual(c["hw"], {})
        self.assertIsNone(c["recommended"])
        self.assertEqual({m["assess"]["verdict"] for m in c["models"]}, {"unknown"})
        self.assertTrue(all(m["assess"]["why"] for m in c["models"]))

    def test_a_failing_assessment_does_not_break_the_screen(self):
        class Broken(FakeAihw):
            def assess(self, model, hw, ctx=4096):
                raise ZeroDivisionError("bug")

            def recommend(self, models, hw):
                raise ValueError("bug")

            def cached(self, max_age=300):
                raise OSError("bug")
        with mock.patch.dict(sys.modules, {"aihw": Broken(HW_BIG)}):
            c = aisetup.catalog(None, self.d, models=self.models, runtime=self.runtime, cfg=self.cfg)
            c2 = aisetup.catalog(HW_BIG, self.d, models=self.models, runtime=self.runtime, cfg=self.cfg)
        self.assertEqual(c["hw"], {})
        self.assertEqual({m["assess"]["verdict"] for m in c2["models"]}, {"unknown"})
        self.assertIsNone(c2["recommended"])

    def test_active_comes_from_the_config(self):
        self.assertEqual(self.cat(HW_BIG, cfg={"ai": {"model": ""}})["active"], None)
        self.assertEqual(self.cat(HW_BIG, cfg={"ai": {"model": "llama3"}})["active"], "llama3", "whatever [ai] model says")
        cfg = os.path.join(self.tmp.name, "c.ini")
        put(cfg, "[ai]\nmodel = big\n")
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": cfg}):
            self.assertEqual(self.cat(HW_BIG, cfg=None)["active"], "big")

    def test_a_directory_that_does_not_exist_is_just_empty(self):
        c = self.cat(HW_CPU8)
        self.assertFalse(c["runtime"]["installed"])
        self.assertFalse(any(m["installed"] for m in c["models"]))
        self.assertFalse(os.path.exists(self.d), "looking does not create anything")

    def test_the_real_catalog(self):
        for name, hw in ALL_HW.items():
            with self.subTest(name), mock.patch.dict(sys.modules, {"aihw": FakeAihw(hw)}):
                c = aisetup.catalog(hw, self.d, cfg={"ai": {"model": ""}})
                self.assertEqual([m["id"] for m in c["models"]], [m["id"] for m in aisetup.MODELS])
                self.assertIn(c["recommended"], [None] + [m["id"] for m in aisetup.MODELS])
                self.assertFalse(any(m["installed"] for m in c["models"]), "an empty directory: nothing installed")
                for m in c["models"]:  # pinned exactly when the manifest has every value a download needs
                    self.assertEqual(m["pinned"], not aisetup.missing_pins(aisetup.find_model(m["id"]), True), m["id"])

    def test_find_dir_prefers_the_system_wide_directory(self):
        system, own = os.path.join(self.tmp.name, "sys"), os.path.join(self.tmp.name, "own")
        for d in (system, own):
            os.makedirs(d)

        def fake_default(plat=None, env=None, euid=None, home=None):
            return system if euid == 0 else own
        with mock.patch.object(aisetup, "default_dir", side_effect=fake_default):
            self.assertEqual(aisetup.find_dir(), own, "nothing installed anywhere: the caller's own")
            put(aisetup.stamp_path(own), "{}")
            self.assertEqual(aisetup.find_dir(), own)
            put(aisetup.stamp_path(system), "{}")
            self.assertEqual(aisetup.find_dir(), system, "what the administrator installed with sudo is what the screen shows")


class UseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")
        self.cfg = os.path.join(self.tmp.name, "config.ini")
        self.runtime, self.models = fake_catalog()
        advice(self, HW_CPU8)

    def use(self, *argv):
        args = aisetup.build_parser().parse_args(["use", "--dir", self.d, "--config", self.cfg] + list(argv))
        return call(aisetup.cmd_use, args, runtime=self.runtime, models=self.models)

    def test_writes_ai_model_and_says_nothing_has_to_restart(self):
        install_files(self.d, self.runtime, self.models)
        put(self.cfg, "# mine\n[web]\nenabled = no\n\n[ai]\n# the model\nenabled = yes\nendpoint = http://127.0.0.1:8080/v1\nmodel = small\n\n[features]\nhealth = yes\n")
        before = get(self.cfg)
        rc, out, err = self.use("mid")
        self.assertEqual(rc, 0, err)
        text = get(self.cfg)
        self.assertEqual(text, before.replace("model = small", "model = mid"), "only that line changed")
        self.assertIn("# mine", text)
        self.assertIn("# the model", text)
        self.assertEqual(nuc_config.load(self.cfg)["ai"]["model"], "mid")
        self.assertEqual(nuc_config.load(self.cfg)["features"]["health"], True)
        self.assertIn("the advisor asks mid from its next question, nothing to restart", out)
        self.assertNotIn("Ollama you run yourself", out)
        self.assertNotIn("enabled = no", out)

    def test_creates_the_section_and_notes_what_is_not_set_up(self):
        install_files(self.d, self.runtime, self.models)
        rc, out, err = self.use("small")
        self.assertEqual(rc, 0)
        ai = nuc_config.load(self.cfg)["ai"]
        self.assertEqual((ai["model"], ai["enabled"]), ("small", False), "the advisor is not switched on behind the admin's back")
        self.assertIn("the default of an Ollama you run yourself (port 11434)", out)
        self.assertIn("http://127.0.0.1:8080/v1", out)
        self.assertIn("[ai] enabled = no", out)

    def test_only_installed_models(self):
        install_files(self.d, self.runtime, self.models, only=("mid",))
        rc, out, err = self.use("mid")
        self.assertEqual(rc, 0)
        before = get(self.cfg)
        with self.assertRaises(aisetup.SetupError) as cm:
            self.use("small")  # in the catalog, not on disk
        self.assertIn("not installed", str(cm.exception))
        self.assertIn("nuc-console-ai setup small", str(cm.exception))
        with self.assertRaises(aisetup.SetupError) as cm:
            self.use("gpt-9")
        self.assertIn("unknown model", str(cm.exception))
        os.makedirs(os.path.dirname(aisetup.model_path(self.d, self.models[3])))
        put(aisetup.model_path(self.d, self.models[3]), '{"config": {}, "layers": [{"digest": "sha256:%s", "size": 9}]}' % ("0" * 64))
        with self.assertRaises(aisetup.SetupError):  # a manifest whose layers are not there
            self.use("small")
        self.assertEqual(get(self.cfg), before, "a refusal changes nothing")

    def test_the_server_must_be_installed_too(self):
        install_files(self.d, self.runtime, self.models)
        os.unlink(aisetup.runtime_path(self.d, self.runtime))
        with self.assertRaises(aisetup.SetupError):
            self.use("mid")
        self.assertFalse(os.path.exists(self.cfg))

    def test_a_model_that_will_not_work_needs_force(self):
        install_files(self.d, self.runtime, self.models)
        with self.assertRaises(aisetup.SetupError) as cm:
            self.use("huge")
        self.assertIn("will not work on this machine", str(cm.exception))
        self.assertIn("--force", str(cm.exception))
        self.assertFalse(os.path.exists(self.cfg))
        rc, out, err = self.use("huge", "--force")
        self.assertEqual(rc, 0)
        self.assertIn("will not work here", out)
        self.assertEqual(nuc_config.load(self.cfg)["ai"]["model"], "huge")

    def test_a_slow_model_is_used_with_a_warning(self):
        self.models[2] = dict(self.models[2], approx_mb=3500)  # ~4.4 GB of 8: more than half of the RAM, less than 85 %
        install_files(self.d, self.runtime, self.models)
        rc, out, err = self.use("mid")
        self.assertEqual(rc, 0, err)
        self.assertIn("will slow the PC down a lot", out)
        self.assertEqual(nuc_config.load(self.cfg)["ai"]["model"], "mid")

    def test_unwritable_config_is_one_line(self):
        install_files(self.d, self.runtime, self.models)
        self.cfg = os.path.join(self.tmp.name, "no", "such", "dir", "config.ini")
        with self.assertRaises(aisetup.SetupError) as cm:
            self.use("mid")
        self.assertIn("model = mid", str(cm.exception))
        self.assertNotIn("\n", str(cm.exception))

class ServeGpuTests(unittest.TestCase):
    """serve: Ollama uses the GPU when it finds one that holds the model (it decides how many layers fit); [ai] gpu = no or --cpu hide the
    GPUs from it, and the service carries that decision in its command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "ai")
        self.cfg = os.path.join(self.tmp.name, "config.ini")
        self.runtime, self.models = fake_catalog()
        install_files(self.d, self.runtime, self.models)
        self.big = self.models[1]

    def dry_run(self, hw, *extra, ini="", model="big"):
        put(self.cfg, ini)
        args = aisetup.build_parser().parse_args(["serve", "--dir", self.d, "--config", self.cfg, "--model", model, "--dry-run"] + list(extra))
        with mock.patch.dict(sys.modules, {"aihw": FakeAihw(hw)}):
            rc, out, err = call(aisetup.cmd_serve, args, runtime=self.runtime, models=self.models)
        self.assertEqual(rc, 0, err)
        return out

    def hidden(self, out):
        return all("%s=-1" % k in out for k in aiollama.GPU_HIDE)

    def test_the_gpu_is_allowed_on_every_machine_unless_told_otherwise(self):
        for name, hw in ALL_HW.items():
            with self.subTest(name):
                self.assertFalse(self.hidden(self.dry_run(hw)), "Ollama measures the GPU itself: nothing hidden")
                self.assertFalse(any(k in self.dry_run(hw, model="huge") for k in aiollama.GPU_HIDE), "even for a model the advice says does not fit")

    def test_config_gpu_no_hides_the_gpus(self):
        self.assertTrue(self.hidden(self.dry_run(HW_BIG, ini="[ai]\ngpu = no\n")))
        self.assertFalse(self.hidden(self.dry_run(HW_BIG, ini="[ai]\ngpu = auto\n")))

    def test_config_gpu_no_does_not_even_detect_the_hardware(self):
        put(self.cfg, "[ai]\ngpu = no\n")
        args = aisetup.build_parser().parse_args(["serve", "--dir", self.d, "--config", self.cfg, "--model", "big", "--dry-run"])
        with mock.patch.object(aisetup, "_hardware", side_effect=AssertionError("detected")):
            rc, out, err = call(aisetup.cmd_serve, args, runtime=self.runtime, models=self.models)
        self.assertEqual(rc, 0)

    def test_cpu_option_and_the_older_services_options(self):
        self.assertTrue(self.hidden(self.dry_run(HW_BIG, "--cpu")))
        self.assertTrue(self.hidden(self.dry_run(HW_BIG, "--gpu-layers", "0")), "an older service's CPU only")
        self.assertFalse(self.hidden(self.dry_run(HW_CPU8, "--gpu-layers", "12", "--threads", "3")), "an older service's GPU: allowed")
        for bad in ("-1", "1000", "many"):
            with self.subTest(bad), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                aisetup.build_parser().parse_args(["serve", "--gpu-layers", bad])

    def test_start_message_says_where_it_runs(self):
        put(self.cfg, "")
        for ini, want in (("", "on the GPU when one holds the model"), ("[ai]\ngpu = no\n", "CPU only")):
            put(self.cfg, ini)
            args = aisetup.build_parser().parse_args(["serve", "--dir", self.d, "--config", self.cfg, "--model", "mid"])
            with mock.patch.dict(sys.modules, {"aihw": FakeAihw(HW_BIG)}), mock.patch.object(aisetup, "run_server", return_value=0):
                rc, out, err = call(aisetup.cmd_serve, args, runtime=self.runtime, models=self.models)
            self.assertIn(want, out)
        self.assertIn("[ai] gpu = no: CPU only", out, "the reason is printed")

    def test_context_beyond_what_the_model_handles_is_refused(self):
        put(self.cfg, "")
        args = aisetup.build_parser().parse_args(["serve", "--dir", self.d, "--config", self.cfg, "--model", "mid", "--ctx", "65536", "--dry-run"])
        with self.assertRaises(aisetup.SetupError) as cm:
            call(aisetup.cmd_serve, args, runtime=self.runtime, models=self.models)
        self.assertIn("--ctx 32768", str(cm.exception))

    def test_the_service_has_the_decision_in_its_command(self):
        """The service reads no config of ours: the unit's command is what `serve` was told, and `serve` accepts it."""
        for gpu in (False, True):
            with self.subTest(gpu):
                argv = aisetup.service_argv(self.d, self.big, 8080, 4096, "linux", python="/usr/bin/python3", script="/opt/aisetup.py", gpu=gpu)
                self.assertEqual(argv[-1] == "--cpu", not gpu)
                args = aisetup.build_parser().parse_args(argv[3:] + ["--dry-run", "--config", os.path.join(self.tmp.name, "none.ini")])
                with mock.patch.object(aisetup, "_hardware", side_effect=AssertionError("the service does not detect: it was told")):
                    rc, out, err = call(aisetup.cmd_serve, args, runtime=self.runtime, models=self.models)
                self.assertEqual(self.hidden(out), not gpu)
        w = aisetup.service_argv(r"C:\PD\nuc-console\ai", self.big, 8080, 4096, "win32", python=r"C:\py\python.exe", script=r"C:\a\aisetup.py", gpu=False)
        self.assertIn("--cpu", w)
        self.assertEqual(w[-2], "--log")

    def test_install_linux_with_a_gpu(self):
        m, ran, wrote = self.big, [], {}
        with mock.patch.object(aisetup, "is_root", return_value=True), mock.patch.object(aisetup, "_linux_user"), \
                mock.patch.object(aisetup, "_gpu_groups", return_value=["render", "video"]), mock.patch.object(aisetup, "_readable_by_all", return_value=True), \
                mock.patch.object(aisetup, "_write_root_file", side_effect=lambda p, data, mode=0o644: wrote.update({p: data})), \
                mock.patch.object(aisetup, "tool", side_effect=lambda n, plat=None: "/usr/bin/" + n), \
                mock.patch.object(aisetup, "run", side_effect=lambda argv, check=True: ran.append(argv)), \
                mock.patch.object(aisetup, "_is_win", return_value=False), mock.patch.object(aisetup, "_is_mac", return_value=False):
            _r, out, _e = call(aisetup.install_service, self.d, m, 8080, 4096, "linux", gpu=True)
            unit = wrote["/etc/systemd/system/nuc-console-ai.service"]
            self.assertNotIn("--cpu", unit)
            self.assertIn("PrivateDevices=no\n", unit, "the sandbox must let the service see the GPU")
            self.assertIn("SupplementaryGroups=render video\n", unit)
            self.assertIn("on the GPU when one holds the model", out)
            wrote.clear()
            call(aisetup.install_service, self.d, m, 8080, 4096, "linux", gpu=False)
            unit = wrote["/etc/systemd/system/nuc-console-ai.service"]
            self.assertIn("--cpu\n", unit)
            self.assertIn("PrivateDevices=yes\n", unit, "CPU only: the sandbox keeps its private /dev")
            self.assertNotIn("SupplementaryGroups", unit)

    def test_install_on_windows_stops_a_running_task_first(self):
        """schtasks /Run does not start a second instance: a task that is still running would keep serving the old model."""
        calls = []
        record = lambda argv, *a, **kw: calls.append(list(argv))  # noqa: E731
        with mock.patch.object(aisetup, "is_root", return_value=True), mock.patch.object(aisetup, "tool", side_effect=lambda n, plat=None: n), \
                mock.patch.object(subprocess, "run", side_effect=record), mock.patch.object(aisetup, "run", side_effect=lambda argv, check=True: record(argv)), \
                mock.patch.object(os, "makedirs"):
            call(aisetup.install_service, r"C:\PD\nuc-console\ai", self.big, 8080, 4096, "win32", gpu=True)
        verbs = [c[1] for c in calls if c[0] == "schtasks"]
        self.assertEqual(verbs, ["/End", "/Create", "/Run"])


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
        self.assertIn("Description=nuc-console local AI model server (Ollama, 127.0.0.1 only)\n", unit)
        self.assertIn("TasksMax=512\n", unit, "the server and its runner threads")
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

    def test_systemd_unit_for_a_gpu(self):
        argv = ["/usr/bin/python3", "-B", "/opt/aisetup.py", "serve"]
        cpu, gpu = aisetup.systemd_unit(argv, 3600), aisetup.systemd_unit(argv, 3600, gpu=True, groups=["render", "video"])
        self.assertIn("PrivateDevices=yes\n", cpu)
        self.assertNotIn("SupplementaryGroups", cpu)
        self.assertIn("PrivateDevices=no\n", gpu)
        self.assertNotIn("PrivateDevices=yes", gpu)
        self.assertIn("SupplementaryGroups=render video\n", gpu)
        self.assertNotIn("SupplementaryGroups", aisetup.systemd_unit(argv, 3600, gpu=True), "no such group on this machine: none is asked for")
        drop = lambda u: [ln for ln in u.splitlines() if not ln.startswith(("PrivateDevices", "SupplementaryGroups"))]  # noqa: E731
        self.assertEqual(drop(cpu), drop(gpu), "everything else of the sandbox is the same")
        for line in ("User=nuc-console-ai", "NoNewPrivileges=yes", "ProtectSystem=strict", "CapabilityBoundingSet="):
            self.assertIn(line + "\n", gpu)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not installed")
    def test_systemd_analyze_accepts_the_unit(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nuc-console-ai.service")
            put(p, aisetup.systemd_unit(["/bin/true", "serve"], 2000, gpu=True, groups=["root"]))
            r = subprocess.run(["systemd-analyze", "verify", "--man=no", p], capture_output=True, text=True)
        bad = [ln for ln in (r.stdout + r.stderr).splitlines()  # only about this unit: the runner's own units have their warnings
               if "nuc-console-ai.service" in ln and re.search(r"Unknown (key|section)|Invalid|Failed to parse|Unknown lvalue", ln)]
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
        argv = aisetup.service_argv("/var/lib/nuc-console/ai", m, 8080, 4096, "linux", python="/usr/bin/python3", script="/opt/nuc-console/aisetup.py")
        self.assertEqual(argv, ["/usr/bin/python3", "-B", "/opt/nuc-console/aisetup.py", "serve", "--dir", "/var/lib/nuc-console/ai", "--model", m["id"],
                                "--port", "8080", "--ctx", "4096"])
        w = aisetup.service_argv(r"C:\PD\nuc-console\ai", m, 8080, 4096, "win32", python=r"C:\py\python.exe", script=r"C:\app\aisetup.py")
        self.assertEqual(w[-2], "--log")
        self.assertTrue(w[-1].endswith("ai.log"))

    @unittest.skipUnless(os.name == "posix", "Unix permissions")
    def test_readable_by_all(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            f = os.path.join(d, "m.gguf")
            put(f, "x")
            os.chmod(f, 0o644)
            self.assertTrue(aisetup._readable_by_all(f, stop=d))  # (above d: the system's temp dir, 0700 on macOS)
            os.chmod(f, 0o600)
            self.assertFalse(aisetup._readable_by_all(f, stop=d))
            os.chmod(f, 0o644)
            os.chmod(d, 0o700)
            self.assertFalse(aisetup._readable_by_all(f, stop=d))
            os.chmod(d, 0o755)

    def test_install_needs_root(self):
        m = aisetup.MODELS[0]
        with mock.patch.object(aisetup, "is_root", return_value=False), self.assertRaises(aisetup.SetupError) as cm:
            aisetup.install_service("/var/lib/nuc-console/ai", m, 8080, 4096, "linux")
        self.assertIn("root", str(cm.exception))
        with mock.patch.object(aisetup, "is_root", return_value=False), self.assertRaises(aisetup.SetupError):
            aisetup.remove_service("linux")

    @unittest.skipUnless(os.name == "posix", "Unix permissions")
    def test_install_linux_steps(self):
        """The Linux path end to end with every system call replaced: what would be written and run."""
        m, ran, wrote = aisetup.MODELS[0], [], {}
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            for p in (aisetup.runtime_path(d, plat="linux"), aisetup.model_path(d, m, "linux")):
                os.makedirs(os.path.dirname(p), mode=0o755)
                put(p, "x")
                os.chmod(p, 0o644)
            with mock.patch.object(aisetup, "is_root", return_value=True), mock.patch.object(aisetup, "_linux_user") as user, \
                    mock.patch.object(aisetup, "_write_root_file", side_effect=lambda p, data, mode=0o644: wrote.update({p: data})), \
                    mock.patch.object(aisetup, "tool", side_effect=lambda n, plat=None: "/usr/bin/" + n), \
                    mock.patch.object(aisetup, "run", side_effect=lambda argv, check=True: ran.append(argv)), \
                    mock.patch.object(aisetup, "_is_win", return_value=False), mock.patch.object(aisetup, "_is_mac", return_value=False), \
                    mock.patch.object(aisetup, "_readable_by_all", return_value=True):  # (the temp dir's parents: 0700 on macOS)
                _r, out, _e = call(aisetup.install_service, d, m, 8080, 4096, "linux")
            argv = aisetup.service_argv(d, m, 8080, 4096, "linux")
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
        self.runtime = fo.fake_runtime("http://127.0.0.1:1")[0]
        self.model, self.other = fo.fake_model("tiny", 5000), fo.fake_model("other", 7000)
        fo.install_runtime(self.d, self.runtime)
        fo.install_model(self.d, self.model)
        fo.install_model(self.d, self.other)
        put(os.path.join(self.d, "web.json"), "{}")

    def remove(self, *extra, ask=None):
        args = aisetup.build_parser().parse_args(["remove", "--dir", self.d] + list(extra))
        return call(aisetup.cmd_remove, args, runtime=self.runtime, models=[self.model, self.other], **({"ask": ask} if ask else {}))

    def blobs(self):
        return sorted(os.listdir(os.path.join(self.d, "models", "blobs")))

    def test_one_model(self):
        before = self.blobs()
        rc, out, err = self.remove("--model", "tiny", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.model)))
        self.assertFalse(aisetup.model_ready(self.d, self.model))
        self.assertTrue(aisetup.model_ready(self.d, self.other), "the other model and the layers it uses are kept")
        self.assertTrue(aisetup.runtime_ready(self.d, self.runtime))
        self.assertEqual(len(self.blobs()), len(before) - 2, "tiny's two layers; the config layer both use stays")
        self.assertIn("deleted, ", out)

    def test_the_model_as_a_plain_argument(self):
        rc, out, err = self.remove("tiny", "--yes")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.model)))
        self.assertTrue(os.path.exists(aisetup.model_path(self.d, self.other)))
        with self.assertRaises(aisetup.SetupError) as cm:
            self.remove("gpt-9", "--yes")
        self.assertIn("unknown model", str(cm.exception))

    def test_removing_the_active_model_says_so(self):
        cfg = os.path.join(self.d, "..", "config.ini")
        put(cfg, "[ai]\nmodel = tiny\n")
        rc, out, err = self.remove("tiny", "--yes", "--config", cfg)
        self.assertEqual(rc, 0)
        self.assertIn("active model", out)
        self.assertIn("nuc-console-ai use ID", out)
        fo.install_model(self.d, self.model)
        put(cfg, "[ai]\nmodel = other\n")
        rc, out, err = self.remove("--model", "tiny", "--yes", "--config", cfg)
        self.assertNotIn("active model", out)

    def test_everything_means_the_server_and_every_model_and_nothing_else(self):
        os.makedirs(os.path.join(self.d, "home", ".ollama"))
        put(os.path.join(self.d, "home", ".ollama", "id_ed25519"), "key")
        rc, out, err = self.remove("--yes")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(os.listdir(self.d)), ["verified.json", "web.json"], "the folders runtime/, models/ and home/ are gone, the rest stays")
        self.assertFalse(aisetup.runtime_ready(self.d, self.runtime))
        self.assertEqual(aisetup.load_stamp(self.d), {}, "the stamp forgets the archive and the unpacked build")
        self.assertIn("runtime/ (everything in it)", out)
        self.assertIn("models/ (everything in it)", out)

    def test_declined(self):
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            rc, out, err = self.remove("--model", "tiny")
        self.assertEqual(rc, 1)
        self.assertTrue(aisetup.model_ready(self.d, self.model))

    def test_nothing_to_remove(self):
        shutil.rmtree(self.d)
        rc, out, err = self.remove("--yes")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to remove", out)
        rc, out, err = self.remove("tiny", "--yes")
        self.assertIn("nothing to remove", out)


class PinsTests(unittest.TestCase):
    def test_registry_manifest(self):
        man = {"schemaVersion": 2, "config": {"digest": "sha256:" + "a" * 64, "size": 500},
               "layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + "b" * 64, "size": 5_000_000_000},
                          {"mediaType": "application/vnd.ollama.image.license", "digest": "sha256:" + "c" * 64, "size": 11_000},
                          {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:" + "d" * 64, "size": 1_000}]}
        self.assertEqual(aiollama.manifest_summary(man), {"size": 5_000_012_500, "model": 5_000_000_000, "license": "sha256:" + "c" * 64})
        with self.assertRaises(aisetup.SetupError):
            aiollama.manifest_summary({"layers": []})
        self.assertEqual(aiollama.manifest_url("qwen3:8b"), "https://registry.ollama.ai/v2/library/qwen3/manifests/8b")
        self.assertEqual(aiollama.manifest_url("hf.co/u/r:Q4_K_M"), "https://hf.co/v2/u/r/manifests/Q4_K_M")
        self.assertEqual(aiollama.blob_url("qwen3:8b", "sha256:" + "c" * 64), "https://registry.ollama.ai/v2/library/qwen3/blobs/sha256:" + "c" * 64)
        self.assertEqual(aisetup.licence_words("                                 Apache License\n                           Version 2.0, January 2004"),
                         "Apache License Version 2.0, January 2004")

    def test_the_release_sums_and_the_token_only_for_the_api(self):
        text = "%s  ./ollama-darwin.tgz\n%s  ./ollama-windows-amd64.zip\nnot a line\n%s *install.sh\n" % ("a" * 64, "b" * 64, "c" * 64)
        self.assertEqual(aisetup.sums_of(text), {"ollama-darwin.tgz": "a" * 64, "ollama-windows-amd64.zip": "b" * 64, "install.sh": "c" * 64})
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t0k"}):
            self.assertEqual(aisetup._headers("https://api.github.com/repos/x", "a")["Authorization"], "Bearer t0k")
            self.assertNotIn("Authorization", aisetup._headers("https://registry.ollama.ai/v2/library/qwen3/manifests/8b", "a"))
            self.assertNotIn("Authorization", aisetup._headers("https://github.com/ollama/ollama/releases/download/v1/x", "a"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("Authorization", aisetup._headers("https://api.github.com/repos/x", "a"))

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

    def test_the_older_model_option_works_but_is_not_advertised(self):
        p = aisetup.build_parser()
        sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction)).choices
        for cmd in ("setup", "remove"):
            self.assertNotIn("--model", sub[cmd].format_help(), cmd)
            self.assertIn("MODEL", sub[cmd].format_usage(), cmd)
        self.assertIn("--model", sub["serve"].format_help(), "serve keeps its own --model")
        self.assertEqual(p.parse_args(["setup", "--model", "a", "--model", "b", "c"]).model_opt, ["a", "b"])
        self.assertEqual(p.parse_args(["setup", "a", "b"]).models, ["a", "b"])
        self.assertEqual(p.parse_args(["setup"]).models, [])
        self.assertEqual(p.parse_args(["remove"]).model, None)
        self.assertEqual(p.parse_args(["remove", "a"]).model, "a")
        self.assertEqual(p.parse_args(["remove", "--model", "a"]).model_opt, "a")

    def test_help_mentions_every_command(self):
        text = aisetup.build_parser().format_help()
        for word in ("models", "setup", "use", "serve", "status", "remove"):
            self.assertIn(word, text)

    def test_models_and_use_through_main(self):
        advice(self, HW_BIG)
        with tempfile.TemporaryDirectory() as d:
            rc, out, err = call(aisetup.main, ["nuc-console-ai", "models", "--dir", d, "--config", os.path.join(d, "c.ini")])
            self.assertEqual((rc, err), (0, ""))
            self.assertIn("qwen3-30b-a3b", out)
            rc, out, err = call(aisetup.main, ["nuc-console-ai", "use", "qwen3-4b", "--dir", d, "--config", os.path.join(d, "c.ini")])
            self.assertEqual(rc, 1)
            self.assertIn("nuc-console-ai: qwen3-4b is not installed", err)
            self.assertFalse(os.path.exists(os.path.join(d, "c.ini")))
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            aisetup.main(["nuc-console-ai", "use"])  # a model is required
        self.assertEqual(cm.exception.code, 2)


class DocsTests(unittest.TestCase):
    """docs/AI.md says what the code does: the table of models, the numbers of the verdicts and the speeds, the options of every command,
    and config.ini, are checked against the code, so that a change on one side without the other fails here."""

    @classmethod
    def setUpClass(cls):
        def read(*parts):
            with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
                return f.read()
        cls.text, cls.config = read("docs", "AI.md"), read("config", "config.ini")
        cls.configuration = read("docs", "CONFIGURATION.md")

    def test_the_table_of_models_is_the_catalog(self):
        import aihw
        rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in self.text.splitlines() if re.match(r"\| `[a-z0-9.-]+` \|", ln)]
        self.assertEqual([r[0].strip("`") for r in rows], [m["id"] for m in aisetup.MODELS], "the same models, in the same order (best first)")
        for r, m in zip(rows, aisetup.MODELS):
            with self.subTest(m["id"]):
                params = "%g B" % m["params_b"] + (" (%g B active)" % m["active_b"] if m.get("active_b") else "")
                self.assertEqual(r[1:7], [m["name"], params, aisetup.fmt_mb(m["approx_mb"]), aisetup.fmt_mem(aihw.need_mb(m)),
                                          "%dk" % (m["ctx_max"] // 1024), m["license"]])

    def test_the_numbers_of_the_verdicts_and_the_speeds_are_the_codes(self):
        import aihw
        rng = lambda t: "%g-%g" % tuple(t)  # noqa: E731
        want = ["`GPU_FIT` %.2f" % aihw.GPU_FIT, "`RAM_COMFY_FRAC` %.2f" % aihw.RAM_COMFY_FRAC, "`RAM_MAX_FRAC` %.2f" % aihw.RAM_MAX_FRAC,
                "`RAM_RESERVE_MB` %d" % aihw.RAM_RESERVE_MB, "%d %% of the RAM (`UNIFIED_MAX_FRAC`)" % round(aihw.UNIFIED_MAX_FRAC * 100),
                "less %d MB for the display" % aihw.GPU_DISPLAY_RESERVE_MB, "about %d MB of runtime" % aihw.OVERHEAD_MB,
                "16 MB per layer",
                "the CPU %s" % rng(aihw.CPU_BW), "NVIDIA %s, AMD %s, Intel %s, others %s" % tuple(rng(aihw.GPU_BW[k]) for k in ("nvidia", "amd", "intel", "other")),
                "%s for any card with %d GB or more" % (rng(aihw.HIGH_END_BW), aihw.HIGH_END_VRAM_MB // 1000),
                "%s (base), %s (Pro), %s (Max) and %s (Ultra)" % tuple(rng(aihw.APPLE_BW[k]) for k in ("base", "pro", "max", "ultra")),
                "%d %% of that for its CPU cores" % round(aihw.APPLE_CPU_FACTOR * 100),
                "cut to %d %% of its low end and %d %% of its high end" % tuple(round(x * 100) for x in aihw.SLOW_FACTOR),
                "less than 2 GB"]
        for w in want:
            with self.subTest(w):
                self.assertIn(w, self.text)
        self.assertEqual(aihw.PARTIAL_MIN_FRAC, 0.10, "the text says a tenth of the layers")
        self.assertEqual(aihw.APU_VRAM_MAX_MB, 2048, "the text says 2 GB")
        self.assertEqual(aihw.KV_BYTES_PER_LAYER_TOKEN * aihw.DEFAULT_CTX // 2 ** 20, 16, "the text says 16 MB per layer")
        self.assertEqual((aisetup.DEFAULT_PORT, aisetup.DEFAULT_CTX), (8080, 4096))

    def test_every_option_of_every_command_is_documented(self):
        subs = next(a for a in aisetup.build_parser()._actions if isinstance(a, argparse._SubParsersAction)).choices
        for name, sub in subs.items():
            self.assertIn("`nuc-console-ai %s" % name, self.text, name)
            for act in sub._actions:
                for opt in act.option_strings:
                    if opt in ("-h", "--help", "-y") or act.help == argparse.SUPPRESS:
                        continue
                    with self.subTest(command=name, option=opt):
                        self.assertIn(opt, self.text)

    def test_the_defaults_in_the_text_are_the_codes(self):
        p = aisetup.build_parser()
        self.assertEqual(p.parse_args(["serve"]).port, 8080)
        self.assertEqual(p.parse_args(["setup"]).port, 8080)
        self.assertEqual(p.parse_args(["serve"]).ctx, 4096)
        self.assertIn("`--ctx N` (default 4096", self.text)
        self.assertIn("`--port N` (8080)", self.text)
        self.assertEqual(aisetup.DEFAULT_MODEL, "qwen3-4b")
        self.assertIn("else `qwen3-4b` if that is installed", self.text)

    def test_config_ini_is_strict_and_documents_only_real_keys(self):
        import configparser
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))  # strict: a key twice is an error
        cp.read_string(self.config)
        self.assertIn("ai", cp["features"], "[features] ai, once")
        self.assertEqual(len(re.findall(r"^ai\s*=", self.config, re.M)), 1)
        known = nuc_config.load("/nonexistent")["ai"]
        for key in cp["ai"]:
            self.assertIn(key, known, "config.ini documents a key the code does not read")
        documented = re.findall(r"^\| `([a-z_]+)` \|", self.configuration[self.configuration.index("## `[ai]`"):self.configuration.index("## Commands")], re.M)
        self.assertEqual(sorted(documented), sorted(cp["ai"]), "the same [ai] keys in CONFIGURATION.md and in config.ini")
        for key in documented:
            self.assertIn(key, known)
        self.assertEqual(cp["ai"]["gpu"], known["gpu"])
        self.assertEqual(int(cp["ai"]["timeout_s"]), known["timeout_s"])
        self.assertEqual(cp["ai"]["endpoint"], aisetup.SHIPPED_ENDPOINT)


if __name__ == "__main__":
    unittest.main()
