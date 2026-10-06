@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
  echo Please run INSTALL_AND_CONFIGURE.bat first.
  pause
  exit /b 1
)
call "%~dp0scripts\active_config.bat"
".venv\Scripts\python.exe" -u -m app --config "%CONFIG%" resume
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
