import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL = os.path.join(ROOT, "install.sh")
INSTALL_MACOS = os.path.join(ROOT, "install-macos.sh")
INSTALL_WINDOWS = os.path.join(ROOT, "install-windows.ps1")


def old_values(local_conf, unit):
    """Runs the two OLD_TZ / OLD_VT lines of install.sh against temp files (this is what a re-install reads)."""
    src = open(INSTALL).read()
    lines = [re.search(r"^%s=.*$" % name, src, re.M).group(0) for name in ("OLD_TZ", "OLD_VT")]
    with tempfile.TemporaryDirectory() as d:
        if local_conf is not None:
            open(os.path.join(d, "local.conf"), "w").write(local_conf)
        if unit is not None:
            open(os.path.join(d, "unit.service"), "w").write(unit)
        script = "set -euo pipefail\nUNITD=%s\nUNITF=%s\n%s\n%s\nprintf '%%s|%%s' \"$OLD_TZ\" \"$OLD_VT\"\n" % (
            d, os.path.join(d, "unit.service"), lines[0], lines[1])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return tuple(r.stdout.split("|"))


@unittest.skipUnless(sys.platform.startswith("linux"), "these lines run on Linux only (GNU sed); macOS: install-macos.sh, Windows: install-windows.ps1")
class ReinstallKeepsChoices(unittest.TestCase):
    """A re-install must keep the time zone and the VT already chosen (a broken sed silently dropped them: the clock went UTC)."""

    def test_reads_zone_and_vt_from_the_drop_in(self):
        self.assertEqual(old_values("[Service]\nTTYPath=/dev/tty3\nEnvironment=TZ=Europe/Rome\n", None), ("Europe/Rome", "3"))

    def test_both_files_carry_the_zone_no_sigpipe_first_wins(self):
        conf = "[Service]\nEnvironment=TZ=Europe/Rome\n"
        self.assertEqual(old_values(conf, "[Service]\nEnvironment=TZ=Asia/Tokyo\n"), ("Europe/Rome", ""))

    def test_zone_from_the_old_unit_only(self):
        self.assertEqual(old_values("[Service]\nTTYPath=/dev/tty1\n", "[Service]\nEnvironment=TZ=Europe/Rome\n"), ("Europe/Rome", "1"))

    def test_nothing_installed_yet(self):
        self.assertEqual(old_values(None, None), ("", ""))


class ConfigStaysAndDistRefreshes(unittest.TestCase):
    def test_config_is_never_overwritten_but_the_dist_copy_is_refreshed(self):
        src = open(INSTALL).read()
        self.assertRegex(src, r"\[ -e /etc/nuc-console/config\.ini \] \|\| install .* /etc/nuc-console/config\.ini\b")
        self.assertIn("config/config.ini /etc/nuc-console/config.ini.dist", src)


# ---- what the installers download is kept and never downloaded again -------------------------------------------------------

