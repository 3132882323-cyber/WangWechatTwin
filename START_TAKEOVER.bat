@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
".venv\Scripts\python.exe" -u -m app --config "config.takeover.yaml" run --mode low_risk_auto
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%