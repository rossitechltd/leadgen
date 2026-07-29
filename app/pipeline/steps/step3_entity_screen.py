from __future__ import annotations

import logging

import sheets
from app.config import get_settings
from app.entity import get_entity_screen_service
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus

logger = logging.getLogger(__name__)


def run(ctx: PipelineContext) -> StepResult:
    """Step 3: Light entity screen — remove obvious personal profiles before page scrape."""
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

    if not settings.openrouter_configured:
        ctx.add_log("Step 3: OPENROUTER_API_KEY not set")
        return StepResult(
            status=StepStatus.FAILED,
            message="OPENROUTER_API_KEY not set in .env",
            stats={},
        )

    ctx.add_log(
        f"Step 3: entity screen on {settings.sheet_dynamic_lead} "
        f"(batch {settings.entity_classify_batch_size}, "
        f"auto-person threshold {settings.entity_screen_auto_person})"
    )

    service = get_entity_screen_service(settings)
    try:
        result = service.run(
            progress_callback=(
                lambda stats, msg: ctx.report_progress(stats, msg)
                if ctx.progress_reporter
                else None
            ),
        )
    except sheets.SheetsError as exc:
        coerced = sheets.coerce_quota_error(exc)
        logger.exception("Step 3 entity screen failed on Sheets quota")
        msg = str(coerced or exc)
        ctx.add_log(f"Step 3: {msg}")
        return StepResult(status=StepStatus.WAITING, message=msg, stats={})
    except Exception as exc:
        logger.exception("Step 3 entity screen failed")
        ctx.add_log(f"Step 3: failed: {exc}")
        return StepResult(status=StepStatus.FAILED, message=str(exc), stats={})

    ctx.add_log(f"Step 3: {result.message}")

    if not result.ok:
        msg = result.message
        if "quota" in msg.lower() or "cooldown" in msg.lower():
            ctx.add_log(f"Step 3: {msg}")
            return StepResult(
                status=StepStatus.WAITING,
                message=msg,
                stats=result.stats,
            )
        return StepResult(
            status=StepStatus.FAILED,
            message=result.message,
            stats=result.stats,
        )

    if result.stats.get("screened", 0) == 0:
        return StepResult(
            status=StepStatus.SKIPPED,
            message="No leads to screen on Dynamic Lead Sheet",
            stats=result.stats,
        )

    return StepResult(
        status=StepStatus.SUCCESS,
        message=result.message,
        stats=result.stats,
    )
