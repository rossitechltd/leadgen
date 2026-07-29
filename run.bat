@echo off
setlocal

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt -q

if not exist ".env" (
    echo.
    echo WARNING: .env file not found. Copy .env.example to .env and configure it.
    echo.
)

echo Starting Lead Gen Pipeline on http://127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
