@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 请先运行 INSTALL_AND_CONFIGURE.bat。
  pause
  exit /b 1
)
echo 本模式会自动处理低风险和部分中风险消息；高风险与关键承诺仍只发占位回复并进入审核。
echo 请先至少完成影子模式和低风险模式测试。
set /p CONFIRM=确认启动请输入 YES：
if /I not "%CONFIRM%"=="YES" (
  echo 已取消。
  pause
  exit /b 0
)
.venv\Scripts\python.exe -m app --config config.yaml run --mode full_auto
pause
