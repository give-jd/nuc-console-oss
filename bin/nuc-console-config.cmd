@echo off
rem Maintenance of config.ini.
rem   nuc-console-config migrate [--dry-run] [--yes]   bring an old, long config.ini to the layout of this release (config.ini.dist), keeping every
rem                                                    setting you made; the old file is kept as config.ini.bak-DATE-TIME. --dry-run: only the diff
rem Needs the rights to edit config.ini: an administrator prompt. Nothing runs by itself (nuc-console-update only mentions it).
rem The NUC_CONSOLE_* variables are cleared: they must not be able to redirect a write done with administrator rights (use --config).
setlocal
set NUC_CONSOLE_HOME=
set NUC_CONSOLE_CONFIG=
"%~dp0..\python\python.exe" -E -B "%~dp0..\app\confmigrate.py" %*
exit /b %errorlevel%
