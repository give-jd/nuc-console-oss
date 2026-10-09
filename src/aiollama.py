"""The model server of the AI page, the AI screen and nuc-console-ai: Ollama (MIT, https://github.com/ollama/ollama).

nuc-console runs a copy of its own: the build of this system and processor that aisetup.RUNTIME pins (version, file, SHA-256, size), downloaded
into <AI folder>/runtime, unpacked there and started as `ollama serve` on 127.0.0.1 with its models in <AI folder>/models. Nothing is installed
on the system and no administrator is needed. This module knows:

  the build    asset_key(), runtime_asset(): which file of the release this machine needs; exe_path(): where its executable is once unpacked
  unpacking    unpack(): .zip (Windows), .tgz (macOS), .tar.zst (Linux: Python 3.14's compression.zstd, else the zstd tool), every member
               checked (no absolute path, no "..", links that stay inside, nothing but files, folders and links), into "<dest>.part",
               renamed at the end
  the server   server_argv(), server_env(): 127.0.0.1 only, the models in the AI folder, no cloud models, one model at a time, nothing pruned
               at start, the GPUs hidden when they must not be used
  the API      Api: version, pull (with progress and cancel), copy, delete, load and unload: the calls nuc-console makes, never through a proxy
  the models   a model is installed when its manifest is in <models>/manifests (Ollama's own layout, which the pinned version fixes);
               prune() deletes one without a server, blobs_ok() checks that every layer is there and, with rehash, that its SHA-256 is the
               digest it is named after

A model is pulled under its Ollama name (qwen3:8b), copied to its catalog id (qwen3-8b: what [ai] model and web.json say) and the first
name is dropped: the server lists the model under the id, and the layers are shared, never copied. Ollama checks every layer it downloads
against the SHA-256 the registry's manifest gives.
Standard library only, Python 3.8+.
"""
import hashlib
import http.client
import json
import ntpath
import os
import platform
import posixpath
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import urllib.parse
import zipfile

LOOPBACK = "127.0.0.1"
REGISTRY = "registry.ollama.ai"
UA = "nuc-console-ai/1"
UNIX_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
UNPACK_FACTOR = 1.8          # the unpacked build takes up to this many times its archive (1.3 to 1.6 for the 0.35 builds): the disk check
STALL_S = 300                # a pull or a load that says nothing for this long has stalled
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")  # (fullmatch) a catalog id, an Ollama model or tag: what the server is ever asked for
GPU_HIDE = ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES")  # "-1" each: Ollama's CPU only


class SetupError(Exception):
    """An expected failure: printed as one line, exit status 1 (aisetup.SetupError is this class)."""


class Cancelled(Exception):
    """The caller asked to stop (the cancel of the web page and of the screen): what was fetched stays, a later pull resumes it."""


# ---------------------------------------------------------------------------------------------------------------------------
# The build of this machine
# ---------------------------------------------------------------------------------------------------------------------------
def _is_win(plat=None):
    return (plat or sys.platform) == "win32"


def _pm(plat=None):
    return ntpath if _is_win(plat) else posixpath


def asset_key(plat=None, machine=None):
    """The release file this machine runs: windows-amd64, windows-arm64, darwin (one build for Apple silicon and Intel), linux-amd64,
    linux-arm64; None for anything else."""
    plat = plat or sys.platform
    m = (machine if machine is not None else platform.machine() or "").lower()
    arm, x64 = m in ("arm64", "aarch64", "armv8", "armv8l"), m in ("x86_64", "amd64", "x64")
    if plat == "win32":
        return "windows-arm64" if arm else "windows-amd64" if x64 else None
    if plat == "darwin":
        return "darwin" if arm or x64 else None
    if plat.startswith("linux"):
        return "linux-arm64" if arm else "linux-amd64" if x64 else None
    return None


def runtime_asset(runtime, plat=None, machine=None):
    """{"key", "file", "sha256", "size", "url"} of the file this machine needs, or None when the release has no build for it."""
    key = asset_key(plat, machine)
    a = (runtime.get("assets") or {}).get(key) if key else None
    if not a:
        return None
    return dict(a, key=key, url=runtime["base"] + a["file"] if a.get("file") else None)


def runtime_dir(d, runtime, plat=None):
    return _pm(plat).join(d, "runtime", "%s-%s" % (runtime["name"], runtime["version"]))


