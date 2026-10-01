@echo off
rem Local AI model for the HEALTH advisor, chosen by what this machine can run; installed once, served on 127.0.0.1 only (docs\AI.md).
rem   nuc-console-ai models                       the hardware found and every model: does it fit, how fast, installed, active, recommended
rem   nuc-console-ai setup [MODEL ...] [--yes]    download the runtime and the models you name (none: the recommended one), once, SHA-256
rem                                               verified; offers to write [ai] in config.ini. --force: a model that will not work here
rem   nuc-console-ai use MODEL                    make an installed model the one the advisor asks for ([ai] model in config.ini)
rem   nuc-console-ai serve [--port 8080] [--gpu-layers N]   run the server in the foreground, on the GPU when the model fits there
rem                                               (--install-service: as a scheduled task, LOCAL SERVICE; --remove-service)
rem   nuc-console-ai status                       what is installed and verified, whether the configured endpoint answers
rem   nuc-console-ai remove [MODEL]               delete the downloaded files of one model (none named: every model and the runtime)
rem   nuc-console-ai pins                         maintainers: the values to pin in aisetup.py (needs the network)
rem models and status only read: no administrator needed. Run setup, use and --install-service from an administrator prompt
rem (files go to %ProgramData%\nuc-console\ai).
rem The NUC_CONSOLE_* variables are cleared: they must not be able to redirect a write done with administrator rights (use --dir, --config).
setlocal
set NUC_CONSOLE_HOME=
set NUC_CONSOLE_CONFIG=
"%~dp0..\python\python.exe" -B "%~dp0..\app\aisetup.py" %*
exit /b %errorlevel%
