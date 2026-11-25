@echo off
title GEX Walls - phone access
cd /d "%~dp0"
echo Open the "On your phone" link below on a phone connected to the same Wi-Fi.
echo If Windows asks about the firewall, allow Private networks only.
".venv\Scripts\python.exe" walls_app.py --lan %*
pause
