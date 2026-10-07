@echo off
rem Same as start-windows.bat but keeps a console window with live logs (for troubleshooting).
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python 3.9+ not found. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)
%PY% run.py %*
echo.
echo Stopped. Log file: %APPDATA%\SentimentMonitor\monitor.log
pause
