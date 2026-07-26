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

if exist "%~dp0fha.cmd" (
  call "%~dp0fha.cmd" serve %*
  if errorlevel 1 goto :trouble
  exit /b 0
)

rem No fha.cmd beside us - an archive from before the launchers shipped. Do the
rem same two checks inline rather than handing a missing path to an interpreter
rem that may not even be installed.
set "FHA_ENTRY="
if exist "%~dp0.fha\tools\fha.py" set "FHA_ENTRY=%~dp0.fha\tools\fha.py"
if not defined FHA_ENTRY if exist "%~dp0tools\fha.py" set "FHA_ENTRY=%~dp0tools\fha.py"
if not defined FHA_ENTRY goto :no_tools

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :run_py
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :run_python
goto :no_python

:run_py
py -3 "%FHA_ENTRY%" serve %*
if errorlevel 1 goto :trouble
exit /b 0

:run_python
python "%FHA_ENTRY%" serve %*
if errorlevel 1 goto :trouble
exit /b 0

:no_tools
echo fha serve: cannot find the tools next to %~dp0 1>&2
echo            (looked for .fha\tools\fha.py, then tools\fha.py). 1>&2
echo. 1>&2
echo            To restore them, run this from your workshop copy of the 1>&2
echo            project - the folder holding manifest.json: 1>&2
echo. 1>&2
echo              cd /d PATH-TO-WORKSHOP 1>&2
echo              fha update-tools --repo PATH-TO-WORKSHOP --root "%~dp0" 1>&2
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
pause
exit /b 1
