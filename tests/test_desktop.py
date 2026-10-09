"""The desktop app (desktop/, docs/DESKTOP.md): the core it carries (tools/desktop_core.py), its configuration, its icons
(tools/desktop_icons.py), what its Rust may do, and the workflow that builds and smoke-tests its packages (.github/workflows/desktop.yml).
The app itself is compiled and run by that workflow only (cargo test, and --smoke-test on every system): this checks what can be
checked without a Rust compiler."""
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import stat
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_release as br  # noqa: E402
import desktop_core as dc  # noqa: E402
import desktop_icons  # noqa: E402

APP = os.path.join(ROOT, "desktop", "src-tauri")
POSIX = os.name == "posix"


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def tar_gz(members):
    """A .tar.gz like the release build writes: members are (name, data | None (folder) | ("link", target) | "fifo", mode)."""
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, data, mode in members:
                info = tarfile.TarInfo(name)
                info.mode = mode
                if data is None:
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                elif data == "fifo":
                    info.type = tarfile.FIFOTYPE
                    tar.addfile(info)
                elif isinstance(data, tuple):
                    info.type, info.linkname = (tarfile.SYMTYPE if data[0] == "link" else tarfile.LNKTYPE), data[1]
                    tar.addfile(info)
                else:
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def zip_bytes(members):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        for name, data in members:
            z.writestr(name, data)
    return raw.getvalue()


TOP = "nuc-console-9.9.9"


def unix_members(top=TOP, extra=()):
    return [
        (top, None, 0o755),
        (top + "/run.sh", b"#!/bin/sh\necho run\n", 0o755),
        (top + "/src", None, 0o755),
        (top + "/src/collector.py", b"print('collector')\n", 0o644),
        (top + "/python/bin/python3.99", b"\x7fELF fake", 0o755),
        (top + "/python/bin/python3", ("link", "python3.99"), 0o777),
        (top + "/python/lib/libpython3.99.so", b"lib", 0o755),
        (top + "/python/lib/libpython3.so", ("link", "libpython3.99.so"), 0o777),
        (top + "/python/lib/python3.99/os.py", b"# os\n", 0o644),
    ] + list(extra)


def embed_zip():
    return zip_bytes([("python.exe", b"MZ fake"), ("pythonw.exe", b"MZ fake"), ("python314.dll", b"dll"), ("python314.zip", b"PK stdlib"),
                      ("python314._pth", b"python314.zip\r\n.\r\n"), ("LICENSE.txt", b"PSF")])


def pins_ps1(amd64, arm64):
    return ("#Requires -Version 5.1\n$PyVersion = '3.14.8'\n$PyBuilds = @{  # pins\n"
            "    'AMD64' = @{ file = \"python-$PyVersion-embed-amd64.zip\"; sha256 = '%s' }\n"
            "    'ARM64' = @{ file = \"python-$PyVersion-embed-arm64.zip\"; sha256 = '%s' }\n}\n" % (amd64, arm64)).encode()


def windows_members(embed=None, pin=None, top=TOP):
    embed = embed_zip() if embed is None else embed
    pin = hashlib.sha256(embed).hexdigest() if pin is None else pin
    return [(top + "/run.ps1", b"param()\r\n"), (top + "/run.cmd", b"@echo off\r\n"), (top + "/src/collector.py", b"print('collector')\n"),
            (top + "/install-windows.ps1", pins_ps1(pin, "0" * 64)), (top + "/python/python-3.14.8-embed-amd64.zip", embed)]


