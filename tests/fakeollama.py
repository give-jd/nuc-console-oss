"""A fake Ollama for the tests of the AI setup (test_aisetup.py) and of the AI page's engine (test_ai_web_actions.py).

The fake server is a small Python program that behaves like `ollama serve` as far as nuc-console uses it: it listens where OLLAMA_HOST says, keeps
its models in OLLAMA_MODELS with Ollama's layout (manifests/registry.ollama.ai/<namespace>/<model>/<tag>, blobs/sha256-<hex>), and answers
/api/version, /v1/models, /api/pull (NDJSON progress), /api/copy, /api/delete and /api/generate (load, unload). What it can pull and how it
misbehaves are read from <AI folder>/fake-ollama.json (the folder above OLLAMA_MODELS), so a test changes them between two steps:

  {"registry": {"tiny:test": [2600000, 300]}, ...}   the models it can pull: the sizes of their layers (deterministic bytes)
  "die": text            at start: write it and exit with status 3
  "mute": true           at start: never answer (it "loads" for ever)
  "stubborn": true       ignore SIGTERM
  "pull_error": text     a pull answers {"error": text} after "pulling manifest"
  "pull_hold": true      a pull stops after the first quarter of the first layer and waits until the client goes away (a cancel)
  "load_error": text     a load answers HTTP 500 {"error": text}
  "load_hold": true      a load waits until the client goes away
  "load_die": true       a load writes "out of memory" and the server exits with status 4

Every request is appended to <AI folder>/fake-ollama.log as one JSON line (method, path, body), for the tests to read.
The runtime archive holds the program at the executable's place for this system (aiollama.exe_rel()): a shell script that runs it with this
interpreter (Unix), or the Python source itself (Windows: the tests' popen hands an .exe that is Python to this interpreter).
"""
import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import aiollama  # noqa: E402
import aisetup  # noqa: E402

POSIX = os.name == "posix"
KEY = aiollama.asset_key()
VERSION = "0.0.1"

