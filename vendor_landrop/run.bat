@echo off
rem Start LAN Drop receiver. Creates .venv on first run and keeps requirements up to date.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv || goto :err
)
.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt || goto :err
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe -m landrop
pause
exit /b

:err
echo Setup failed.
pause
