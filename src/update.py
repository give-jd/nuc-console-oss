#!/usr/bin/env python3
"""nuc-console-update, the logic (stdlib only, Python 3.8+). Never automatic, never in the background: you run it.

The wrappers (bin/nuc-console-update for Linux and macOS, bin\\nuc-console-update.cmd/.ps1 for Windows) ask GitHub for the
latest release (curl, Invoke-RestMethod: HTTPS only), save the answer and call this file, which decides and verifies:

    update.py --release-json FILE --cache DIR [--mode installed|portable] [--app DIR] [--root DIR] [--check] [--yes]
              [--stage-file FILE] [--allow-unattested]

  * the installed version is VERSION in nuc_config.py of --app (default: the folder of this file);
  * versions are compared as numbers (1.10.0 is newer than 1.9.9), nothing happens when the release is not newer;
  * the archive for this OS and processor (and SHA256SUMS) go to the cache: a file whose SHA-256 is already right is not downloaded
    again (Linux and macOS: nuc-console-X.Y.Z-linux-x86_64, -linux-arm64, -macos-arm64, -macos-x86_64 .tar.gz; Windows: -windows-x64,
    -windows-arm64 .zip; each carries the Python it runs with); the archive is checked against SHA256SUMS (a mismatch deletes it and stops); `gh attestation verify` also runs when
    gh is installed and logged in (a failure always stops; without gh, or without a login, --mode installed, which runs the installer as root, stops
    too unless --allow-unattested; --mode portable says that provenance was not checked and goes on);
  * the archive is extracted into a fresh folder of the cache (members that could escape it are refused), and its VERSION must be
    the release's;
  * --mode portable: replaces src/, bin/, docs/ ... of --root with the new ones; data/ and cache/ stay (a git checkout is refused:
    git pull). --mode installed: the folder is written to --stage-file and the wrapper runs that release's installer (it keeps
    config.ini, the baseline, ...): on Windows the installer replaces the very Python that runs this file, so it cannot be
    started from here.
"""
import argparse
import hashlib
import hmac
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlsplit

REPO = "give-jd/nuc-console-oss"
API = "https://api.github.com/repos/%s/releases/latest" % REPO
SUMS = "SHA256SUMS"
WINDOWS = sys.platform == "win32"
MAX_ARCHIVE = 256 << 20  # bytes: the biggest archive that is downloaded or unpacked (every archive carries a Python: 20 to 40 MB)
OWN_DIRS = ("src", "bin", "docs", "config", "scripts", "systemd", "launchd")  # folders that belong wholly to a release
KEEP = ("data", "cache")  # portable folder: yours, never touched
STRICT = re.compile(r"\d+\.\d+\.\d+")


class UpdateError(Exception):
    pass


# ---- versions and assets -------------------------------------------------------------------------------------------------

def parse_version(text):
    """'v1.10.2' / '1.10' / '2' -> (1, 10, 2) / (1, 10, 0) / (2, 0, 0): numbers, so that 1.10.0 > 1.9.9."""
    m = re.fullmatch(r"v?(\d{1,6})(?:\.(\d{1,6}))?(?:\.(\d{1,6}))?", text.strip() if isinstance(text, str) else "")
    if not m:
        raise ValueError("not a version number: %r" % (text,))
    return tuple(int(x or 0) for x in m.groups())


def newer(latest, current):
    return parse_version(latest) > parse_version(current)


def read_version(app_dir):
    """VERSION = "X.Y.Z" of nuc_config.py in app_dir, read as text (nothing of that folder is imported or run)."""
    path = os.path.join(app_dir, "nuc_config.py")
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"^VERSION\s*=\s*[\"']([^\"']+)[\"']", f.read(), re.M)
    except OSError as e:
        raise UpdateError("cannot read %s: %s" % (path, e))
    if not m:
        raise UpdateError('%s has no VERSION = "X.Y.Z"' % path)
    return m.group(1)


def detect_os():
    return "windows" if WINDOWS else "macos" if sys.platform == "darwin" else "linux"


def detect_arch():
    raw = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or "") if WINDOWS else ""
    return raw or platform.machine()


def norm_os(name):
    n = (name or "").lower()
    if n.startswith("win"):
        return "windows"
    if n in ("darwin", "macos", "mac", "osx"):
        return "macos"
    if n.startswith("linux"):
        return "linux"
    raise UpdateError("unsupported system: %r (linux, macos or windows)" % name)


