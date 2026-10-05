@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
if not exist "config.test_contact.yaml" exit /b 1
".venv\Scripts\python.exe" -u -m app --config "config.test_contact.yaml" run
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%