def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class DownloadsAreKept(unittest.TestCase):
    """Static checks: the cache locations, and that nothing downloads outside them."""

    def test_macos_installs_the_package_from_the_cache_and_downloads_only_there(self):
        src = read_text(INSTALL_MACOS)
        self.assertRegex(src, r"(?m)^CACHE=/Library/Caches/nuc-console\b")
        self.assertIn('installer -pkg "$PKG"', src)
        self.assertEqual(len(re.findall(r"curl .*python\.org", src)), 1)  # the one download of a package: inside fetch_python_pkg
        body = re.search(r"(?ms)^fetch_python_pkg\(\) \{.*?^\}$", src).group(0)
        self.assertIn('curl -fsSL -o "$DL/python.pkg"', body)
        self.assertIn('mktemp -d "$CACHE/.download.XXXXXX"', body)  # a temporary name in the same folder, then mv: atomic
        self.assertIn('mv -f "$DL/python.pkg" "$PKG"', body)
        self.assertLess(body.index("shasum"), body.index("mv -f"))  # hash and signature before the name that is reused
        self.assertLess(body.index("pkgutil --check-signature"), body.index("mv -f"))

    def test_windows_looks_in_script_folder_then_cache_and_downloads_into_the_cache(self):
        src = read_text(INSTALL_WINDOWS)
        self.assertIn("$Cache = Join-Path $Data 'cache'", src)
        self.assertNotIn("GetTempPath", src)  # the download used to go to %TEMP% and was deleted after the install
        body = re.search(r"(?ms)^function Get-PythonZip \{.*?^\}$", src).group(0)
        lookup = "@((Join-Path (Join-Path $Here 'python') $build.file), (Join-Path $Here $build.file), $cached)"  # in this order
        order = [body.index(token) for token in ("if ($PythonZip)", lookup, "Invoke-WebRequest")]
        self.assertEqual(order, sorted(order))
        self.assertIn('$part = "$cached.download"', body)
        self.assertLess(body.index("Get-Sha256 $part"), body.index("Move-Item"))  # checked, then it gets the name that is reused

    def test_windows_cache_has_the_acl_of_the_data_folder(self):
        src = read_text(INSTALL_WINDOWS)
        create = src.index('foreach ($d in @($Data, "$Data\\run", "$Data\\lib", "$Data\\logs", $Cache))')
        acl = src.index("& icacls.exe $Data /inheritance:r")
        self.assertLess(create, acl)  # created inside $Data before the ACL: it inherits "only SYSTEM and Administrators write"
        self.assertLess(acl, src.index("Get-PythonZip  #"))  # and the ACL is set before anything is downloaded into it


def powershell():
    for name in (("powershell", "pwsh") if os.name == "nt" else ("pwsh",)):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def ps_run(script, *args):
    """Runs a PowerShell script (text) from a file; -> CompletedProcess."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.ps1")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        cmd = [powershell(), "-NoProfile", "-NonInteractive"] + (["-ExecutionPolicy", "Bypass"] if os.name == "nt" else []) + ["-File", path] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def ps_q(text):
    return "'" + text.replace("'", "''") + "'"


@unittest.skipUnless(powershell(), "PowerShell is not installed")
class WindowsInstallerPowerShell(unittest.TestCase):
    def test_install_windows_ps1_parses(self):
        r = ps_run("param([string]$Path)\n$e = $null; $t = $null\n"
                   "$null = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$t, [ref]$e)\n"
                   "if ($e.Count) { $e | ForEach-Object { Write-Output $_.ToString() }; exit 1 }\n", INSTALL_WINDOWS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


HARNESS = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$PyVersion = '3.99.1'
$arch = 'AMD64'
$Here = @HERE@
$Cache = @CACHE@
$PythonZip = @PYZIP@
$build = @{ file = 'python-3.99.1-embed-amd64.zip'; sha256 = @SHA@ }
$script:Downloads = 0
$script:Uri = ''
$script:Served = [IO.File]::ReadAllBytes(@SERVED@)
function Say($text) { Write-Host "nuc-console: $text" }
function Invoke-WebRequest {  # the real one would go to python.org: this one writes what the test serves
    param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile)
    $script:Downloads++
    $script:Uri = $Uri
    [IO.File]::WriteAllBytes($OutFile, $script:Served)
}
@FUNCTIONS@
try { $r = Get-PythonZip; Write-Output "RESULT=$r" } catch { Write-Output "ERROR=$($_.Exception.Message)" }
Write-Output "DOWNLOADS=$script:Downloads"
Write-Output "URI=$script:Uri"
"""


