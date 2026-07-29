@echo off
setlocal
cd /d "%~dp0"

echo === Lead Gen Scrape Laptop - Install ===

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist "venv\Scripts\activate.bat" (
    python -m venv venv
)

call venv\Scripts\activate.bat
pip install -r requirements.txt -q

if not exist ".env" copy .env.example .env

if not exist "credentials\autoleadverification-e76d53033380.json" (
    echo WARNING: Put service account JSON in credentials\
)

echo Install complete. Run run_worker.bat to start scraping.
pause
