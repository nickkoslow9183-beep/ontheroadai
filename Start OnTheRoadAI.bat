@echo off
rem Double-click to start OnTheRoadAI. Keep this window open while using it.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 launch.py
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3 is not installed. Get it free from https://www.python.org/downloads/
    echo During setup, tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  python launch.py
)
if errorlevel 1 pause
