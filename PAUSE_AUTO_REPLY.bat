@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
  echo Please run INSTALL_AND_CONFIGURE.bat first.
  pause
  exit /b 1
)
set "CONFIG=config.yaml"
if exist "config.history.yaml" if exist ".runtime\history_reader\keys.json" set "CONFIG=config.history.yaml"
if exist "config.takeover.yaml" set "CONFIG=config.takeover.yaml"
".venv\Scripts\python.exe" -u -m app --config "%CONFIG%" pause
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
