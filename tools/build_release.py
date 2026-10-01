#!/usr/bin/env python3
"""Builds the release archives of nuc-console (stdlib only, Python 3.8+); .github/workflows/release.yml publishes them.

    python3 tools/build_release.py --version X.Y.Z --out dist/ [--python-zips DIR]
    python3 tools/build_release.py --list-python     # the embeddable Pythons to download: "file sha256 url" per line

Creates in --out (each archive has one top folder, nuc-console-X.Y.Z/):
  nuc-console-X.Y.Z-linux.tar.gz        install.sh, src/, bin/, config/, systemd/, scripts/, docs/, README.md, ...
  nuc-console-X.Y.Z-macos.tar.gz        install.sh + install-macos.sh, src/, bin/, config/, launchd/, scripts/, docs/, ...
  nuc-console-X.Y.Z-windows-x64.zip     install-windows.cmd + .ps1, src/, bin/*.cmd, config/, docs/, ... and
  nuc-console-X.Y.Z-windows-arm64.zip   python/python-<v>-embed-<arch>.zip: install-windows.ps1 takes it from there,
                                        so the install needs no download (only with --python-zips)
  SHA256SUMS                            "<sha256>  <name>" of every archive: sha256sum -c SHA256SUMS

What goes in is computed, never listed by hand: every file git tracks (every file under --root when it is not a git
checkout), minus what is for development only (tests/, tools/, CONTRIBUTING.md, dotfiles such as .github/), and per
OS minus what belongs to another one: systemd/ is Linux only; launchd/ and install-macos.sh macOS only; *.cmd, *.bat
and *.ps1 Windows only; shell scripts (*.sh, extensionless files with a #!/bin/sh-like line) and scripts/ not on Windows.
A new file (a portable launcher, an updater) is shipped without touching this script.

Reproducible: sorted entries, every mtime = SOURCE_DATE_EPOCH (default: the time of the last commit), uid/gid 0 and
no user names, modes 0755/0644 from git's exec bit (not from the file system), gzip and zip headers without a name or
a time of their own. The same tree, version and Python zips give byte-identical archives (with the same zlib: CI
builds with Python 3.12 on Ubuntu). Line endings as .gitattributes checks them out, whatever the checkout did:
shell scripts and #! files LF, *.cmd CRLF.

Refuses: a --version other than VERSION in src/nuc_config.py, and a Python zip whose SHA-256 is not the one pinned
in install-windows.ps1 ($PyVersion / $PyBuilds: the one source of truth for the installer, this script and the
workflow; --pins reads another file, for the tests).
"""
import argparse
import ast
import gzip
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import time
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT = "nuc-console"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
PY_URL = "https://www.python.org/ftp/python/{version}/{file}"  # the URL install-windows.ps1 downloads from
WINDOWS_ARCHS = (("x64", "AMD64"), ("arm64", "ARM64"))  # release archive suffix, $PyBuilds key
PYTHON_DIR = "python"  # inside the Windows archives: install-windows.ps1 looks for the embeddable zip here first
DEV_DIRS, DEV_FILES = ("tests", "tools"), ("CONTRIBUTING.md",)  # never shipped (nor any dotfile: .github/, .gitignore)
WINDOWS_EXT, SHELL_EXT = (".cmd", ".bat", ".ps1"), (".sh",)
ZIP_EPOCH_MIN = 315532800  # 1980-01-01 00:00 UTC: the oldest time a zip entry can hold


