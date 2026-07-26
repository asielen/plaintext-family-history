@echo off
rem fha - the archive command line. Run it from a terminal as `fha <command>`
rem (e.g. `fha lint`, `fha index`, `fha find "Margaret Cole"`). Your working
rem directory is preserved, so relative paths and archive auto-detection work
rem from anywhere inside the archive. Finds the tools whether they are vendored
rem flat (tools\) or consolidated under .fha\. The POSIX twin is the `fha` file.
if exist "%~dp0.fha\tools\fha.py" (
  py -3 "%~dp0.fha\tools\fha.py" %*
  exit /b %errorlevel%
)
if exist "%~dp0tools\fha.py" (
  py -3 "%~dp0tools\fha.py" %*
  exit /b %errorlevel%
)
rem Neither entrypoint is here. Handing the missing path to Python anyway would
rem print a raw interpreter error with no way forward, so say what is wrong and
rem give a command that actually runs. `fha update-tools` alone would re-enter
rem this launcher and land right back here; run from the workshop it also needs
rem both --repo and --root.
echo fha: cannot find the tools next to %~dp0 1>&2
echo      (looked for .fha\tools\fha.py, then tools\fha.py). 1>&2
echo. 1>&2
echo      To restore them, run this from your workshop copy of the project - 1>&2
echo      the folder holding manifest.json - replacing PATH-TO-WORKSHOP: 1>&2
echo. 1>&2
echo        cd /d PATH-TO-WORKSHOP 1>&2
echo        fha update-tools --repo PATH-TO-WORKSHOP --root "%~dp0" 1>&2
echo. 1>&2
echo      If you have no workshop copy yet, download the project first; see 1>&2
echo      docs\UPDATING.md in this folder. 1>&2
exit /b 3
