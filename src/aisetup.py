"""nuc-console-ai: choose a small local model by what this machine can run, install it once (setup), run it on 127.0.0.1 (serve).

  models  the hardware found (RAM, GPU and its memory) and every model of the catalog: does it fit (GPU / GPU+CPU / RAM / SLOW /
          TOO BIG), a rough speed, installed, active, the recommended one
  setup   download the runtime (llamafile, one executable for Linux, macOS and Windows) and one or several models (GGUF) into a
          cache directory, verify each against the SHA-256 pinned below, never download a file that is already there with the
          right hash; with your consent write [ai] endpoint/model in config.ini. No model named = the recommended one; a model
          that will not work here is refused unless --force
  use     make an installed, verified model the one the advisor asks for ([ai] model in config.ini)
  serve   run the server in the foreground, on 127.0.0.1 only, at low priority, on the GPU when the model fits there ([ai] gpu =
          no: never) (--install-service / --remove-service: a system service: systemd unit, launchd daemon or Windows scheduled
          task, each run by an unprivileged account)
  status  what is installed and verified, whether the configured endpoint answers
  remove  delete the downloaded files (one model, or everything)
  pins    for maintainers: prints the values to paste in RUNTIME and MODELS (needs the network)

Security: HTTPS only (a redirect to http:// is refused), every file is checked against a SHA-256 written in this file, downloads
go to "<name>.part" and are renamed only after the check (a mismatch deletes them), commands are argument lists (no shell), the
server binds to 127.0.0.1 and nothing here can change that, no auto-update: a new runtime or model means a new pin in a new release.
The advice (aihw.py) only reads the machine and does arithmetic: it never downloads and never hashes. catalog() is what the screens
call: it reads the stamp file only, so an unprivileged process can show it.
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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape as xml_escape

import nuc_config

LOOPBACK = "127.0.0.1"  # the only address the server is ever started on (serve_argv has no parameter for it)
DEFAULT_PORT = 8080
DEFAULT_CTX = 4096  # tokens of context: bounds the memory of the KV cache (a 32k default would need gigabytes)
ALL_LAYERS = 999  # the -ngl that means "every layer" (aihw counts the output layer too, so its number is one more than the model's own)
ALLOWED_LICENSES = ("Apache-2.0", "MIT")  # permissive only; tests refuse anything else in MODELS and RUNTIME
SHIPPED_ENDPOINT = "http://127.0.0.1:11434/v1"  # nuc_config's default [ai] endpoint (Ollama): setup --yes never replaces another one
UA = "nuc-console-ai/1"
DISK_MARGIN = 300 * 10 ** 6  # free space kept besides the files (the runtime unpacks a loader and caches in the home directory)
SERVICE_NAME = "nuc-console-ai"  # systemd unit, system user, Windows task name
MAC_LABEL, MAC_USER = "com.nuc-console.ai", "_nuc-console-ai"
UNIX_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

# ---------------------------------------------------------------------------------------------------------------------------
# The pins. A value that is None means "not pinned yet": `setup` refuses to download it. Never fill one from memory or from a
# web page: run `python3 aisetup.py pins` (it asks the Hugging Face and GitHub APIs) and check the output, then re-run the tests.
# ---------------------------------------------------------------------------------------------------------------------------
# llamafile (Mozilla, Apache-2.0): ONE executable for Linux, macOS, Windows, x86_64 and arm64; it runs a GGUF model given with -m
# and serves an OpenAI-compatible API under /v1. The tag in the URL pins the release; the SHA-256 pins the bytes.
# 0.10.x: the llama.cpp it is built on knows Qwen3, SmolLM3 and gpt-oss (0.9.x does not). The asset and its SHA-256 (the release's
# own digest, and the hash of the downloaded file: the same) were read by the ai-pins workflow on 2026-10-01. The project moved
# from Mozilla-Ocho to mozilla-ai: the URL is the new one (the old one redirects);
# the GPU flags serve_argv adds (--gpu auto -ngl N, or --gpu disable) must be confirmed against `llamafile --help` of the pinned
# version, and the newer models (SmolLM3, gpt-oss) against the llama.cpp that version is built on: an older runtime may not know them.
# "args" are extra arguments for every start; the GPU ones are not here because they depend on the machine (gpu_plan).
RUNTIME = {
    "name": "llamafile", "version": "0.10.6", "license": "Apache-2.0",
    "url": "https://github.com/mozilla-ai/llamafile/releases/download/0.10.6/llamafile-0.10.6",
    "sha256": "d579f61dcd3a306f518e6d90e599d77793ed5f09543023d09c96ad35fcfa63f0", "size": 368094430,
    "args": [],
}

# Instruct models, GGUF Q4_K_M, permissive licences, ordered best first (rank 1 = best for the advisor's job: short, grounded advice).
# "revision" is a Hugging Face COMMIT (never "main"), "sha256" and "size" are those of the file at that commit (the Hub's tree API:
# lfs.oid, lfs.size), read by `aisetup.py pins` (the ai-pins workflow runs it on GitHub) on 2026-10-01; a commit never changes.
# The rest is for ADVICE only (what fits, how fast), before and after pinning; a download never uses it: params_b (billions; MoE:
# active_b = parameters read per token), layers (transformer blocks: how many fit on a GPU becomes -ngl), ctx_max (tokens the model
# was trained for), approx_mb (approximate Q4_K_M file in MB of 10^6 bytes: the maintainer's estimate, not a measurement), ram_mb
# (approximate memory of the server with DEFAULT_CTX tokens of context). Qwen3 and SmolLM3 start in "thinking" mode (long <think>
# blocks): the advisor should ask for /no_think.
MODELS = [
    {"id": "qwen3-30b-a3b", "name": "Qwen3 30B-A3B (MoE)", "license": "Apache-2.0", "rank": 1, "params_b": 30.5, "active_b": 3.3,
     "quant": "Q4_K_M", "layers": 48, "ctx_max": 32768, "approx_mb": 18600, "ram_mb": 19300,
     "notes": "MoE: reads only 3.3B per token, fast on CPU if the RAM holds it; /no_think",
     "repo": "Qwen/Qwen3-30B-A3B-GGUF", "file": "Qwen3-30B-A3B-Q4_K_M.gguf", "revision": "e4d4bafdfb96a411a163846265362aceb0b9c63a",
     "sha256": "0d003f6662faee786ed5da3e31b29c978de5ae5d275c8794c606a7f3c01aa8f5", "size": 18556685824},
    {"id": "gpt-oss-20b", "name": "OpenAI gpt-oss 20B (MoE)", "license": "Apache-2.0", "rank": 2, "params_b": 21.0, "active_b": 3.6,
     "quant": "Q4_K_M", "layers": 24, "ctx_max": 131072, "approx_mb": 11600, "ram_mb": 12100,
     "notes": "MoE: reads only 3.6B per token; a reasoning model (long answers)",
     "repo": "unsloth/gpt-oss-20b-GGUF", "file": "gpt-oss-20b-Q4_K_M.gguf", "revision": "d449b42d93e1c2c7bda5312f5c25c8fb91dfa9b4",
     "sha256": "c27536640e410032865dc68781d80a08b98f8db5e93575919af8ccc0568aeb4f", "size": 11624759488},
    {"id": "phi-4", "name": "Phi-4 14B", "license": "MIT", "rank": 3, "params_b": 14.7, "quant": "Q4_K_M", "layers": 40,
     "ctx_max": 16384, "approx_mb": 9100, "ram_mb": 10300, "notes": "dense 14B: strong reasoning, slow without a GPU",
     "repo": "bartowski/phi-4-GGUF", "file": "phi-4-Q4_K_M.gguf", "revision": "19cd65f97c2f1712a81c506611d3f9c94b16a1e1",
     "sha256": "009aba717c09d4a35890c7d35eb59d54e1dba884c7c526e7197d9c13ab5911d9", "size": 9053114816},
    {"id": "qwen3-14b", "name": "Qwen3 14B", "license": "Apache-2.0", "rank": 4, "params_b": 14.8, "quant": "Q4_K_M", "layers": 40,
     "ctx_max": 32768, "approx_mb": 9000, "ram_mb": 10000, "notes": "dense 14B: slow without a GPU; /no_think",
     "repo": "Qwen/Qwen3-14B-GGUF", "file": "Qwen3-14B-Q4_K_M.gguf", "revision": "530227a7d994db8eca5ab5ced2fb692b614357fd",
     "sha256": "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0", "size": 9001752960},
    {"id": "qwen3-8b", "name": "Qwen3 8B", "license": "Apache-2.0", "rank": 5, "params_b": 8.2, "quant": "Q4_K_M", "layers": 36,
     "ctx_max": 32768, "approx_mb": 5000, "ram_mb": 6000, "notes": "a good balance on 16 GB; /no_think",
     "repo": "Qwen/Qwen3-8B-GGUF", "file": "Qwen3-8B-Q4_K_M.gguf", "revision": "7c41481f57cb95916b40956ab2f0b139b296d974",
     "sha256": "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785", "size": 5027783488},
    {"id": "granite-3.3-8b", "name": "IBM Granite 3.3 8B instruct", "license": "Apache-2.0", "rank": 6, "params_b": 8.2,
     "quant": "Q4_K_M", "layers": 40, "ctx_max": 131072, "approx_mb": 4900, "ram_mb": 5900, "notes": "enterprise-tuned, 128k context",
     "repo": "ibm-granite/granite-3.3-8b-instruct-GGUF", "file": "granite-3.3-8b-instruct-Q4_K_M.gguf",
     "revision": "e40e9dd739c7be00fa965c16ce167088190ce114",
     "sha256": "77bcee066a76dcdd10d0d123c87e32c8ec2c74e31b6ffd87ebee49c9ac215dca", "size": 4942873344},
    {"id": "qwen3-4b", "name": "Qwen3 4B", "license": "Apache-2.0", "rank": 7, "params_b": 4.0, "quant": "Q4_K_M", "layers": 36,
     "ctx_max": 32768, "approx_mb": 2500, "ram_mb": 3600, "notes": "the default: small and capable; /no_think",
     "repo": "Qwen/Qwen3-4B-GGUF", "file": "Qwen3-4B-Q4_K_M.gguf", "revision": "bc640142c66e1fdd12af0bd68f40445458f3869b",
     "sha256": "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5", "size": 2497280256},
    {"id": "phi-4-mini", "name": "Phi-4-mini instruct 3.8B", "license": "MIT", "rank": 8, "params_b": 3.8, "quant": "Q4_K_M", "layers": 32,
     "ctx_max": 131072, "approx_mb": 2500, "ram_mb": 3600, "notes": "good at reasoning for its size, 128k context",
     "repo": "bartowski/microsoft_Phi-4-mini-instruct-GGUF", "file": "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
     "revision": "7ff82c2aaa4dde30121698a973765f39be5288c0",
     "sha256": "01999f17c39cc3074afae5e9c539bc82d45f2dd7faa3917c66cbef76fce8c0c2", "size": 2491874688},
    {"id": "smollm3-3b", "name": "SmolLM3 3B", "license": "Apache-2.0", "rank": 9, "params_b": 3.1, "quant": "Q4_K_M", "layers": 36,
     "ctx_max": 65536, "approx_mb": 1900, "ram_mb": 2500, "notes": "3B with a thinking mode; /no_think",
     "repo": "unsloth/SmolLM3-3B-GGUF", "file": "SmolLM3-3B-Q4_K_M.gguf", "revision": "a7bc17204c8a326d6bd6e466e076959eddae2025",
     "sha256": "4de907d2d388a5508fb7cb443a06effe14cce3518b0a78d3bdd9e74d9edce989", "size": 1915306528},
    {"id": "granite-3.3-2b", "name": "IBM Granite 3.3 2B instruct", "license": "Apache-2.0", "rank": 10, "params_b": 2.5,
     "quant": "Q4_K_M", "layers": 40, "ctx_max": 131072, "approx_mb": 1550, "ram_mb": 2400, "notes": "small and quick, 128k context",
     "repo": "ibm-granite/granite-3.3-2b-instruct-GGUF", "file": "granite-3.3-2b-instruct-Q4_K_M.gguf",
     "revision": "7cdf86ccd1f1bb3491c9b7017b033f2e51367397",
     "sha256": "ac71e9e32c0bea919b409c5918f69ca74339854b0319c5065e4e9fb6d95c4852", "size": 1545303328},
    {"id": "qwen3-1.7b", "name": "Qwen3 1.7B", "license": "Apache-2.0", "rank": 11, "params_b": 1.7, "quant": "Q4_K_M", "layers": 28,
     "ctx_max": 32768, "approx_mb": 1100, "ram_mb": 2000, "notes": "for old or small machines; /no_think",
     "repo": "unsloth/Qwen3-1.7B-GGUF", "file": "Qwen3-1.7B-Q4_K_M.gguf", "revision": "d7f544eead698dbd1f15126ef60b45a1e1933222",
     "sha256": "b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897", "size": 1107409472},
    {"id": "qwen3-0.6b", "name": "Qwen3 0.6B", "license": "Apache-2.0", "rank": 12, "params_b": 0.6, "quant": "Q4_K_M", "layers": 28,
     "ctx_max": 32768, "approx_mb": 400, "ram_mb": 1200, "notes": "the smallest: simple summaries only; /no_think",
     "repo": "unsloth/Qwen3-0.6B-GGUF", "file": "Qwen3-0.6B-Q4_K_M.gguf", "revision": "50968a4468ef4233ed78cd7c3de230dd1d61a56b",
     "sha256": "ac2d97712095a558e31573f62f466a3f9d93990898b0ec79d7c974c1780d524a", "size": 396705472},
]
DEFAULT_MODEL = "qwen3-4b"  # without a reading of the machine: the best that fits an 8 GB one; MODELS is ordered best first (pick_default)

HEX40, HEX64 = re.compile(r"^[0-9a-f]{40}$"), re.compile(r"^[0-9a-f]{64}$")


class SetupError(Exception):
    """An expected failure: printed as one line, exit status 1."""


class Cancelled(Exception):
    """The caller asked download() to stop (the cancel of the web page and of the screen): the partial file stays, a later download resumes it."""


def model_url(m):
    """https://huggingface.co/<repo>/resolve/<commit>/<file>, or None while the model is not pinned."""
    if not m.get("revision"):
        return None
    return "https://huggingface.co/%s/resolve/%s/%s" % (m["repo"], m["revision"], urllib.parse.quote(m["file"]))


