@echo off
REM Console-visible launcher for troubleshooting. Use TrayTools.lnk for normal use.
REM ASCII only on purpose: .bat is read in the system codepage and UTF-8 Japanese breaks parsing.
REM The venv lives outside OneDrive so its ~5400 files are not cloud-synced.
"%USERPROFILE%\.venvs\tray-tools\Scripts\python.exe" "%~dp0main.py"
echo.
echo exit code: %ERRORLEVEL%
pause