def norm_arch(arch):
    a = (arch or "").lower()
    if a in ("x86_64", "amd64", "x64"):
        return "x64"
    if a in ("arm64", "aarch64"):
        return "arm64"
    return a or "unknown"


def https_only(url):
    parts = urlsplit(url or "")
    if parts.scheme != "https" or not parts.hostname:
        raise UpdateError("refusing a URL that is not HTTPS: %r" % (url,))
    return url


OS_LABEL = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}
# the processor part of the archive name, per system: tools/build_release.py (archive_names) makes these names
ARCH_SUFFIX = {"linux": {"x64": "x86_64", "arm64": "arm64"}, "macos": {"x64": "x86_64", "arm64": "arm64"},
               "windows": {"x64": "x64", "arm64": "arm64"}}


def archive_name(version, os_name, arch):
    """The archive of this system, as tools/build_release.py names it: per system and processor, e.g.
    nuc-console-1.5.0-linux-x86_64.tar.gz, -linux-arm64.tar.gz, -macos-arm64.tar.gz, -macos-x86_64.tar.gz, -windows-x64.zip,
    -windows-arm64.zip. A processor there is no archive for (32-bit x86 or ARM, RISC-V, ...) is refused with the list of what exists."""
    os_name, arch = norm_os(os_name), norm_arch(arch)
    suffix = ARCH_SUFFIX[os_name].get(arch)
    if suffix is None:
        raise UpdateError("there is no nuc-console archive for %s on the processor %r: only 64-bit x86 (%s) and 64-bit ARM (%s) have one"
                          % (OS_LABEL[os_name], arch, ARCH_SUFFIX[os_name]["x64"], ARCH_SUFFIX[os_name]["arm64"]))
    return "nuc-console-%s-%s-%s.%s" % (version, os_name, suffix, "zip" if os_name == "windows" else "tar.gz")


def release_version(release):
    """'1.5.0' from the tag_name ('v1.5.0') of a release answer; anything but X.Y.Z is refused."""
    if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
        raise UpdateError("the release answer has no tag_name")
    tag = release["tag_name"].strip()
    version = tag[1:] if tag[:1] == "v" else tag
    if not STRICT.fullmatch(version):
        raise UpdateError("the latest release is called %r: X.Y.Z expected" % tag)
    return version


def pick_asset(release, os_name, arch):
    """-> {"version", "name", "url", "sums_url"} of the archive for this OS and processor in a GitHub release (JSON of the API).
    The processor decides: a release that has no archive for it is refused, and the message says which ones it does have."""
    version = release_version(release)
    name = archive_name(version, os_name, arch)
    found, names = {}, []
    for a in release.get("assets") or []:
        if isinstance(a, dict) and isinstance(a.get("name"), str):
            names.append(a["name"])
            if a["name"] in (name, SUMS) and isinstance(a.get("browser_download_url"), str):
                found[a["name"]] = a["browser_download_url"]
    if name not in found:
        mine = sorted(n for n in names if n.startswith("nuc-console-%s-%s-" % (version, norm_os(os_name))))
        raise UpdateError("release %s has no %s (the archive for %s on %s)%s" % (
            release["tag_name"], name, OS_LABEL[norm_os(os_name)], norm_arch(arch),
            ": for this system it has " + ", ".join(mine) if mine else " (still being built, or not for this system)"))
    if SUMS not in found:
        raise UpdateError("release %s has no %s (still being built)" % (release["tag_name"], SUMS))
    if any(urlsplit(https_only(found[n])).hostname != "github.com" for n in (name, SUMS)):
        raise UpdateError("the assets are not on github.com: refused")
    return {"version": version, "name": name, "url": found[name], "sums_url": found[SUMS]}


# ---- SHA-256 and the cache -----------------------------------------------------------------------------------------------

