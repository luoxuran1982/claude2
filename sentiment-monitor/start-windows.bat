@echo off
rem Sentiment Monitor - double-click to start. No extra packages needed (Python standard library only).
rem The app window opens automatically; closing it also stops the background service.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 run.py %* & exit /b 0)
where pythonw >nul 2>nul && (start "" pythonw run.py %* & exit /b 0)
where py >nul 2>nul && (py -3 run.py %* & exit /b 0)
where python >nul 2>nul && (python run.py %* & exit /b 0)
echo Python 3.9+ not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
echo Or use the packaged SentimentMonitor.exe which needs no Python.
pause
exit /b 1
