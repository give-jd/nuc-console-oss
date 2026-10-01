#Requires -Version 5.1
<#
.SYNOPSIS
  Updates nuc-console (Windows) to the latest GitHub release. You run it: it is never automatic, never in the background.

.DESCRIPTION
  Use nuc-console-update.cmd (it asks for administrator rights when they are needed):
    nuc-console-update.cmd -Check       only say whether a newer release exists
    nuc-console-update.cmd              ask, then update
    nuc-console-update.cmd -Yes         do not ask
    nuc-console-update.cmd -Installed   the installed nuc-console, even when run from an extracted folder

  Run from the bin\ of an extracted folder that has run.cmd (the portable mode) it updates that folder: src\, bin\, docs\ ...
  are replaced, .\data (config, baseline) and .\cache stay; no rights needed. Otherwise it updates the installed one
  (%ProgramFiles%\nuc-console): administrator rights; it runs the new release's install-windows.ps1, which keeps
  %ProgramData%\nuc-console\config.ini and the baseline.

  What it does: asks api.github.com (HTTPS, Invoke-RestMethod) for the latest release; if it is newer, downloads the archive for
  this processor (windows-x64 or windows-arm64) and SHA256SUMS into the cache (%ProgramData%\nuc-console\cache, portable:
  .\cache; a file whose SHA-256 is already right is not downloaded again), checks the SHA-256, runs `gh attestation verify`
  when gh is installed (a failure stops it; without gh it says the provenance was not checked), unpacks it and installs it.
  The comparing, downloading and checking is src\update.py; this script only asks GitHub and runs the installer.

.PARAMETER Check
  Only report.

.PARAMETER Yes
  Do not ask for confirmation.

.PARAMETER Installed
  Update the installed nuc-console, even when this script runs from an extracted folder.

.PARAMETER Elevated
  Set by nuc-console-update.cmd when it re-started itself with administrator rights: has no other effect.
#>
param([switch]$Check, [switch]$Yes, [switch]$Installed, [switch]$Elevated)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$Api = 'https://api.github.com/repos/give-jd/nuc-console-oss/releases/latest'
$Here = $PSScriptRoot
$Root = Split-Path -Parent $Here

function Say($text) { Write-Host "nuc-console-update: $text" }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not $Installed -and (Test-Path -LiteralPath (Join-Path $Root 'run.cmd')) -and (Test-Path -LiteralPath (Join-Path $Root 'src\update.py'))) {
    $mode = 'portable'
    $app = Join-Path $Root 'src'
    $cache = Join-Path $Root 'cache'
    $python = @(& (Join-Path $Root 'run.ps1') -WhichPython)[-1]  # the Python of the folder: the bundled one (unpacked once), else this machine's
    $pidFile = Join-Path $Root 'data\portable.pid'
    if (Test-Path -LiteralPath $pidFile) {
        $running = (Get-Content -LiteralPath $pidFile -Raw).Trim()
        if ($running -match '^\d+$' -and (Get-Process -Id ([int]$running) -ErrorAction SilentlyContinue)) {
            throw "nuc-console is running from this folder (process $running): close it first"
        }
    }
} else {
    $mode = 'installed'
    $dest = Join-Path $env:ProgramFiles 'nuc-console'
    $app = Join-Path $dest 'app'
    $python = Join-Path $dest 'python\python.exe'
    $cache = Join-Path (Join-Path $env:ProgramData 'nuc-console') 'cache'
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath (Join-Path $app 'update.py'))) {
        throw "nuc-console is not installed in ${dest}: install it first, or update a portable folder from its own bin\"
    }
    if (-not $Check -and -not (Test-Admin)) { throw 'administrator rights required: run nuc-console-update.cmd (it asks for them)' }
}
$updatePy = Join-Path $app 'update.py'

$tmp = Join-Path ([IO.Path]::GetTempPath()) ('nuc-console-update-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $json = Join-Path $tmp 'release.json'
    $stage = Join-Path $tmp 'stage'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = 'SilentlyContinue'  # the progress bar makes web requests 10x slower on PowerShell 5.1
    Say 'asking GitHub for the latest release'
    try {
        Invoke-RestMethod -Uri $Api -Headers @{ Accept = 'application/vnd.github+json' } -UserAgent 'nuc-console-update' -TimeoutSec 60 -OutFile $json
    } catch {
        throw "could not read the latest release from $Api ($($_.Exception.Message))"
    }

    $argv = @('-I', '-B', $updatePy, '--release-json', $json, '--cache', $cache, '--mode', $mode, '--app', $app, '--stage-file', $stage)
    if ($mode -eq 'portable') { $argv += @('--root', $Root) }
    if ($Check) { $argv += '--check' }
    if ($Yes) { $argv += '--yes' }
    & $python @argv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    # an installed one: the new release's installer runs here, after the Python that checked it has ended (it replaces that Python)
    if (Test-Path -LiteralPath $stage) {
        $top = (Get-Content -LiteralPath $stage -Raw).Trim()
        $stageDir = Split-Path -Parent $top
        $cacheFull = [IO.Path]::GetFullPath($cache).TrimEnd('\')
        if (([IO.Path]::GetFullPath((Split-Path -Parent $stageDir)).TrimEnd('\') -ne $cacheFull) -or ((Split-Path -Leaf $stageDir) -notlike 'stage-*')) {
            throw "unexpected folder to install from: $top"
        }
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        try {
            & $powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $top 'install-windows.ps1')
            $rc = $LASTEXITCODE
        } finally {
            Remove-Item -LiteralPath $stageDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($rc -ne 0) { throw "the installer failed (exit code $rc): the previous version may be partly replaced, run this again" }
    }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