def missing_pins(entry, is_model):
    """Names of the values that are still not pinned (empty = ready to download)."""
    fields = ["revision", "sha256", "size"] if is_model else ["sha256", "size"]
    bad = [f for f in fields if entry.get(f) in (None, "")]
    if is_model and entry.get("revision") and not HEX40.match(entry["revision"]):
        bad.append("revision (not a 40-hex commit)")
    if entry.get("sha256") and not HEX64.match(entry["sha256"]):
        bad.append("sha256 (not 64 hex)")
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
    """<dir>/runtime/llamafile-<version>; on Windows with .exe (the system only runs executables with that suffix)."""
    runtime = runtime or RUNTIME
    name = posixpath.basename(urllib.parse.urlsplit(runtime["url"]).path)
    if _is_win(plat) and not name.lower().endswith(".exe"):
        name += ".exe"
    return _pm(plat).join(d, "runtime", name)


def model_path(d, model, plat=None):
    return _pm(plat).join(d, "models", model["file"])


def stamp_path(d):
    return os.path.join(d, "verified.json")


def ensure_dirs(d):
    """<dir>, <dir>/runtime, <dir>/models; the directories created here (and only those) get 0755 whatever the umask, so that the
    service account can walk down to the files. An existing directory (a home, say) is never touched."""
    for p in (d, os.path.join(d, "runtime"), os.path.join(d, "models")):
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
    """{"used": bytes taken by the downloaded files (runtime/ and models/, partial downloads too), "free": bytes free on the disk the folder is
    on (None when unknown)}: what the screens say about the folder. Never raises."""
    used = 0
    for sub in ("runtime", "models"):
        try:
            with os.scandir(os.path.join(d, sub)) as entries:
                for e in entries:
                    try:
                        if e.is_file(follow_symlinks=False):
                            used += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
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
    tmp = stamp_path(d) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, stamp_path(d))


