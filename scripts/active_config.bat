@echo off
set "CONFIG=config.yaml"
if exist "%~dp0..\config.history.yaml" if exist "%~dp0..\.runtime\history_reader\keys.json" set "CONFIG=config.history.yaml"
if exist "%~dp0..\config.takeover.yaml" set "CONFIG=config.takeover.yaml"
if exist "%~dp0..\config.http-api.yaml" set "CONFIG=config.http-api.yaml"
if /I "%~1"=="--print" echo %CONFIG%
exit /b 0
