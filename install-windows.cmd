@echo off
rem nuc-console installer for Windows: double-click it (it asks for administrator rights), or run it from an admin prompt.
rem Options go to install-windows.ps1:  install-windows.cmd -Uninstall   |   install-windows.cmd -NoDisplay
net session >nul 2>&1
if errorlevel 1 (
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" %*
set rc=%errorlevel%
echo.
pause
exit /b %rc%
