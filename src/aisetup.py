"""nuc-console-ai: choose a small local model by what this machine can run, install it once (setup), run it on 127.0.0.1 (serve).

  models  the hardware found (RAM, GPU and its memory) and every model of the catalog: does it fit (GPU / GPU+CPU / RAM / SLOW /
          TOO BIG), a rough speed, installed, active, the recommended one
  setup   download the model server (Ollama: the build of this system and processor, pinned below, SHA-256 checked, unpacked in the
          cache directory) and pull one or several models from the Ollama library through it (Ollama checks every layer against the
          registry's SHA-256); never twice; with your consent write [ai] endpoint/model in config.ini. No model named = the recommended
          one; a model that will not work here is refused unless --force
  use     make an installed model the one the advisor asks for ([ai] model in config.ini)
  serve   run the server in the foreground, on 127.0.0.1 only, at low priority, on the GPU when Ollama finds one ([ai] gpu = no: never)
          (--install-service / --remove-service: a system service: systemd unit, launchd daemon or Windows scheduled task, each run by an
          unprivileged account)
  status  what is installed, whether the configured endpoint answers
  remove  delete the downloaded files (one model, or everything)
  pins    for maintainers: prints the values to paste in RUNTIME and MODELS (needs the network)

Security: HTTPS only (a redirect to http:// is refused), the server's archive is checked against a SHA-256 written in this file before it is
unpacked (downloads go to "<name>.part", renamed only after the check; every member of the archive is checked before it is written),
commands are argument lists (no shell), the server binds to 127.0.0.1 and nothing here can change that, no auto-update: a new server
means a new pin in a new release. The models come from the Ollama library (registry.ollama.ai), named in MODELS: Ollama verifies each file
it downloads against the digest of the registry's manifest. The advice (aihw.py) only reads the machine and does arithmetic: it never
downloads and never hashes. catalog() is what the screens call: it reads the stamp file and the manifests only, so an unprivileged process
can show it. The Ollama specifics (the build, unpacking, the server's environment, its API, its folder of models) are in aiollama.py.
Standard library only, Python 3.8+. Output is plain ASCII (Windows consoles).
"""
import argparse
import hashlib
import http.client
import json
import ntpath
import os
import plistlib
import posixpath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape as xml_escape

import aiollama
import nuc_config

LOOPBACK = "127.0.0.1"  # the only address the server is ever started on (serve_env has no parameter for it)
DEFAULT_PORT = 8080     # not Ollama's own 11434: an Ollama you run yourself keeps its port, and the two are never mistaken for each other
DEFAULT_CTX = 4096  # tokens of context: bounds the memory of the KV cache (a 32k default would need gigabytes)
ALLOWED_LICENSES = ("Apache-2.0", "MIT")  # permissive only; tests refuse anything else in MODELS and RUNTIME
SHIPPED_ENDPOINT = "http://127.0.0.1:11434/v1"  # nuc_config's default [ai] endpoint (Ollama): setup --yes never replaces another one
UA = "nuc-console-ai/1"
DISK_MARGIN = 300 * 10 ** 6  # free space kept besides the files
SERVICE_NAME = "nuc-console-ai"  # systemd unit, system user, Windows task name
MAC_LABEL, MAC_USER = "com.nuc-console.ai", "_nuc-console-ai"
UNIX_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
PORT_TRIES = 20  # a server started here takes the first free port from DEFAULT_PORT up
START_WAIT_S = 60.0  # a server started by setup must answer within this long

SetupError, Cancelled = aiollama.SetupError, aiollama.Cancelled  # one class each for this file, aiollama and aiweb

# ---------------------------------------------------------------------------------------------------------------------------
# The pins. A value that is None means "not pinned yet": `setup` refuses to download it. Never fill one from memory or from a
# web page: run `python3 aisetup.py pins` (it asks the GitHub API and the Ollama registry) and check the output, then re-run the tests.
# ---------------------------------------------------------------------------------------------------------------------------
# Ollama (MIT): one archive per system and processor, with the server and the libraries of every GPU backend it supports (CUDA, ROCm on
# Linux through an add-on this build does not fetch, Vulkan, Metal); it serves an OpenAI-compatible API under /v1 and its own under /api.
# "base" + the file of this machine's key is the URL; "sha256" and "size" pin the bytes. The values are the release's own SHA256SUMS
# (sha256sum.txt) and the sizes GitHub serves, read on 2026-10-02 from the v0.35.0 release (the ai-pins workflow reads them again from the
# API's asset digests). Keys: aiollama.asset_key(); Linux builds are .tar.zst (Python 3.14 reads them, else the zstd tool).
RUNTIME = {
    "name": "ollama", "version": "0.35.0", "license": "MIT",
    "base": "https://github.com/ollama/ollama/releases/download/v0.35.0/",
    "assets": {
        "linux-amd64": {"file": "ollama-linux-amd64.tar.zst", "sha256": "1c114a6b220c5efca2ef2b1e5f01d1e535e26f6cd6d1678c8489325d2835e525",
                        "size": 1427765407},
        "linux-arm64": {"file": "ollama-linux-arm64.tar.zst", "sha256": "cb627d332b1fe5055bd5485ca10d595da8429e447648209e375390ec3bd09374",
                        "size": 1550231393},
        "darwin": {"file": "ollama-darwin.tgz", "sha256": "2608dbb0a0f0136a198db9d48b4f74ece55f452314a39452fca35b7cf20c2589", "size": 160167937},
        "windows-amd64": {"file": "ollama-windows-amd64.zip", "sha256": "d6f7d3dd4f5d013553a78c1e78b2521fcf41d43dd2863e4596cdc046fe6036db",
                          "size": 1461196158},
        "windows-arm64": {"file": "ollama-windows-arm64.zip", "sha256": "99d061915a68fb563da0fb9316fd112cfc6fce0c9478601b2765b1f973cb715e",
                          "size": 208072407},
    },
}

# Instruct models, permissive licences, ordered best first (rank 1 = best for the advisor's job: short, grounded advice). "ollama" is the
# name the model is pulled under (the Ollama library's, registry.ollama.ai); once pulled it is known to the server by its "id". "size" is
# what the registry's manifest says the model weighs (every layer), read by `aisetup.py pins`; it feeds the disk check and the screens.
# The rest is for ADVICE only (what fits, how fast): params_b (billions; MoE: active_b = parameters read per token), layers (transformer
# blocks), ctx_max (tokens the model was trained for), approx_mb (approximate file in MB of 10^6 bytes: the maintainer's estimate, not a
# measurement), ram_mb (approximate memory of the server with DEFAULT_CTX tokens of context). Qwen3 and SmolLM3 start in "thinking"
# mode (long <think> blocks): the advisor should ask for /no_think.
MODELS = [
    {"id": "qwen3-30b-a3b", "name": "Qwen3 30B-A3B (MoE)", "license": "Apache-2.0", "rank": 1, "params_b": 30.5, "active_b": 3.3,
     "quant": "Q4_K_M", "layers": 48, "ctx_max": 32768, "approx_mb": 18600, "ram_mb": 19300,
     "notes": "MoE: reads only 3.3B per token, fast on CPU if the RAM holds it; /no_think", "ollama": "qwen3:30b-a3b", "size": None},
    {"id": "gpt-oss-20b", "name": "OpenAI gpt-oss 20B (MoE)", "license": "Apache-2.0", "rank": 2, "params_b": 21.0, "active_b": 3.6,
     "quant": "Q4_K_M", "layers": 24, "ctx_max": 131072, "approx_mb": 11600, "ram_mb": 12100,
     "notes": "MoE: reads only 3.6B per token; a reasoning model (long answers)", "ollama": "gpt-oss:20b", "size": None},
    {"id": "phi-4", "name": "Phi-4 14B", "license": "MIT", "rank": 3, "params_b": 14.7, "quant": "Q4_K_M", "layers": 40,
     "ctx_max": 16384, "approx_mb": 9100, "ram_mb": 10300, "notes": "dense 14B: strong reasoning, slow without a GPU", "ollama": "phi4:14b",
     "size": None},
    {"id": "qwen3-14b", "name": "Qwen3 14B", "license": "Apache-2.0", "rank": 4, "params_b": 14.8, "quant": "Q4_K_M", "layers": 40,
     "ctx_max": 32768, "approx_mb": 9000, "ram_mb": 10000, "notes": "dense 14B: slow without a GPU; /no_think", "ollama": "qwen3:14b", "size": None},
    {"id": "qwen3-8b", "name": "Qwen3 8B", "license": "Apache-2.0", "rank": 5, "params_b": 8.2, "quant": "Q4_K_M", "layers": 36,
     "ctx_max": 32768, "approx_mb": 5000, "ram_mb": 6000, "notes": "a good balance on 16 GB; /no_think", "ollama": "qwen3:8b", "size": None},
    {"id": "granite-3.3-8b", "name": "IBM Granite 3.3 8B instruct", "license": "Apache-2.0", "rank": 6, "params_b": 8.2,
     "quant": "Q4_K_M", "layers": 40, "ctx_max": 131072, "approx_mb": 4900, "ram_mb": 5900, "notes": "enterprise-tuned, 128k context",
     "ollama": "granite3.3:8b", "size": None},
    {"id": "qwen3-4b", "name": "Qwen3 4B", "license": "Apache-2.0", "rank": 7, "params_b": 4.0, "quant": "Q4_K_M", "layers": 36,
     "ctx_max": 32768, "approx_mb": 2500, "ram_mb": 3600, "notes": "the default: small and capable; /no_think", "ollama": "qwen3:4b", "size": None},
    {"id": "phi-4-mini", "name": "Phi-4-mini instruct 3.8B", "license": "MIT", "rank": 8, "params_b": 3.8, "quant": "Q4_K_M", "layers": 32,
     "ctx_max": 131072, "approx_mb": 2500, "ram_mb": 3600, "notes": "good at reasoning for its size, 128k context", "ollama": "phi4-mini:3.8b",
     "size": None},
    {"id": "smollm3-3b", "name": "SmolLM3 3B", "license": "Apache-2.0", "rank": 9, "params_b": 3.1, "quant": "Q4_K_M", "layers": 36,
     "ctx_max": 65536, "approx_mb": 1900, "ram_mb": 2500, "notes": "3B with a thinking mode; /no_think", "ollama": "hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M",
     "size": None},
    {"id": "granite-3.3-2b", "name": "IBM Granite 3.3 2B instruct", "license": "Apache-2.0", "rank": 10, "params_b": 2.5,
     "quant": "Q4_K_M", "layers": 40, "ctx_max": 131072, "approx_mb": 1550, "ram_mb": 2400, "notes": "small and quick, 128k context",
     "ollama": "granite3.3:2b", "size": None},
    {"id": "qwen3-1.7b", "name": "Qwen3 1.7B", "license": "Apache-2.0", "rank": 11, "params_b": 1.7, "quant": "Q4_K_M", "layers": 28,
     "ctx_max": 32768, "approx_mb": 1100, "ram_mb": 2000, "notes": "for old or small machines; /no_think", "ollama": "qwen3:1.7b", "size": None},
    {"id": "qwen3-0.6b", "name": "Qwen3 0.6B", "license": "Apache-2.0", "rank": 12, "params_b": 0.6, "quant": "Q4_K_M", "layers": 28,
     "ctx_max": 32768, "approx_mb": 400, "ram_mb": 1200, "notes": "the smallest: simple summaries only; /no_think", "ollama": "qwen3:0.6b",
     "size": None},
]
DEFAULT_MODEL = "qwen3-4b"  # without a reading of the machine: the best that fits an 8 GB one; MODELS is ordered best first (pick_default)

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def model_name(m):
    """The name a model is pulled under (the Ollama library's), or None while it is not pinned."""
    return m.get("ollama") or None


