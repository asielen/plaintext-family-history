@echo off
rem fha - the archive command line. Run it from a terminal as `fha <command>`
rem (e.g. `fha lint`, `fha index`, `fha find "Margaret Cole"`). Your working
rem directory is preserved, so relative paths and archive auto-detection work
rem from anywhere inside the archive. Finds the tools whether they are vendored
rem flat (tools\) or consolidated under .fha\.
if exist "%~dp0.fha\tools\fha.py" (
  py -3 "%~dp0.fha\tools\fha.py" %*
) else (
  py -3 "%~dp0tools\fha.py" %*
)