SERVER = r'''
import hashlib, http.server, json, os, select, signal, socketserver, sys, threading
host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)
MODELS = os.environ["OLLAMA_MODELS"]
CONTROL = os.path.join(os.path.dirname(MODELS), "fake-ollama.json")
LOG = os.path.join(os.path.dirname(MODELS), "fake-ollama.log")
LOCK = threading.Lock()

def control():
    try:
        with open(CONTROL, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def blob(n, salt):
    return b"".join(hashlib.sha256(salt + str(i).encode()).digest() for i in range(n // 32 + 1))[:n]

def split(name):
    model, tag = (name.rsplit(":", 1) + ["latest"])[:2] if ":" in name else (name, "latest")
    ns, model = model.split("/", 1) if "/" in model else ("library", model)
    return os.path.join(MODELS, "manifests", "registry.ollama.ai", ns, model, tag)

def layers_of(path):
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    return [m["config"]["digest"]] + [x["digest"] for x in m["layers"]]

def bpath(digest):
    return os.path.join(MODELS, "blobs", digest.replace(":", "-"))

def all_manifests():
    out = []
    for root, _d, files in os.walk(os.path.join(MODELS, "manifests")):
        out += [os.path.join(root, f) for f in files]
    return out

c = control()
if c.get("die"):
    print(c["die"], file=sys.stderr, flush=True)
    sys.exit(3)
if c.get("stubborn") and hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("fake ollama starting on", port, flush=True)
if c.get("mute"):
    import time
    print("loading", flush=True)
    while True:
        time.sleep(0.05)

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except ValueError:
            return {}

    def note(self, body):
        with LOCK, open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"method": self.command, "path": self.path, "body": body}) + "\n")

    def answer(self, status, obj):
        b = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def line(self, obj):
        self.wfile.write((json.dumps(obj) + "\n").encode())
        self.wfile.flush()

    def until_gone(self):
        while True:
            r, _w, _x = select.select([self.connection], [], [], 0.05)
            if r:
                try:
                    if not self.connection.recv(1):
                        return
                except OSError:
                    return

    def do_GET(self):
        self.note(None)
        if self.path == "/api/version":
            return self.answer(200, {"version": "%s"})
        if self.path == "/v1/models":
            ids = []
            for p in sorted(all_manifests()):
                rel = os.path.relpath(p, os.path.join(MODELS, "manifests", "registry.ollama.ai")).replace(os.sep, "/").split("/")
                ids.append(("%%s:%%s" %% (rel[1], rel[2])) if rel[0] == "library" else "%%s/%%s:%%s" %% tuple(rel))
            return self.answer(200, {"object": "list", "data": [{"id": i, "object": "model"} for i in ids] or None})
        self.answer(404, {"error": "not found"})

    def do_DELETE(self):
        body = self.body()
        self.note(body)
        p = split(body.get("model", ""))
        if not os.path.isfile(p):
            return self.answer(404, {"error": "model '%%s' not found" %% body.get("model")})
        gone = set(layers_of(p))
        os.unlink(p)
        used = set()
        for q in all_manifests():
            used.update(layers_of(q))
        for d in gone - used:
            if os.path.exists(bpath(d)):
                os.unlink(bpath(d))
        self.answer(200, {})

    def do_POST(self):
        body = self.body()
        self.note(body)
        c = control()
        if self.path == "/api/copy":
            src, dst = split(body.get("source", "")), split(body.get("destination", ""))
            if not os.path.isfile(src):
                return self.answer(404, {"error": "model '%%s' not found" %% body.get("source")})
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, "rb") as f, open(dst, "wb") as g:
                g.write(f.read())
            return self.answer(200, {})
        if self.path == "/api/generate":
            if not os.path.isfile(split(body.get("model", ""))):
                return self.answer(404, {"error": "model '%%s' not found" %% body.get("model")})
            if body.get("keep_alive") == 0:
                return self.answer(200, {"model": body["model"], "done": True, "done_reason": "unload"})
            if c.get("load_die"):
                print("out of memory", file=sys.stderr, flush=True)
                os._exit(4)
            if c.get("load_error"):
                return self.answer(500, {"error": c["load_error"]})
            if c.get("load_hold"):
                self.send_response(200)
                self.end_headers()
                return self.until_gone()
            return self.answer(200, {"model": body["model"], "done": True, "done_reason": "load"})
        if self.path == "/api/pull":
            name = body.get("model", "")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.line({"status": "pulling manifest"})
            if c.get("pull_error"):
                return self.line({"error": c["pull_error"]})
            sizes = (c.get("registry") or {}).get(name)
            if sizes is None:
                return self.line({"error": "pull model manifest: file does not exist"})
            os.makedirs(os.path.join(MODELS, "blobs"), exist_ok=True)
            layers = []
            for i, n in enumerate(sizes):
                data = blob(n, (name + str(i)).encode())
                d = "sha256:" + hashlib.sha256(data).hexdigest()
                layers.append({"mediaType": "application/vnd.ollama.image.model" if i == 0 else "application/vnd.ollama.image.params", "digest": d, "size": n})
                part = bpath(d) + "-partial"
                for k in range(1, 5):
                    with open(part, "ab" if k > 1 else "wb") as f:
                        f.write(data[(k - 1) * n // 4:k * n // 4])
                    try:
                        self.line({"status": "pulling " + d[7:19], "digest": d, "total": n, "completed": k * n // 4})
                    except OSError:
                        return
                    if c.get("pull_hold") and i == 0 and k == 1:
                        return self.until_gone()
                os.replace(part, bpath(d))
            cfg = json.dumps({"model_format": "gguf"}).encode()
            cd = "sha256:" + hashlib.sha256(cfg).hexdigest()
            with open(bpath(cd), "wb") as f:
                f.write(cfg)
            self.line({"status": "verifying sha256 digest"})
            self.line({"status": "writing manifest"})
            p = split(name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"schemaVersion": 2, "config": {"digest": cd, "size": len(cfg)}, "layers": layers}, f)
            return self.line({"status": "success"})
        self.answer(404, {"error": "not found"})

class S(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # not HTTPServer's: its socket.getfqdn() hangs on macOS runners

s = S((host, int(port)), H)
print("listening on", port, flush=True)
s.serve_forever()
''' % VERSION