def exe_rel(plat=None):
    """Where the executable is inside the unpacked build: ollama.exe (Windows), ollama (macOS), bin/ollama (Linux)."""
    plat = plat or sys.platform
    return "ollama.exe" if plat == "win32" else "ollama" if plat == "darwin" else posixpath.join("bin", "ollama")


def exe_path(d, runtime, plat=None):
    p = _pm(plat)
    return p.join(runtime_dir(d, runtime, plat), *exe_rel(plat).split("/"))


def archive_path(d, runtime, plat=None, machine=None):
    """<dir>/runtime/<file of this machine's build>, or None when there is none."""
    a = runtime_asset(runtime, plat, machine)
    return _pm(plat).join(d, "runtime", a["file"]) if a and a.get("file") else None


def models_dir(d, plat=None):
    return _pm(plat).join(d, "models")


# ---------------------------------------------------------------------------------------------------------------------------
# Unpacking: nothing outside the folder, nothing but files, folders and links
# ---------------------------------------------------------------------------------------------------------------------------
def _parts(name):
    """A member's name -> its path parts; SetupError for an absolute path, a drive, a '..' or a name with a control character."""
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or re.search(r"[\x00-\x1f]", n):
        raise SetupError("the archive holds an unsafe name: %s" % _safe(name))
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise SetupError("the archive holds an unsafe name: %s" % _safe(name))
    return parts


def _link_ok(parts, target):
    """A symbolic link at `parts` pointing to `target` stays inside the folder."""
    t = target.replace("\\", "/")
    if t.startswith("/") or re.match(r"^[A-Za-z]:", t):
        return False
    depth = len(parts) - 1
    for p in t.split("/"):
        if p == "..":
            depth -= 1
            if depth < 0:
                return False
        elif p not in ("", "."):
            depth += 1
    return True


def _write(src, path, mode, cancel):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        while True:
            if cancel is not None and cancel():
                raise Cancelled()
            chunk = src.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    os.chmod(path, mode)


def _inside(root, path):
    """The folder of `path`, once the links unpacked so far are followed, is still under `root`: the check of a link's text is lexical,
    a chain of links (a -> ., a/l -> ..) is not."""
    r, d = os.path.realpath(root), os.path.realpath(os.path.dirname(path))
    if d != r and not d.startswith(r.rstrip(os.sep) + os.sep):
        raise SetupError("the archive holds a link that leaves it: %s" % _safe(os.path.relpath(path, root)))


def _untar(t, root, cancel):
    done = {}
    for m in t:
        if cancel is not None and cancel():
            raise Cancelled()
        parts = _parts(m.name)
        if not parts:
            continue
        path = os.path.join(root, *parts)
        _inside(root, path)
        if m.isdir():
            os.makedirs(path, exist_ok=True)
        elif m.isfile():
            src = t.extractfile(m)
            _write(src, path, 0o755 if m.mode & 0o111 else 0o644, cancel)
            done["/".join(parts)] = path
        elif m.issym():
            if not _link_ok(parts, m.linkname):
                raise SetupError("the archive holds a link that leaves it: %s" % _safe(m.name))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.symlink(m.linkname, path)
        elif m.islnk():  # a hard link: a copy of a file unpacked before it
            src = done.get("/".join(_parts(m.linkname)))
            if src is None:
                raise SetupError("the archive holds a link to nothing it unpacked: %s" % _safe(m.name))
            shutil.copy2(src, path)
        # anything else (a device, a fifo) is not unpacked


def _unzip(path, root, cancel):
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if cancel is not None and cancel():
                raise Cancelled()
            parts = _parts(info.filename)
            if not parts:
                continue
            target = os.path.join(root, *parts)
            if info.filename.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            unix = info.external_attr >> 16
            if stat.S_ISLNK(unix):
                raise SetupError("the archive holds a link, which a zip of this build never does: %s" % _safe(info.filename))
            with z.open(info) as src:
                _write(src, target, 0o755 if unix & 0o111 or parts[-1].lower().endswith(".exe") else 0o644, cancel)


