@echo off
rem Accepts the current set of exposed ports as "expected": it is the alert baseline. Run it from an administrator prompt.
rem Also: --problem <id> --reason "..." accepts a known ATTENTION item; --forget <id> undoes it. List ids with nuc-console-problems.
rem The NUC_CONSOLE_* variables are cleared: they must not be able to redirect a write done with administrator rights.
setlocal
set NUC_CONSOLE_BASELINE=
set NUC_CONSOLE_NET=
set NUC_CONSOLE_STATE=
set NUC_CONSOLE_BOOT=
set NUC_CONSOLE_ACCEPTED=
set NUC_CONSOLE_CONFIG=
"%~dp0..\python\python.exe" -B "%~dp0..\app\render.py" --accept %*
exit /b %errorlevel%
