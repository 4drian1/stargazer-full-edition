@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set PYTHONUTF8=1
cd /d "%ROOT%"
if not exist "%ROOT%pkg_venv\Scripts\python.exe" (
  echo First run: creating isolated environment...
  python -m venv "%ROOT%pkg_venv"
  "%ROOT%pkg_venv\Scripts\python.exe" -m pip install -q flask pandas numpy pyarrow scikit-learn
)
start "" http://127.0.0.1:17891
"%ROOT%pkg_venv\Scripts\python.exe" -m full.webapp --port 17891
pause
