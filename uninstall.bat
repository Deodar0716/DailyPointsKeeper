@echo off
setlocal
cd /d "%~dp0"
title Uninstall DailyPointsKeeper scheduled task

echo ================================================================
echo    Remove the DailyPointsKeeper scheduled task
echo ================================================================
echo.
echo Logs, state files and config.json stay on disk (var\ folder).
echo Delete them yourself if you also want the history gone.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File register_task.ps1 -RemoveOnly
if errorlevel 1 (
  echo.
  echo [X] Could not remove the task. Try:
  echo         schtasks /Delete /TN DailyPointsKeeper /F
  echo.
  pause
  exit /b 1
)

echo.
echo Done - the daily claims will stop.
echo.
pause
exit /b 0
