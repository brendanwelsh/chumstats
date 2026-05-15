@echo off
REM List every player we have stats for, sorted by matches played.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m carball.cli players
pause
