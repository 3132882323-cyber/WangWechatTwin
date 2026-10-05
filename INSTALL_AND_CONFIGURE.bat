@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
if exist ".venv\Scripts\python.exe" goto deps
where py >nul 2>nul
if errorlevel 1 (
  echo Install Python 3.10-3.12 64-bit with Python Launcher, then run again.
  pause
  exit /b 1
)
set "PY_CMD="
py -3.12 -c "import sys" >nul 2>nul && set "PY_CMD=py -3.12"
if not defined PY_CMD py -3.11 -c "import sys" >nul 2>nul && set "PY_CMD=py -3.11"
if not defined PY_CMD py -3.10 -c "import sys" >nul 2>nul && set "PY_CMD=py -3.10"
if not defined PY_CMD exit /b 1
%PY_CMD% -m venv .venv
if errorlevel 1 exit /b 1
:deps
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install wxauto4==41.1.7
if errorlevel 1 exit /b 1
if not exist "config.yaml" copy /y "config.example.yaml" "config.yaml" >nul
if not exist ".env" copy /y ".env.example" ".env" >nul
echo Setup complete. Existing config and runtime files were preserved.
echo Read README.md to connect your own logged-in account. Default is draft-only.
pause
exit /b 0