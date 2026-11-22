@echo off
REM ---- Aluminum Wholesale POS: one-click start on Windows ----
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :error
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
)

echo.
echo Starting shop app at http://127.0.0.1:5000
echo Close this window or press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" app.py
goto :eof

:error
echo.
echo Setup failed. Check that Python 3.9+ is installed and on PATH.
pause
