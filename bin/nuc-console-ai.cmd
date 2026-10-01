@echo off
rem A small local AI model for the HEALTH advisor: installed once, served on 127.0.0.1 only (see docs/HEALTH.md).
rem   nuc-console-ai setup [--model ID] [--yes]    download the runtime and a model (pinned, SHA-256 verified), once; offers to write [ai] in config.ini
rem   nuc-console-ai serve [--port 8080]            run the server in the foreground (--install-service: as a scheduled task, LOCAL SERVICE; --remove-service)
rem   nuc-console-ai status                         what is installed, whether the configured endpoint answers
rem   nuc-console-ai remove [--model ID]            delete the downloaded files
rem Run setup and --install-service from an administrator prompt (files go to %ProgramData%\nuc-console\ai).
rem The NUC_CONSOLE_* variables are cleared: they must not be able to redirect a write done with administrator rights (use --dir, --config).
setlocal
set NUC_CONSOLE_HOME=
set NUC_CONSOLE_CONFIG=
"%~dp0..\python\python.exe" -B "%~dp0..\app\aisetup.py" %*
exit /b %errorlevel%
