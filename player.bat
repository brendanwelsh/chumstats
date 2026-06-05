@echo off
REM Show career dashboard for any player by name.
REM Usage:  player.bat <player_name>
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: player.bat ^<player_name^>
  echo Example: player.bat Jenox7
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m ballshark.cli player %*
pause
