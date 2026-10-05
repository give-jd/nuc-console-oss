#Requires -Version 5.1
<#
.SYNOPSIS
  nuc-console, portable (Windows): run it from the extracted folder, double-click run.cmd. No installation, no service, no
  system change outside this folder (except the browser it opens).

.DESCRIPTION
    run.cmd                       the dashboard in your browser, on http://127.0.0.1:<a free port>
    run.cmd -Port 8787            a port of your choice
    run.cmd -NoOpen               only print the address
    run.cmd -Problems             what needs attention now, why it matters and how to fix it (-Problems --json: for scripts)
    run.cmd -Accept               accept the ports exposed right now as the baseline of the port alarms
    run.cmd -Accept --problem ID --reason "why"      a known ATTENTION item (--forget ID undoes it)

  What it does:
    1. finds Python: the official embeddable one that the release ZIP carries in python\ (unpacked once into python\ after its
       SHA-256 is checked against the pin in install-windows.ps1, then reused), else a Python 3.8+ of this machine (py -3,
       python);
    2. keeps everything it writes (config.ini, state, the baseline of the port alarms, logs) in .\data (or in
       $env:NUC_CONSOLE_DATA, an absolute path), which is created on the first run (config.ini is copied there once: your edits
       stay);
    3. starts the collector (as you: without administrator rights the sections that need them show less; right-click run.cmd,
       Run as administrator, shows everything) and the web view on 127.0.0.1 only (no token: nothing else can connect), and
       opens your default browser;
    4. closing this window or Ctrl+C stops both (a job object kills them even if this window is closed hard).
  Update: bin\nuc-console-update.cmd (keeps .\data).

.PARAMETER Port
  The port of the web view (default: a free one).

.PARAMETER NoOpen
  Do not open the browser; the address is printed.

.PARAMETER Problems
  List what needs attention now, why it matters and how to fix it (run it while another window runs run.cmd), then exit.
  Followed by --json it prints JSON.

.PARAMETER Accept
  Accept the ports exposed now as the baseline of the port alarms (run it while another window runs run.cmd), then exit.
  Followed by --problem ID --reason "why" it accepts a known ATTENTION item instead (--forget ID undoes it).

.PARAMETER WhichPython
  Print the Python this folder runs with and exit (used by bin\nuc-console-update.ps1).
