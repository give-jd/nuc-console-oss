#!/usr/bin/env python3
"""Fills desktop/src-tauri/core, the core the desktop app carries (standard library only, Python 3.8+).

    python3 tools/desktop_core.py --archive dist/nuc-console-X.Y.Z-linux-x86_64.tar.gz    # what a release ships: its Python included
    python3 tools/desktop_core.py --checkout [--system linux|macos|windows]              # development: this checkout, no Python of its own
    python3 tools/desktop_core.py --collect target/release/bundle --target T --version V --bundles deb,rpm --out packages

The desktop app (desktop/, docs/DESKTOP.md) is a window around the portable run: it starts core/run.sh (core\\run.ps1 on Windows)
with NUC_CONSOLE_DATA in the user's folder, and the bundler copies core/ into every package as it is. With --archive the core is
the release archive of that system and processor, unpacked: the files, and the Python it carries. Two things change on the way, so
that the core runs from a folder nobody may write to (Program Files, /usr/lib, an .app):
  - Linux and macOS: the symbolic links of the Python become copies of what they point at (the bundlers copy files, not links);
  - Windows: the embeddable Python is unpacked into python\\ the way run.ps1 does it on its first run (the ._pth file, the stamp
    that names the zip and its SHA-256), after its SHA-256 is checked against the pin of install-windows.ps1, and the zip goes.
With --checkout it is the files this checkout ships for that system (tools/build_release.py decides which), and the app runs them
with the Python of the machine, as a clone does.

The archive is read the way tools/build_release.py writes it, and refused when it is not: one top folder, nothing outside it, no
absolute path, no '..', no device, and links only inside python/ that stay there. core/ is replaced as a whole, and only when it is
one this script made (core.json says what it holds) or empty.

--collect takes the packages the bundler made for a target (exactly one of each kind --bundles names: .deb, .rpm, .AppImage, .dmg,
.msi, the NSIS setup .exe) and copies them as nuc-console-desktop-<version>-<target>.<ext>, next to the release archives' names; a
package whose name does not carry the version (the bundler was not given it) is refused."""
import argparse
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_release as br  # noqa: E402

ROOT = br.ROOT
OUT = os.path.join(ROOT, "desktop", "src-tauri", "core")
MARK = "core.json"  # what the folder holds: written last, read before core/ is replaced
ARCHIVE_RE = re.compile(r"nuc-console-(\d+\.\d+\.\d+)-(linux-x86_64|linux-arm64|macos-arm64|macos-x86_64|windows-x64|windows-arm64)\.(tar\.gz|zip)$")
WINDOWS_PIN_KEYS = dict(br.WINDOWS_ARCHS)  # archive suffix -> the $PyBuilds key of install-windows.ps1
LINK_DEPTH = 8
TARGETS = ("linux-x86_64", "linux-arm64", "macos-arm64", "macos-x86_64", "windows-x64", "windows-arm64")
# a kind of package -> (the folder of the bundler's output it is in, the end of its file name there, the end of its published name)
PACKAGES = {"deb": ("deb", ".deb", ".deb"), "rpm": ("rpm", ".rpm", ".rpm"), "appimage": ("appimage", ".AppImage", ".AppImage"),
            "dmg": ("dmg", ".dmg", ".dmg"), "msi": ("msi", ".msi", ".msi"), "nsis": ("nsis", "-setup.exe", "-setup.exe")}
KINDS = {"linux": ("deb", "rpm", "appimage"), "macos": ("app", "dmg"), "windows": ("msi", "nsis")}  # what each system can build


class CoreError(Exception):
    pass


def system_of(suffix):
    return suffix.split("-")[0]


def current_system():
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


# ---- reading an archive ------------------------------------------------------------------------------------------------

def _rel(name, top, source):
    """The path of a member below the top folder ('' for the top folder itself), or CoreError."""
    if not name or "\0" in name or "\\" in name or name.startswith("/") or re.match(r"[A-Za-z]:", name):
        raise CoreError(f"{source}: {name!r}: not a relative path")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise CoreError(f"{source}: {name}: '..' in a path")
    if not parts or parts[0] != top:
        raise CoreError(f"{source}: {name}: not inside {top}/")
    return "/".join(parts[1:])


