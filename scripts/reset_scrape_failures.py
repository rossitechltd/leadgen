#!/usr/bin/env python3
"""Reset scrape_failed leads back to pending_scrape and clear local failure counts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import sheets
from app.config import get_settings
from app.sheets.columns import COL_LEAD_ACTIVITY, is_scrape_failed_activity


def main() -> int:
    settings = get_settings()
    failures_path = ROOT / "data" / "scrape_queue" / "failures.json"

    rows = sheets.read_all(settings.sheet_dynamic_lead)
    reset_rows: list[int] = []
    for index, row in enumerate(rows, start=2):
        activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
        if is_scrape_failed_activity(activity):
            sheets.update_row_by_header(
                settings.sheet_dynamic_lead,
                index,
                {COL_LEAD_ACTIVITY: "pending_scrape"},
            )
            reset_rows.append(index)

    if failures_path.exists():
        failures_path.write_text("{}\n", encoding="utf-8")

    active_path = ROOT / "data" / "scrape_queue" / "active.json"
    if active_path.exists():
        active_path.unlink()

    print(f"Reset {len(reset_rows)} scrape_failed row(s) to pending_scrape")
    print("Cleared failures.json and active.json")
    print("Restart Step 3 with MMM running on scrapesheet row 2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
