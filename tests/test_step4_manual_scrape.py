"""Tests for manual-only Step 4 (page scrape note — not automated)."""

from unittest.mock import MagicMock

from app.pipeline.steps.base import PipelineContext, StepStatus
from app.pipeline.steps import step3_page_scrape


def test_step4_skipped_with_manual_note():
    ctx = PipelineContext(
        sheets_client=MagicMock(),
        settings=MagicMock(),
        abandon_checker=lambda: False,
    )
    result = step3_page_scrape.run(ctx)
    assert result.status == StepStatus.SKIPPED
    assert result.stats.get("manual_only") is True
    assert "Mini Mouse Macro" in result.message