def read_tar(data, top, source):
    """-> {path: bytes | None (a folder) | ("link", target)} of a .tar.gz, paths below `top`, each mode's exec bit as a set."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r:")
    except (OSError, EOFError, tarfile.TarError) as e:
        raise CoreError(f"{source}: not a .tar.gz ({e})")
    entries, execs = {}, set()
    with tar:
        for m in tar.getmembers():
            rel = _rel(m.name, top, source)
            if not rel:
                continue
            if m.isdir():
                entries[rel] = None
            elif m.issym():
                if not br.link_inside(rel, m.linkname):
                    raise CoreError(f"{source}: {m.name} -> {m.linkname}: a link must be relative and stay inside {br.PYTHON_DIR}/")
                entries[rel] = ("link", m.linkname)
            elif m.isfile():
                entries[rel] = tar.extractfile(m).read()
                if m.mode & 0o111:
                    execs.add(rel)
            else:
                raise CoreError(f"{source}: {m.name}: a hard link, a device or a pipe is not what the release build writes")
    return entries, execs


def read_zip(data, top, source):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise CoreError(f"{source}: not a zip ({e})")
    entries = {}
    with z:
        for info in z.infolist():
            kind = (info.external_attr >> 16) & 0o170000
            if kind not in (0, 0o040000, 0o100000):
                raise CoreError(f"{source}: {info.filename}: a link or a device is not what the release build writes")
            rel = _rel(info.filename.rstrip("/"), top, source)
            if not rel:
                continue
            entries[rel] = None if info.is_dir() else z.read(info)
    return entries, set()


def resolve(entries, path, source):
    """The bytes a link of the Python points at (links to links followed), or CoreError: a copy replaces it."""
    seen = path
    for _ in range(LINK_DEPTH):
        target = entries.get(path)
        if not isinstance(target, tuple):
            break
        path = posixpath.normpath(posixpath.join(posixpath.dirname(path), target[1]))
    else:
        raise CoreError(f"{source}: {seen}: too many links")
    data = entries.get(path)
    if not isinstance(data, bytes):
        raise CoreError(f"{source}: {seen}: points at {path}, which is {'a folder' if path in entries else 'not in the archive'}")
    return data, path


# ---- the Windows Python, as run.ps1 unpacks it ---------------------------------------------------------------------------

def unpack_windows_python(entries, suffix, source):
    """Unpack python/python-<v>-embed-<arch>.zip into python/ as run.ps1 does on its first run: its files, the ._pth file that keeps
    that Python to its own library and ..\\src, and nuc-console-python.txt, the stamp run.ps1 compares with the pin before it
    reuses the folder. The zip is checked against the pin of install-windows.ps1 (the one the archive carries) and goes."""
    ps1 = entries.get("install-windows.ps1")
    if not isinstance(ps1, bytes):
        raise CoreError(f"{source}: no install-windows.ps1: the pin of its Python is there")
    version, builds = _read_pins_bytes(ps1, source)
    file, digest = builds[WINDOWS_PIN_KEYS[suffix.split("-", 1)[1]]]
    zipped = entries.pop(f"{br.PYTHON_DIR}/{file}", None)
    if not isinstance(zipped, bytes):
        raise CoreError(f"{source}: no {br.PYTHON_DIR}/{file}: the Python install-windows.ps1 pins (Python {version})")
    if hashlib.sha256(zipped).hexdigest() != digest:
        raise CoreError(f"{source}: {br.PYTHON_DIR}/{file}: SHA-256 {hashlib.sha256(zipped).hexdigest()}, install-windows.ps1 pins {digest}: refused")
    names = []
    for rel, body in read_zip_flat(zipped, file):
        entries[f"{br.PYTHON_DIR}/{rel}"] = body
        names.append(rel)
    pth = [n for n in names if re.fullmatch(r"python3\d*\._pth", n)]
    stdlib = [n for n in names if re.fullmatch(r"python3\d*\.zip", n)]
    if len(pth) != 1 or len(stdlib) != 1 or "python.exe" not in names:
        raise CoreError(f"{source}: {file}: not an embeddable Python (python.exe, python3XX.zip and python3XX._pth expected)")
    entries[f"{br.PYTHON_DIR}/{pth[0]}"] = f"{stdlib[0]}\r\n.\r\n..\\src\r\n".encode("ascii")
    entries[f"{br.PYTHON_DIR}/nuc-console-python.txt"] = f"{file} {digest}\r\n".encode("ascii")
    return file


def _read_pins_bytes(data, source):
    """build_release.read_pins of install-windows.ps1 as the archive carries it (it reads a file)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "install-windows.ps1")
        with open(path, "wb") as f:
            f.write(data)
        try:
            return br.read_pins(path)
        except br.ReleaseError as e:
            raise CoreError(f"{source}: install-windows.ps1: {e}")


