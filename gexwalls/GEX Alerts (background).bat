@echo off
title GEX Walls - downside alert monitor
cd /d "%~dp0"
echo Background alert monitor (no browser page). Leave this window open, or minimize it.
".venv\Scripts\python.exe" walls_app.py --watch
pause
