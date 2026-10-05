@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 请先运行 INSTALL_AND_CONFIGURE.bat。
  pause
  exit /b 1
)
echo 本模式只自动发送低风险、高置信度日常回复；报价、工期、合同、付款等进入审核。
.venv\Scripts\python.exe -m app --config config.yaml run --mode low_risk_auto
pause