class ReleaseError(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


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
    install-windows.ps1 (or a file written like it)."""
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


def python_downloads(pins):
    """[(file, sha256, url)] of the embeddable Pythons pinned in `pins`, one per Windows archive."""
    version, builds = read_pins(pins)
    return [(builds[key][0], builds[key][1], PY_URL.format(version=version, file=builds[key][0])) for _, key in WINDOWS_ARCHS]


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
    """-> sorted [(archive path, data or None for a folder, mode)] of the archive for `system`."""
    entries = {}
    for path, data, executable in files:
        if ships(path, data, system):
            entries[f"{top}/{path}"] = (eol(path, data), 0o755 if executable else 0o644)
    for path, data in extra:
        entries[f"{top}/{path}"] = (data, 0o644)
    for path in list(entries):
        parts = path.split("/")
        for i in range(1, len(parts)):
            entries.setdefault("/".join(parts[:i]), (None, 0o755))
    return [(path,) + entries[path] for path in sorted(entries, key=lambda p: p.split("/"))]


# ---- archives ----------------------------------------------------------------------------------------------------------

def tar_gz(entries, epoch):
    raw = io.BytesIO()
    # no file name and a fixed time in the gzip header (GzipFile otherwise writes the output name and now)
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=epoch) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path, data, mode in entries:
                info = tarfile.TarInfo(path)
                info.mtime, info.mode, info.uid, info.gid, info.uname, info.gname = epoch, mode, 0, 0, "", ""
                if data is None:
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                else:
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def zip_bytes(entries, epoch):
    raw = io.BytesIO()
    when = time.gmtime(max(epoch, ZIP_EPOCH_MIN))[:6]
    with zipfile.ZipFile(raw, "w") as z:
        for path, data, mode in entries:
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


def build(version, out, root=ROOT, python_zips=None, pins=None, epoch=None, log=print):
    """Writes the archives and SHA256SUMS into `out`; -> the archive names. Nothing is written if a check fails."""
    if not VERSION_RE.fullmatch(version or ""):
        raise ReleaseError(f"--version {version!r}: X.Y.Z expected (the tag without its v)")
    found = read_version(root)
    if version != found:
        raise ReleaseError(f'--version {version} is not VERSION = "{found}" in src/nuc_config.py: refused')
    epoch = source_epoch(root) if epoch is None else epoch
    pythons = []
    if python_zips is not None:
        pins = pins or os.path.join(root, "install-windows.ps1")
        for (suffix, _), (file, digest, _url) in zip(WINDOWS_ARCHS, python_downloads(pins)):
            path = os.path.join(python_zips, file)
            if not os.path.isfile(path):
                raise ReleaseError(f"{path}: missing (tools/build_release.py --list-python says what to download)")
            data = read_bytes(path)
            if sha256(data) != digest:
                raise ReleaseError(f"{path}: SHA-256 {sha256(data)}, {os.path.basename(pins)} pins {digest}: refused")
            pythons.append((suffix, file, data))
    files = [(path, read_bytes(os.path.join(root, path)), executable)
             for path, executable in source_files(root, skip=[out])]
    top = f"{PROJECT}-{version}"
    archives = [(f"{top}-linux.tar.gz", tar_gz(layout(files, "linux", top), epoch)),
                (f"{top}-macos.tar.gz", tar_gz(layout(files, "macos", top), epoch))]
    for suffix, file, data in pythons:
        archives.append((f"{top}-windows-{suffix}.zip",
                         zip_bytes(layout(files, "windows", top, extra=[(f"{PYTHON_DIR}/{file}", data)]), epoch)))
    if not pythons:
        log("windows archives skipped: no --python-zips")
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
    p.add_argument("--python-zips", metavar="DIR",
                   help="folder with the embeddable Python zips pinned in install-windows.ps1: builds the Windows archives")
    p.add_argument("--pins", metavar="FILE", help="read $PyVersion / $PyBuilds from FILE instead of install-windows.ps1 (tests)")
    p.add_argument("--root", default=ROOT, help="source tree (default: this repository)")
    p.add_argument("--list-python", action="store_true",
                   help="print 'file sha256 url' of each embeddable Python to download, then exit")
    a = p.parse_args(argv)
    try:
        if a.list_python:
            for line in python_downloads(a.pins or os.path.join(a.root, "install-windows.ps1")):
                print(" ".join(line))
            return 0
        if not a.version:
            p.error("--version is required")
        build(a.version, a.out, root=a.root, python_zips=a.python_zips, pins=a.pins)
    except (ReleaseError, OSError, subprocess.CalledProcessError) as e:
        print(f"build_release: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
