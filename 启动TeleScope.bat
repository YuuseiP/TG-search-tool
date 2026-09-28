@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TeleScope

if not defined TELESCOPE_CONDA_ROOT set "TELESCOPE_CONDA_ROOT=D:\Soft\miniconda3"
if not defined TELESCOPE_CONDA_ENV set "TELESCOPE_CONDA_ENV=tg-history-search"

set "CONDA_ACTIVATE=%TELESCOPE_CONDA_ROOT%\condabin\conda.bat"
if not exist "%CONDA_ACTIVATE%" (
  echo.
  echo Conda was not found: %TELESCOPE_CONDA_ROOT%
  echo Set TELESCOPE_CONDA_ROOT, then start this file again.
  pause
  exit /b 1
)

call "%CONDA_ACTIVATE%" activate "%TELESCOPE_CONDA_ENV%"
if errorlevel 1 (
  echo.
  echo Could not activate Conda environment: %TELESCOPE_CONDA_ENV%
  echo Set TELESCOPE_CONDA_ENV, then start this file again.
  pause
  exit /b 1
)

python entry.py
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo TeleScope stopped with exit code %EXIT_CODE%. See launch-error.log for details.
  pause
)

exit /b %EXIT_CODE%
