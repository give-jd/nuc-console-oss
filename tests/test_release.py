"""tools/build_release.py: the release archives (what is in them, byte-for-byte reproducible, SHA256SUMS, what is refused).

Runs on every OS and downloads nothing: the Windows archives are built with small fake "Python zips" whose hashes are
written to a fake pin file (--pins) that looks like the one in install-windows.ps1.
"""
import contextlib
import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
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


def fake_pythons(folder, version=FAKE_PY):
    """Writes two small fake embeddable Pythons into folder and a pin file for them; -> pin file path."""
    pins = ["#Requires -Version 5.1", "$ErrorActionPreference = 'Stop'", "", "$PyVersion = '%s'" % version, "$PyBuilds = @{  # fake"]
    for key in ("AMD64", "ARM64"):
        data = ("PK fake embeddable python %s %s\n" % (version, key)).encode("ascii") * 20
        write(os.path.join(folder, "python-%s-embed-%s.zip" % (version, key.lower())), data)
        pins.append("    '%s' = @{ file = \"python-$PyVersion-embed-%s.zip\"; sha256 = '%s' }" % (key, key.lower(), sha256(data)))
    pins += ["}", "$Dest = 'whatever follows'", ""]
    path = os.path.join(folder, "pins.ps1")
    write(path, "\n".join(pins))
    return path


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
        for file, digest, url in br.python_downloads(os.path.join(ROOT, "install-windows.ps1")):
            self.assertTrue(url.startswith("https://www.python.org/ftp/python/"), url)
            self.assertTrue(url.endswith("/" + file), url)

    def test_list_python_prints_file_sha_url(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(br.main(["--list-python"]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            file, digest, url = line.split(" ")
            self.assertRegex(file, r"^python-\d+\.\d+\.\d+-embed-(amd64|arm64)\.zip$")
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertTrue(url.startswith("https://"))

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


# ---- archives of this repository ----------------------------------------------------------------------------------------

class RepoArchives(TempDirCase):
    """One build of the real repository (Linux, macOS, Windows x64/arm64 with fake Pythons) shared by the tests."""

    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="nuc-release-repo-")
        cls.pyzips = os.path.join(cls.base, "pyzips")
        cls.pins = fake_pythons(cls.pyzips)
        cls.out = os.path.join(cls.base, "dist")
        cls.version = repo_version()
        cls.top = "nuc-console-%s" % cls.version
        with contextlib.redirect_stderr(io.StringIO()):  # "uncommitted changes" while developing
            cls.names = br.build(cls.version, cls.out, root=ROOT, python_zips=cls.pyzips, pins=cls.pins, epoch=EPOCH, log=lambda s: None)
        cls.linux = tar_members(os.path.join(cls.out, cls.top + "-linux.tar.gz"))
        cls.macos = tar_members(os.path.join(cls.out, cls.top + "-macos.tar.gz"))
        cls.x64 = zip_members(os.path.join(cls.out, cls.top + "-windows-x64.zip"))
        cls.arm64 = zip_members(os.path.join(cls.out, cls.top + "-windows-arm64.zip"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, True)

    def all_archives(self):
        return (("linux", self.linux), ("macos", self.macos), ("windows-x64", self.x64), ("windows-arm64", self.arm64))

    def test_the_four_archives_and_the_sums_are_there(self):
        self.assertEqual(sorted(self.names), sorted(self.top + s for s in ("-linux.tar.gz", "-macos.tar.gz", "-windows-x64.zip", "-windows-arm64.zip")))
        self.assertEqual(sorted(os.listdir(self.out)), sorted(self.names + ["SHA256SUMS"]))  # no .part left behind

    def test_one_top_folder_and_nothing_for_development(self):
        for label, members in self.all_archives():
            for name in members:
                parts = name.split("/")
                self.assertEqual(parts[0], self.top, (label, name))
                self.assertFalse(any(p.startswith(".") for p in parts), (label, name))  # .github, .gitignore, .gitattributes
                self.assertNotIn(parts[1:2], (["tests"], ["tools"]), (label, name))
                self.assertNotEqual(name, self.top + "/CONTRIBUTING.md", label)
                self.assertNotIn("__pycache__", name)

    def test_what_every_archive_carries(self):
        common = ["src/collector.py", "src/render.py", "src/nuc_config.py", "src/web.py", "config/config.ini", "docs/INSTALL.md",
                  "README.md", "LICENSE", "SECURITY.md"]
        for label, members in self.all_archives():
            for rel in common:
                self.assertIn("%s/%s" % (self.top, rel), members, (label, rel))
            self.assertIn(self.top, members)  # the folder entries are there too
            self.assertIn(self.top + "/src", members)

    def test_linux_has_systemd_and_install_sh(self):
        have = set(self.linux)
        for rel in ("install.sh", "systemd/nuc-console.service", "systemd/nuc-console-collector.service", "systemd/nuc-console-web.service",
                    "bin/nuc-console-accept", "bin/nuc-console-problems", "scripts/enable-ufw.sh", "scripts/rebind-all-dbs.sh"):
            self.assertIn("%s/%s" % (self.top, rel), have, rel)
        for rel in ("install-macos.sh", "install-windows.ps1", "install-windows.cmd", "launchd", "bin/nuc-console-accept.cmd"):
            self.assertNotIn("%s/%s" % (self.top, rel), have, rel)
        self.assertFalse([n for n in have if "/launchd/" in n])

    def test_macos_has_launchd_and_both_installers(self):
        have = set(self.macos)
        for rel in ("install.sh", "install-macos.sh", "launchd/com.nuc-console.collector.plist", "launchd/com.nuc-console.web.plist",
                    "launchd/com.nuc-console.display.plist", "bin/nuc-console-accept", "src/collect_darwin.py"):
            self.assertIn("%s/%s" % (self.top, rel), have, rel)
        for rel in ("install-windows.ps1", "install-windows.cmd", "systemd", "bin/nuc-console-accept.cmd"):
            self.assertNotIn("%s/%s" % (self.top, rel), have, rel)
        self.assertFalse([n for n in have if "/systemd/" in n])

    def test_windows_has_the_cmd_world_and_no_shell(self):
        for label, members in (("x64", self.x64), ("arm64", self.arm64)):
            have = set(members)
            for rel in ("install-windows.ps1", "install-windows.cmd", "bin/nuc-console-accept.cmd", "bin/nuc-console-problems.cmd",
                        "src/collect_windows.py", "src/winapi.py"):
                self.assertIn("%s/%s" % (self.top, rel), have, (label, rel))
            for name in have:
                self.assertFalse(name.endswith(".sh"), name)
                self.assertNotIn(name.split("/", 2)[1:2], (["systemd"], ["launchd"], ["scripts"]), name)
            for rel in ("install.sh", "install-macos.sh", "bin/nuc-console-accept", "bin/nuc-console-problems"):
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
        self.assertNotEqual(self.x64[self.top + "/python/python-%s-embed-amd64.zip" % FAKE_PY][1],
                            self.arm64[self.top + "/python/python-%s-embed-arm64.zip" % FAKE_PY][1])

    def test_linux_and_macos_do_not_carry_python(self):
        for label, members in (("linux", self.linux), ("macos", self.macos)):
            self.assertFalse([n for n in members if "/python/" in n or n.endswith(".zip")], label)

    def test_exec_bits(self):
        for label, members in (("linux", self.linux), ("macos", self.macos)):
            for rel in ("install.sh", "bin/nuc-console-accept", "bin/nuc-console-problems", "scripts/enable-ufw.sh"):
                self.assertEqual(mode_of(members["%s/%s" % (self.top, rel)][0]), 0o755, (label, rel))
            self.assertEqual(mode_of(members[self.top + "/README.md"][0]), 0o644, label)
            self.assertEqual(mode_of(members[self.top + "/config/config.ini"][0]), 0o644, label)
        self.assertEqual(mode_of(self.macos[self.top + "/install-macos.sh"][0]), 0o755)
        for label, members in (("x64", self.x64), ("arm64", self.arm64)):
            for rel in ("install-windows.ps1", "install-windows.cmd", "bin/nuc-console-accept.cmd"):
                self.assertEqual(mode_of(members["%s/%s" % (self.top, rel)][0]), 0o644, (label, rel))
        for label, members in self.all_archives():  # every mode is one of the two, folders 0755
            for name, (info, data) in members.items():
                self.assertIn(mode_of(info), (0o644, 0o755), (label, name))
                if data is None:
                    self.assertEqual(mode_of(info), 0o755, (label, name))

    def test_no_user_names_no_ids_fixed_times(self):
        for label, members in (("linux", self.linux), ("macos", self.macos)):
            for name, (m, _) in members.items():
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
            for name, (_, data) in members.items():
                if data is None or name.endswith(".zip"):
                    continue
                source = read(os.path.join(ROOT, name.split("/", 1)[1]))
                self.assertEqual(lf(data), lf(source), (label, name))

    def test_line_endings_whatever_the_checkout_did(self):
        for label, members in self.all_archives():
            shell = []
            for name, (_, data) in members.items():
                base = name.rsplit("/", 1)[-1]
                if data is None:
                    continue
                if base.endswith((".cmd", ".bat")):
                    self.assertTrue(data.count(b"\n") == data.count(b"\r\n") > 0, (label, name))
                elif base.endswith(".sh") or ("." not in base and data[:2] == b"#!"):  # bin\*.ps1 is neither: as checked out
                    self.assertNotIn(b"\r", data, (label, name))
                    shell.append(name[len(self.top) + 1:])
            if label in ("linux", "macos"):  # the shell scripts were all checked: run.sh, install.sh and the helpers in bin/
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
        self.assertEqual(len(rows), 4)
        self.assertNotIn("SHA256SUMS", text)

    def test_the_command_line_does_the_same(self):
        """main(): the same files, and the paths are those the workflow passes."""
        out = os.path.join(self.tmp(), "cli")
        r = subprocess.run([sys.executable, SCRIPT, "--version", self.version, "--out", out, "--python-zips", self.pyzips, "--pins", self.pins],
                           capture_output=True, text=True, env=dict(os.environ, SOURCE_DATE_EPOCH=str(EPOCH)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(out)), sorted(self.names + ["SHA256SUMS"]))
        for name in self.names + ["SHA256SUMS"]:
            self.assertEqual(read(os.path.join(out, name)), read(os.path.join(self.out, name)), name)  # SOURCE_DATE_EPOCH honoured


class Reproducible(TempDirCase):
    def build(self, root, tag, **kw):
        out = os.path.join(self.tmp(), tag)
        py = os.path.join(self.tmp(), tag + "-py")
        pins = fake_pythons(py)
        kw.setdefault("epoch", EPOCH)
        with contextlib.redirect_stderr(io.StringIO()):
            names = br.build(repo_version() if root == ROOT else "9.9.9", out, root=root, python_zips=py, pins=pins, log=lambda s: None, **kw)
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

    def test_old_epochs_are_clamped_to_what_a_zip_can_hold(self):
        root = make_tree(self.tmp())
        out = self.tmp()
        py = os.path.join(self.tmp(), "py")
        pins = fake_pythons(py)
        br.build("9.9.9", out, root=root, python_zips=py, pins=pins, epoch=0, log=lambda s: None)
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

    def test_a_python_zip_with_another_hash_is_refused_and_nothing_is_written(self):
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        pins = fake_pythons(py)
        write(os.path.join(py, "python-%s-embed-arm64.zip" % FAKE_PY), b"PK tampered")
        with self.assertRaisesRegex(br.ReleaseError, "pins .* refused"):
            br.build(repo_version(), out, root=ROOT, python_zips=py, pins=pins, epoch=EPOCH, log=lambda s: None)
        self.assertFalse(os.path.exists(out))

    def test_fake_zips_never_pass_the_real_pins(self):
        out, py = os.path.join(self.tmp(), "dist"), self.tmp()
        fake_pythons(py, "3.14.8")  # the real version and file names, but not the real files
        with self.assertRaisesRegex(br.ReleaseError, "install-windows.ps1 pins"):
            br.build(repo_version(), out, root=ROOT, python_zips=py, epoch=EPOCH, log=lambda s: None)
        self.assertFalse(os.path.exists(out))

    def test_a_missing_python_zip_is_refused(self):
        py = self.tmp()
        pins = fake_pythons(py)
        os.remove(os.path.join(py, "python-%s-embed-amd64.zip" % FAKE_PY))
        with self.assertRaisesRegex(br.ReleaseError, "missing"):
            br.build(repo_version(), os.path.join(self.tmp(), "dist"), root=ROOT, python_zips=py, pins=pins, epoch=EPOCH, log=lambda s: None)

    def test_without_python_zips_only_linux_and_macos_are_built(self):
        out = os.path.join(self.tmp(), "dist")
        logs = []
        with contextlib.redirect_stderr(io.StringIO()):
            names = br.build(repo_version(), out, root=ROOT, epoch=EPOCH, log=logs.append)
        self.assertEqual(sorted(names), sorted("nuc-console-%s-%s.tar.gz" % (repo_version(), s) for s in ("linux", "macos")))
        self.assertTrue([m for m in logs if "windows archives skipped" in m])

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
        with self.assertRaisesRegex(br.ReleaseError, "symbolic link"):
            br.build("9.9.9", os.path.join(self.tmp(), "dist"), root=root, epoch=EPOCH, log=lambda s: None)


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
        "tests/test_x.py": "pass\n",
        "tools/t.py": "pass\n",
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
        pins = fake_pythons(py)
        br.build("9.9.9", out, root=root, python_zips=py, pins=pins, epoch=EPOCH, log=lambda s: None)
        top = "nuc-console-9.9.9"
        return (top, tar_members(os.path.join(out, top + "-linux.tar.gz")), tar_members(os.path.join(out, top + "-macos.tar.gz")),
                zip_members(os.path.join(out, top + "-windows-x64.zip")))

    def test_layout_of_a_small_tree(self):
        top, linux, macos, win = self.build_all(make_tree(self.tmp()))
        files = lambda members: sorted(n[len(top) + 1:] for n, (_, d) in members.items() if d is not None)
        self.assertEqual(files(linux), ["LICENSE", "README.md", "SECURITY.md", "bin/tool", "config/config.ini", "docs/INSTALL.md", "install.sh",
                                       "scripts/s.sh", "src/collect_windows.py", "src/collector.py", "src/nuc_config.py", "systemd/a.service"])
        self.assertEqual(files(macos), ["LICENSE", "README.md", "SECURITY.md", "bin/tool", "config/config.ini", "docs/INSTALL.md", "install-macos.sh",
                                       "install.sh", "launchd/a.plist", "scripts/s.sh", "src/collect_windows.py", "src/collector.py",
                                       "src/nuc_config.py"])
        self.assertEqual(files(win), ["LICENSE", "README.md", "SECURITY.md", "bin/tool.cmd", "config/config.ini", "docs/INSTALL.md",
                                      "install-windows.cmd", "install-windows.ps1", "python/python-%s-embed-amd64.zip" % FAKE_PY,
                                      "src/collect_windows.py", "src/collector.py", "src/nuc_config.py"])

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
        br.build("9.9.9", out, root=root, epoch=EPOCH, log=lambda s: None)
        for name in ("nuc-console-9.9.9-linux.tar.gz", "nuc-console-9.9.9-macos.tar.gz"):
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
                br.build("9.9.9", out, root=root, log=lambda s: None)
        top = "nuc-console-9.9.9"
        members = tar_members(os.path.join(out, top + "-linux.tar.gz"))
        self.assertNotIn(top + "/notes.txt", members)
        self.assertEqual(mode_of(members[top + "/bin/tool"][0]), 0o755)
        self.assertEqual(mode_of(members[top + "/install.sh"][0]), 0o755)
        self.assertEqual(mode_of(members[top + "/src/collector.py"][0]), 0o644)
        commit_time = int(self.git(root, "log", "-1", "--format=%ct").strip())
        self.assertEqual(commit_time, 1580608922)
        self.assertEqual({m.mtime for m, _ in members.values()}, {commit_time})  # the default time: the last commit's

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
        for ref, rest in uses:
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
        self.assertEqual(sorted(self.jobs), ["build", "release"])
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
        self.assertIn("needs: build", release)
        self.assertIn("github.event_name == 'push'", release)  # a dry run publishes nothing

    def test_what_is_published_is_what_was_built_and_attested(self):
        release = self.jobs["release"]
        self.assertIn("sha256sum --check SHA256SUMS", release)
        self.assertIn("actions/attest-build-provenance@", release)
        for pattern in ("dist/*.tar.gz", "dist/*.zip", "dist/SHA256SUMS"):
            self.assertIn(pattern, release)  # every archive, and the sums
        self.assertIn("gh release create", release)
        self.assertIn("--verify-tag", release)
        self.assertLess(release.index("attest-build-provenance"), release.index("gh release create"))
        self.assertIn("--python-zips", self.jobs["build"])  # the Windows archives carry their Python
        self.assertIn("cmp ", self.jobs["build"])  # and the build is made twice and compared

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


if __name__ == "__main__":
    unittest.main()
