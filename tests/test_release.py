"""tools/build_release.py: the release archives (what is in them, byte-for-byte reproducible, SHA256SUMS, what is refused).

Runs on every OS and downloads nothing: every archive carries a Python, and the tests build them with small fakes whose hashes
are written to fake pin files: "Python zips" for Windows (--pins, a file like install-windows.ps1) and fake python-build-standalone
tarballs for Linux and macOS (--python-pins, a file like tools/python-pins.json). The real pin file may still hold null (nothing
is pinned until the python-pins job of the ai-pins workflow has been run): then the real build is refused, which is tested too.
"""
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tarfile
import tempfile
import time
import unittest
import zipfile
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "tools", "build_release.py")
_spec = importlib.util.spec_from_file_location("build_release", SCRIPT)
br = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(br)

EPOCH = 1700000000  # 2023-11-14 22:13:20 UTC
FAKE_PY = "3.99.1"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)


def repo_version():
    return br.read_version(ROOT)


FAKE_RELEASE = "20991231"  # the tag of a fake python-build-standalone release
PBS_MTIME = 1500000000  # what the members of the fake tarballs carry: it must survive into the archives
TOPS = ("linux-x86_64", "linux-arm64", "macos-arm64", "macos-x86_64")


def fake_tarball(members, top="python"):
    """-> bytes of a .tar.gz; members = [(name, kind, content/target, mode)] with kind one of file, dir, sym, hard, dev."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as t:
        for name, kind, content, mode in members:
            info = tarfile.TarInfo(name)
            info.mtime, info.mode, info.uid, info.gid, info.uname, info.gname = PBS_MTIME, mode, 1234, 5678, "someone", "somegroup"
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                t.addfile(info)
            elif kind in ("sym", "hard"):
                info.type, info.linkname = (tarfile.SYMTYPE if kind == "sym" else tarfile.LNKTYPE), content
                t.addfile(info)
            elif kind == "dev":
                info.type, info.devmajor, info.devminor = tarfile.CHRTYPE, 1, 3
                t.addfile(info)
            else:
                info.size = len(content)
                t.addfile(info, io.BytesIO(content))
    return raw.getvalue()


def fake_python_tree(label, version=FAKE_PY):
    """The members of a small fake `install_only` Python: what the real one has in kind (an exec file, links, folders, a library)."""
    minor = ".".join(version.split(".")[:2])
    return [
        ("python", "dir", None, 0o755),
        ("python/bin", "dir", None, 0o755),
        ("python/bin/python3.%s" % minor.split(".")[1], "file", ("#!/bin/sh\n# fake python for %s\n" % label).encode(), 0o755),
        ("python/bin/python3", "sym", "python3.%s" % minor.split(".")[1], 0o777),
        ("python/bin/python", "sym", "python3", 0o777),
        ("python/bin/pip3", "file", b"#!/bin/sh\necho pip\n", 0o755),
        ("python/lib", "dir", None, 0o755),
        ("python/lib/libpython%s.so.1.0" % minor, "file", b"\x7fELF fake library for " + label.encode(), 0o755),
        ("python/lib/libpython%s.so" % minor, "sym", "libpython%s.so.1.0" % minor, 0o777),
        ("python/lib/python%s" % minor, "dir", None, 0o755),
        ("python/lib/python%s/os.py" % minor, "file", b"# os of " + label.encode() + b"\n", 0o644),
        ("python/lib/python%s/__pycache__" % minor, "dir", None, 0o755),
        ("python/lib/python%s/__pycache__/os.cpython-%s.pyc" % (minor, minor.replace(".", "")), "file", b"pyc " + label.encode(), 0o644),
        ("python/lib/python%s/empty" % minor, "dir", None, 0o755),
        ("python/include", "dir", None, 0o755),
        ("python/include/Python.h", "file", b"/* header */\n", 0o600),
    ]


def standalone_pins(folder, tarballs, version=FAKE_PY, release=FAKE_RELEASE, **override):
    """Writes the tarballs {suffix: bytes} into folder under their pinned names and a pin file for them; -> pin file path.
    override: suffix -> dict of the fields of its entry to change (a wrong hash, a wrong size...)."""
    targets = {}
    for suffix, triple in br.STANDALONE_TARGETS.items():
        file = "cpython-%s+%s-%s-install_only_stripped.tar.gz" % (version, release, triple)
        data = tarballs[suffix]
        write(os.path.join(folder, file), data)
        targets[suffix] = {"target": triple, "file": file, "sha256": sha256(data), "size": len(data)}
        targets[suffix].update(override.get(suffix, {}))
    path = os.path.join(folder, "python-pins.json")
    write(path, json.dumps({"_comment": "fake", "python": version, "release": release, "targets": targets}, indent=1))
    return path


def fake_standalone(folder, version=FAKE_PY, release=FAKE_RELEASE, **override):
    """Four small fake python-build-standalone tarballs and their pin file; -> pin file path."""
    return standalone_pins(folder, {suffix: fake_tarball(fake_python_tree(suffix, version)) for suffix in br.STANDALONE_TARGETS},
                           version, release, **override)


def fake_pythons(folder, version=FAKE_PY):
    """Writes everything an archive carries into folder: two small fake embeddable Pythons (Windows) and four fake
    python-build-standalone tarballs (Linux, macOS), and a pin file for each kind; -> (windows pin file, standalone pin file)."""
    pins = ["#Requires -Version 5.1", "$ErrorActionPreference = 'Stop'", "", "$PyVersion = '%s'" % version, "$PyBuilds = @{  # fake"]
    for key in ("AMD64", "ARM64"):
        data = ("PK fake embeddable python %s %s\n" % (version, key)).encode("ascii") * 20
        write(os.path.join(folder, "python-%s-embed-%s.zip" % (version, key.lower())), data)
        pins.append("    '%s' = @{ file = \"python-$PyVersion-embed-%s.zip\"; sha256 = '%s' }" % (key, key.lower(), sha256(data)))
    pins += ["}", "$Dest = 'whatever follows'", ""]
    path = os.path.join(folder, "pins.ps1")
    write(path, "\n".join(pins))
    return path, fake_standalone(folder, version)


def tar_members(path):
    with tarfile.open(path, "r:gz") as t:
        return {m.name: (m, t.extractfile(m).read() if m.isfile() else None) for m in t.getmembers()}


def zip_members(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename.rstrip("/"): (i, z.read(i) if not i.filename.endswith("/") else None) for i in z.infolist()}


def lf(data):
    return data.replace(b"\r\n", b"\n")


def mode_of(member):
    return member.mode & 0o777 if isinstance(member, tarfile.TarInfo) else (member.external_attr >> 16) & 0o777


class TempDirCase(unittest.TestCase):
    def tmp(self):
        d = tempfile.mkdtemp(prefix="nuc-release-")
        self.addCleanup(shutil.rmtree, d, True)
        return d


# ---- the pins of the real installer -------------------------------------------------------------------------------------

class RealPins(unittest.TestCase):
    def test_both_architectures_are_pinned_with_a_sha256(self):
        version, builds = br.read_pins(os.path.join(ROOT, "install-windows.ps1"))
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(sorted(builds), ["AMD64", "ARM64"])
        for key, (file, digest) in builds.items():
            self.assertEqual(file, "python-%s-embed-%s.zip" % (version, key.lower()))
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotEqual(builds["AMD64"][1], builds["ARM64"][1])

    def test_the_pins_are_the_ones_the_installer_downloads_with(self):
        ps1 = read(os.path.join(ROOT, "install-windows.ps1")).decode("utf-8")
        # one source of truth: the URL the script gives to the workflow is the one the installer builds
        self.assertIn('https://www.python.org/ftp/python/$PyVersion/$($build.file)', ps1)
        self.assertEqual(br.PY_URL, "https://www.python.org/ftp/python/{version}/{file}")
        with tempfile.TemporaryDirectory() as d:
            _, standalone = fake_pythons(d)
            rows = br.python_downloads(os.path.join(ROOT, "install-windows.ps1"), standalone)
        self.assertEqual(len(rows), 6)
        for file, digest, url in rows[4:]:  # the Windows ones come last
            self.assertTrue(url.startswith("https://www.python.org/ftp/python/"), url)
            self.assertTrue(url.endswith("/" + file), url)

    def test_list_python_prints_file_sha_url_of_all_six(self):
        with tempfile.TemporaryDirectory() as d:
            windows, standalone = fake_pythons(d)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(br.main(["--list-python", "--pins", windows, "--python-pins", standalone]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 6)
        for line, suffix in zip(lines[:4], br.STANDALONE_TARGETS):  # in the order of the archives
            file, digest, url = line.split(" ")
            self.assertEqual(file, "cpython-%s+%s-%s-install_only_stripped.tar.gz" % (FAKE_PY, FAKE_RELEASE, br.STANDALONE_TARGETS[suffix]))
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            # a + in a file name is %2B in the URL: the one GitHub serves
            self.assertEqual(url, "https://github.com/astral-sh/python-build-standalone/releases/download/%s/%s" % (FAKE_RELEASE, file.replace("+", "%2B")))
        for line in lines[4:]:
            file, digest, url = line.split(" ")
            self.assertRegex(file, r"^python-\d+\.\d+\.\d+-embed-(amd64|arm64)\.zip$")
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertTrue(url.startswith("https://www.python.org/"))

    def test_list_python_with_the_pin_file_of_the_repository(self):
        """Not pinned yet (null): it refuses, and says how to fill it. Pinned: six lines. Never anything in between."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = br.main(["--list-python"])
        if rc == 0:
            self.assertEqual(len(out.getvalue().splitlines()), 6)
        else:
            self.assertEqual((rc, out.getvalue()), (1, ""))
            self.assertIn("not pinned yet", err.getvalue())
            self.assertIn("python-pins", err.getvalue())

    def test_the_windows_archives_carry_python_where_the_installer_looks_first(self):
        ps1 = read(os.path.join(ROOT, "install-windows.ps1")).decode("utf-8")
        self.assertIn("(Join-Path $Here '%s')" % br.PYTHON_DIR, ps1)

    def test_version_is_read_without_importing(self):
        sys.path.insert(0, os.path.join(ROOT, "src"))
        try:
            import nuc_config
        finally:
            sys.path.remove(os.path.join(ROOT, "src"))
        self.assertEqual(repo_version(), nuc_config.VERSION)
        self.assertRegex(nuc_config.VERSION, r"^\d+\.\d+\.\d+$")


