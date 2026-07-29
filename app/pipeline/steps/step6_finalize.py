from __future__ import annotations

import logging

from app.finalize import get_finalize_service
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus

logger = logging.getLogger(__name__)


def run(ctx: PipelineContext) -> StepResult:
    """
    Step 8: Move qualified leads to a dated Finalised sheet and clear Dynamic Lead Sheet.

    Sheet name format: DD/MM Finalised leads (e.g. 25/07 Finalised leads).
    Same-day reruns use (1), (2), etc.
    """
    service = get_finalize_service(ctx.settings)
    ctx.add_log("Step 8: finalizing qualified leads to dated sheet")

    result = service.run()
    if not result.ok:
        ctx.add_log(f"Step 7 failed: {result.message}")
        return StepResult(
            status=StepStatus.FAILED,
            message=result.message,
            stats=result.stats,
        )

    ctx.add_log(f"Step 8: {result.message}")
    if result.stats.get("qualified", 0) == 0:
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
