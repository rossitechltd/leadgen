#!/usr/bin/env python3
"""24/7 worker — writes scrape data to Dynamic Lead Sheet and loads next link."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.config import get_settings  # noqa: E402
from app.scrape_queue import get_scrape_queue  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scrape_queue_worker")


def main() -> int:
    settings = get_settings()
    if not settings.sheets_configured:
        logger.error("Google Sheets not configured — add service account JSON")
        return 1

    queue = get_scrape_queue()
    poll_secs = settings.page_scrape_poll_secs

    logger.info(
        "Worker started — scrapesheet=%s, poll=%ss",
        settings.sheet_scrape_queue,
        poll_secs,
    )

    while True:
        try:
            result = queue.run_cycle()
            phase = result.get("phase")
            finalize = result.get("finalize")
            enqueue = result.get("enqueue")

            if finalize and finalize.action == "success":
                logger.info(
                    "Wrote scrape to Dynamic Lead row %s", finalize.source_row
                )
            elif finalize and finalize.action == "failed":
                logger.warning("Scrape failed for row %s", finalize.source_row)
            elif finalize and finalize.action == "retry":
                logger.info("Retrying scrape for row %s", finalize.source_row)
            elif finalize and finalize.action == "error":
                logger.error("%s", finalize.message)

            if enqueue and enqueue.source_row:
                logger.info("scrapesheet row 2 → %s", enqueue.link)
        except Exception:
            logger.exception("Worker cycle error")

        time.sleep(poll_secs)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.info("Stopped")
        raise SystemExit(0)
