@echo off
rem The peers of [hosts] in config.ini (docs\CONFIGURATION.md): what the fleet client sees of them. It only reads from the peers (one GET each).
rem   nuc-console-fleet list           the configured peers and the last state the poller cached
rem   nuc-console-fleet test NAME      one poll of that peer, now, in this window: the state and the reason in words (never the token)
rem   nuc-console-fleet once           poll every peer and write the cache
rem not installed here (an extracted folder)? python src\peers.py list does the same there
if not exist "%~dp0..\app\peers.py" (
    echo nuc-console is not installed here. In an extracted folder use: python src\peers.py list ^| test NAME ^| once
    exit /b 1
)
setlocal
set NUC_CONSOLE_HOME=
set NUC_CONSOLE_CONFIG=
set NUC_CONSOLE_PEERS_CACHE=
set NUC_CONSOLE_SETTINGS=
set NUC_CONSOLE_NOTIFY_DIR=
set NUC_CONSOLE_HOST_ID=
"%~dp0..\python\python.exe" -E -B "%~dp0..\app\peers.py" %*
exit /b %errorlevel%
