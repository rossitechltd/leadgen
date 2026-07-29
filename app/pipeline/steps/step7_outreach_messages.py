from __future__ import annotations

import logging

import sheets
from app.outreach import get_outreach_message_service
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus

logger = logging.getLogger(__name__)


def run(ctx: PipelineContext) -> StepResult:
    """
    Step 8: Outreach messages.

    Randomly assigns one of three outreach templates to Message1 for each surviving
    scraped lead (not ACTIVE/PARKED/redirect). Sets va=qualified when missing.
    """
    service = get_outreach_message_service(ctx.settings)
    ctx.add_log(f"Step 8: assigning outreach Message1 on {ctx.settings.sheet_dynamic_lead}")

    try:
        result = service.run(
            progress_callback=(
                lambda stats, msg: ctx.report_progress(stats, msg)
                if ctx.progress_reporter
                else None
            ),
        )
    except sheets.SheetsError as exc:
        logger.exception("Step 8 outreach messages paused on Sheets error")
        msg = str(exc)
        ctx.add_log(f"Step 8: {msg}")
        return StepResult(status=StepStatus.WAITING, message=msg, stats={})
    except Exception as exc:
        logger.exception("Step 8 outreach messages failed")
        ctx.add_log(f"Step 8: failed: {exc}")
        return StepResult(status=StepStatus.FAILED, message=str(exc), stats={})

    ctx.add_log(f"Step 8: {result.message}")

    if not result.ok:
        msg = result.message
        if "quota" in msg.lower() or "cooldown" in msg.lower():
            return StepResult(status=StepStatus.WAITING, message=msg, stats=result.stats)
        return StepResult(status=StepStatus.FAILED, message=msg, stats=result.stats)

    if result.stats.get("targets", 0) == 0:
        return StepResult(
            status=StepStatus.SKIPPED,
            message="No leads to assign outreach messages",
            stats=result.stats,
        )

    return StepResult(
        status=StepStatus.SUCCESS,
        message=result.message,
        stats=result.stats,
    )
