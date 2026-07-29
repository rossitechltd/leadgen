from __future__ import annotations

import logging

from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus

logger = logging.getLogger(__name__)


def run(ctx: PipelineContext) -> StepResult:
    """
    Step 4: Manual page scrape only — not automated in this app.

    Users scrape with Mini Mouse Macro and paste into the sheet themselves.
    """
    ctx.add_log(
        "Step 4: scrape manually outside this app (MMM + paste into sheet)"
    )
    return StepResult(
        status=StepStatus.SKIPPED,
        message="Scrape manually with Mini Mouse Macro and paste into your sheet",
        stats={"manual_only": True},
    )
