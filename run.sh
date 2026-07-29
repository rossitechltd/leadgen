#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "Installing dependencies..."
pip install -r requirements.txt -q

if [ ! -f ".env" ]; then
  echo ""
  echo "WARNING: .env file not found. Copy .env.example to .env if you need overrides."
  echo ""
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "Port ${PORT} is in use — stopping previous instance..."
  lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo "Starting Lead Gen Pipeline on http://${HOST}:${PORT}"
exec uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
