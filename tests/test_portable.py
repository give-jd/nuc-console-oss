"""The portable run (run.sh, run.cmd/run.ps1) and the updater (src/update.py and the bin/nuc-console-update wrappers).

The logic of the updater is in src/update.py and is tested here once, with the network and `gh` injected; the wrappers are run
for real (Linux, macOS) with a stand-in curl that serves a fake release, so no test touches the network or the installed system.
"""
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
import update  # noqa: E402

WINDOWS = sys.platform == "win32"
VERSION = update.read_version(os.path.join(ROOT, "src"))
NEW = "99.1.0"
SUFFIXES = ("linux-x86_64.tar.gz", "linux-arm64.tar.gz", "macos-arm64.tar.gz", "macos-x86_64.tar.gz", "windows-x64.zip", "windows-arm64.zip")
NAMES = ["nuc-console-%s-%s" % (NEW, s) for s in SUFFIXES]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
    if mode is not None:
        os.chmod(path, mode)


def set_version(text, version):
    return re.sub(r'^VERSION = "[^"]+"', 'VERSION = "%s"' % version, text, count=1, flags=re.M)


class Sym(str):
    """The data of a file of make_archive that is a symbolic link: its target."""


def make_archive(kind, top, files):
    """-> bytes of a .tar.gz or .zip with one top folder; files = {relative path: (bytes or Sym, mode)}."""
    raw = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, (data, mode) in sorted(files.items()):
                z.writestr("%s/%s" % (top, rel), data)
    else:
        with tarfile.open(fileobj=raw, mode="w:gz") as t:
            for rel, (data, mode) in sorted(files.items()):
                info = tarfile.TarInfo("%s/%s" % (top, rel))
                info.mode, info.mtime = mode, 1700000000
                if isinstance(data, Sym):
                    info.type, info.linkname = tarfile.SYMTYPE, str(data)
                    t.addfile(info)
                else:
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def release_files(version, extra=None):
    """What a release archive holds (a small stand-in), with the real src/update.py and nuc_config.py (version changed)."""
    files = {
        "src/nuc_config.py": (set_version(read(os.path.join(ROOT, "src", "nuc_config.py")), version).encode(), 0o644),
        "src/update.py": (read(os.path.join(ROOT, "src", "update.py")).encode(), 0o644),
        "src/newmodule.py": (b"# new in %s\n" % version.encode(), 0o644),
        "bin/nuc-console-update": (read(os.path.join(ROOT, "bin", "nuc-console-update")).encode(), 0o755),
        "run.sh": (read(os.path.join(ROOT, "run.sh")).encode(), 0o755),
        "config/config.ini": (b"[features]\n", 0o644),
        "docs/NEW.md": (b"new\n", 0o644),
    }
    files.update(extra or {})
    return files


def release_json(version, names, host="github.com", tag=None):
    return {"tag_name": tag or "v" + version, "draft": False, "prerelease": False,
            "assets": [{"name": n, "browser_download_url": "https://%s/give-jd/nuc-console-oss/releases/download/v%s/%s" % (host, version, n)}
                       for n in names]}


class Downloader:
    """Stands in for the network: serves bytes by the file name of the URL, remembers what was asked."""

    def __init__(self, files):
        self.files, self.urls = dict(files), []

    def __call__(self, url, dest):
        self.urls.append(url)
        name = url.rsplit("/", 1)[-1]
        if name not in self.files:
            raise update.UpdateError("download failed: %s" % url)
        with open(dest, "wb") as f:
            f.write(self.files[name])

    def asked(self, name):
        return [u for u in self.urls if u.endswith("/" + name)]


class World:
    """A release NEW with its archives for `os_name`, served by a Downloader."""

    def __init__(self, os_name="linux", arch="x86_64", version=NEW, tamper=None, extra=None):
        self.os_name, self.arch, self.version = os_name, arch, version
        self.name = update.archive_name(version, os_name, arch)
        kind = "zip" if self.name.endswith(".zip") else "tar"
        self.archive = make_archive(kind, "nuc-console-" + version, release_files(version, extra))
        served = tamper(self.archive) if tamper else self.archive
        names = [self.name, update.SUMS]
        self.release = release_json(version, names)
        sums = ("%s  %s\n" % (sha(self.archive), self.name)).encode()
        self.net = Downloader({self.name: served, update.SUMS: sums})
        self.messages = []

    def run(self, app, cache, mode="installed", root="", **kw):
        kw.setdefault("attest", lambda path: ("missing", "provenance not checked: gh is not installed"))
        kw.setdefault("yes", True)
        return update.run_update(self.release, app, cache, mode, root, self.os_name, self.arch, download=self.net,
                                 say=self.messages.append, **kw)


def old_tree(root, version="1.0.0"):
    """An old portable folder."""
    write(os.path.join(root, "src", "nuc_config.py"), 'VERSION = "%s"\n' % version)
    write(os.path.join(root, "src", "oldmodule.py"), "# gone in the new release\n")
    write(os.path.join(root, "bin", "nuc-console-update"), "#!/bin/sh\n# old\n", 0o755)
    write(os.path.join(root, "docs", "OLD.md"), "old\n")
    write(os.path.join(root, "run.sh"), "#!/bin/sh\n# old\n", 0o755)
    write(os.path.join(root, "data", "config.ini"), "[features]\nmap = no\n")
    write(os.path.join(root, "data", "lib", "baseline.json"), '{"ports": []}')
    write(os.path.join(root, "notes.txt"), "mine\n")


class Versions(unittest.TestCase):
    def test_numbers_not_text(self):
        self.assertTrue(update.newer("1.10.0", "1.9.9"))
        self.assertTrue(update.newer("2.0.0", "1.99.99"))
        self.assertTrue(update.newer("1.4.1", "1.4.0"))
        self.assertFalse(update.newer("1.9.9", "1.10.0"))

    def test_equal_is_not_newer(self):
        self.assertFalse(update.newer("1.4.0", "1.4.0"))
        self.assertFalse(update.newer("v1.4.0", "1.4.0"))
        self.assertFalse(update.newer("1.4", "1.4.0"))

    def test_parse(self):
        self.assertEqual(update.parse_version("v1.10.2"), (1, 10, 2))
        self.assertEqual(update.parse_version(" 1.10 "), (1, 10, 0))
        self.assertEqual(update.parse_version("2"), (2, 0, 0))

    def test_what_is_not_a_version_is_refused(self):
        for bad in ("", "latest", "1.x.0", "1.2.3.4", "v", "1.2.3-rc1", None, 5, "1..2"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                update.parse_version(bad)

    def test_installed_version_is_read_as_text(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "nuc_config.py"), 'import os\nraise SystemExit("must not run")\nVERSION = "3.2.1"  # x\n')
            self.assertEqual(update.read_version(d), "3.2.1")
            write(os.path.join(d, "nuc_config.py"), "x = 1\n")
            with self.assertRaises(update.UpdateError):
                update.read_version(d)
            with self.assertRaises(update.UpdateError):
                update.read_version(os.path.join(d, "nowhere"))

    def test_the_real_version_is_a_plain_x_y_z(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")


class Assets(unittest.TestCase):
    def pick(self, os_name, arch, version="1.5.0", names=None):
        every = ["nuc-console-%s-%s" % (version, s) for s in SUFFIXES]
        return update.pick_asset(release_json(version, (every if names is None else names) + [update.SUMS]), os_name, arch)

    def test_linux_and_macos_have_one_archive_per_processor(self):
        for os_name in ("linux", "Linux"):
            for arch in ("x86_64", "AMD64", "amd64", "x64"):
                self.assertEqual(self.pick(os_name, arch)["name"], "nuc-console-1.5.0-linux-x86_64.tar.gz", (os_name, arch))
            for arch in ("aarch64", "arm64", "ARM64", "AARCH64"):
                self.assertEqual(self.pick(os_name, arch)["name"], "nuc-console-1.5.0-linux-arm64.tar.gz", (os_name, arch))
        for os_name in ("darwin", "macos", "Darwin"):
            for arch in ("x86_64", "amd64"):
                self.assertEqual(self.pick(os_name, arch)["name"], "nuc-console-1.5.0-macos-x86_64.tar.gz", (os_name, arch))
            for arch in ("arm64", "aarch64"):
                self.assertEqual(self.pick(os_name, arch)["name"], "nuc-console-1.5.0-macos-arm64.tar.gz", (os_name, arch))

    def test_windows_per_processor(self):
        for arch in ("AMD64", "amd64", "x86_64", "x64"):
            self.assertEqual(self.pick("windows", arch)["name"], "nuc-console-1.5.0-windows-x64.zip")
        for arch in ("ARM64", "arm64", "aarch64"):
            self.assertEqual(self.pick("win32", arch)["name"], "nuc-console-1.5.0-windows-arm64.zip")
        for arch in ("x86", "ia64", "", None):
            with self.assertRaises(update.UpdateError):
                self.pick("windows", arch)

    def test_every_system_and_processor_gets_a_different_archive_of_the_six(self):
        got = {(o, a): self.pick(o, a)["name"] for o in ("linux", "macos", "windows") for a in ("x86_64", "arm64")}
        self.assertEqual(len(set(got.values())), 6)
        self.assertEqual(sorted(got.values()), sorted("nuc-console-1.5.0-%s" % s for s in SUFFIXES))

    def test_a_processor_without_an_archive_is_refused_with_a_clear_message(self):
        for os_name in ("linux", "macos", "windows"):
            for arch in ("armv7l", "i686", "x86", "riscv64", "ppc64le", "s390x", "", None):
                with self.assertRaisesRegex(update.UpdateError, "no nuc-console archive for", msg=(os_name, arch)) as cm:
                    self.pick(os_name, arch)
                self.assertIn("64-bit", str(cm.exception))
        with self.assertRaisesRegex(update.UpdateError, r"Linux on the processor 'armv7l'.*x86_64.*arm64"):
            self.pick("linux", "armv7l")

    def test_a_release_that_has_no_archive_for_this_processor_says_which_ones_it_has(self):
        names = ["nuc-console-1.5.0-linux-x86_64.tar.gz", "nuc-console-1.5.0-linux-arm64.tar.gz", "nuc-console-1.5.0-macos-arm64.tar.gz",
                 "nuc-console-1.5.0-windows-x64.zip"]
        with self.assertRaises(update.UpdateError) as cm:
            self.pick("macos", "x86_64", names=names)  # the Intel Mac archive is missing
        msg = str(cm.exception)
        self.assertIn("nuc-console-1.5.0-macos-x86_64.tar.gz", msg)
        self.assertIn("for this system it has nuc-console-1.5.0-macos-arm64.tar.gz", msg)
        self.assertNotIn("linux", msg)
        # the old single archive of a system (no processor in the name) is not one of these
        with self.assertRaises(update.UpdateError):
            self.pick("linux", "x86_64", names=["nuc-console-1.5.0-linux.tar.gz"])

    def test_result_carries_version_urls_and_sums(self):
        a = self.pick("linux", "x86_64", "1.10.0")
        self.assertEqual(a["version"], "1.10.0")
        self.assertEqual(a["url"], "https://github.com/give-jd/nuc-console-oss/releases/download/v1.10.0/nuc-console-1.10.0-linux-x86_64.tar.gz")
        self.assertTrue(a["sums_url"].endswith("/SHA256SUMS"))

    def test_an_unknown_system_is_refused(self):
        with self.assertRaises(update.UpdateError):
            self.pick("freebsd", "x86_64")

    def test_a_release_without_the_archive_or_the_sums_is_refused(self):
        with self.assertRaises(update.UpdateError):
            self.pick("macos", "arm64", names=["nuc-console-1.5.0-linux-arm64.tar.gz"])  # only linux so far
        rel = release_json("1.5.0", ["nuc-console-1.5.0-linux-x86_64.tar.gz"])
        with self.assertRaises(update.UpdateError):
            update.pick_asset(rel, "linux", "x86_64")  # no SHA256SUMS

    def test_only_https_and_only_github(self):
        for url in ("http://github.com/x/a.tar.gz", "ftp://github.com/a", "https://evil.example/a.tar.gz", "file:///etc/passwd", ""):
            rel = release_json("1.5.0", ["nuc-console-1.5.0-linux-x86_64.tar.gz", update.SUMS])
            rel["assets"][0]["browser_download_url"] = url
            with self.assertRaises(update.UpdateError, msg=url):
                update.pick_asset(rel, "linux", "x86_64")

    def test_the_tag_must_be_x_y_z(self):
        for tag in ("latest", "v1.5", "v1.5.0-rc1", "nightly", ""):
            with self.assertRaises(update.UpdateError, msg=tag):
                update.pick_asset(release_json("1.5.0", NAMES, tag=tag), "linux", "x86_64")
        self.assertEqual(update.release_version({"tag_name": "1.5.0"}), "1.5.0")
        with self.assertRaises(update.UpdateError):
            update.release_version({"name": "no tag"})
        with self.assertRaises(update.UpdateError):
            update.release_version([])

    def test_https_only(self):
        self.assertEqual(update.https_only("https://x.example/a"), "https://x.example/a")
        for url in ("http://x.example/a", "https:///a", "//x.example", None):
            with self.assertRaises(update.UpdateError):
                update.https_only(url)

    def test_the_archive_names_are_the_ones_the_release_build_makes(self):
        path = os.path.join(ROOT, "tools", "build_release.py")
        if not os.path.exists(path):
            self.skipTest("tools/ is not in this tree")
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_release", path)
        br = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(br)
        made = br.archive_names("1.5.0")
        wanted = {(o, a): update.archive_name("1.5.0", o, a) for o in ("linux", "macos", "windows") for a in ("x86_64", "arm64")}
        self.assertEqual(sorted(wanted.values()), sorted(made))  # the six names of the build are the six the updater asks for


class Sums(unittest.TestCase):
    H1, H2 = "a" * 64, "B" * 64

    def test_parse(self):
        text = "%s  nuc-console-1.5.0-linux.tar.gz\n%s *nuc-console-1.5.0-macos.tar.gz\n# a comment\n\nnot a hash line\n%s  x\n" % (
            self.H1, self.H2, "c" * 63)
        self.assertEqual(update.parse_sha256sums(text), {"nuc-console-1.5.0-linux.tar.gz": self.H1, "nuc-console-1.5.0-macos.tar.gz": self.H2.lower()})

    def test_crlf_and_tabs(self):
        self.assertEqual(update.parse_sha256sums("%s\tf.zip\r\n" % self.H1), {"f.zip": self.H1})

    def test_two_different_hashes_for_one_name_are_refused(self):
        with self.assertRaises(update.UpdateError):
            update.parse_sha256sums("%s  f\n%s  f\n" % (self.H1, "d" * 64))
        self.assertEqual(update.parse_sha256sums("%s  f\n%s  f\n" % (self.H1, self.H1)), {"f": self.H1})

    def test_empty(self):
        self.assertEqual(update.parse_sha256sums(""), {})

    def test_verify_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f")
            write(p, b"hello")
            good = sha(b"hello")
            self.assertTrue(update.verify_file(p, good))
            self.assertTrue(update.verify_file(p, good.upper()))
            self.assertFalse(update.verify_file(p, sha(b"hellO")))
            self.assertFalse(update.verify_file(p, "0" * 64))
            self.assertFalse(update.verify_file(os.path.join(d, "missing"), good))
            self.assertFalse(update.verify_file(d, good))  # a folder

    def test_the_real_sha256sum_format_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "a.bin"), b"abc")
            line = "%s  a.bin\n" % sha(b"abc")
            self.assertEqual(update.parse_sha256sums(line)["a.bin"], sha(b"abc"))


