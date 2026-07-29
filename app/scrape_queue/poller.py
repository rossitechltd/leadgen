"""Background poller — watches scrapesheet and updates Dynamic Lead Sheet."""

from __future__ import annotations

import logging

from app.config import get_settings
from app.scrape_queue import get_scrape_queue

import sheets

logger = logging.getLogger(__name__)


def scrape_queue_poll() -> None:
    """
    Run one scrape-queue cycle on the main PC.

    When MMM pastes into scrapesheet row 2 column B:
    - verify + copy to Dynamic Lead Sheet
    - set the next pending link in column A
    """
    from app.pipeline.runner import get_pipeline_runner
    from app.scheduler import reschedule_scrape_queue_poll

    settings = get_settings()
    if not settings.scrape_queue_poll_enabled:
        return
    if not settings.sheets_configured:
        return

    # Step 4 wait loop polls the queue itself — skip while any pipeline step runs.
    runner = get_pipeline_runner()
    if runner.state.is_running:
        step_id = runner.state.current_step_id
        if step_id == 4:
            logger.debug(
                "Scrape queue poll skipped — Step 4 is running (wait loop handles ticks)"
            )
        else:
            logger.debug(
                "Scrape queue poll skipped — pipeline step %s running",
                step_id,
            )
        reschedule_scrape_queue_poll()
        return

    try:
        if sheets.is_quota_cooldown():
            logger.info(
                "Scrape queue paused — Sheets cooldown %.0fs",
                sheets.quota_cooldown_remaining_secs(),
            )
            reschedule_scrape_queue_poll()
            return
        result = get_scrape_queue().tick()
    except sheets.SheetsError as exc:
        logger.warning("Scrape queue poll paused: %s", exc)
        reschedule_scrape_queue_poll()
        return
    except Exception as exc:
        coerced = sheets.coerce_quota_error(exc)
        if coerced is not None:
            logger.warning("Scrape queue poll paused: %s", coerced)
            reschedule_scrape_queue_poll()
            return
        logger.exception("Scrape queue poll failed")
        reschedule_scrape_queue_poll()
        return

    finalize = result.get("finalize")
    enqueue = result.get("enqueue")

    if finalize and finalize.action == "success":
        saved = finalize.stats.get("saved_chars")
        if saved:
            logger.info(
                "Scrape queue: saved row %s (%d chars) — %s",
                finalize.source_row,
                saved,
                finalize.message,
            )
        else:
            logger.info(
                "Scrape queue: wrote back row %s — %s",
                finalize.source_row,
                finalize.message,
            )
    elif finalize and finalize.action == "retry":
        logger.warning("Scrape queue: %s", finalize.message)
    elif finalize and finalize.action == "failed":
        logger.warning("Scrape queue: %s", finalize.message)
    elif finalize and finalize.action == "cooldown":
        logger.info("Scrape queue: %s", finalize.message)
    elif finalize and finalize.action == "error":
        logger.error("Scrape queue: %s", finalize.message)
    elif finalize and finalize.action in {"stabilizing", "waiting"}:
        logger.info("Scrape queue: %s", finalize.message)

    if enqueue and enqueue.source_row and enqueue.message:
        logger.info("Scrape queue: %s", enqueue.message)

    reschedule_scrape_queue_poll()
