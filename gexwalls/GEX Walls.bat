@echo off
title GEX Walls
cd /d "%~dp0"
".venv\Scripts\python.exe" walls_app.py %*
pause
