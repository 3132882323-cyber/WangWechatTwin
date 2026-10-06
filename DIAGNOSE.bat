@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 请先运行“INSTALL_AND_CONFIGURE.bat”。
  pause
  exit /b 1
)
echo === 核心功能自检 ===
.venv\Scripts\python.exe -m pytest -q
if errorlevel 1 (
  echo 核心功能自检失败，先不要启动自动回复。
  pause
  exit /b 1
)
echo.
echo === 本机微信与配置诊断 ===
call "%~dp0scripts\active_config.bat"
.venv\Scripts\python.exe -m app --config "%CONFIG%" doctor
pause
