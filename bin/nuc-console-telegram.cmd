@echo off
rem Telegram notifications of the ATTENTION list. The machine only sends: it never reads your messages and listens on no port.
rem   nuc-console-telegram --setup     pair: your own bot token and your @username, then press Start in Telegram (administrator prompt)
rem   nuc-console-telegram --status     is it on, paired, running (--json for scripts)
rem   nuc-console-telegram --test       send a test message
rem   nuc-console-telegram --preview    print what the current problems would send (nothing is sent)
rem   nuc-console-telegram --on ^| --off ^| --forget (administrator prompt)     --help for all of it
rem The NUC_CONSOLE_* variables are cleared: they must not be able to redirect a write done with administrator rights.
setlocal
set NUC_CONSOLE_CONFIG=
set NUC_CONSOLE_NOTIFY_DIR=
set NUC_CONSOLE_BASELINE=
set NUC_CONSOLE_NET=
set NUC_CONSOLE_STATE=
set NUC_CONSOLE_BOOT=
set NUC_CONSOLE_ACCEPTED=
"%~dp0..\python\python.exe" -B "%~dp0..\app\notify.py" %*
exit /b %errorlevel%