def parse_sha256sums(text):
    """'<64 hex>  name' (or ' *name') lines, as `sha256sum` writes them -> {name: hex}. Other lines are ignored."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?(\S.*?)\s*", line)
        if not m:
            continue
        digest, name = m.group(1).lower(), m.group(2)
        if out.get(name, digest) != digest:
            raise UpdateError("%s lists %s twice with different hashes" % (SUMS, name))
        out[name] = digest
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_file(path, expected):
    """True when the file exists and its SHA-256 is `expected`."""
    try:
        return hmac.compare_digest(sha256_file(path), expected.lower())
    except OSError:
        return False


def trusted_tool(path):
    """As root, a helper is run only when root owns it and nobody else can write it (a user's binary must not become root code)."""
    if WINDOWS or not hasattr(os, "geteuid") or os.geteuid() != 0 or not path:
        return bool(path)
    try:
        st = os.stat(os.path.realpath(path))
    except OSError:
        return False
    return st.st_uid == 0 and not st.st_mode & 0o022


class _HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_only(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def https_download(url, dest, run=subprocess.run, which=shutil.which):
    """Downloads over HTTPS (redirects too) into dest. curl where there is one (Linux, macOS: it uses the system's certificates,
    the python.org Python for macOS has none), else urllib (Windows: the Windows certificate store)."""
    https_only(url)
    curl = None if WINDOWS else which("curl")
    if curl and trusted_tool(curl):
        r = run([curl, "-fsSL", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2", "--connect-timeout", "20",
                 "--max-time", "900", "--retry", "2", "--max-filesize", str(MAX_ARCHIVE), "-o", dest, url],
                capture_output=True, text=True)
        if r.returncode != 0:
            raise UpdateError("download failed (curl %d): %s %s" % (r.returncode, url, (r.stderr or "").strip()))
        return
    req = urllib.request.Request(url, headers={"User-Agent": "nuc-console-update"})
    try:
        with urllib.request.build_opener(_HttpsRedirect).open(req, timeout=60) as resp, open(dest, "wb") as f:
            size = 0
            for block in iter(lambda: resp.read(1 << 20), b""):
                size += len(block)
                if size > MAX_ARCHIVE:
                    raise UpdateError("download too big: %s" % url)
                f.write(block)
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError("download failed: %s: %s" % (url, e))


def prepare_cache(cache):
    """Creates the cache folder (0755). As root it refuses one that is not root's alone: an archive that was checked must not
    be swappable by anyone else before it is unpacked and run."""
    if not os.path.lexists(cache):
        os.makedirs(cache, mode=0o755)
        os.chmod(cache, 0o755)  # the umask may have taken some of it away
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        st = os.lstat(cache)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise UpdateError("%s is not a folder only root can write to (a link, or somebody else's): refused" % cache)


def prune_cache(cache, keep):
    """The archives of older releases are not needed any more (the Python zips and other files of the cache are not ours)."""
    for n in os.listdir(cache):
        if n != keep and re.fullmatch(r"nuc-console-\d+\.\d+\.\d+-(linux|macos|windows)(-\w+)?\.(tar\.gz|zip)(\.part)?", n):
            os.remove(os.path.join(cache, n))


def ensure_cached(cache, name, expected, url, download, say=print):
    """-> (path, downloaded). A file of the cache whose SHA-256 is already `expected` is used as it is: no download.
    Else (missing, partial, corrupt) it is downloaded, and a download that does not match `expected` is deleted and refused."""
    if os.path.basename(name) != name or not name or name.startswith("."):
        raise UpdateError("unsafe file name: %r" % name)
    dest, part = os.path.join(cache, name), os.path.join(cache, name) + ".part"
    if verify_file(dest, expected):
        say("%s is already in the cache (SHA-256 matches SHA256SUMS): not downloaded again" % name)
        return dest, False
    os.makedirs(cache, exist_ok=True)
    for stale in (dest, part):
        if os.path.exists(stale):
            os.remove(stale)
    say("downloading %s" % name)
    try:
        download(https_only(url), part)
        got = sha256_file(part)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    if not hmac.compare_digest(got, expected.lower()):
        os.remove(part)
        raise UpdateError("%s has SHA-256 %s but %s says %s: deleted, not installed" % (name, got, SUMS, expected.lower()))
    os.replace(part, dest)
    return dest, True


def gh_attest(path, repo=REPO, which=shutil.which, run=subprocess.run):
    """gh attestation verify -> (status, message): 'ok' | 'missing' (no gh) | 'noauth' (gh is not logged in) | 'failed' (refuse)."""
    gh = which("gh")
    if not gh:
        return "missing", "provenance not checked: gh (GitHub CLI) is not installed"
    if not trusted_tool(gh):
        return "missing", "provenance not checked: %s is not owned by root, not run as root" % gh
    try:
        r = run([gh, "attestation", "verify", path, "--repo", repo], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "failed", "gh attestation verify did not answer in time"
    except OSError as e:
        return "missing", "provenance not checked: cannot run gh (%s)" % e
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode == 0:
        return "ok", "provenance verified (gh attestation verify, %s)" % repo
    if re.search(r"gh auth login|GH_TOKEN|GITHUB_TOKEN|not logged in|authentication required|no oauth token", text, re.I):
        return "noauth", "provenance not checked: gh is not logged in (gh auth login)"
    return "failed", "gh attestation verify failed: %s" % (text.splitlines()[-1] if text else "exit code %d" % r.returncode)


# ---- extraction ----------------------------------------------------------------------------------------------------------

def _safe_name(name):
    parts = name.replace("\\", "/").split("/")
    if name.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", name) or ".." in parts or "\0" in name:
        raise UpdateError("unsafe path in the archive: %r" % name)
    return [p for p in parts if p not in ("", ".")] or [""]


def _link_inside(name, target):
    """May the symbolic link `name` of an archive point at `target`? Only a relative path that stays inside the archive's top
    folder (the Python of the Linux and macOS archives has links such as python/bin/python3 -> python3.13)."""
    if not target or "\0" in target or target.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", target):
        return False
    top = _safe_name(name)[0]
    joined = posixpath.normpath(posixpath.join(posixpath.dirname("/".join(_safe_name(name))), target.replace("\\", "/")))
    return joined.startswith(top + "/")


def _links_stay_inside(dest, members):
    """_link_inside only reads the names: two links that each look harmless can lead out when one follows the other. Once
    unpacked, what every link really resolves to (realpath) must still be inside its top folder; else the folder is deleted."""
    real = os.path.realpath(dest)
    for m in members:
        if not m.issym():
            continue
        top = os.path.join(real, _safe_name(m.name)[0])
        target = os.path.realpath(os.path.join(real, *_safe_name(m.name)))
        if target != top and not target.startswith(top + os.sep):
            shutil.rmtree(dest, ignore_errors=True)
            raise UpdateError("the archive holds links that lead out of its folder: %r" % m.name)


def extract(archive, dest):
    """Unpacks a .tar.gz or .zip of the release into dest -> the path of its one top folder (nuc-console-X.Y.Z).
    Absolute paths, '..', devices and hard links are refused, and so is a symbolic link that is absolute or leaves the top folder;
    so is an archive that holds more than MAX_ARCHIVE bytes."""
    os.makedirs(dest, exist_ok=True)
    tops, total = set(), 0
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                tops.add(_safe_name(info.filename)[0])
                total += info.file_size
            if total > MAX_ARCHIVE * 4:
                raise UpdateError("archive too big once unpacked")
            z.extractall(dest)
    else:
        with tarfile.open(archive, "r:gz") as t:
            members = t.getmembers()
            links = set()  # names of the symbolic links seen so far: nothing may be written through one
            for m in members:
                parts = _safe_name(m.name)
                if any("/".join(parts[:i]) in links for i in range(1, len(parts))):
                    raise UpdateError("the archive writes through a link: %r" % m.name)
                if m.issym():
                    links.add("/".join(parts))
                    if not _link_inside(m.name, m.linkname):
                        raise UpdateError("the archive holds a link that leaves its folder: %r -> %r" % (m.name, m.linkname))
                elif not (m.isfile() or m.isdir()):
                    raise UpdateError("the archive holds something that is not a file, a folder or a link inside it: %r" % m.name)
                tops.add(_safe_name(m.name)[0])
                total += m.size
                if not m.issym():
                    m.mode = 0o755 if m.isdir() or m.mode & 0o111 else 0o644  # no setuid, nothing writable by others
                m.uid = m.gid = 0
                m.uname = m.gname = ""
            if total > MAX_ARCHIVE * 4:
                raise UpdateError("archive too big once unpacked")
            kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            t.extractall(dest, members=members, **kwargs)
            _links_stay_inside(dest, members)
    tops.discard("")  # an entry for the folder itself ('./')
    if len(tops) != 1:
        raise UpdateError("the archive must hold one top folder, it holds %s" % sorted(tops))
    return os.path.join(dest, tops.pop())


def _same(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b) and sha256_file(a) == sha256_file(b)
    except OSError:
        return False


def _release_entries(new):
    """-> [(path with /, absolute path, link target or None)] of every file and every symbolic link (also one that points to a
    folder: os.walk lists it among the folders and does not enter it) of the extracted release `new`."""
    out = []
    for dirpath, dirs, files in os.walk(new):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]:
            full = os.path.join(dirpath, name)
            out.append((os.path.relpath(full, new).replace(os.sep, "/"), full, os.readlink(full) if os.path.islink(full) else None))
    return out


def _unchanged(src, dst, link):
    if link is not None:
        return os.path.islink(dst) and os.readlink(dst) == link
    return os.path.isfile(dst) and not os.path.islink(dst) and _same(src, dst)


def apply_portable(new, root, say=print):
    """Replaces the code of the portable folder `root` with the release extracted in `new`; data/ and cache/ (yours) stay.
    Two steps, so that a full disk or a locked file stops it before anything is replaced: every new or changed file is first
    written beside its target (.new), then all are renamed over their targets (a script that is running right now is never
    half-written); what the old release had in src/, bin/, docs/ ... and the new one dropped is deleted. Symbolic links (the
    ones of the Python of a Linux or macOS archive) are made again as links, and the times of the files are kept (the .pyc files of
    a standard library may record the time of their source). python/ belongs to the release too when it ships python/bin/python3
    (the Python of a Linux or macOS archive): a file of the old Python that the new one lacks is deleted. -> files changed."""
    shipped, todo = set(), []
    for rel, src, link in _release_entries(new):
        if rel.split("/")[0] in KEEP:
            continue
        shipped.add(rel)
        dst = os.path.join(root, *rel.split("/"))
        if not _unchanged(src, dst, link):
            todo.append((src, dst, link))
    written = []
    try:
        for src, dst, link in todo:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".new"
            written.append(tmp)
            if os.path.lexists(tmp):
                os.remove(tmp)
            if link is not None:
                os.symlink(link, tmp)
            else:
                shutil.copyfile(src, tmp)
                shutil.copystat(src, tmp)
    except BaseException:
        for tmp in written:
            if os.path.lexists(tmp):
                os.remove(tmp)
        raise
    for (_src, dst, _link), tmp in zip(todo, written):
        if os.path.isdir(dst) and not os.path.islink(dst):  # a folder of the old release where the new one has a file or a link
            shutil.rmtree(dst)
        os.replace(tmp, dst)
    changed = len(todo)
    own = OWN_DIRS + (("python",) if "python/bin/python3" in shipped else ())
    stale = []
    for top in own:
        for dirpath, dirs, files in os.walk(os.path.join(root, top)):
            names = files + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]
            stale += [os.path.join(dirpath, f) for f in names
                      if os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/") not in shipped]
    pydir = os.path.join(root, "python")  # the Windows archive's embeddable Python: a new zip replaces the old one
    if os.path.isdir(pydir) and "python" not in own:
        stale += [os.path.join(pydir, f) for f in os.listdir(pydir)
                  if re.fullmatch(r"python-[\d.]+-embed-\w+\.zip", f) and "python/" + f not in shipped]
    for path in stale:
        os.remove(path)
    for top in own:  # folders the new release does not have any more
        for dirpath, _dirs, _files in os.walk(os.path.join(root, top), topdown=False):
            if not os.path.islink(dirpath) and not os.listdir(dirpath):
                os.rmdir(dirpath)
    say("%d files updated, %d removed in %s (data/ and cache/ untouched)" % (changed, len(stale), root))
    return changed


# ---- the whole update ----------------------------------------------------------------------------------------------------

def ask(prompt):
    if not sys.stdin or not sys.stdin.isatty():
        raise UpdateError("not interactive: add --yes to update without asking")
    return input(prompt).strip().lower() in ("y", "yes")


def clean_stages(cache, older_than=3600):
    """Folders a stopped update left in the cache (extracted releases)."""
    try:
        names = os.listdir(cache)
    except OSError:
        return
    for n in names:
        p = os.path.join(cache, n)
        if n.startswith("stage-") and os.path.isdir(p) and time.time() - os.path.getmtime(p) > older_than:
            shutil.rmtree(p, ignore_errors=True)


def run_update(release, app, cache, mode="installed", root="", os_name=None, arch=None, check=False, yes=False,
               stage_file="", download=https_download, attest=gh_attest, confirm=ask, say=print, allow_unattested=False):
    """-> exit code. Raises UpdateError for what stops an update (the message says why).
    Provenance: 'failed' always stops; 'ok' goes on; anything else (no gh, no login, gh not runnable) stops in mode installed,
    which runs the new installer as root, unless allow_unattested (the --allow-unattested flag); in mode portable it warns."""
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError("the release is a draft or a pre-release: ignored")
    if mode == "portable" and os.path.lexists(os.path.join(root, ".git")):  # a clone is updated by git; this would overwrite tracked files
        raise UpdateError("%s is a git checkout: update it with git pull (this replaces the files of an extracted release)" % root)
    current, latest = read_version(app), release_version(release)
    if not newer(latest, current):
        say("already at %s" % current + ("" if parse_version(current) == parse_version(latest)
                                         else " (newer than the latest release, %s)" % latest))
        return 0
    asset = pick_asset(release, os_name or detect_os(), arch or detect_arch())
    where = root if mode == "portable" else app
    say("nuc-console %s in %s; the latest release is %s" % (current, where, latest))
    if check:
        say("update available: %s -> %s (%s)" % (current, latest, asset["name"]))
        return 0
    if not yes and not confirm("update nuc-console %s -> %s? [y/N] " % (current, latest)):
        say("not updated")
        return 0
    prepare_cache(cache)
    clean_stages(cache)
    part = os.path.join(cache, SUMS + ".part")  # SHA256SUMS is always fetched anew: it is what the archive is checked against
    try:
        download(https_only(asset["sums_url"]), part)
        with open(part, encoding="utf-8", errors="replace") as f:
            sums = parse_sha256sums(f.read())
        os.replace(part, os.path.join(cache, SUMS))
    finally:
        if os.path.exists(part):
            os.remove(part)
    if asset["name"] not in sums:
        raise UpdateError("%s has no hash for %s: not installed" % (SUMS, asset["name"]))
    path, _ = ensure_cached(cache, asset["name"], sums[asset["name"]], asset["url"], download, say)
    say("SHA-256 ok: %s matches %s" % (asset["name"], SUMS))
    status, message = attest(path)
    if status == "failed":
        raise UpdateError(message + ": not installed")
    if status != "ok" and mode == "installed":
        if not allow_unattested:
            raise UpdateError(message + ": not installed. An installed update runs the new installer as root, so its provenance must be "
                              "verified: install gh and run `gh auth login`, or check the archive yourself with `gh attestation verify "
                              "%s --repo %s` and run again with --allow-unattested" % (path, REPO))
        say("WARNING: --allow-unattested: %s; going on without independent verification of the release" % message)
    else:
        say(message)
    stage = tempfile.mkdtemp(prefix="stage-", dir=cache)
    keep = False
    try:
        top = extract(path, stage)
        got = read_version(os.path.join(top, "src"))
        if got != latest:
            raise UpdateError("the archive holds version %s, the release is %s: not installed" % (got, latest))
        prune_cache(cache, asset["name"])
        if mode == "portable":
            apply_portable(top, root, say)
            say("updated to %s: start it again with run.sh / run.cmd (stop a running one first)" % latest)
        else:
            if not stage_file:
                raise UpdateError("--stage-file is needed to install")
            with open(stage_file, "w", encoding="utf-8") as f:
                f.write(top + "\n")
            keep = True  # the wrapper runs the installer from it, then deletes it
            say("%s verified and unpacked: installing %s" % (asset["name"], latest))
    finally:
        if not keep:
            shutil.rmtree(stage, ignore_errors=True)
    return 0


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="nuc-console-update helper: use bin/nuc-console-update, it calls this.")
    p.add_argument("--release-json", required=True, help="the answer of %s, saved by the wrapper" % API)
    p.add_argument("--cache", required=True)
    p.add_argument("--mode", choices=("installed", "portable"), default="installed")
    p.add_argument("--app", default=here, help="folder with the installed nuc_config.py (default: this file's)")
    p.add_argument("--root", default="", help="portable: the folder to update")
    p.add_argument("--check", action="store_true", help="only report")
    p.add_argument("--yes", action="store_true", help="do not ask")
    p.add_argument("--allow-unattested", action="store_true",
                   help="installed: go on although the build provenance was not verified (no gh, or not logged in); a failed check still stops")
    p.add_argument("--stage-file", default="", help="installed: where the folder to install from is written")
    p.add_argument("--os", dest="os_name")
    p.add_argument("--arch")
    a = p.parse_args(argv)
    try:
        if a.mode == "portable" and not a.root:
            raise UpdateError("--root is needed with --mode portable")
        with open(a.release_json, encoding="utf-8") as f:
            release = json.load(f)
        if not isinstance(release, dict):
            raise UpdateError("the release answer is not what GitHub sends")
        return run_update(release, a.app, a.cache, a.mode, a.root, a.os_name, a.arch, a.check, a.yes, a.stage_file,
                          allow_unattested=a.allow_unattested)
    except (UpdateError, ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as e:
        print("nuc-console-update: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
