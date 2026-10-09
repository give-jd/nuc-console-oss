import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
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
        create = src.index('foreach ($d in @($Data, "$Data\\run", "$Data\\lib", "$Data\\logs", "$Data\\ai", $Cache))')
        acl = src.index("& icacls.exe $Data /inheritance:r")
        self.assertLess(create, acl)  # created inside $Data before the ACL: it inherits "only SYSTEM and Administrators write"
        self.assertLess(acl, src.index("Get-PythonZip  #"))  # and the ACL is set before anything is downloaded into it


SYSTEMD = os.path.join(os.path.dirname(__file__), "..", "systemd")


def unit_lines(name):
    with open(os.path.join(SYSTEMD, name), encoding="utf-8") as f:
        return [ln.strip() for ln in f.read().splitlines() if ln.strip() and not ln.strip().startswith("#")]


class AiFolderIsTheWebAccounts(unittest.TestCase):
    """The AI page and the AI screen download the local model into the AI folder as the account of the web view and of the console: the
    installers give them that folder (and no other place), on each system, and loosen nothing else (docs/AI.md, docs/INSTALL.md)."""

    def test_linux_the_folder_and_its_two_subfolders_belong_to_the_user_of_the_units(self):
        src = read_text(INSTALL)
        self.assertIn("install -d -o nuc-console -g nuc-console -m 0755 /var/lib/nuc-console/ai /var/lib/nuc-console/ai/runtime /var/lib/nuc-console/ai/models", src)
        self.assertLess(src.index("useradd --system --no-create-home --shell /usr/sbin/nologin nuc-console"), src.index("-o nuc-console -g nuc-console -m 0755 /var/lib/nuc-console/ai"),
                        "the user exists before the folder is given to it")
        at = src.index("-o nuc-console -g nuc-console -m 0755 /var/lib/nuc-console/ai")
        self.assertLess(at, src.index("systemctl daemon-reload", at), "before the units are read again and the services restarted")
        self.assertNotRegex(src, r"chown -R .*/var/lib/nuc-console\b(?!/ai)", "nothing else of the state folder is handed over")
        self.assertNotRegex(src, r"chmod -R .*/var/lib/nuc-console", "nor are its permissions loosened")  # the archive's Python in $DEST is another matter

    def test_linux_both_units_may_write_the_ai_folder_and_the_web_unit_keeps_its_sandbox(self):
        web, tty = unit_lines("nuc-console-web.service"), unit_lines("nuc-console.service")
        for name, lines, more in (("web", web, ["ReadWritePaths=-/var/lib/nuc-console-notify/inbox"]), ("tty", tty, [])):
            self.assertEqual([x for x in lines if x.startswith("ReadWritePaths=")], ["ReadWritePaths=-/var/lib/nuc-console/ai"] + more, name)
            for kept in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "User=nuc-console"):
                self.assertIn(kept, lines, "%s: %s stays" % (name, kept))
        for kept in ("ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes", "CapabilityBoundingSet=", "PrivateDevices=yes",
                     "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK", "LockPersonality=yes", "ProtectClock=yes", "ProtectHostname=yes",
                     "RestrictNamespaces=yes", "RestrictSUIDSGID=yes"):
            self.assertIn(kept, web, "the web unit keeps %s" % kept)

    def test_linux_the_web_unit_has_room_for_the_model_server_it_starts_and_still_a_cap(self):
        web = unit_lines("nuc-console-web.service")
        self.assertIn("MemoryMax=85%", web, "the model server is a child, in this unit's cgroup: a share of the machine, the same as the page's own 'too big'")
        self.assertIn("TasksMax=512", web)
        self.assertFalse([x for x in web if x.startswith("MemoryMax=") and x.endswith("M")], "no fixed 256M any more: a model does not fit in it")
        import re as _re
        self.assertEqual(_re.search(r"RAM_MAX_FRAC = ([0-9.]+)", read_text(os.path.join(os.path.dirname(INSTALL), "src", "aihw.py"))).group(1), "0.85",
                         "85% is aihw's line between slow and too big")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_linux_systemd_accepts_the_units(self):
        for name in ("nuc-console-web.service", "nuc-console.service"):
            text = read_text(os.path.join(SYSTEMD, name))
            text = re.sub(r"^ExecStart=.*$", "ExecStart=/bin/true", text, flags=re.M)
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                r = subprocess.run(["systemd-analyze", "verify", "--man=no", path], capture_output=True, text=True)
            bad = [ln for ln in (r.stdout + r.stderr).splitlines()  # only about this unit: the runner's own units have their warnings (snapd.service: RestartMode)
                   if name in ln and any(k in ln for k in ("ReadWritePaths", "MemoryMax", "TasksMax", "Unknown key", "Unknown section", "Unknown lvalue",
                                                            "Invalid", "Failed to parse"))]
            self.assertEqual(bad, [], name)

    def test_macos_the_web_users_folder_is_made_after_the_user_and_owned_by_it(self):
        src = read_text(INSTALL_MACOS)
        self.assertIn('AI_PARENT="/Library/Application Support/nuc-console"', src)
        self.assertIn('install -d -m 0755 -o "$SVC_USER" -g "$SVC_USER" "$AI" "$AI/runtime" "$AI/models"', src)
        self.assertGreater(src.index('install -d -m 0755 -o "$SVC_USER" -g "$SVC_USER" "$AI"'), src.index('dscl . -create "/Users/$SVC_USER" Password'))
        self.assertLess(src.index('install -d -m 0755 -o "$SVC_USER" -g "$SVC_USER" "$AI"'), src.index('fill launchd/com.nuc-console.web.plist'))
        self.assertLess(src.index('install -d -m 0755 "$AI_PARENT"'), src.index('install -d -m 0755 -o "$SVC_USER"'), "the folder above it is root's")
        self.assertNotIn('chown -R', src.split("# ---- 3. collector and web view")[1].split("# ---- 4.")[0].replace('chown "$SVC_USER:$SVC_USER" "$LOG/web.log"', ""))

    def test_windows_local_service_may_write_the_ai_folder_and_nothing_else_new(self):
        src = read_text(INSTALL_WINDOWS)
        self.assertIn("& icacls.exe \"$Data\\ai\" /grant '*S-1-5-19:(OI)(CI)M'", src)
        grants = [ln for ln in src.splitlines() if "icacls.exe" in ln and "$Data" in ln and "'*S-1-5-19:(OI)(CI)M'" in ln]
        self.assertEqual(len(grants), 2, "logs and ai are the only folders LOCAL SERVICE may modify (the others it only reads)")
        self.assertTrue(all(("$Data\\logs" in ln) or ("$Data\\ai" in ln) for ln in grants), grants)
        self.assertIn("& icacls.exe $Data /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' '*S-1-5-19:(OI)(CI)RX'", src,
                      "the data folder's own ACL is as it was: users read, LOCAL SERVICE reads")
        self.assertLess(src.index("& icacls.exe $Data /inheritance:r"), src.index("& icacls.exe \"$Data\\ai\" /grant"))

    def test_every_installer_says_the_web_view_has_buttons_now(self):
        for path in (INSTALL, INSTALL_MACOS, INSTALL_WINDOWS):
            self.assertNotIn("read-only web view", read_text(path), path)


