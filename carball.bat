@echo off
REM Generic carball CLI wrapper. Pass any args.
REM Examples:
REM   carball run
REM   carball dashboard
REM   carball match <match_id>
REM   carball vs Jenox7
REM   carball setup
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run setup-once.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m carball.cli %*
