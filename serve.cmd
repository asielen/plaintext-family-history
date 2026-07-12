@echo off
rem fha serve - double-click this to open the private workbench for this archive.
rem It runs on this machine only (127.0.0.1), no network, no login. Close the
rem window (or press Ctrl-C) to stop it; nothing is lost.
cd /d "%~dp0"
py -3 tools\fha.py serve %*
if errorlevel 1 (
  echo.
  echo fha serve could not start - read the message above.
  pause
)