class TempCase(unittest.TestCase):
    def tmp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return os.path.realpath(d.name)

    def archive(self, name, data):
        path = os.path.join(self.tmp(), name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def core(self, name, data):
        out = os.path.join(self.tmp(), "core")
        files, info = dc.from_archive(self.archive(name, data))
        dc.write(files, info, out, log=lambda s: None)
        return out, info


class CoreFromArchive(TempCase):
    def test_a_unix_archive_becomes_the_core_with_the_links_of_its_python_made_copies(self):
        out, info = self.core("nuc-console-9.9.9-linux-x86_64.tar.gz", tar_gz(unix_members()))
        self.assertEqual(read(os.path.join(out, "run.sh")), "#!/bin/sh\necho run\n")
        python3 = os.path.join(out, "python", "bin", "python3")
        self.assertFalse(os.path.islink(python3))  # the bundlers copy files, not links
        self.assertEqual(read(python3, "rb"), b"\x7fELF fake")
        self.assertEqual(read(os.path.join(out, "python", "lib", "libpython3.so"), "rb"), b"lib")
        if POSIX:
            for rel, mode in (("run.sh", 0o755), ("python/bin/python3", 0o755), ("python/bin/python3.99", 0o755), ("src/collector.py", 0o644),
                              ("python/lib/python3.99/os.py", 0o644)):
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(out, rel)).st_mode), mode, rel)
        mark = json.loads(read(os.path.join(out, "core.json")))
        self.assertEqual((mark["version"], mark["system"], mark["target"], mark["from"]), ("9.9.9", "linux", "linux-x86_64", "nuc-console-9.9.9-linux-x86_64.tar.gz"))
        self.assertEqual(mark["python"], "python/bin/python3")
        self.assertEqual(mark["files"], 7)
        self.assertEqual(info["sha256"], hashlib.sha256(tar_gz(unix_members())).hexdigest())

    def test_a_windows_archive_gets_its_python_unpacked_the_way_run_ps1_does_it(self):
        embed = embed_zip()
        out, info = self.core("nuc-console-9.9.9-windows-x64.zip", zip_bytes(windows_members(embed)))
        py = os.path.join(out, "python")
        self.assertEqual(sorted(os.listdir(py)), ["LICENSE.txt", "nuc-console-python.txt", "python.exe", "python314._pth", "python314.dll",
                                                  "python314.zip", "pythonw.exe"])  # the zip is gone: run.ps1 never needs it again
        # what run.ps1 writes: the ._pth keeps this Python to its library and ..\src; the stamp is "<file> <sha256>", compared trimmed
        self.assertEqual(read(os.path.join(py, "python314._pth"), "rb"), b"python314.zip\r\n.\r\n..\\src\r\n")
        want = "python-3.14.8-embed-amd64.zip " + hashlib.sha256(embed).hexdigest()
        self.assertEqual(read(os.path.join(py, "nuc-console-python.txt"), "rb").decode("ascii").strip(), want)
        self.assertEqual(info["python"], "python-3.14.8-embed-amd64.zip")
        ps1 = read(os.path.join(ROOT, "run.ps1"))
        self.assertIn("$want = \"$($pin.file) $($pin.sha256)\"", ps1)  # the stamp run.ps1 compares (Trim()) before it reuses the folder
        self.assertIn("(Get-Content -LiteralPath $stamp -Raw).Trim() -eq $want", ps1)
        self.assertIn("\"$stdlib`r`n.`r`n..\\src\"", ps1)  # and the ._pth it writes

    def test_a_windows_python_that_is_not_the_pinned_one_is_refused(self):
        with self.assertRaisesRegex(dc.CoreError, "pins"):
            self.core("nuc-console-9.9.9-windows-x64.zip", zip_bytes(windows_members(pin="1" * 64)))
        with self.assertRaisesRegex(dc.CoreError, "no python/python-3.14.8-embed-arm64.zip"):
            self.core("nuc-console-9.9.9-windows-arm64.zip", zip_bytes(windows_members()))  # the arm64 archive carries the arm64 zip
        bad = zip_bytes([("python.exe", b"MZ")])
        with self.assertRaisesRegex(dc.CoreError, "not an embeddable Python"):
            self.core("nuc-console-9.9.9-windows-x64.zip", zip_bytes(windows_members(bad)))
        evil = zip_bytes([("../../evil.dll", b"x"), ("python.exe", b"MZ")])
        with self.assertRaisesRegex(dc.CoreError, "not a plain relative path"):
            self.core("nuc-console-9.9.9-windows-x64.zip", zip_bytes(windows_members(evil)))

    def test_what_the_release_build_never_writes_is_refused(self):
        cases = {
            "../outside": [("../outside", b"x", 0o644)],
            "absolute": [("/etc/passwd", b"x", 0o644)],
            "another top": [("other/x", b"x", 0o644)],
            "dot-dot": [(TOP + "/src/../../x", b"x", 0o644)],
            "a link that leaves python/": [(TOP + "/python/bin/evil", ("link", "../../../../etc/passwd"), 0o777)],
            "an absolute link": [(TOP + "/python/bin/evil", ("link", "/etc/passwd"), 0o777)],
            "a link outside python/": [(TOP + "/run2.sh", ("link", "run.sh"), 0o777)],
            "a hard link": [(TOP + "/python/bin/hard", ("hard", TOP + "/run.sh"), 0o644)],
            "a pipe": [(TOP + "/python/fifo", "fifo", 0o644)],
            "a link to nothing": [(TOP + "/python/bin/gone", ("link", "nothing"), 0o777)],
            "a link to a folder": [(TOP + "/python/libs", ("link", "lib"), 0o777)],
        }
        for label, extra in cases.items():
            with self.subTest(label):
                with self.assertRaises(dc.CoreError):
                    self.core("nuc-console-9.9.9-linux-x86_64.tar.gz", tar_gz(unix_members(extra=extra)))
        with self.assertRaisesRegex(dc.CoreError, "not the name of a release archive"):
            self.core("nuc-console.tar.gz", tar_gz(unix_members()))
        with self.assertRaisesRegex(dc.CoreError, "not inside"):
            self.core("nuc-console-9.9.8-linux-x86_64.tar.gz", tar_gz(unix_members()))  # the version of the name is the one of the folder
        without_python = [m for m in unix_members() if "/python/" not in m[0]]
        with self.assertRaisesRegex(dc.CoreError, "without its Python"):
            self.core("nuc-console-9.9.9-linux-x86_64.tar.gz", tar_gz(without_python))
        without_run = [m for m in unix_members() if not m[0].endswith("run.sh")]
        with self.assertRaisesRegex(dc.CoreError, "no run.sh"):
            self.core("nuc-console-9.9.9-macos-arm64.tar.gz", tar_gz(without_run))
        with self.assertRaisesRegex(dc.CoreError, "not a .tar.gz"):
            self.core("nuc-console-9.9.9-linux-x86_64.tar.gz", b"not gzip")

    def test_core_is_replaced_only_when_this_script_made_it(self):
        base = self.tmp()
        out = os.path.join(base, "core")
        files, info = dc.from_archive(self.archive("nuc-console-9.9.9-linux-arm64.tar.gz", tar_gz(unix_members())))
        os.makedirs(out)
        with open(os.path.join(out, "precious.txt"), "w") as f:
            f.write("mine")
        with self.assertRaisesRegex(dc.CoreError, "not a folder this script made"):
            dc.write(files, info, out, log=lambda s: None)
        self.assertEqual(os.listdir(out), ["precious.txt"])  # untouched
        os.remove(os.path.join(out, "precious.txt"))
        dc.write(files, info, out, log=lambda s: None)  # an empty one is fine
        with open(os.path.join(out, "stale.txt"), "w") as f:
            f.write("from an older core")
        dc.write(files, info, out, log=lambda s: None)  # one it made is replaced as a whole
        self.assertNotIn("stale.txt", os.listdir(out))
        self.assertFalse(os.path.exists(out + ".part"))
        if POSIX:
            link = os.path.join(base, "link")
            os.symlink(out, link)
            with self.assertRaises(dc.CoreError):
                dc.write(files, info, link, log=lambda s: None)

    def test_the_command_line(self):
        path = self.archive("nuc-console-9.9.9-macos-x86_64.tar.gz", tar_gz(unix_members()))
        out = os.path.join(self.tmp(), "core")
        said = io.StringIO()
        with contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            self.assertEqual(dc.main(["--archive", path, "--out", out]), 0)
            self.assertTrue(os.path.isfile(os.path.join(out, "python", "bin", "python3")))
            self.assertEqual(dc.main(["--archive", os.path.join(self.tmp(), "missing.tar.gz"), "--out", out]), 1)
        self.assertIn("7 files", said.getvalue())
        self.assertIn("desktop_core: error: missing.tar.gz: not the name of a release archive", said.getvalue())