class Cache(unittest.TestCase):
    def test_a_good_file_in_the_cache_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as d:
            data = b"archive"
            write(os.path.join(d, "a.tar.gz"), data)
            net = Downloader({"a.tar.gz": b"something else"})
            path, downloaded = update.ensure_cached(d, "a.tar.gz", sha(data), "https://github.com/x/a.tar.gz", net, say=lambda m: None)
            self.assertEqual((path, downloaded), (os.path.join(d, "a.tar.gz"), False))
            self.assertEqual(net.urls, [])
            self.assertEqual(read(path), "archive")

    def test_a_missing_file_is_downloaded_once_then_reused(self):
        with tempfile.TemporaryDirectory() as d:
            data = b"archive"
            net = Downloader({"a.tar.gz": data})
            args = (d, "a.tar.gz", sha(data), "https://github.com/x/a.tar.gz", net)
            self.assertTrue(update.ensure_cached(*args, say=lambda m: None)[1])
            self.assertFalse(update.ensure_cached(*args, say=lambda m: None)[1])
            self.assertEqual(len(net.urls), 1)
            self.assertEqual(os.listdir(d), ["a.tar.gz"])  # no .part left

    def test_a_cache_folder_that_does_not_exist_yet_is_created(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "a", "b")
            net = Downloader({"f": b"x"})
            update.ensure_cached(cache, "f", sha(b"x"), "https://github.com/x/f", net, say=lambda m: None)
            self.assertTrue(os.path.isfile(os.path.join(cache, "f")))

    def test_a_corrupt_or_partial_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            data = b"the right archive"
            write(os.path.join(d, "a.zip"), data[:5])  # cut short
            write(os.path.join(d, "a.zip.part"), b"leftover")
            net = Downloader({"a.zip": data})
            path, downloaded = update.ensure_cached(d, "a.zip", sha(data), "https://github.com/x/a.zip", net, say=lambda m: None)
            self.assertTrue(downloaded)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), data)
            self.assertEqual(os.listdir(d), ["a.zip"])

    def test_a_download_that_does_not_match_is_deleted_and_refused(self):
        with tempfile.TemporaryDirectory() as d:
            net = Downloader({"a.zip": b"tampered"})
            with self.assertRaises(update.UpdateError) as cm:
                update.ensure_cached(d, "a.zip", sha(b"the right one"), "https://github.com/x/a.zip", net, say=lambda m: None)
            self.assertIn("not installed", str(cm.exception))
            self.assertEqual(os.listdir(d), [])  # neither the file nor a .part

    def test_a_bad_copy_in_the_cache_is_not_trusted_either(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "a.zip"), b"tampered in the cache")
            net = Downloader({"a.zip": b"tampered again"})
            with self.assertRaises(update.UpdateError):
                update.ensure_cached(d, "a.zip", sha(b"the right one"), "https://github.com/x/a.zip", net, say=lambda m: None)
            self.assertEqual(os.listdir(d), [])

    def test_a_failed_download_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            def boom(url, dest):
                write(dest, b"half")
                raise update.UpdateError("network down")
            with self.assertRaises(update.UpdateError):
                update.ensure_cached(d, "a.zip", sha(b"x"), "https://github.com/x/a.zip", boom, say=lambda m: None)
            self.assertEqual(os.listdir(d), [])

    def test_only_https_urls_are_fetched(self):
        with tempfile.TemporaryDirectory() as d:
            net = Downloader({"a.zip": b"x"})
            with self.assertRaises(update.UpdateError):
                update.ensure_cached(d, "a.zip", sha(b"x"), "http://github.com/x/a.zip", net, say=lambda m: None)
            self.assertEqual(net.urls, [])

    def test_unsafe_names_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("../evil", "a/b", "", ".hidden", "/etc/passwd"):
                with self.assertRaises(update.UpdateError, msg=name):
                    update.ensure_cached(d, name, sha(b"x"), "https://github.com/x/f", Downloader({}), say=lambda m: None)

    @unittest.skipIf(WINDOWS, "folder modes")
    def test_the_cache_folder_is_created_for_root_and_nobody_else_to_write(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "cache")
            old = os.umask(0o077)
            try:
                update.prepare_cache(cache)
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE(os.stat(cache).st_mode), 0o755)  # install-macos.sh wants exactly this
            update.prepare_cache(cache)  # again: fine

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "only root is that strict")
    def test_root_refuses_a_cache_others_can_write_or_that_is_a_link(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "cache")
            os.mkdir(cache)
            os.chmod(cache, 0o777)
            with self.assertRaises(update.UpdateError):
                update.prepare_cache(cache)
            link = os.path.join(d, "link")
            os.mkdir(os.path.join(d, "real"))
            os.symlink(os.path.join(d, "real"), link)
            with self.assertRaises(update.UpdateError):
                update.prepare_cache(link)

    def test_old_archives_go_but_other_files_of_the_cache_stay(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("nuc-console-1.0.0-linux.tar.gz", "nuc-console-1.5.0-linux.tar.gz", "nuc-console-1.0.0-windows-x64.zip",
                      "nuc-console-1.0.0-windows-arm64.zip.part", "python-3.14.8-embed-amd64.zip", "python-3.14.8-macos11.pkg", "SHA256SUMS"):
                write(os.path.join(d, n), b"x")
            update.prune_cache(d, "nuc-console-1.5.0-linux.tar.gz")
            self.assertEqual(sorted(os.listdir(d)), ["SHA256SUMS", "nuc-console-1.5.0-linux.tar.gz", "python-3.14.8-embed-amd64.zip",
                                                     "python-3.14.8-macos11.pkg"])

    def test_the_archives_of_every_processor_are_old_archives_too(self):
        with tempfile.TemporaryDirectory() as d:
            for suffix in SUFFIXES:
                write(os.path.join(d, "nuc-console-1.4.9-" + suffix), b"x")
                write(os.path.join(d, "nuc-console-1.5.0-" + suffix + ".part"), b"x")
            write(os.path.join(d, "nuc-console-1.5.0-linux-x86_64.tar.gz"), b"x")
            write(os.path.join(d, "cpython-3.13.5+20250708-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"), b"not ours")
            update.prune_cache(d, "nuc-console-1.5.0-linux-x86_64.tar.gz")
            self.assertEqual(sorted(os.listdir(d)), ["cpython-3.13.5+20250708-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
                                                     "nuc-console-1.5.0-linux-x86_64.tar.gz"])


class Attestation(unittest.TestCase):
    def go(self, which, run=None):
        with mock.patch.object(update, "trusted_tool", return_value=True):
            return update.gh_attest("/x/a.tar.gz", which=which, run=run)

    def result(self, rc, out="", err=""):
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, out, err)

    def test_without_gh_it_says_so(self):
        status, msg = self.go(lambda n: None)
        self.assertEqual(status, "missing")
        self.assertIn("not checked", msg)

    def test_verified(self):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "Loaded 1 attestation", "")
        self.assertEqual(self.go(lambda n: "/usr/bin/gh", run)[0], "ok")
        self.assertEqual(seen, [["/usr/bin/gh", "attestation", "verify", "/x/a.tar.gz", "--repo", "give-jd/nuc-console-oss"]])

    def test_a_failure_is_a_failure(self):
        status, msg = self.go(lambda n: "/usr/bin/gh", self.result(1, "", "X Verification failed: no matching attestations"))
        self.assertEqual(status, "failed")
        self.assertIn("no matching", msg)

    def test_gh_without_a_login_cannot_check_it_says_so(self):
        status, msg = self.go(lambda n: "/usr/bin/gh", self.result(4, "", "To get started with GitHub CLI, please run:  gh auth login"))
        self.assertEqual(status, "noauth")
        self.assertIn("not checked", msg)

    def test_a_timeout_refuses(self):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 180)
        self.assertEqual(self.go(lambda n: "/usr/bin/gh", run)[0], "failed")

    def test_gh_that_cannot_run(self):
        def run(cmd, **kw):
            raise OSError("exec format error")
        self.assertEqual(self.go(lambda n: "/usr/bin/gh", run)[0], "missing")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "root only")
    def test_root_does_not_run_a_gh_that_is_not_roots(self):
        with tempfile.TemporaryDirectory() as d:
            gh = os.path.join(d, "gh")
            write(gh, "#!/bin/sh\nexit 0\n", 0o777)
            status, msg = update.gh_attest("/x/a.tar.gz", which=lambda n: gh, run=lambda *a, **k: self.fail("it must not run"))
            self.assertEqual(status, "missing")


