@echo off
REM Generic chumstats CLI wrapper. Pass any args.
REM Examples:
REM   chumstats run
REM   chumstats dashboard
REM   chumstats match <match_id>
REM   chumstats vs <player>
REM   chumstats setup
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run setup-once.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m chumstats.cli %*
