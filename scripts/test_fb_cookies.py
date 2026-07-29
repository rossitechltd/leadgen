#!/usr/bin/env python3
"""Quick check: can Apify use your Facebook cookies? Fetches 1 group member."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.config import get_settings
from app.scrapers.apify_members import ApifyConfigError, load_group_urls, scrape_group_members

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> int:
    settings = get_settings()
    if not settings.apify_configured:
        print("Set APIFY_API_TOKEN in .env")
        return 1
    try:
        groups = load_group_urls(settings.fb_groups_path)
    except ApifyConfigError as exc:
        print(exc)
        return 1

    print(f"Testing cookies against {groups[0]} ...")
    try:
        members = scrape_group_members(
            api_token=settings.apify_api_token,
            cookies_path=settings.fb_cookies_path,
            group_url=groups[0],
            proxy_country=settings.apify_proxy_country,
            proxy_groups=settings.apify_proxy_groups,
            count=1,
            timeout_secs=300,
        )
    except Exception as exc:
        print(f"FAILED: {exc}")
        return 1

    if not members:
        print("FAILED: actor returned no members")
        return 1

    print(f"OK: cookies work — sample member: {members[0].get('name')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
