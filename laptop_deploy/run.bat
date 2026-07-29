@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo Run install.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
echo Optional: coordinator API on http://127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000