def forget(d, path):
    data = load_stamp(d)
    if data.pop(os.path.basename(path), None) is not None:
        tmp = stamp_path(d) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, stamp_path(d))


def is_verified(d, path, entry, rehash=False):
    """True when `path` is the pinned file: by its stamp (size and mtime unchanged), or by hashing it again (rehash)."""
    if missing_pins(entry, "revision" in entry):
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size != entry["size"]:
        return False
    if rehash:
        return sha256_file(path) == entry["sha256"]
    s = load_stamp(d).get(os.path.basename(path))
    return bool(s) and s.get("sha256") == entry["sha256"] and s.get("size") == st.st_size and s.get("mtime_ns") == st.st_mtime_ns


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


def default_threads(cores=None):
    """Leave two cores free for the machine's own work."""
    return max(1, (cores if cores is not None else (os.cpu_count() or 1)) - 2)


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


def gpu_plan(model, hw, cfg_gpu="auto", ctx=DEFAULT_CTX):
    """-> (layers, why). layers > 0: that many layers on the GPU (serve passes --gpu auto -ngl N; a model that fits entirely gets
    ALL_LAYERS); 0: the CPU only (--gpu disable). The GPU is used when aihw says the model fits there (all of it, or in part: verdict
    gpu or partial) and [ai] gpu is not "no"."""
    if cfg_gpu == "no":
        return 0, "[ai] gpu = no: CPU only"
    a = assess_model(model, hw, ctx)
    if a is None:
        return 0, "%s: CPU only" % NO_ADVICE
    layers = int(a.get("gpu_layers") or 0)
    if a.get("verdict") in ("gpu", "partial") and layers > 0:
        every = model.get("layers")
        return (ALL_LAYERS if every and layers >= every else min(layers, ALL_LAYERS)), a.get("why") or ""
    return 0, a.get("why") or "CPU only"