def read_zip_flat(data, source):
    """[(path, bytes)] of the files of a zip whose files are at its top (the embeddable Python), checked like the archive."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise CoreError(f"{source}: not a zip ({e})")
    out = []
    with z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir():
                continue
            parts = name.split("/")
            if "\0" in name or "\\" in name or name.startswith("/") or ".." in parts or re.match(r"[A-Za-z]:", name) or "" in parts:
                raise CoreError(f"{source}: {name!r}: not a plain relative path")
            out.append((name, z.read(info)))
    return out


# ---- what core/ holds ----------------------------------------------------------------------------------------------------

def from_archive(path):
    """-> (files, info): files is {path: (bytes, executable)} of the core of a release archive, info what core.json says."""
    name = os.path.basename(path)
    m = ARCHIVE_RE.fullmatch(name)
    if not m:
        raise CoreError(f"{name}: not the name of a release archive (nuc-console-X.Y.Z-<system>-<processor>.tar.gz or .zip)")
    version, suffix, ext = m.groups()
    data = br.read_bytes(path)
    top = f"{br.PROJECT}-{version}"
    entries, execs = (read_tar if ext == "tar.gz" else read_zip)(data, top, name)
    python = None
    if system_of(suffix) == "windows":
        python = unpack_windows_python(entries, suffix, name)
    files = {}
    for rel, body in entries.items():
        if body is None:
            continue
        if isinstance(body, tuple):
            body, target = resolve(entries, rel, name)
            files[rel] = (body, target in execs)
        else:
            files[rel] = (body, rel in execs)
    for need in (("run.ps1", "src/collector.py") if system_of(suffix) == "windows" else ("run.sh", "src/collector.py")):
        if need not in files:
            raise CoreError(f"{name}: no {need}: not a nuc-console archive")
    if system_of(suffix) != "windows" and f"{br.PYTHON_DIR}/bin/python3" not in files:
        raise CoreError(f"{name}: no {br.PYTHON_DIR}/bin/python3: an archive without its Python")
    info = {"version": version, "system": system_of(suffix), "target": suffix, "from": name,
            "sha256": hashlib.sha256(data).hexdigest(), "python": python or br.PYTHON_DIR + "/bin/python3"}
    return files, info


def from_checkout(system, root=ROOT):
    """-> (files, info) of the files this checkout ships for `system`: no Python, the app uses the machine's."""
    if system not in ("linux", "macos", "windows"):
        raise CoreError(f"--system {system}: linux, macos or windows")
    files = {}
    for path, executable in br.source_files(root):
        data = br.read_bytes(os.path.join(root, path))
        if br.ships(path, data, system):
            files[path] = (br.eol(path, data), executable)
    return files, {"version": br.read_version(root), "system": system, "target": None, "from": "checkout", "sha256": None, "python": None}


def is_ours(out):
    """May `out` be replaced? Only an empty folder, or one this script made."""
    if not os.path.lexists(out):
        return True
    if os.path.islink(out) or not os.path.isdir(out):
        return False
    names = os.listdir(out)
    if not names:
        return True
    try:
        with open(os.path.join(out, MARK), encoding="utf-8") as f:
            return isinstance(json.load(f).get("system"), str)
    except (OSError, ValueError, AttributeError):
        return False