class PinParsing(TempDirCase):
    def pins(self, text):
        path = os.path.join(self.tmp(), "pins.ps1")
        write(path, text)
        return path

    def test_a_missing_architecture_is_refused(self):
        p = self.pins("$PyVersion = '3.99.1'\n$PyBuilds = @{\n    'AMD64' = @{ file = \"python-$PyVersion-embed-amd64.zip\"; sha256 = '%s' }\n}\n" % ("a" * 64))
        with self.assertRaisesRegex(br.ReleaseError, "ARM64"):
            br.read_pins(p)

    def test_a_short_or_uppercase_hash_is_refused(self):
        for digest in ("a" * 63, "A" * 64):
            p = self.pins("$PyVersion = '3.99.1'\n$PyBuilds = @{\n    'AMD64' = @{ file = \"python-$PyVersion-embed-amd64.zip\"; sha256 = '%s' }\n}\n" % digest)
            with self.assertRaisesRegex(br.ReleaseError, "64 lowercase hex"):
                br.read_pins(p)

    def test_a_file_name_that_is_not_the_embeddable_zip_is_refused(self):
        p = self.pins("$PyVersion = '3.99.1'\n$PyBuilds = @{\n    'AMD64' = @{ file = \"evil.zip\"; sha256 = '%s' }\n}\n" % ("a" * 64))
        with self.assertRaisesRegex(br.ReleaseError, "unexpected file name"):
            br.read_pins(p)

    def test_no_python_version_is_refused(self):
        with self.assertRaisesRegex(br.ReleaseError, "PyVersion"):
            br.read_pins(self.pins("$PyBuilds = @{\n}\n"))


