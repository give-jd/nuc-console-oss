"""nuc-console-ai: install a small local model and its server, once (setup), run it on 127.0.0.1 (serve), report (status).

  setup   download the runtime (llamafile, one executable for Linux, macOS and Windows) and one small open model (GGUF) into a
          cache directory, verify both against the SHA-256 pinned below, never download a file that is already there with the
          right hash; with your consent write [ai] endpoint/model in config.ini
  serve   run the server in the foreground, on 127.0.0.1 only, at low priority (--install-service / --remove-service: a system
          service: systemd unit, launchd daemon or Windows scheduled task, each run by an unprivileged account)
  status  what is installed and verified, whether the configured endpoint answers
  remove  delete the downloaded files
  pins    for maintainers: prints the values to paste in RUNTIME and MODELS (needs the network)

Security: HTTPS only (a redirect to http:// is refused), every file is checked against a SHA-256 written in this file, downloads
go to "<name>.part" and are renamed only after the check (a mismatch deletes them), commands are argument lists (no shell), the
server binds to 127.0.0.1 and nothing here can change that, no auto-update: a new runtime or model means a new pin in a new release.
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
# STATUS: UNPINNED. The version and the asset name below are the maintainer's best knowledge and must be confirmed with `pins`;
# "args" (CPU only: the server shares a monitoring machine and must never start compiling GPU code) must be confirmed against
# `llamafile --help` of the pinned version.
RUNTIME = {
    "name": "llamafile", "version": "0.9.3", "license": "Apache-2.0",
    "url": "https://github.com/Mozilla-Ocho/llamafile/releases/download/0.9.3/llamafile-0.9.3",
    "sha256": None, "size": None,
    "args": ["--gpu", "disable"],
}

# Small instruct models, GGUF Q4_K_M, permissive licences. "revision" is a Hugging Face COMMIT (never "main"), "sha256" and "size"
# are those of the file at that commit (the Hub's tree API: lfs.oid, lfs.size). ram_mb is the approximate total memory of the
# server with DEFAULT_CTX tokens of context. repo/file: best knowledge, UNVERIFIED until `pins` finds them; revision, sha256, size:
# NOT PINNED. Qwen3 starts in "thinking" mode (long <think> blocks): the advisor should ask for /no_think or similar.
MODELS = [
    {"id": "qwen3-4b", "name": "Qwen3 4B", "license": "Apache-2.0", "ram_mb": 3600,
     "repo": "Qwen/Qwen3-4B-GGUF", "file": "Qwen3-4B-Q4_K_M.gguf", "revision": None, "sha256": None, "size": None},
    {"id": "phi-4-mini", "name": "Phi-4-mini instruct 3.8B", "license": "MIT", "ram_mb": 3600,
     "repo": "bartowski/microsoft_Phi-4-mini-instruct-GGUF", "file": "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
     "revision": None, "sha256": None, "size": None},
    {"id": "granite-3.3-2b", "name": "IBM Granite 3.3 2B instruct", "license": "Apache-2.0", "ram_mb": 2400,
     "repo": "ibm-granite/granite-3.3-2b-instruct-GGUF", "file": "granite-3.3-2b-instruct-Q4_K_M.gguf",
     "revision": None, "sha256": None, "size": None},
    {"id": "qwen3-1.7b", "name": "Qwen3 1.7B", "license": "Apache-2.0", "ram_mb": 2000,
     "repo": "unsloth/Qwen3-1.7B-GGUF", "file": "Qwen3-1.7B-Q4_K_M.gguf", "revision": None, "sha256": None, "size": None},
]
DEFAULT_MODEL = "qwen3-4b"  # the best of the list that fits in about 4 GB; MODELS is ordered best first (pick_default)

HEX40, HEX64 = re.compile(r"^[0-9a-f]{40}$"), re.compile(r"^[0-9a-f]{64}$")


class SetupError(Exception):
    """An expected failure: printed as one line, exit status 1."""


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


def pick_default(total_mb, models=None):
    """The default model: the best one (the list is ordered) that fits the machine's RAM with some room; the smallest otherwise."""
    models = models if models is not None else MODELS
    if not total_mb:
        return next((m for m in models if m["id"] == DEFAULT_MODEL), models[0])
    for m in models:
        if m["ram_mb"] <= total_mb * 0.85:
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
        for q in missing:
            try:
                os.chmod(q, 0o755)
            except OSError:
                pass


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


