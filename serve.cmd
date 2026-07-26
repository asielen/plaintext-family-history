@echo off
setlocal
rem fha serve - double-click this to open the private workbench for this archive.
rem It runs on this machine only (127.0.0.1), no network, no login. Close the
rem window (or press Ctrl-C) to stop it; nothing is lost.
rem
rem This is the file a non-technical owner double-clicks, so it must never flash
rem a raw interpreter error and vanish. It hands over to fha.cmd, which already
rem locates the tools (flat or vendored) and checks the Python version, so that
rem guidance lives in ONE place and cannot drift between the two launchers.
cd /d "%~dp0"

rem No parenthesised block here on purpose. Inside one, %errorlevel% is
rem expanded when cmd PARSES the block, so it holds a stale value by the
rem time the call has run; and `&` does not bind to `if`, so an `if ... set
rem ... & goto` runs the goto unconditionally - sending even a clean exit 0
rem into the failure path. Sequential lines with a label read the real code.
if not exist "%~dp0fha.cmd" goto :no_shim
call "%~dp0fha.cmd" serve %*
set "FHA_RC=%errorlevel%"
if not "%FHA_RC%"=="0" goto :trouble
exit /b 0

:no_shim

rem No fha.cmd beside us - an archive from before the launchers shipped. Do the
rem same two checks inline rather than handing a missing path to an interpreter
rem that may not even be installed.
set "FHA_ENTRY="
rem `~z` is the file SIZE: a zero-byte entrypoint (interrupted copy, a sync
rem that created the file before filling it) satisfies `if exist`, and Python
rem then runs it and exits 0 having done nothing - every command a silent
rem no-op. Size-checked so an empty preferred copy loses to an intact one.
if exist "%~dp0.fha\tools\fha.py" (
  for %%S in ("%~dp0.fha\tools\fha.py") do if %%~zS GTR 0 set "FHA_ENTRY=%~dp0.fha\tools\fha.py"
)
if not defined FHA_ENTRY if exist "%~dp0tools\fha.py" (
  for %%S in ("%~dp0tools\fha.py") do if %%~zS GTR 0 set "FHA_ENTRY=%~dp0tools\fha.py"
)
if not defined FHA_ENTRY goto :no_tools

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :run_py
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :run_python
goto :no_python

:run_py
py -3 "%FHA_ENTRY%" serve %*
set "FHA_RC=%errorlevel%"
if not "%FHA_RC%"=="0" goto :trouble
exit /b 0

:run_python
python "%FHA_ENTRY%" serve %*
set "FHA_RC=%errorlevel%"
if not "%FHA_RC%"=="0" goto :trouble
exit /b 0

:no_tools
echo fha serve: cannot find the tools next to %~dp0 1>&2
echo            (looked for .fha\tools\fha.py, then tools\fha.py). 1>&2
echo. 1>&2
echo            To restore them, run this from your workshop copy of the 1>&2
echo            project - the folder holding manifest.json: 1>&2
echo. 1>&2
echo            In Command Prompt: 1>&2
echo              cd /d PATH-TO-WORKSHOP 1>&2
echo              fha update-tools --repo PATH-TO-WORKSHOP --root "%~dp0" 1>&2
echo. 1>&2
echo            In PowerShell (`cd /d` is not PowerShell syntax, and a bare 1>&2
echo            `fha` is not run from the current folder): 1>&2
echo              Set-Location PATH-TO-WORKSHOP 1>&2
echo              .\fha update-tools --repo PATH-TO-WORKSHOP --root "%~dp0" 1>&2
echo. 1>&2
pause
exit /b 3

:no_python
echo fha serve: Python 3.10 or later is required, and neither `py -3` nor 1>&2
echo            `python` on your PATH is new enough (or neither is installed). 1>&2
echo. 1>&2
echo            Download it from https://www.python.org/downloads/ and tick 1>&2
echo            "Add Python to PATH" in the installer, then try again. 1>&2
echo. 1>&2
pause
exit /b 3

:trouble
echo.
echo fha serve could not start - read the message above.
rem Hand back the code the command actually returned. `serve` uses 3 for a
rem can't-run condition (missing Jinja2, bad config, busy port); flattening
rem everything to 1 tells a script "warning" when the tool said "failed".
rem `pause` resets errorlevel, so the value is captured before it runs.
if not defined FHA_RC set "FHA_RC=1"
pause
exit /b %FHA_RC%
