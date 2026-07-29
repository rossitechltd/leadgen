#!/usr/bin/env python3
"""24/7 worker for Scrape Queue — verify scrapes and load next lead.

IMPORTANT: Run this on ONE machine only (typically the main PC).
If the main app runs with SCRAPE_QUEUE_POLL_ENABLED=true, do NOT run this
worker on the laptop — both will hand off links and trigger double MMM scrapes.

MMM should loop on scrapesheet row 2 and trigger ONLY when column A (link) changes:
  1. Read link from A2 — if unchanged since last loop, WAIT
  2. Scrape that URL
  3. Clear B2, paste scrape into B2
  4. Loop (wait until link in A2 changes again)
"""

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

    if settings.scrape_queue_poll_enabled:
        logger.error(
            "SCRAPE_QUEUE_POLL_ENABLED=true — the main app poller is already "
            "running ticks. Disable it on this machine or stop this worker to "
            "avoid double scrapes."
        )
        return 1

    queue = get_scrape_queue()
    poll_secs = settings.page_scrape_poll_secs

    logger.info(
        "Scrape queue worker started (poll=%ss, queue=%s)",
        poll_secs,
        settings.sheet_scrape_queue,
    )

    while True:
        try:
            result = queue.tick()
            finalize = result.get("finalize")
            enqueue = result.get("enqueue")
            if finalize and finalize.action not in {"idle", "waiting", "none"}:
                logger.info("Finalize: %s", finalize.message)
            if enqueue and enqueue.ok and enqueue.source_row:
                logger.info("Enqueue: %s", enqueue.message)
        except Exception:
            logger.exception("Worker tick error")

        time.sleep(poll_secs)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.info("Stopped")
        raise SystemExit(0)
