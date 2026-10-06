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
if /I "%CONFIG%"=="config.http-api.yaml" (
  echo HTTP transport selected. The configured mode is preserved.
  echo During interface migration, shadow mode generates drafts only.
  ".venv\Scripts\python.exe" -u -m app --config "%CONFIG%" run
) else (
  ".venv\Scripts\python.exe" -u -m app --config "%CONFIG%" run --mode low_risk_auto
)
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
