from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus
from app.scrape_queue import get_scrape_queue

logger = logging.getLogger(__name__)


def run(ctx: PipelineContext) -> StepResult:
    """
    Step 3: Finalize scrapesheet data if present, then enqueue next link.

    Works on main PC or laptop — write-back uses the link in scrapesheet row 2
    to find the matching Dynamic Lead Sheet row (no local state file required).
    """
    settings = get_settings()

    if not settings.sheets_configured:
        ctx.add_log(
            f"Step 3: Google Sheets key not found at {settings.service_account_path}"
        )
        return StepResult(
            status=StepStatus.FAILED,
            message=f"Service account JSON not found: {settings.service_account_path.name}",
            stats={},
        )

    queue = get_scrape_queue()

    try:
        queue.ensure_queue_sheet()
        status = queue.get_status()
        pending = status["pending"]

        ctx.add_log(
            f"Step 3: {pending} pending lead(s); scrapesheet idle={status['queue_idle']}"
        )

        stats: dict[str, Any] = {
            "pending": pending,
            "queue_idle": status["queue_idle"],
            "queue_row": status.get("queue_row"),
            "sheet_scrape_queue": settings.sheet_scrape_queue,
        }

        # Write back to Dynamic Lead Sheet when MMM has filled the data column
        finalize = queue.finalize_if_ready()
        stats["finalize"] = {
            "ok": finalize.ok,
            "message": finalize.message,
            "action": finalize.action,
            "source_row": finalize.source_row,
        }
        if finalize.action not in {"idle", "waiting", "none"}:
            ctx.add_log(f"Step 3 finalize: {finalize.message}")

        if finalize.action == "error":
            return StepResult(
                status=StepStatus.FAILED,
                message=finalize.message,
                stats=stats,
            )

        if pending == 0 and status["queue_idle"] and finalize.action in {"idle", "waiting"}:
            return StepResult(
                status=StepStatus.SUCCESS,
                message="No leads pending page scrape",
                stats=stats,
            )

        enqueue = queue.enqueue_next_lead()
        stats["enqueue"] = {
            "ok": enqueue.ok,
            "message": enqueue.message,
            "source_row": enqueue.source_row,
            "link": enqueue.link,
        }
        ctx.add_log(f"Step 3 enqueue: {enqueue.message}")

        if not enqueue.ok:
            return StepResult(
                status=StepStatus.FAILED,
                message=enqueue.message,
                stats=stats,
            )

        parts = [p for p in (finalize.message, enqueue.message) if p and "idle" not in p.lower()]
        return StepResult(
            status=StepStatus.SUCCESS,
            message=" — ".join(parts) if parts else "Step 3 complete",
            stats=stats,
        )
    except Exception as exc:
        logger.exception("Step 3 scrape queue failed")
        ctx.add_log(f"Step 3: failed: {exc}")
        return StepResult(
            status=StepStatus.FAILED,
            message=str(exc),
            stats={},
        )
