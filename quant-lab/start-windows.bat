@echo off
rem Windows source run: double-click. First run creates .venv and installs dependencies.
chcp 65001 >nul
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
  echo Installing dependencies, this may take a few minutes...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto pipfail
  copy /y requirements.txt .venv\req-installed.txt >nul
)
start "" ".venv\Scripts\pythonw.exe" run.py %*
exit /b 0
:nopython
echo Python 3.10+ not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
exit /b 1
:pipfail
echo Failed to install dependencies. Check your network (or set a PyPI mirror), then run this file again.
pause
exit /b 1
