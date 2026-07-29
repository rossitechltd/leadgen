from __future__ import annotations

import logging

from app.config import get_settings
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus
from app.refinement import get_profile_refinement

logger = logging.getLogger(__name__)


def run(ctx: PipelineContext) -> StepResult:
    """
    Step 5: Profile Refinement.

    Reads Scrape on Dynamic Lead Sheet, writes structured JSON to refined
    (and fills Phone/Owner/Website columns) via OpenRouter GPT-4o-mini.
    """
    settings = get_settings()

    if not settings.sheets_configured:
        ctx.add_log(
            f"Step 5: Google Sheets key not found at {settings.service_account_path}"
        )
        return StepResult(
            status=StepStatus.FAILED,
            message=f"Service account JSON not found: {settings.service_account_path.name}",
            stats={},
        )

    if not settings.openrouter_configured:
        ctx.add_log("Step 5: OPENROUTER_API_KEY not set")
        return StepResult(
            status=StepStatus.FAILED,
            message="OPENROUTER_API_KEY not set in .env",
            stats={},
        )

    service = get_profile_refinement(settings)
    ctx.add_log(
        f"Step 5: refining {settings.sheet_dynamic_lead} "
        f"(Scrape → refined, batch size {settings.refine_batch_size})"
    )

    try:
        result = service.run(
            progress_callback=(
                lambda stats, msg: ctx.report_progress(stats, msg)
                if ctx.progress_reporter
                else None
            ),
        )
    except Exception as exc:
        logger.exception("Step 4 profile refinement failed")
        ctx.add_log(f"Step 5: failed: {exc}")
        return StepResult(status=StepStatus.FAILED, message=str(exc), stats={})

    ctx.add_log(f"Step 5: {result.message}")

    if not result.ok:
        return StepResult(
            status=StepStatus.FAILED,
            message=result.message,
            stats=result.stats,
        )

    if result.stats.get("processed", 0) == 0 and result.stats.get("pending", 0) == 0:
        return StepResult(
            status=StepStatus.SKIPPED,
            message=result.message,
            stats=result.stats,
        )

    if result.stats.get("processed", 0) == 0 and result.stats.get("errors", 0) > 0:
        return StepResult(
            status=StepStatus.FAILED,
            message=result.message,
            stats=result.stats,
        )

    return StepResult(
        status=StepStatus.SUCCESS,
        message=result.message,
        stats=result.stats,
    )
