from __future__ import annotations

import logging

from app.config import get_settings
from app.entity.uncertain_clarify_service import get_entity_uncertain_clarify_service
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus

logger = logging.getLogger(__name__)


def run(ctx: PipelineContext) -> StepResult:
    """Step 6: Re-classify entity_uncertain leads using scrape + refined data."""
    settings = get_settings()

    if not settings.sheets_configured:
        ctx.add_log(
            f"Step 6: Google Sheets key not found at {settings.service_account_path}"
        )
        return StepResult(
            status=StepStatus.FAILED,
            message=f"Service account JSON not found: {settings.service_account_path.name}",
            stats={},
        )

    if not settings.openrouter_configured:
        ctx.add_log("Step 6: OPENROUTER_API_KEY not set")
        return StepResult(
            status=StepStatus.FAILED,
            message="OPENROUTER_API_KEY not set in .env",
            stats={},
        )

    service = get_entity_uncertain_clarify_service(settings)
    ctx.add_log(
        f"Step 6: clarifying entity_uncertain on {settings.sheet_dynamic_lead} "
        f"(batch {settings.entity_classify_batch_size})"
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
        logger.exception("Step 6 entity clarify failed")
        ctx.add_log(f"Step 6: failed: {exc}")
        return StepResult(status=StepStatus.FAILED, message=str(exc), stats={})

    ctx.add_log(f"Step 6: {result.message}")

    if not result.ok:
        return StepResult(
            status=StepStatus.FAILED,
            message=result.message,
            stats=result.stats,
        )

    if result.stats.get("candidates", 0) == 0:
        return StepResult(
            status=StepStatus.SKIPPED,
            message=result.message,
            stats=result.stats,
        )

    return StepResult(
        status=StepStatus.SUCCESS,
        message=result.message,
        stats=result.stats,
    )
