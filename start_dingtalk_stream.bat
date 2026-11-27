@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
"%~dp0pkg_venv\Scripts\python.exe" -B -m full.dingtalk_bot
pause