class RealPythonPins(unittest.TestCase):
    """tools/python-pins.json, the pin file of the repository: placeholders (null) until the python-pins job of the ai-pins workflow
    has read the real values; once somebody pastes them it must be complete and consistent, never half filled."""

    def load(self):
        with open(os.path.join(ROOT, "tools", "python-pins.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_it_names_the_four_targets_and_says_what_it_is(self):
        doc = self.load()
        self.assertEqual(sorted(doc["targets"]), sorted(br.STANDALONE_TARGETS))
        for suffix, triple in br.STANDALONE_TARGETS.items():
            self.assertEqual(doc["targets"][suffix]["target"], triple)
        self.assertEqual(sorted(k for k in doc if not k.startswith("_")), ["python", "release", "targets"])
        self.assertIn("NOT PINNED YET", doc["_comment"])
        self.assertIn("python-pins", doc["_comment"])

    def test_either_not_pinned_at_all_or_pinned_completely(self):
        doc = self.load()
        values = [doc["python"], doc["release"]] + [t[k] for t in doc["targets"].values() for k in ("file", "sha256", "size")]
        if all(v is None for v in values):  # the placeholders: the build refuses, tested below
            with self.assertRaisesRegex(br.ReleaseError, "not pinned yet"):
                br.read_python_pins(os.path.join(ROOT, "tools", "python-pins.json"))
            return
        python, release, pins = br.read_python_pins(os.path.join(ROOT, "tools", "python-pins.json"))  # raises on anything half filled
        self.assertEqual(sorted(pins), sorted(br.STANDALONE_TARGETS))
        self.assertRegex(python, r"^3\.(12|13)\.\d+$")  # what the python-pins job offers: 3.13.x, or 3.12.x
        for suffix, (file, digest, size) in pins.items():
            self.assertEqual(len({d for _f, d, _s in pins.values()}), 4, "four different files")
            self.assertTrue(file.startswith("cpython-%s+%s-" % (python, release)), suffix)

    def test_the_comment_is_the_one_the_pins_script_writes(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        try:
            import python_pins
        finally:
            sys.path.remove(os.path.join(ROOT, "tools"))
        self.assertEqual(self.load()["_comment"], python_pins.COMMENT)

    def test_the_build_refuses_while_nothing_is_pinned_and_writes_nothing(self):
        """The refusal comes first: whatever --python-dir holds, and even with a Python dir of fakes."""
        if not all(t["sha256"] is None for t in self.load()["targets"].values()):
            self.skipTest("the Pythons are pinned")
        with tempfile.TemporaryDirectory() as d:
            windows, _ = fake_pythons(os.path.join(d, "py"))
            out = os.path.join(d, "dist")
            with self.assertRaisesRegex(br.ReleaseError, "not pinned yet"):
                br.build(repo_version(), out, root=ROOT, python_dir=os.path.join(d, "py"), pins=windows, epoch=EPOCH, log=lambda s: None)
            self.assertFalse(os.path.exists(out))
            r = subprocess.run([sys.executable, SCRIPT, "--version", repo_version(), "--out", out, "--python-dir", os.path.join(d, "py")],
                               capture_output=True, text=True, env=dict(os.environ, SOURCE_DATE_EPOCH=str(EPOCH)))
            self.assertEqual(r.returncode, 1)
            self.assertIn("not pinned yet", r.stderr)
            self.assertFalse(os.path.exists(out))


class PythonPinParsing(TempDirCase):
    def pins(self, mutate=None, **fields):
        d = self.tmp()
        path = fake_standalone(d)
        doc = json.loads(read(path).decode("utf-8"))
        doc.update(fields)
        if mutate:
            mutate(doc)
        write(path, json.dumps(doc))
        return path

    def test_a_good_file_is_read(self):
        python, release, pins = br.read_python_pins(self.pins())
        self.assertEqual((python, release, list(pins)), (FAKE_PY, FAKE_RELEASE, list(br.STANDALONE_TARGETS)))
        for suffix, (file, digest, size) in pins.items():
            self.assertEqual(file, "cpython-%s+%s-%s-install_only_stripped.tar.gz" % (FAKE_PY, FAKE_RELEASE, br.STANDALONE_TARGETS[suffix]))
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertGreater(size, 0)

    def test_a_null_anywhere_is_not_pinned(self):
        for where in ("python", "release", "file", "sha256", "size"):
            def mutate(doc):
                if where in ("python", "release"):
                    doc[where] = None
                else:
                    doc["targets"]["macos-arm64"][where] = None
            with self.assertRaisesRegex(br.ReleaseError, "not pinned yet"):
                br.read_python_pins(self.pins(mutate))

    def test_all_null_says_so_and_names_the_way_out(self):
        def mutate(doc):
            doc["python"] = doc["release"] = None
            for t in doc["targets"].values():
                t["file"] = t["sha256"] = t["size"] = None
        with self.assertRaisesRegex(br.ReleaseError, r"every value is null.*python-pins job.*python_pins\.py"):
            br.read_python_pins(self.pins(mutate))

    def test_malformed_values_are_refused(self):
        def target(**kw):
            return lambda doc: doc["targets"]["linux-arm64"].update(kw)
        cases = [
            ("64 lowercase hex", target(sha256="A" * 64)),
            ("64 lowercase hex", target(sha256="a" * 63)),
            ("positive number", target(size=0)),
            ("positive number", target(size="12")),
            ("positive number", target(size=True)),
            ("file must be", target(file="cpython-3.99.1+20991231-evil-install_only_stripped.tar.gz")),
            ("file must be", target(file="../evil.tar.gz")),
            ("target must be", target(target="x86_64-unknown-linux-musl")),
            ("exactly", lambda doc: doc["targets"].pop("macos-x86_64")),
            ("exactly", lambda doc: doc["targets"].update({"freebsd-x86_64": {}})),
            ("unknown key", lambda doc: doc.update({"extra": 1})),
        ]
        for message, mutate in cases:
            with self.assertRaisesRegex(br.ReleaseError, message, msg=message):
                br.read_python_pins(self.pins(mutate))
        for field, value in (("python", "3.13"), ("python", 3), ("release", "2099-12-31"), ("release", 20991231)):
            with self.assertRaises(br.ReleaseError, msg=(field, value)):
                br.read_python_pins(self.pins(**{field: value}))

    def test_a_file_that_is_not_json_or_not_there_is_refused(self):
        d = self.tmp()
        write(os.path.join(d, "bad.json"), "{ not json")
        with self.assertRaisesRegex(br.ReleaseError, "not JSON"):
            br.read_python_pins(os.path.join(d, "bad.json"))
        write(os.path.join(d, "list.json"), "[]")
        with self.assertRaisesRegex(br.ReleaseError, "expected"):
            br.read_python_pins(os.path.join(d, "list.json"))
        with self.assertRaises(br.ReleaseError):
            br.read_python_pins(os.path.join(d, "missing.json"))


# ---- archives of this repository ----------------------------------------------------------------------------------------

class RepoArchives(TempDirCase):
    """One build of the real repository (the six archives, with fake Pythons) shared by the tests."""

    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="nuc-release-repo-")
        cls.pythons = os.path.join(cls.base, "pythons")
        cls.pins, cls.python_pins = fake_pythons(cls.pythons)
        cls.out = os.path.join(cls.base, "dist")
        cls.version = repo_version()
        cls.top = "nuc-console-%s" % cls.version
        with contextlib.redirect_stderr(io.StringIO()):  # "uncommitted changes" while developing
            cls.names = br.build(cls.version, cls.out, root=ROOT, python_dir=cls.pythons, pins=cls.pins, python_pins=cls.python_pins,
                                 epoch=EPOCH, log=lambda s: None)
        cls.unix = {suffix: tar_members(os.path.join(cls.out, "%s-%s.tar.gz" % (cls.top, suffix))) for suffix in br.STANDALONE_TARGETS}
        cls.linux = cls.unix["linux-x86_64"]
        cls.macos = cls.unix["macos-arm64"]
        cls.x64 = zip_members(os.path.join(cls.out, cls.top + "-windows-x64.zip"))
        cls.arm64 = zip_members(os.path.join(cls.out, cls.top + "-windows-arm64.zip"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, True)

    def all_archives(self):
        return [(suffix, members) for suffix, members in self.unix.items()] + [("windows-x64", self.x64), ("windows-arm64", self.arm64)]

    def python_part(self, members):
        return {n: v for n, v in members.items() if n.startswith(self.top + "/python/") or n == self.top + "/python"}

    def repo_part(self, members):
        return {n: v for n, v in members.items() if not n.startswith(self.top + "/python/") and n != self.top + "/python"}

    def test_the_six_archives_and_the_sums_are_there(self):
        expected = ["%s-%s" % (self.top, s) for s in ("linux-x86_64.tar.gz", "linux-arm64.tar.gz", "macos-arm64.tar.gz", "macos-x86_64.tar.gz",
                                                       "windows-x64.zip", "windows-arm64.zip")]
        self.assertEqual(self.names, expected)
        self.assertEqual(self.names, br.archive_names(self.version))
        self.assertEqual(sorted(os.listdir(self.out)), sorted(self.names + ["SHA256SUMS"]))  # no .part left behind

    def test_one_top_folder_and_nothing_for_development(self):
        for label, members in self.all_archives():
            for name in members:
                parts = name.split("/")
                self.assertEqual(parts[0], self.top, (label, name))
                self.assertFalse(any(p.startswith(".") for p in parts), (label, name))  # .github, .gitignore, .gitattributes
                self.assertNotIn(parts[1:2], (["tests"], ["tools"], ["desktop"]), (label, name))
                self.assertNotIn(name, (self.top + "/CONTRIBUTING.md", self.top + "/CLAUDE.md"), label)
            for name in self.repo_part(members):
                self.assertNotIn("__pycache__", name)

    def test_what_every_archive_carries(self):
        common = ["src/collector.py", "src/render.py", "src/nuc_config.py", "src/web.py", "config/config.ini", "docs/INSTALL.md",
                  "README.md", "LICENSE", "SECURITY.md"]
        for label, members in self.all_archives():
            for rel in common:
                self.assertIn("%s/%s" % (self.top, rel), members, (label, rel))
            self.assertIn(self.top, members)  # the folder entries are there too
            self.assertIn(self.top + "/src", members)
            self.assertIn(self.top + "/python", members)  # and every archive has its Python

    def test_linux_has_systemd_and_install_sh(self):
        for suffix in ("linux-x86_64", "linux-arm64"):
            have = set(self.unix[suffix])
            for rel in ("install.sh", "run.sh", "systemd/nuc-console.service", "systemd/nuc-console-collector.service",
                        "systemd/nuc-console-web.service", "bin/nuc-console-accept", "bin/nuc-console-problems", "scripts/enable-ufw.sh",
                        "scripts/rebind-all-dbs.sh"):
                self.assertIn("%s/%s" % (self.top, rel), have, (suffix, rel))
            for rel in ("install-macos.sh", "install-windows.ps1", "install-windows.cmd", "launchd", "bin/nuc-console-accept.cmd"):
                self.assertNotIn("%s/%s" % (self.top, rel), have, (suffix, rel))
            self.assertFalse([n for n in have if "/launchd/" in n])

    def test_macos_has_launchd_and_both_installers(self):
        for suffix in ("macos-arm64", "macos-x86_64"):
            have = set(self.unix[suffix])
            for rel in ("install.sh", "install-macos.sh", "run.sh", "launchd/com.nuc-console.collector.plist",
                        "launchd/com.nuc-console.web.plist", "launchd/com.nuc-console.display.plist", "bin/nuc-console-accept",
                        "src/collect_darwin.py"):
                self.assertIn("%s/%s" % (self.top, rel), have, (suffix, rel))
            for rel in ("install-windows.ps1", "install-windows.cmd", "systemd", "bin/nuc-console-accept.cmd"):
                self.assertNotIn("%s/%s" % (self.top, rel), have, (suffix, rel))
            self.assertFalse([n for n in have if "/systemd/" in n])

    def test_windows_has_the_cmd_world_and_no_shell(self):
        for label, members in (("x64", self.x64), ("arm64", self.arm64)):
            have = set(members)
            for rel in ("install-windows.ps1", "install-windows.cmd", "run.cmd", "bin/nuc-console-accept.cmd", "bin/nuc-console-problems.cmd",
                        "src/collect_windows.py", "src/winapi.py"):
                self.assertIn("%s/%s" % (self.top, rel), have, (label, rel))
            for name in have:
                self.assertFalse(name.endswith(".sh"), name)
                self.assertNotIn(name.split("/", 2)[1:2], (["systemd"], ["launchd"], ["scripts"]), name)
            for rel in ("install.sh", "install-macos.sh", "run.sh", "bin/nuc-console-accept", "bin/nuc-console-problems"):
                self.assertNotIn("%s/%s" % (self.top, rel), have, (label, rel))

    def test_windows_zip_carries_the_pinned_python_for_its_own_architecture_only(self):
        version, builds = br.read_pins(self.pins)
        self.assertEqual(version, FAKE_PY)
        for label, members, key in (("x64", self.x64, "AMD64"), ("arm64", self.arm64, "ARM64")):
            file = builds[key][0]
            name = "%s/%s/%s" % (self.top, br.PYTHON_DIR, file)
            self.assertIn(name, members, label)
            self.assertEqual(sha256(members[name][1]), builds[key][1], label)
            self.assertEqual([n for n in members if n.endswith(".zip")], [name], label)
            self.assertEqual(sorted(self.python_part(members)), [self.top + "/python", name], label)  # the zip, nothing unpacked
        self.assertNotEqual(self.x64[self.top + "/python/python-%s-embed-amd64.zip" % FAKE_PY][1],
                            self.arm64[self.top + "/python/python-%s-embed-arm64.zip" % FAKE_PY][1])

    def test_linux_and_macos_carry_their_python_unpacked_for_their_own_target_only(self):
        _, _, pins = br.read_python_pins(self.python_pins)
        for suffix, members in self.unix.items():
            tree = {n[len(self.top) + 1:]: v for n, v in self.python_part(members).items()}
            tarball = tar_members(os.path.join(self.pythons, pins[suffix][0]))  # the pinned file, as the build read it
            self.assertEqual(set(tree), set(tarball), suffix)  # the same entries: nothing added, nothing dropped
            self.assertFalse([n for n in members if n.endswith(".zip")], suffix)
            for name, (member, data) in tarball.items():  # every file, with its content, from this target's tarball and no other
                got_member, got_data = tree[name]
                self.assertEqual(got_data, data, (suffix, name))
                self.assertEqual(got_member.type, member.type, (suffix, name))
                if member.issym():
                    self.assertEqual(got_member.linkname, member.linkname, (suffix, name))
        # four different tarballs, four different Pythons
        probes = {self.unix[s][self.top + "/python/bin/python3.99"][1] for s in br.STANDALONE_TARGETS}
        self.assertEqual(len(probes), 4)

    def test_the_symbolic_links_of_the_python_are_links_and_stay_inside_it(self):
        for suffix, members in self.unix.items():
            links = {n: m for n, (m, _) in members.items() if m.issym()}
            self.assertEqual(sorted(n[len(self.top) + 1:] for n in links),
                             ["python/bin/python", "python/bin/python3", "python/lib/libpython3.99.so"], suffix)
            for name, m in links.items():
                self.assertEqual(m.mode & 0o777, 0o777, (suffix, name))
                self.assertFalse(m.linkname.startswith("/"), (suffix, name))
                target = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname)).replace(os.sep, "/")
                self.assertIn(target, members, (suffix, name, m.linkname))  # a link that points at something that is there
                self.assertTrue(target.startswith(self.top + "/python/"), (suffix, name))
            self.assertEqual(members[self.top + "/python/bin/python3"][0].linkname, "python3.99", suffix)

    def test_the_python_keeps_its_exec_bits_and_its_times_everything_else_is_normal(self):
        for suffix, members in self.unix.items():
            python = self.python_part(members)
            self.assertEqual(mode_of(python[self.top + "/python/bin/python3.99"][0]), 0o755, suffix)
            self.assertEqual(mode_of(python[self.top + "/python/bin/pip3"][0]), 0o755, suffix)
            self.assertEqual(mode_of(python[self.top + "/python/lib/libpython3.99.so.1.0"][0]), 0o755, suffix)
            self.assertEqual(mode_of(python[self.top + "/python/lib/python3.99/os.py"][0]), 0o644, suffix)
            self.assertEqual(mode_of(python[self.top + "/python/include/Python.h"][0]), 0o644, suffix)  # 0600 in the tarball: no stray bits
            for name, (m, data) in python.items():
                self.assertEqual((m.uid, m.gid, m.uname, m.gname), (0, 0, "", ""), (suffix, name))  # not the 1234 of the tarball
                if m.issym():
                    continue
                self.assertIn(mode_of(m), (0o644, 0o755), (suffix, name))
                if data is None:
                    self.assertEqual(mode_of(m), 0o755, (suffix, name))
            # the times of the Python are the ones of its (pinned) tarball: its .pyc files may record them; the empty folder is kept
            self.assertEqual(python[self.top + "/python/lib/python3.99/os.py"][0].mtime, PBS_MTIME, suffix)
            self.assertIn(self.top + "/python/lib/python3.99/empty", python, suffix)
            self.assertEqual(python[self.top + "/python/lib/python3.99/empty"][0].mtime, PBS_MTIME, suffix)

    def test_nothing_else_changed_the_repository_files_are_the_same_as_without_python(self):
        # the files outside python/ are exactly those of the repository, the same set on both processors of a system
        for system, pair in (("linux", ("linux-x86_64", "linux-arm64")), ("macos", ("macos-arm64", "macos-x86_64"))):
            a, b = (self.repo_part(self.unix[s]) for s in pair)
            self.assertEqual(sorted(a), sorted(b), system)
            for name in a:
                self.assertEqual(a[name][1], b[name][1], (system, name))
                self.assertEqual(mode_of(a[name][0]), mode_of(b[name][0]), (system, name))

    def test_exec_bits(self):
        for label, members in self.unix.items():
            for rel in ("install.sh", "run.sh", "bin/nuc-console-accept", "bin/nuc-console-problems", "scripts/enable-ufw.sh"):
                self.assertEqual(mode_of(members["%s/%s" % (self.top, rel)][0]), 0o755, (label, rel))
            self.assertEqual(mode_of(members[self.top + "/README.md"][0]), 0o644, label)
            self.assertEqual(mode_of(members[self.top + "/config/config.ini"][0]), 0o644, label)
        for suffix in ("macos-arm64", "macos-x86_64"):
            self.assertEqual(mode_of(self.unix[suffix][self.top + "/install-macos.sh"][0]), 0o755)
        for label, members in (("x64", self.x64), ("arm64", self.arm64)):
            for rel in ("install-windows.ps1", "install-windows.cmd", "bin/nuc-console-accept.cmd"):
                self.assertEqual(mode_of(members["%s/%s" % (self.top, rel)][0]), 0o644, (label, rel))
        for label, members in self.all_archives():  # every mode of the repository's files is one of the two, folders 0755
            for name, (info, data) in self.repo_part(members).items():
                self.assertIn(mode_of(info), (0o644, 0o755), (label, name))
                if data is None:
                    self.assertEqual(mode_of(info), 0o755, (label, name))

    def test_no_user_names_no_ids_fixed_times(self):
        for label, members in self.unix.items():
            for name, (m, _) in self.repo_part(members).items():
                self.assertEqual((m.uid, m.gid, m.uname, m.gname, m.mtime), (0, 0, "", "", EPOCH), (label, name))
        stamp = time.gmtime(EPOCH)[:6]
        for label, members in (("x64", self.x64), ("arm64", self.arm64)):
            for name, (i, _) in members.items():
                self.assertEqual((i.date_time, i.create_system), (stamp, 3), (label, name))
        for name in self.names:  # the gzip header holds no file name and the build time is the fixed one
            if name.endswith(".tar.gz"):
                head = read(os.path.join(self.out, name))[:10]
                self.assertEqual((head[:2], head[3] & 0x08), (b"\x1f\x8b", 0), name)
                self.assertEqual(int.from_bytes(head[4:8], "little"), EPOCH, name)

    def test_entries_are_sorted(self):
        for label, members in self.all_archives():
            names = list(members)
            self.assertEqual(names, sorted(names, key=lambda n: n.split("/")), label)

    def test_contents_are_the_files_of_the_repository(self):
        for label, members in self.all_archives():
            for name, (_, data) in self.repo_part(members).items():
                if data is None or name.endswith(".zip"):
                    continue
                source = read(os.path.join(ROOT, name.split("/", 1)[1]))
                self.assertEqual(lf(data), lf(source), (label, name))

    def test_line_endings_whatever_the_checkout_did(self):
        for label, members in self.all_archives():
            shell = []
            for name, (_, data) in self.repo_part(members).items():
                base = name.rsplit("/", 1)[-1]
                if data is None:
                    continue
                if base.endswith((".cmd", ".bat")):
                    self.assertTrue(data.count(b"\n") == data.count(b"\r\n") > 0, (label, name))
                elif base.endswith(".sh") or ("." not in base and data[:2] == b"#!"):  # bin\*.ps1 is neither: as checked out
                    self.assertNotIn(b"\r", data, (label, name))
                    shell.append(name[len(self.top) + 1:])
            if label in self.unix:  # the shell scripts were all checked: run.sh, install.sh and the helpers in bin/
                self.assertLessEqual({"run.sh", "install.sh", "bin/nuc-console-accept", "bin/nuc-console-problems", "bin/nuc-console-update"},
                                     set(shell), label)

    def test_sums_match_the_archives(self):
        text = read(os.path.join(self.out, "SHA256SUMS")).decode("ascii")
        self.assertTrue(text.endswith("\n"))
        rows = [line.split("  ") for line in text.splitlines()]
        self.assertEqual([r[1] for r in rows], sorted(self.names))
        for digest, name in rows:
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertEqual(digest, sha256(read(os.path.join(self.out, name))), name)  # what `sha256sum -c` checks
        self.assertEqual(len(rows), 6)
        self.assertNotIn("SHA256SUMS", text)

    def test_the_command_line_does_the_same(self):
        """main(): the same files, and the paths are those the workflow passes."""
        out = os.path.join(self.tmp(), "cli")
        r = subprocess.run([sys.executable, SCRIPT, "--version", self.version, "--out", out, "--python-dir", self.pythons,
                            "--pins", self.pins, "--python-pins", self.python_pins],
                           capture_output=True, text=True, env=dict(os.environ, SOURCE_DATE_EPOCH=str(EPOCH)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(out)), sorted(self.names + ["SHA256SUMS"]))
        for name in self.names + ["SHA256SUMS"]:
            self.assertEqual(read(os.path.join(out, name)), read(os.path.join(self.out, name)), name)  # SOURCE_DATE_EPOCH honoured

    def test_the_archives_unpack_with_tar_and_the_python_runs_from_there(self):
        """What a user does: tar xzf, then run. The links are links on disk and the fake python3 (a script) answers."""
        if os.name == "nt" or not shutil.which("tar"):
            self.skipTest("tar and links")
        d = self.tmp()
        subprocess.run(["tar", "xzf", os.path.join(self.out, "%s-linux-x86_64.tar.gz" % self.top), "-C", d], check=True)
        py = os.path.join(d, self.top, "python", "bin", "python3")
        self.assertTrue(os.path.islink(py))
        self.assertEqual(os.readlink(py), "python3.99")
        self.assertTrue(os.access(py, os.X_OK))
        r = subprocess.run([py], capture_output=True, text=True)  # the fake: a #!/bin/sh script that prints a comment, so exit 0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.stat(os.path.join(d, self.top, "python", "lib", "python3.99", "os.py")).st_mtime, PBS_MTIME)


class Reproducible(TempDirCase):
    def build(self, root, tag, **kw):
        out = os.path.join(self.tmp(), tag)
        py = os.path.join(self.tmp(), tag + "-py")
        pins, python_pins = fake_pythons(py)
        kw.setdefault("epoch", EPOCH)
        with contextlib.redirect_stderr(io.StringIO()):
            names = br.build(repo_version() if root == ROOT else "9.9.9", out, root=root, python_dir=py, pins=pins, python_pins=python_pins,
                             log=lambda s: None, **kw)
        return {n: read(os.path.join(out, n)) for n in names + ["SHA256SUMS"]}

    def test_two_builds_of_the_repository_are_byte_identical(self):
        a, b = self.build(ROOT, "a"), self.build(ROOT, "b")
        self.assertEqual(sorted(a), sorted(b))
        for name in a:
            self.assertEqual(a[name], b[name], name)

    def test_the_time_and_only_the_time_changes_the_archives(self):
        a, b = self.build(ROOT, "a"), self.build(ROOT, "b", epoch=EPOCH + 86400)
        for name in a:
            if name != "SHA256SUMS":
                self.assertNotEqual(a[name], b[name], name)

    def test_file_system_times_do_not_matter(self):
        root = make_tree(self.tmp())
        a = self.build(root, "a")
        for dirpath, _, files in os.walk(root):
            for f in files:
                os.utime(os.path.join(dirpath, f), (1000000000, 1000000000))
        b = self.build(root, "b")
        self.assertEqual(a, b)

    def test_the_order_and_the_times_of_the_tarball_do_not_matter_but_its_content_does(self):
        """The same Python in a tarball written in another order, with other owners and a gzip made at another time, gives the same
        archive: what is in the archive is what is in the tree, sorted. The hash pin is what ties it to the real release."""
        def build(members, **kw):
            d = self.tmp()
            windows, _ = fake_pythons(d)
            tarballs = {s: fake_tarball(fake_python_tree(s)) for s in br.STANDALONE_TARGETS}
            tarballs["linux-x86_64"] = members
            python_pins = standalone_pins(d, tarballs)
            out = os.path.join(self.tmp(), "dist")
            with contextlib.redirect_stderr(io.StringIO()):
                br.build("9.9.9", out, root=make_tree(self.tmp()), python_dir=d, pins=windows, python_pins=python_pins, epoch=EPOCH,
                         log=lambda s: None)
            return read(os.path.join(out, "nuc-console-9.9.9-linux-x86_64.tar.gz"))
        tree = fake_python_tree("linux-x86_64")
        same = build(fake_tarball(tree))
        self.assertEqual(same, build(fake_tarball(list(reversed(tree)))))  # another order: the folders come after their files
        other = [(n, k, (c + b"!" if k == "file" and n.endswith("os.py") else c), m) for n, k, c, m in tree]
        self.assertNotEqual(same, build(fake_tarball(other)))  # another file inside: another archive

    def test_old_epochs_are_clamped_to_what_a_zip_can_hold(self):
        root = make_tree(self.tmp())
        out = self.tmp()
        py = os.path.join(self.tmp(), "py")
        pins, python_pins = fake_pythons(py)
        br.build("9.9.9", out, root=root, python_dir=py, pins=pins, python_pins=python_pins, epoch=0, log=lambda s: None)
        members = zip_members(os.path.join(out, "nuc-console-9.9.9-windows-x64.zip"))
        self.assertEqual({i.date_time for i, _ in members.values()}, {(1980, 1, 1, 0, 0, 0)})


# ---- what is refused ----------------------------------------------------------------------------------------------------

class Refusals(TempDirCase):
    def test_a_version_that_is_not_nuc_config_version_is_refused_and_nothing_is_written(self):
        out = os.path.join(self.tmp(), "dist")
        for wrong in ("0.0.1", "99.0.0", repo_version() + ".1"):
            with self.assertRaises(br.ReleaseError):
                br.build(wrong, out, root=ROOT, epoch=EPOCH, log=lambda s: None)
        self.assertFalse(os.path.exists(out))

    def test_the_command_line_refuses_it_too(self):
        out = os.path.join(self.tmp(), "dist")
        wrong = "0.0.1"
        r = subprocess.run([sys.executable, SCRIPT, "--version", wrong, "--out", out], capture_output=True, text=True,
                           env=dict(os.environ, SOURCE_DATE_EPOCH=str(EPOCH)))
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused", r.stderr)
        self.assertIn(repo_version(), r.stderr)
        self.assertFalse(os.path.exists(out))

    def test_a_tag_is_not_a_version(self):
        for bad in ("v" + repo_version(), "1.4", "", "1.4.0-rc1", "1.4.0\n"):
            with self.assertRaisesRegex(br.ReleaseError, r"X\.Y\.Z"):
                br.build(bad, os.path.join(self.tmp(), "dist"), root=ROOT, epoch=EPOCH, log=lambda s: None)

    def test_the_version_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            br.main(["--out", self.tmp()])

    def build(self, out, pythons, pins, python_pins, **kw):
        kw.setdefault("root", ROOT)
        return br.build(repo_version(), out, python_dir=pythons, pins=pins, python_pins=python_pins, epoch=EPOCH, log=lambda s: None, **kw)

    def test_a_python_zip_with_another_hash_is_refused_and_nothing_is_written(self):
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        pins, python_pins = fake_pythons(py)
        write(os.path.join(py, "python-%s-embed-arm64.zip" % FAKE_PY), b"PK tampered")
        with self.assertRaisesRegex(br.ReleaseError, "pins .* refused"):
            self.build(out, py, pins, python_pins)
        self.assertFalse(os.path.exists(out))

    def test_a_tarball_with_another_hash_or_size_is_refused_and_nothing_is_written(self):
        for suffix in br.STANDALONE_TARGETS:
            out, py = os.path.join(self.tmp(), "dist"), self.tmp()
            pins, python_pins = fake_pythons(py)
            _, _, pinned = br.read_python_pins(python_pins)
            file = pinned[suffix][0]
            write(os.path.join(py, file), read(os.path.join(py, file)) + b"\0")  # one byte more: the size is checked, and the hash
            with self.assertRaisesRegex(br.ReleaseError, r"bytes, python-pins\.json pins %d: refused" % pinned[suffix][2], msg=suffix):
                self.build(out, py, pins, python_pins)
            self.assertFalse(os.path.exists(out), suffix)
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        pins, python_pins = fake_pythons(py)
        _, _, pinned = br.read_python_pins(python_pins)
        file = pinned["macos-arm64"][0]
        data = bytearray(read(os.path.join(py, file)))
        data[-1] ^= 1  # the same size, another content
        write(os.path.join(py, file), bytes(data))
        with self.assertRaisesRegex(br.ReleaseError, r"SHA-256 .* python-pins\.json pins .* refused"):
            self.build(out, py, pins, python_pins)
        self.assertFalse(os.path.exists(out))

    def test_a_missing_tarball_or_zip_is_refused(self):
        for name in ("python-%s-embed-amd64.zip" % FAKE_PY, "cpython-%s+%s-aarch64-apple-darwin-install_only_stripped.tar.gz" % (FAKE_PY, FAKE_RELEASE)):
            py = self.tmp()
            pins, python_pins = fake_pythons(py)
            os.remove(os.path.join(py, name))
            with self.assertRaisesRegex(br.ReleaseError, "missing"):
                self.build(os.path.join(self.tmp(), "dist"), py, pins, python_pins)

    def test_fake_files_never_pass_the_real_pins(self):
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        _, python_pins = fake_pythons(py, "3.14.8")  # the real version and file names of the Windows zips, but not the real files
        with self.assertRaisesRegex(br.ReleaseError, "install-windows.ps1 pins"):
            br.build(repo_version(), out, root=ROOT, python_dir=py, python_pins=python_pins, epoch=EPOCH, log=lambda s: None)
        self.assertFalse(os.path.exists(out))

    def test_without_python_dir_nothing_is_built(self):
        """Every archive carries its Python: there is no build without one (and no half build of the archives that have it)."""
        out = os.path.join(self.tmp(), "dist")
        pins, python_pins = fake_pythons(self.tmp())
        with self.assertRaisesRegex(br.ReleaseError, "--python-dir is required"):
            br.build(repo_version(), out, root=ROOT, pins=pins, python_pins=python_pins, epoch=EPOCH, log=lambda s: None)
        self.assertFalse(os.path.exists(out))
        r = subprocess.run([sys.executable, SCRIPT, "--version", repo_version(), "--out", out, "--pins", pins, "--python-pins", python_pins],
                           capture_output=True, text=True, env=dict(os.environ, SOURCE_DATE_EPOCH=str(EPOCH)))
        self.assertEqual(r.returncode, 1)
        self.assertIn("--python-dir is required", r.stderr)
        self.assertFalse(os.path.exists(out))

    def test_a_pin_file_with_a_null_refuses_even_when_the_files_are_there(self):
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        pins, python_pins = fake_pythons(py)
        doc = json.loads(read(python_pins).decode("utf-8"))
        doc["targets"]["linux-arm64"]["sha256"] = None
        write(python_pins, json.dumps(doc))
        with self.assertRaisesRegex(br.ReleaseError, "not pinned yet"):
            self.build(out, py, pins, python_pins)
        self.assertFalse(os.path.exists(out))

    def python_tree_is_refused(self, members, message):
        """A tarball whose hash IS pinned (so the pin does not stop it) but whose content the build must refuse."""
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        windows, _ = fake_pythons(py)
        tarballs = {s: fake_tarball(fake_python_tree(s)) for s in br.STANDALONE_TARGETS}
        tarballs["linux-arm64"] = fake_tarball(members)
        python_pins = standalone_pins(py, tarballs)
        with self.assertRaisesRegex(br.ReleaseError, message):
            self.build(out, py, windows, python_pins)
        self.assertFalse(os.path.exists(out))

    def test_a_python_tree_that_could_be_turned_against_the_user_is_refused(self):
        tree = fake_python_tree("evil")
        cases = [
            ("not inside python/", tree + [("python/../evil", "file", b"x", 0o644)]),
            ("not inside python/", tree + [("../evil", "file", b"x", 0o644)]),
            ("not inside python/", tree + [("/etc/evil", "file", b"x", 0o644)]),
            ("not inside python/", tree + [("other/evil", "file", b"x", 0o644)]),
            ("a link must be relative", tree + [("python/bin/evil", "sym", "/etc/passwd", 0o777)]),
            ("a link must be relative", tree + [("python/bin/evil", "sym", "../../../../etc", 0o777)]),
            ("a link must be relative", tree + [("python/bin/evil", "sym", "../..", 0o777)]),
            ("below a symbolic link", tree + [("python/lib/via", "sym", "python3.99", 0o777), ("python/lib/via/evil", "file", b"x", 0o644)]),
            ("device or a pipe", tree + [("python/dev", "dev", None, 0o666)]),
            ("no python/bin/python3", [m for m in tree if m[0] != "python/bin/python3"]),
        ]
        for message, members in cases:
            self.python_tree_is_refused(members, message)

    def test_a_hard_link_in_the_tarball_becomes_a_copy_and_a_link_that_stays_inside_is_kept(self):
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        windows, _ = fake_pythons(py)
        tree = fake_python_tree("hard") + [("python/bin/pip3.99", "hard", "python/bin/pip3", 0o755),
                                           ("python/lib/python3.99/bin", "sym", "../../bin", 0o777)]
        tarballs = {s: fake_tarball(fake_python_tree(s)) for s in br.STANDALONE_TARGETS}
        tarballs["macos-x86_64"] = fake_tarball(tree)
        names = self.build(out, py, windows, standalone_pins(py, tarballs))
        members = tar_members(os.path.join(out, "nuc-console-%s-macos-x86_64.tar.gz" % repo_version()))
        top = "nuc-console-%s/python/" % repo_version()
        self.assertEqual(members[top + "bin/pip3.99"][1], b"#!/bin/sh\necho pip\n")  # the data of its target
        self.assertTrue(members[top + "bin/pip3.99"][0].isfile())
        self.assertEqual(mode_of(members[top + "bin/pip3.99"][0]), 0o755)
        self.assertTrue(members[top + "lib/python3.99/bin"][0].issym())
        self.assertEqual(len(names), 6)

    def test_a_tarball_that_is_not_a_tarball_is_refused(self):
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        windows, _ = fake_pythons(py)
        tarballs = {s: fake_tarball(fake_python_tree(s)) for s in br.STANDALONE_TARGETS}
        tarballs["linux-x86_64"] = gzip.compress(b"not a tar file at all")
        python_pins = standalone_pins(py, tarballs)
        with self.assertRaisesRegex(br.ReleaseError, "unreadable|not a .tar.gz"):
            self.build(out, py, windows, python_pins)
        tarballs["linux-x86_64"] = b"not even gzip"
        python_pins = standalone_pins(py, tarballs)
        with self.assertRaisesRegex(br.ReleaseError, "not a .tar.gz"):
            self.build(out, py, windows, python_pins)
        self.assertFalse(os.path.exists(out))

    def test_a_bad_source_date_epoch_is_refused(self):
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "yesterday"}):
            with self.assertRaisesRegex(br.ReleaseError, "SOURCE_DATE_EPOCH"):
                br.source_epoch(ROOT)

    def test_a_tree_without_git_needs_an_epoch(self):
        root = make_tree(self.tmp())
        with mock.patch.dict(os.environ):
            os.environ.pop("SOURCE_DATE_EPOCH", None)
            with self.assertRaisesRegex(br.ReleaseError, "SOURCE_DATE_EPOCH"):
                br.source_epoch(root)
            os.environ["SOURCE_DATE_EPOCH"] = "123"
            self.assertEqual(br.source_epoch(root), 123)

    @unittest.skipIf(os.name == "nt", "symbolic links need a privilege on Windows")
    def test_a_symbolic_link_cannot_be_shipped(self):
        root = make_tree(self.tmp())
        os.symlink("/etc/passwd", os.path.join(root, "docs", "passwd"))
        py = self.tmp()
        pins, python_pins = fake_pythons(py)
        with self.assertRaisesRegex(br.ReleaseError, "symbolic link"):
            br.build("9.9.9", os.path.join(self.tmp(), "dist"), root=root, python_dir=py, pins=pins, python_pins=python_pins, epoch=EPOCH,
                     log=lambda s: None)