class Download(unittest.TestCase):
    def test_curl_is_asked_for_https_only(self):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(update, "WINDOWS", False), mock.patch.object(update, "trusted_tool", return_value=True):
            update.https_download("https://github.com/x/a", "/tmp/dest", run=run, which=lambda n: "/usr/bin/curl")
        cmd = seen[0]
        self.assertEqual(cmd[0], "/usr/bin/curl")
        self.assertEqual(cmd[cmd.index("--proto") + 1], "=https")
        self.assertEqual(cmd[cmd.index("--proto-redir") + 1], "=https")
        self.assertEqual(cmd[-1], "https://github.com/x/a")
        self.assertEqual(cmd[cmd.index("-o") + 1], "/tmp/dest")

    def test_curl_failure_is_an_error(self):
        with mock.patch.object(update, "WINDOWS", False), mock.patch.object(update, "trusted_tool", return_value=True):
            with self.assertRaises(update.UpdateError):
                update.https_download("https://github.com/x/a", "/tmp/dest", which=lambda n: "/usr/bin/curl",
                                      run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 22, "", "404"))

    def test_http_is_not_even_tried(self):
        for fn in (update.https_download,):
            with self.assertRaises(update.UpdateError):
                fn("http://github.com/x/a", "/tmp/dest", run=lambda *a, **k: self.fail("no"), which=lambda n: "/usr/bin/curl")

    def test_a_redirect_to_http_is_refused(self):
        h = update._HttpsRedirect()
        with self.assertRaises(update.UpdateError):
            h.redirect_request(mock.Mock(), None, 302, "Found", {}, "http://evil.example/a")


