@echo off
REM Double-click this to start ballshark: live ingest + Discord bot + overlay server.
REM Ctrl+C in the window stops everything.
cd /d "%~dp0"
title ballshark - running (Ctrl+C to stop)

if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run setup-once.bat first.
  pause
  exit /b 1
)

echo.
echo === ballshark ===
echo   overlay   ^=^>  http://127.0.0.1:5050/
echo   dashboard ^=^>  http://127.0.0.1:5050/dashboard
echo.
echo   Discord posts go to channel %%DISCORD_CHANNEL_ID%% from your .env
echo   Stop with Ctrl+C in this window.
echo.

".venv\Scripts\python.exe" -m ballshark.cli run
echo.
echo ballshark stopped.
pause