class CoreFromCheckout(unittest.TestCase):
    def test_the_files_this_checkout_ships_for_each_system_and_no_python(self):
        names = {}
        for system in ("linux", "macos", "windows"):
            with contextlib.redirect_stderr(io.StringIO()):  # "uncommitted changes" while a change is being made
                files, info = dc.from_checkout(system)
            self.assertIsNone(info["python"])
            self.assertEqual(info["version"], br.read_version(ROOT))
            names[system] = set(files)
            for path in files:
                self.assertFalse(path.split("/")[0] in ("tests", "tools", "desktop") or path.startswith("."), (system, path))
        self.assertIn("run.sh", names["linux"])
        self.assertNotIn("run.ps1", names["linux"])
        self.assertIn("run.sh", names["macos"])
        self.assertIn("run.ps1", names["windows"])
        self.assertNotIn("run.sh", names["windows"])
        self.assertIn("src/collector.py", names["windows"])
        with self.assertRaises(dc.CoreError):
            dc.from_checkout("beos")


class Collect(TempCase):
    def bundle(self, files):
        d = self.tmp()
        for rel in files:
            path = os.path.join(d, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(rel.encode())
        return d

    def test_the_packages_are_named_after_the_release_archives(self):
        d = self.bundle(["deb/nuc-console_9.9.9_amd64.deb", "rpm/nuc-console-9.9.9-1.x86_64.rpm", "appimage/nuc-console_9.9.9_amd64.AppImage",
                         "appimage/nuc-console.AppDir/AppRun"])
        out = os.path.join(self.tmp(), "packages")
        names = dc.collect(d, "linux-x86_64", "9.9.9", "deb,rpm,appimage", out, log=lambda s: None)
        self.assertEqual(names, ["nuc-console-desktop-9.9.9-linux-x86_64.deb", "nuc-console-desktop-9.9.9-linux-x86_64.rpm",
                                 "nuc-console-desktop-9.9.9-linux-x86_64.AppImage"])
        self.assertEqual(sorted(os.listdir(out)), sorted(names))
        self.assertEqual(read(os.path.join(out, names[0]), "rb"), b"deb/nuc-console_9.9.9_amd64.deb")
        d = self.bundle(["macos/nuc-console.app/Contents/Info.plist", "dmg/nuc-console_9.9.9_aarch64.dmg"])
        self.assertEqual(dc.collect(d, "macos-arm64", "9.9.9", "app,dmg", out, log=lambda s: None), ["nuc-console-desktop-9.9.9-macos-arm64.dmg"])
        d = self.bundle(["msi/nuc-console_9.9.9_x64_en-US.msi", "nsis/nuc-console_9.9.9_x64-setup.exe"])
        self.assertEqual(dc.collect(d, "windows-x64", "9.9.9", "msi,nsis", out, log=lambda s: None),
                         ["nuc-console-desktop-9.9.9-windows-x64.msi", "nuc-console-desktop-9.9.9-windows-x64-setup.exe"])

    def test_a_missing_doubled_or_unversioned_package_is_refused(self):
        out = os.path.join(self.tmp(), "packages")
        quiet = dict(log=lambda s: None)
        with self.assertRaisesRegex(dc.CoreError, "0 files"):
            dc.collect(self.bundle(["deb/nuc-console_9.9.9_amd64.deb"]), "linux-arm64", "9.9.9", "deb,rpm", out, **quiet)
        with self.assertRaisesRegex(dc.CoreError, "2 files"):
            dc.collect(self.bundle(["deb/a_9.9.9.deb", "deb/b_9.9.9.deb"]), "linux-arm64", "9.9.9", "deb", out, **quiet)
        with self.assertRaisesRegex(dc.CoreError, "not the package of version 9.9.9"):
            dc.collect(self.bundle(["deb/nuc-console_0.0.0_amd64.deb"]), "linux-x86_64", "9.9.9", "deb", out, **quiet)
        for target, bundles in (("windows-x64", "deb"), ("macos-arm64", "msi"), ("linux-x86_64", ""), ("linux-x86_64", "dmg")):
            with self.assertRaisesRegex(dc.CoreError, "--bundles"):
                dc.collect(self.tmp(), target, "9.9.9", bundles, out, **quiet)
        with self.assertRaisesRegex(dc.CoreError, "--target"):
            dc.collect(self.tmp(), "linux", "9.9.9", "deb", out, **quiet)
        with self.assertRaisesRegex(dc.CoreError, "--version"):
            dc.collect(self.tmp(), "linux-x86_64", "v9.9.9", "deb", out, **quiet)


class Config(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conf = json.loads(read(os.path.join(APP, "tauri.conf.json")))

    def test_what_the_app_is_and_where_its_pages_come_from(self):
        c = self.conf
        self.assertEqual(c["productName"], "nuc-console")
        self.assertRegex(c["identifier"], r"^[a-z0-9]+(\.[a-z0-9-]+)+$")
        self.assertNotIn("version", c)  # VERSION of src/nuc_config.py, given by the workflow (--config version.json)
        self.assertEqual(c["build"], {"frontendDist": "../ui"})  # its own start page, no dev server, no remote URL
        self.assertTrue(os.path.isfile(os.path.join(APP, "..", "ui", "index.html")))
        self.assertEqual(c["app"]["windows"], [])  # the app makes its window (hidden at login)
        self.assertFalse(c["app"]["withGlobalTauri"])
        self.assertNotIn("plugins", c)  # no updater key, no shell scope, nothing configured that a page could reach
        self.assertFalse(os.path.exists(os.path.join(APP, "capabilities")), "no capability: no page may call into the app")

    def test_the_start_page_policy_is_strict(self):
        csp = self.conf["app"]["security"]["csp"]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe", csp)
        self.assertNotIn("http", csp)

    def test_the_bundle_carries_the_core_and_the_icons(self):
        b = self.conf["bundle"]
        self.assertEqual(b["resources"], ["core/"])  # tools/desktop_core.py fills it; the app starts core/run.sh or core\run.ps1
        self.assertEqual(b["targets"], "all")  # the workflow names the kinds per system
        for icon in b["icon"]:
            self.assertTrue(os.path.isfile(os.path.join(APP, icon)), icon)
        self.assertEqual(b["macOS"]["signingIdentity"], "-")  # ad-hoc: Apple silicon refuses to run code with no signature at all
        self.assertEqual(b["windows"]["nsis"]["installMode"], "currentUser")  # the setup .exe needs no administrator
        self.assertEqual(b["license"], "MIT")
        self.assertTrue(read(os.path.join(ROOT, "LICENSE")).splitlines()[2].startswith(b["copyright"]), b["copyright"])  # the licence's holder
        self.assertIn("/core/", read(os.path.join(APP, ".gitignore")))  # never committed

    def test_the_crates_are_tauri_and_its_plugins_and_nothing_else(self):
        text = read(os.path.join(APP, "Cargo.toml"))
        sections = {}
        name = None
        for line in text.splitlines():
            m = re.match(r"^\[(.+)\]$", line.strip())
            if m:
                name = m.group(1)
                sections[name] = []
            elif name and re.match(r"^[\w-]+\s*=", line):
                sections[name].append(line.split("=", 1)[0].strip())
        self.assertEqual(sorted(sections["dependencies"]), ["tauri", "tauri-plugin-autostart", "tauri-plugin-opener", "tauri-plugin-single-instance"])
        self.assertEqual(sections["build-dependencies"], ["tauri-build"])
        self.assertEqual(sections["target.'cfg(unix)'.dependencies"], ["libc"])
        self.assertIn('version = "0.0.0"', text)
        self.assertIn('name = "nuc-console"', text)


class Rust(unittest.TestCase):
    """What src/main.rs may do, read as text (it is compiled and tested by the desktop workflow)."""

    @classmethod
    def setUpClass(cls):
        cls.src = read(os.path.join(APP, "src", "main.rs"))

    def test_it_starts_the_portable_core_with_its_data_in_the_users_folder(self):
        src = self.src
        self.assertIn('Command::new("/bin/sh")', src)
        self.assertIn('core.join("run.sh")).args(["--web", "--no-open"])', src)
        self.assertIn(r'System32\WindowsPowerShell\v1.0\powershell.exe', src)  # never a powershell found on the PATH
        self.assertIn('.arg(core.join("run.ps1")).arg("-NoOpen")', src)
        self.assertIn('cmd.env("NUC_CONSOLE_DATA", data)', src)
        self.assertIn("app_local_data_dir()", src)
        self.assertIn('resource_dir()?.join("core")', src)
        for var in ("PYTHONHOME", "PYTHONPATH"):
            self.assertIn('.env_remove("%s")' % var, src)

    def test_the_address_it_reads_is_the_one_the_launchers_and_the_web_view_print(self):
        address = re.search(r'const ADDRESS: &str = "([^"]+)"', self.src).group(1)
        web_log = re.search(r'const WEB_LOG_ADDRESS: &str = "([^"]+)"', self.src).group(1)
        self.assertEqual(address, "dashboard on http://127.0.0.1:")
        self.assertIn('echo "nuc-console: dashboard on $URL', read(os.path.join(ROOT, "run.sh")))
        self.assertIn('Say "dashboard on $url', read(os.path.join(ROOT, "run.ps1")))
        self.assertTrue(read(os.path.join(ROOT, "run.sh")).count("--local"))  # so the address is 127.0.0.1
        self.assertIn("'--local'", read(os.path.join(ROOT, "run.ps1")))
        self.assertEqual(web_log, "nuc-console web view on http://127.0.0.1:")
        self.assertIn('print(f"nuc-console web view on http://', read(os.path.join(ROOT, "src", "web.py")))
        self.assertIn("already running", read(os.path.join(ROOT, "run.sh")))  # an earlier core is found by that message
        self.assertIn("already running", read(os.path.join(ROOT, "run.ps1")))

    def test_the_app_uses_the_cores_token_and_signals_only_the_core(self):
        src, guard = self.src, read(os.path.join(ROOT, "desktop", "src-tauri", "src", "guard.rs"))
        self.assertIn("mod guard;", src)
        self.assertIn('data.join("web.token")', src + guard)   # what src/webhttp.py local_token() writes
        self.assertEqual(re.search(r'LOCAL_TOKEN = "([^"]+)"', read(os.path.join(ROOT, "src", "webhttp.py"))).group(1), "web.token")
        self.assertIn("/app?token={token}", src)                 # the window; the browser gets open-app.html, never the token on a command line
        self.assertNotIn("open_url(format!", src)
        self.assertIn("Authorization: Bearer {token}", src)      # the smoke test
        self.assertIn("guard::is_core(pid, script)", src)       # adopting an earlier core
        self.assertIn("guard::is_core(pid, &s)", src)           # and stopping it
        self.assertEqual(src.count("if !guard::pid_ok(pid)"), 3)  # terminate (unix, windows) and kill: never pid 0 or 1
        self.assertIn("pid > 1 && i32::try_from(pid).is_ok()", guard)

    def test_the_window_shows_only_the_app_and_the_core(self):
        src = self.src
        self.assertIn(".on_navigation(move |url| allowed(&nav, url))", src)
        self.assertIn("same_origin(url, &base)", src)
        self.assertIn('matches!(url.scheme(), "http" | "https" | "mailto")', src)  # a web link goes to the browser
        self.assertNotIn("innerHTML", src)  # the start page gets text, never markup
        self.assertIn("textContent", src)
        self.assertNotRegex(src, r"\bunsafe\s*\{(?![^}]*libc::kill)")  # the only unsafe code sends a signal

    def test_quitting_stops_the_core_and_closing_hides_the_window(self):
        src = self.src
        self.assertIn("RunEvent::Exit => stop(app)", src)
        self.assertIn("libc::SIGTERM", src)  # run.sh stops all it started on SIGTERM
        self.assertIn("process_group(0)", src)
        self.assertIn('"/T", "/F"', src)  # run.ps1's process tree
        self.assertIn("api.prevent_close()", src)
        self.assertIn('Some(vec!["--hidden"])', src)  # start at login: without the window
        for opt in ('"--hidden"', '"--quit"', '"--smoke-test"'):
            self.assertIn(opt, src)

    def test_the_start_page_has_no_script_and_the_ids_the_app_fills(self):
        html = read(os.path.join(ROOT, "desktop", "ui", "index.html"))
        self.assertNotIn("<script", html.lower())
        self.assertNotRegex(html, r"(src|href)=\"(https?:)?//")
        self.assertIn('href="style.css"', html)
        for ident in ("status", "log"):
            self.assertIn('id="%s"' % ident, html)
            self.assertIn('getElementById(\\"%s\\")' % ident, self.src)
        self.assertNotIn("url(", read(os.path.join(ROOT, "desktop", "ui", "style.css")))  # nothing fetched


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR", "not a PNG"
    return struct.unpack(">II", data[16:24])


class Icons(unittest.TestCase):
    def icon(self, name):
        return read(os.path.join(APP, "icons", name), "rb")

    def test_the_icons_are_the_ones_the_script_draws(self):
        self.assertEqual(self.icon("32x32.png"), desktop_icons.png(32))  # drawn the same way every time, from code
        for name, size in (("32x32.png", 32), ("128x128.png", 128), ("128x128@2x.png", 256), ("icon.png", 512)):
            self.assertEqual(png_size(self.icon(name)), (size, size), name)

    def test_the_ico_holds_png_images_from_16_to_256(self):
        data = self.icon("icon.ico")
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind), (0, 1))
        sizes = []
        for i in range(count):
            w, h, _c, _r, _planes, _bits, length, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
            image = data[offset:offset + length]
            self.assertEqual(png_size(image), (w or 256, h or 256))
            sizes.append(w or 256)
        self.assertEqual(sizes, [16, 24, 32, 48, 64, 128, 256])

    def test_the_icns_holds_png_images_of_the_types_it_names(self):
        data = self.icon("icon.icns")
        self.assertEqual(data[:4], b"icns")
        self.assertEqual(struct.unpack(">I", data[4:8])[0], len(data))
        at, found = 8, []
        while at < len(data):
            kind, length = data[at:at + 4], struct.unpack(">I", data[at + 4:at + 8])[0]
            size = {v: k for k, v in desktop_icons.ICNS_TYPES.items()}[kind]
            self.assertEqual(png_size(data[at + 8:at + length]), (size, size))
            found.append(size)
            at += length
        self.assertEqual(found, sorted(desktop_icons.ICNS_TYPES))


