@echo off
cd /d "%~dp0"
python recorder.py
echo.
echo recorder.py has exited. Press any key to close this window.
pause >nul