def download(url, dest, sha256, size, *, mode=0o644, allow_loopback_http=False, progress=None, retries=4, timeout=30, backoff=2.0):
    """Download `url` to `dest` (resuming "<dest>.part"), check size and SHA-256, then rename. -> "cached" | "downloaded".
    A file already at `dest` with the right hash is never downloaded again. A wrong hash deletes the download and raises."""
    check_url(url, allow_loopback_http)
    if not (sha256 and HEX64.match(sha256) and isinstance(size, int) and size > 0):
        raise SetupError("%s: SHA-256 and size are not pinned: refusing to download" % os.path.basename(dest))
    if os.path.isfile(dest) and os.path.getsize(dest) == size and sha256_file(dest) == sha256:
        return "cached"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part, opener, last = dest + ".part", _opener(allow_loopback_http), None
    for attempt in range(retries + 1):
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
            time.sleep(backoff * (attempt + 1))
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
# The server command line
# ---------------------------------------------------------------------------------------------------------------------------
def serve_argv(d, model, port=DEFAULT_PORT, threads=None, ctx=DEFAULT_CTX, runtime=None, plat=None):
    """argv list that starts the server. The host is fixed to 127.0.0.1. Unix: the runtime is an "actually portable executable"
    that the kernel cannot start by itself without binfmt_misc; started through sh it works everywhere (and avoids binfmt/WINE
    interference). Windows: the file is a real .exe."""
    runtime = runtime or RUNTIME
    exe = runtime_path(d, runtime, plat)
    args = ["--server", "--host", LOOPBACK, "--port", str(int(port)), "-m", model_path(d, model, plat), "-a", model["id"],
            "-t", str(int(threads if threads is not None else default_threads())), "-c", str(int(ctx)), "--nobrowser"]
    args += list(runtime.get("args", []))
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
# setup
# ---------------------------------------------------------------------------------------------------------------------------
def show_choices(models, default_id, runtime):
    print("Local model for the HEALTH advisor (open model, Q4_K_M, runs on the CPU, served on %s only):" % LOOPBACK)
    print("   %-15s %-28s %8s %9s  %s" % ("ID", "NAME", "SIZE", "RAM", "LICENCE"))
    for m in models:
        print(" %s %-15s %-28s %8s %9s  %s" % ("*" if m["id"] == default_id else " ", m["id"], m["name"], fmt_size(m["size"]),
                                               "~%.1f GB" % (m["ram_mb"] / 1000.0), m["license"]))
    print("   runtime: %s %s (%s), %s; * = default for this machine" % (runtime["name"], runtime["version"], runtime["license"],
                                                                      fmt_size(runtime["size"])))


def cmd_setup(args, runtime=None, models=None, allow_loopback_http=False, ask=input):
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    d = args.dir or default_dir()
    total, avail = memory_mb()
    model = find_model(args.model, models) if args.model else pick_default(total, models)
    show_choices(models, model["id"], runtime)
    todo = [("runtime", runtime, runtime_path(d, runtime), 0o755, False), (model["id"], model, model_path(d, model), 0o644, True)]
    unpinned = [(n, missing_pins(e, is_m)) for n, e, _p, _m, is_m in todo if missing_pins(e, is_m)]
    if unpinned:
        print("\nnuc-console-ai: nothing downloaded: this build does not pin everything it needs.", file=sys.stderr)
        for n, bad in unpinned:
            print("  %s: not pinned: %s" % (n, ", ".join(bad)), file=sys.stderr)
        print("A maintainer fills them in with `python3 aisetup.py pins` (values come from the Hugging Face and GitHub APIs, "
              "never from memory). Downloads are only ever made from pinned, hash-checked values.", file=sys.stderr)
        return 1
    if total and total < model["ram_mb"]:
        print("\nwarning: this machine has about %.1f GB of RAM and %s needs about %.1f GB: it will swap or fail. "
              "Choose a smaller one with --model." % (total / 1000.0, model["id"], model["ram_mb"] / 1000.0))
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
        offer_config(config_path(args.config), endpoint_for(args.port), model["id"], args.yes, ask)
    print("\nready. Run it:   nuc-console-ai serve --port %d     (foreground; Ctrl+C stops)\n"
          "or as a service: sudo nuc-console-ai serve --install-service\nThen set [ai] enabled = yes. Check: nuc-console-ai status" % args.port)
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
            raise SetupError("%s is not installed (or not verified) in %s: run nuc-console-ai setup --model %s" % (m["id"], d, m["id"]))
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


