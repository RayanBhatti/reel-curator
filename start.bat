@echo off
title Reel Curator

:: Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python is not installed or not in PATH.
    echo Install Python from https://python.org
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Create virtual environment if it doesn't exist
if not exist .venv (
    echo Setting up for the first time...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: Activate virtual environment
call .venv\Scripts\activate.bat

:: Install/update dependencies
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

:: Locate Firefox
set "FIREFOX_PATH="
if exist "%ProgramFiles%\Mozilla Firefox\firefox.exe" set "FIREFOX_PATH=%ProgramFiles%\Mozilla Firefox\firefox.exe"
if exist "%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe" set "FIREFOX_PATH=%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"

:: Open Firefox after a short delay (fall back to default browser if Firefox is missing)
if defined FIREFOX_PATH (
    start "" cmd /c "timeout /t 2 /nobreak >nul && start """" ""%FIREFOX_PATH%"" http://localhost:5000"
) else (
    echo Firefox not found, using default browser.
    start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"
)

python backend/app.py