def missing_pins(entry, is_model, plat=None, machine=None):
    """Names of the values that are still not pinned (empty = ready to download). A model needs its Ollama name; the runtime needs the
    file, SHA-256 and size of the build of this system and processor."""
    if is_model:
        name = entry.get("ollama")
        if not name:
            return ["ollama (the name to pull)"]
        try:
            aiollama.parse_name(name)
        except SetupError:
            return ["ollama (not a model name)"]
        return []
    a = aiollama.runtime_asset(entry, plat, machine)
    if a is None:
        return ["a build for this system (%s)" % (aiollama.asset_key(plat, machine) or "%s %s" % (plat or sys.platform, machine or "?"))]
    bad = [f for f in ("file", "sha256", "size") if a.get(f) in (None, "")]
    if a.get("sha256") and not HEX64.match(a["sha256"]):
        bad.append("sha256 (not 64 hex)")
    if a.get("size") is not None and not (isinstance(a["size"], int) and a["size"] > 0):
        bad.append("size (not a number of bytes)")
    return bad


def find_model(model_id, models=None):
    for m in (models if models is not None else MODELS):
        if m["id"] == model_id:
            return m
    raise SetupError("unknown model '%s' (known: %s)" % (model_id, ", ".join(m["id"] for m in (models or MODELS))))


def pick_default(total_mb, models=None, hw=None):
    """The model to suggest: with a reading of the machine (hw) and aihw, the recommended one (the smallest when nothing fits: setup
    then explains why it is refused); otherwise the best one (the list is ordered) that fits half the RAM, like aihw's "ram" verdict."""
    models = models if models is not None else MODELS
    if hw and _aihw():
        rec = recommend_id(models, hw)
        return next((m for m in models if m["id"] == rec), None) or min(models, key=lambda m: m["ram_mb"])
    if not total_mb:
        return next((m for m in models if m["id"] == DEFAULT_MODEL), models[0])
    for m in models:
        if m["ram_mb"] <= total_mb * 0.5:
            return m
    return min(models, key=lambda m: m["ram_mb"])


# ---------------------------------------------------------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------------------------------------------------------
def _is_win(plat=None):
    return (plat or sys.platform) == "win32"


def _is_mac(plat=None):
    return (plat or sys.platform) == "darwin"


def _pm(plat=None):
    return ntpath if _is_win(plat) else posixpath


def default_dir(plat=None, env=None, euid=None, home=None):
    """The cache directory. NUC_CONSOLE_HOME/ai when set; else per system, system-wide when root (Windows: always ProgramData)."""
    env = os.environ if env is None else env
    p = _pm(plat)
    if env.get("NUC_CONSOLE_HOME"):
        return p.join(env["NUC_CONSOLE_HOME"], "ai")
    if _is_win(plat):
        return p.join(env.get("ProgramData") or r"C:\ProgramData", "nuc-console", "ai")
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else 1
    home = home or os.path.expanduser("~")
    if _is_mac(plat):
        base = "/Library/Application Support" if euid == 0 else p.join(home, "Library", "Application Support")
        return p.join(base, "nuc-console", "ai")
    if euid == 0:
        return "/var/lib/nuc-console/ai"
    return p.join(env.get("XDG_DATA_HOME") or p.join(home, ".local", "share"), "nuc-console", "ai")


def runtime_path(d, runtime=None, plat=None):
    """The server's executable once its build is unpacked: <dir>/runtime/ollama-<version>/ollama.exe (Windows), .../ollama (macOS),
    .../bin/ollama (Linux)."""
    return aiollama.exe_path(d, runtime or RUNTIME, plat)


def archive_path(d, runtime=None, plat=None, machine=None):
    """<dir>/runtime/<the archive of this machine's build> (kept after unpacking: `serve --install-service` checks it again), or None."""
    return aiollama.archive_path(d, runtime or RUNTIME, plat, machine)


def model_path(d, model, plat=None):
    """The file that says a model is installed: its manifest under the catalog id, in the server's folder of models."""
    return aiollama.manifest_path(aiollama.models_dir(d, plat), model["id"], plat)


def stamp_path(d):
    return os.path.join(d, "verified.json")


def ensure_dirs(d):
    """<dir>, <dir>/runtime, <dir>/models; the directories created here (and only those) get 0755 whatever the umask, so that the
    service account can walk down to the files. An existing directory (a home, say) is never touched."""
    for p in (d, os.path.join(d, "runtime"), aiollama.models_dir(d)):
        missing, q = [], os.path.abspath(p)
        while not os.path.isdir(q) and os.path.dirname(q) != q:
            missing.append(q)
            q = os.path.dirname(q)
        os.makedirs(p, exist_ok=True)
        for q in reversed(missing):  # top down: a folder is adopted from the one it is in
            try:
                os.chmod(q, 0o755)
            except OSError:
                pass
            adopt(q, os.path.dirname(q))