def systemd_unit(argv, ram_mb=None):
    state = "/var/lib/" + SERVICE_NAME  # writable by the service user only (StateDirectory): the runtime unpacks its loader here
    lines = [
        "[Unit]", "Description=nuc-console local AI model server (llamafile, %s only)" % LOOPBACK, "After=network.target", "",
        "[Service]", "Type=simple", "User=" + SERVICE_NAME,
        "ExecStart=" + " ".join(systemd_quote(a) for a in argv),
        "Environment=HOME=%s TMPDIR=%s" % (state, state), "StateDirectory=" + SERVICE_NAME,
        "Nice=10", "Restart=on-failure", "RestartSec=10",
        "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
        "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes", "ProtectClock=yes",
        "ProtectHostname=yes", "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "RestrictNamespaces=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes", "TasksMax=128",
    ]
    if ram_mb:
        lines.append("MemoryMax=%dM" % int(ram_mb * 1.5))
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


def service_argv(d, model, port, threads, ctx, plat=None, python=None, script=None):
    """The service runs `aisetup.py serve ...` with every value explicit (the service user reads no config of ours)."""
    python = python or sys.executable
    if _is_win(plat) and python.lower().endswith("python.exe") and os.path.exists(python[:-10] + "pythonw.exe"):
        python = python[:-10] + "pythonw.exe"  # no console window
    argv = [python, "-B", script or os.path.realpath(__file__), "serve", "--dir", d, "--model", model["id"], "--port", str(port),
            "--threads", str(threads), "--ctx", str(ctx)]
    if _is_win(plat):
        argv += ["--log", ntpath.join(os.environ.get("ProgramData") or r"C:\ProgramData", "nuc-console", "logs", "ai.log")]
    return argv


def _readable_by_all(path):
    """Every directory above `path` can be entered, and the file read, by any user (the service user is not the one who ran setup)."""
    p = os.path.abspath(path)
    while True:
        try:
            mode = os.stat(p).st_mode
        except OSError:
            return False
        need = 0o005 if os.path.isdir(p) else 0o004
        if mode & need != need:
            return False
        parent = os.path.dirname(p)
        if parent == p:
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


def install_service(d, model, port, threads, ctx, plat=None):
    if not is_root():
        raise SetupError("--install-service needs %s" % ("an administrator prompt" if _is_win(plat) else "root: sudo nuc-console-ai serve --install-service"))
    if not _is_win(plat) and not (_readable_by_all(runtime_path(d)) and _readable_by_all(model_path(d, model))):
        raise SetupError("%s is not readable by other users (the service runs as its own account): run setup as root, or use --dir "
                         "on a shared path" % d)
    argv = service_argv(d, model, port, threads, ctx, plat)
    if _is_win(plat):
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
        _write_root_file(unit, systemd_unit(argv, model["ram_mb"]))
        systemctl = tool("systemctl")
        run([systemctl, "daemon-reload"])
        run([systemctl, "enable", SERVICE_NAME + ".service"])
        run([systemctl, "restart", SERVICE_NAME + ".service"])
        print("service: %s.service (user %s, sandboxed, 127.0.0.1 only) started. Logs: journalctl -u %s" % (SERVICE_NAME, SERVICE_NAME, SERVICE_NAME))
    print("model %s on %s (the first answer may take a minute: the model is loaded into memory)" % (model["id"], endpoint_for(port)))


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


def cmd_serve(args, runtime=None, models=None):
    runtime = runtime if runtime is not None else RUNTIME
    if args.remove_service:
        remove_service()
        return 0
    d = args.dir or default_dir()
    cfg_model = nuc_config.load(config_path(args.config))["ai"]["model"]
    model = choose_model(args, d, models, runtime, cfg_model)
    threads = args.threads or default_threads()
    argv = serve_argv(d, model, args.port, threads, args.ctx, runtime)
    if args.install_service:
        install_service(d, model, args.port, threads, args.ctx)
        return 0
    if args.dry_run:
        print(subprocess.list2cmdline(argv) if _is_win() else " ".join(shlex.quote(a) for a in argv))
        return 0
    if args.log:
        nuc_config.log_to(args.log)
    print("nuc-console-ai: %s on %s (%d threads, context %d, low priority). Ctrl+C stops." % (model["id"], endpoint_for(args.port), threads, args.ctx),
          flush=True)
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
    d = args.dir or default_dir()
    cfg_file = config_path(args.config)
    ai = nuc_config.load(cfg_file)["ai"]
    print("nuc-console-ai status")
    print("  directory : %s" % d)
    rp = runtime_path(d, runtime)

    def state(path, entry, is_model):
        if missing_pins(entry, is_model):
            return "not pinned in this build (setup refuses to download it)"
        if not os.path.exists(path):
            return "not installed"
        return "installed, SHA-256 verified" if is_verified(d, path, entry, rehash=args.verify) else "present but NOT verified (run setup again)"
    print("  runtime   : %s %s: %s" % (runtime["name"], runtime["version"], state(rp, runtime, False)))
    for m in models:
        print("  model     : %-15s %-28s %s" % (m["id"], m["name"], state(model_path(d, m), m, True)))
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
    targets = [model_path(d, find_model(args.model, models))] if args.model else \
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
    raise SetupError("no file %s in that repository" % path)