#>
param([ValidateRange(0, 65535)][int]$Port = 0, [switch]$NoOpen, [switch]$Accept, [switch]$Problems, [switch]$WhichPython,
      [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @())

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

# Windows PowerShell started from a PowerShell 7 window through cmd.exe (run.cmd, install-windows.cmd) or another program inherits
# PowerShell 7's module folders in PSModulePath, ahead of its own: its Microsoft.PowerShell.Utility and .Security would be loaded
# from there, and they do not work here (Get-FileHash, Get-Acl, Get-AuthenticodeSignature: "not recognized"). PowerShell 7 leaves
# them out only for a powershell.exe it starts itself; here they go before any module is loaded, for this script and all it starts:
# a folder next to a pwsh.exe (PowerShell 7's own modules) and the ...\PowerShell\Modules ones (its modules for a user, for all).
if ($PSVersionTable.PSEdition -eq 'Desktop' -and $env:PSModulePath) {
    $env:PSModulePath = @($env:PSModulePath.Split(';') | Where-Object {
        $_ -and $_.TrimEnd('\') -notmatch '\\PowerShell\\Modules$' -and -not [IO.File]::Exists($_.TrimEnd('\') + '\..\pwsh.exe')
    }) -join ';'
}

$Here = $PSScriptRoot
$Src = Join-Path $Here 'src'
$PyDir = Join-Path $Here 'python'
# $env:NUC_CONSOLE_DATA (an absolute path) moves the data folder: the desktop app runs this script from inside the app, which
# ordinary users cannot write to, with its data in theirs
$Data = if ($env:NUC_CONSOLE_DATA) { $env:NUC_CONSOLE_DATA } else { Join-Path $Here 'data' }
if ($Data -notmatch '^([A-Za-z]:\\|\\\\)') { throw "NUC_CONSOLE_DATA must be an absolute path: $Data" }
$Logs = Join-Path $Data 'logs'

function Say($text) { Write-Host "nuc-console: $text" }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-Sha256($path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower() }

# ---- Python ------------------------------------------------------------------------------------------------------------------

# @{ file; sha256 } of the embeddable Python for this processor, from $PyVersion / $PyBuilds of install-windows.ps1 (the one place
# the pin lives: the installer, tools\build_release.py and this script read it there); $null when it cannot be read
function Get-PythonPin($arch) {
    $installer = Join-Path $Here 'install-windows.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { return $null }
    $text = Get-Content -LiteralPath $installer -Raw
    $v = [regex]::Match($text, "(?m)^\`$PyVersion\s*=\s*'([0-9.]+)'")
    $b = [regex]::Match($text, "'$arch'\s*=\s*@\{\s*file\s*=\s*`"([^`"]+)`"\s*;\s*sha256\s*=\s*'([0-9a-f]{64})'")
    if (-not $v.Success -or -not $b.Success) { return $null }
    return @{ file = $b.Groups[1].Value.Replace('$PyVersion', $v.Groups[1].Value); sha256 = $b.Groups[2].Value }
}

# the bundled Python: unpacked once (a stamp file records which zip, and its SHA-256), reused afterwards, never unpacked twice
function Get-BundledPython {
    $exe = Join-Path $PyDir 'python.exe'
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $pin = Get-PythonPin $arch
    if (-not $pin) { return $null }
    $stamp = Join-Path $PyDir 'nuc-console-python.txt'
    $want = "$($pin.file) $($pin.sha256)"
    if ((Test-Path -LiteralPath $exe -PathType Leaf) -and (Test-Path -LiteralPath $stamp -PathType Leaf) -and
        ((Get-Content -LiteralPath $stamp -Raw).Trim() -eq $want)) { return $exe }
    $zip = Join-Path $PyDir $pin.file
    if (-not (Test-Path -LiteralPath $zip -PathType Leaf)) { return $null }  # a source checkout: no bundled Python
    $hash = Get-Sha256 $zip
    if ($hash -ne $pin.sha256) {
        Say "warning: $zip has SHA-256 $hash, install-windows.ps1 pins $($pin.sha256): not used"
        return $null
    }
    Say "first run: unpacking $($pin.file) into $PyDir (once)"
    # what an older unpacking left goes; the Python zips (the release's and the one of an older pin) stay
    Get-ChildItem -LiteralPath $PyDir -Force | Where-Object { $_.Name -notlike 'python-*-embed-*.zip' } | Remove-Item -Recurse -Force
    Expand-Archive -LiteralPath $zip -DestinationPath $PyDir -Force
    $sig = Get-AuthenticodeSignature -LiteralPath $exe
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Get-ChildItem -LiteralPath $PyDir -Force | Where-Object { $_.Name -notlike 'python-*-embed-*.zip' } | Remove-Item -Recurse -Force
        Say "warning: python.exe is not signed by the Python Software Foundation ($($sig.Status)): not used"
        return $null
    }
    # the ._pth file makes this Python ignore PYTHONPATH & co. and see only its own library and our code
    $pth = Get-ChildItem -LiteralPath $PyDir -Filter 'python3*._pth' | Select-Object -First 1
    $stdlib = (Get-ChildItem -LiteralPath $PyDir -Filter 'python3*.zip' | Select-Object -First 1).Name
    Set-Content -LiteralPath $pth.FullName -Value "$stdlib`r`n.`r`n..\src" -Encoding ASCII
    Set-Content -LiteralPath $stamp -Value $want -Encoding ASCII
    return $exe
}

function Test-PythonOk($exe) {  # a working Python 3.8+ (the Microsoft Store stub of python.exe answers with an error)
    try {
        $out = & $exe -c 'import sys; print(int(sys.version_info >= (3, 8)))' 2>$null
        return (($LASTEXITCODE -eq 0) -and ("$out".Trim() -eq '1'))
    } catch { return $false }
}

function Find-SystemPython {
    $launcher = Get-Command 'py.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        $exe = $null
        try { $exe = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -First 1 } catch { }
        if ($exe -and (Test-PythonOk "$exe")) { return "$exe" }
    }
    foreach ($name in 'python.exe', 'python3.exe') {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd -and (Test-PythonOk $cmd.Source)) { return $cmd.Source }
    }
    return $null
}

if (-not (Test-Path -LiteralPath (Join-Path $Src 'collector.py') -PathType Leaf)) {
    throw 'src\collector.py not found next to run.ps1: run it from the extracted folder'
}

# ---- as administrator: only a folder that ordinary users cannot write to ---------------------------------------------------------
# (the same rule as run.sh as root) With administrator rights this folder's code is run and its data written: a folder that
# Everyone, Users or Authenticated Users can write to (C:\ itself, a shared folder), or one with a link in it, could be turned
# against the administrator. Nothing is run or written there; the message says what to do.

# $true when Everyone, Users or Authenticated Users are allowed to create, change or delete things in $path, or to change who can
function Test-WritableByUsers([string]$path) {
    # WriteData 2, AppendData 4, DeleteSubdirectoriesAndFiles 64, Delete 65536, ChangePermissions 262144, TakeOwnership 524288,
    # and the generic GenericAll 268435456 and GenericWrite 1073741824
    $mask = 2 -bor 4 -bor 64 -bor 65536 -bor 262144 -bor 524288 -bor 268435456 -bor 1073741824
    $users = @('S-1-1-0', 'S-1-5-32-545', 'S-1-5-11')  # Everyone, BUILTIN\Users, Authenticated Users
    $acl = $null
    try { $acl = Get-Acl -LiteralPath $path } catch { return $true }  # who can write is not known: not trusted
    foreach ($ace in $acl.Access) {
        if ("$($ace.AccessControlType)" -ne 'Allow') { continue }
        $sid = ''
        try { $sid = $ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { continue }
        if (($users -contains $sid) -and (([int]$ace.FileSystemRights -band $mask) -ne 0)) { return $true }
    }
    return $false
}

function Test-ReparsePoint([string]$path) {  # a link or a junction
    return ((([int](Get-Item -LiteralPath $path -Force).Attributes) -band 1024) -ne 0)
}

if (Test-Admin) {
    foreach ($d in @($Here, $Src, $PyDir, $Data, (Join-Path $Data 'run'), (Join-Path $Data 'lib'), $Logs)) {
        if (-not (Test-Path -LiteralPath $d)) { continue }
        if (Test-ReparsePoint $d) {
            throw "$d is a link: with administrator rights nothing is run or written there. Remove the link, or run without administrator rights"
        }
        if (Test-WritableByUsers $d) {
            throw "$d can be changed by ordinary users (Everyone, Users or Authenticated Users have write access): with administrator rights nothing is run or written there. Move this folder where only administrators can write (for example under C:\Program Files), or run it without administrator rights"
        }
    }
}

$python = Get-BundledPython
if (-not $python) { $python = Find-SystemPython }
if (-not $python) { throw 'Python 3.8 or newer not found (the release ZIP carries one in python\; else install it from python.org)' }
if ($WhichPython) { Write-Output $python; exit 0 }

# ---- the data folder ---------------------------------------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $Data, (Join-Path $Data 'run'), (Join-Path $Data 'lib'), $Logs | Out-Null
$cfg = Join-Path $Data 'config.ini'
if (-not (Test-Path -LiteralPath $cfg)) { Copy-Item -LiteralPath (Join-Path $Here 'config\config.ini') -Destination $cfg }  # never overwrite your edits
Copy-Item -LiteralPath (Join-Path $Here 'config\config.ini') -Destination (Join-Path $Data 'config.ini.dist') -Force  # always refreshed: diff it with config.ini
$env:NUC_CONSOLE_HOME = $Data  # config, state and baseline in this one folder (the children inherit it)
foreach ($n in 'NUC_CONSOLE_CONFIG', 'NUC_CONSOLE_STATE', 'NUC_CONSOLE_NET', 'NUC_CONSOLE_BOOT', 'NUC_CONSOLE_BASELINE', 'NUC_CONSOLE_ACCEPTED') {
    Remove-Item -LiteralPath "Env:$n" -ErrorAction SilentlyContinue
}

if ($Accept) {
    & $python -B (Join-Path $Src 'render.py') --accept @Rest
    exit $LASTEXITCODE
}
if ($Problems) {
    & $python -B (Join-Path $Src 'render.py') --problems @Rest
    exit $LASTEXITCODE
}

# ---- one instance per folder (two collectors would write the same files) ---------------------------------------------------------
$pidFile = Join-Path $Data 'portable.pid'
if (Test-Path -LiteralPath $pidFile) {
    $old = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($old -match '^\d+$' -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)) {
        throw "already running (process $old): close it first (if that is wrong, delete $pidFile)"
    }
}
Set-Content -LiteralPath $pidFile -Value $PID -Encoding ASCII

# ---- a job object: whatever happens to this window, the children die with it ---------------------------------------------------
$JobCode = @'
using System;
using System.Runtime.InteropServices;
public static class NucJob {
    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimits {
        public long PerProcessUserTimeLimit; public long PerJobUserTimeLimit; public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize; public UIntPtr MaximumWorkingSetSize; public uint ActiveProcessLimit;
        public UIntPtr Affinity; public uint PriorityClass; public uint SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct IoCounters {
        public ulong ReadOperationCount; public ulong WriteOperationCount; public ulong OtherOperationCount;
        public ulong ReadTransferCount; public ulong WriteTransferCount; public ulong OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct ExtendedLimits {
        public BasicLimits Basic; public IoCounters Io;
        public UIntPtr ProcessMemoryLimit; public UIntPtr JobMemoryLimit; public UIntPtr PeakProcessMemoryUsed; public UIntPtr PeakJobMemoryUsed;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    static IntPtr job = IntPtr.Zero;
    public static void Create() {
        job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new InvalidOperationException("CreateJobObject failed");
        ExtendedLimits limits = new ExtendedLimits();
        limits.Basic.LimitFlags = 0x2000;  // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: the OS closes the handle when this process ends
        int size = Marshal.SizeOf(typeof(ExtendedLimits));
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try {
            Marshal.StructureToPtr(limits, buffer, false);
            if (!SetInformationJobObject(job, 9, buffer, (uint)size)) throw new InvalidOperationException("SetInformationJobObject failed");
        } finally { Marshal.FreeHGlobal(buffer); }
    }
    public static void Add(IntPtr process) {
        if (job == IntPtr.Zero || !AssignProcessToJobObject(job, process)) throw new InvalidOperationException("AssignProcessToJobObject failed");
    }
}
'@
$jobOk = $false
try { Add-Type -TypeDefinition $JobCode -ErrorAction Stop; [NucJob]::Create(); $jobOk = $true } catch { }

$children = New-Object System.Collections.ArrayList
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'  # no console window of its own (the output goes to --log)
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { $pythonw = $python }

function Start-Child([string]$script, [string[]]$more) {
    $argv = @('-B', $script) + $more
    $line = ($argv | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $p = Start-Process -FilePath $pythonw -ArgumentList $line -WorkingDirectory $Data -WindowStyle Hidden -PassThru
    [void]$p.Handle  # keeps the handle: HasExited and ExitCode stay right
    if ($jobOk) { try { [NucJob]::Add($p.Handle) } catch { } }
    [void]$children.Add($p)
    return $p
}

function Save-Baseline {  # $true when the baseline exists now; the collector needs a first complete snapshot for it
    $old, $out, $rc = $ErrorActionPreference, '', 1
    $ErrorActionPreference = 'Continue'  # a native command's stderr must not stop the script
    try { $out = & $python -B (Join-Path $Src 'render.py') --accept --if-missing 2>&1; $rc = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
    Set-Content -LiteralPath (Join-Path $Logs 'baseline.log') -Value ($out | Out-String) -Encoding UTF8
    return ($rc -eq 0)
}

# ---- start, and stop everything we started --------------------------------------------------------------------------------------
try { $Host.UI.RawUI.WindowTitle = 'nuc-console' } catch { }
try {
    if (-not (Test-Admin)) {
        Say 'running without administrator rights: the sections that need them (firewall, other users'' processes, services) show less. Right-click run.cmd > Run as administrator shows everything.'
    }
    $collector = Start-Child (Join-Path $Src 'collector.py') @('--log', (Join-Path $Logs 'collector.log'))
    # the Telegram notifier (docs/TELEGRAM.md): beside the web view it takes the Telegram page's requests (pair, on, off, test) and keeps the
    # token in data\notify (the children inherit NUC_CONSOLE_WEB)
    $env:NUC_CONSOLE_WEB = '1'
    [void](Start-Child (Join-Path $Src 'notify.py') @('--log', (Join-Path $Logs 'notify.log')))
    $webLog = Join-Path $Logs 'web.log'
    if (Test-Path -LiteralPath $webLog) { Remove-Item -LiteralPath $webLog -Force }
    $web = Start-Child (Join-Path $Src 'web.py') @('--local', '--port', "$Port", '--log', $webLog)

    $url = ''
    for ($i = 0; $i -lt 150 -and -not $url; $i++) {
        $text = if (Test-Path -LiteralPath $webLog) { Get-Content -LiteralPath $webLog -Raw -ErrorAction SilentlyContinue } else { '' }
        if ($text) {
            $m = [regex]::Match($text, '(?m)^nuc-console web view on (http://\S+)')
            if ($m.Success) { $url = $m.Groups[1].Value }
        }
        if (-not $url) {
            if ($web.HasExited) { break }
            Start-Sleep -Milliseconds 200
        }
    }
    if (-not $url) {
        $tail = if (Test-Path -LiteralPath $webLog) { Get-Content -LiteralPath $webLog -Tail 5 | Out-String } else { '' }
        throw "the web view did not start: $tail"
    }
    $url = "$url/?fit=1"
    Say "dashboard on $url (close this window or press Ctrl+C to stop)"
    if (-not $NoOpen) {
        try { Start-Process $url } catch { Say "could not open the browser: open $url yourself" }
    }

    # the baseline of the port alarms: once the collector has written its first complete snapshot (without administrator
    # rights it is incomplete: tried for a few minutes, then run.cmd -Accept as administrator)
    $baseline = Test-Path -LiteralPath (Join-Path $Data 'lib\baseline.json')
    $told = $false
    for ($tick = 0; -not $web.HasExited; $tick++) {
        if (-not $baseline -and $tick % 4 -eq 3 -and $tick -lt 150) { $baseline = Save-Baseline }
        if (-not $told -and $collector.HasExited) {
            $told = $true
            Say "the collector stopped (see $Logs\collector.log): the dashboard will show stale data"
        }
        Start-Sleep -Seconds 1
    }
    Say "the web view stopped (see $webLog)"
} finally {
    foreach ($p in $children) {
        try { if (-not $p.HasExited) { $p.Kill() } } catch { }
    }
    foreach ($p in $children) {
        try { [void]$p.WaitForExit(3000) } catch { }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