# ---- a small tree: new files (the portable launcher, the updater) get in without touching the script -------------------

def make_tree(base, version="9.9.9"):
    """A tree like the repository, not a git checkout; -> its folder."""
    root = os.path.join(base, "tree")
    files = {
        "src/nuc_config.py": 'VERSION = "%s"\nPORTABLE = ""\n' % version,
        "src/collector.py": "#!/usr/bin/env python3\nprint('collector')\n",
        "src/collect_windows.py": "print('windows')\n",
        "bin/tool": "#!/bin/sh\necho tool\n",
        "bin/tool.cmd": "@echo off\r\necho tool\r\n",
        "install.sh": "#!/usr/bin/env bash\necho install\n",
        "install-macos.sh": "#!/bin/bash\necho macos\n",
        "install-windows.cmd": "@echo off\npowershell -File install-windows.ps1\n",  # LF in a checkout without .gitattributes
        "install-windows.ps1": "#Requires -Version 5.1\n$PyVersion = '3.99.1'\n",
        "config/config.ini": "[web]\nport = 1\n",
        "systemd/a.service": "[Service]\n",
        "launchd/a.plist": "<plist/>\n",
        "scripts/s.sh": "#!/bin/sh\n",
        "docs/INSTALL.md": "# install\n",
        "README.md": "# readme\n",
        "LICENSE": "MIT\n",
        "SECURITY.md": "# security\n",
        "CONTRIBUTING.md": "# contributing\n",
        "CLAUDE.md": "# rules\n",
        "tests/test_x.py": "pass\n",
        "tools/t.py": "pass\n",
        "desktop/src-tauri/src/main.rs": "fn main() {}\n",  # the desktop app's sources: its packages carry an archive
        "desktop/ui/index.html": "<!doctype html>\n",
        ".github/workflows/w.yml": "name: w\n",
        ".gitignore": "dist/\n",
    }
    for rel, data in files.items():
        write(os.path.join(root, *rel.split("/")), data)
        if rel in ("src/collector.py", "bin/tool", "install.sh", "install-macos.sh", "scripts/s.sh"):
            os.chmod(os.path.join(root, *rel.split("/")), 0o755)
    return root


