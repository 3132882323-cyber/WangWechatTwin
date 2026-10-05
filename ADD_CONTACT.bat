@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 请先运行“INSTALL_AND_CONFIGURE.bat”。
  pause
  exit /b 1
)
set /p NAME=请输入微信中的准确备注名或昵称：
if "%NAME%"=="" exit /b 0
set /p REL=关系（例如 箱房客户/供应商/员工/熟人）：
set /p DOMAIN=业务类型（general/modular_housing/underwear/ai）：
if "%DOMAIN%"=="" set "DOMAIN=general"
set /p NOTES=当前项目和注意事项：
.venv\Scripts\python.exe scripts\add_contact.py "%NAME%" --relationship "%REL%" --domain "%DOMAIN%" --mode shadow --notes "%NOTES%"
echo 已按影子模式加入。重启程序后生效。
pause
