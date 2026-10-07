@echo off
rem Windows: double-click to run. First run creates .venv and installs dependencies.
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto nopython
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 goto nopython
)
fc /b requirements.txt .venv\req-installed.txt >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto pipfail
  copy /y requirements.txt .venv\req-installed.txt >nul
)
".venv\Scripts\python.exe" run.py %*
pause
exit /b 0
:nopython
echo Python 3.9+ not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
exit /b 1
:pipfail
echo Failed to install dependencies. Check your network, then run this file again.
pause
exit /b 1
