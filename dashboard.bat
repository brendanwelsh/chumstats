@echo off
REM Print your career dashboard in the terminal.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run setup-once.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m carball.cli dashboard
pause
