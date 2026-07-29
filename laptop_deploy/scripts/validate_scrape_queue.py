#!/usr/bin/env python3
"""Monitor scrape queue E2E — verify leads save and link advances."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import sheets  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.scrape_queue import get_scrape_queue  # noqa: E402
from app.sheets.columns import (  # noqa: E402
    COL_LEAD_ACTIVITY,
    COL_SCRAPE,
    LEAD_ACTIVITY_SCRAPED,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("validate_scrape_queue")


def _api_server_running(host: str, port: int) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def _read_lead_row(settings, row_index: int) -> tuple[str, int]:
    try:
        row = sheets.read_row(settings.sheet_dynamic_lead, row_index)
    except Exception:
        return "", 0
    activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
    scrape_len = len(str(row.get(COL_SCRAPE) or ""))
    return activity, scrape_len


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate scrape queue cycles")
    parser.add_argument("--cycles", type=int, default=3, help="Successful saves required")
    parser.add_argument("--timeout", type=int, default=600, help="Max seconds to wait")
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between checks",
    )
    parser.add_argument(
        "--monitor-only",
        action="store_true",
        help="Only read sheet state (use when run.sh poller is active)",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.sheets_configured:
        logger.error("Google Sheets not configured")
        return 1

    server_running = _api_server_running(settings.host, settings.port)
    monitor_only = args.monitor_only or server_running
    if server_running and not args.monitor_only:
        logger.info(
            "API server detected — monitor-only mode (poller handles ticks)"
        )
        monitor_only = True

    queue = get_scrape_queue()
    seen_saved_rows: set[int] = set()
    cycles_done = 0
    start = time.monotonic()
    last_log = ""

    # Rows already scraped before we started — don't count toward cycles
    initial_scraped: set[int] = set()
    for row_index, row in sheets.read_all_with_row_indices(
        settings.sheet_dynamic_lead, use_cache=True
    ):
        activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
        scrape_len = len(str(row.get(COL_SCRAPE) or ""))
        if activity == LEAD_ACTIVITY_SCRAPED and scrape_len > 0:
            initial_scraped.add(row_index)
    seen_saved_rows.update(initial_scraped)
    logger.info("Already scraped rows: %s", sorted(initial_scraped) or "none")

    logger.info(
        "Watching scrapesheet — need %d successful saves (timeout %ds, mode=%s)",
        args.cycles,
        args.timeout,
        "monitor" if monitor_only else "drive",
    )

    last_full_scan = 0.0
    full_scan_interval = 30.0

    while cycles_done < args.cycles and (time.monotonic() - start) < args.timeout:
        if sheets.is_quota_cooldown():
            logger.warning(
                "Sheets quota cooldown %.0fs — waiting",
                sheets.quota_cooldown_remaining_secs(),
            )
            time.sleep(min(args.interval, sheets.quota_cooldown_remaining_secs() + 1))
            continue

        try:
            link, data = sheets.read_scrape_queue_row(
                settings.sheet_scrape_queue, 2, use_cache=False
            )
        except sheets.SheetsError:
            time.sleep(args.interval)
            continue
        active = {}
        state_path = settings.scrape_state_path
        if state_path.exists():
            import json

            try:
                active = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                active = {}
        source_row = active.get("source_row")

        finalize_action = "—"
        finalize_msg = ""
        if not monitor_only:
            result = queue.tick()
            finalize = result.get("finalize")
            if finalize:
                finalize_action = finalize.action
                finalize_msg = finalize.message
                if finalize.action == "success" and finalize.source_row:
                    seen_saved_rows.add(finalize.source_row)

        now = time.monotonic()
        if monitor_only and (now - last_full_scan) >= full_scan_interval:
            last_full_scan = now
            sheets.invalidate_worksheet_cache(settings.sheet_dynamic_lead)
            for row_index, row in sheets.read_all_with_row_indices(
                settings.sheet_dynamic_lead, use_cache=False
            ):
                activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
                scrape_len = len(str(row.get(COL_SCRAPE) or ""))
                if (
                    activity == LEAD_ACTIVITY_SCRAPED
                    and scrape_len > 0
                    and row_index not in seen_saved_rows
                ):
                    seen_saved_rows.add(row_index)
                    cycles_done = len(seen_saved_rows) - len(initial_scraped)
                    logger.info(
                        "Cycle %d complete — row %s scraped (%d chars)",
                        cycles_done,
                        row_index,
                        scrape_len,
                    )

        cycles_done = len(seen_saved_rows) - len(initial_scraped)

        activity = ""
        scrape_len = 0
        if source_row:
            activity, scrape_len = _read_lead_row(settings, source_row)

        line = (
            f"A2={link[:50] if link else '(empty)'} | B={len(data)} chars | "
            f"active_row={source_row} activity={activity!r} scrape_len={scrape_len} | "
            f"finalize={finalize_action} {finalize_msg[:60]}"
        )
        if line != last_log:
            logger.info(line)
            last_log = line

        if cycles_done >= args.cycles:
            break
        time.sleep(args.interval)

    if cycles_done >= args.cycles:
        logger.info("SUCCESS — %d scrape cycles completed", cycles_done)
        return 0

    logger.error(
        "TIMEOUT — only %d/%d cycles after %ds. saved_rows=%s active=%s",
        cycles_done,
        args.cycles,
        args.timeout,
        sorted(seen_saved_rows),
        queue.get_status().get("active_state"),
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
