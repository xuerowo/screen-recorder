@echo off
cd /d "%~dp0"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo Setting up local Python environment...
    py -3.11 -m venv "%~dp0.venv"
    if errorlevel 1 goto setup_failed
)

"%PYTHON_EXE%" -c "import recorder" >nul 2>nul
if errorlevel 1 (
    echo Installing Python dependencies...
    "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 goto setup_failed
)

"%PYTHON_EXE%" "%~dp0recorder.py"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo recorder.py has exited. Press any key to close this window.
pause >nul
exit /b %EXIT_CODE%

:setup_failed
echo.
echo Failed to set up Python environment. Make sure Python 3.11 is installed.
pause >nul
exit /b 1
