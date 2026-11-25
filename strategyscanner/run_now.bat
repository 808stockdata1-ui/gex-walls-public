@echo off
rem Run a scan now and open the report. Extra arguments pass through, e.g. run_now.bat --tickers SPY,AAPL --no-log
cd /d "%~dp0"
.venv\Scripts\python.exe run_daily.py %*
echo %* | find "--no-log" >nul && (start "" "reports\preview.html") || (start "" "reports\latest.html")