def runtime_exe():
    """The bytes of the fake `ollama` executable for this system."""
    if not POSIX:
        return SERVER.encode()
    return ("#!/bin/sh\nexec '%s' -c '%s' \"$@\"\n" % (sys.executable, SERVER.replace("'", "'\"'\"'"))).encode()


def runtime_archive(exe=None):
    """-> (file name, bytes): the fake build, a .zip on Windows and a .tgz elsewhere, with the executable where aiollama.exe_rel() says
    and a library beside it (like the real builds)."""
    exe = runtime_exe() if exe is None else exe
    rel = aiollama.exe_rel()
    buf = io.BytesIO()
    if not POSIX:
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(rel, exe)
            z.writestr("lib/ollama/LICENSE", "fake")
        return "ollama-fake.zip", buf.getvalue()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data, mode in ((rel, exe, 0o755), ("lib/ollama/LICENSE", b"fake", 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("lib/ollama/LICENSE.txt")
        link.type, link.linkname = tarfile.SYMTYPE, "LICENSE"
        t.addfile(link)
    return "ollama-fake.tgz", buf.getvalue()


def fake_runtime(base, archive=None):
    """-> (runtime dict with a build for this machine only, its file name, its bytes). base: the download server's URL."""
    name, data = archive or runtime_archive()
    rt = {"name": "ollama", "version": VERSION, "license": "MIT", "base": base.rstrip("/") + "/",
          "assets": {KEY: {"file": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}}
    return rt, name, data


def fake_model(name, size=30000, rank=1, **kw):
    """A catalog entry pulled as "<name>:test" (the fake registry's name) and known by its id `name` once installed."""
    m = {"id": name, "name": name.capitalize() + " test model", "license": "MIT", "rank": rank, "params_b": 1.0, "quant": "Q4_K_M", "layers": 32,
         "ctx_max": 32768, "approx_mb": 3, "ram_mb": 100, "notes": "note " + name, "ollama": name + ":test", "size": size}
    m.update(kw)
    return m


def registry_for(models, extra=0):
    """The fake registry of these models: each one a weights layer of its pinned size (less a small params layer) and that layer."""
    return {m["ollama"]: [max(1, (m.get("size") or 30000) - 300 - extra), 300] for m in models}


def write_control(d, **kw):
    """<d>/fake-ollama.json: what the fake server can pull and how it misbehaves (see the docstring)."""
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "fake-ollama.json"), "w", encoding="utf-8") as f:
        json.dump(kw, f)


def requests_seen(d):
    """The requests the fake server received, oldest first: [{"method", "path", "body"}]."""
    try:
        with open(os.path.join(d, "fake-ollama.log"), encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def install_runtime(d, runtime, keep_archive=True, archive=None):
    """The fake build unpacked in <d> like a finished setup (the archive kept and recorded, the stamp marked), without any download."""
    name, data = archive or runtime_archive()
    aisetup.ensure_dirs(d)
    p = os.path.join(d, "runtime", name)
    with open(p, "wb") as f:
        f.write(data)
    aisetup.record(d, p, hashlib.sha256(data).hexdigest())
    aisetup.unpack_runtime(d, runtime)
    if not keep_archive:
        os.unlink(p)


def install_model(d, m, sizes=None):
    """The model `m` installed in <d>/models under its id, as the server leaves it after a pull and a copy, without any server."""
    models = aiollama.models_dir(d)
    os.makedirs(os.path.join(models, "blobs"), exist_ok=True)
    layers = []
    for i, n in enumerate(sizes or [max(1, (m.get("size") or 30000) - 300), 300]):
        data = hashlib.sha256((m["id"] + str(i)).encode()).digest() * (n // 32 + 1)
        data = data[:n]
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        with open(aiollama.blob_path(models, digest), "wb") as f:
            f.write(data)
        layers.append({"mediaType": "application/vnd.ollama.image.model", "digest": digest, "size": n})
    cfg = b'{"model_format": "gguf"}'
    cd = "sha256:" + hashlib.sha256(cfg).hexdigest()
    with open(aiollama.blob_path(models, cd), "wb") as f:
        f.write(cfg)
    p = aisetup.model_path(d, m)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"schemaVersion": 2, "config": {"digest": cd, "size": len(cfg)}, "layers": layers}, f)
