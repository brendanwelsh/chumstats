@echo off
REM Generic ballshark CLI wrapper. Pass any args.
REM Examples:
REM   ballshark run
REM   ballshark dashboard
REM   ballshark match <match_id>
REM   ballshark vs Jenox7
REM   ballshark setup
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run setup-once.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m ballshark.cli %*
