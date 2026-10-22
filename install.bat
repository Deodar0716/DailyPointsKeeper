@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Install DailyPointsKeeper scheduled task

echo ================================================================
echo    DailyPointsKeeper - install the daily claim task
echo    (per-user task, no administrator rights needed)
echo ================================================================
echo.

rem ---- 1. pick an interpreter ----------------------------------------------
rem Prefer a real per-user CPython. PATH's pythonw is frequently another tool's
rem virtualenv, which disappears when that tool is upgraded - and the Qoder
rem adapter needs 'cryptography' installed into whatever interpreter wins.
set "PY="
for /D %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\pythonw.exe" set "PY=%%D\pythonw.exe"
if not defined PY (
  for %%P in (pythonw.exe) do if not defined PY set "PY=%%~$PATH:P"
)
if not defined PY (
  echo [X] pythonw.exe not found. Install Python 3.9+ for this user,
  echo     then re-run this script, or call register_task.ps1 with
  echo         -PythonExe C:\path\to\pythonw.exe
  echo.
  pause
  exit /b 1
)
echo [ok] interpreter : !PY!
if exist "!PY!\..\pyvenv.cfg" echo [!] note: that pythonw.exe lives in a virtualenv -
if exist "!PY!\..\pyvenv.cfg" echo     a real per-user Python is the safer choice.
echo.

rem ---- 2. config ------------------------------------------------------------
if not exist "config.json" (
  copy /y "config.example.json" "config.json" >nul
  echo [ok] created config.json from the example.
  echo      Open it and set notify.channel + your target before you trust
  echo      the notifications. It is git-ignored: it holds your chat ids
  echo      and paths, so never commit it.
  echo.
)

rem ---- 3. self-check -------------------------------------------------------
set "DPK_CONFIG=%CD%\config.json"
echo Running self-check (doctor)...
"!PY:pythonw.exe=python.exe!" -X utf8 daily_points_keeper.py doctor
echo.

rem ---- 4. optional dependency ---------------------------------------------
"!PY:pythonw.exe=python.exe!" -c "import cryptography" 2>nul
if errorlevel 1 (
  echo [i] The Qoder adapter needs the 'cryptography' package:
  echo         "!PY:pythonw.exe=python.exe!" -m pip install -r requirements.txt
  echo     Other providers work without it.
  echo.
)

rem ---- 5. register ---------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File register_task.ps1 -PythonExe "!PY!"
if errorlevel 1 (
  echo.
  echo [X] Registration failed.
  pause
  exit /b 1
)

echo.
echo Done. The task runs the slots printed above; missed slots are made up
echo on next logon (StartWhenAvailable). To remove it: uninstall.bat
echo.
pause
exit /b 0