def pin_from_release(release, url):
    """GitHub release JSON -> {"sha256", "size"} of the asset with this download URL (needs the release to publish digests)."""
    for a in release.get("assets", []) if isinstance(release, dict) else []:
        if a.get("browser_download_url") == url:
            digest = str(a.get("digest") or "")
            if not digest.startswith("sha256:") or not HEX64.match(digest[7:]):
                raise SetupError("the release publishes no SHA-256 for this asset: download it over a trusted link and run sha256sum")
            return {"sha256": digest[7:], "size": a["size"]}
    raise SetupError("no asset %s in release %s" % (url, release.get("tag_name") if isinstance(release, dict) else "?"))


def cmd_pins(args, runtime=None, models=None):
    runtime = runtime if runtime is not None else RUNTIME
    models = models if models is not None else MODELS
    bad = 0
    print("# paste into RUNTIME / MODELS of aisetup.py, check the licence, run the tests, try `setup` and `serve` for real\n")
    try:
        rel = fetch_json("https://api.github.com/repos/Mozilla-Ocho/llamafile/releases/tags/" + runtime["version"])
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
    ap = argparse.ArgumentParser(prog="nuc-console-ai", description="A small local AI model for the HEALTH advisor: install once, serve on 127.0.0.1.",
                                 epilog="status exit codes: 0 the endpoint answers; 3 installed but not answering (start it); 1 not installed.")
    sub = ap.add_subparsers(dest="cmd", metavar="{setup,serve,status,remove}")
    sub.required = True

    def common(p, config=True):
        p.add_argument("--dir", help="cache directory (default: %s)" % default_dir())
        if config:
            p.add_argument("--config", help="config.ini to read/write (default: %s)" % config_path())
    p = sub.add_parser("setup", help="download the runtime and a model once, verify them, offer to write config.ini")
    common(p)
    p.add_argument("--model", help="model id (default: the best that fits this machine: %s)" % ", ".join(m["id"] for m in MODELS))
    p.add_argument("--port", type=_port, default=DEFAULT_PORT, help="port the server will use, for [ai] endpoint (default %(default)s)")
    p.add_argument("--yes", "-y", action="store_true", help="agree to the download and to writing a first [ai] config")
    p.add_argument("--no-config", action="store_true", help="do not offer to write config.ini")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("serve", help="run the server in the foreground on 127.0.0.1, at low priority")
    common(p)
    p.add_argument("--model", help="model id (default: [ai] model if installed, else the default one)")
    p.add_argument("--port", type=_port, default=DEFAULT_PORT, help="default %(default)s")
    p.add_argument("--threads", type=_ranged(1, 512), help="default: cores minus two (at least 1)")
    p.add_argument("--ctx", type=_ranged(512, 131072), default=DEFAULT_CTX, help="context tokens, default %(default)s")
    p.add_argument("--dry-run", action="store_true", help="print the command, do not start it")
    p.add_argument("--install-service", action="store_true", help="install and start it as a system service (root / administrator)")
    p.add_argument("--remove-service", action="store_true", help="stop and remove that service")
    p.add_argument("--log", help="Windows service: send output to this file")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("status", help="what is installed and verified, whether the configured endpoint answers")
    common(p)
    p.add_argument("--endpoint", help="probe this /v1 URL instead of [ai] endpoint")
    p.add_argument("--verify", action="store_true", help="hash the files again instead of trusting the stamp")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("remove", help="delete the downloaded files")
    common(p, config=False)
    p.add_argument("--model", help="only this model (default: every model and the runtime)")
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