def gpu_text(layers, model=None):
    """Where the server runs, in words: 'CPU only', 'all 36 layers on the GPU', '20 of 36 layers on the GPU'. `layers` is what -ngl gets:
    the model's own number or more (ALL_LAYERS, or aihw's one more for the output layer) means all of them."""
    n = (model or {}).get("layers")
    if not layers:
        return "CPU only"
    if n:
        return "all %d layers on the GPU" % n if layers >= n else "%d of %d layers on the GPU" % (layers, n)
    return "all layers on the GPU" if layers >= ALL_LAYERS else "%d layers on the GPU" % layers


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
    service account has no home to download into). Same files as the commands, same place: `sudo nuc-console-ai setup` and a button meet there."""
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
    """Everything a screen shows about the models: {"hw", "dir", "space": {"used", "free"}, "runtime": {"installed", "version"}, "recommended": id|None,
    "active": [ai] model|None, "models": [model + assess, installed, pinned, commands]} in rank order, best first.
    Never downloads, never hashes (the stamp file says what was verified), never raises: an unprivileged process can call it.
    `hw` (default aihw.cached()) can be given: the demo screens pass invented machines. models/runtime/cfg/plat are for tests.
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
    rp = runtime_path(d, runtime, plat)
    out = []
    for m in models:
        a = assess_model(m, hw)
        if a is None:
            a = {"verdict": "unknown", "where": "", "need_mb": m.get("ram_mb") or 0, "gpu_layers": 0, "tok_s": None, "why": NO_ADVICE}
        out.append(dict(m, assess=a, installed=is_verified(d, model_path(d, m, plat), m), pinned=not missing_pins(m, True),
                        commands=commands_for(m["id"], plat)))
    return {"hw": hw, "dir": d, "space": dir_space(d), "runtime": {"installed": is_verified(d, rp, runtime), "version": runtime["version"]},
            "recommended": recommend_id(models, hw), "active": (cfg.get("ai") or {}).get("model") or None, "models": out}


# ---------------------------------------------------------------------------------------------------------------------------
# The server command line
# ---------------------------------------------------------------------------------------------------------------------------
def serve_argv(d, model, port=DEFAULT_PORT, threads=None, ctx=DEFAULT_CTX, runtime=None, plat=None, gpu_layers=0):
    """argv list that starts the server. The host is fixed to 127.0.0.1. Unix: the runtime is an "actually portable executable"
    that the kernel cannot start by itself without binfmt_misc; started through sh it works everywhere (and avoids binfmt/WINE
    interference). Windows: the file is a real .exe. gpu_layers > 0: that many layers on the GPU (--gpu auto -ngl N; ALL_LAYERS = all
    of them); 0: the CPU only (--gpu disable), so that the server never starts compiling GPU code on a machine whose GPU cannot hold the model."""
    runtime = runtime or RUNTIME
    exe = runtime_path(d, runtime, plat)
    args = ["--server", "--host", LOOPBACK, "--port", str(int(port)), "-m", model_path(d, model, plat), "-a", model["id"],
            "-t", str(int(threads if threads is not None else default_threads())), "-c", str(int(ctx))]  # (--server opens no browser; 0.10 refuses --nobrowser)
    args += list(runtime.get("args", []))
    gpu_layers = int(gpu_layers or 0)
    if gpu_layers < 0:
        raise ValueError("gpu_layers must not be negative")
    args += ["--gpu", "auto", "-ngl", str(gpu_layers)] if gpu_layers else ["--gpu", "disable"]
    return [exe] + args if _is_win(plat) else ["/bin/sh", exe] + args


def lower_priority():
    """Unix: nice 10 (not more if the service manager already did it)."""
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < 10:
            os.setpriority(os.PRIO_PROCESS, 0, 10)
    except (AttributeError, OSError):
        pass


