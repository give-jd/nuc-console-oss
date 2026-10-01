#Requires -Version 5.1
<#
.SYNOPSIS
  Install nuc-console on Windows 10/11 (and Server 2019+). Idempotent: run it again to upgrade.

.DESCRIPTION
  One command, from an administrator prompt (or double-click install-windows.cmd, which asks for the rights):
    powershell -ExecutionPolicy Bypass -File install-windows.ps1

  What it does:
    1. puts a private Python (the official python.org "embeddable" build, SHA-256 pinned and Authenticode-checked) and
       the code in %ProgramFiles%\nuc-console; nothing else on the system uses or changes it;
    2. creates %ProgramData%\nuc-console (config.ini only if missing, run\, lib\, logs\), writable only by SYSTEM and
       Administrators, readable by users;
    3. registers scheduled tasks in the folder \nuc-console\: the collector (SYSTEM, at startup, restarted if it stops)
       and the read-only web view (LOCAL SERVICE) on 127.0.0.1 only, which shows the dashboard to this machine's browser;
       at every logon the dashboard opens: in a normal browser window ([display] mode = browser, the default) or full screen
       (mode = fullscreen: Alt+F4 closes it, F11 leaves full screen); and the optional Telegram notifier (LOCAL SERVICE,
       outbound HTTPS to api.telegram.org only, idle until you switch it on: docs\TELEGRAM.md);
    4. adds a "nuc-console" shortcut to the Start menu (the dashboard in your normal browser) and
       %ProgramFiles%\nuc-console\bin to the system PATH (nuc-console-problems, nuc-console-accept);
    5. waits for the first collector snapshot, stores the port baseline (only if missing) and opens the dashboard.

.PARAMETER Uninstall
  Removes the tasks, the program folder and the PATH entry. %ProgramData%\nuc-console (config, baseline) is left in place.
  The Telegram notifier's folder (%ProgramData%\nuc-console\notify, with the bot token) is deleted.

.PARAMETER Display
  browser (default): at every logon the dashboard opens in a normal window of your browser. fullscreen (or kiosk): it opens
  full screen. none: it never opens by itself (a machine without a monitor). Written to [display] mode in config.ini; without
  it, config.ini decides (a re-install keeps the choice; the default is browser).

.PARAMETER NoDisplay
  Same as -Display none.

.PARAMETER PythonZip
  Path of an already downloaded python-3.14.8-embed-<arch>.zip (offline install); it is checked against the same hash.
#>
param([switch]$Uninstall, [ValidateSet('browser', 'fullscreen', 'kiosk', 'none')][string]$Display, [switch]$NoDisplay,
      [string]$PythonZip = '')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$PyVersion = '3.14.8'
$PyBuilds = @{  # SHA-256 of the python.org files (verified against their Sigstore signatures when pinned)
    'AMD64' = @{ file = "python-$PyVersion-embed-amd64.zip"; sha256 = 'a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310' }
    'ARM64' = @{ file = "python-$PyVersion-embed-arm64.zip"; sha256 = '155be84ccb57c6331cf0e39001c78a1dfac3be62f403f3ff5f2e29b80dda7ebe' }
}
$Dest = Join-Path $env:ProgramFiles 'nuc-console'
$App, $Py, $Bin = (Join-Path $Dest 'app'), (Join-Path $Dest 'python'), (Join-Path $Dest 'bin')
$Data = Join-Path $env:ProgramData 'nuc-console'
$TaskPath = '\nuc-console\'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Shortcut = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\nuc-console.url'
if ($NoDisplay) { $Display = 'none' }

function Say($text) { Write-Host "nuc-console: $text" }

function Account($sid) {  # well-known accounts by SID: their names are translated on non-English Windows
    (New-Object Security.Principal.SecurityIdentifier $sid).Translate([Security.Principal.NTAccount]).Value
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Remove-Tasks {
    foreach ($t in @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)) {
        Stop-ScheduledTask -TaskPath $TaskPath -TaskName $t.TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $t.TaskName -Confirm:$false
    }
    # tasks end their action, not always the Python process behind it: stop what still runs from our folder
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Py, [StringComparison]::OrdinalIgnoreCase) } |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

function Set-MachinePath([bool]$add) {
    $parts = @([Environment]::GetEnvironmentVariable('Path', 'Machine') -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ne $Bin })
    if ($add) { $parts += $Bin }
    [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'Machine')
}

if (-not (Test-Admin)) { throw 'administrator rights required: right-click install-windows.cmd > Run as administrator' }

