#!/usr/bin/env python3
"""Builds the release archives of nuc-console (stdlib only, Python 3.8+); .github/workflows/release.yml publishes them.

    python3 tools/build_release.py --version X.Y.Z --out dist/ --python-dir DIR
    python3 tools/build_release.py --list-python     # the Pythons to download into DIR: "file sha256 url" per line

Every archive carries the Python it runs with, so that "download, unpack, use" needs no Python and no network on the machine.
Creates in --out (each archive has one top folder, nuc-console-X.Y.Z/):
  nuc-console-X.Y.Z-linux-x86_64.tar.gz   install.sh, run.sh, src/, bin/, config/, systemd/, scripts/, docs/, README.md, ...
  nuc-console-X.Y.Z-linux-arm64.tar.gz    and python/: a python-build-standalone CPython for that target, already unpacked
  nuc-console-X.Y.Z-macos-arm64.tar.gz    (python/bin/python3), with install.sh + install-macos.sh and launchd/ instead of
  nuc-console-X.Y.Z-macos-x86_64.tar.gz   systemd/ on macOS
  nuc-console-X.Y.Z-windows-x64.zip       install-windows.cmd + .ps1, run.cmd + .ps1, src/, bin/*.cmd, config/, docs/, ... and
  nuc-console-X.Y.Z-windows-arm64.zip     python/python-<v>-embed-<arch>.zip: the official embeddable Python, which
                                          install-windows.ps1 and run.ps1 take from there (it stays a zip: they check it first)
  SHA256SUMS                              "<sha256>  <name>" of every archive: sha256sum -c SHA256SUMS

What goes in is computed, never listed by hand: every file git tracks (every file under --root when it is not a git
checkout), minus what is for development only (tests/, tools/, CONTRIBUTING.md, dotfiles such as .github/), and per
OS minus what belongs to another one: systemd/ is Linux only; launchd/ and install-macos.sh macOS only; *.cmd, *.bat
and *.ps1 Windows only; shell scripts (*.sh, extensionless files with a #!/bin/sh-like line) and scripts/ not on Windows.
A new file (a portable launcher, an updater) is shipped without touching this script.

Reproducible: sorted entries, every mtime = SOURCE_DATE_EPOCH (default: the time of the last commit), uid/gid 0 and
no user names, modes 0755/0644 from git's exec bit (not from the file system), gzip and zip headers without a name or
a time of their own. The same tree, version and Pythons give byte-identical archives (with the same zlib: CI builds with
Python 3.12 on Ubuntu). Line endings as .gitattributes checks them out, whatever the checkout did: shell scripts and #!
files LF, *.cmd CRLF. The one exception to "every mtime" is the unpacked Python tree: its entries keep the times of the
tarball (pinned by SHA-256, so the same every time), because the .pyc files of a standard library may record the time of their
source. Symbolic links inside that tree are kept as links (a link outside the tree, an absolute one, a device and a file below
a link are refused), hard links become copies, and its exec bits are kept (modes 0755/0644, links 0777).

Refuses: a --version other than VERSION in src/nuc_config.py; a Python that is not pinned (tools/python-pins.json holds null
until the python-pins job of .github/workflows/ai-pins.yml has filled it: never a value that was not read from the release);
and any Python file whose size or SHA-256 is not the pinned one. The pins: tools/python-pins.json for the Linux and macOS
tarballs, $PyVersion / $PyBuilds in install-windows.ps1 for the Windows zips (the one source of truth of the installer, this
script and the workflow; --pins and --python-pins read other files, for the tests). Nothing is written if a check fails.
"""
import argparse
import ast
import collections
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import tarfile
import time
import urllib.parse
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT = "nuc-console"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
PY_URL = "https://www.python.org/ftp/python/{version}/{file}"  # the URL install-windows.ps1 downloads from
WINDOWS_ARCHS = (("x64", "AMD64"), ("arm64", "ARM64"))  # release archive suffix, $PyBuilds key
PYTHON_DIR = "python"  # inside every archive: Windows has the embeddable zip there, Linux and macOS the unpacked Python
# Linux and macOS: python-build-standalone (relocatable CPython builds). Archive suffix -> the target in its file names.
STANDALONE_TARGETS = {
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-arm64": "aarch64-unknown-linux-gnu",
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x86_64": "x86_64-apple-darwin",
}
STANDALONE_FLAVOUR = "install_only_stripped"
STANDALONE_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/{release}/{file}"  # file is URL-quoted: its + is %2B
STANDALONE_FILE = "cpython-{python}+{release}-{target}-%s.tar.gz" % STANDALONE_FLAVOUR
PINS_FILE = os.path.join("tools", "python-pins.json")  # relative to --root
DEV_DIRS, DEV_FILES = ("tests", "tools"), ("CONTRIBUTING.md",)  # never shipped (nor any dotfile: .github/, .gitignore)
WINDOWS_EXT, SHELL_EXT = (".cmd", ".bat", ".ps1"), (".sh",)
ZIP_EPOCH_MIN = 315532800  # 1980-01-01 00:00 UTC: the oldest time a zip entry can hold


