@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TeleScope Local Launcher

where py >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python Launcher ^(py^) is not installed or is not on PATH.
  echo Install Python from python.org, enable "Add Python to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

py entry.py
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] TeleScope did not start. See launch-error.log in this folder.
  echo.
  pause
)

exit /b %EXIT_CODE%
