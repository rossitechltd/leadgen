from __future__ import annotations

import logging

import sheets
from app.config import get_settings
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus
from app.qualify import get_ai_qualify_service

logger = logging.getLogger(__name__)


def run(ctx: PipelineContext) -> StepResult:
    """
    Step 7: AI Qualify.

    Keeps leads that have a phone and no genuine active website.
    Removes: no phone, ACTIVE websites, PARKED domains, business website redirects.
    Expired/unreachable/no website → keep.
    Person/business separation is handled in Step 3 (entity screen).
    """
    settings = get_settings()

    if not settings.sheets_configured:
        ctx.add_log(
            f"Step 7: Google Sheets key not found at {settings.service_account_path}"
        )
        return StepResult(
            status=StepStatus.FAILED,
            message=f"Service account JSON not found: {settings.service_account_path.name}",
            stats={},
        )

    if not settings.openrouter_configured:
        ctx.add_log("Step 7: OPENROUTER_API_KEY not set")
        return StepResult(
            status=StepStatus.FAILED,
            message="OPENROUTER_API_KEY not set in .env",
            stats={},
        )

    service = get_ai_qualify_service(settings)
    ctx.add_log(
        f"Step 7: qualifying {settings.sheet_dynamic_lead} "
        f"(website timeout {settings.qualify_website_timeout_secs}s)"
    )

    try:
        result = service.run(
            progress_callback=(
                lambda stats, msg: ctx.report_progress(stats, msg)
                if ctx.progress_reporter
                else None
            ),
        )
    except sheets.SheetsError as exc:
        logger.exception("Step 7 AI qualify paused on Sheets quota")
        msg = str(exc)
        ctx.add_log(f"Step 7: {msg}")
        return StepResult(status=StepStatus.WAITING, message=msg, stats={})
    except Exception as exc:
        logger.exception("Step 7 AI qualify failed")
        ctx.add_log(f"Step 7: failed: {exc}")
        return StepResult(status=StepStatus.FAILED, message=str(exc), stats={})

    ctx.add_log(f"Step 7: {result.message}")

    if not result.ok:
        msg = result.message
        if "quota" in msg.lower() or "cooldown" in msg.lower():
            ctx.add_log(f"Step 7: {msg}")
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

    if result.stats.get("candidates", 0) == 0:
        return StepResult(
            status=StepStatus.SKIPPED,
            message="No leads to qualify on Dynamic Lead Sheet",
            stats=result.stats,
        )

    return StepResult(
        status=StepStatus.SUCCESS,
        message=result.message,
        stats=result.stats,
    )
