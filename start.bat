@echo off
if not exist .venv (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"
python backend/app.py
