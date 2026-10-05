@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" notepad.exe data\reply_samples.csv