class Extract(unittest.TestCase):
    def tar(self, d, members):
        path = os.path.join(d, "a.tar.gz")
        with tarfile.open(path, "w:gz") as t:
            for info, data in members:
                t.addfile(info, io.BytesIO(data) if data is not None else None)
        return path

    def info(self, name, size=0, mode=0o644, kind=tarfile.REGTYPE, link=""):
        i = tarfile.TarInfo(name)
        i.size, i.mode, i.type, i.linkname = size, mode, kind, link
        return i

    def test_a_good_tar_and_a_good_zip(self):
        with tempfile.TemporaryDirectory() as d:
            for kind, ext in (("tar", "tar.gz"), ("zip", "zip")):
                path = os.path.join(d, "a." + ext)
                write(path, make_archive(kind, "nuc-console-1.0.0", {"src/a.py": (b"x", 0o644), "run.sh": (b"y", 0o755)}))
                out = os.path.join(d, "out-" + kind)
                top = update.extract(path, out)
                self.assertEqual(top, os.path.join(out, "nuc-console-1.0.0"))
                self.assertEqual(read(os.path.join(top, "src", "a.py")), "x")

    @unittest.skipIf(WINDOWS, "modes")
    def test_modes_are_two_plain_ones(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.tar.gz")
            write(path, make_archive("tar", "t", {"a": (b"x", 0o4777), "b": (b"y", 0o666), "c": (b"z", 0o700)}))
            top = update.extract(path, os.path.join(d, "out"))
            modes = {n: stat.S_IMODE(os.stat(os.path.join(top, n)).st_mode) for n in "abc"}
            self.assertEqual(modes, {"a": 0o755, "b": 0o644, "c": 0o755})

    def test_paths_that_escape_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("../evil", "t/../../evil", "/abs/evil", "t/../../../etc/x"):
                path = self.tar(d, [(self.info(name, 1), b"x")])
                with self.assertRaises(update.UpdateError, msg=name):
                    update.extract(path, os.path.join(d, "out"))
            self.assertFalse(os.path.exists(os.path.join(d, "evil")))
            zpath = os.path.join(d, "z.zip")
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("t/../../evil.txt", "x")
            with self.assertRaises(update.UpdateError):
                update.extract(zpath, os.path.join(d, "out2"))
            self.assertFalse(os.path.exists(os.path.join(d, "evil.txt")))

    def test_links_and_devices_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for kind, link in ((tarfile.SYMTYPE, "/etc/passwd"), (tarfile.LNKTYPE, "t/a"), (tarfile.FIFOTYPE, "")):
                path = self.tar(d, [(self.info("t/a", 1), b"x"), (self.info("t/l", 0, kind=kind, link=link), None)])
                with self.assertRaises(update.UpdateError):
                    update.extract(path, os.path.join(d, "out"))

    @unittest.skipIf(WINDOWS, "symbolic links")
    def test_a_link_inside_the_folder_is_extracted_as_a_link(self):
        """The Python of the Linux and macOS archives: python/bin/python3 -> python3.13."""
        with tempfile.TemporaryDirectory() as d:
            path = self.tar(d, [(self.info("t/python/bin/python3.13", 1, 0o755), b"x"),
                                (self.info("t/python/bin/python3", 0, 0o777, tarfile.SYMTYPE, "python3.13"), None),
                                (self.info("t/python/bin/python", 0, 0o777, tarfile.SYMTYPE, "python3"), None),
                                (self.info("t/python/lib/libx.so", 0, 0o777, tarfile.SYMTYPE, "../lib64/libx.so.1"), None),
                                (self.info("t/python/lib64/libx.so.1", 1), b"y")])
            top = update.extract(path, os.path.join(d, "out"))
            link = os.path.join(top, "python", "bin", "python3")
            self.assertTrue(os.path.islink(link))
            self.assertEqual(os.readlink(link), "python3.13")
            self.assertEqual(read(os.path.join(top, "python", "bin", "python")), "x")  # a link to a link
            self.assertEqual(read(os.path.join(top, "python", "lib", "libx.so")), "y")
            self.assertTrue(os.stat(os.path.join(top, "python", "bin", "python3.13")).st_mode & stat.S_IXUSR)

    def test_a_link_that_leaves_the_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for name, target in (("t/l", "../x"), ("t/a/l", "../../x"), ("t/a/l", "../../../etc/passwd"), ("t/l", "/etc/passwd"),
                                 ("t/l", "\\\\server\\share"), ("t/l", "C:\\x"), ("t/l", ""), ("t/l", ".."), ("t/a/l", "../.."), ("t/l", "a/../../x")):
                path = self.tar(d, [(self.info("t/a/f", 1), b"x"), (self.info(name, 0, 0o777, tarfile.SYMTYPE, target), None)])
                with self.assertRaises(update.UpdateError, msg=(name, target)):
                    update.extract(path, os.path.join(d, "out"))
            self.assertFalse(os.path.exists(os.path.join(d, "x")))

    def test_one_top_folder(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.tar(d, [(self.info("one/a", 1), b"x"), (self.info("two/b", 1), b"y")])
            with self.assertRaises(update.UpdateError):
                update.extract(path, os.path.join(d, "out"))


class Flow(unittest.TestCase):
    """run_update: the whole decision, with the network and gh injected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "folder")
        self.cache = os.path.join(self.root, "cache")
        old_tree(self.root)

    def portable(self, world, **kw):
        return world.run(os.path.join(self.root, "src"), self.cache, "portable", self.root, **kw)

    def unchanged(self):
        self.assertEqual(read(os.path.join(self.root, "src", "nuc_config.py")), 'VERSION = "1.0.0"\n')
        self.assertTrue(os.path.exists(os.path.join(self.root, "src", "oldmodule.py")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "src", "newmodule.py")))

    def test_up_to_date_does_nothing_and_asks_nothing(self):
        world = World(version="1.0.0")
        self.assertEqual(self.portable(world), 0)
        self.assertEqual(world.messages, ["already at 1.0.0"])
        self.assertEqual(world.net.urls, [])
        self.assertFalse(os.path.exists(self.cache))

    def test_a_release_older_than_this_does_nothing(self):
        world = World(version="0.9.0")
        self.assertEqual(self.portable(world), 0)
        self.assertTrue(world.messages[0].startswith("already at 1.0.0"))
        self.assertEqual(world.net.urls, [])

    def test_check_only_reports(self):
        world = World()
        self.assertEqual(self.portable(world, check=True), 0)
        self.assertTrue(any("update available: 1.0.0 -> %s" % NEW in m for m in world.messages), world.messages)
        self.assertEqual(world.net.urls, [])
        self.assertFalse(os.path.exists(self.cache))
        self.unchanged()

    def test_a_pre_release_or_a_draft_is_ignored(self):
        for flag in ("prerelease", "draft"):
            world = World()
            world.release[flag] = True
            with self.assertRaises(update.UpdateError):
                self.portable(world)
            self.unchanged()

    def test_a_git_checkout_is_never_overwritten(self):
        # a clone has run.sh and src/ too: the files of the release would replace the ones git tracks
        for kind in ("folder", "file"):  # a worktree has a .git file
            git = os.path.join(self.root, ".git")
            if kind == "folder":
                os.mkdir(git)
            else:
                shutil.rmtree(git)
                write(git, "gitdir: /somewhere/else\n")
            for check in (False, True):
                world = World()
                with self.assertRaises(update.UpdateError) as ctx:
                    self.portable(world, check=check)
                self.assertIn("git pull", str(ctx.exception))
                self.assertEqual(world.net.urls, [])
                self.assertFalse(os.path.exists(self.cache))
                self.unchanged()
        os.remove(os.path.join(self.root, ".git"))
        self.assertEqual(self.portable(World()), 0)  # without it, the same folder is updated
        installed = os.path.join(self.tmp.name, "installed")  # an installed copy is not a clone, whatever sits beside it
        old_tree(installed)
        os.mkdir(os.path.join(installed, ".git"))
        self.assertEqual(World().run(os.path.join(installed, "src"), os.path.join(self.tmp.name, "cache2"), "installed",
                                     stage_file=os.path.join(self.tmp.name, "stage")), 0)

    def test_not_confirmed_does_nothing(self):
        world = World()
        asked = []
        self.assertEqual(self.portable(world, yes=False, confirm=lambda q: asked.append(q) or False), 0)
        self.assertEqual(len(asked), 1)
        self.assertIn(NEW, asked[0])
        self.assertEqual(world.net.urls, [])
        self.unchanged()

    def test_confirmed_updates(self):
        world = World()
        self.assertEqual(self.portable(world, yes=False, confirm=lambda q: True), 0)
        self.assertEqual(read(os.path.join(self.root, "src", "nuc_config.py")).count('VERSION = "%s"' % NEW), 1)

    def test_without_a_terminal_it_refuses_to_guess(self):
        world = World()
        with mock.patch.object(update.sys, "stdin", io.StringIO("")):
            with self.assertRaises(update.UpdateError) as cm:
                self.portable(world, yes=False)
        self.assertIn("--yes", str(cm.exception))
        self.unchanged()

    def test_portable_replaces_the_code_and_keeps_the_data(self):
        world = World()
        self.assertEqual(self.portable(world), 0)
        r = self.root
        self.assertIn('VERSION = "%s"' % NEW, read(os.path.join(r, "src", "nuc_config.py")))
        self.assertTrue(os.path.exists(os.path.join(r, "src", "newmodule.py")))
        self.assertFalse(os.path.exists(os.path.join(r, "src", "oldmodule.py")))  # dropped by the new release
        self.assertFalse(os.path.exists(os.path.join(r, "docs", "OLD.md")))
        self.assertEqual(read(os.path.join(r, "docs", "NEW.md")), "new\n")
        self.assertEqual(read(os.path.join(r, "data", "config.ini")), "[features]\nmap = no\n")  # the user's
        self.assertEqual(read(os.path.join(r, "data", "lib", "baseline.json")), '{"ports": []}')
        self.assertEqual(read(os.path.join(r, "notes.txt")), "mine\n")  # not ours: untouched
        self.assertEqual(sorted(os.listdir(self.cache)), sorted([world.name, "SHA256SUMS"]))  # stage folder is gone
        self.assertFalse([f for dp, _d, fs in os.walk(r) for f in fs if f.endswith(".new")])

    def test_a_failure_while_copying_changes_nothing(self):
        world = World()
        real, calls = shutil.copyfile, []

        def flaky(src, dst, **kw):
            calls.append(dst)
            if len(calls) == 3:
                raise OSError(28, "No space left on device")
            return real(src, dst, **kw)
        with mock.patch.object(update.shutil, "copyfile", flaky):
            with self.assertRaises(OSError):
                self.portable(world)
        self.assertGreaterEqual(len(calls), 3)
        self.unchanged()
        self.assertEqual(read(os.path.join(self.root, "docs", "OLD.md")), "old\n")
        self.assertFalse([f for dp, _d, fs in os.walk(self.root) for f in fs if f.endswith(".new")])
        self.assertEqual([n for n in os.listdir(self.cache) if n.startswith("stage-")], [])

    @unittest.skipIf(WINDOWS, "exec bits")
    def test_exec_bits_follow_the_archive(self):
        world = World()
        self.portable(world)
        self.assertTrue(os.stat(os.path.join(self.root, "run.sh")).st_mode & stat.S_IXUSR)
        self.assertTrue(os.stat(os.path.join(self.root, "bin", "nuc-console-update")).st_mode & stat.S_IXUSR)
        self.assertFalse(os.stat(os.path.join(self.root, "src", "newmodule.py")).st_mode & stat.S_IXUSR)

    def test_data_and_cache_of_the_archive_are_never_unpacked(self):
        world = World(extra={"data/config.ini": (b"[features]\nmap = yes\n", 0o644), "cache/x": (b"x", 0o644)})
        self.portable(world)
        self.assertEqual(read(os.path.join(self.root, "data", "config.ini")), "[features]\nmap = no\n")
        self.assertFalse(os.path.exists(os.path.join(self.cache, "x")))

    def test_windows_python_zip_is_replaced_and_the_unpacked_python_stays(self):
        world = World("windows", "AMD64", extra={"python/python-9.9.9-embed-amd64.zip": (b"new zip", 0o644)})
        write(os.path.join(self.root, "python", "python-3.14.8-embed-amd64.zip"), b"old zip")
        write(os.path.join(self.root, "python", "python.exe"), b"unpacked")
        write(os.path.join(self.root, "python", "nuc-console-python.txt"), "stamp")
        self.portable(world)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "python"))),
                         ["nuc-console-python.txt", "python-9.9.9-embed-amd64.zip", "python.exe"])

    def new_python(self, minor="13"):
        return {"python/bin/python3.%s" % minor: (b"#!/bin/sh\n# python 3.%s\n" % minor.encode(), 0o755),
                "python/bin/python3": (Sym("python3.%s" % minor), 0o777),
                "python/bin/python": (Sym("python3"), 0o777),
                "python/lib/libpython3.%s.so.1.0" % minor: (b"lib" + minor.encode(), 0o755),
                "python/lib/libpython3.%s.so" % minor: (Sym("libpython3.%s.so.1.0" % minor), 0o777),
                "python/lib/python3.%s/os.py" % minor: (b"# os " + minor.encode() + b"\n", 0o644)}

    @unittest.skipIf(WINDOWS, "symbolic links")
    def test_the_python_of_a_linux_or_macos_archive_is_replaced_links_and_all(self):
        """Python 3.12 -> 3.13: the links point to the new files, the old Python is gone, data/ and what is not shipped stay."""
        write(os.path.join(self.root, "python", "bin", "python3.12"), "#!/bin/sh\n# python 3.12\n", 0o755)
        os.symlink("python3.12", os.path.join(self.root, "python", "bin", "python3"))
        os.symlink("python3", os.path.join(self.root, "python", "bin", "python"))
        write(os.path.join(self.root, "python", "lib", "python3.12", "os.py"), "# os 12\n")
        write(os.path.join(self.root, "python", "lib", "python3.12", "__pycache__", "os.pyc"), "stray")  # a Python that was run without -B
        os.symlink("libpython3.12.so.1.0", os.path.join(self.root, "python", "lib", "libpython3.12.so"))
        write(os.path.join(self.root, "python", "lib", "libpython3.12.so.1.0"), "lib12")
        world = World("linux", "x86_64", extra=self.new_python())
        self.portable(world)
        py = os.path.join(self.root, "python")
        self.assertEqual(os.readlink(os.path.join(py, "bin", "python3")), "python3.13")
        self.assertEqual(os.readlink(os.path.join(py, "bin", "python")), "python3")
        self.assertEqual(os.readlink(os.path.join(py, "lib", "libpython3.13.so")), "libpython3.13.so.1.0")
        self.assertEqual(read(os.path.join(py, "bin", "python")), "#!/bin/sh\n# python 3.13\n")  # through both links
        self.assertTrue(os.stat(os.path.join(py, "bin", "python3.13")).st_mode & stat.S_IXUSR)
        self.assertFalse(os.path.lexists(os.path.join(py, "lib", "libpython3.12.so")))  # the dangling old link went too
        self.assertEqual(sorted(os.listdir(os.path.join(py, "bin"))), ["python", "python3", "python3.13"])
        self.assertEqual(sorted(os.listdir(os.path.join(py, "lib"))), ["libpython3.13.so", "libpython3.13.so.1.0", "python3.13"])
        self.assertFalse(os.path.exists(os.path.join(py, "lib", "python3.12")))  # no stray folder of the old Python
        self.assertEqual(read(os.path.join(self.root, "data", "config.ini")), "[features]\nmap = no\n")
        self.assertEqual(read(os.path.join(self.root, "notes.txt")), "mine\n")
        self.assertFalse([f for dp, _d, fs in os.walk(self.root) for f in fs + _d if f.endswith(".new")])

    @unittest.skipIf(WINDOWS, "symbolic links")
    def test_an_unchanged_python_is_not_written_again_and_keeps_its_times(self):
        new = os.path.join(self.tmp.name, "new")
        archive = os.path.join(self.tmp.name, "a.tar.gz")
        write(archive, make_archive("tar", "nuc-console-9.0.0", release_files("9.0.0", self.new_python())))
        top = update.extract(archive, new)
        first = update.apply_portable(top, self.root, say=lambda m: None)
        self.assertGreater(first, 5)
        lib = os.path.join(self.root, "python", "lib", "python3.13", "os.py")
        self.assertEqual(os.stat(lib).st_mtime, 1700000000)  # the time of the archive: the .pyc files of a library may record it
        self.assertEqual(update.apply_portable(top, self.root, say=lambda m: None), 0)  # nothing to write: files and links are the same
        os.remove(os.path.join(self.root, "python", "bin", "python3"))  # a link that is gone is made again, and only it
        os.symlink("elsewhere", os.path.join(self.root, "python", "bin", "python3"))  # a link that points elsewhere is corrected
        self.assertEqual(update.apply_portable(top, self.root, say=lambda m: None), 1)
        self.assertEqual(os.readlink(os.path.join(self.root, "python", "bin", "python3")), "python3.13")

    @unittest.skipIf(WINDOWS, "symbolic links")
    def test_a_file_a_folder_and_a_link_may_change_places_between_releases(self):
        write(os.path.join(self.root, "python", "bin", "python3.13"), "old file")
        write(os.path.join(self.root, "python", "bin", "python3"), "was a file")  # now a link
        write(os.path.join(self.root, "python", "lib", "libpython3.13.so", "inside"), "was a folder")  # now a link
        world = World("linux", "x86_64", extra=self.new_python())
        self.portable(world)
        py = os.path.join(self.root, "python")
        self.assertTrue(os.path.islink(os.path.join(py, "bin", "python3")))
        self.assertTrue(os.path.islink(os.path.join(py, "lib", "libpython3.13.so")))
        self.assertEqual(read(os.path.join(py, "bin", "python3.13")), "#!/bin/sh\n# python 3.13\n")

    def test_a_folder_python_is_only_cleaned_when_the_release_ships_one(self):
        """A release without python/bin/python3 (Windows, a source tree) leaves the unpacked python/ of the run alone."""
        write(os.path.join(self.root, "python", "bin", "extra"), "mine")
        world = World("linux", "x86_64")
        self.portable(world)
        self.assertEqual(read(os.path.join(self.root, "python", "bin", "extra")), "mine")

    def test_the_second_update_does_not_download_the_archive_again(self):
        world = World()
        self.portable(world)
        self.assertEqual(len(world.net.asked(world.name)), 1)
        write(os.path.join(self.root, "src", "nuc_config.py"), 'VERSION = "1.0.0"\n')  # as if rolled back
        world.messages.clear()
        self.portable(world)
        self.assertEqual(len(world.net.asked(world.name)), 1)  # still the one download
        self.assertEqual(len(world.net.asked("SHA256SUMS")), 2)  # SHA256SUMS is what the cache is checked against: always fresh
        self.assertTrue(any("already in the cache" in m for m in world.messages), world.messages)
        self.assertIn('VERSION = "%s"' % NEW, read(os.path.join(self.root, "src", "nuc_config.py")))

    def test_a_cached_archive_with_a_wrong_hash_is_downloaded_again(self):
        world = World()
        os.makedirs(self.cache)
        write(os.path.join(self.cache, world.name), b"corrupt")
        self.portable(world)
        self.assertEqual(len(world.net.asked(world.name)), 1)

    def test_a_hash_mismatch_refuses_and_deletes(self):
        world = World(tamper=lambda b: b + b"x")
        with self.assertRaises(update.UpdateError) as cm:
            self.portable(world)
        self.assertIn("not installed", str(cm.exception))
        self.assertEqual(os.listdir(self.cache), ["SHA256SUMS"])  # the bad archive is gone
        self.unchanged()

    def test_an_archive_that_is_not_in_the_sums_is_refused(self):
        world = World()
        world.net.files["SHA256SUMS"] = ("%s  something-else.zip\n" % ("a" * 64)).encode()
        with self.assertRaises(update.UpdateError):
            self.portable(world)
        self.unchanged()

    def test_garbage_sums_are_refused(self):
        world = World()
        world.net.files["SHA256SUMS"] = b"<html>rate limited</html>"
        with self.assertRaises(update.UpdateError):
            self.portable(world)
        self.unchanged()

    def test_a_failed_attestation_refuses_before_anything_is_replaced(self):
        world = World()
        with self.assertRaises(update.UpdateError) as cm:
            self.portable(world, attest=lambda p: ("failed", "gh attestation verify failed: bad signature"))
        self.assertIn("not installed", str(cm.exception))
        self.unchanged()
        self.assertEqual([n for n in os.listdir(self.cache) if n.startswith("stage-")], [])

    def test_a_verified_attestation_and_a_missing_one_both_go_on_and_say_which(self):
        for status, msg in (("ok", "provenance verified"), ("missing", "provenance not checked: gh is not installed"),
                            ("noauth", "provenance not checked: gh is not logged in")):
            self.setUp()
            world = World()
            self.portable(world, attest=lambda p, s=status, m=msg: (s, m))
            self.assertIn(msg, world.messages)
            self.assertIn('VERSION = "%s"' % NEW, read(os.path.join(self.root, "src", "nuc_config.py")))

    def test_the_attestation_is_asked_about_the_cached_archive(self):
        world = World()
        seen = []
        self.portable(world, attest=lambda p: seen.append(p) or ("ok", "ok"))
        self.assertEqual(seen, [os.path.join(self.cache, world.name)])

    def test_an_archive_of_another_version_than_the_release_is_refused(self):
        world = World()
        wrong = make_archive("tar", "nuc-console-" + NEW, release_files("98.0.0"))
        world.net.files[world.name] = wrong
        world.net.files["SHA256SUMS"] = ("%s  %s\n" % (sha(wrong), world.name)).encode()
        with self.assertRaises(update.UpdateError) as cm:
            self.portable(world)
        self.assertIn("98.0.0", str(cm.exception))
        self.unchanged()

    def test_an_installed_one_hands_the_folder_to_the_wrapper(self):
        world = World()
        app = os.path.join(self.tmp.name, "app")
        write(os.path.join(app, "nuc_config.py"), 'VERSION = "1.0.0"\n')
        stage_file = os.path.join(self.tmp.name, "stage")
        cache = os.path.join(self.tmp.name, "cache")
        self.assertEqual(world.run(app, cache, "installed", stage_file=stage_file), 0)
        top = read(stage_file).strip()
        self.assertEqual(os.path.dirname(os.path.dirname(top)), cache)
        self.assertTrue(os.path.basename(os.path.dirname(top)).startswith("stage-"))
        self.assertEqual(os.path.basename(top), "nuc-console-" + NEW)
        self.assertTrue(os.path.exists(os.path.join(top, "src", "nuc_config.py")))
        self.assertEqual(read(os.path.join(app, "nuc_config.py")), 'VERSION = "1.0.0"\n')  # nothing replaced here: the installer does it

    def test_an_installed_one_needs_the_stage_file(self):
        world = World()
        app = os.path.join(self.tmp.name, "app")
        write(os.path.join(app, "nuc_config.py"), 'VERSION = "1.0.0"\n')
        with self.assertRaises(update.UpdateError):
            world.run(app, os.path.join(self.tmp.name, "cache"), "installed")

    def test_old_archives_are_pruned_after_an_update(self):
        world = World()
        os.makedirs(self.cache)
        write(os.path.join(self.cache, "nuc-console-1.0.0-linux.tar.gz"), b"old")
        write(os.path.join(self.cache, "python-3.14.8-embed-amd64.zip"), b"python")
        self.portable(world)
        self.assertEqual(sorted(os.listdir(self.cache)), sorted([world.name, "SHA256SUMS", "python-3.14.8-embed-amd64.zip"]))

    def test_stale_stage_folders_of_a_stopped_update_are_cleaned(self):
        os.makedirs(os.path.join(self.cache, "stage-old"))
        os.utime(os.path.join(self.cache, "stage-old"), (time.time() - 7200, time.time() - 7200))
        os.makedirs(os.path.join(self.cache, "stage-recent"))
        self.portable(World())
        self.assertFalse(os.path.exists(os.path.join(self.cache, "stage-old")))
        self.assertTrue(os.path.exists(os.path.join(self.cache, "stage-recent")))  # maybe another run's

    def test_command_line(self):
        world = World()
        rj = os.path.join(self.tmp.name, "release.json")
        write(rj, json.dumps(world.release))
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = update.main(["--release-json", rj, "--cache", self.cache, "--mode", "portable", "--root", self.root,
                              "--app", os.path.join(self.root, "src"), "--check", "--os", "linux", "--arch", "x86_64"])
        self.assertEqual(rc, 0)
        self.assertIn("update available: 1.0.0 -> %s" % NEW, out.getvalue())

    def test_command_line_errors_are_one_line_and_exit_1(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(update.main(["--release-json", os.path.join(self.tmp.name, "nope"), "--cache", self.cache]), 1)
            write(os.path.join(self.tmp.name, "bad.json"), "{not json")
            self.assertEqual(update.main(["--release-json", os.path.join(self.tmp.name, "bad.json"), "--cache", self.cache]), 1)
            write(os.path.join(self.tmp.name, "list.json"), "[]")
            self.assertEqual(update.main(["--release-json", os.path.join(self.tmp.name, "list.json"), "--cache", self.cache]), 1)
            self.assertEqual(update.main(["--release-json", os.path.join(self.tmp.name, "list.json"), "--cache", self.cache, "--mode", "portable"]), 1)
        self.assertEqual(err.getvalue().count("nuc-console-update:"), 4)
        self.assertNotIn("Traceback", err.getvalue())


# ---- the launchers ---------------------------------------------------------------------------------------------------------

def tree_copy(dest, with_bin=False):
    """The files a portable folder needs, from this repository."""
    for name in ("run.sh", "run.cmd", "run.ps1", "install-windows.ps1"):
        shutil.copy2(os.path.join(ROOT, name), os.path.join(dest, name))
    shutil.copytree(os.path.join(ROOT, "src"), os.path.join(dest, "src"), ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(os.path.join(ROOT, "config"), os.path.join(dest, "config"))
    if with_bin:
        shutil.copytree(os.path.join(ROOT, "bin"), os.path.join(dest, "bin"))


def powershell():
    return shutil.which("powershell") if WINDOWS else (shutil.which("pwsh") or shutil.which("powershell"))


class Files(unittest.TestCase):
    def test_the_launchers_and_the_updater_are_there(self):
        for rel in ("run.sh", "run.cmd", "run.ps1", "src/update.py", "bin/nuc-console-update", "bin/nuc-console-update.cmd",
                    "bin/nuc-console-update.ps1"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)

    @unittest.skipIf(WINDOWS, "exec bits")
    def test_the_shell_scripts_are_executable(self):
        for rel in ("run.sh", "bin/nuc-console-update", "bin/nuc-console-accept", "bin/nuc-console-problems"):
            self.assertTrue(os.stat(os.path.join(ROOT, rel)).st_mode & stat.S_IXUSR, rel)

    @unittest.skipIf(WINDOWS, "the sh helpers")
    def test_the_installed_helpers_point_a_portable_folder_to_run_sh(self):
        if os.path.exists("/opt/nuc-console/render.py"):
            self.skipTest("nuc-console is installed on this machine")
        for helper, option in (("nuc-console-problems", "--problems"), ("nuc-console-accept", "--accept")):
            r = subprocess.run([os.path.join(ROOT, "bin", helper)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 1, helper)
            self.assertIn("./run.sh " + option, r.stderr, helper)
            self.assertNotIn("Traceback", r.stderr)
        for rel, option in (("bin/nuc-console-problems.cmd", "run.cmd -Problems"), ("bin/nuc-console-accept.cmd", "run.cmd -Accept")):
            text = read(os.path.join(ROOT, rel))
            self.assertIn(r'if not exist "%~dp0..\app\render.py"', text, rel)
            self.assertIn(option, text, rel)
        self.assertIn("[switch]$Problems", read(os.path.join(ROOT, "run.ps1")))
        self.assertIn("-Problems", read(os.path.join(ROOT, "run.cmd")))

    def test_git_records_the_exec_bit(self):
        if not shutil.which("git") or not os.path.isdir(os.path.join(ROOT, ".git")) and not os.path.isfile(os.path.join(ROOT, ".git")):
            self.skipTest("not a git checkout")
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "-s", "run.sh", "bin/nuc-console-update", "bin/nuc-console-update.cmd", "run.cmd"],
                             capture_output=True, text=True)
        modes = {line.split("\t")[1]: line.split()[0] for line in out.stdout.splitlines()}
        if not modes:
            self.skipTest("the new files are not added yet")
        for rel in ("run.sh", "bin/nuc-console-update"):
            self.assertEqual(modes.get(rel), "100755", rel)
        for rel in ("bin/nuc-console-update.cmd", "run.cmd"):
            self.assertEqual(modes.get(rel), "100644", rel)

    def test_shell_scripts_are_posix_sh_without_dependencies(self):
        for rel in ("run.sh", "bin/nuc-console-update"):
            text = read(os.path.join(ROOT, rel))
            self.assertTrue(text.startswith("#!/bin/sh\n"), rel)
            self.assertNotIn("[[", text, rel)
            self.assertNotIn("\r", text, rel)
            self.assertNotRegex(text, r"(?m)^\s*(exec\s+)?sudo\b", rel)  # never calls sudo itself: the user decides

    def test_powershell_files_are_plain_ascii(self):
        # Windows PowerShell 5.1 reads a file without a BOM as ANSI: anything else would be garbled
        for rel in ("run.ps1", "bin/nuc-console-update.ps1"):
            with open(os.path.join(ROOT, rel), "rb") as f:
                data = f.read()
            data.decode("ascii")
            self.assertTrue(data.startswith(b"#Requires -Version 5.1"), rel)

    def test_cmd_files_only_run_their_ps1_with_the_policy_bypassed_for_the_process(self):
        for rel, ps1 in (("run.cmd", "run.ps1"), ("bin/nuc-console-update.cmd", "nuc-console-update.ps1")):
            text = read(os.path.join(ROOT, rel))
            self.assertIn("powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0" + ps1 + "\"", text, rel)
            self.assertNotRegex(text, r"(?i)set-executionpolicy", rel)

    def test_the_update_cmd_reads_nothing_of_itself_once_powershell_runs(self):
        # the update replaces bin\\nuc-console-update.cmd while cmd.exe runs it, and cmd reads a batch file line by line, from where
        # it stopped: whatever follows the PowerShell line would be read from the new file at the old offset
        lines = read(os.path.join(ROOT, "bin", "nuc-console-update.cmd")).splitlines()
        self.assertTrue(lines[-1].startswith("powershell ") and lines[-1].endswith(" & exit /b"), lines[-1])
        elevated = lines[lines.index("if defined ELEV ("):]
        self.assertEqual(elevated[-2], ")")  # the elevated copy is one block, read whole before it runs
        self.assertEqual([i for i, l in enumerate(lines) if l.lstrip().startswith("powershell ") and "-ExecutionPolicy Bypass -File" in l and i < len(lines) - 1],
                         [lines.index("if defined ELEV (") + 1])
        self.assertNotIn("%errorlevel%", "\n".join(elevated))

    def test_the_updater_asks_the_same_address_everywhere(self):
        for rel in ("bin/nuc-console-update", "bin/nuc-console-update.ps1"):
            self.assertIn(update.API, read(os.path.join(ROOT, rel)), rel)
        self.assertTrue(update.API.startswith("https://api.github.com/repos/give-jd/nuc-console-oss/"))

    def test_the_updater_and_the_launchers_install_no_service_and_no_timer(self):
        for rel in ("bin/nuc-console-update", "bin/nuc-console-update.cmd", "bin/nuc-console-update.ps1", "run.sh", "run.cmd", "run.ps1"):
            text = read(os.path.join(ROOT, rel))
            self.assertNotRegex(text, r"(?i)systemctl|launchctl|Register-ScheduledTask|schtasks|crontab", rel)
        for rel in ("systemd", "launchd"):  # the services never run the updater: updating is something you do
            for dp, _d, fs in os.walk(os.path.join(ROOT, rel)):
                for f in fs:
                    self.assertNotIn("nuc-console-update", read(os.path.join(dp, f)), f)

    @unittest.skipUnless(hasattr(sys, "stdlib_module_names"), "Python 3.10+")
    def test_update_py_imports_only_the_standard_library(self):
        imports = set(re.findall(r"^(?:from|import) (\w+)", read(os.path.join(ROOT, "src", "update.py")), re.M))
        self.assertLessEqual(imports, set(sys.stdlib_module_names))

    def test_run_ps1_refuses_a_folder_ordinary_users_can_write_as_administrator(self):
        text = read(os.path.join(ROOT, "run.ps1"))
        check = text.index("if (Test-Admin) {\n    foreach ($d in @($Here, $Src")
        self.assertLess(check, text.index("$python = Get-BundledPython"))  # before anything is unpacked or run as administrator
        for sid in ("S-1-1-0", "S-1-5-32-545", "S-1-5-11"):  # Everyone, Users, Authenticated Users
            self.assertIn("'%s'" % sid, text)
        for name in ("$Here", "$Src", "$PyDir", "$Data", "$Logs"):
            self.assertIn(name, text[check:text.index("$python = Get-BundledPython")])
        self.assertIn("Test-ReparsePoint", text)

    def test_powershell_parses(self):
        ps = powershell()
        if not ps:
            self.skipTest("no PowerShell here")
        files = ",".join("'%s'" % os.path.join(ROOT, p).replace("'", "''") for p in ("run.ps1", os.path.join("bin", "nuc-console-update.ps1")))
        script = ("$bad = 0; foreach ($f in @(%s)) { $t = $null; $e = $null; "
                  "[void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$t, [ref]$e); "
                  "foreach ($x in $e) { Write-Output ($f + ': ' + $x.Message); $bad++ } }; exit $bad" % files)
        r = subprocess.run([ps, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


@unittest.skipIf(WINDOWS, "run.sh: Linux and macOS")
class RunSh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(os.path.realpath(self.tmp.name), "nuc-console")
        os.mkdir(self.dir)
        tree_copy(self.dir)
        self.env = dict(os.environ, PYTHON=sys.executable, HOME=self.tmp.name)
        for k in list(self.env):
            if k.startswith("NUC_CONSOLE_"):
                del self.env[k]

    def sh(self, *args, **kw):
        return subprocess.run([os.path.join(self.dir, "run.sh")] + list(args), capture_output=True, text=True, env=self.env,
                              cwd=kw.pop("cwd", self.dir), timeout=120, **kw)

    def listing(self):
        return sorted(os.path.relpath(os.path.join(dp, f), self.dir) for dp, _d, fs in os.walk(self.dir) for f in fs
                      if "__pycache__" not in dp and not f.endswith(".pyc"))

    def test_the_data_folder_is_made_and_the_config_is_copied_once(self):
        before = self.listing()
        r = self.sh("--accept")  # nothing collected yet: it must say so, not crash
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        data = os.path.join(self.dir, "data")
        for sub in ("run", "lib", "logs"):
            self.assertTrue(os.path.isdir(os.path.join(data, sub)), sub)
        self.assertEqual(read(os.path.join(data, "config.ini")), read(os.path.join(ROOT, "config", "config.ini")))
        self.assertEqual(read(os.path.join(data, "config.ini.dist")), read(os.path.join(ROOT, "config", "config.ini")))
        write(os.path.join(data, "config.ini"), "[features]\nmap = no\n")
        write(os.path.join(self.dir, "config", "config.ini"), "[features]\nmap = yes\n")  # a newer release's
        self.sh("--accept")
        self.assertEqual(read(os.path.join(data, "config.ini")), "[features]\nmap = no\n")  # your edits stay
        self.assertEqual(read(os.path.join(data, "config.ini.dist")), "[features]\nmap = yes\n")  # the new options to diff
        self.assertEqual(sorted(set(self.listing()) - set(before)), sorted(os.path.join("data", n) for n in ("config.ini", "config.ini.dist")))
        self.assertEqual(stat.S_IMODE(os.stat(data).st_mode) & 0o077, 0)  # yours alone (umask 077)

    def test_accept_passes_its_arguments_to_the_renderer(self):
        r = self.sh("--accept", "--problem", "no-such-problem", "--reason", "x")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)  # render.py's own answer, not the usage of run.sh
        self.assertIn("unknown problem id", r.stderr)

    def test_nothing_is_written_outside_the_folder(self):
        home = os.path.join(self.tmp.name, "home")
        os.mkdir(home)
        self.env["HOME"] = home
        self.sh("--accept")
        self.assertEqual(os.listdir(home), [])

    def test_which_python(self):
        r = self.sh("--which-python")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, sys.executable), r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "data")))  # it only answers
        self.env["PYTHON"] = "/nonexistent/python"
        r = self.sh("--which-python")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Python 3.8", r.stderr)

    def bundled_python(self, body=None):
        """A python/bin/python3 like the one of a release archive (a link to python3.99, as there), here a script that logs what it is
        asked and runs this machine's Python; -> the path of the link."""
        log = os.path.join(self.tmp.name, "bundled.log")
        body = body or '#!/bin/sh\necho "$@" >> "%s"\nexec "%s" "$@"\n' % (log, sys.executable)
        write(os.path.join(self.dir, "python", "bin", "python3.99"), body, 0o755)
        os.symlink("python3.99", os.path.join(self.dir, "python", "bin", "python3"))
        self.log = log
        return os.path.join(self.dir, "python", "bin", "python3")

    def test_the_python_of_the_archive_is_used_first(self):
        bundled = self.bundled_python()
        del self.env["PYTHON"]  # nothing chooses: the archive's Python comes before the one of the machine
        r = self.sh("--which-python")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, bundled), r.stderr)
        self.assertEqual(len(read(self.log).splitlines()), 1)  # it was run once, to ask whether it is 3.8+
        r = self.sh("--accept")  # and it is the one that runs the program
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # nothing collected yet
        calls = read(self.log).splitlines()
        self.assertTrue([c for c in calls if c.startswith("-B ") and c.endswith("src/render.py --accept")], calls)
        r = self.sh("--problems", stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue([c for c in read(self.log).splitlines() if c.endswith("src/render.py --problems")])

    def test_python_in_the_environment_still_chooses(self):
        self.bundled_python()
        r = self.sh("--which-python")  # PYTHON is set by setUp
        self.assertEqual((r.returncode, r.stdout.strip()), (0, sys.executable), r.stderr)

    def test_the_python_of_the_archive_is_found_from_any_directory_and_through_a_link(self):
        bundled = self.bundled_python()
        del self.env["PYTHON"]
        link = os.path.join(self.tmp.name, "link-to-run.sh")
        os.symlink(os.path.join(self.dir, "run.sh"), link)
        for cwd in (self.tmp.name, "/"):
            r = subprocess.run([os.path.join(self.dir, "run.sh"), "--which-python"], capture_output=True, text=True, env=self.env, cwd=cwd, timeout=60)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, bundled), (cwd, r.stderr))

    def test_a_python_of_the_archive_that_does_not_run_here_is_skipped_with_a_warning(self):
        """The archive of another processor: the machine's Python is used if there is one, and the message says what happened."""
        self.bundled_python("#!/bin/sh\nexit 126\n")
        del self.env["PYTHON"]
        r = self.sh("--which-python")
        self.assertIn("the Python in %s does not run here" % os.path.join(self.dir, "python"), r.stderr)
        self.assertIn("archive for", r.stderr)
        if r.returncode == 0:  # this machine has a python3 3.8+ somewhere: that one
            self.assertNotEqual(r.stdout.strip(), os.path.join(self.dir, "python", "bin", "python3"))
        else:
            self.assertIn("Python 3.8 or newer not found", r.stderr)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "as root only")
    def test_as_root_a_python_that_others_can_write_is_not_run(self):
        """sudo ./run.sh runs everything as root: a bundled Python that anybody can replace would be root's code."""
        marker = os.path.join(self.tmp.name, "ran-as-root")
        self.bundled_python('#!/bin/sh\ntouch "%s"\nexit 0\n' % marker)
        del self.env["PYTHON"]
        os.chmod(os.path.join(self.dir, "python", "bin"), 0o777)
        r = self.sh("--which-python")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("writable by others", r.stderr)
        self.assertFalse(os.path.exists(marker))  # refused before it was even asked for its version
        os.chmod(os.path.join(self.dir, "python", "bin"), 0o755)
        r = self.sh("--which-python")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_problems_name_the_commands_of_a_portable_folder(self):
        # nothing is collected yet: the collectors are reported, with advice that exists here (no systemctl, no nuc-console-accept)
        r = self.sh("--problems", stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("collector-net", r.stdout)
        self.assertIn("./run.sh --accept --problem collector-net --reason", r.stdout)
        self.assertIn(os.path.join(self.dir, "data", "logs", "collector.log"), r.stdout)
        self.assertNotRegex(r.stdout, r"systemctl|journalctl -u nuc|nuc-console-accept|nuc-console-problems")
        records = json.loads(self.sh("--problems", "--json", stdin=subprocess.DEVNULL).stdout)
        self.assertIn("collector-net", [x["id"] for x in records])
        self.assertFalse([x for x in records if "systemctl" in x["fix"]])
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "data"))), ["config.ini", "config.ini.dist", "lib", "logs", "run"])
        self.assertIn("--problems", self.sh("--help").stdout)

    def test_bad_options(self):
        self.assertEqual(self.sh("--bogus").returncode, 2)
        for port in ("abc", "70000", "-1", ""):
            r = self.sh("--web", "--port", port)
            self.assertEqual(r.returncode, 1, port)
        self.assertEqual(self.sh("--help").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "data")))

    def test_console_without_a_terminal_says_so(self):
        r = self.sh("--console", stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs a terminal", r.stderr)

    def test_it_must_run_from_the_extracted_folder(self):
        os.remove(os.path.join(self.dir, "src", "collector.py"))
        r = self.sh("--accept")
        self.assertEqual(r.returncode, 1)
        self.assertIn("src/collector.py", r.stderr)

    def descendants(self, pid):
        rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout.split()
        pairs = list(zip(map(int, rows[::2]), map(int, rows[1::2])))
        found, todo = set(), [pid]
        while todo:
            cur = todo.pop()
            for p, pp in pairs:
                if pp == cur and p not in found:
                    found.add(p)
                    todo.append(p)
        return found

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(r) and not r.startswith("Z")

    def web_url(self, p, log):
        deadline = time.time() + 90
        while time.time() < deadline:
            if p.poll() is not None:
                self.fail("run.sh ended early: " + p.stdout.read())
            m = re.search(r"^nuc-console web view on (http://127\.0\.0\.1:(\d+))", read(log) if os.path.exists(log) else "", re.M)
            if m:
                return int(m.group(2))
            time.sleep(0.2)
        self.fail("the web view did not come up")

    def get(self, port, path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read().decode("utf-8", "replace")
        c.close()
        return r.status, body

    def run_and_stop(self, sig):
        p = subprocess.Popen([os.path.join(self.dir, "run.sh"), "--web", "--no-open"], cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))  # a runner started in the background ignores it
        try:
            port = self.web_url(p, os.path.join(self.dir, "data", "logs", "web.log"))
            self.assertEqual(self.get(port, "/healthz")[0], 200)
            status, body = self.get(port, "/?fit=1")  # no token: loopback only
            self.assertEqual(status, 200)
            self.assertIn("nuc-console", body)
            pidfile = os.path.join(self.dir, "data", "portable.pid")
            self.assertEqual(int(read(pidfile)), p.pid)
            kids = self.descendants(p.pid)
            self.assertGreaterEqual(len(kids), 2, kids)  # the collector and the web view (and the baseline waiter)
            twice = subprocess.run([os.path.join(self.dir, "run.sh"), "--web", "--no-open"], cwd=self.dir, env=self.env, capture_output=True,
                                   text=True, timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual(twice.returncode, 1)
            self.assertIn("already running", twice.stderr)
            self.assertEqual(int(read(pidfile)), p.pid)  # the refused one did not touch it
            os.kill(p.pid, sig)
            p.wait(timeout=30)
            self.assertEqual(p.returncode, 128 + sig)
            deadline = time.time() + 10
            while time.time() < deadline and any(self.alive(k) for k in kids):
                time.sleep(0.1)
            self.assertEqual([k for k in kids if self.alive(k)], [], "orphans left")
            self.assertFalse(os.path.exists(pidfile))
            with self.assertRaises(OSError):
                self.get(port, "/healthz")
        finally:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
            if p.stdout:
                p.stdout.close()

    def test_the_baseline_is_tried_until_it_exists_and_then_left_alone(self):
        # a stand-in Python answers the baseline command: no good snapshot the first time, one the second time
        fake = os.path.join(self.tmp.name, "python-stand-in")
        write(fake, """#!/bin/sh
case " $* " in
    *" --accept --if-missing "*)
        n=$NUC_CONSOLE_HOME/tries
        c=$(cat "$n" 2>/dev/null || echo 0)
        echo $((c + 1)) > "$n"
        [ "$c" -ge 1 ] || exit 1
        echo '{"ports": []}' > "$NUC_CONSOLE_HOME/lib/baseline.json"
        exit 0 ;;
esac
exec "%s" "$@"
""" % sys.executable, 0o755)
        self.env["PYTHON"] = fake
        p = subprocess.Popen([os.path.join(self.dir, "run.sh"), "--web", "--no-open"], cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        data = os.path.join(self.dir, "data")
        try:
            deadline = time.time() + 60
            while time.time() < deadline and not os.path.exists(os.path.join(data, "lib", "baseline.json")):
                self.assertIsNone(p.poll(), "run.sh ended early")
                time.sleep(0.3)
            self.assertTrue(os.path.exists(os.path.join(data, "lib", "baseline.json")))
            time.sleep(6)  # it must not ask again
            self.assertEqual(read(os.path.join(data, "tries")).strip(), "2")
            os.kill(p.pid, signal.SIGTERM)
            p.wait(timeout=30)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            p.stdout.close()

    def test_a_baseline_attempt_in_progress_does_not_hold_up_the_stop(self):
        # an attempt is a command of the waiter: if it ran in the foreground the waiter's trap would wait for it to end,
        # and with a slow machine quitting would take as long as the attempt
        marker = os.path.join(self.tmp.name, "attempt-started")
        fake = os.path.join(self.tmp.name, "python-stand-in")
        write(fake, """#!/bin/sh
case " $* " in
    *" --accept --if-missing "*) : > "%s"; exec sleep 40 ;;
esac
exec "%s" "$@"
""" % (marker, sys.executable), 0o755)
        self.env["PYTHON"] = fake
        p = subprocess.Popen([os.path.join(self.dir, "run.sh"), "--web", "--no-open"], cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        try:
            deadline = time.time() + 60
            while time.time() < deadline and not os.path.exists(marker):
                self.assertIsNone(p.poll(), "run.sh ended early")
                time.sleep(0.2)
            self.assertTrue(os.path.exists(marker), "the baseline was never tried")
            kids = self.descendants(p.pid)
            started = time.time()
            os.kill(p.pid, signal.SIGTERM)
            p.wait(timeout=20)  # it was 40 s: the length of the attempt
            self.assertLess(time.time() - started, 15)
            deadline = time.time() + 10
            while time.time() < deadline and any(self.alive(k) for k in kids):
                time.sleep(0.1)
            self.assertEqual([k for k in kids if self.alive(k)], [], "orphans left")
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            p.stdout.close()

    def test_an_existing_baseline_is_never_asked_for_again(self):
        data = os.path.join(self.dir, "data")
        write(os.path.join(data, "lib", "baseline.json"), '{"ports": [1]}')
        p = subprocess.Popen([os.path.join(self.dir, "run.sh"), "--web", "--no-open"], cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        try:
            self.web_url(p, os.path.join(data, "logs", "web.log"))
            time.sleep(5)
            self.assertFalse(os.path.exists(os.path.join(data, "logs", "baseline.log")))  # the waiter was not even started
            self.assertEqual(read(os.path.join(data, "lib", "baseline.json")), '{"ports": [1]}')
            os.kill(p.pid, signal.SIGTERM)
            p.wait(timeout=30)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            p.stdout.close()

    def test_web_run_serves_on_loopback_and_ctrl_c_stops_everything(self):
        self.run_and_stop(signal.SIGINT)

    def test_web_run_stops_everything_on_terminate(self):
        self.run_and_stop(signal.SIGTERM)

    def run_console_and_stop(self, stop, status):
        """--console in a pseudo-terminal, stopped by `stop(master fd, pid)`: it must end with `status`, put the terminal back,
        and leave no process and no pid file."""
        import fcntl
        import pty
        import select
        import struct
        import termios
        pid, fd = pty.fork()
        if pid == 0:  # the child: run.sh, with the pseudo-terminal as its keyboard and screen
            try:
                for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):  # a test run in the background (nohup, `&`) ignores some,
                    signal.signal(sig, signal.SIG_DFL)  # and a shell cannot trap a signal that was ignored when it started
                os.chdir(self.dir)
                os.execve(os.path.join(self.dir, "run.sh"), ["run.sh", "--console"], dict(self.env, TERM="xterm-256color"))
            finally:
                os._exit(127)
        # a size, as a real terminal has: a pseudo-terminal of 0x0 columns draws nothing on Python 3.8 (shutil.get_terminal_size
        # answers 0 there, the fallback only from 3.11)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        screen, kids, code, stopped = b"", set(), None, None
        deadline = time.time() + 90
        try:
            while time.time() < deadline and code is None:
                if select.select([fd], [], [], 0.2)[0]:
                    try:
                        screen += os.read(fd, 65536)
                    except OSError:  # the other end is gone: run.sh has ended
                        pass
                done, st = os.waitpid(pid, os.WNOHANG)
                if done:
                    code = os.WEXITSTATUS(st) if os.WIFEXITED(st) else -os.WTERMSIG(st)
                    break
                kids |= self.descendants(pid)
                if stopped is None and b"q: quit" in screen and len(kids) >= 2:  # drawn: the keyboard is being read
                    stopped = time.time()
                    stop(fd, pid)
                elif stopped is not None and time.time() - stopped > 30:
                    self.fail("run.sh --console did not stop within 30 s of the stop request")
            for _ in range(50):  # what it wrote last may still be in the pseudo-terminal
                if not select.select([fd], [], [], 0.3)[0]:
                    break
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                screen += data
            self.assertIsNotNone(stopped, "the dashboard never came up: " + screen.decode("utf-8", "replace")[-500:])
            self.assertEqual(code, status)
            self.assertNotIn(b"Traceback", screen)
            self.assertTrue(screen.rstrip().endswith(b"\x1b[?25h\x1b[0m"), "the cursor and the colours are not restored")
            deadline = time.time() + 10
            while time.time() < deadline and any(self.alive(k) for k in kids):
                time.sleep(0.1)
            self.assertEqual([k for k in kids if self.alive(k)], [], "orphans left")
            self.assertFalse(os.path.exists(os.path.join(self.dir, "data", "portable.pid")))
        finally:
            for k in kids | {pid}:  # whatever a failed assertion left running
                if code is None or k != pid:
                    try:
                        os.kill(k, signal.SIGKILL)
                    except OSError:
                        pass
            if code is None:
                try:
                    os.waitpid(pid, 0)
                except OSError:
                    pass
            os.close(fd)

    def test_console_q_quits_and_stops_everything(self):
        self.run_console_and_stop(lambda fd, pid: os.write(fd, b"q"), 0)

    def test_console_ctrl_c_stops_everything(self):
        self.run_console_and_stop(lambda fd, pid: os.write(fd, b"\x03"), 130)

    def test_console_stops_when_only_run_sh_is_told_to(self):
        # kill / timeout reach the shell and not the dashboard: a shell runs its trap only after a foreground command ends,
        # so the dashboard must not be one
        self.run_console_and_stop(lambda fd, pid: os.kill(pid, signal.SIGTERM), 143)

    def test_console_stops_on_hangup(self):
        self.run_console_and_stop(lambda fd, pid: os.kill(pid, signal.SIGHUP), 129)


@unittest.skipIf(WINDOWS, "the sh wrapper: Linux and macOS")
class UpdateSh(unittest.TestCase):
    """bin/nuc-console-update for real, in a portable folder, with a stand-in curl and gh (no network, no installed system)."""

    CURL = """#!/bin/sh
out="" url=""
while [ $# -gt 0 ]; do
    case $1 in
        -o) out=$2; shift 2 ;;
        -H|--proto|--proto-redir|--connect-timeout|--max-time|--retry|--max-filesize) shift 2 ;;
        -*) shift ;;
        *) url=$1; shift ;;
    esac
done
echo "$url" >> "$FAKE_DIR/curl.log"
case $url in
    https://api.github.com/repos/give-jd/nuc-console-oss/releases/latest) src=$FAKE_DIR/latest.json ;;
    https://github.com/give-jd/nuc-console-oss/releases/download/*) src=$FAKE_DIR/${url##*/} ;;
    *) exit 6 ;;
esac
[ -f "$src" ] || exit 22
cp "$src" "$out"
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = os.path.realpath(self.tmp.name)
        self.dir = os.path.join(base, "nuc-console")
        self.fake = os.path.join(base, "fake")
        self.bin = os.path.join(base, "fakebin")
        for d in (self.dir, self.fake, self.bin):
            os.mkdir(d)
        tree_copy(self.dir, with_bin=True)
        write(os.path.join(self.dir, "docs", "OLD.md"), "old\n")
        write(os.path.join(self.dir, "data", "config.ini"), "[features]\nmap = no\n")
        write(os.path.join(self.bin, "curl"), self.CURL, 0o755)
        write(os.path.join(self.bin, "gh"), "#!/bin/sh\nexit 0\n", 0o755)
        self.env = dict(os.environ, PATH=self.bin + os.pathsep + "/usr/bin:/bin", PYTHON=sys.executable, HOME=base, FAKE_DIR=self.fake)
        for k in list(self.env):
            if k.startswith("NUC_CONSOLE_"):
                del self.env[k]
        self.os_name = update.detect_os()
        self.publish(NEW)

    def publish(self, version, tamper=None):
        name = update.archive_name(version, self.os_name, update.detect_arch())  # the archive for this machine, whichever it is
        data = make_archive("tar", "nuc-console-" + version, release_files(version))
        sums = "%s  %s\n" % (sha(data), name)
        write(os.path.join(self.fake, name), tamper(data) if tamper else data)
        write(os.path.join(self.fake, "SHA256SUMS"), sums)
        write(os.path.join(self.fake, "latest.json"), json.dumps(release_json(version, [name, "SHA256SUMS"])))
        self.name = name

    def update(self, *args, **kw):
        return subprocess.run([os.path.join(self.dir, "bin", "nuc-console-update")] + list(args), capture_output=True, text=True,
                              env=kw.pop("env", self.env), cwd=self.dir, timeout=120, stdin=subprocess.DEVNULL)

    def asked(self):
        path = os.path.join(self.fake, "curl.log")
        return read(path).split() if os.path.exists(path) else []

    def current(self):
        return update.read_version(os.path.join(self.dir, "src"))

    def test_already_up_to_date(self):
        self.publish(VERSION)
        r = self.update("--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already at %s" % VERSION, r.stdout)
        self.assertEqual(self.asked(), [update.API])

    def test_check_reports_and_changes_nothing(self):
        r = self.update("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("update available: %s -> %s" % (VERSION, NEW), r.stdout)
        self.assertEqual(self.asked(), [update.API])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "cache")))
        self.assertEqual(self.current(), VERSION)

    def test_a_git_checkout_is_not_updated(self):
        for make in (lambda p: os.mkdir(p), lambda p: write(p, "gitdir: /somewhere/else\n")):
            git = os.path.join(self.dir, ".git")
            if os.path.isdir(git):
                os.rmdir(git)
            make(git)
            r = self.update("--yes")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("git pull", r.stderr)
            self.assertEqual(self.asked(), [])  # GitHub was not even asked
            self.assertEqual(self.current(), VERSION)
            self.assertFalse(os.path.exists(os.path.join(self.dir, "cache")))

    def test_without_yes_and_without_a_terminal_it_does_not_guess(self):
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn("--yes", r.stderr)
        self.assertEqual(self.current(), VERSION)

    def test_updates_the_folder_and_keeps_data_and_cache(self):
        r = self.update("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.current(), NEW)
        self.assertIn("provenance", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "src", "newmodule.py")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "docs", "OLD.md")))
        self.assertEqual(read(os.path.join(self.dir, "data", "config.ini")), "[features]\nmap = no\n")
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "cache"))), sorted([self.name, "SHA256SUMS"]))
        self.assertTrue(os.stat(os.path.join(self.dir, "run.sh")).st_mode & stat.S_IXUSR)
        self.assertEqual(self.asked(), [update.API] + ["https://github.com/give-jd/nuc-console-oss/releases/download/v%s/%s" % (NEW, n)
                                                       for n in ("SHA256SUMS", self.name)])
        again = self.update("--check")  # and the new one is current
        self.assertIn("already at %s" % NEW, again.stdout)

    def test_the_cached_archive_is_not_downloaded_again(self):
        self.update("--yes")
        write(os.path.join(self.dir, "src", "nuc_config.py"), set_version(read(os.path.join(self.dir, "src", "nuc_config.py")), VERSION))
        r = self.update("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already in the cache", r.stdout)
        self.assertEqual(len([u for u in self.asked() if u.endswith(self.name)]), 1)  # the one download of the first update

    def test_a_wrong_hash_refuses(self):
        self.publish(NEW, tamper=lambda b: b[:-3] + b"xyz")
        write(os.path.join(self.fake, "SHA256SUMS"), "%s  %s\n" % ("0" * 64, self.name))
        r = self.update("--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not installed", r.stderr)
        self.assertEqual(self.current(), VERSION)
        self.assertEqual(os.listdir(os.path.join(self.dir, "cache")), ["SHA256SUMS"])

    def test_a_failed_gh_attestation_refuses(self):
        write(os.path.join(self.bin, "gh"), "#!/bin/sh\necho 'verification failed' >&2\nexit 1\n", 0o755)
        r = self.update("--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("gh attestation verify failed", r.stderr)
        self.assertEqual(self.current(), VERSION)

    def test_gh_is_asked_about_the_downloaded_archive(self):
        log = os.path.join(self.fake, "gh.log")
        write(os.path.join(self.bin, "gh"), "#!/bin/sh\necho \"$@\" >> \"%s\"\nexit 0\n" % log, 0o755)
        self.assertEqual(self.update("--yes").returncode, 0)
        self.assertEqual(read(log).strip(), "attestation verify %s --repo give-jd/nuc-console-oss" % os.path.join(self.dir, "cache", self.name))

    def test_without_gh_it_says_the_provenance_was_not_checked(self):
        os.remove(os.path.join(self.bin, "gh"))
        env = dict(self.env, PATH=self.bin + os.pathsep + "/usr/bin:/bin")
        if shutil.which("gh", path=env["PATH"]):
            self.skipTest("a gh in /usr/bin")
        r = self.update("--yes", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("provenance not checked", r.stdout)
        self.assertEqual(self.current(), NEW)

    def test_no_network_is_a_clear_error(self):
        os.remove(os.path.join(self.fake, "latest.json"))
        r = self.update("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not read the latest release", r.stderr)

    def test_a_running_folder_is_not_updated(self):
        write(os.path.join(self.dir, "data", "portable.pid"), "%d\n" % os.getpid())
        r = self.update("--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("running", r.stderr)
        self.assertEqual(self.current(), VERSION)
        write(os.path.join(self.dir, "data", "portable.pid"), "99999999\n")  # a stale one does not block
        self.assertEqual(self.update("--yes").returncode, 0)

    def test_installed_mode_needs_an_installation(self):
        if os.path.exists("/opt/nuc-console/update.py"):
            self.skipTest("nuc-console is installed on this machine")
        r = self.update("--installed", "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not installed", r.stderr)
        self.assertEqual(self.current(), VERSION)

    def test_bad_options(self):
        self.assertEqual(self.update("--bogus").returncode, 2)
        self.assertEqual(self.update("--help").returncode, 0)

    def test_the_updater_leaves_the_environment_alone(self):
        # a PYTHONPATH of the user must not be able to inject code into the updater (python -I)
        evil = os.path.join(self.tmp.name, "evil")
        write(os.path.join(evil, "hashlib.py"), "raise SystemExit('injected')\n")
        env = dict(self.env, PYTHONPATH=evil)
        r = self.update("--check", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("injected", r.stdout + r.stderr)


@unittest.skipUnless(WINDOWS, "run.ps1 itself: Windows")
class RunPs1(unittest.TestCase):
    def test_the_data_folder_is_made_and_the_config_is_copied_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(os.path.realpath(tmp), "nuc-console")
            os.mkdir(d)
            tree_copy(d)
            os.remove(os.path.join(d, "install-windows.ps1"))  # no bundled Python in a source tree: this machine's is used
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(d, "run.ps1"), "-Accept"]
            env = {k: v for k, v in os.environ.items() if not k.startswith("NUC_CONSOLE_")}
            r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # nothing collected yet: --accept refuses, it does not crash
            self.assertNotIn("Traceback", r.stderr)
            data = os.path.join(d, "data")
            for sub in ("run", "lib", "logs"):
                self.assertTrue(os.path.isdir(os.path.join(data, sub)), (sub, r.stdout + r.stderr))
            self.assertEqual(read(os.path.join(data, "config.ini")), read(os.path.join(ROOT, "config", "config.ini")))
            write(os.path.join(data, "config.ini"), "[features]\nmap = no\n")
            subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
            self.assertEqual(read(os.path.join(data, "config.ini")), "[features]\nmap = no\n")


class PortableAdvice(unittest.TestCase):
    """On screen, the advice must name commands that exist in a portable folder (there is no nuc-console-accept on the PATH)."""

    def advice(self, home):
        code = ("import render, json; print(json.dumps([render.ACCEPT_CMD, render.CMD['restart'], render.CMD['logs'], "
                "{k: v[2] for k, v in render.CATALOG.items()}, render.PROBLEMS_CMD]))")
        env = {k: v for k, v in os.environ.items() if not k.startswith("NUC_CONSOLE_")}
        if home:
            env["NUC_CONSOLE_HOME"] = home
        out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, env=env, cwd=os.path.join(ROOT, "src"))
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_portable(self):
        with tempfile.TemporaryDirectory() as tmp:
            accept, restart, logs, catalog, problems = self.advice(tmp)
            advice = list(catalog.values())
            self.assertEqual(accept, "run.cmd -Accept" if WINDOWS else "./run.sh --accept")
            self.assertEqual(problems, "run.cmd -Problems" if WINDOWS else "./run.sh --problems")
            self.assertEqual(logs, os.path.join(os.path.abspath(tmp), "logs", "collector.log"))
            self.assertNotIn("systemctl", restart + logs)
            self.assertNotIn("launchctl", restart)
            self.assertTrue(any(accept in a for a in advice))  # the baseline items say how to accept
            self.assertFalse([a for a in advice if "nuc-console-accept" in a], "advice that does not exist here")

    def test_portable_advice_names_no_service(self):
        # a portable run has no systemd unit, LaunchDaemon or scheduled task: restarting is quitting and starting it again
        with tempfile.TemporaryDirectory() as tmp:
            _accept, restart, logs, catalog, _problems = self.advice(tmp)
            for pid in ("collector-containers", "collector-net", "collector-boot", "stale-containers", "stale-net", "net-sections",
                        "ufw-unreadable"):
                self.assertIn(logs, catalog[pid], pid)
            for pid in ("collector-net", "collector-boot", "stale-containers", "stale-net"):
                self.assertIn(restart, catalog[pid], pid)
            bad = {pid: a for pid, a in catalog.items()
                   if re.search(r"systemctl (restart|status) nuc-console|journalctl -u nuc-console|system/com\.nuc-console|\\nuc-console\\", a)}
            self.assertEqual(bad, {})

    def test_installed_is_as_before(self):
        accept, restart, logs, catalog, problems = self.advice("")
        # the installed collector is a service, named in the words of each OS: a scheduled task, a LaunchDaemon, a systemd unit
        if WINDOWS:
            words = ("nuc-console-accept", "Start-ScheduledTask -TaskPath \\nuc-console\\ -TaskName collector (administrator PowerShell)",
                     "Get-ScheduledTask -TaskPath \\nuc-console\\ ; log: %ProgramData%\\nuc-console\\logs\\collector.log")
        elif sys.platform == "darwin":
            words = ("sudo nuc-console-accept", "sudo launchctl kickstart -k system/com.nuc-console.collector",
                     "sudo launchctl print system/com.nuc-console.collector; log: /var/log/nuc-console/collector.log")
        else:
            words = ("sudo nuc-console-accept", "sudo systemctl restart nuc-console-collector",
                     "sudo systemctl status nuc-console-collector; journalctl -u nuc-console-collector")
        self.assertEqual((accept, problems), (words[0], "nuc-console-problems"))
        self.assertEqual((restart, catalog["stale-net"]), (words[1], words[1]))
        self.assertEqual(catalog["collector-containers"], words[2])
        self.assertTrue([a for a in catalog.values() if "nuc-console-accept" in a])

    def test_the_screen_and_the_message_name_the_portable_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if not k.startswith("NUC_CONSOLE_")}
            env["NUC_CONSOLE_HOME"] = tmp
            r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "src", "render.py"), "--accept", "--problem", "failed-units"],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 2, r.stderr)  # no reason given: it says where the reasons are shown
            self.assertIn("run.cmd -Problems" if WINDOWS else "./run.sh --problems", r.stderr)
            self.assertNotIn("nuc-console-problems", r.stderr)


class CollectorPortable(unittest.TestCase):
    def test_the_collector_once_with_the_portable_folder_on_every_os(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "data")
            os.makedirs(os.path.join(data, "run"))
            env = {k: v for k, v in os.environ.items() if not k.startswith("NUC_CONSOLE_")}
            env["NUC_CONSOLE_HOME"] = data
            r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "src", "collector.py"), "--once"], capture_output=True, text=True,
                               env=env, timeout=170)
            self.assertEqual(r.returncode, 0, r.stderr)
            # and "sensors" on macOS and Windows: the CPU temperature only the collector can read there (Linux: render.py reads sysfs)
            sensors = ["sensors"] if sys.platform in ("darwin", "win32") else []
            self.assertEqual(sorted(json.loads(r.stdout)), ["boot", "containers", "net"] + sensors)

    def test_portable_paths(self):
        code = ("import nuc_config as n, os; print(n.PORTABLE, n.ETC_DIR, n.RUN_DIR, n.LIB_DIR, n.DEFAULT_PATH, sep='|')")
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if not k.startswith("NUC_CONSOLE_")}
            env["NUC_CONSOLE_HOME"] = tmp
            out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, env=env, cwd=os.path.join(ROOT, "src")).stdout
            home = os.path.abspath(tmp)
            self.assertEqual(out.strip().split("|"), [tmp, home, os.path.join(home, "run"), os.path.join(home, "lib"), os.path.join(home, "config.ini")])


if __name__ == "__main__":
    unittest.main()