class NewFilesGetIn(TempDirCase):
    def build_all(self, root):
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        pins, python_pins = fake_pythons(py)
        br.build("9.9.9", out, root=root, python_dir=py, pins=pins, python_pins=python_pins, epoch=EPOCH, log=lambda s: None)
        top = "nuc-console-9.9.9"
        return (top, tar_members(os.path.join(out, top + "-linux-x86_64.tar.gz")), tar_members(os.path.join(out, top + "-macos-arm64.tar.gz")),
                zip_members(os.path.join(out, top + "-windows-x64.zip")))

    def test_layout_of_a_small_tree(self):
        top, linux, macos, win = self.build_all(make_tree(self.tmp()))
        files = lambda members: sorted(n[len(top) + 1:] for n, (m, d) in members.items() if d is not None or (hasattr(m, "issym") and m.issym()))
        no_python = lambda names: [n for n in names if not n.startswith("python/")]
        self.assertEqual(no_python(files(linux)), ["LICENSE", "README.md", "SECURITY.md", "bin/tool", "config/config.ini", "docs/INSTALL.md", "install.sh",
                                                   "scripts/s.sh", "src/collect_windows.py", "src/collector.py", "src/nuc_config.py", "systemd/a.service"])
        self.assertEqual(no_python(files(macos)), ["LICENSE", "README.md", "SECURITY.md", "bin/tool", "config/config.ini", "docs/INSTALL.md", "install-macos.sh",
                                                   "install.sh", "launchd/a.plist", "scripts/s.sh", "src/collect_windows.py", "src/collector.py",
                                                   "src/nuc_config.py"])
        self.assertEqual(files(win), ["LICENSE", "README.md", "SECURITY.md", "bin/tool.cmd", "config/config.ini", "docs/INSTALL.md",
                                      "install-windows.cmd", "install-windows.ps1", "python/python-%s-embed-amd64.zip" % FAKE_PY,
                                      "src/collect_windows.py", "src/collector.py", "src/nuc_config.py"])
        for members in (linux, macos):  # the Python of each: a tree with python/bin/python3 in it
            self.assertIn(top + "/python/bin/python3", members)

    def test_the_portable_launcher_and_the_updater_are_picked_up(self):
        root = make_tree(self.tmp())
        write(os.path.join(root, "run.sh"), "#!/bin/sh\nexec python3 src/render.py\n")
        os.chmod(os.path.join(root, "run.sh"), 0o755)
        write(os.path.join(root, "run.cmd"), "@echo off\r\npython src\\render.py\r\n")
        write(os.path.join(root, "bin", "nuc-console-update"), "#!/bin/sh\necho update\n")
        os.chmod(os.path.join(root, "bin", "nuc-console-update"), 0o755)
        write(os.path.join(root, "bin", "nuc-console-update.cmd"), "@echo off\r\necho update\r\n")
        write(os.path.join(root, "src", "updater.py"), "print('update')\n")
        top, linux, macos, win = self.build_all(root)
        for label, members, have, lacks in (("linux", linux, ("run.sh", "bin/nuc-console-update", "src/updater.py"), ("run.cmd", "bin/nuc-console-update.cmd")),
                                            ("macos", macos, ("run.sh", "bin/nuc-console-update", "src/updater.py"), ("run.cmd", "bin/nuc-console-update.cmd")),
                                            ("windows", win, ("run.cmd", "bin/nuc-console-update.cmd", "src/updater.py"), ("run.sh", "bin/nuc-console-update"))):
            for rel in have:
                self.assertIn("%s/%s" % (top, rel), members, (label, rel))
            for rel in lacks:
                self.assertNotIn("%s/%s" % (top, rel), members, (label, rel))
        self.assertEqual(mode_of(linux[top + "/run.sh"][0]), 0o755)
        self.assertEqual(mode_of(macos[top + "/bin/nuc-console-update"][0]), 0o755)

    def test_a_python_script_without_extension_is_for_every_system(self):
        root = make_tree(self.tmp())
        write(os.path.join(root, "bin", "nuc-console-update"), "#!/usr/bin/env python3\r\nprint('update')\r\n")
        top, linux, macos, win = self.build_all(root)
        for label, members in (("linux", linux), ("macos", macos), ("windows", win)):
            self.assertIn(top + "/bin/nuc-console-update", members, label)
            self.assertEqual(members[top + "/bin/nuc-console-update"][1], b"#!/usr/bin/env python3\nprint('update')\n", label)  # a #! line ends in LF

    def test_line_endings_follow_the_rules_whatever_the_checkout_did(self):
        root = make_tree(self.tmp())
        write(os.path.join(root, "install.sh"), "#!/usr/bin/env bash\r\necho install\r\n")  # a Windows checkout with autocrlf
        top, linux, macos, win = self.build_all(root)
        self.assertEqual(linux[top + "/install.sh"][1], b"#!/usr/bin/env bash\necho install\n")
        self.assertEqual(win[top + "/install-windows.cmd"][1], b"@echo off\r\npowershell -File install-windows.ps1\r\n")
        self.assertEqual(win[top + "/bin/tool.cmd"][1], b"@echo off\r\necho tool\r\n")
        self.assertEqual(win[top + "/README.md"][1], b"# readme\n")  # other files are left alone

    def test_the_output_folder_inside_the_tree_is_not_shipped(self):
        root = make_tree(self.tmp())
        out = os.path.join(root, "out")
        write(os.path.join(out, "stale.txt"), "old build")
        py = self.tmp()
        pins, python_pins = fake_pythons(py)
        br.build("9.9.9", out, root=root, python_dir=py, pins=pins, python_pins=python_pins, epoch=EPOCH, log=lambda s: None)
        for name in ("nuc-console-9.9.9-linux-x86_64.tar.gz", "nuc-console-9.9.9-macos-arm64.tar.gz"):
            self.assertFalse([n for n in tar_members(os.path.join(out, name)) if "/out/" in n or "stale" in n], name)

    def test_a_tree_inside_another_repository_is_not_a_checkout(self):
        root = make_tree(self.tmp())
        self.assertFalse(br.is_checkout(root))
        self.assertTrue(br.source_files(root))


