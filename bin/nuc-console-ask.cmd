@echo off
rem Asks the optional local AI model about this machine (needs [ai] enabled = yes in config.ini). It suggests; it never runs anything.
rem   nuc-console-ask QUESTION...        answer a question from the history (read-only queries; the model never sees SQL); also: ask QUESTION...
rem   nuc-console-ask advise [--days N]  advice on the HEALTH findings of the last N days (1-30, default 7); also: --advise
rem   nuc-console-ask status             is the model server reachable? which models? which one is configured? also: --status
rem Read-only, no administrator needed. Exit codes: 0 ok, 1 server/model failure, 2 usage, 3 [ai] off or endpoint refused, 4 no history, 5 busy.
"%~dp0..\python\python.exe" -B "%~dp0..\app\advisor.py" %*
exit /b %errorlevel%