def _zstd_reader(path):
    """(a readable stream of the decompressed archive, the process behind it or None)."""
    try:
        from compression import zstd  # Python 3.14 and newer
        return zstd.open(path, "rb"), None
    except ImportError:
        pass
    tool = shutil.which("zstd", path=UNIX_PATH)
    if not tool:
        raise SetupError("this Ollama build is a .tar.zst and this machine cannot read it: install zstd (apt install zstd, dnf install zstd) and try again")
    proc = subprocess.Popen([tool, "-d", "-c", "-q", path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc.stdout, proc


def unpack(archive, dest, cancel=None):
    """Unpack `archive` (.zip, .tgz/.tar.gz, .tar.zst) into the folder `dest`, which is replaced. The work is done in "<dest>.part" and renamed
    at the end, so a folder at `dest` is always a whole build. Cancelled (cancel() says True) and every error leave no partial folder."""
    tmp = dest + ".part"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    low = archive.lower()
    try:
        if low.endswith(".zip"):
            _unzip(archive, tmp, cancel)
        elif low.endswith((".tgz", ".tar.gz")):
            with tarfile.open(archive, "r:gz") as t:
                _untar(t, tmp, cancel)
        elif low.endswith(".tar.zst"):
            stream, proc = _zstd_reader(archive)
            try:
                with tarfile.open(fileobj=stream, mode="r|") as t:
                    _untar(t, tmp, cancel)
            finally:
                stream.close()
                if proc is not None:
                    if proc.wait() != 0:
                        raise SetupError("zstd could not read %s" % _safe(os.path.basename(archive)))
        else:
            raise SetupError("unknown kind of archive: %s" % _safe(os.path.basename(archive)))
    except (tarfile.TarError, zipfile.BadZipFile, EOFError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SetupError("cannot unpack %s: %s" % (_safe(os.path.basename(archive)), _safe(e, 120)))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)


# ---------------------------------------------------------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------------------------------------------------------
def server_argv(exe):
    return [exe, "serve"]


def server_env(d, port, ctx, gpu=True, base=None, plat=None):
    """The environment of `ollama serve`: `base` (the caller's small fixed environment) and Ollama's settings. 127.0.0.1:<port> only; the
    models in the AI folder; `ctx` tokens of context; one model loaded and one request at a time; no cloud models; nothing pruned at start
    (a pull that was cancelled resumes, and the other nuc-console process may be using the folder); gpu=False hides every GPU (CUDA, ROCm,
    Vulkan: Ollama then runs on the CPU; Metal on a Mac cannot be hidden this way)."""
    env = dict(base or {})
    env.update({"OLLAMA_HOST": "%s:%d" % (LOOPBACK, int(port)), "OLLAMA_MODELS": models_dir(d, plat), "OLLAMA_CONTEXT_LENGTH": str(int(ctx)),
                "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_NO_CLOUD": "1", "OLLAMA_NOPRUNE": "1"})
    if not gpu:
        env.update({k: "-1" for k in GPU_HIDE})
    return env


# ---------------------------------------------------------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------------------------------------------------------
def _safe(s, n=80):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", str(s))[:n]


def base_of(endpoint):
    """'http://127.0.0.1:8080/v1' -> 'http://127.0.0.1:8080' (the native API is beside the OpenAI-compatible one)."""
    u = urllib.parse.urlsplit(endpoint)
    return "%s://%s" % (u.scheme, u.netloc)


class Api(object):
    """The few calls of Ollama's HTTP API nuc-console makes, on a loopback server, never through a proxy. Errors are SetupError with the
    server's own words; a cancel (cancel() says True, looked at every 0.2 s) closes the connection and raises Cancelled."""

    def __init__(self, base, timeout=30.0):
        u = urllib.parse.urlsplit(base)
        if u.scheme != "http" or not u.hostname:
            raise SetupError("not an http:// server: %s" % _safe(base))
        self.host, self.port, self.timeout = u.hostname, u.port or 80, timeout

    def _open(self, method, path, body, timeout):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, data, headers)
        return conn

    def call(self, method, path, body=None, timeout=None):
        """-> (status, JSON answer or None)."""
        conn = self._open(method, path, body, timeout or self.timeout)
        try:
            r = conn.getresponse()
            raw = r.read(1 << 20)
            try:
                obj = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else None
            except ValueError:
                obj = None
            return r.status, obj
        finally:
            conn.close()

    def _stream(self, path, body, on_line, cancel, timeout=STALL_S):
        """POST and read the answer line by line (NDJSON), calling on_line(obj) for each; an {"error"} line or an HTTP error raises."""
        conn = self._open("POST", path, body, timeout)
        sock = conn.sock  # (the connection forgets it once an HTTP/1.0 answer has begun: the answer reads from it until it ends)
        stop, gone = threading.Event(), {"cancelled": False}

        def watch():
            while not stop.wait(0.2):
                if cancel is not None and cancel():
                    gone["cancelled"] = True
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except (OSError, AttributeError):
                        pass
                    return
        w = threading.Thread(target=watch, daemon=True)
        w.start()
        try:
            r = conn.getresponse()
            if r.status >= 400:
                obj = _json(r.read(1 << 16))
                raise SetupError(_error(obj) or "the model server answered HTTP %d" % r.status)
            while True:
                if cancel is not None and cancel():
                    gone["cancelled"] = True
                    break
                line = r.readline(1 << 16)
                if not line:
                    break
                obj = _json(line)
                if obj is None:
                    continue
                if obj.get("error"):
                    raise SetupError(_error(obj))
                on_line(obj)
        except (OSError, http.client.HTTPException) as e:
            if gone["cancelled"]:
                raise Cancelled()
            raise SetupError("the model server stopped answering: %s" % _safe(e, 120))
        finally:
            stop.set()
            conn.close()
        if gone["cancelled"]:
            raise Cancelled()

    def version(self, timeout=2.0):
        """The server's version, or None when nothing (or not an Ollama) answers."""
        try:
            status, obj = self.call("GET", "/api/version", timeout=timeout)
        except (OSError, http.client.HTTPException):
            return None
        return _safe(obj.get("version"), 40) if status == 200 and isinstance(obj, dict) and obj.get("version") else None

    def pull(self, name, progress=None, cancel=None, insecure=False):
        """Download `name` from its registry. progress(done, total, status): bytes of every layer seen so far; status: Ollama's words
        ("pulling manifest", "verifying sha256 digest", ...)."""
        layers = {}

        def line(obj):
            dg, status = obj.get("digest"), _safe(obj.get("status") or "", 60)
            if dg and isinstance(obj.get("total"), int):
                layers[dg] = (int(obj.get("completed") or 0), obj["total"])
            if progress:
                progress(sum(c for c, _t in layers.values()), sum(t for _c, t in layers.values()), status)
        self._stream("/api/pull", {"model": name, "stream": True, "insecure": bool(insecure)}, line, cancel)

    def copy(self, source, destination):
        status, obj = self.call("POST", "/api/copy", {"source": source, "destination": destination})
        if status != 200:
            raise SetupError(_error(obj) or "the model server could not copy %s to %s (HTTP %d)" % (_safe(source), _safe(destination), status))

    def delete(self, name):
        """True when it was there and is gone; False when the server did not have it."""
        status, obj = self.call("DELETE", "/api/delete", {"model": name})
        if status == 404:
            return False
        if status != 200:
            raise SetupError(_error(obj) or "the model server could not delete %s (HTTP %d)" % (_safe(name), status))
        return True

    def load(self, name, cancel=None, timeout=STALL_S):
        """Load the model into memory now (the first answer is then quick): returns when it is loaded."""
        self._stream("/api/generate", {"model": name}, lambda obj: None, cancel, timeout)

    def unload(self, name):
        try:
            self.call("POST", "/api/generate", {"model": name, "keep_alive": 0}, timeout=10)
        except (OSError, http.client.HTTPException):
            pass


def _json(raw):
    try:
        obj = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _error(obj):
    return "the model server says: %s" % _safe(obj["error"], 240) if isinstance(obj, dict) and obj.get("error") else ""


# ---------------------------------------------------------------------------------------------------------------------------
# The models on disk (Ollama's layout: manifests/<host>/<namespace>/<model>/<tag>, blobs/sha256-<hex>)
# ---------------------------------------------------------------------------------------------------------------------------
def parse_name(name):
    """'qwen3:8b' -> ('registry.ollama.ai', 'library', 'qwen3', '8b'); 'qwen3-8b' -> (..., 'qwen3-8b', 'latest');
    'hf.co/user/repo:Q4_K_M' -> ('hf.co', 'user', 'repo', 'Q4_K_M'). SetupError for a name with an unsafe part."""
    n, tag = name, "latest"
    if ":" in n.rsplit("/", 1)[-1]:
        n, tag = n.rsplit(":", 1)
    parts = n.split("/")
    if len(parts) == 1:
        host, ns, model = REGISTRY, "library", parts[0]
    elif len(parts) == 2:
        host, ns, model = REGISTRY, parts[0], parts[1]
    elif len(parts) == 3:
        host, ns, model = parts
    else:
        raise SetupError("not a model name: %s" % _safe(name))
    if not (re.fullmatch(r"[A-Za-z0-9.-]+(:\d{1,5})?", host) and all(NAME.fullmatch(x) for x in (ns, model, tag))):
        raise SetupError("not a model name: %s" % _safe(name))
    return host, ns, model, tag


def manifest_path(models, name, plat=None):
    host, ns, model, tag = parse_name(name)
    return _pm(plat).join(models, "manifests", host, ns, model, tag)


def blob_path(models, digest, plat=None):
    return _pm(plat).join(models, "blobs", digest.replace(":", "-"))


def read_manifest(path):
    """The digests and sizes of a manifest's layers (its config too): [(digest, size)]; [] when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for e in [data.get("config")] + list(data.get("layers") or []) if isinstance(data, dict) else []:
        if isinstance(e, dict) and re.match(r"^sha256:[0-9a-f]{64}$", str(e.get("digest", ""))):
            out.append((e["digest"], int(e.get("size") or 0)))
    return out


def has_model(models, name):
    try:
        return os.path.isfile(manifest_path(models, name))
    except SetupError:
        return False


def model_bytes(models, name):
    """The size of a model's layers, from its manifest (0 when it is not there)."""
    try:
        return sum(s for _d, s in read_manifest(manifest_path(models, name)))
    except SetupError:
        return 0


def blobs_ok(models, name, rehash=False):
    """Every layer of the model is there with its size; with rehash, the SHA-256 of each is also the digest it is named after."""
    try:
        layers = read_manifest(manifest_path(models, name))
    except SetupError:
        return False
    if not layers:
        return False
    for digest, size in layers:
        p = blob_path(models, digest)
        try:
            if os.path.getsize(p) != size:
                return False
        except OSError:
            return False
        if rehash:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if "sha256:" + h.hexdigest() != digest:
                return False
    return True


def _all_manifests(models):
    out = []
    for root, _dirs, files in os.walk(os.path.join(models, "manifests")):
        out += [os.path.join(root, f) for f in files]
    return out


def prune(models, names):
    """Delete the models `names` without a server: their manifests, then the layers no other manifest uses (only those: a file nuc-console
    does not know of is left alone). -> (bytes freed, [(file, why) that could not be deleted])."""
    gone, stuck = set(), []
    for name in names:
        p = manifest_path(models, name)
        if not os.path.isfile(p):
            continue
        gone.update(d for d, _s in read_manifest(p))
        try:
            os.unlink(p)
        except OSError as e:
            stuck.append((os.path.basename(p), e.strerror or str(e)))
            continue
        q = os.path.dirname(p)
        while q.startswith(os.path.join(models, "manifests")) and q != os.path.join(models, "manifests"):
            try:
                os.rmdir(q)  # only when empty
            except OSError:
                break
            q = os.path.dirname(q)
    used = set()
    for p in _all_manifests(models):
        used.update(d for d, _s in read_manifest(p))
    freed = 0
    for digest in sorted(gone - used):
        for p in (blob_path(models, digest), os.path.join(models, "metadata", digest.replace(":", "-") + ".json")):
            try:
                size = os.path.getsize(p)
                os.unlink(p)
                freed += size
            except FileNotFoundError:
                pass
            except OSError as e:
                stuck.append((os.path.basename(p), e.strerror or str(e)))
    return freed, stuck


# ---------------------------------------------------------------------------------------------------------------------------
# The registry (maintainers: `aisetup.py pins`)
# ---------------------------------------------------------------------------------------------------------------------------
MANIFEST_TYPES = "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json"


def manifest_url(name):
    host, ns, model, tag = parse_name(name)
    return "https://%s/v2/%s/%s/manifests/%s" % (host, ns, model, tag)


def blob_url(name, digest):
    host, ns, model, _tag = parse_name(name)
    return "https://%s/v2/%s/%s/blobs/%s" % (host, ns, model, digest)


def manifest_summary(manifest):
    """A registry manifest -> {"size": bytes of every layer and the config, "model": bytes of the weights, "license": digest of the licence
    layer or None}."""
    layers = list(manifest.get("layers") or []) if isinstance(manifest, dict) else []
    if not layers:
        raise SetupError("the registry's manifest has no layers")
    cfg = manifest.get("config") or {}
    size = int(cfg.get("size") or 0) + sum(int(x.get("size") or 0) for x in layers if isinstance(x, dict))
    model = sum(int(x.get("size") or 0) for x in layers if isinstance(x, dict) and str(x.get("mediaType", "")).endswith(".model"))
    lic = next((x.get("digest") for x in layers if isinstance(x, dict) and str(x.get("mediaType", "")).endswith(".license")), None)
    return {"size": size, "model": model, "license": lic}