def run_blocks_have_no_expressions(test, text):
    """${{ }} in a run: block is shell injection (an input, a name): values go through env: and are quoted there."""
    in_run = False
    for line in text.split("\n"):
        indent = len(line) - len(line.lstrip())
        if re.match(r"\s*(- )?run:\s*\|", line):
            in_run, run_indent = True, indent
            continue
        if in_run and line.strip() and indent <= run_indent:
            in_run = False
        if in_run or re.match(r"\s*(- )?run:\s*\S", line):
            test.assertNotIn("${{", line)


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(os.path.join(ROOT, ".github", "workflows", "desktop.yml")).replace("\r\n", "\n")
        cls.release = read(os.path.join(ROOT, ".github", "workflows", "release.yml")).replace("\r\n", "\n")
        cls.matrix = re.findall(r'\{ target: ([\w-]+), runner: ([\w.-]+), bundles: "([\w,]+)" \}', cls.text)

    def test_every_action_is_pinned_and_nothing_can_write(self):
        uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", self.text, re.M)
        self.assertGreaterEqual(len(uses), 5)
        for ref, rest in uses:
            self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", ref)
            self.assertRegex(rest, r"#\s*v\d+\.\d+\.\d+", ref)
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotRegex(self.text, r"\w: write\b")
        for word in ("id-token", "attestations", "secrets.", "pull_request_target", "continue-on-error"):
            self.assertNotIn(word, self.text)
        self.assertEqual(self.text.count("persist-credentials: false"), self.text.count("actions/checkout@"))
        run_blocks_have_no_expressions(self, self.text)

    def test_when_it_runs(self):
        for path in ('"desktop/**"', '"tools/desktop_core.py"', '"tools/build_release.py"', '"run.sh"', '"run.ps1"', '".github/workflows/desktop.yml"'):
            self.assertIn(path, self.text)
        self.assertIn("workflow_dispatch:", self.text)
        self.assertRegex(self.text, r"workflow_call:\n    inputs:\n      dist:")
        self.assertIn("if: ${{ !inputs.dist }}", self.text)  # called with archives: they are not built again
        self.assertIn("name: ${{ inputs.dist || 'desktop-dist' }}", self.text)

    def test_one_package_job_per_release_archive_on_a_runner_of_its_own(self):
        targets = [t for t, _r, _b in self.matrix]
        archives = [n[len("nuc-console-9.9.9-"):].replace(".tar.gz", "").replace(".zip", "") for n in br.archive_names("9.9.9")]
        self.assertEqual(sorted(targets), sorted(archives))
        self.assertEqual(sorted(targets), sorted(dc.TARGETS))
        runners = {t: r for t, r, _b in self.matrix}
        self.assertEqual(runners["macos-arm64"], "macos-latest")
        self.assertEqual(runners["macos-x86_64"], "macos-15-intel")
        self.assertEqual(runners["linux-arm64"], "ubuntu-22.04-arm")
        self.assertEqual(runners["windows-arm64"], "windows-11-arm")
        self.assertIn("fail-fast: false", self.text)

    def test_the_packages_it_makes_are_the_ones_the_release_publishes(self):
        names = []
        for target, _runner, bundles in self.matrix:
            for kind in bundles.split(","):
                self.assertIn(kind, dc.KINDS[dc.system_of(target)], (target, kind))
                if kind != "app":
                    names.append(target + dc.PACKAGES[kind][2])
        listed = re.search(r"for p in ([^;]+); do", self.release).group(1).replace("\\\n", " ").split()
        self.assertEqual(sorted(names), sorted(listed))
        self.assertIn("-eq %d" % len(names), self.release)

    def test_the_core_comes_from_the_archive_and_every_package_is_installed_and_started(self):
        t = self.text
        for word in ("python tools/desktop_core.py --archive", "python tools/desktop_core.py --collect", "--config version.json",
                     'cargo tauri build --ci --bundles "$BUNDLES"', "cargo fmt --check", "cargo test", 'cargo install tauri-cli --version "^2" --locked'):
            self.assertIn(word, t, word)
        for started in ('"/usr/bin/$name" --smoke-test', '"$image" --smoke-test', '"$app/Contents/MacOS/$bin" --smoke-test', "-ArgumentList '--smoke-test'"):
            self.assertIn(started, t)  # the .deb, the AppImage, the .dmg, the Windows installer: each installed, then started
        for word in ('sudo apt-get install -y "./$deb"', "--which-python", "APPIMAGE_EXTRACT_AND_RUN=1", "hdiutil attach", "codesign --verify",
                     "msiexec.exe", "'/S'", "'^smoke test: ok'", "xvfb-run", "dbus-run-session"):
            self.assertIn(word, t, word)
        self.assertIn("name: desktop-package-${{ matrix.target }}", t)
        self.assertIn("pattern: desktop-package-*", self.release)


if __name__ == "__main__":
    unittest.main()