if ($Uninstall) {
    Remove-Tasks
    if (Test-Path "$Data\notify") { Remove-Item -Recurse -Force "$Data\notify" }  # the Telegram bot token is in there: first
    try { $svc = New-Object -ComObject Schedule.Service; $svc.Connect(); $svc.GetFolder('\').DeleteFolder('nuc-console', 0) } catch { }
    Set-MachinePath $false
    if (Test-Path $Shortcut) { Remove-Item -Force $Shortcut }
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    Say "removed ($Data with config.ini and the baseline is left in place)"
    exit 0
}

# ---- 1. private Python ---------------------------------------------------------------------------------------------------
$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if (-not $PyBuilds.ContainsKey($arch)) { throw "unsupported processor architecture: $arch (64-bit x86 or ARM only)" }
$build = $PyBuilds[$arch]
$stamp = Join-Path $Py 'nuc-console-python.txt'
if (-not ((Test-Path $stamp) -and ((Get-Content $stamp -Raw).Trim() -eq "$($build.file) $($build.sha256)"))) {
    $zip = $PythonZip
    if (-not $zip) {
        $zip = Join-Path ([IO.Path]::GetTempPath()) $build.file
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Say "downloading Python $PyVersion ($arch) from python.org"
        $ProgressPreference = 'SilentlyContinue'  # the progress bar makes Invoke-WebRequest 10x slower on PowerShell 5.1
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$PyVersion/$($build.file)" -OutFile $zip
    }
    $hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    if ($hash -ne $build.sha256) { throw "$zip has SHA-256 $hash, expected $($build.sha256): not installed" }
    if (Test-Path $Py) { Remove-Tasks; Remove-Item -Recurse -Force $Py }
    Expand-Archive -Path $zip -DestinationPath $Py
    $sig = Get-AuthenticodeSignature (Join-Path $Py 'python.exe')
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Remove-Item -Recurse -Force $Py
        throw "python.exe is not signed by the Python Software Foundation ($($sig.Status)): not installed"
    }
    Set-Content -Path $stamp -Value "$($build.file) $($build.sha256)" -Encoding ASCII
    if (-not $PythonZip) { Remove-Item -Force $zip }
}
# the ._pth file makes this Python ignore PYTHONPATH & co. and see only its own library and our code
$pth = Get-ChildItem -Path $Py -Filter 'python3*._pth' | Select-Object -First 1
$zipName = (Get-ChildItem -Path $Py -Filter 'python3*.zip' | Select-Object -First 1).Name
Set-Content -Path $pth.FullName -Value "$zipName`r`n.`r`n..\app" -Encoding ASCII
$python, $pythonw = (Join-Path $Py 'python.exe'), (Join-Path $Py 'pythonw.exe')

# ---- 2. code, commands, data folder ------------------------------------------------------------------------------------
Remove-Tasks  # an upgrade must not keep running the old code
New-Item -ItemType Directory -Force -Path $App, $Bin | Out-Null
Get-ChildItem -Path $App -Filter '*.py' | Remove-Item -Force
Copy-Item -Path (Join-Path $Here 'src\*.py') -Destination $App -Force
Copy-Item -Path (Join-Path $Here 'bin\*.cmd') -Destination $Bin -Force
foreach ($d in @($Data, "$Data\run", "$Data\lib", "$Data\logs")) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
# writable only by SYSTEM and Administrators (a user must not be able to fake the state or edit what SYSTEM reads);
# readable by everyone who runs the dashboard; LOCAL SERVICE (web view) may write its log
& icacls.exe $Data /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' '*S-1-5-19:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed on $Data" }
& icacls.exe "$Data\logs" /grant '*S-1-5-19:(OI)(CI)M' | Out-Null
# the Telegram notifier runs as NETWORK SERVICE, not as the web view's LOCAL SERVICE (the web view may be reachable on the LAN):
# notify\ holds status.json only (no secret: readable by the dashboard and its users); notify\private\ holds the bot token
# and the paired chat: SYSTEM, Administrators and NETWORK SERVICE only
& icacls.exe $Data /grant '*S-1-5-20:(OI)(CI)RX' | Out-Null  # it reads config.ini and the state, like the web view
& icacls.exe "$Data\logs" /grant '*S-1-5-20:(OI)(CI)M' | Out-Null
New-Item -ItemType Directory -Force -Path "$Data\notify", "$Data\notify\private" | Out-Null
& icacls.exe "$Data\notify" /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-20:(OI)(CI)M' '*S-1-5-19:(OI)(CI)RX' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed on $Data\notify" }
& icacls.exe "$Data\notify\private" /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-20:(OI)(CI)M' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed on $Data\notify\private" }
$cfg = Join-Path $Data 'config.ini'
if (-not (Test-Path $cfg)) { Copy-Item (Join-Path $Here 'config\config.ini') $cfg }  # never overwrite the admin's edits
Copy-Item (Join-Path $Here 'config\config.ini') (Join-Path $Data 'config.ini.dist') -Force  # diff it to see new options
if ($Display) { & $python -B (Join-Path $App 'nuc_config.py') --set $cfg display mode $Display }  # only that line changes
$mode = (& $python -B (Join-Path $App 'nuc_config.py') --get display mode).Trim()
$port = (& $python -B (Join-Path $App 'nuc_config.py') --get web port).Trim()
$url = "http://127.0.0.1:$port/?fit=1"
Set-MachinePath $true

# ---- 3. scheduled tasks ------------------------------------------------------------------------------------------------
function Register-Service($name, $script, $sid, $log, $description, $extra = '') {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-B `"$App\$script`" --log `"$Data\logs\$log`" $extra".TrimEnd() -WorkingDirectory $App
    # at startup, and every 5 minutes: if it stopped, it starts again (IgnoreNew: never two at once)
    $triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)))
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId (Account $sid) -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $name -Action $action -Trigger $triggers -Settings $settings `
        -Principal $principal -Description $description -Force | Out-Null
}
Register-Service 'collector' 'collector.py' 'S-1-5-18' 'collector.log' 'nuc-console collector: ports, firewall, containers, services (SYSTEM)'
$t0 = Get-Date
Start-ScheduledTask -TaskPath $TaskPath -TaskName 'collector'

# the web view: as configured in [web] if enabled there; else, for this machine's own browser, on 127.0.0.1 only (--local)
& $python -B (Join-Path $App 'web.py') --enabled
$web = ($LASTEXITCODE -eq 0) -or ($mode -ne 'none')
if ($web) {
    Register-Service 'web' 'web.py' 'S-1-5-19' 'web.log' 'nuc-console read-only web view (LOCAL SERVICE)' '--local'
    Start-ScheduledTask -TaskPath $TaskPath -TaskName 'web'
    Set-Content -Path $Shortcut -Value "[InternetShortcut]`r`nURL=$url" -Encoding ASCII  # Start menu: opens the default browser
} elseif (Test-Path $Shortcut) { Remove-Item -Force $Shortcut }

# the Telegram notifier: always registered; it exits at once (and stays idle) unless [telegram] enabled = yes and the chat is paired.
# Outbound HTTPS to api.telegram.org only, as LOCAL SERVICE; its folder (token, chat) is the one locked above
Register-Service 'notify' 'notify.py' 'S-1-5-20' 'notify.log' 'nuc-console Telegram notifier: outbound only (NETWORK SERVICE)'
Start-ScheduledTask -TaskPath $TaskPath -TaskName 'notify'

if ($mode -ne 'none') {  # at every logon: a normal browser window (--open) or a full-screen one (--kiosk)
    $arg = @{ browser = '--open'; fullscreen = '--kiosk' }[$mode]
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-B `"$App\render.py`" $arg" -WorkingDirectory $App
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -GroupId (Account 'S-1-5-32-545') -RunLevel Limited
    Register-ScheduledTask -TaskPath $TaskPath -TaskName 'display' -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
        -Settings $settings -Principal $principal -Description "nuc-console dashboard at logon ($mode)" -Force | Out-Null
}

# ---- 4. first snapshot, baseline, dashboard ----------------------------------------------------------------------------
$net = Join-Path $Data 'run\net.json'
for ($i = 0; $i -lt 90; $i++) {
    if ((Test-Path $net) -and ((Get-Item $net).LastWriteTime -ge $t0)) { break }
    Start-Sleep -Seconds 1
}
& $python -B (Join-Path $App 'render.py') --accept --if-missing
if ($LASTEXITCODE -ne 0) { Say 'warning: baseline not created (collector not ready yet): run nuc-console-accept from an administrator prompt' }
if ($web) {  # the web view starts in a moment: wait for it before opening anything
    for ($i = 0; $i -lt 20; $i++) {
        try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/healthz").StatusCode -eq 200) { break } } catch { }
        Start-Sleep -Seconds 1
    }
}
if ($mode -ne 'none') {  # now, for the user at the screen (the task runs as that user, never as administrator)
    try { Start-ScheduledTask -TaskPath $TaskPath -TaskName 'display' } catch { Say 'the dashboard opens at the next logon' }
}

$how = @{ browser = "opens in your browser at every logon (again: Start menu > nuc-console, or $url)"; fullscreen = "opens full screen at every logon (Alt+F4 closes it, F11 leaves full screen; again: Start menu > nuc-console)"; none = 'never opens by itself (-Display none)' }[$mode]
Say "ok: collector running as SYSTEM; dashboard $how. Text size: A- / A+ at the bottom of the page"
Say "config: $cfg   logs: $Data\logs   commands: nuc-console-problems, nuc-console-accept (open a new prompt for the PATH)"