def adopt(path, like):
    """Root on Linux/macOS: the folder `path`, just created, takes the owner of the folder `like` it is in when that one is not root's. The
    installers give the AI folder to the account of the web view, and that account must still be able to write into the folders
    `sudo nuc-console-ai setup` makes in it (models/, runtime/). Anyone else, and Windows (the ACL is inherited): nothing."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        st = os.stat(like)
        if st.st_uid != 0:
            os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def dir_space(d):
    """{"used": bytes taken by the downloaded files (runtime/ and models/, everything in them, partial downloads too), "free": bytes free on
    the disk the folder is on (None when unknown)}: what the screens say about the folder. Never raises."""
    used = 0
    for sub in ("runtime", "models"):
        for root, _dirs, files in os.walk(os.path.join(d, sub)):
            for f in files:
                try:
                    st = os.lstat(os.path.join(root, f))
                except OSError:
                    continue
                if not os.path.islink(os.path.join(root, f)):
                    used += st.st_size
    try:
        free = free_bytes(d)
    except OSError:
        free = None
    return {"used": used, "free": free}


def space_text(space):
    """'5.0 GB downloaded, 120.0 GB free on that disk' (plain words for the screens and the commands)."""
    sp = space if isinstance(space, dict) else {}
    used, free = sp.get("used"), sp.get("free")
    return "%s downloaded, %s" % (fmt_size(used) if used else "nothing", "%s free on that disk" % fmt_size(free) if free else "free space unknown on that disk")


def safe(s, n=80):
    """Text from outside (a server's answer, a file name) with control characters replaced: it may reach a terminal."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", str(s))[:n]


def fmt_size(n):
    if not n:
        return "?"
    return "%.1f GB" % (n / 1e9) if n >= 1e9 else "%d MB" % round(n / 1e6) if n >= 1e6 else "%d KB" % max(1, round(n / 1e3))


# ---------------------------------------------------------------------------------------------------------------------------
# Verified files: a stamp (name -> sha256, size, mtime) written after a check, so that status and serve do not hash 2 GB each time
# ---------------------------------------------------------------------------------------------------------------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_stamp(d):
    try:
        with open(stamp_path(d), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(d, path, sha256):
    """Remember that `path` was verified (atomic write, 0644)."""
    st = os.stat(path)
    data = load_stamp(d)
    data[os.path.basename(path)] = {"sha256": sha256, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    _write_stamp(d, data)


def forget(d, name):
    """Forget an entry of the stamp: a file's (its path) or the unpacked build's (runtime_key)."""
    data = load_stamp(d)
    if data.pop(os.path.basename(name), None) is not None:
        _write_stamp(d, data)


def _write_stamp(d, data):
    tmp = stamp_path(d) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, stamp_path(d))


def is_verified(d, path, sha256, size, rehash=False):
    """True when `path` is the pinned file (the server's archive): by its stamp (size and mtime unchanged), or by hashing it again."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size != size:
        return False
    if rehash:
        return sha256_file(path) == sha256
    s = load_stamp(d).get(os.path.basename(path))
    return bool(s) and s.get("sha256") == sha256 and s.get("size") == st.st_size and s.get("mtime_ns") == st.st_mtime_ns


def runtime_key(runtime=None):
    """The stamp's entry of the unpacked build: ollama-<version>."""
    runtime = runtime or RUNTIME
    return "%s-%s" % (runtime["name"], runtime["version"])


def runtime_ready(d, runtime=None, plat=None, machine=None, rehash=False):
    """True when the build of this machine is unpacked: the stamp says it was unpacked from the archive with the pinned SHA-256, and its
    executable is there. rehash: the archive is also hashed again against the pin (`serve --install-service`, which then unpacks it again)."""
    runtime = runtime or RUNTIME
    if missing_pins(runtime, False, plat, machine):
        return False
    a = aiollama.runtime_asset(runtime, plat, machine)
    s = load_stamp(d).get(runtime_key(runtime))
    if not (isinstance(s, dict) and s.get("sha256") == a["sha256"] and os.path.isfile(runtime_path(d, runtime, plat))):
        return False
    if rehash:
        p = archive_path(d, runtime, plat, machine)
        try:
            return os.path.getsize(p) == a["size"] and sha256_file(p) == a["sha256"]
        except OSError:
            return False
    return True


def model_ready(d, model, rehash=False):
    """True when the model is installed: its manifest under the catalog id and every layer it names, with their sizes (rehash: and their
    SHA-256, the digest each layer is named after)."""
    if missing_pins(model, True):
        return False
    return aiollama.blobs_ok(aiollama.models_dir(d), model["id"], rehash)


def mark_unpacked(d, runtime=None, plat=None, machine=None):
    """Remember that the build was unpacked from the pinned archive (the stamp, atomic, 0644)."""
    runtime = runtime or RUNTIME
    a = aiollama.runtime_asset(runtime, plat, machine)
    data = load_stamp(d)
    data[runtime_key(runtime)] = {"sha256": a["sha256"], "file": a["file"]}
    _write_stamp(d, data)


# ---------------------------------------------------------------------------------------------------------------------------
# Download: HTTPS only, resume with Range, hash before rename
# ---------------------------------------------------------------------------------------------------------------------------
def check_url(url, allow_loopback_http=False):
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return
    if allow_loopback_http and u.scheme == "http" and u.hostname in (LOOPBACK, "localhost", "::1"):  # tests only: no CLI option
        return
    raise SetupError("refusing to download from %s: HTTPS only" % safe(url))


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Hugging Face and GitHub redirect to a CDN: follow, but only to https:// (the Range header is kept by urllib)."""
    allow_http = False

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_url(newurl, self.allow_http)
        except SetupError:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(allow_loopback_http):
    h = _HttpsRedirects()
    h.allow_http = allow_loopback_http
    handlers = [h] + ([urllib.request.ProxyHandler({})] if allow_loopback_http else [])  # a local test server: never via a proxy
    return urllib.request.build_opener(*handlers)


class _Restart(Exception):
    """The partial file cannot be continued (the server did not honour the Range request): start again."""


def _stream(opener, url, part, have, size, timeout, progress):
    headers = {"User-Agent": UA}
    if have:
        headers["Range"] = "bytes=%d-" % have
    with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        status = r.getcode()
        if status == 206 and have:
            m = re.match(r"bytes (\d+)-", r.headers.get("Content-Range", ""))
            if not m or int(m.group(1)) != have:
                raise _Restart()
            mode = "ab"
        else:  # 200: no Range support (or nothing to resume): the body is the whole file
            have, mode = 0, "wb"
        done = have
        with open(part, mode) as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                done += len(chunk)
                if done > size:
                    raise SetupError("the server sent more than the %d bytes expected: download refused" % size)
                f.write(chunk)
                if progress:
                    progress(done, size)
        return done


def _nap(seconds, stop=None):
    """time.sleep(seconds); with `stop` (a cancel check) in slices of a fifth of a second, calling it before each."""
    if stop is None:
        time.sleep(seconds)
        return
    left = seconds
    while left > 0:
        stop()
        step = min(0.2, left)
        time.sleep(step)
        left -= step
    stop()


def download(url, dest, sha256, size, *, mode=0o644, allow_loopback_http=False, progress=None, retries=4, timeout=30, backoff=2.0, cancel=None):
    """Download `url` to `dest` (resuming "<dest>.part"), check size and SHA-256, then rename. -> "cached" | "downloaded".
    A file already at `dest` with the right hash is never downloaded again. A wrong hash deletes the download and raises.
    cancel: a function that says True when the caller wants to stop: looked at at every chunk and while waiting to try again, it raises
    Cancelled and keeps the partial file."""
    def stop():
        if cancel is not None and cancel():
            raise Cancelled()
    if cancel is not None:
        inner = progress

        def progress(done, total):  # noqa: F811 - the caller's progress, behind the cancel check
            stop()
            if inner:
                inner(done, total)
    check_url(url, allow_loopback_http)
    if not (sha256 and HEX64.match(sha256) and isinstance(size, int) and size > 0):
        raise SetupError("%s: SHA-256 and size are not pinned: refusing to download" % os.path.basename(dest))
    if os.path.isfile(dest) and os.path.getsize(dest) == size and sha256_file(dest) == sha256:
        return "cached"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part, opener, last = dest + ".part", _opener(allow_loopback_http), None
    for attempt in range(retries + 1):
        stop()
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > size:
            os.unlink(part)
            have = 0
        if have == size:
            break
        try:
            _stream(opener, url, part, have, size, timeout, progress)
        except _Restart:
            if os.path.exists(part):
                os.unlink(part)
            last = "the server ignored the resume request"
            continue
        except SetupError:
            if os.path.exists(part):
                os.unlink(part)
            raise
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 416:  # our partial file does not fit what the server has: start again
                if os.path.exists(part):
                    os.unlink(part)
                last = "HTTP 416"
                continue
            if 400 <= e.code < 500:
                raise SetupError("%s: HTTP %d" % (safe(url), e.code))
            last = "HTTP %d" % e.code
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            last = safe(getattr(e, "reason", e))
        if os.path.exists(part) and os.path.getsize(part) == size:
            break
        if attempt < retries:
            _nap(backoff * (attempt + 1), stop if cancel is not None else None)
    if not os.path.exists(part) or os.path.getsize(part) != size:
        raise SetupError("download incomplete (%s); run the command again to resume" % (last or "unknown error"))
    got = sha256_file(part)
    if got != sha256:
        os.unlink(part)
        raise SetupError("%s: SHA-256 is %s, expected %s: file deleted, nothing installed" % (os.path.basename(dest), got, sha256))
    os.chmod(part, mode)
    os.replace(part, dest)
    return "downloaded"


def make_progress(label, out=None):
    out = out or sys.stderr
    state = {"t0": time.time(), "last": -1}
    tty = hasattr(out, "isatty") and out.isatty()

    def progress(done, total):
        pct = done * 100 // total
        if tty:
            rate = done / max(0.001, time.time() - state["t0"]) / 1e6
            out.write("\r  %s: %3d%%  %s / %s  %.1f MB/s   " % (label, pct, fmt_size(done), fmt_size(total), rate))
            out.write("\n" if done >= total else "")
            out.flush()
        elif pct // 10 != state["last"] // 10 or done >= total:
            out.write("  %s: %d%%\n" % (label, pct))
            out.flush()
        state["last"] = pct
    return progress


# ---------------------------------------------------------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------------------------------------------------------
def parse_meminfo(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def memory_mb():
    """(total, available) in MB; None where this system does not tell."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii", errors="replace") as f:
                m = parse_meminfo(f.read())
        else:
            import hostinfo  # macOS (sysctl, vm_stat), Windows (GlobalMemoryStatusEx)
            m = hostinfo.meminfo()
        return m["MemTotal"] // 2 ** 20, m.get("MemAvailable", m["MemTotal"]) // 2 ** 20
    except (OSError, KeyError, ImportError, ValueError, AttributeError):
        return None, None


def free_bytes(path):
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return shutil.disk_usage(path or ".").free


def tool(name, plat=None):
    """Absolute path of a system tool from a fixed PATH (never the caller's)."""
    if _is_win(plat):
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        return os.path.join(root, "System32", name + ".exe")
    found = shutil.which(name, path=UNIX_PATH)
    if not found:
        raise SetupError("%s not found" % name)
    return found


def run(argv, check=True):
    r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if check and r.returncode != 0:
        raise SetupError("%s failed: %s" % (os.path.basename(argv[0]), r.stdout.strip()[-300:]))
    return r


def is_root():
    if _is_win():
        import ctypes
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return os.geteuid() == 0


def confirm(question, assume_yes=False, ask=input):
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print("nuc-console-ai: not interactive: add --yes to agree to: %s" % question, file=sys.stderr)
        return False
    try:
        return ask(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------------------------------------------------------------------
# Advice: what this machine can run. aihw.py measures the machine and does the arithmetic (it is imported when needed); without it,
# or without a reading, every function here says "unknown" instead of guessing: the advice is optional, it never stops a command.
# ---------------------------------------------------------------------------------------------------------------------------
VERDICT_LABEL = {"gpu": "GPU", "partial": "GPU+CPU", "ram": "RAM", "slow": "SLOW", "no": "TOO BIG"}
NO_ADVICE = "this machine's hardware could not be assessed"


def _aihw():
    try:
        import aihw
    except ImportError:
        return None
    return aihw


def _hardware():
    """aihw.cached(): what the machine has (detected at most every five minutes per process); {} when it cannot be told."""
    mod = _aihw()
    try:
        return (mod.cached() if mod else None) or {}
    except Exception:  # noqa: BLE001 - advice is optional: a detection bug must not stop setup or a screen
        return {}


def assess_model(model, hw, ctx=DEFAULT_CTX):
    """aihw.assess(): {"verdict": gpu|partial|ram|slow|no, "where", "need_mb", "gpu_layers", "tok_s", "why"}; None without advice."""
    mod = _aihw()
    if mod is None or not hw:
        return None
    try:
        return mod.assess(model, hw, ctx)
    except Exception:  # noqa: BLE001
        return None


def recommend_id(models, hw):
    """aihw.recommend(): the id of the best model that fits comfortably; None when nothing fits or there is no advice."""
    mod = _aihw()
    if mod is None or not hw:
        return None
    try:
        return mod.recommend(models, hw)
    except Exception:  # noqa: BLE001
        return None


def gpu_plan(cfg_gpu="auto"):
    """-> (gpu, why). gpu True: Ollama uses the GPU when it finds one that holds the model, all of it or some of its layers (it measures
    the free video memory itself, every time it loads a model); False: the GPUs are hidden from the server, which runs on the CPU only
    ([ai] gpu = no: the GPU is needed for something else, or its driver is not trusted)."""
    if cfg_gpu == "no":
        return False, "[ai] gpu = no: CPU only"
    return True, ""


def gpu_text(gpu):
    """Where the server runs, in words."""
    return "on the GPU when one holds the model (Ollama decides), else the CPU" if gpu else "CPU only"


def find_dir(plat=None):
    """The directory to read when none is given: the system-wide one (where `sudo nuc-console-ai setup` puts the files and the service
    reads them) if it holds a stamp file, else the caller's own. An unprivileged screen sees what the administrator installed."""
    own = default_dir(plat)
    for d in (default_dir(plat, euid=0), own):
        if os.path.isfile(stamp_path(d)):
            return d
    return own


def work_dir(plat=None):
    """The folder the web page and the console screen download into: find_dir()'s, except that the system-wide folder comes before an own folder
    with nothing installed in it yet when this account can write there (the installers hand it to the account of the web view and the console: a
    service account has no home to download into). Same files as the commands, same place: `sudo nuc-console-ai setup` and a button meet there.
    Windows has one folder for every account (ProgramData's): os.access, which reads the mode bits but not an ACL, is only asked on Unix."""
    d, system = find_dir(plat), default_dir(plat, euid=0)
    if d != system and not os.path.isfile(stamp_path(d)) and os.path.isdir(system) and os.access(system, os.W_OK | os.X_OK):
        return system
    return d


def commands_for(model_id, plat=None):
    """The commands to show next to a model (they change the system: an administrator runs them; Windows has no sudo)."""
    pre, post = ("", " (in an administrator prompt)") if _is_win(plat) else ("sudo ", "")
    verbs = {"install": "setup", "use": "use", "remove": "remove"}
    return {k: "%snuc-console-ai %s %s%s" % (pre, v, model_id, post) for k, v in verbs.items()}


def catalog(hw=None, d=None, models=None, runtime=None, cfg=None, plat=None):
    """Everything a screen shows about the models: {"hw", "dir", "space": {"used", "free"}, "runtime": {"installed", "name", "version"},
    "recommended": id|None, "active": [ai] model|None, "models": [model + assess, installed, pinned, commands]} in rank order, best first.
    Never downloads, never hashes (the stamp says what was unpacked, the manifests what was pulled), never raises: an unprivileged process
    can call it. `hw` (default aihw.cached()) can be given: the demo screens pass invented machines. models/runtime/cfg/plat are for tests.
    "assess" is aihw's answer, or verdict "unknown" with a sentence when the machine cannot be assessed."""
    models = list(MODELS if models is None else models)
    runtime = RUNTIME if runtime is None else runtime
    d = d or work_dir(plat)
    hw = hw if hw is not None else _hardware()
    if cfg is None:
        cfg = nuc_config.load(config_path())
        try:  # the active model is the one the AI page chose, when it chose (web.json over config.ini)
            import advisor
            cfg = advisor.effective_cfg(cfg)
        except Exception:  # noqa: BLE001 - config.ini's word then
            pass
    out = []
    for m in models:
        a = assess_model(m, hw)
        if a is None:
            a = {"verdict": "unknown", "where": "", "need_mb": m.get("ram_mb") or 0, "gpu_layers": 0, "tok_s": None, "why": NO_ADVICE}
        try:
            installed = model_ready(d, m)
        except (OSError, ValueError):
            installed = False
        out.append(dict(m, assess=a, installed=installed, pinned=not missing_pins(m, True), commands=commands_for(m["id"], plat)))
    try:
        rt_ok = runtime_ready(d, runtime, plat)
    except (OSError, ValueError, KeyError, TypeError):
        rt_ok = False
    return {"hw": hw, "dir": d, "space": dir_space(d), "runtime": {"installed": rt_ok, "name": "Ollama", "version": runtime["version"]},
            "recommended": recommend_id(models, hw), "active": (cfg.get("ai") or {}).get("model") or None, "models": out}


# ---------------------------------------------------------------------------------------------------------------------------
# The server command line
# ---------------------------------------------------------------------------------------------------------------------------
def serve_argv(d, runtime=None, plat=None):
    """argv that starts the server: `<executable> serve`. Where it listens (127.0.0.1 only), its folder of models and the rest are in its
    environment: serve_env()."""
    return aiollama.server_argv(runtime_path(d, runtime, plat))


def base_env(home, plat=None):
    """The small fixed environment the server starts with, never the caller's (an OLLAMA_HOST there must not open it to the network): a
    fixed PATH, a home for its key (~/.ollama), and on Windows the system's own folders (the GPU drivers are found through them)."""
    if _is_win(plat):
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        keep = ("SystemRoot", "SystemDrive", "windir", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "TEMP", "TMP",
                "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.update(PATH=";".join((ntpath.join(root, "System32"), root, ntpath.join(root, "System32", "Wbem"))), USERPROFILE=home, HOME=home)
        return env
    return {"PATH": UNIX_PATH, "HOME": home, "TMPDIR": home, "LANG": "C"}


def serve_env(d, port, ctx=DEFAULT_CTX, gpu=True, home=None, plat=None):
    """The server's whole environment: base_env() and aiollama.server_env() (127.0.0.1:<port>, the models in <dir>/models, the context,
    one model at a time, no cloud, the GPUs hidden unless `gpu`)."""
    return aiollama.server_env(d, port, ctx, gpu, base_env(home or os.path.expanduser("~"), plat), plat)


def lower_priority():
    """Unix: nice 10 (not more if the service manager already did it)."""
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < 10:
            os.setpriority(os.PRIO_PROCESS, 0, 10)
    except (AttributeError, OSError):
        pass


def run_server(argv, env):
    """Run in the foreground at low priority; returns the exit status (Unix: replaces this process and does not return)."""
    sys.stdout.flush()
    sys.stderr.flush()
    if _is_win():
        flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
        if not (sys.stdout and sys.stdout.isatty()):  # a scheduled task has no console: no window for the child either
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:  # a scheduled task has no console: the child writes where this process writes (the --log file)
            sys.stdout.fileno()
            out, err = sys.stdout, subprocess.STDOUT
        except (AttributeError, OSError, ValueError):
            out, err = subprocess.DEVNULL, subprocess.DEVNULL
        proc = subprocess.Popen(argv, env=env, creationflags=flags, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                return proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                return 1
    lower_priority()
    os.execve(argv[0], argv, env)


def pick_port(preferred=None):
    """A free port on 127.0.0.1: `preferred` (the one used before), else the first from DEFAULT_PORT up."""
    ports = [int(preferred)] if preferred else []
    for port in ports + list(range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if not _is_win():  # like the server itself: a port in TIME_WAIT is free (on Windows this option would let two bind)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((LOOPBACK, port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise SetupError("no free port from %d to %d on 127.0.0.1" % (DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES - 1))


def port_of(endpoint):
    """'http://127.0.0.1:8080/v1' -> 8080; None when it has no port."""
    try:
        return urllib.parse.urlsplit(endpoint).port
    except (ValueError, AttributeError):
        return None


class TempServer(object):
    """A server started for a moment (`setup` pulls its models through it): a child on a free port, its output in <dir>/home/server.log,
    stopped when the `with` ends. The GPUs are hidden: nothing is loaded, only downloaded."""

    def __init__(self, d, runtime=None, popen=subprocess.Popen, wait=START_WAIT_S):
        self.d, self.runtime, self.popen, self.wait = d, runtime or RUNTIME, popen, wait
        self.proc = self.api = None

    def __enter__(self):
        home = os.path.join(self.d, "home")
        os.makedirs(home, exist_ok=True)
        port = pick_port()
        self.log = os.path.join(home, "server.log")
        kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if _is_win() else {"start_new_session": True}
        with open(self.log, "wb") as out:
            self.proc = self.popen(serve_argv(self.d, self.runtime), env=serve_env(self.d, port, DEFAULT_CTX, False, home), cwd=home,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **kw)
        self.api = aiollama.Api("http://%s:%d" % (LOOPBACK, port))
        deadline = time.monotonic() + self.wait
        while self.api.version(1.0) is None:
            if self.proc.poll() is not None:
                raise SetupError("the model server stopped at once (exit status %s): %s" % (self.proc.returncode, self.tail()))
            if time.monotonic() > deadline:
                self.__exit__(None, None, None)
                raise SetupError("the model server did not answer within %d s: %s" % (self.wait, self.tail()))
            time.sleep(0.2)
        return self.api

    def tail(self):
        try:
            with open(self.log, "rb") as f:
                lines = [safe(x.decode("utf-8", "replace").strip(), 200) for x in f.read()[-4000:].splitlines() if x.strip()]
        except OSError:
            lines = []
        return " / ".join(lines[-3:]) or "it wrote nothing"

    def __exit__(self, *exc):
        p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
        return False


def unpack_runtime(d, runtime=None, cancel=None, plat=None, machine=None):
    """Unpack the verified archive of this machine's build into <dir>/runtime/ollama-<version> and say so in the stamp."""
    runtime = runtime or RUNTIME
    forget(d, runtime_key(runtime))
    aiollama.unpack(archive_path(d, runtime, plat, machine), aiollama.runtime_dir(d, runtime, plat), cancel)
    if not os.path.isfile(runtime_path(d, runtime, plat)):
        raise SetupError("the archive has no %s: this is not the build this version expects" % aiollama.exe_rel(plat))
    mark_unpacked(d, runtime, plat, machine)


def install_model(api, d, m, progress=None, cancel=None, insecure=False):
    """Pull the model through the server `api`, name it by its catalog id, drop the library's name (the layers stay: the id uses them)."""
    name = model_name(m)
    api.pull(name, progress, cancel, insecure)
    if name != m["id"]:
        api.copy(name, m["id"])
        api.delete(name)
    if not model_ready(d, m):
        raise SetupError("the server pulled %s, but its files are not in %s: is another server using that folder?" % (name, safe(aiollama.models_dir(d), 160)))


def runtime_bytes(d, runtime=None, plat=None, machine=None):
    """Bytes the build still needs on disk: what is left of its archive to download, and its unpacked size (an estimate) when it is not
    unpacked yet; None when this system has no pinned build."""
    runtime = runtime or RUNTIME
    if missing_pins(runtime, False, plat, machine):
        return None
    if runtime_ready(d, runtime, plat, machine):
        return 0
    a, p = aiollama.runtime_asset(runtime, plat, machine), archive_path(d, runtime, plat, machine)
    left = 0 if is_verified(d, p, a["sha256"], a["size"]) else a["size"] - (os.path.getsize(p + ".part") if os.path.exists(p + ".part") else 0)
    return max(0, left) + int(a["size"] * aiollama.UNPACK_FACTOR)


def model_bytes(m):
    """What a model weighs: the registry's size when it is pinned, else the estimate."""
    return m.get("size") or int((m.get("approx_mb") or 0) * 10 ** 6)


# ---------------------------------------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------------------------------------
def config_path(arg=None):
    return arg or os.environ.get("NUC_CONSOLE_CONFIG", nuc_config.DEFAULT_PATH)


def endpoint_for(port):
    return "http://%s:%d/v1" % (LOOPBACK, int(port))


def write_config(path, endpoint, model_id):
    """[ai] endpoint and model, through nuc_config.set_key: only those two lines change, comments and the rest are kept."""
    nuc_config.set_key(path, "ai", "endpoint", endpoint)
    nuc_config.set_key(path, "ai", "model", model_id)


def offer_config(path, endpoint, model_id, assume_yes, ask=input):
    cur = nuc_config.load(path)["ai"]
    lines = "  [ai]\n  endpoint = %s\n  model = %s" % (endpoint, model_id)
    if cur["endpoint"] == endpoint and cur["model"] == model_id:
        print("config: %s already has this [ai] endpoint and model" % path)
        return
    custom = cur["endpoint"] != SHIPPED_ENDPOINT or cur["model"]
    if assume_yes and custom:  # --yes is consent to a first setup, not to replacing a server the admin chose
        print("config: %s already points at %s (model '%s'): left as it is. To use this server set:\n%s"
              % (path, safe(cur["endpoint"]), safe(cur["model"]), lines))
        return
    if not confirm("Write [ai] endpoint = %s and model = %s in %s?" % (endpoint, model_id, path), assume_yes, ask):
        print("config: not changed. To use this server set in %s:\n%s" % (path, lines))
        return
    try:
        write_config(path, endpoint, model_id)
    except OSError as e:
        print("config: cannot write %s (%s). Set by hand:\n%s" % (path, e.strerror or e, lines))
        return
    print("config: %s updated (endpoint, model). The advisor stays off until [ai] enabled = yes." % path)


# ---------------------------------------------------------------------------------------------------------------------------
# models: the hardware and the catalog
# ---------------------------------------------------------------------------------------------------------------------------
def fmt_mb(mb):
    """MB (10^6 bytes, the unit of file sizes) -> '5.0 GB' / '400 MB'."""
    if not mb:
        return "?"
    return "%.1f GB" % (mb / 1000.0) if mb >= 1000 else "%d MB" % mb


def fmt_ram(mb):
    """RAM and VRAM as the machine reports them (MiB) -> '16.0 GB'."""
    return "unknown" if mb is None else "%.1f GB" % (mb / 1024.0)


def fmt_mem(mb):
    """What a model needs is memory, counted in MiB like the RAM it is compared with -> '5.7 GB' / '700 MB' (as aihw's sentences say it)."""
    if not mb:
        return "?"
    return "%.1f GB" % (mb / 1024.0) if mb >= 1024 else "%d MB" % mb


def size_text(m):
    """The pinned size when there is one, else the approximate one ('~5.0 GB'): for choosing, not for downloading."""
    return fmt_size(m["size"]) if m.get("size") else "~" + fmt_mb(m.get("approx_mb"))


def _rate(x):
    """One speed like aihw rounds it: a decimal below 10 tokens/s ('0.5': a whole number would show 0), whole numbers from 10 on."""
    x = float(x)
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("not a speed")
    x = max(x, 0.1)
    return "%d" % round(x) if round(x, 1) >= 10 else "%.1f" % x


def speed_text(a):
    """The estimated tokens/s of an assessment as a range: '0.5-3.2', '8.4-17', '120-340'; '?' when there is no estimate."""
    t = (a or {}).get("tok_s")
    try:
        lo, hi = _rate(t[0]), _rate(t[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return "?"
    return lo if lo == hi else "%s-%s" % (lo, hi)


def hw_lines(hw):
    """The machine in a few plain lines: what the advice is based on, and what could not be read."""
    if not hw:
        return ["hardware : not detected (no advice on what fits)"]
    cpu, ram = hw.get("cpu") or {}, hw.get("ram") or {}
    flags = [k for k in ("avx2", "avx512", "neon") if any(str(f).startswith(k) for f in cpu.get("flags") or [])]
    cores = [x for x in ("%s cores" % cpu["cores"] if cpu.get("cores") else "", "%s threads" % cpu["threads"] if cpu.get("threads") else "") if x]
    free = ", %s free" % fmt_ram(ram["available_mb"]) if ram.get("available_mb") is not None else ""
    out = ["CPU      : %s%s%s" % (safe(cpu.get("model") or "unknown"), " (%s)" % ", ".join(cores) if cores else "", " " + " ".join(flags) if flags else ""),
           "RAM      : %s%s" % (fmt_ram(ram.get("total_mb")), free)]
    for g in hw.get("gpus") or []:
        if g.get("unified"):
            mem = "unified memory (it uses the RAM)"
        elif g.get("vram_mb") is None:
            mem = "memory unknown"
        else:
            mem = fmt_ram(g["vram_mb"]) + (", %s free" % fmt_ram(g["vram_free_mb"]) if g.get("vram_free_mb") is not None else "")
        out.append("GPU      : %s, %s, %s" % (safe(g.get("name") or "unknown"), mem, g.get("backend") or "none"))
    if not hw.get("gpus"):
        out.append("GPU      : none found: the CPU does the work")
    return out + ["note     : %s" % safe(n, 120) for n in hw.get("notes") or []]


def state_text(m, active):
    if m["installed"]:
        return "installed, ACTIVE" if m["id"] == active else "installed"
    return "not installed" if m["pinned"] else "not pinned yet"


def model_row(m, mark, state):
    a = m["assess"]
    need = "~" + fmt_mem(a["need_mb"]) if a["need_mb"] else "?"
    return "%s %-15s %-27s %8s %8s  %-8s %9s  %s" % (mark, m["id"], m["name"], size_text(m), need, VERDICT_LABEL.get(a["verdict"], "?"), speed_text(a), state)


def cmd_models(args, runtime=None, models=None, hw=None):
    """The hardware found and every model of the catalog, best first: does it fit, a rough speed, installed, active, recommended (*)."""
    d = args.dir or find_dir()
    hw = hw if hw is not None else _hardware()
    cat = catalog(hw, d, models, runtime, nuc_config.load(config_path(args.config)))
    print("This machine (the advice below is based on it):")
    for ln in hw_lines(hw):
        print("  " + ln)
    print("  Models   : %s (%s)" % (safe(d, 200), space_text(cat["space"])))
    print("\n  %-15s %-27s %8s %8s  %-8s %9s  %s" % ("ID", "MODEL", "SIZE", "NEEDS", "FITS", "TOK/S", "STATE"))
    for m in cat["models"]:
        print(model_row(m, "*" if m["id"] == cat["recommended"] else " ", state_text(m, cat["active"])))
    rec = next((m for m in cat["models"] if m["id"] == cat["recommended"]), None)
    if rec:
        print("\n* recommended for this machine: %s: %s" % (rec["id"], safe(rec["assess"]["why"], 200)))
    else:
        print("\n* recommended: none (%s)" % ("nothing in the catalog fits comfortably" if hw else "the machine could not be assessed"))
    print("FITS: GPU = all on the GPU; GPU+CPU = partly on the GPU; RAM = fits in memory; SLOW = fits, but the PC\n"
          "      will slow down a lot; TOO BIG = will not work here.\n"
          "TOK/S is a rough estimate of the generation speed, not a promise. NEEDS: with %d tokens of context." % DEFAULT_CTX)
    c = commands_for("ID")
    print("Install: %s\nSwitch : %s\nRemove : %s" % (c["install"], c["use"], c["remove"]))
    return 0


# ---------------------------------------------------------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------------------------------------------------------
def show_choices(chosen, runtime, hw, recommended=False, have_server=False):
    print("Local model%s for the HEALTH advisor (open model from the Ollama library, served on %s only)%s:" % (
        "s" if len(chosen) > 1 else "", LOOPBACK, ", recommended for this machine" if recommended else ""))
    print("   %-15s %-27s %8s %8s  %-8s %s" % ("ID", "NAME", "SIZE", "NEEDS", "FITS", "LICENCE"))
    for m in chosen:
        a = assess_model(m, hw) or {}
        need = "~" + fmt_mem(a.get("need_mb") or m["ram_mb"])
        print("   %-15s %-27s %8s %8s  %-8s %s" % (m["id"], m["name"], size_text(m), need, VERDICT_LABEL.get(a.get("verdict"), "?"), m["license"]))
    a = aiollama.runtime_asset(runtime)
    print("   server : Ollama %s (%s)%s" % (runtime["version"], runtime["license"], ", installed" if have_server else
                                            ", %s to download" % fmt_size(a["size"]) if a and a.get("size") else ""))


def check_fit(chosen, hw, total, force):
    """Print a warning for a model that will slow the PC down, refuse (SetupError) one that will not work unless `force`."""
    refused = []
    for m in chosen:
        a = assess_model(m, hw)
        if a is None:  # no advice: the plain RAM rule
            if total and total < m["ram_mb"]:
                print("\nwarning: this machine has about %s of RAM and %s needs about %s: it will swap or fail. "
                      "Choose a smaller one (nuc-console-ai models)." % (fmt_mem(total), m["id"], fmt_mem(m["ram_mb"])))
        elif a["verdict"] == "no":
            if force:
                print("\nwarning: %s will not work on this machine, installing it anyway (--force): %s" % (m["id"], safe(a["why"], 200)))
            else:
                refused.append("%s will not work on this machine: %s" % (m["id"], safe(a["why"], 200)))
        elif a["verdict"] == "slow":
            print("\nwarning: %s fits, but the PC will slow down a lot while it runs: %s" % (m["id"], safe(a["why"], 200)))
    if refused:
        raise SetupError("%s. Choose a smaller model (nuc-console-ai models), or add --force to install it anyway" % "; ".join(refused))


def cmd_setup(args, runtime=None, models=None, allow_loopback_http=False, ask=input, hw=None, popen=subprocess.Popen):
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    d = args.dir or default_dir()
    hw = hw if hw is not None else _hardware()
    total = ((hw or {}).get("ram") or {}).get("total_mb") or memory_mb()[0]
    asked = list(dict.fromkeys(list(args.models or []) + list(args.model_opt or [])))  # --model is the older spelling
    chosen = [find_model(i, models) for i in asked] or [pick_default(total, models, hw)]
    show_choices(chosen, runtime, hw, not asked, runtime_ready(d, runtime))
    check_fit(chosen, hw, total, args.force)  # first: a model that cannot work here is not worth a word about pins
    unpinned = [(n, bad) for n, bad in [("the server", missing_pins(runtime, False))] + [(m["id"], missing_pins(m, True)) for m in chosen] if bad]
    if unpinned:
        print("\nnuc-console-ai: nothing downloaded: this build does not pin everything it needs.", file=sys.stderr)
        for n, bad in unpinned:
            print("  %s: not pinned: %s" % (n, ", ".join(bad)), file=sys.stderr)
        print("A maintainer fills them in with `python3 aisetup.py pins` (values come from the GitHub API and the Ollama registry, "
              "never from memory). Downloads are only ever made from pinned values.", file=sys.stderr)
        return 1
    a, arch = aiollama.runtime_asset(runtime), archive_path(d, runtime)
    have_rt = runtime_ready(d, runtime)
    have_arch = have_rt or is_verified(d, arch, a["sha256"], a["size"], rehash=True)  # hashed once: it takes seconds for a 1 GB file
    todo = [m for m in chosen if not model_ready(d, m)]
    need = (runtime_bytes(d, runtime) or 0) + sum(model_bytes(m) for m in todo)
    if need and free_bytes(d) < need + DISK_MARGIN:
        raise SetupError("not enough free disk space in %s: %s needed, %s free (use --dir for another disk)"
                         % (d, fmt_size(need + DISK_MARGIN), fmt_size(free_bytes(d))))
    fetch = (0 if have_arch else a["size"]) + sum(model_bytes(m) for m in todo)
    if fetch and not confirm("\nDownload about %s into %s?" % (fmt_size(fetch), d), args.yes, ask):
        print("nuc-console-ai: nothing downloaded.", file=sys.stderr)
        return 1
    ensure_dirs(d)
    if have_rt:
        print("server: Ollama %s already there: not downloaded again" % runtime["version"])
    else:
        if have_arch:
            record(d, arch, a["sha256"])
            print("server: %s already there, SHA-256 verified: not downloaded again" % a["file"])
        else:
            print("server: downloading %s (%s)" % (a["file"], fmt_size(a["size"])))
            how = download(a["url"], arch, a["sha256"], a["size"], allow_loopback_http=allow_loopback_http, progress=make_progress("server"))
            record(d, arch, a["sha256"])
            print("server: %s, SHA-256 verified" % how)
        print("server: unpacking into %s" % aiollama.runtime_dir(d, runtime), flush=True)
        unpack_runtime(d, runtime)
    for m in chosen:
        if m not in todo:
            print("%s: already installed: not downloaded again" % m["id"])
    if todo:
        with TempServer(d, runtime, popen) as api:
            for m in todo:
                print("%s: downloading %s from the Ollama library (%s)" % (m["id"], model_name(m), size_text(m)), flush=True)
                prog = make_progress(m["id"])
                install_model(api, d, m, lambda done, tot, _st: prog(done, tot) if tot else None, insecure=allow_loopback_http)
                print("%s: installed (%s)" % (m["id"], fmt_size(aiollama.model_bytes(aiollama.models_dir(d), m["id"]))))
    if not args.no_config:
        offer_config(config_path(args.config), endpoint_for(args.port), chosen[0]["id"], args.yes, ask)
    print("\nready. Run it:   nuc-console-ai serve --port %d     (foreground; Ctrl+C stops)\n"
          "or as a service: sudo nuc-console-ai serve --install-service\nThen set [ai] enabled = yes. Check: nuc-console-ai status" % args.port)
    if len(chosen) > 1:
        print("The server serves every installed model; the advisor asks one (%s here): switch with nuc-console-ai use ID." % ", ".join(m["id"] for m in chosen))
    return 0


def cmd_use(args, runtime=None, models=None, hw=None):
    """Make an installed model the one the advisor asks for: [ai] model in config.ini (nothing else is changed)."""
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    d = args.dir or default_dir()
    m = find_model(args.model, models)
    if m not in installed_models(d, models, runtime):
        raise SetupError("%s is not installed in %s: run nuc-console-ai setup %s" % (m["id"], d, m["id"]))
    a = assess_model(m, hw if hw is not None else _hardware())
    if a and a["verdict"] == "no" and not args.force:
        raise SetupError("%s will not work on this machine: %s. Add --force to use it anyway" % (m["id"], safe(a["why"], 200)))
    if a and a["verdict"] in ("no", "slow"):
        print("warning: %s %s: %s" % (m["id"], "will not work here" if a["verdict"] == "no" else "will slow the PC down a lot", safe(a["why"], 200)))
    path = config_path(args.config)
    try:
        nuc_config.set_key(path, "ai", "model", m["id"])
    except OSError as e:
        raise SetupError("cannot write %s (%s): run as root/administrator, or set model = %s under [ai] by hand" % (path, e.strerror or e, m["id"]))
    ai = nuc_config.load(path)["ai"]
    print("config: %s: [ai] model = %s" % (path, m["id"]))
    if ai["endpoint"] == SHIPPED_ENDPOINT:
        print("note: [ai] endpoint is still the default of an Ollama you run yourself (port 11434); for the server of this tool set endpoint = %s"
              % endpoint_for(DEFAULT_PORT))
    if not ai["enabled"]:
        print("note: [ai] enabled = no: the advisor does not use it yet")
    print("The server serves every installed model: the advisor asks %s from its next question, nothing to restart." % m["id"])
    return 0


# ---------------------------------------------------------------------------------------------------------------------------
# serve and services
# ---------------------------------------------------------------------------------------------------------------------------
def installed_models(d, models=None, runtime=None):
    """Models of the list that are installed while the server is too."""
    runtime = runtime if runtime is not None else RUNTIME
    if not runtime_ready(d, runtime):
        return []
    return [m for m in (models if models is not None else MODELS) if model_ready(d, m)]


def choose_model(args, d, models=None, runtime=None, cfg_model=None):
    models = models if models is not None else MODELS
    if args.model:
        m = find_model(args.model, models)
        if m not in installed_models(d, models, runtime):
            raise SetupError("%s is not installed in %s: run nuc-console-ai setup %s" % (m["id"], d, m["id"]))
        return m
    have = installed_models(d, models, runtime)
    if not have:
        raise SetupError("no server and model installed in %s: run nuc-console-ai setup first" % d)
    return next((m for m in have if m["id"] == cfg_model), None) or next((m for m in have if m["id"] == DEFAULT_MODEL), have[0])


def systemd_quote(arg):
    """One argument of ExecStart= (systemd splits on spaces, expands %specifiers and $VARIABLES)."""
    if re.fullmatch(r"[A-Za-z0-9_./:=+@,-]+", arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def systemd_unit(argv, ram_mb=None, gpu=False, groups=()):
    """gpu: the server uses the GPU, so the device nodes (/dev/nvidia*, /dev/dri, /dev/kfd) must exist for it and `groups` (render,
    video: the ones the machine has) let the service user open them; everything else of the sandbox stays."""
    state = "/var/lib/" + SERVICE_NAME  # writable by the service user only (StateDirectory): the server keeps its key here (~/.ollama)
    lines = [
        "[Unit]", "Description=nuc-console local AI model server (Ollama, %s only)" % LOOPBACK, "After=network.target", "",
        "[Service]", "Type=simple", "User=" + SERVICE_NAME,
        "ExecStart=" + " ".join(systemd_quote(a) for a in argv),
        "Environment=HOME=%s TMPDIR=%s" % (state, state), "StateDirectory=" + SERVICE_NAME,
        "Nice=10", "Restart=on-failure", "RestartSec=10",
        "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=" + ("no" if gpu else "yes"),
        "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes", "ProtectClock=yes",
        "ProtectHostname=yes", "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "RestrictNamespaces=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes", "TasksMax=512",
    ]
    if ram_mb:
        lines.append("MemoryMax=%dM" % int(ram_mb * 1.5))
    if gpu and groups:
        lines.append("SupplementaryGroups=" + " ".join(groups))
    return "\n".join(lines + ["", "[Install]", "WantedBy=multi-user.target", ""])


def launchd_plist(argv, home, log):
    return plistlib.dumps({
        "Label": MAC_LABEL, "ProgramArguments": list(argv), "UserName": MAC_USER, "GroupName": MAC_USER,
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10, "Nice": 10, "ProcessType": "Background",
        "EnvironmentVariables": {"HOME": home, "TMPDIR": home, "PATH": "/usr/bin:/bin"},
        "StandardOutPath": log, "StandardErrorPath": log})


def task_xml(command, arguments):
    """Windows scheduled task: at boot, as LOCAL SERVICE (S-1-5-19), below-normal priority, restarted when it stops."""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo><Description>nuc-console local AI model server (Ollama, 127.0.0.1 only)</Description></RegistrationInfo>\n"
        "  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>\n"
        '  <Principals><Principal id="Author"><UserId>S-1-5-19</UserId><LogonType>ServiceAccount</LogonType>'
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
        "  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate>"
        "<StartWhenAvailable>true</StartWhenAvailable><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled>"
        "<Hidden>false</Hidden><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority>"
        "<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure></Settings>\n"
        '  <Actions Context="Author"><Exec><Command>%s</Command><Arguments>%s</Arguments></Exec></Actions>\n'
        "</Task>\n" % (xml_escape(command), xml_escape(arguments)))


def service_argv(d, model, port, ctx, plat=None, python=None, script=None, gpu=True):
    """The service runs `aisetup.py serve ...` with every value explicit (the service user reads no config of ours): --cpu too, decided when
    the service is installed. A new model or [ai] gpu = a new `serve --install-service`."""
    python = python or sys.executable
    if _is_win(plat) and python.lower().endswith("python.exe") and os.path.exists(python[:-10] + "pythonw.exe"):
        python = python[:-10] + "pythonw.exe"  # no console window
    argv = [python, "-B", script or os.path.realpath(__file__), "serve", "--dir", d, "--model", model["id"], "--port", str(port), "--ctx", str(ctx)]
    if not gpu:
        argv.append("--cpu")
    if _is_win(plat):
        argv += ["--log", ntpath.join(os.environ.get("ProgramData") or r"C:\ProgramData", "nuc-console", "logs", "ai.log")]
    return argv


def _readable_by_all(path, stop=None):
    """Every directory above `path` (up to `stop`, tests) can be entered, and the file read, by any user (the service user is not
    the one who ran setup)."""
    p, stop = os.path.abspath(path), stop and os.path.abspath(stop)
    while True:
        try:
            mode = os.stat(p).st_mode
        except OSError:
            return False
        need = 0o005 if os.path.isdir(p) else 0o004
        if mode & need != need:
            return False
        parent = os.path.dirname(p)
        if parent == p or p == stop:
            return True
        p = parent


def _write_root_file(path, data, mode=0o644):
    tmp = path + ".tmp"
    with open(tmp, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _linux_user():
    import pwd
    try:
        pwd.getpwnam(SERVICE_NAME)
    except KeyError:
        run([tool("useradd"), "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", SERVICE_NAME])


def _mac_user():
    dscl = "/usr/bin/dscl"
    if run([dscl, ".", "-read", "/Users/" + MAC_USER], check=False).returncode == 0:
        return
    free = None
    for i in range(400, 500):  # a hidden service account in the system range, like the web view's
        if not run([dscl, ".", "-search", "/Users", "UniqueID", str(i)]).stdout.strip() and \
                not run([dscl, ".", "-search", "/Groups", "PrimaryGroupID", str(i)]).stdout.strip():
            free = str(i)
            break
    if free is None:
        raise SetupError("no free user id for %s" % MAC_USER)
    for rec, key, val in (("/Groups/" + MAC_USER, "PrimaryGroupID", free), ("/Users/" + MAC_USER, "UniqueID", free),
                          ("/Users/" + MAC_USER, "PrimaryGroupID", free), ("/Users/" + MAC_USER, "UserShell", "/usr/bin/false"),
                          ("/Users/" + MAC_USER, "NFSHomeDirectory", "/var/empty"), ("/Users/" + MAC_USER, "RealName", "nuc-console AI server"),
                          ("/Users/" + MAC_USER, "IsHidden", "1"), ("/Users/" + MAC_USER, "Password", "*")):
        run([dscl, ".", "-create", rec, key, val])


def _gpu_groups():
    """The groups that own the GPU device nodes (Linux: render for /dev/dri and /dev/kfd, video) that this machine has."""
    import grp
    out = []
    for g in ("render", "video"):
        try:
            grp.getgrnam(g)
            out.append(g)
        except KeyError:
            pass
    return out


def install_service(d, model, port, ctx, plat=None, gpu=True):
    if not is_root():
        raise SetupError("--install-service needs %s" % ("an administrator prompt" if _is_win(plat) else "root: sudo nuc-console-ai serve --install-service"))
    if not _is_win(plat) and not (_readable_by_all(runtime_path(d)) and _readable_by_all(model_path(d, model))):
        raise SetupError("%s is not readable by other users (the service runs as its own account): run setup as root, or use --dir "
                         "on a shared path" % d)
    argv = service_argv(d, model, port, ctx, plat, gpu=gpu)
    if _is_win(plat):
        # a task that is running keeps its old model and ignores /Run: stop it first
        subprocess.run([tool("schtasks", plat), "/End", "/TN", "\\nuc-console\\ai"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        subprocess.run([tool("icacls", plat), d, "/grant", "*S-1-5-19:(OI)(CI)RX"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        logs = os.path.dirname(argv[-1])
        os.makedirs(logs, exist_ok=True)
        subprocess.run([tool("icacls", plat), logs, "/grant", "*S-1-5-19:(OI)(CI)M"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        with tempfile.TemporaryDirectory() as t:
            xml = os.path.join(t, "task.xml")
            with open(xml, "w", encoding="utf-16") as f:
                f.write(task_xml(argv[0], subprocess.list2cmdline(argv[1:])))
            run([tool("schtasks", plat), "/Create", "/TN", "\\nuc-console\\ai", "/XML", xml, "/F"])
        run([tool("schtasks", plat), "/Run", "/TN", "\\nuc-console\\ai"])
        print("service: scheduled task \\nuc-console\\ai (LOCAL SERVICE, at boot, below-normal priority) started; log: %s" % argv[-1])
    elif _is_mac(plat):
        _mac_user()
        home, log, plist = "/var/db/" + SERVICE_NAME, "/var/log/nuc-console/ai.log", "/Library/LaunchDaemons/%s.plist" % MAC_LABEL
        for p in (home, "/var/log/nuc-console"):
            os.makedirs(p, exist_ok=True)
        shutil.chown(home, MAC_USER, MAC_USER)
        os.chmod(home, 0o755)
        open(log, "a").close()
        shutil.chown(log, MAC_USER, MAC_USER)
        _write_root_file("/etc/newsyslog.d/%s.conf" % SERVICE_NAME, "%s  %s:%s  644  5  1024  *  NJ\n" % (log, MAC_USER, MAC_USER))
        subprocess.run([tool("launchctl"), "bootout", "system/" + MAC_LABEL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _write_root_file(plist, launchd_plist(argv, home, log))
        shutil.chown(plist, "root", "wheel")
        run([tool("launchctl"), "bootstrap", "system", plist])
        print("service: launchd daemon %s (user %s, 127.0.0.1 only); log: %s" % (MAC_LABEL, MAC_USER, log))
    else:
        _linux_user()
        unit = "/etc/systemd/system/%s.service" % SERVICE_NAME
        _write_root_file(unit, systemd_unit(argv, model["ram_mb"], gpu=gpu, groups=_gpu_groups() if gpu else ()))
        systemctl = tool("systemctl")
        run([systemctl, "daemon-reload"])
        run([systemctl, "enable", SERVICE_NAME + ".service"])
        run([systemctl, "restart", SERVICE_NAME + ".service"])
        print("service: %s.service (user %s, sandboxed, 127.0.0.1 only) started. Logs: journalctl -u %s" % (SERVICE_NAME, SERVICE_NAME, SERVICE_NAME))
    print("model %s on %s, %s (the first answer may take a minute: the model is loaded into memory)"
          % (model["id"], endpoint_for(port), gpu_text(gpu)))


def remove_service(plat=None):
    if not is_root():
        raise SetupError("--remove-service needs %s" % ("an administrator prompt" if _is_win(plat) else "root"))
    if _is_win(plat):
        subprocess.run([tool("schtasks", plat), "/End", "/TN", "\\nuc-console\\ai"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        run([tool("schtasks", plat), "/Delete", "/TN", "\\nuc-console\\ai", "/F"], check=False)
    elif _is_mac(plat):
        subprocess.run([tool("launchctl"), "bootout", "system/" + MAC_LABEL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for p in ("/Library/LaunchDaemons/%s.plist" % MAC_LABEL, "/etc/newsyslog.d/%s.conf" % SERVICE_NAME):
            if os.path.exists(p):
                os.unlink(p)
    else:
        systemctl = tool("systemctl")
        run([systemctl, "disable", "--now", SERVICE_NAME + ".service"], check=False)
        unit = "/etc/systemd/system/%s.service" % SERVICE_NAME
        if os.path.exists(unit):
            os.unlink(unit)
        run([systemctl, "daemon-reload"], check=False)
    print("service removed (the downloaded model, the log and the %s account are left: nuc-console-ai remove deletes the files)" % SERVICE_NAME)


def cmd_serve(args, runtime=None, models=None, hw=None):
    runtime = runtime if runtime is not None else RUNTIME
    if args.remove_service:
        remove_service()
        return 0
    d = args.dir or default_dir()
    ai = nuc_config.load(config_path(args.config))["ai"]
    model = choose_model(args, d, models, runtime, ai["model"])
    if model.get("ctx_max") and args.ctx > model["ctx_max"]:
        raise SetupError("%s handles %d tokens of context at most: use --ctx %d or less" % (model["id"], model["ctx_max"], model["ctx_max"]))
    if args.cpu or args.gpu_layers == 0:  # explicit (the service passes it: it was decided when the service was installed); --gpu-layers 0: older services
        gpu, why = False, "CPU only (--cpu)"
    else:
        gpu, why = gpu_plan(ai["gpu"])
    argv = serve_argv(d, runtime)
    env = serve_env(d, args.port, args.ctx, gpu)
    if args.install_service:
        # the AI page and screen download into this folder as the unprivileged web account, which can then replace what is in it: the service runs
        # these files as another account, so the server's archive is hashed again and unpacked again, and every layer of the model is hashed (docs/AI.md)
        print("checking the SHA-256 of the files the service will run...", flush=True)
        if not runtime_ready(d, runtime, rehash=True):
            raise SetupError("the server's archive in %s is missing or not the pinned file (its SHA-256 differs): run nuc-console-ai setup again, nothing was installed" % d)
        unpack_runtime(d, runtime)
        if not model_ready(d, model, rehash=True):
            raise SetupError("model %s in %s has a file whose SHA-256 is not its name: run nuc-console-ai remove %s and setup %s again, nothing was installed"
                             % (model["id"], d, model["id"], model["id"]))
        install_service(d, model, args.port, args.ctx, gpu=gpu)
        return 0
    if args.dry_run:
        shown = ["%s=%s" % (k, v) for k, v in sorted(env.items()) if k.startswith("OLLAMA_") or k in aiollama.GPU_HIDE]
        print(" ".join(shown + ([subprocess.list2cmdline(argv)] if _is_win() else [shlex.quote(x) for x in argv])))
        return 0
    if args.log:
        nuc_config.log_to(args.log)
    print("nuc-console-ai: Ollama %s on %s (models in %s, context %d, %s, low priority); the advisor asks %s. Ctrl+C stops."
          % (runtime["version"], endpoint_for(args.port), aiollama.models_dir(d), args.ctx, gpu_text(gpu), model["id"]), flush=True)
    if why:
        print("nuc-console-ai: %s" % safe(why, 200), flush=True)
    return run_server(argv, env)


# ---------------------------------------------------------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------------------------------------------------------
def probe(endpoint, timeout=3):
    """GET <endpoint>/models -> (ok, [model ids], why). Never through a proxy; http(s) only."""
    if urllib.parse.urlsplit(endpoint).scheme not in ("http", "https"):
        return False, [], "not an http(s) endpoint"
    req = urllib.request.Request(endpoint.rstrip("/") + "/models", headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            body = r.read(1 << 20)
    except urllib.error.HTTPError as e:
        e.close()
        return False, [], "HTTP %d" % e.code
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        return False, [], safe(getattr(e, "reason", e))
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        items = data.get("data", False) if isinstance(data, dict) else False
        if items is None:  # Ollama with no model yet
            items = []
        if not isinstance(items, list):
            return False, [], "the answer has no model list (is this an OpenAI-compatible /v1?)"
        return True, [short_id(safe(m.get("id"))) for m in items if isinstance(m, dict) and m.get("id")], ""
    except ValueError:
        return False, [], "the answer is not JSON"


def short_id(model_id):
    """'qwen3-8b:latest' -> 'qwen3-8b': Ollama lists a model with its tag, the catalog and [ai] model name it without the default one."""
    return model_id[:-7] if model_id.endswith(":latest") else model_id


def is_loopback_endpoint(endpoint):
    return urllib.parse.urlsplit(endpoint).hostname in (LOOPBACK, "localhost", "::1")


def cmd_status(args, runtime=None, models=None):
    """Exit status: 0 the endpoint answers (with the configured model); 3 it does not but a model is installed (run serve);
    1 it does not and nothing is installed (run setup)."""
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    d = args.dir or find_dir()  # like `models`: after `sudo setup`, an unprivileged status looks where the administrator installed
    cfg_file = config_path(args.config)
    ai = nuc_config.load(cfg_file)["ai"]
    print("nuc-console-ai status")
    print("  directory : %s  (%s)" % (d, space_text(dir_space(d))))
    if missing_pins(runtime, False):
        rt = "not pinned for this system in this build (setup refuses to download it): %s" % ", ".join(missing_pins(runtime, False))
    elif runtime_ready(d, runtime, rehash=args.verify):
        rt = "installed%s" % (", its archive SHA-256 verified" if args.verify else "")
    elif os.path.exists(runtime_path(d, runtime)):
        rt = "present but NOT verified (run setup again)"
    else:
        rt = "not installed"
    print("  server    : Ollama %s: %s" % (runtime["version"], rt))
    absent = 0
    for m in models:
        if not os.path.isfile(model_path(d, m)):  # the catalog is long: only what is on disk is listed here
            absent += 1
            continue
        st = ("installed%s" % (", every layer SHA-256 verified" if args.verify else "")) if model_ready(d, m, rehash=args.verify) else \
            "present but INCOMPLETE (run nuc-console-ai setup %s again)" % m["id"]
        print("  model     : %-15s %-28s %s%s" % (m["id"], m["name"], st, "  (active)" if m["id"] == ai["model"] else ""))
    if absent:
        print("  models    : %d more in the catalog, not installed (nuc-console-ai models)" % absent)
    ready = [m for m in models if runtime_ready(d, runtime) and model_ready(d, m)]
    print("  config    : %s: [ai] enabled = %s, endpoint = %s, model = %s, allow_remote = %s"
          % (cfg_file, "yes" if ai["enabled"] else "no", safe(ai["endpoint"]), safe(ai["model"]) or "(empty)", "yes" if ai["allow_remote"] else "no"))
    endpoint = args.endpoint or ai["endpoint"]
    if not is_loopback_endpoint(endpoint) and not ai["allow_remote"] and not args.endpoint:
        print("  endpoint  : %s is not on this machine and allow_remote = no: the advisor refuses it" % safe(endpoint))
        return 3 if ready else 1
    ok, ids, why = probe(endpoint)
    if not ok:
        print("  endpoint  : %s: not answering (%s)" % (safe(endpoint), why))
        print("  next      : %s" % ("nuc-console-ai serve  (or sudo nuc-console-ai serve --install-service)" if ready else "nuc-console-ai setup"))
        return 3 if ready else 1
    print("  endpoint  : %s: answering, /v1/models lists: %s" % (safe(endpoint), ", ".join(ids) or "(nothing)"))
    if ai["model"] and ai["model"] not in ids:
        print("  warning   : [ai] model = %s is not in that list" % safe(ai["model"]))
        return 3
    if not ai["enabled"]:
        print("  note      : [ai] enabled = no: the advisor does not use it yet")
    return 0


# ---------------------------------------------------------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------------------------------------------------------
def du(path):
    """Bytes of the files under `path` (a file or a folder; links are not followed); 0 when there is nothing."""
    if os.path.isfile(path) and not os.path.islink(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            q = os.path.join(root, f)
            try:
                total += 0 if os.path.islink(q) else os.path.getsize(q)
            except OSError:
                pass
    return total


def removal_plan(d, model=None, models=None):
    """What would be deleted: [(what, path, bytes)]. One model: its manifest and the layers no other model uses (aiollama.prune);
    everything: the folders runtime/ and models/ of the AI folder (the server's builds and archives, every model, partial downloads too)."""
    if model is not None:
        mdir = aiollama.models_dir(d)
        return [(model["id"], model_path(d, model), aiollama.model_bytes(mdir, model["id"]))] if os.path.isfile(model_path(d, model)) else []
    out = []
    for sub in ("runtime", "models"):
        p = os.path.join(d, sub)
        if os.path.isdir(p) and os.listdir(p):
            out.append((sub + "/", p, du(p)))
    return out


def remove_files(d, model=None, runtime=None):
    """Delete what removal_plan() lists (everything: the server's home too) -> (bytes freed, [(name, why)] that could not be deleted)."""
    runtime = runtime or RUNTIME
    if model is not None:
        return aiollama.prune(aiollama.models_dir(d), [model["id"]])
    freed, stuck = 0, []
    for sub in ("runtime", "models"):
        p = os.path.join(d, sub)
        if not os.path.isdir(p):
            continue
        before = du(p)

        def failed(_fn, path, exc):
            e = exc[1]
            stuck.append((os.path.basename(path), getattr(e, "strerror", None) or str(e)))
        shutil.rmtree(p, onerror=failed)
        freed += before - du(p)
    shutil.rmtree(os.path.join(d, "home"), ignore_errors=True)  # the server's key and the log of the servers setup started
    forget(d, runtime_key(runtime))
    for a in (runtime.get("assets") or {}).values():
        if a.get("file"):
            forget(d, a["file"])
    return freed, stuck


def cmd_remove(args, runtime=None, models=None, ask=input):
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    d = args.dir or default_dir()
    which = args.model or args.model_opt  # MODEL, or the older --model MODEL
    m = find_model(which, models) if which else None
    plan = removal_plan(d, m, models)
    if not plan:
        print("nothing to remove in %s" % d)
        return 0
    size = sum(n for _w, _p, n in plan)
    print("will delete from %s:" % d)
    for what, _p, n in plan:
        print("  %s  (%s)" % (what if m else what + " (everything in it)", fmt_size(n) if n else "empty"))
    if not confirm("Delete %s, %s?" % (m["id"] if m else "the server and every model", fmt_size(size)), args.yes, ask):
        print("nothing deleted.")
        return 1
    freed, stuck = remove_files(d, m, runtime)
    for name, why in stuck[:5]:
        print("cannot delete %s: %s (is the server still running?)" % (name, why), file=sys.stderr)
    print("deleted, %s freed. [ai] in config.ini is not changed: set enabled = no there if you do not use another server." % fmt_size(freed)
          if not stuck else "some files remain.")
    if which and not stuck and nuc_config.load(config_path(args.config))["ai"]["model"] == which:
        print("note: %s is the active model ([ai] model): choose another with nuc-console-ai use ID" % which)
    return 1 if stuck else 0


# ---------------------------------------------------------------------------------------------------------------------------
# pins (maintainers): read the values to pin from the GitHub API and the Ollama registry
# ---------------------------------------------------------------------------------------------------------------------------
def _headers(url, accept):
    """The request's headers; $GITHUB_TOKEN (the workflow's, for the API's rate limit) goes to api.github.com and nowhere else."""
    h = {"User-Agent": UA, "Accept": accept}
    if os.environ.get("GITHUB_TOKEN") and urllib.parse.urlsplit(url).hostname == "api.github.com":
        h["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    return h


def fetch_json(url, timeout=30, accept="application/json"):
    check_url(url)
    req = urllib.request.Request(url, headers=_headers(url, accept))
    with urllib.request.build_opener(_HttpsRedirects()).open(req, timeout=timeout) as r:
        return json.loads(r.read(8 << 20).decode("utf-8"))


def fetch_text(url, n=400, timeout=30):
    check_url(url)
    req = urllib.request.Request(url, headers=_headers(url, "*/*"))
    with urllib.request.build_opener(_HttpsRedirects()).open(req, timeout=timeout) as r:
        return r.read(n).decode("utf-8", "replace")


def sums_of(text):
    """A release's sha256sum.txt ('<hex>  ./<file>' per line) -> {file: hex}."""
    out = {}
    for ln in text.splitlines():
        m = re.match(r"^([0-9a-f]{64})\s+\*?(?:\./)?(\S+)$", ln.strip())
        if m:
            out[m.group(2)] = m.group(1)
    return out


def pin_from_release(release, url):
    """GitHub release JSON -> {"sha256", "size"} of the asset with this download URL (needs the release to publish digests)."""
    for a in release.get("assets", []) if isinstance(release, dict) else []:
        if a.get("browser_download_url") == url:
            digest = str(a.get("digest") or "")
            if not digest.startswith("sha256:") or not HEX64.match(digest[7:]):
                raise SetupError("the release publishes no SHA-256 for this asset: download it over a trusted link and run sha256sum")
            return {"sha256": digest[7:], "size": a["size"]}
    names = [str(a.get("name")) for a in release.get("assets", []) if isinstance(a, dict)] if isinstance(release, dict) else []
    raise SetupError("no asset %s in release %s (assets: %s)" % (url, release.get("tag_name") if isinstance(release, dict) else "?",
                                                                  ", ".join(names[:12]) or "none"))


def licence_words(text):
    """The first words of a licence text, enough to tell which it is ('Apache License Version 2.0', 'MIT License')."""
    return safe(" ".join(text.split())[:60], 60)


def cmd_pins(args, runtime=None, models=None):
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    bad = 0
    print("# paste into RUNTIME / MODELS of aisetup.py, check the licence, run the tests, try `setup` and `serve` for real\n")
    try:  # the newest server knows the newest model families: say which one it is
        print("RUNTIME latest release: %s" % fetch_json("https://api.github.com/repos/ollama/ollama/releases/latest").get("tag_name"))
    except (OSError, ValueError, http.client.HTTPException) as e:
        print("RUNTIME latest release: ERROR %s" % safe(e, 200))
    try:
        rel = fetch_json("https://api.github.com/repos/ollama/ollama/releases/tags/v" + runtime["version"])
        for key, a in sorted((runtime.get("assets") or {}).items()):
            try:
                print("RUNTIME %s %s: %s" % (runtime["version"], key, json.dumps(dict(file=a["file"], **pin_from_release(rel, runtime["base"] + a["file"])))))
            except (SetupError, KeyError) as e:
                bad += 1
                print("RUNTIME %s %s: ERROR %s" % (runtime["version"], key, safe(e, 200)))
    except (OSError, ValueError, KeyError, http.client.HTTPException) as e:
        bad += 1
        print("RUNTIME %s: ERROR %s" % (runtime["version"], safe(e, 200)))
    try:  # the release's own list of SHA-256, a second source for the same numbers
        sums = sums_of(fetch_text(runtime["base"] + "sha256sum.txt", 1 << 16))
        for key, a in sorted((runtime.get("assets") or {}).items()):
            ok = sums.get(a["file"]) == a["sha256"]
            bad += 0 if ok else 1
            print("RUNTIME %s %s: sha256sum.txt %s" % (runtime["version"], key, "agrees" if ok else "DIFFERS: %s" % sums.get(a["file"])))
    except (OSError, ValueError, KeyError, http.client.HTTPException) as e:
        bad += 1
        print("RUNTIME %s sha256sum.txt: ERROR %s" % (runtime["version"], safe(e, 200)))
    for m in models:
        try:
            man = fetch_json(aiollama.manifest_url(m["ollama"]), accept=aiollama.MANIFEST_TYPES)
            info = aiollama.manifest_summary(man)
            lic = licence_words(fetch_text(aiollama.blob_url(m["ollama"], info["license"]))) if info["license"] else "(no licence layer)"
            print("%s (%s): %s licence: %s" % (m["id"], m["ollama"], json.dumps({"size": info["size"], "approx_mb": round(info["model"] / 1e6)}), lic))
        except (SetupError, OSError, ValueError, KeyError, http.client.HTTPException) as e:
            bad += 1
            print("%s (%s): ERROR %s" % (m["id"], m.get("ollama"), safe(e, 200)))
    return 1 if bad else 0


# ---------------------------------------------------------------------------------------------------------------------------
def _port(s):
    n = int(s)
    if not 1 <= n <= 65535:
        raise argparse.ArgumentTypeError("a port is 1-65535")
    return n


def _ranged(lo, hi):
    def conv(s):
        n = int(s)
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError("must be between %d and %d" % (lo, hi))
        return n
    return conv


def build_parser():
    ap = argparse.ArgumentParser(prog="nuc-console-ai", description="A small local AI model for the HEALTH advisor, chosen by what this "
                                 "machine can run: look (models), install (setup), switch (use), serve on 127.0.0.1.",
                                 epilog="status exit codes: 0 the endpoint answers; 3 installed but not answering (start it); 1 not installed.")
    sub = ap.add_subparsers(dest="cmd", metavar="{models,setup,use,serve,status,remove}")
    sub.required = True

    def common(p, config=True, reads=False):
        p.add_argument("--dir", help=("cache directory (default: the system-wide one if `setup` put models there, else %s)" if reads
                                      else "cache directory (default: %s)") % default_dir())
        if config:
            p.add_argument("--config", help="config.ini to read/write (default: %s)" % config_path())
    p = sub.add_parser("models", help="the hardware found and every model: does it fit, how fast, installed, active, recommended")
    common(p, reads=True)
    p.set_defaults(func=cmd_models)
    p = sub.add_parser("setup", help="download the server (Ollama) and one or more models once, offer to write config.ini")
    common(p)
    p.add_argument("models", nargs="*", metavar="MODEL", help="model id(s) (default: the one recommended for this machine; see `models`)")
    p.add_argument("--model", action="append", dest="model_opt", metavar="MODEL", help=argparse.SUPPRESS)  # the older spelling of MODEL
    p.add_argument("--force", action="store_true", help="install a model that will not work on this machine anyway")
    p.add_argument("--port", type=_port, default=DEFAULT_PORT, help="port the server will use, for [ai] endpoint (default %(default)s)")
    p.add_argument("--yes", "-y", action="store_true", help="agree to the download and to writing a first [ai] config")
    p.add_argument("--no-config", action="store_true", help="do not offer to write config.ini")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("use", help="make an installed model the one the advisor asks for ([ai] model)")
    common(p)
    p.add_argument("model", metavar="MODEL", help="model id: one of those `models` lists as installed")
    p.add_argument("--force", action="store_true", help="use a model that will not work on this machine anyway")
    p.set_defaults(func=cmd_use)
    p = sub.add_parser("serve", help="run the server in the foreground on 127.0.0.1, at low priority, on the GPU when one holds the model")
    common(p)
    p.add_argument("--model", help="model id (default: [ai] model if installed, else the default one)")
    p.add_argument("--port", type=_port, default=DEFAULT_PORT, help="default %(default)s")
    p.add_argument("--threads", type=_ranged(1, 512), help=argparse.SUPPRESS)  # older services pass it: Ollama chooses its threads
    p.add_argument("--ctx", type=_ranged(512, 131072), default=DEFAULT_CTX, help="context tokens, default %(default)s")
    p.add_argument("--cpu", action="store_true", help="hide the GPUs from the server: CPU only (default: the GPU when one holds the model, "
                   "unless [ai] gpu = no)")
    p.add_argument("--gpu-layers", type=_ranged(0, 999), metavar="N", help=argparse.SUPPRESS)  # older services: 0 = --cpu, other numbers: ignored
    p.add_argument("--dry-run", action="store_true", help="print the command, do not start it")
    p.add_argument("--install-service", action="store_true", help="install and start it as a system service (root / administrator)")
    p.add_argument("--remove-service", action="store_true", help="stop and remove that service")
    p.add_argument("--log", help="Windows service: send output to this file")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("status", help="what is installed, whether the configured endpoint answers")
    common(p, reads=True)
    p.add_argument("--endpoint", help="probe this /v1 URL instead of [ai] endpoint")
    p.add_argument("--verify", action="store_true", help="hash the files again (the server's archive against its pin, every layer of the models against its name)")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("remove", help="delete the downloaded files of one model (or of everything)")
    common(p)
    p.add_argument("model", nargs="?", metavar="MODEL", help="only this model (default: every model and the server)")
    p.add_argument("--model", dest="model_opt", metavar="MODEL", help=argparse.SUPPRESS)  # the older spelling of MODEL
    p.add_argument("--yes", "-y", action="store_true", help="do not ask")
    p.set_defaults(func=cmd_remove)
    p = sub.add_parser("pins", help="maintainers: print the values to pin (network)")
    p.set_defaults(func=cmd_pins)
    return ap


def main(argv):
    args = build_parser().parse_args(argv[1:])
    try:
        return args.func(args)
    except SetupError as e:
        print("nuc-console-ai: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nnuc-console-ai: interrupted (a partial download is kept: run the command again to resume)", file=sys.stderr)
        return 130
    except PermissionError as e:
        print("nuc-console-ai: %s: permission denied (run as root/administrator, or use --dir)" % (e.filename or e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