def run_server(argv):
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
        proc = subprocess.Popen(argv, creationflags=flags, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
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
    os.execv(argv[0], argv)


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
def show_choices(chosen, runtime, hw, recommended=False):
    print("Local model%s for the HEALTH advisor (open model, Q4_K_M, served on %s only)%s:" % (
        "s" if len(chosen) > 1 else "", LOOPBACK, ", recommended for this machine" if recommended else ""))
    print("   %-15s %-27s %8s %8s  %-8s %s" % ("ID", "NAME", "SIZE", "NEEDS", "FITS", "LICENCE"))
    for m in chosen:
        a = assess_model(m, hw) or {}
        need = "~" + fmt_mem(a.get("need_mb") or m["ram_mb"])
        print("   %-15s %-27s %8s %8s  %-8s %s" % (m["id"], m["name"], size_text(m), need, VERDICT_LABEL.get(a.get("verdict"), "?"), m["license"]))
    print("   runtime: %s %s (%s), %s" % (runtime["name"], runtime["version"], runtime["license"], fmt_size(runtime["size"])))


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


def cmd_setup(args, runtime=None, models=None, allow_loopback_http=False, ask=input, hw=None):
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    d = args.dir or default_dir()
    hw = hw if hw is not None else _hardware()
    total = ((hw or {}).get("ram") or {}).get("total_mb") or memory_mb()[0]
    asked = list(dict.fromkeys(list(args.models or []) + list(args.model_opt or [])))  # --model is the older spelling
    chosen = [find_model(i, models) for i in asked] or [pick_default(total, models, hw)]
    show_choices(chosen, runtime, hw, not asked)
    check_fit(chosen, hw, total, args.force)  # first: a model that cannot work here is not worth a word about pins
    todo = [("runtime", runtime, runtime_path(d, runtime), 0o755, False)] + [(m["id"], m, model_path(d, m), 0o644, True) for m in chosen]
    unpinned = [(n, missing_pins(e, is_m)) for n, e, _p, _m, is_m in todo if missing_pins(e, is_m)]
    if unpinned:
        print("\nnuc-console-ai: nothing downloaded: this build does not pin everything it needs.", file=sys.stderr)
        for n, bad in unpinned:
            print("  %s: not pinned: %s" % (n, ", ".join(bad)), file=sys.stderr)
        print("A maintainer fills them in with `python3 aisetup.py pins` (values come from the Hugging Face and GitHub APIs, "
              "never from memory). Downloads are only ever made from pinned, hash-checked values.", file=sys.stderr)
        return 1
    ok = {n: is_verified(d, p, e, rehash=True) for n, e, p, _m, _is_m in todo}  # hashed once: it takes seconds for a 2 GB file
    need = 0
    for n, e, p, _m, _is_m in todo:
        if not ok[n]:
            have = os.path.getsize(p + ".part") if os.path.exists(p + ".part") else 0
            need += max(0, e["size"] - have)
    if need and free_bytes(d) < need + DISK_MARGIN:
        raise SetupError("not enough free disk space in %s: %s needed, %s free (use --dir for another disk)"
                         % (d, fmt_size(need + DISK_MARGIN), fmt_size(free_bytes(d))))
    if sys.platform == "darwin" and os.uname().machine == "arm64":
        if subprocess.run(["/usr/bin/xcode-select", "-p"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            print("note: on Apple silicon the runtime needs the Command Line Tools to start for the first time: xcode-select --install")
    if need and not confirm("\nDownload %s into %s?" % (fmt_size(need), d), args.yes, ask):
        print("nuc-console-ai: nothing downloaded.", file=sys.stderr)
        return 1
    ensure_dirs(d)
    for n, e, p, mode, is_m in todo:
        url = model_url(e) if is_m else e["url"]
        if ok[n]:
            record(d, p, e["sha256"])
            print("%s: already there, SHA-256 verified: not downloaded again" % n)
            continue
        print("%s: downloading %s" % (n, fmt_size(e["size"])))
        how = download(url, p, e["sha256"], e["size"], mode=mode, allow_loopback_http=allow_loopback_http, progress=make_progress(n))
        record(d, p, e["sha256"])
        print("%s: %s, SHA-256 verified" % (n, how))
    if not args.no_config:
        offer_config(config_path(args.config), endpoint_for(args.port), chosen[0]["id"], args.yes, ask)
    print("\nready. Run it:   nuc-console-ai serve --port %d     (foreground; Ctrl+C stops)\n"
          "or as a service: sudo nuc-console-ai serve --install-service\nThen set [ai] enabled = yes. Check: nuc-console-ai status" % args.port)
    if len(chosen) > 1:
        print("The server runs one model at a time (%s here): switch with nuc-console-ai use ID, then restart it." % ", ".join(m["id"] for m in chosen))
    return 0


def restart_hint(plat=None):
    """How the running server is switched to the model in [ai] model: the service has every value explicit, so it is installed again."""
    admin = "in an administrator prompt: " if _is_win(plat) else "sudo "
    return ["service   : %snuc-console-ai serve --install-service  (writes it again for this model, restarts it)" % admin,
            "foreground: stop it (Ctrl+C) and run: nuc-console-ai serve"]


def cmd_use(args, runtime=None, models=None, hw=None):
    """Make an installed, verified model the one the advisor asks for: [ai] model in config.ini (nothing else is changed)."""
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    d = args.dir or default_dir()
    m = find_model(args.model, models)
    if m not in installed_models(d, models, runtime):
        raise SetupError("%s is not installed (or not verified) in %s: run nuc-console-ai setup %s" % (m["id"], d, m["id"]))
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
        print("note: [ai] endpoint is still the Ollama default; for the server of this tool set endpoint = %s" % endpoint_for(DEFAULT_PORT))
    if not ai["enabled"]:
        print("note: [ai] enabled = no: the advisor does not use it yet")
    print("The server runs one model at a time. To switch the running one to %s:" % m["id"])
    for ln in restart_hint():
        print("  " + ln)
    return 0


# ---------------------------------------------------------------------------------------------------------------------------
# serve and services
# ---------------------------------------------------------------------------------------------------------------------------
def installed_models(d, models=None, runtime=None):
    """Models of the list that are on disk and verified (by stamp) while the runtime is too."""
    runtime = runtime if runtime is not None else RUNTIME
    if not is_verified(d, runtime_path(d, runtime), runtime):
        return []
    return [m for m in (models if models is not None else MODELS) if is_verified(d, model_path(d, m), m)]


def choose_model(args, d, models=None, runtime=None, cfg_model=None):
    models = models if models is not None else MODELS
    if args.model:
        m = find_model(args.model, models)
        if m not in installed_models(d, models, runtime):
            raise SetupError("%s is not installed (or not verified) in %s: run nuc-console-ai setup %s" % (m["id"], d, m["id"]))
        return m
    have = installed_models(d, models, runtime)
    if not have:
        raise SetupError("no verified runtime and model in %s: run nuc-console-ai setup first" % d)
    return next((m for m in have if m["id"] == cfg_model), None) or next((m for m in have if m["id"] == DEFAULT_MODEL), have[0])


def systemd_quote(arg):
    """One argument of ExecStart= (systemd splits on spaces, expands %specifiers and $VARIABLES)."""
    if re.fullmatch(r"[A-Za-z0-9_./:=+@,-]+", arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def systemd_unit(argv, ram_mb=None, gpu=False, groups=()):
    """gpu: the server uses the GPU, so the device nodes (/dev/nvidia*, /dev/dri, /dev/kfd) must exist for it and `groups` (render,
    video: the ones the machine has) let the service user open them; everything else of the sandbox stays."""
    state = "/var/lib/" + SERVICE_NAME  # writable by the service user only (StateDirectory): the runtime unpacks its loader here
    lines = [
        "[Unit]", "Description=nuc-console local AI model server (llamafile, %s only)" % LOOPBACK, "After=network.target", "",
        "[Service]", "Type=simple", "User=" + SERVICE_NAME,
        "ExecStart=" + " ".join(systemd_quote(a) for a in argv),
        "Environment=HOME=%s TMPDIR=%s" % (state, state), "StateDirectory=" + SERVICE_NAME,
        "Nice=10", "Restart=on-failure", "RestartSec=10",
        "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=" + ("no" if gpu else "yes"),
        "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes", "ProtectClock=yes",
        "ProtectHostname=yes", "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "RestrictNamespaces=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes", "TasksMax=128",
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
        "  <RegistrationInfo><Description>nuc-console local AI model server (llamafile, 127.0.0.1 only)</Description></RegistrationInfo>\n"
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


def service_argv(d, model, port, threads, ctx, plat=None, python=None, script=None, gpu_layers=0):
    """The service runs `aisetup.py serve ...` with every value explicit (the service user reads no config of ours): the GPU layers
    too, decided when the service is installed (0 = CPU only). A new model or [ai] gpu = a new `serve --install-service`."""
    python = python or sys.executable
    if _is_win(plat) and python.lower().endswith("python.exe") and os.path.exists(python[:-10] + "pythonw.exe"):
        python = python[:-10] + "pythonw.exe"  # no console window
    argv = [python, "-B", script or os.path.realpath(__file__), "serve", "--dir", d, "--model", model["id"], "--port", str(port),
            "--threads", str(threads), "--ctx", str(ctx), "--gpu-layers", str(int(gpu_layers or 0))]
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


def install_service(d, model, port, threads, ctx, plat=None, gpu_layers=0):
    if not is_root():
        raise SetupError("--install-service needs %s" % ("an administrator prompt" if _is_win(plat) else "root: sudo nuc-console-ai serve --install-service"))
    if not _is_win(plat) and not (_readable_by_all(runtime_path(d)) and _readable_by_all(model_path(d, model))):
        raise SetupError("%s is not readable by other users (the service runs as its own account): run setup as root, or use --dir "
                         "on a shared path" % d)
    argv = service_argv(d, model, port, threads, ctx, plat, gpu_layers=gpu_layers)
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
        _write_root_file(unit, systemd_unit(argv, model["ram_mb"], gpu=bool(gpu_layers), groups=_gpu_groups() if gpu_layers else ()))
        systemctl = tool("systemctl")
        run([systemctl, "daemon-reload"])
        run([systemctl, "enable", SERVICE_NAME + ".service"])
        run([systemctl, "restart", SERVICE_NAME + ".service"])
        print("service: %s.service (user %s, sandboxed, 127.0.0.1 only) started. Logs: journalctl -u %s" % (SERVICE_NAME, SERVICE_NAME, SERVICE_NAME))
    print("model %s on %s, %s (the first answer may take a minute: the model is loaded into memory)"
          % (model["id"], endpoint_for(port), gpu_text(gpu_layers, model)))


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
    threads = args.threads or default_threads()
    if args.gpu_layers is not None:  # explicit (the service passes it: it was decided when the service was installed)
        layers, why = args.gpu_layers, "GPU layers as given by --gpu-layers"
    else:
        layers, why = gpu_plan(model, hw if hw is not None else (_hardware() if ai["gpu"] != "no" else {}), ai["gpu"], args.ctx)
    argv = serve_argv(d, model, args.port, threads, args.ctx, runtime, gpu_layers=layers)
    if args.install_service:
        install_service(d, model, args.port, threads, args.ctx, gpu_layers=layers)
        return 0
    if args.dry_run:
        print(subprocess.list2cmdline(argv) if _is_win() else " ".join(shlex.quote(a) for a in argv))
        return 0
    if args.log:
        nuc_config.log_to(args.log)
    print("nuc-console-ai: %s on %s (%d threads, context %d, %s, low priority). Ctrl+C stops."
          % (model["id"], endpoint_for(args.port), threads, args.ctx, gpu_text(layers, model)),
          flush=True)
    if why:
        print("nuc-console-ai: %s" % safe(why, 200), flush=True)
    return run_server(argv)


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
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return False, [], "the answer has no model list (is this an OpenAI-compatible /v1?)"
        return True, [safe(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")], ""
    except ValueError:
        return False, [], "the answer is not JSON"


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
    rp = runtime_path(d, runtime)

    def state(path, entry, is_model):
        if missing_pins(entry, is_model):
            return "not pinned in this build (setup refuses to download it)"
        if not os.path.exists(path):
            return "not installed"
        return "installed, SHA-256 verified" if is_verified(d, path, entry, rehash=args.verify) else "present but NOT verified (run setup again)"
    print("  runtime   : %s %s: %s" % (runtime["name"], runtime["version"], state(rp, runtime, False)))
    absent = 0
    for m in models:
        st = state(model_path(d, m), m, True)
        if st.startswith(("not installed", "not pinned")):  # the catalog is long: only what is on disk is listed here
            absent += 1
            continue
        print("  model     : %-15s %-28s %s%s" % (m["id"], m["name"], st, "  (active)" if m["id"] == ai["model"] else ""))
    if absent:
        print("  models    : %d more in the catalog, not installed (nuc-console-ai models)" % absent)
    ready = installed_models(d, models, runtime) if not args.verify else [
        m for m in models if is_verified(d, rp, runtime, True) and is_verified(d, model_path(d, m), m, True)]
    print("  config    : %s: [ai] enabled = %s, endpoint = %s, model = %s, allow_remote = %s"
          % (cfg_file, "yes" if ai["enabled"] else "no", safe(ai["endpoint"]), safe(ai["model"]) or "(empty)", "yes" if ai["allow_remote"] else "no"))
    endpoint = args.endpoint or ai["endpoint"]
    if not is_loopback_endpoint(endpoint) and not ai["allow_remote"] and not args.endpoint:
        print("  server    : %s is not on this machine and allow_remote = no: the advisor refuses it" % safe(endpoint))
        return 3 if ready else 1
    ok, ids, why = probe(endpoint)
    if not ok:
        print("  server    : %s: not answering (%s)" % (safe(endpoint), why))
        print("  next      : %s" % ("nuc-console-ai serve  (or sudo nuc-console-ai serve --install-service)" if ready else "nuc-console-ai setup"))
        return 3 if ready else 1
    print("  server    : %s: answering, /v1/models lists: %s" % (safe(endpoint), ", ".join(ids) or "(nothing)"))
    if ai["model"] and ai["model"] not in ids:
        print("  warning   : [ai] model = %s is not in that list" % safe(ai["model"]))
        return 3
    if not ai["enabled"]:
        print("  note      : [ai] enabled = no: the advisor does not use it yet")
    return 0


# ---------------------------------------------------------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------------------------------------------------------
def cmd_remove(args, runtime=None, models=None, ask=input):
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    d = args.dir or default_dir()
    which = args.model or args.model_opt  # MODEL, or the older --model MODEL
    targets = [model_path(d, find_model(which, models))] if which else \
        [model_path(d, m) for m in models] + [runtime_path(d, runtime)]
    files = [p for t in targets for p in (t, t + ".part") if os.path.isfile(p)]
    if not files:
        print("nothing to remove in %s" % d)
        return 0
    size = sum(os.path.getsize(p) for p in files)
    print("will delete from %s:" % d)
    for p in files:
        print("  %s  (%s)" % (os.path.relpath(p, d), fmt_size(os.path.getsize(p))))
    if not confirm("Delete %d file(s), %s?" % (len(files), fmt_size(size)), args.yes, ask):
        print("nothing deleted.")
        return 1
    bad = 0
    for p in files:
        try:
            os.unlink(p)
            forget(d, p)
        except OSError as e:
            bad += 1
            print("cannot delete %s: %s (is the server still running?)" % (p, e.strerror or e), file=sys.stderr)
    for sub in ("models", "runtime"):
        try:
            os.rmdir(os.path.join(d, sub))  # only when empty
        except OSError:
            pass
    print("deleted. [ai] in config.ini is not changed: set enabled = no there if you do not use another server." if not bad else "some files remain.")
    if which and not bad and nuc_config.load(config_path(args.config))["ai"]["model"] == which:
        print("note: %s is the active model ([ai] model): choose another with nuc-console-ai use ID, and restart the server" % which)
    return 1 if bad else 0


# ---------------------------------------------------------------------------------------------------------------------------
# pins (maintainers): read the values to pin from the Hugging Face and GitHub APIs
# ---------------------------------------------------------------------------------------------------------------------------
def fetch_json(url, timeout=30):
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.build_opener(_HttpsRedirects()).open(req, timeout=timeout) as r:
        return json.loads(r.read(8 << 20).decode("utf-8"))


def pin_from_hub(info, tree, path):
    """Hub model JSON + tree JSON (at that commit) -> {"revision", "sha256", "size", "license"} of file `path`."""
    rev = info.get("sha")
    if not isinstance(rev, str) or not HEX40.match(rev):
        raise SetupError("the Hub gave no commit id")
    for e in tree if isinstance(tree, list) else []:
        if isinstance(e, dict) and e.get("path") == path:
            lfs = e.get("lfs") or {}
            if not HEX64.match(str(lfs.get("oid", ""))) or not isinstance(lfs.get("size"), int):
                raise SetupError("%s is not an LFS file with a SHA-256 on the Hub" % path)
            lic = (info.get("cardData") or {}).get("license") or next((t[8:] for t in info.get("tags", []) if t.startswith("license:")), "?")
            return {"revision": rev, "sha256": lfs["oid"], "size": lfs["size"], "license": lic}
    near = sorted(e["path"] for e in (tree if isinstance(tree, list) else [])
                  if isinstance(e, dict) and str(e.get("path", "")).lower().endswith(".gguf") and "q4_k_m" in str(e.get("path", "")).lower())
    raise SetupError("no file %s in that repository" % path + ("; Q4_K_M files there: %s" % ", ".join(near[:5]) if near else ""))


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


def cmd_pins(args, runtime=None, models=None):
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    bad = 0
    print("# paste into RUNTIME / MODELS of aisetup.py, check the licence, run the tests, try `setup` and `serve` for real\n")
    try:  # the newest runtime knows the newest model families: say which one it is
        print("RUNTIME latest release: %s" % fetch_json("https://api.github.com/repos/mozilla-ai/llamafile/releases/latest").get("tag_name"))
    except (OSError, ValueError, http.client.HTTPException) as e:
        print("RUNTIME latest release: ERROR %s" % safe(e, 200))
    try:
        rel = fetch_json("https://api.github.com/repos/mozilla-ai/llamafile/releases/tags/" + runtime["version"])
        print("RUNTIME %s: %s" % (runtime["version"], json.dumps(pin_from_release(rel, runtime["url"]))))
    except (SetupError, OSError, ValueError, KeyError, http.client.HTTPException) as e:
        bad += 1
        print("RUNTIME %s: ERROR %s" % (runtime["version"], safe(e, 200)))
    for m in models:
        try:
            base = "https://huggingface.co/api/models/" + m["repo"]
            info = fetch_json(base)
            if not isinstance(info.get("sha"), str):
                raise SetupError("the Hub gave no commit id")
            tree = fetch_json("%s/tree/%s" % (base, info.get("sha")))
            print("%s (%s): %s" % (m["id"], m["repo"], json.dumps(pin_from_hub(info, tree, m["file"]))))
        except (SetupError, OSError, ValueError, KeyError, http.client.HTTPException) as e:
            bad += 1
            print("%s (%s): ERROR %s" % (m["id"], m["repo"], safe(e, 200)))
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
    p = sub.add_parser("setup", help="download the runtime and one or more models once, verify them, offer to write config.ini")
    common(p)
    p.add_argument("models", nargs="*", metavar="MODEL", help="model id(s) (default: the one recommended for this machine; see `models`)")
    p.add_argument("--model", action="append", dest="model_opt", metavar="MODEL", help=argparse.SUPPRESS)  # the older spelling of MODEL
    p.add_argument("--force", action="store_true", help="install a model that will not work on this machine anyway")
    p.add_argument("--port", type=_port, default=DEFAULT_PORT, help="port the server will use, for [ai] endpoint (default %(default)s)")
    p.add_argument("--yes", "-y", action="store_true", help="agree to the download and to writing a first [ai] config")
    p.add_argument("--no-config", action="store_true", help="do not offer to write config.ini")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("use", help="make an installed model the one the advisor asks for ([ai] model), and say how to restart the server")
    common(p)
    p.add_argument("model", metavar="MODEL", help="model id: one of those `models` lists as installed")
    p.add_argument("--force", action="store_true", help="use a model that will not work on this machine anyway")
    p.set_defaults(func=cmd_use)
    p = sub.add_parser("serve", help="run the server in the foreground on 127.0.0.1, at low priority, on the GPU when the model fits there")
    common(p)
    p.add_argument("--model", help="model id (default: [ai] model if installed, else the default one)")
    p.add_argument("--port", type=_port, default=DEFAULT_PORT, help="default %(default)s")
    p.add_argument("--threads", type=_ranged(1, 512), help="default: cores minus two (at least 1)")
    p.add_argument("--ctx", type=_ranged(512, 131072), default=DEFAULT_CTX, help="context tokens, default %(default)s")
    p.add_argument("--gpu-layers", type=_ranged(0, ALL_LAYERS), metavar="N", help="layers on the GPU: 0 = CPU only, the model's own number "
                   "or more (%d) = all of them (default: decided from this machine's hardware, unless [ai] gpu = no)" % ALL_LAYERS)
    p.add_argument("--dry-run", action="store_true", help="print the command, do not start it")
    p.add_argument("--install-service", action="store_true", help="install and start it as a system service (root / administrator)")
    p.add_argument("--remove-service", action="store_true", help="stop and remove that service")
    p.add_argument("--log", help="Windows service: send output to this file")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("status", help="what is installed and verified, whether the configured endpoint answers")
    common(p, reads=True)
    p.add_argument("--endpoint", help="probe this /v1 URL instead of [ai] endpoint")
    p.add_argument("--verify", action="store_true", help="hash the files again instead of trusting the stamp")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("remove", help="delete the downloaded files of one model (or of everything)")
    common(p)
    p.add_argument("model", nargs="?", metavar="MODEL", help="only this model (default: every model and the runtime)")
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
