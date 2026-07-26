@echo off
setlocal
rem fha - the archive command line. Run it from a terminal as `fha <command>`
rem (e.g. `fha lint`, `fha index`, `fha find "Margaret Cole"`). Your working
rem directory is preserved, so relative paths and archive auto-detection work
rem from anywhere inside the archive. Finds the tools whether they are vendored
rem flat (tools\) or consolidated under .fha\. The POSIX twin is the `fha` file.

rem 1. Locate the entrypoint.
set "FHA_ENTRY="
if exist "%~dp0.fha\tools\fha.py" set "FHA_ENTRY=%~dp0.fha\tools\fha.py"
if not defined FHA_ENTRY if exist "%~dp0tools\fha.py" set "FHA_ENTRY=%~dp0tools\fha.py"
if not defined FHA_ENTRY goto :no_tools

rem 2. Pick an interpreter. The tool suite is written in Python 3.10+ syntax, so
rem an older one dies with a raw SyntaxError while parsing, and a machine with no
rem `py` launcher at all gets a bare "not recognized" - neither of which tells a
rem non-technical owner what to do. Probe the real version before handing over,
rem and try `python` too: plenty of Windows installs have one without the other.
rem Written with goto labels rather than parenthesised if-blocks on purpose:
rem inside a block cmd expands %VAR% when it PARSES the block, so a variable set
rem and then read in the same block reads its old value. Labels sidestep it.
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :run_py
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :run_python
goto :no_python

:run_py
py -3 "%FHA_ENTRY%" %*
exit /b %errorlevel%

:run_python
python "%FHA_ENTRY%" %*
exit /b %errorlevel%

:no_tools
rem Handing the missing path to Python anyway would print a raw interpreter error
rem with no way forward, so say what is wrong and give a command that actually
rem runs. `fha update-tools` alone would re-enter this launcher and land right
rem back here; run from the workshop it also needs both --repo and --root.
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

:no_python
echo fha: Python 3.10 or later is required, and neither `py -3` nor `python` on 1>&2
echo      your PATH is new enough (or neither is installed). 1>&2
echo. 1>&2
echo      Download it from https://www.python.org/downloads/ and tick 1>&2
echo      "Add Python to PATH" in the installer, then re-run this command. 1>&2
exit /b 3
