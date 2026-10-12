@echo off
cd /d "%~dp0"
py -3 bootstrap.py --setup
if errorlevel 1 (
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run.py"
