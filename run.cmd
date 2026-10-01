@echo off
rem nuc-console, portable: double-click it in the extracted folder. Nothing is installed; everything it writes is in .\data.
rem   run.cmd              the dashboard in your browser (on 127.0.0.1 only, a free port)
rem   run.cmd -Port 8787   a port of your choice        run.cmd -NoOpen   only print the address
rem   run.cmd -Accept      accept the ports exposed now as the baseline of the port alarms
rem   run.cmd -Accept --problem ID --reason "why"      a known ATTENTION item (--forget ID undoes it)
rem Closing this window (or Ctrl+C) stops everything it started. Right-click > Run as administrator shows more.
rem PowerShell runs run.ps1 with the execution policy bypassed for this process only (no machine setting changes).
setlocal
title nuc-console
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set rc=%errorlevel%
if "%rc%"=="1" (
    echo.
    echo nuc-console did not start: see the message above.
    pause
)
exit /b %rc%