class ReleaseError(Exception):
    pass


class Link(str):
    """The data of an archive entry that is a symbolic link: its target, a relative path (only inside the unpacked Python)."""


# path, data (bytes: a file; None: a folder; Link: a symbolic link), mode, mtime (None: the time of the build)
Entry = collections.namedtuple("Entry", "path data mode mtime")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def archive_names(version):
    """The archives of a release, in the order they are built: Linux and macOS per processor, then Windows per processor."""
    top = f"{PROJECT}-{version}"
    return [f"{top}-{suffix}.tar.gz" for suffix in STANDALONE_TARGETS] + [f"{top}-windows-{suffix}.zip" for suffix, _ in WINDOWS_ARCHS]


# ---- inputs ----------------------------------------------------------------------------------------------------------

def read_version(root):
    """VERSION = "X.Y.Z" of src/nuc_config.py, read without importing it."""
    path = os.path.join(root, "src", "nuc_config.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    raise ReleaseError(f'{path}: no VERSION = "X.Y.Z"')


def read_pins(path):
    """-> (python version, {"AMD64": (file, sha256), "ARM64": (file, sha256)}) from $PyVersion and $PyBuilds of
    install-windows.ps1 (or a file written like it): the embeddable Pythons of the Windows archives."""
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    m = re.search(r"^\$PyVersion\s*=\s*'([^']*)'", text, re.M)
    if not m or not VERSION_RE.fullmatch(m.group(1)):
        raise ReleaseError(f"{path}: no $PyVersion = 'X.Y.Z'")
    version = m.group(1)
    start = text.find("$PyBuilds = @{")
    end = text.find("\n}", start)
    if start < 0 or end < 0:
        raise ReleaseError(f"{path}: no $PyBuilds = @{{ ... }}")
    builds = {}
    for key, file, digest in re.findall(r"""'(\w+)'\s*=\s*@\{\s*file\s*=\s*"([^"]*)"\s*;\s*sha256\s*=\s*'([^']*)'\s*\}""",
                                        text[start:end]):
        file = file.replace("$PyVersion", version)
        if not re.fullmatch(r"python-%s-embed-%s\.zip" % (re.escape(version), key.lower()), file):
            raise ReleaseError(f"{path}: $PyBuilds '{key}': unexpected file name {file}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ReleaseError(f"{path}: $PyBuilds '{key}': the SHA-256 must be 64 lowercase hex digits")
        if key in builds:
            raise ReleaseError(f"{path}: $PyBuilds '{key}' twice")
        builds[key] = (file, digest)
    missing = [key for _, key in WINDOWS_ARCHS if key not in builds]
    if missing:
        raise ReleaseError(f"{path}: $PyBuilds has no {', '.join(missing)}")
    return version, builds


def read_python_pins(path):
    """-> (python version, release tag, {archive suffix: (file, sha256, size)}) from tools/python-pins.json: the
    python-build-standalone tarballs of the Linux and macOS archives. A file in which anything is still null is refused
    (the placeholders of a repository whose Pythons have not been pinned yet): never a value that was not read from the release."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            doc = json.load(f)
    except OSError as e:
        raise ReleaseError(f"{path}: {e.strerror or e}")
    except ValueError as e:
        raise ReleaseError(f"{path}: not JSON ({e})")
    if not isinstance(doc, dict) or not isinstance(doc.get("targets"), dict):
        raise ReleaseError(f'{path}: {{"python": ..., "release": ..., "targets": {{...}}}} expected')
    unknown = [k for k in doc if k not in ("python", "release", "targets") and not k.startswith("_")]
    if unknown:
        raise ReleaseError(f"{path}: unknown key {unknown[0]!r}")
    targets = doc["targets"]
    if sorted(targets) != sorted(STANDALONE_TARGETS):
        raise ReleaseError(f"{path}: targets must be exactly {', '.join(STANDALONE_TARGETS)}")
    nulls = [k for k in ("python", "release") if doc.get(k) is None]
    for suffix in STANDALONE_TARGETS:
        entry = targets[suffix]
        if not isinstance(entry, dict):
            raise ReleaseError(f"{path}: targets.{suffix} must be an object")
        nulls += [f"{suffix}.{k}" for k in ("file", "sha256", "size") if entry.get(k) is None]
    if nulls:
        what = "every value is null" if len(nulls) == 2 + 3 * len(STANDALONE_TARGETS) else f"{', '.join(nulls)}: null"
        raise ReleaseError(
            f"{path}: the Python for the Linux and macOS archives is not pinned yet ({what}). Run the python-pins job of "
            "the ai-pins workflow (or python3 tools/python_pins.py on a machine with network access) and paste the JSON it prints into "
            "this file: no archive is built without a pinned, verified Python")
    python, release = doc["python"], doc["release"]
    if not isinstance(python, str) or not VERSION_RE.fullmatch(python):
        raise ReleaseError(f"{path}: python must be 'X.Y.Z', not {python!r}")
    if not isinstance(release, str) or not re.fullmatch(r"\d{8}", release):
        raise ReleaseError(f"{path}: release must be the 8 digits of the python-build-standalone tag (20250708), not {release!r}")
    pins = {}
    for suffix, triple in STANDALONE_TARGETS.items():
        entry = targets[suffix]
        if entry.get("target") != triple:
            raise ReleaseError(f"{path}: targets.{suffix}.target must be {triple}")
        file = STANDALONE_FILE.format(python=python, release=release, target=triple)
        if entry["file"] != file:
            raise ReleaseError(f"{path}: targets.{suffix}.file must be {file}, not {entry['file']!r}")
        if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise ReleaseError(f"{path}: targets.{suffix}.sha256 must be 64 lowercase hex digits")
        size = entry["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ReleaseError(f"{path}: targets.{suffix}.size must be a positive number of bytes")
        pins[suffix] = (file, entry["sha256"], size)
    return python, release, pins


def python_downloads(pins, python_pins):
    """[(file, sha256, url)] of every Python an archive carries, in the order of archive_names(): the python-build-standalone
    tarballs (Linux, macOS) and the embeddable zips (Windows). The files go into --python-dir under these names."""
    _, release, standalone = read_python_pins(python_pins)
    rows = [(file, digest, STANDALONE_URL.format(release=release, file=urllib.parse.quote(file)))
            for file, digest, _size in standalone.values()]
    version, builds = read_pins(pins)
    rows += [(builds[key][0], builds[key][1], PY_URL.format(version=version, file=builds[key][0])) for _, key in WINDOWS_ARCHS]
    return rows


def check_python_file(python_dir, file, digest, size, pin_file):
    """The bytes of python_dir/file once its size (when pinned) and SHA-256 are the pinned ones."""
    path = os.path.join(python_dir, file)
    if not os.path.isfile(path):
        raise ReleaseError(f"{path}: missing (tools/build_release.py --list-python says what to download)")
    data = read_bytes(path)
    if size is not None and len(data) != size:
        raise ReleaseError(f"{path}: {len(data)} bytes, {pin_file} pins {size}: refused")
    if sha256(data) != digest:
        raise ReleaseError(f"{path}: SHA-256 {sha256(data)}, {pin_file} pins {digest}: refused")
    return data


def git(root, *args):
    return subprocess.run(["git", "-C", root] + list(args), capture_output=True, check=True).stdout.decode("utf-8")


def is_checkout(root):
    """True when root is the top folder of a git checkout (not a folder inside some other repository)."""
    try:
        top = git(root, "rev-parse", "--show-toplevel").strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return os.path.normcase(os.path.realpath(top)) == os.path.normcase(os.path.realpath(root))


def source_files(root, skip=()):
    """-> sorted [(path with /, executable)]: what git tracks, or every file under root when it is not a checkout.
    The exec bit comes from git (a Windows checkout has none); without git, from the file (Windows: a #! line)."""
    files = {}
    if is_checkout(root):
        for entry in git(root, "ls-files", "-s", "-z").split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            mode = meta.split()[0]
            if mode in ("120000", "160000"):
                raise ReleaseError(f"{path}: a symbolic link or a submodule cannot be shipped")
            if os.path.isfile(os.path.join(root, path)):  # deleted but not committed yet: the status warning says so
                files[path] = mode == "100755"
        if git(root, "status", "--porcelain", "--untracked-files=no").strip():
            print("build_release: warning: uncommitted changes: these archives are not the ones of the last commit",
                  file=sys.stderr)
        return sorted(files.items())
    skip = {os.path.normcase(os.path.realpath(p)) for p in skip}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "dist")
                       and os.path.normcase(os.path.realpath(os.path.join(dirpath, d))) not in skip]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if name.endswith((".pyc", ".pyo")):
                continue
            if os.path.islink(full):
                raise ReleaseError(f"{full}: a symbolic link cannot be shipped")
            if os.name == "nt":
                executable = read_bytes(full)[:2] == b"#!"
            else:
                executable = bool(os.stat(full).st_mode & 0o111)
            files[os.path.relpath(full, root).replace(os.sep, "/")] = executable
    return sorted(files.items())


def source_epoch(root):
    """SOURCE_DATE_EPOCH, else the time of the last commit: the mtime of every entry."""
    value = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if value:
        if not value.isdigit():
            raise ReleaseError("SOURCE_DATE_EPOCH must be a whole number of seconds")
        return int(value)
    if is_checkout(root):
        return int(git(root, "log", "-1", "--format=%ct").strip())
    raise ReleaseError("not a git checkout: set SOURCE_DATE_EPOCH (the time every entry gets)")


# ---- what goes where ---------------------------------------------------------------------------------------------------

SHELL_SHEBANG = re.compile(rb"#![ \t]*(?:/usr/bin/env[ \t]+)?(?:\S*/)?(?:ba|da|z|k|a)?sh\b")


def is_shell(path, data):
    """A shell script: *.sh, or without extension a #!/bin/sh-like first line (a Python script with a #! line is not)."""
    ext = os.path.splitext(path.rsplit("/", 1)[-1])[1].lower()
    return ext in SHELL_EXT or (not ext and SHELL_SHEBANG.match(data[:128]) is not None)


def ships(path, data, system):
    """Does the archive for `system` (linux, macos, windows) carry this file?"""
    parts = path.split("/")
    if any(p.startswith(".") for p in parts) or parts[0] in DEV_DIRS or path in DEV_FILES:
        return False
    ext = os.path.splitext(parts[-1])[1].lower()
    if parts[0] == "systemd":
        return system == "linux"
    if parts[0] == "launchd" or parts[-1] == "install-macos.sh":
        return system == "macos"
    if ext in WINDOWS_EXT:
        return system == "windows"
    if parts[0] == "scripts" or is_shell(path, data):
        return system != "windows"
    return True


def eol(path, data):
    """Line endings as .gitattributes checks them out, whatever the checkout did (core.autocrlf on Windows)."""
    if path.lower().endswith(".cmd") or path.lower().endswith(".bat"):
        return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if is_shell(path, data) or (not os.path.splitext(path)[1] and data[:2] == b"#!"):  # a #! line ends in LF or fails
        return data.replace(b"\r\n", b"\n")
    return data


def layout(files, system, top, extra=()):
    """-> sorted [Entry] of the archive for `system`: the files of the repository that ship there, the `extra` entries (the
    Python), and a folder entry for every folder above them."""
    entries = {}
    for path, data, executable in files:
        if ships(path, data, system):
            full = f"{top}/{path}"
            entries[full] = Entry(full, eol(path, data), 0o755 if executable else 0o644, None)
    for entry in extra:
        entries[entry.path] = entry
    for path in list(entries):
        parts = path.split("/")
        for i in range(1, len(parts)):
            folder = "/".join(parts[:i])
            entries.setdefault(folder, Entry(folder, None, 0o755, None))
    return [entries[path] for path in sorted(entries, key=lambda p: p.split("/"))]


# ---- the Python of the Linux and macOS archives ------------------------------------------------------------------------

def link_inside(path, target):
    """May the symbolic link `path` (relative to the folder that holds python/) point at `target`? Only a relative path that
    stays inside python/: an absolute one, or one that climbs out of the tree, could be turned against whoever unpacks it."""
    if not target or "\0" in target or target.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", target):
        return False
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return joined == PYTHON_DIR or joined.startswith(PYTHON_DIR + "/")


def standalone_python(data, top, source="the tarball"):
    """-> [Entry] of a python-build-standalone install_only tarball (bytes of a .tar.gz with one folder, python/), as
    top/python/...: folders, files with their exec bit and their time, symbolic links as links. A hard link becomes a copy.
    Refused: a member outside python/, an absolute path or '..', a link that is absolute or leaves python/, an entry below a
    link, a device or a pipe, and a tree without python/bin/python3."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r:")  # unpacked once: reading it by member is cheap then
    except (OSError, EOFError, tarfile.TarError) as e:
        raise ReleaseError(f"{source}: not a .tar.gz ({e})")
    entries = {}
    with tar:
        try:
            members = tar.getmembers()
        except (OSError, EOFError, tarfile.TarError) as e:
            raise ReleaseError(f"{source}: unreadable ({e})")
        for m in members:
            parts = [p for p in m.name.split("/") if p not in ("", ".")]
            if not parts:
                continue
            if m.name.startswith("/") or "\0" in m.name or ".." in parts or parts[0] != PYTHON_DIR:
                raise ReleaseError(f"{source}: {m.name}: not inside {PYTHON_DIR}/")
            rel = "/".join(parts)
            path = f"{top}/{rel}"
            mtime = int(m.mtime)
            if m.isdir():
                entries[path] = Entry(path, None, 0o755, mtime)
            elif m.issym():
                if not link_inside(rel, m.linkname):
                    raise ReleaseError(f"{source}: {m.name} -> {m.linkname}: a link must be relative and stay inside {PYTHON_DIR}/")
                entries[path] = Entry(path, Link(m.linkname), 0o777, mtime)
            elif m.isfile() or m.islnk():
                try:
                    body = tar.extractfile(m).read()
                except (OSError, KeyError, AttributeError, tarfile.TarError) as e:
                    raise ReleaseError(f"{source}: {m.name}: unreadable ({e})")
                entries[path] = Entry(path, body, 0o755 if m.mode & 0o111 else 0o644, mtime)
            else:
                raise ReleaseError(f"{source}: {m.name}: a device or a pipe cannot be shipped")
    for path in entries:
        parent = path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if isinstance(getattr(entries.get(parent), "data", None), Link):
                raise ReleaseError(f"{source}: {path.split('/', 1)[1]}: below a symbolic link")
    if f"{top}/{PYTHON_DIR}/bin/python3" not in entries:
        raise ReleaseError(f"{source}: no {PYTHON_DIR}/bin/python3: not a python-build-standalone install_only tarball")
    return [entries[path] for path in sorted(entries)]


# ---- archives ----------------------------------------------------------------------------------------------------------

def tar_gz(entries, epoch):
    raw = io.BytesIO()
    # no file name and a fixed time in the gzip header (GzipFile otherwise writes the output name and now)
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=epoch) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path, data, mode, mtime in entries:
                info = tarfile.TarInfo(path)
                info.mtime = epoch if mtime is None else mtime
                info.mode, info.uid, info.gid, info.uname, info.gname = mode, 0, 0, "", ""
                if data is None:
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                elif isinstance(data, Link):
                    info.type, info.linkname = tarfile.SYMTYPE, str(data)
                    tar.addfile(info)
                else:
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def zip_bytes(entries, epoch):
    raw = io.BytesIO()
    when = time.gmtime(max(epoch, ZIP_EPOCH_MIN))[:6]
    with zipfile.ZipFile(raw, "w") as z:
        for path, data, mode, _mtime in entries:
            if isinstance(data, Link):
                raise ReleaseError(f"{path}: a symbolic link cannot be put in a zip")
            if data is None:
                info = zipfile.ZipInfo(path + "/", when)
                info.external_attr = (0o040000 | mode) << 16 | 0x10  # a folder, for Unix and for Windows tools
                data, level, info.compress_type = b"", None, zipfile.ZIP_STORED
            else:
                info = zipfile.ZipInfo(path, when)
                info.external_attr = (0o100000 | mode) << 16
                if path.endswith(".zip"):  # the embeddable Python: already compressed
                    level, info.compress_type = None, zipfile.ZIP_STORED
                else:
                    level, info.compress_type = 9, zipfile.ZIP_DEFLATED
            info.create_system = 3  # "Unix" whatever OS builds it (the default depends on it)
            z.writestr(info, data, compresslevel=level)
    return raw.getvalue()


def write_atomic(path, data):
    part = path + ".part"
    with open(part, "wb") as f:
        f.write(data)
    os.replace(part, path)


def build(version, out, root=ROOT, python_dir=None, pins=None, python_pins=None, epoch=None, log=print):
    """Writes the six archives and SHA256SUMS into `out`; -> the archive names. Every archive carries its Python, so
    `python_dir` (the folder with the files of `python_downloads`) is required and every file in it must be the pinned one.
    `pins`: the file with $PyVersion / $PyBuilds (default: install-windows.ps1 of `root`); `python_pins`: the JSON of the
    python-build-standalone tarballs (default: tools/python-pins.json of `root`). Nothing is written if a check fails."""
    if not VERSION_RE.fullmatch(version or ""):
        raise ReleaseError(f"--version {version!r}: X.Y.Z expected (the tag without its v)")
    found = read_version(root)
    if version != found:
        raise ReleaseError(f'--version {version} is not VERSION = "{found}" in src/nuc_config.py: refused')
    epoch = source_epoch(root) if epoch is None else epoch
    pins = pins or os.path.join(root, "install-windows.ps1")
    python_pins = python_pins or os.path.join(root, PINS_FILE)
    # the pins first: while a Python is not pinned nothing is built, whatever python_dir holds
    _, _, standalone = read_python_pins(python_pins)
    _, builds = read_pins(pins)
    if not python_dir:
        raise ReleaseError("--python-dir is required: every archive carries its Python "
                           "(tools/build_release.py --list-python says which files to download into it)")
    tarballs = {suffix: check_python_file(python_dir, file, digest, size, os.path.basename(python_pins))
                for suffix, (file, digest, size) in standalone.items()}
    zips = {suffix: (builds[key][0], check_python_file(python_dir, builds[key][0], builds[key][1], None, os.path.basename(pins)))
            for suffix, key in WINDOWS_ARCHS}
    files = [(path, read_bytes(os.path.join(root, path)), executable)
             for path, executable in source_files(root, skip=[out])]
    top = f"{PROJECT}-{version}"
    archives = []
    for suffix in STANDALONE_TARGETS:
        system = suffix.split("-")[0]
        python = standalone_python(tarballs.pop(suffix), top, source=standalone[suffix][0])
        archives.append((f"{top}-{suffix}.tar.gz", tar_gz(layout(files, system, top, extra=python), epoch)))
    for suffix, _ in WINDOWS_ARCHS:
        file, data = zips[suffix]
        python = [Entry(f"{top}/{PYTHON_DIR}/{file}", data, 0o644, None)]
        archives.append((f"{top}-windows-{suffix}.zip", zip_bytes(layout(files, "windows", top, extra=python), epoch)))
    assert [name for name, _ in archives] == archive_names(version)
    os.makedirs(out, exist_ok=True)
    for name, data in archives:
        write_atomic(os.path.join(out, name), data)
        log(f"{name}  {len(data)} bytes  sha256 {sha256(data)}")
    sums = "".join(f"{sha256(data)}  {name}\n" for name, data in sorted(archives))
    write_atomic(os.path.join(out, "SHA256SUMS"), sums.encode("ascii"))
    log(f"SHA256SUMS  {len(archives)} archives  (mtime {epoch}, {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(epoch))})")
    return [name for name, _ in archives]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", help="X.Y.Z, the tag without its v: must be VERSION in src/nuc_config.py")
    p.add_argument("--out", default="dist", help="output folder (default: dist)")
    p.add_argument("--python-dir", metavar="DIR",
                   help="folder with the Pythons the archives carry (the files --list-python names): required, every file is checked against its pin")
    p.add_argument("--pins", metavar="FILE", help="read $PyVersion / $PyBuilds from FILE instead of install-windows.ps1 (tests)")
    p.add_argument("--python-pins", metavar="FILE", help="read the python-build-standalone pins from FILE instead of tools/python-pins.json (tests)")
    p.add_argument("--root", default=ROOT, help="source tree (default: this repository)")
    p.add_argument("--list-python", action="store_true",
                   help="print 'file sha256 url' of each Python to download (4 python-build-standalone tarballs, 2 embeddable zips), then exit")
    a = p.parse_args(argv)
    try:
        pins = a.pins or os.path.join(a.root, "install-windows.ps1")
        python_pins = a.python_pins or os.path.join(a.root, PINS_FILE)
        if a.list_python:
            for line in python_downloads(pins, python_pins):
                print(" ".join(line))
            return 0
        if not a.version:
            p.error("--version is required")
        build(a.version, a.out, root=a.root, python_dir=a.python_dir, pins=pins, python_pins=python_pins)
    except (ReleaseError, OSError, subprocess.CalledProcessError) as e:
        print(f"build_release: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
