@echo off
rem Seen It - one-time setup of the optional helper for Advanced mode.
rem
rem   Double-click this file, or run:   install.cmd "C:\path\to\your archive"
rem
rem Seen It works without this. Run it only if you want the add-on to mark
rem what your family archive already holds, or to keep its data in a folder
rem you choose. It changes only your own (current-user) browser settings - no
rem administrator needed - and is safe to run again after moving a folder.
rem Needs Python 3 (python.org - tick "Add python.exe to PATH" when installing).

setlocal
set "HOSTDIR=%~dp0"
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY goto :nopython

set "ARCHIVE=%~1"
if defined ARCHIVE goto :run
echo.
echo Where is your family archive? Paste its top folder - the one that holds
echo fha.yaml - and press Enter. (Tip: in File Explorer, click the folder,
echo then Ctrl+Shift+C copies its path.) Or just press Enter to skip.
echo.
set /p "ARCHIVE=Archive folder: "
rem "Copy as path" puts quotes around the folder. Take them off before the
rem value is used anywhere: a quoted folder with a space in its name breaks
rem every test of it below ("... was unexpected at this time").
if defined ARCHIVE set "ARCHIVE=%ARCHIVE:"=%"

:run
if not defined ARCHIVE goto :noarchive
%PY% "%HOSTDIR%seenit_host.py" --install --archive "%ARCHIVE%"
goto :checked
:noarchive
%PY% "%HOSTDIR%seenit_host.py" --install
:checked
if errorlevel 1 goto :failed
echo.
pause
exit /b 0

:nopython
echo Python was not found. Install Python 3 from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then run this file again.
pause
exit /b 1

:failed
echo.
echo The helper was not set up - the line above says why. Fix that and run this file again.
pause
exit /b 1
