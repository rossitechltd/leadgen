#!/usr/bin/env python3
"""Send a one-shot Telegram test message."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.notifications.telegram import dashboard_url, is_telegram_configured, send_telegram


def main() -> int:
    if not is_telegram_configured():
        print("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return 1

    text = f"Lead Gen Pipeline — Telegram test OK\n\n{dashboard_url()}"
    result = send_telegram(text)
    if result.get("ok"):
        print("Telegram test message sent.")
        return 0

    print(f"Telegram test failed: {result.get('error') or result}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
