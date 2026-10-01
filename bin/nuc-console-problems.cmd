@echo off
rem Lists every current anomaly of the dashboard (ATTENTION) with why it matters and how to fix it. Read-only, no administrator needed.
rem   nuc-console-problems            human-readable
rem   nuc-console-problems --json     machine-readable
rem Accept a known one (administrator prompt):  nuc-console-accept --problem <id> --reason "why it is fine"   (forget: --forget <id>)
"%~dp0..\python\python.exe" -B "%~dp0..\app\render.py" --problems %*
exit /b %errorlevel%
