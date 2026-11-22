@echo off
setlocal EnableExtensions
title Aluminum Wholesale POS
cd /d "%~dp0"

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"

REM ---- 1. Is the private environment present *and* able to import Flask? ----
REM Checking only for python.exe is not enough: an interrupted setup (or a
REM damaged disk) leaves python.exe behind with no pip and no Flask, and the
REM app then dies the moment the window opens.
set "NEED_SETUP=1"
if exist "%PY%" (
    "%PY%" -c "import flask" >nul 2>&1
    if not errorlevel 1 set "NEED_SETUP=0"
)

if "%NEED_SETUP%"=="1" (
    call :setup
    if errorlevel 1 goto :setup_failed
)

REM ---- 2. Run the shop ----------------------------------------------------
echo.
echo Starting shop app at http://127.0.0.1:5000
echo Close this window or press Ctrl+C to stop.
echo.
"%PY%" app.py
if errorlevel 1 (
    echo.
    echo The shop app stopped with an error - read the message above.
    pause
)
goto :eof

REM ---- Setup: (re)build .venv and install Flask --------------------------
:setup
set "SYSPY="
where py >nul 2>nul
if not errorlevel 1 set "SYSPY=py -3"
if not defined SYSPY (
    where python >nul 2>nul
    if not errorlevel 1 set "SYSPY=python"
)
if not defined SYSPY (
    echo Python was not found. Install Python 3.9+ from python.org first,
    echo then run this file again.
    exit /b 1
)

if exist "%VENV%" (
    echo Removing the old environment...
    rmdir /s /q "%VENV%" 2>nul
)

echo Creating the private Python environment - one time, about 30 seconds...
%SYSPY% -m venv "%VENV%"
if errorlevel 1 exit /b 1

echo Installing Flask...
"%PY%" -m pip install --disable-pip-version-check --quiet --upgrade pip
"%PY%" -m pip install --disable-pip-version-check --quiet -r requirements.txt
if errorlevel 1 exit /b 1
"%PY%" -c "import flask" >nul 2>&1
if errorlevel 1 exit /b 1

echo Environment ready.
exit /b 0

:setup_failed
echo.
echo Setup failed. Check that Python 3.9+ is installed and on PATH.
pause