@unittest.skipUnless(powershell(), "PowerShell is not installed")
class WindowsPythonZipLookup(unittest.TestCase):
    """Get-PythonZip of install-windows.ps1, run for real with a stand-in for the download."""
    GOOD = b"the embeddable python, as pinned\n" * 8
    FILE = "python-3.99.1-embed-amd64.zip"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nuc-install-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.here, self.cache = os.path.join(self.tmp, "here"), os.path.join(self.tmp, "cache")
        for d in (os.path.join(self.here, "python"), self.cache):
            os.makedirs(d)
        src = read_text(INSTALL_WINDOWS)
        self.functions = "\n".join(re.search(pattern, src).group(0) for pattern in (
            r"(?m)^function Get-Sha256.*$", r"(?ms)^function Get-PythonZip \{.*?^\}$"))

    def put(self, where, data=None):
        path = os.path.join(self.tmp, *where)
        with open(path, "wb") as f:
            f.write(self.GOOD if data is None else data)
        return path

    def run_ps(self, pyzip="", served=None):
        served_file = self.put(("served.bin",), self.GOOD if served is None else served)
        script = (HARNESS.replace("@HERE@", ps_q(self.here)).replace("@CACHE@", ps_q(self.cache)).replace("@PYZIP@", ps_q(pyzip))
                  .replace("@SHA@", ps_q(hashlib.sha256(self.GOOD).hexdigest())).replace("@SERVED@", ps_q(served_file))
                  .replace("@FUNCTIONS@", self.functions))
        r = ps_run(script)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = {}
        for line in r.stdout.splitlines():
            for key in ("RESULT", "ERROR", "DOWNLOADS", "URI"):
                if line.startswith(key + "="):
                    out[key.lower()] = line[len(key) + 1:].rstrip("\r")
        out["downloads"] = int(out["downloads"])
        return out

    def same(self, a, b):
        self.assertEqual(os.path.normcase(os.path.normpath(a)), os.path.normcase(os.path.normpath(b)))

    def cache_files(self):
        return sorted(os.listdir(self.cache))

    def test_the_zip_shipped_in_the_release_is_used_without_download(self):
        shipped = self.put(("here", "python", self.FILE))
        r = self.run_ps()
        self.same(r["result"], shipped)
        self.assertEqual((r["downloads"], self.cache_files()), (0, []))

    def test_a_zip_next_to_the_script_is_used(self):
        beside = self.put(("here", self.FILE))
        r = self.run_ps()
        self.same(r["result"], beside)
        self.assertEqual(r["downloads"], 0)

    def test_the_cache_is_used_without_download(self):
        cached = self.put(("cache", self.FILE))
        r = self.run_ps()
        self.same(r["result"], cached)
        self.assertEqual(r["downloads"], 0)

    def test_a_copy_with_another_hash_is_skipped_for_one_that_matches(self):
        self.put(("here", "python", self.FILE), b"damaged")
        cached = self.put(("cache", self.FILE))
        r = self.run_ps()
        self.same(r["result"], cached)
        self.assertEqual(r["downloads"], 0)

    def test_nothing_anywhere_downloads_once_into_the_cache_and_the_next_run_reuses_it(self):
        r = self.run_ps()
        self.assertNotIn("error", r)
        self.same(r["result"], os.path.join(self.cache, self.FILE))
        self.assertEqual(r["downloads"], 1)
        self.assertEqual(r["uri"], "https://www.python.org/ftp/python/3.99.1/" + self.FILE)
        self.assertEqual(self.cache_files(), [self.FILE])  # no temporary name left
        with open(os.path.join(self.cache, self.FILE), "rb") as f:
            self.assertEqual(f.read(), self.GOOD)
        again = self.run_ps()
        self.same(again["result"], os.path.join(self.cache, self.FILE))
        self.assertEqual(again["downloads"], 0)

    def test_a_damaged_cached_copy_is_replaced(self):
        self.put(("cache", self.FILE), b"half a download")
        r = self.run_ps()
        self.assertEqual(r["downloads"], 1)
        with open(os.path.join(self.cache, self.FILE), "rb") as f:
            self.assertEqual(f.read(), self.GOOD)

    def test_what_an_interrupted_download_left_is_never_used(self):
        self.put(("cache", self.FILE + ".download"), b"interrupted")
        r = self.run_ps()
        self.assertEqual(r["downloads"], 1)
        self.assertEqual(self.cache_files(), [self.FILE])

    def test_a_download_with_the_wrong_hash_is_refused_and_leaves_nothing(self):
        r = self.run_ps(served=b"not what python.org signed")
        self.assertIn("expected", r["error"])
        self.assertEqual((r["downloads"], self.cache_files()), (1, []))

    def test_pythonzip_wins_over_everything(self):
        self.put(("here", "python", self.FILE))
        mine = self.put(("mine.zip",))
        r = self.run_ps(pyzip=mine)
        self.same(r["result"], mine)
        self.assertEqual((r["downloads"], self.cache_files()), (0, []))

    def test_pythonzip_must_have_the_pinned_hash(self):
        self.put(("here", "python", self.FILE))  # a good one is at hand, but -PythonZip is what the admin asked for
        bad = self.put(("bad.zip",), b"something else")
        r = self.run_ps(pyzip=bad)
        self.assertIn("not installed", r["error"])
        self.assertEqual(r["downloads"], 0)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash") and shutil.which("sha256sum") and shutil.which("stat"),
                     "the macOS tools are replaced by stand-ins that run on Linux only")
