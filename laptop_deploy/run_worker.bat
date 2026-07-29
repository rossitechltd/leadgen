@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo Run install.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
echo Scrape queue worker - watches Scrape Queue row 2
echo Keep this open with Mini Mouse Macro running.
python scripts\scrape_queue_worker.py
