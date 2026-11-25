@echo off
title GEX Walls - text mode
cd /d "%~dp0"
".venv\Scripts\python.exe" walls_app.py --cli %*
pause