@unittest.skipUnless(shutil.which("git"), "git is needed")
class GitCheckout(TempDirCase):
    def git(self, root, *args):
        cmd = ["git", "-c", "user.name=Release Test", "-c", "user.email=release-test@example.com", "-c", "commit.gpgsign=false",
               "-c", "core.autocrlf=false", "-C", root] + list(args)
        env = dict(os.environ, GIT_AUTHOR_DATE="2020-02-02T02:02:02Z", GIT_COMMITTER_DATE="2020-02-02T02:02:02Z")
        return subprocess.run(cmd, capture_output=True, text=True, check=True, env=env).stdout

    def checkout(self):
        root = make_tree(self.tmp())
        self.git(root, "init", "-q")
        self.git(root, "add", "-A")
        self.git(root, "update-index", "--chmod=+x", "install.sh", "bin/tool")
        self.git(root, "update-index", "--chmod=-x", "src/collector.py")
        self.git(root, "commit", "-q", "-m", "release test")
        return root

    def test_files_come_from_git_not_from_the_file_system(self):
        root = self.checkout()
        write(os.path.join(root, "notes.txt"), "not committed")  # untracked: never shipped
        os.chmod(os.path.join(root, "bin", "tool"), 0o644)  # the exec bit is git's (a Windows checkout has none)
        out = os.path.join(self.tmp(), "dist")
        with mock.patch.dict(os.environ):
            os.environ.pop("SOURCE_DATE_EPOCH", None)
            with contextlib.redirect_stderr(io.StringIO()):
                py = self.tmp()
                pins, python_pins = fake_pythons(py)
                br.build("9.9.9", out, root=root, python_dir=py, pins=pins, python_pins=python_pins, log=lambda s: None)
        top = "nuc-console-9.9.9"
        members = tar_members(os.path.join(out, top + "-linux-x86_64.tar.gz"))
        self.assertNotIn(top + "/notes.txt", members)
        self.assertEqual(mode_of(members[top + "/bin/tool"][0]), 0o755)
        self.assertEqual(mode_of(members[top + "/install.sh"][0]), 0o755)
        self.assertEqual(mode_of(members[top + "/src/collector.py"][0]), 0o644)
        commit_time = int(self.git(root, "log", "-1", "--format=%ct").strip())
        self.assertEqual(commit_time, 1580608922)
        # the default time: the last commit's (the Python keeps the times of its tarball)
        self.assertEqual({m.mtime for n, (m, _) in members.items() if n != top + "/python" and not n.startswith(top + "/python/")},
                         {commit_time})

    def test_uncommitted_changes_are_reported(self):
        root = self.checkout()
        write(os.path.join(root, "README.md"), "changed")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            br.source_files(root)
        self.assertIn("uncommitted changes", err.getvalue())

    def test_a_file_deleted_but_not_committed_is_not_shipped(self):
        root = self.checkout()
        os.remove(os.path.join(root, "LICENSE"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotIn("LICENSE", [p for p, _ in br.source_files(root)])


class Workflow(unittest.TestCase):
    """.github/workflows/release.yml, read as text (no YAML parser in the standard library): what must hold whatever else changes."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(os.path.join(ROOT, ".github", "workflows", "release.yml")).decode("utf-8").replace("\r\n", "\n")  # a Windows checkout
        cls.jobs = {}
        name = None
        for line in cls.text.split("\n"):
            if line.startswith("jobs:"):
                name = ""
            elif name is not None and re.match(r"  [a-z][\w-]*:\s*$", line):
                name = line.strip()[:-1]
                cls.jobs[name] = []
            elif name and line.startswith("   "):
                cls.jobs[name].append(line)
        cls.jobs = {k: "\n".join(v) for k, v in cls.jobs.items()}

    def test_every_action_is_pinned_by_commit_sha_and_says_which_release(self):
        uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", self.text, re.M)
        self.assertGreaterEqual(len(uses), 5)
        local = [ref for ref, _ in uses if ref.startswith("./")]
        self.assertEqual(local, ["./.github/workflows/desktop.yml"])  # a workflow of this repository, at this commit: nothing to pin
        for ref, rest in uses:
            if ref in local:
                continue
            self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", ref)
            self.assertRegex(rest, r"#\s*v\d+\.\d+\.\d+", ref)

    def test_the_tag_must_be_the_version_of_the_code(self):
        build = self.jobs["build"]
        self.assertIn("nuc_config.VERSION", build)
        self.assertIn('"${TAG#v}" != "$version"', build)
        self.assertRegex(build, r"\^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$")  # only vX.Y.Z
        self.assertLess(build.index("nuc_config.VERSION"), build.index("unittest"))  # before the tests and the build

    def test_only_the_publishing_job_can_write(self):
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")
        self.assertEqual(sorted(self.jobs), ["build", "desktop", "release", "smoke-unix", "smoke-windows"])
        build, release = self.jobs["build"], self.jobs["release"]
        self.assertRegex(build, r"permissions:\n      contents: read\n")
        for word in ("write", "id-token", "attestations"):
            self.assertNotIn(word, build)
        self.assertRegex(release, r"contents: write\b")
        self.assertRegex(release, r"id-token: write\b")
        self.assertRegex(release, r"attestations: write\b")
        self.assertEqual(len(re.findall(r"^\s+\w[\w-]*: write\b", self.text, re.M)), 3)  # and nothing else is granted
        # the job that can write runs nothing of this repository: no checkout, no tests, no script of tools/
        for word in ("actions/checkout", "unittest", "tools/", "python", "setup-python"):
            self.assertNotIn(word, release)
        self.assertIn("github.event_name == 'push'", release)  # a dry run publishes nothing
        for job in ("smoke-unix", "smoke-windows", "desktop"):  # these read only: they run the archives, they publish and sign nothing
            self.assertRegex(self.jobs[job], r"permissions:\n      contents: read\n")
            self.assertNotRegex(self.jobs[job], r"\w: write\b")
            for word in ("id-token", "attestations", "gh release", "attest-build-provenance"):
                self.assertNotIn(word, self.jobs[job])

    def test_what_is_published_is_what_was_built_and_attested(self):
        release = self.jobs["release"]
        self.assertIn("sha256sum --check SHA256SUMS", release)
        self.assertIn("actions/attest-build-provenance@", release)
        for pattern in ("dist/*.tar.gz", "dist/*.zip", "dist/SHA256SUMS"):
            self.assertIn(pattern, release)  # every archive, and the sums
        self.assertIn("gh release create", release)
        self.assertIn("--verify-tag", release)
        # and the desktop app's packages, made by desktop.yml around those very archives: listed, counted, summed and attested too
        desktop = self.jobs["desktop"]
        self.assertIn("uses: ./.github/workflows/desktop.yml", desktop)
        self.assertIn("needs: build", desktop)
        self.assertRegex(desktop, r"with:\n      dist: dist\b")
        for word in ("pattern: desktop-package-*", "-eq 10", "SHA256SUMS-desktop", "desktop/nuc-console-desktop-*", "desktop/SHA256SUMS-desktop",
                     'gh release create "$TAG" dist/* desktop/*'):
            self.assertIn(word, release, word)
        for suffix in ("linux-x86_64.deb", "linux-x86_64.rpm", "linux-x86_64.AppImage", "linux-arm64.deb", "linux-arm64.rpm", "macos-arm64.dmg",
                       "macos-x86_64.dmg", "windows-x64.msi", "windows-x64-setup.exe", "windows-arm64-setup.exe"):
            self.assertIn(suffix, release, suffix)
        self.assertLess(release.index("attest-build-provenance"), release.index("gh release create"))
        self.assertIn("--python-dir", self.jobs["build"])  # every archive carries its Python
        self.assertIn("cmp ", self.jobs["build"])  # and the build is made twice and compared

    def test_six_archives_are_expected_and_every_python_is_checked_before_the_build(self):
        build, release = self.jobs["build"], self.jobs["release"]
        for suffix in ("linux-x86_64.tar.gz", "linux-arm64.tar.gz", "macos-arm64.tar.gz", "macos-x86_64.tar.gz", "windows-x64.zip", "windows-arm64.zip"):
            self.assertIn(suffix, build, suffix)
            self.assertIn(suffix, release, suffix)
            self.assertIn("nuc-console-9.9.9-" + suffix, br.archive_names("9.9.9"), suffix)  # the names the script builds
        for job in (build, release):
            self.assertIn("-eq 7", job)  # the six archives and SHA256SUMS
        self.assertIn("-eq 6", build)  # the six Pythons
        self.assertIn("--list-python", build)  # the files and their hashes come from the pins, not from the workflow
        self.assertIn("sha256sum --check -", build)
        self.assertLess(build.index("--list-python"), build.index("tools/build_release.py --version"))
        self.assertNotIn("linux.tar.gz", self.text)  # the old names are gone
        self.assertNotIn("macos.tar.gz", self.text)

    def smoke_matrix(self, job):
        return re.findall(r"\{ archive: ([\w-]+), runner: ([\w.-]+) \}", self.jobs[job])

    def test_every_archive_is_run_on_a_runner_of_its_own_before_anything_is_published(self):
        # build_release.archive_names is what is published: each of those archives has exactly one entry in a smoke matrix
        published = sorted(n[len("nuc-console-9.9.9-"):].replace(".tar.gz", "").replace(".zip", "") for n in br.archive_names("9.9.9"))
        unix, windows = self.smoke_matrix("smoke-unix"), self.smoke_matrix("smoke-windows")
        self.assertEqual(sorted(a for a, _ in unix + windows), published)
        self.assertEqual(sorted(a for a, _ in unix), [a for a in published if not a.startswith("windows-")])
        self.assertEqual(sorted(a for a, _ in windows), [a for a in published if a.startswith("windows-")])
        # the system AND the processor of the archive: a runner of the other processor would not run its Python at all
        self.assertEqual(dict(unix + windows), {
            "linux-x86_64": "ubuntu-latest", "linux-arm64": "ubuntu-24.04-arm",  # free for public repositories
            "macos-arm64": "macos-latest",  # Apple silicon
            "macos-x86_64": "macos-15-intel",  # the last hosted Intel Mac: when GitHub retires it, drop the entry and say so in CONTRIBUTING.md
            "windows-x64": "windows-latest", "windows-arm64": "windows-11-arm"})
        for job in ("smoke-unix", "smoke-windows"):
            text = self.jobs[job]
            self.assertIn("needs: build", text)
            self.assertIn("fail-fast: false", text)  # one broken archive does not hide the state of the others
            self.assertIn("runs-on: ${{ matrix.runner }}", text)
            self.assertRegex(text, r"actions/download-artifact@[0-9a-f]{40} # v\d+\.\d+\.\d+\n\s+with:\n\s+name: dist\n")
            # the archive alone: no checkout of the repository and no Python set up, or the Python of the archive would prove nothing
            for word in ("actions/checkout", "setup-python", "python-version", "actions/cache"):
                self.assertNotIn(word, text)
            self.assertEqual(re.findall(r"uses: (\S+)@", text), ["actions/download-artifact"])
            self.assertIn("SHA256SUMS", text)  # what is run is what is listed, which is what the release job publishes
            self.assertIn("ARCHIVE: ${{ matrix.archive }}", text)
            self.assertIn("TAG: ${{ github.event_name == 'push' && github.ref_name || inputs.tag }}", text)  # the archive of this tag
            self.assertIn("--once --demo", text)
        # unix: run.sh names the Python of the archive, --problems exits 0, then the demo with that Python; nothing steers it
        unix = self.jobs["smoke-unix"]
        for word in ("./run.sh --which-python", "$SMOKE/python/bin/python3", "unset PYTHON PYTHONHOME PYTHONPATH", "./run.sh --problems",
                     "--problems --json", "-B src/render.py --once --demo", "tar xzf", "sys.prefix"):
            self.assertIn(word, unix, word)
        self.assertLess(unix.index("--which-python"), unix.index("./run.sh --problems"))
        self.assertLess(unix.index("./run.sh --problems"), unix.index("--once --demo"))
        # windows: the same through run.cmd; as an administrator (the runners are) run.ps1 only runs from a folder users cannot write
        windows = self.jobs["smoke-windows"]
        for word in ("run.cmd -WhichPython", "python\\python.exe", "run.cmd -Problems", "src\\render.py", "Expand-Archive", "$env:ProgramFiles",
                     "nuc-console-python.txt", "sys.prefix"):
            self.assertIn(word, windows, word)
        self.assertLess(windows.index("run.cmd -WhichPython"), windows.index("run.cmd -Problems"))
        self.assertLess(windows.index("run.cmd -Problems"), windows.index("--once --demo"))
        # nothing is published unless every smoke job passed: the release job needs every other job (a new job cannot be forgotten)
        needs = re.search(r"(?m)^    needs: \[([^\]]*)\]", self.jobs["release"])
        self.assertTrue(needs)
        self.assertEqual(sorted(n.strip() for n in needs.group(1).split(",")), sorted(set(self.jobs) - {"release"}))
        self.assertNotRegex(self.jobs["release"], r"continue-on-error|always\(\)|failure\(\)|cancelled\(\)")
        for job in self.jobs.values():
            self.assertNotIn("continue-on-error", job)  # a failing smoke test must fail the run

    def test_no_expression_is_pasted_into_a_script(self):
        # ${{ }} in a run: block is shell injection (a tag name, an input): values go through env: and are quoted there
        in_run = False
        for line in self.text.split("\n"):
            indent = len(line) - len(line.lstrip())
            if re.match(r"\s*(- )?run:\s*\|", line):
                in_run, run_indent = True, indent
                continue
            if in_run and line.strip() and indent <= run_indent:
                in_run = False
            if in_run or re.match(r"\s*(- )?run:\s*\S", line):
                self.assertNotIn("${{", line)
        self.assertNotIn("pull_request_target", self.text)


class PinsWorkflow(unittest.TestCase):
    """.github/workflows/ai-pins.yml: the python-pins job reads what tools/python-pins.json needs, and can change nothing."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(os.path.join(ROOT, ".github", "workflows", "ai-pins.yml")).decode("utf-8").replace("\r\n", "\n")

    def test_the_python_pins_job_runs_the_script_that_prints_the_json(self):
        self.assertRegex(self.text, r"(?m)^  python-pins:$")
        job = self.text[self.text.index("  python-pins:"):self.text.index("  for-real:")]
        self.assertIn("python3 tools/python_pins.py", job)
        self.assertIn("--download", job)  # the real bytes are compared with the numbers
        self.assertIn("GITHUB_TOKEN", job)
        self.assertNotIn("write", job)
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")  # nothing here can write
        self.assertEqual(len(re.findall(r"^\s+\w[\w-]*: write\b", self.text, re.M)), 0)
        self.assertIn("tools/python-pins.json", self.text)  # a pull request that changes the pins runs it
        self.assertIn("workflow_dispatch:", self.text)

    def test_the_script_the_workflow_runs_exists(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, "tools", "python_pins.py")))


if __name__ == "__main__":
    unittest.main()