def write(files, info, out=OUT, log=print):
    """Replace `out` with `files` and core.json: written next to it first, then moved into place."""
    out = os.path.abspath(out)
    if not is_ours(out):
        raise CoreError(f"{out}: not a folder this script made (no {MARK}): it is not replaced")
    part = out + ".part"
    if os.path.lexists(part):
        shutil.rmtree(part)
    os.makedirs(part)
    size = 0
    for rel in sorted(files):
        body, executable = files[rel]
        dest = os.path.join(part, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(body)
        os.chmod(dest, 0o755 if executable else 0o644)
        size += len(body)
    with open(os.path.join(part, MARK), "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(info, files=len(files)), f, indent=1, sort_keys=True)
        f.write("\n")
    if os.path.lexists(out):
        shutil.rmtree(out)
    os.replace(part, out)
    log(f"{out}: {len(files)} files, {size // 1024} KB ({info['from']}, {info['system']}, version {info['version']})")
    return out


def collect(bundle_dir, target, version, bundles, out, log=print):
    """Copy the packages of `target` the bundler wrote under bundle_dir into `out` as nuc-console-desktop-<version>-<target>.<ext>:
    one of each kind `bundles` names ("app" is the folder the .dmg carries: nothing to copy). -> the names written."""
    if target not in TARGETS:
        raise CoreError(f"--target {target}: one of {', '.join(TARGETS)}")
    if not br.VERSION_RE.fullmatch(version or ""):
        raise CoreError(f"--version {version!r}: X.Y.Z expected")
    kinds = [k.strip() for k in bundles.split(",") if k.strip()]
    wrong = [k for k in kinds if k not in KINDS[system_of(target)]]
    if not kinds or wrong:
        raise CoreError(f"--bundles {bundles!r}: {system_of(target)} builds {', '.join(KINDS[system_of(target)])}")
    names = []
    os.makedirs(out, exist_ok=True)
    for kind in kinds:
        if kind == "app":
            continue
        folder, end, published = PACKAGES[kind]
        where = os.path.join(bundle_dir, folder)
        found = sorted(n for n in (os.listdir(where) if os.path.isdir(where) else []) if n.endswith(end))
        if len(found) != 1:
            raise CoreError(f"{where}: {len(found)} files ending in {end}, one expected")
        if version not in found[0]:
            raise CoreError(f"{found[0]}: not the package of version {version} (was the bundler given --config version.json?)")
        name = f"{br.PROJECT}-desktop-{version}-{target}{published}"
        shutil.copyfile(os.path.join(where, found[0]), os.path.join(out, name))
        log(f"{name}  <-  {folder}/{found[0]}")
        names.append(name)
    return names


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--archive", metavar="FILE", help="a release archive (tools/build_release.py): the core a package carries")
    src.add_argument("--checkout", action="store_true", help="the files of this checkout, without a Python (for development)")
    src.add_argument("--collect", metavar="DIR", help="the bundler's output folder (target/release/bundle): copy its packages to --out")
    p.add_argument("--system", default=current_system(), help="with --checkout: linux, macos or windows (default: this one)")
    p.add_argument("--target", help="with --collect: the target the packages are for (linux-x86_64 ... windows-arm64)")
    p.add_argument("--version", help="with --collect: X.Y.Z, the version the packages must carry")
    p.add_argument("--bundles", default="", help="with --collect: the kinds the bundler was asked for (deb,rpm,appimage | app,dmg | msi,nsis)")
    p.add_argument("--out", help="the folder to fill (default: desktop/src-tauri/core; with --collect: packages)")
    a = p.parse_args(argv)
    try:
        if a.collect:
            collect(a.collect, a.target, a.version, a.bundles, a.out or "packages")
            return 0
        files, info = from_archive(a.archive) if a.archive else from_checkout(a.system)
        write(files, info, a.out or OUT)
    except (CoreError, br.ReleaseError, OSError) as e:
        print(f"desktop_core: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
