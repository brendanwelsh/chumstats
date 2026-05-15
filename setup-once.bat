@echo off
REM First-time installer. Run once after cloning / extracting.
REM Idempotent - safe to re-run.
cd /d "%~dp0"
title carball setup

echo === carball setup ===
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo Python is not on PATH.
  echo Install Python 3.11+ from https://www.python.org/downloads/
  echo or install via:  winget install Python.Python.3.12
  echo Then re-run this script.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Could not create venv. Check your Python install.
    pause
    exit /b 1
  )
)

echo Installing carball and dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -e .[dev,server,bot] -q
if errorlevel 1 (
  echo Install failed.
  pause
  exit /b 1
)

echo.
echo === enabling Stats API in Rocket League ===
".venv\Scripts\python.exe" -m carball.cli setup

echo.
if not exist .env (
  echo No .env found. Copy .env.example to .env and fill in your Discord token + channel id.
) else (
  echo .env present. Edit it if you need to change Discord token / channel.
)

echo.
echo Setup done. Double-click run.bat to launch carball.
pause