class MacosPythonPackageCache(unittest.TestCase):
    """pkg_ok and fetch_python_pkg of install-macos.sh, run for real in bash with stand-ins for what only macOS has
    (BSD stat, shasum, pkgutil, chown) and for curl, which serves a local file and counts its calls."""
    GOOD = b"the python.org package, as pinned\n" * 8
    STUBS = {
        # BSD `stat -f '%u %Lp' PATH`: every file looks root-owned, the mode is the real one
        "stat": '#!/bin/sh\n[ "$1" = -f ] && [ "$2" = \'%u %Lp\' ] || { echo "stat stand-in: $*" >&2; exit 2; }\n'
                'printf \'0 %s\\n\' "$(@STAT@ -c %a -- "$3")"\n',
        "shasum": '#!/bin/sh\n[ "$*" = "-a 256 -c -" ] || { echo "shasum stand-in: $*" >&2; exit 2; }\nexec @SHA256SUM@ -c -\n',
        "pkgutil": '#!/bin/sh\nif grep -q BADSIG "$2"; then echo "Status: no signature"; '
                   'else echo "   1. Developer ID Installer: Python Software Foundation (BMM5U3QETM)"; fi\n',
        "chown": "#!/bin/sh\nexit 0\n",
        "curl": '#!/bin/sh\necho "$*" >> "$STUB_LOG"\n[ -z "${STUB_FAIL:-}" ] || exit 22\n'
                'while [ $# -gt 1 ]; do if [ "$1" = -o ]; then out="$2"; fi; shift; done\ncat "$STUB_SERVE" > "$out"\n',
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nuc-install-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "Caches", "nuc-console")
        os.makedirs(os.path.dirname(self.cache))
        self.log = os.path.join(self.tmp, "curl.log")
        self.served = os.path.join(self.tmp, "served.bin")
        stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        for name, text in self.STUBS.items():
            path = os.path.join(stubs, name)
            with open(path, "w") as f:
                f.write(text.replace("@STAT@", shutil.which("stat")).replace("@SHA256SUM@", shutil.which("sha256sum")))
            os.chmod(path, 0o755)
        self.env = dict(os.environ, PATH=stubs + os.pathsep + os.environ["PATH"], STUB_LOG=self.log, STUB_SERVE=self.served)
        self.env.pop("STUB_FAIL", None)
        src = read_text(INSTALL_MACOS)
        self.functions = "\n".join(re.search(r"(?ms)^%s\(\) \{.*?^\}$" % name, src).group(0) for name in ("pkg_ok", "fetch_python_pkg"))
        self.serve(self.GOOD)

    def serve(self, data):
        with open(self.served, "wb") as f:
            f.write(data)

    def fetch(self, **env):
        script = ("set -euo pipefail\nCACHE=%s\nPY_VERSION=9.9.9\nPY_PKG_SHA256=%s\n%s\nfetch_python_pkg\necho \"PKG=$PKG\"\n"
                  % (shlex.quote(self.cache), hashlib.sha256(self.GOOD).hexdigest(), self.functions))
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=dict(self.env, **env), timeout=60)

    def downloads(self):
        if not os.path.exists(self.log):
            return 0
        with open(self.log) as f:
            return len(f.read().splitlines())

    def pkg(self):
        return os.path.join(self.cache, "python-9.9.9-macos11.pkg")

    def mode(self, path):
        return os.stat(path).st_mode & 0o777

    def content(self, path):
        with open(path, "rb") as f:
            return f.read()

    def test_first_install_downloads_into_the_cache_and_the_second_reuses_it(self):
        r = self.fetch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PKG=" + self.pkg(), r.stdout)
        self.assertEqual(self.downloads(), 1)
        self.assertIn("https://www.python.org/ftp/python/9.9.9/python-9.9.9-macos11.pkg", self.content(self.log).decode())
        self.assertEqual(os.listdir(self.cache), ["python-9.9.9-macos11.pkg"])  # no .download.* left
        self.assertEqual((self.mode(self.cache), self.mode(self.pkg())), (0o755, 0o644))
        self.assertEqual(self.content(self.pkg()), self.GOOD)
        again = self.fetch()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already downloaded", again.stdout)
        self.assertEqual(self.downloads(), 1)  # not again

    def test_a_damaged_copy_is_downloaded_again(self):
        self.assertEqual(self.fetch().returncode, 0)
        with open(self.pkg(), "wb") as f:
            f.write(b"half a package")
        os.chmod(self.pkg(), 0o644)
        self.assertEqual(self.fetch().returncode, 0)
        self.assertEqual((self.downloads(), self.content(self.pkg())), (2, self.GOOD))

    def test_a_copy_that_is_not_root_s_0644_is_not_trusted_even_if_the_hash_matches(self):
        self.assertEqual(self.fetch().returncode, 0)
        os.chmod(self.pkg(), 0o666)  # writable by others: it may change between the check and the install
        self.assertEqual(self.fetch().returncode, 0)
        self.assertEqual((self.downloads(), self.mode(self.pkg())), (2, 0o644))

    def test_a_cache_folder_others_can_write_is_replaced(self):
        os.makedirs(self.cache)
        with open(self.pkg(), "wb") as f:
            f.write(self.GOOD)
        os.chmod(self.pkg(), 0o644)
        os.chmod(self.cache, 0o777)  # anyone could have put (or swapped) the file in there
        r = self.fetch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.downloads(), self.mode(self.cache)), (1, 0o755))

    def test_a_symbolic_link_in_place_of_the_cache_is_replaced(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        os.symlink(other, self.cache)
        self.assertEqual(self.fetch().returncode, 0)
        self.assertFalse(os.path.islink(self.cache))
        self.assertEqual(os.listdir(other), [])  # nothing was written through the link

    def test_a_download_with_the_wrong_hash_is_refused_and_leaves_nothing(self):
        self.serve(b"not what python.org published\n")
        r = self.fetch()
        self.assertEqual(r.returncode, 1)
        self.assertIn("wrong SHA-256", r.stderr)
        self.assertEqual(os.listdir(self.cache), [])

    def test_a_package_without_the_psf_signature_is_refused_and_leaves_nothing(self):
        self.GOOD = b"BADSIG signed by somebody else\n" * 4  # the hash is of this one, the signature check is what fails
        self.serve(self.GOOD)
        r = self.fetch()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not signed by the Python Software Foundation", r.stderr)
        self.assertEqual(os.listdir(self.cache), [])

    def test_a_failed_download_leaves_nothing(self):
        r = self.fetch(STUB_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("download failed", r.stderr)
        self.assertEqual(os.listdir(self.cache), [])

    def test_the_package_of_an_older_pin_is_removed_and_other_files_stay(self):
        os.makedirs(self.cache, mode=0o755)
        os.chmod(self.cache, 0o755)
        for name in ("python-9.0.0-macos11.pkg", "other.txt"):
            with open(os.path.join(self.cache, name), "wb") as f:
                f.write(b"x")
        self.assertEqual(self.fetch().returncode, 0)
        self.assertEqual(sorted(os.listdir(self.cache)), ["other.txt", "python-9.9.9-macos11.pkg"])


if __name__ == "__main__":
    unittest.main()
