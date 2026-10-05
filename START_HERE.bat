@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
:menu
cls
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_menu.ps1" <nul
if errorlevel 1 (
  echo Menu display failed. Choose 1=setup 2=diagnose 3=shadow 6=console 0=exit.
)
set "CHOICE="
set /p "CHOICE=Select [0-9,A,B,C]: "
if /I "!CHOICE!"=="0" exit /b 0
set "TARGET="
if /I "!CHOICE!"=="1" set "TARGET=INSTALL_AND_CONFIGURE.bat"
if /I "!CHOICE!"=="2" set "TARGET=DIAGNOSE.bat"
if /I "!CHOICE!"=="3" set "TARGET=START_SHADOW_MODE.bat"
if /I "!CHOICE!"=="4" set "TARGET=START_LOW_RISK_AUTO.bat"
if /I "!CHOICE!"=="5" set "TARGET=FULL_AUTO.bat"
if /I "!CHOICE!"=="6" set "TARGET=OPEN_CONSOLE.bat"
if /I "!CHOICE!"=="7" set "TARGET=ADD_CONTACT.bat"
if /I "!CHOICE!"=="8" set "TARGET=PAUSE_AUTO_REPLY.bat"
if /I "!CHOICE!"=="9" set "TARGET=RESUME_AUTO_REPLY.bat"
if /I "!CHOICE!"=="A" set "TARGET=EDIT_CONFIG.bat"
if /I "!CHOICE!"=="B" set "TARGET=TRAIN_MY_STYLE.bat"
if /I "!CHOICE!"=="C" set "TARGET=START_TAKEOVER.bat"
if defined TARGET (
  call "%~dp0!TARGET!"
) else (
  echo Invalid selection. Enter a menu key.
)
goto menu