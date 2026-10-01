@echo off
rem Updates nuc-console to the latest GitHub release. You run it: it is never automatic, never in the background.
rem   nuc-console-update -Check       only say whether a newer release exists
rem   nuc-console-update             ask, then update          nuc-console-update -Yes     do not ask
rem   nuc-console-update -Installed  the installed nuc-console, even when run from an extracted folder
rem Run from the bin\ of an extracted folder that has run.cmd (the portable mode) it updates that folder (data\ stays) and
rem needs no rights. Otherwise it updates the installed one: that needs administrator rights, asked for here like
rem install-windows.cmd does (not for -Check). The logic is in nuc-console-update.ps1, run with the execution policy
rem bypassed for this process only.
setlocal EnableExtensions
set "ADMIN=1"
set "CHECK="
set "INST="
set "ELEV="
if not "%~1"=="" for %%A in (%*) do (
    if /i "%%~A"=="-Check" set "CHECK=1"
    if /i "%%~A"=="-Installed" set "INST=1"
    if /i "%%~A"=="-Elevated" set "ELEV=1"
)
if not defined INST if exist "%~dp0..\run.cmd" set "ADMIN="
if defined CHECK set "ADMIN="
if defined ADMIN (
    net session >nul 2>&1
    if errorlevel 1 (
        if "%~1"=="" (
            powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '-Elevated' -Verb RunAs"
        ) else (
            powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%* -Elevated' -Verb RunAs"
        )
        exit /b
    )
)
if not exist "%~dp0nuc-console-update.ps1" (
    echo nuc-console-update.ps1 is missing next to this file. Run bin\nuc-console-update.cmd -Installed from the folder of the
    echo newest release instead ^(download it from https://github.com/give-jd/nuc-console-oss/releases^).
    if defined ELEV pause
    exit /b 1
)
rem An update replaces this very file while cmd runs it, and cmd reads a batch file line by line from where it stopped, so
rem nothing of this file may be read after PowerShell starts: the elevated copy is one block, which cmd reads whole before
rem it runs it, and the plain one ends with exit /b chained to the PowerShell line. The exit code stays PowerShell's.
if defined ELEV (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nuc-console-update.ps1" %*
    echo.
    pause
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nuc-console-update.ps1" %* & exit /b