class TelegramInbox(unittest.TestCase):
    """The web view's Telegram page leaves requests in the notifier's inbox: its account may create files there and do nothing else, and only the
    notifier lists and reads them (docs/TELEGRAM.md, SECURITY.md)."""

    def test_linux_the_inbox_is_2730_and_only_the_web_unit_is_in_its_group(self):
        src = read_text(INSTALL)
        self.assertIn("install -d -m 2730 -o nuc-console-notify -g nuc-console-notify /var/lib/nuc-console-notify/inbox", src)
        self.assertIn("chmod 2730 /var/lib/nuc-console-notify/inbox", src)
        self.assertLess(src.index("/var/lib/nuc-console-notify  #"), src.index("/var/lib/nuc-console-notify/inbox"), "inside the notifier's folder")
        web, tty, notify_unit = unit_lines("nuc-console-web.service"), unit_lines("nuc-console.service"), unit_lines("nuc-console-notify.service")
        self.assertIn("SupplementaryGroups=nuc-console-notify", web)
        self.assertIn("ReadWritePaths=-/var/lib/nuc-console-notify/inbox", web)
        self.assertIn("Wants=nuc-console-notify.service", web)
        self.assertFalse([x for x in tty if "nuc-console-notify" in x], "the console needs none of it")
        self.assertIn("User=nuc-console", web)
        self.assertIn("User=nuc-console-notify", notify_unit, "the token stays another account's")
        self.assertIn("UMask=0077", notify_unit)

    def test_macos_the_inbox_is_the_service_accounts(self):
        self.assertIn('install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$NOTIFY_LIB/inbox"', read_text(INSTALL_MACOS))

    def test_windows_local_service_may_only_create_files_in_the_inbox(self):
        src = read_text(INSTALL_WINDOWS)
        line = next(ln for ln in src.splitlines() if ln.startswith('& icacls.exe "$Data\\notify\\inbox"'))
        self.assertIn("/inheritance:r /grant:r", line)
        self.assertIn("'*S-1-5-19:(W)'", line, "write (add a file) on the folder only: no list, no read, not inherited by the files")
        self.assertIn("'*S-1-5-20:(OI)(CI)M'", line, "the notifier reads and deletes them")
        self.assertNotIn("S-1-5-32-545", line, "users have nothing there")
        self.assertLess(src.index('"$Data\\notify\\private" /inheritance:r'), src.index('"$Data\\notify\\inbox" /inheritance:r'))


