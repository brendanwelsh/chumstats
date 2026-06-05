@echo off
REM Compare you vs another player on lifetime stats.
REM Usage:  compare.bat <player_name>
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: compare.bat ^<player_name^>
  echo Example: compare.bat Jenox7
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m ballshark.cli compare %*
pause
