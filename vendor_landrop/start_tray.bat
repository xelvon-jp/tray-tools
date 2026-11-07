@echo off
rem Start LAN Drop in the system tray (no console). Updates requirements first.
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
    python -m venv .venv || goto :err
)
.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt || goto :err
start "" .venv\Scripts\pythonw.exe -m landrop --tray
exit /b

:err
echo Setup failed.
pause