def powershell():
    for name in (("powershell", "pwsh") if os.name == "nt" else ("pwsh",)):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def ps_run(script, *args, env=None):
    """Runs a PowerShell script (text) from a file; -> CompletedProcess."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.ps1")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        cmd = [powershell(), "-NoProfile", "-NonInteractive"] + (["-ExecutionPolicy", "Bypass"] if os.name == "nt" else []) + ["-File", path] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)


def ps_q(text):
    return "'" + text.replace("'", "''") + "'"


@unittest.skipUnless(powershell(), "PowerShell is not installed")
class WindowsInstallerPowerShell(unittest.TestCase):
    def test_install_windows_ps1_parses(self):
        r = ps_run("param([string]$Path)\n$e = $null; $t = $null\n"
                   "$null = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$t, [ref]$e)\n"
                   "if ($e.Count) { $e | ForEach-Object { Write-Output $_.ToString() }; exit 1 }\n", INSTALL_WINDOWS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# what install-windows.ps1, run.ps1 and bin\nuc-console-update.ps1 do first: PowerShell 7's module folders out of PSModulePath
MODULE_PATH_BLOCK = r"(?ms)^if \(\$PSVersionTable\.PSEdition -eq 'Desktop'.*?^\}$"
WINDOWS_SCRIPTS = (INSTALL_WINDOWS, os.path.join(ROOT, "run.ps1"), os.path.join(ROOT, "bin", "nuc-console-update.ps1"))


class WindowsPowerShellModulePath(unittest.TestCase):
    """Windows PowerShell started from a PowerShell 7 window through cmd.exe (run.cmd, install-windows.cmd) inherits PowerShell 7's
    module folders first in PSModulePath: Get-FileHash, Get-Acl and Get-AuthenticodeSignature were "not recognized"."""

    def test_the_three_scripts_drop_them_before_anything_else(self):
        block = re.search(MODULE_PATH_BLOCK, read_text(INSTALL_WINDOWS)).group(0)
        for path in WINDOWS_SCRIPTS:
            text = read_text(path)
            self.assertEqual(text.count(block), 1, path)  # the same lines in the three
            before = [line for line in re.sub(r"(?s)<#.*?#>", "", text[:text.index(block)]).splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
            # nothing that loads a module comes first: the help, param(), and the two settings of the language
            self.assertEqual(before[-2:], ["$ErrorActionPreference = 'Stop'", "Set-StrictMode -Version 2"], path)
            self.assertRegex(" ".join(before[:-2]), r"^param\(.*\)$", path)

    @unittest.skipUnless(os.name == "nt" and powershell(), "Windows PowerShell (powershell.exe) runs on Windows only")
    def test_powershell_7_folders_go_the_others_stay_and_the_modules_load(self):
        block = re.search(MODULE_PATH_BLOCK, read_text(INSTALL_WINDOWS)).group(0)
        with tempfile.TemporaryDirectory() as d:
            ps7 = os.path.join(d, "pwsh7")                                                   # a PowerShell 7 installed anywhere
            mine, everyone = os.path.join(d, "Documents", "PowerShell", "Modules"), os.path.join(d, "Program Files", "PowerShell", "Modules")
            keep = [os.path.join(d, "Documents", "WindowsPowerShell", "Modules"), os.path.join(d, "tools", "Modules")]
            os.makedirs(os.path.join(ps7, "Modules", "Microsoft.PowerShell.Utility"))
            open(os.path.join(ps7, "pwsh.exe"), "wb").close()
            for p in [mine, everyone] + keep:
                os.makedirs(p)
            # first, as PowerShell 7 puts its own; then what this process has (in CI: the PowerShell 7 that runs the step, for real)
            inherited = [os.path.join(ps7, "Modules"), mine + "\\", everyone, keep[0], "", keep[1]] + os.environ.get("PSModulePath", "").split(";")
            script = ("param([string]$File)\n$ErrorActionPreference = 'Stop'\nSet-StrictMode -Version 2\n" + block + "\n"
                      "Write-Output ('PATH=' + $env:PSModulePath)\n"
                      "Write-Output ('SHA256=' + (Get-FileHash -Algorithm SHA256 -LiteralPath $File).Hash)\n"
                      "Write-Output ('ACES=' + @((Get-Acl -LiteralPath $File).Access).Count)\n")
            env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}  # os.environ has it as PSMODULEPATH on Windows
            env["PSModulePath"] = ";".join(inherited)
            r = ps_run(script, INSTALL_WINDOWS, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = dict(line.rstrip("\r").split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        entries = [os.path.normcase(os.path.normpath(p)) for p in out["PATH"].split(";") if p]
        for gone in (os.path.join(ps7, "Modules"), mine, everyone):
            self.assertNotIn(os.path.normcase(gone), entries)
        for stays in keep:
            self.assertIn(os.path.normcase(stays), entries)
        with open(INSTALL_WINDOWS, "rb") as f:
            self.assertEqual(out["SHA256"].lower(), hashlib.sha256(f.read()).hexdigest())   # Microsoft.PowerShell.Utility: its own
        self.assertGreater(int(out["ACES"]), 0)                                                  # Microsoft.PowerShell.Security too


HARNESS = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
@MODULEPATH@
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
        self.module_path = re.search(MODULE_PATH_BLOCK, src).group(0)  # what the script does first: here too, before anything loads
        self.functions = "\n".join(re.search(pattern, src).group(0) for pattern in (
            r"(?m)^function Get-Sha256.*$", r"(?ms)^function Get-PythonZip \{.*?^\}$"))

    def put(self, where, data=None):
        path = os.path.join(self.tmp, *where)
        with open(path, "wb") as f:
            f.write(self.GOOD if data is None else data)
        return path

    def run_ps(self, pyzip="", served=None):
        served_file = self.put(("served.bin",), self.GOOD if served is None else served)
        script = (HARNESS.replace("@MODULEPATH@", self.module_path)
                  .replace("@HERE@", ps_q(self.here)).replace("@CACHE@", ps_q(self.cache)).replace("@PYZIP@", ps_q(pyzip))
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


# ---- the Python of a release archive: install.sh (Linux) and install-macos.sh use it, run for real in bash ---------------------------

def write_file(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def archive_python(folder, ok=True):
    """A python/ like the one of an archive (a link python3 -> python3.99, a library, a link to it), in a hostile umask: 0700 / 0600.
    The fake python3.99 is a script: it exits 0 for `-c` when ok (so `Python 3.8+?` is answered yes), else 1."""
    write_file(os.path.join(folder, "python", "bin", "python3.99"), "#!/bin/sh\n" + ("exit 0\n" if ok else "exit 1\n"), 0o700)
    os.symlink("python3.99", os.path.join(folder, "python", "bin", "python3"))
    write_file(os.path.join(folder, "python", "lib", "libpython3.99.so.1.0"), "lib", 0o700)
    os.symlink("libpython3.99.so.1.0", os.path.join(folder, "python", "lib", "libpython3.99.so"))
    write_file(os.path.join(folder, "python", "lib", "python3.99", "os.py"), "# os\n", 0o600)
    for dirpath, _dirs, _files in os.walk(os.path.join(folder, "python")):
        os.chmod(dirpath, 0o700)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "install.sh runs on Linux")
class LinuxInstallerPython(unittest.TestCase):
    """install.sh: the system's /usr/bin/python3 when it is 3.8+, else the archive's python/ (copied to /opt/nuc-console/python)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nuc-install-py-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = read_text(INSTALL)
        self.choose = re.search(r"(?ms)^# ---- Python 3\.8\+: the system's, else the one of the archive.*?^fi\n", self.src).group(0)
        self.copy = re.search(r"(?ms)^if \[ \"\$PY\" != \"\$SYS_PY\" \]; then\n    # the archive's Python.*?^fi\n", self.src).group(0)
        self.py_ok = re.search(r"(?m)^py_ok\(\) .*$", self.src).group(0)
        self.dest = os.path.join(self.tmp, "opt", "nuc-console")
        self.folder = os.path.join(self.tmp, "archive")
        os.makedirs(self.folder)
        os.makedirs(self.dest)
        stubs = os.path.join(self.tmp, "stubs")
        write_file(os.path.join(stubs, "chown"), "#!/bin/sh\nexit 0\n", 0o755)  # the test is not root: the owner is not changed here
        self.env = dict(os.environ, PATH=stubs + os.pathsep + os.environ["PATH"])

    def choose_python(self, system_python):
        script = "set -euo pipefail\nDEST=%s\n%s\necho \"PY=$PY\"\n" % (
            shlex.quote(self.dest), self.choose.replace("SYS_PY=/usr/bin/python3", "SYS_PY=" + shlex.quote(system_python)))
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=self.folder, env=self.env, timeout=60)

    def good_system_python(self, ok=True):
        path = os.path.join(self.tmp, "sys", "python3")
        write_file(path, "#!/bin/sh\n" + ("exit 0\n" if ok else "exit 1\n"), 0o755)
        return path

    def test_the_system_python_is_used_when_there_is_one_even_with_the_archive_python_there(self):
        archive_python(self.folder)
        sys_py = self.good_system_python()
        r = self.choose_python(sys_py)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "PY=" + sys_py), r.stderr)

    def test_no_system_python_or_one_older_than_3_8_gives_the_one_of_the_archive(self):
        archive_python(self.folder)
        for system in (os.path.join(self.tmp, "nowhere", "python3"), self.good_system_python(ok=False)):
            r = self.choose_python(system)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "PY=" + os.path.join(self.dest, "python", "bin", "python3")), (system, r.stderr))

    def test_neither_is_an_error_that_says_what_to_do(self):
        r = self.choose_python(os.path.join(self.tmp, "nowhere", "python3"))  # a clone on a machine without python3: no python/ either
        self.assertEqual(r.returncode, 1)
        self.assertIn("Python 3.8 or newer is needed", r.stderr)
        self.assertIn("release archive", r.stderr)

    def copy_python(self, ok=True):
        archive_python(self.folder, ok)
        script = ("set -euo pipefail\nDEST=%s\nSYS_PY=/usr/bin/python3\nPY=$DEST/python/bin/python3\n%s\n%s\n"
                  % (shlex.quote(self.dest), self.py_ok, self.copy))
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=self.folder, env=self.env, timeout=60)

    def mode(self, *parts):
        return os.lstat(os.path.join(self.dest, *parts)).st_mode & 0o777

    def test_the_python_is_copied_with_its_links_and_readable_by_the_service_users(self):
        r = self.copy_python()
        self.assertEqual(r.returncode, 0, r.stderr)
        py = os.path.join(self.dest, "python")
        self.assertEqual(os.readlink(os.path.join(py, "bin", "python3")), "python3.99")  # links stay links
        self.assertEqual(os.readlink(os.path.join(py, "lib", "libpython3.99.so")), "libpython3.99.so.1.0")
        self.assertEqual(sorted(os.listdir(self.dest)), ["python"])  # no python.new or python.old left
        for rel in (("python",), ("python", "bin"), ("python", "lib", "python3.99")):  # extracted with umask 077: now 0755
            self.assertEqual(self.mode(*rel), 0o755, rel)
        self.assertEqual(self.mode("python", "bin", "python3.99"), 0o755)  # exec kept, others may run it
        self.assertEqual(self.mode("python", "lib", "python3.99", "os.py"), 0o644)  # others may read it, nobody else may write it
        self.assertEqual(self.mode("python", "lib", "libpython3.99.so.1.0"), 0o755)
        self.assertIn("using the Python of the archive", r.stdout)

    def test_a_python_that_does_not_run_here_stops_the_install_and_leaves_the_old_one(self):
        write_file(os.path.join(self.dest, "python", "marker"), "the old install")
        r = self.copy_python(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not run on this machine", r.stderr)
        self.assertEqual(sorted(os.listdir(self.dest)), ["python"])  # python.new removed
        self.assertTrue(os.path.exists(os.path.join(self.dest, "python", "marker")))  # the installed one is as it was

    def test_a_new_python_replaces_the_old_one_whole(self):
        write_file(os.path.join(self.dest, "python", "old-file"), "from the last release")
        self.assertEqual(self.copy_python().returncode, 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.dest, "python"))), ["bin", "lib"])

    def test_the_units_and_the_commands_that_name_the_system_python_are_all_rewritten(self):
        """Every systemd unit (the glob) and every sh command of bin/ that runs /usr/bin/python3 is in the sed of install.sh."""
        sed = re.search(r'(?ms)^    sed -i "s\|\$SYS_PY\|\$PY\|g" (.*?)\nfi\n', self.src).group(1)
        self.assertIn("/etc/systemd/system/nuc-console*.service", sed)
        named = set()
        for m in re.finditer(r"/usr/local/(?:s?bin)/nuc-console-\{([^}]*)\}", sed):
            named |= set("nuc-console-" + n for n in m.group(1).split(","))
        users = set()
        for name in os.listdir(os.path.join(ROOT, "bin")):
            if not name.endswith((".cmd", ".ps1")) and "/usr/bin/python3" in read_text(os.path.join(ROOT, "bin", name)):
                users.add(name)
        self.assertEqual(users, named)  # a new command that runs the system Python must be added to that sed
        for name in os.listdir(os.path.join(ROOT, "systemd")):
            text = read_text(os.path.join(ROOT, "systemd", name))
            if "/usr/bin/python3" in text:
                fixed = text.replace("/usr/bin/python3", "/opt/nuc-console/python/bin/python3")
                self.assertNotIn("/usr/bin/python3", fixed)
                self.assertRegex(fixed, r"(?m)^ExecStart=/opt/nuc-console/python/bin/python3 ")
        # and the steps that run the installed code use the chosen Python, not whatever python3 the PATH has
        for line in self.src.splitlines():
            if "$DEST/render.py" in line or "$DEST/web.py" in line or "$DEST/notify.py" in line:
                self.assertIn('"$PY" "$DEST/', line)

    def test_the_uninstall_removes_the_python_with_the_rest(self):
        down = self.src[self.src.index('"--uninstall" ]'):self.src.index("exit 0")]
        self.assertIn('rm -rf "$DEST"', down)  # /opt/nuc-console/python is inside it


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "the macOS tools are replaced by stand-ins that run on Linux only")
class MacosInstallerPython(unittest.TestCase):
    """install-macos.sh: the archive's Python is copied to /opt/nuc-console/python, checked, and used; nothing is downloaded."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nuc-install-macpy-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = read_text(INSTALL_MACOS)
        self.dest = os.path.join(self.tmp, "opt", "nuc-console")
        self.folder = os.path.join(self.tmp, "archive")
        os.makedirs(self.folder)
        os.makedirs(self.dest)
        stubs = os.path.join(self.tmp, "stubs")
        for name, text in (("stat", "#!/bin/sh\n[ \"$1\" = -f ] && [ \"$2\" = %u ] && { echo 0; exit 0; }\necho \"stat stand-in: $*\" >&2; exit 2\n"),
                           ("chown", "#!/bin/sh\nexit 0\n"), ("xattr", "#!/bin/sh\necho \"$*\" >> \"$STUB_LOG\"\n")):
            write_file(os.path.join(stubs, name), text, 0o755)
        self.log = os.path.join(self.tmp, "xattr.log")
        self.env = dict(os.environ, PATH=stubs + os.pathsep + os.environ["PATH"], STUB_LOG=self.log)
        functions = [re.search(r"(?ms)^py_ok\(\) \{.*?^\}$", self.src).group(0), re.search(r"(?m)^python_real\(\) \{.*\}$", self.src).group(0)]
        functions.append(re.search(r"(?ms)^prepare_bundled_python\(\) \{.*?^\}$", self.src).group(0))
        self.functions = "\n".join(functions)

    def prepare(self, ok=True):
        archive_python(self.folder, ok)
        script = "set -euo pipefail\nDEST=%s\n%s\nprepare_bundled_python\n" % (shlex.quote(self.dest), self.functions)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=self.folder, env=self.env, timeout=60)

    def test_the_python_is_copied_beside_the_final_one_checked_and_cleaned_of_quarantine_marks(self):
        r = self.prepare()
        self.assertEqual(r.returncode, 0, r.stderr)
        new = os.path.join(self.dest, "python.new")
        self.assertEqual(os.readlink(os.path.join(new, "bin", "python3")), "python3.99")
        self.assertEqual(os.lstat(os.path.join(new, "bin")).st_mode & 0o777, 0o755)
        self.assertEqual(os.lstat(os.path.join(new, "lib", "python3.99", "os.py")).st_mode & 0o777, 0o644)
        self.assertEqual(sorted(os.listdir(self.dest)), ["python.new"])  # the final one is put in place later, with the services stopped
        with open(self.log) as f:
            self.assertIn("-cr %s" % new, f.read())  # a browser download marks every file: the copy that root runs has none

    def test_a_python_that_does_not_run_on_this_mac_stops_the_install(self):
        r = self.prepare(ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not run on this Mac", r.stderr)
        self.assertIn("macos-arm64", r.stderr)
        self.assertEqual(os.listdir(self.dest), [])

    def test_the_archives_python_comes_first_and_nothing_is_downloaded_for_it(self):
        src = self.src
        use = src.index("    prepare_bundled_python\n    PY=")
        self.assertLess(use, src.index("for cand in /Library/Frameworks/Python.framework"))  # before the search for a system Python
        self.assertLess(use, src.index("    fetch_python_pkg\n    tmp="))  # and before the download of the python.org package
        self.assertIn('if [ -x python/bin/python3 ]; then', src)
        self.assertIn('if [ -z "$PY" ] && xcode-select', src)  # the old search is still there, for a clone
        # in place only after the services are stopped, and the old one of an earlier install is dropped when it is not used any more
        place = src.index('mv "$DEST/python.new" "$DEST/python"')
        self.assertLess(src.index('launchctl bootout "system/$label" 2>/dev/null || true; done\ninstall -d'), place)
        self.assertLess(place, src.index('install -m 0644 src/*.py "$DEST/"'))


if __name__ == "__main__":
    unittest.main()
