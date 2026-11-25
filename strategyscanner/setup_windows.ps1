# One-time setup: private Python environment, self-tests, and a weekday Task Scheduler job.
# Run from this folder:  powershell -ExecutionPolicy Bypass -File setup_windows.ps1
param([string]$RunAt = "1:40AM", [string]$UniverseAt = "3:00AM")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
& .\.venv\Scripts\python.exe tests\test_core.py
if ($LASTEXITCODE -ne 0) { throw "Self-tests failed" }

$pyw = Join-Path $here ".venv\Scripts\pythonw.exe"
$action = New-ScheduledTaskAction -Execute $pyw -Argument "run_daily.py" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $RunAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "StrategyScanner Daily" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Weekday options strategy scan (daily/weekly/monthly/LEAPS) with a self-grading pick log" -Force | Out-Null
$uaction = New-ScheduledTaskAction -Execute $pyw -Argument "tools\build_universe.py" -WorkingDirectory $here
$utrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $UniverseAt
Register-ScheduledTask -TaskName "StrategyScanner Universe" -Action $uaction -Trigger $utrigger -Settings $settings `
    -Description "Weekly refresh of every optionable stock/ETF with a liquid chain (universe_auto.txt)" -Force | Out-Null
Write-Output "Scheduled 'StrategyScanner Daily' for weekdays at $RunAt and 'StrategyScanner Universe' for Sundays at $UniverseAt local time."
