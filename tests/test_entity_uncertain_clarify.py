"""Tests for entity_uncertain clarify service."""

from unittest.mock import MagicMock, patch

from app.entity.classifier_batch import EntityClassifyResult
from app.entity.constants import (
    LEAD_ACTIVITY_ENTITY_BUSINESS,
    LEAD_ACTIVITY_ENTITY_UNCERTAIN,
)
from app.entity.uncertain_clarify_service import EntityUncertainClarifyService


def _service() -> EntityUncertainClarifyService:
    settings = MagicMock()
    settings.sheets_configured = True
    settings.openrouter_configured = True
    settings.sheet_dynamic_lead = "Dynamic Lead Sheet"
    settings.entity_classify_batch_size = 10
    settings.entity_classify_auto_person = 0.8
    settings.openrouter_api_key = "key"
    settings.openrouter_model = "model"
    settings.openrouter_base_url = "https://example.com"
    return EntityUncertainClarifyService(settings)


def test_clarify_only_entity_uncertain_rows():
    service = _service()
    rows = [
        (
            2,
            {
                "Facebook Link": "https://facebook.com/a",
                "Business Name": "Biz A",
                "Lead Activity": LEAD_ACTIVITY_ENTITY_UNCERTAIN,
                "Scrape": "x" * 80,
                "refined": '{"phone": "1"}',
            },
        ),
        (
            3,
            {
                "Facebook Link": "https://facebook.com/b",
                "Business Name": "Biz B",
                "Lead Activity": "entity_business",
                "Scrape": "y" * 80,
                "refined": "{}",
            },
        ),
    ]

    with patch("app.entity.uncertain_clarify_service.sheets") as mock_sheets:
        mock_sheets.read_all_with_row_indices.return_value = rows
        mock_sheets.ensure_worksheet.return_value = None
        with patch(
            "app.entity.uncertain_clarify_service.classify_entities_batch",
            return_value=[
                EntityClassifyResult(
                    row_index=2,
                    entity_type="business",
                    confidence=0.9,
                    reason="Commercial page",
                )
            ],
        ):
            result = service.run()

    assert result.ok
    assert result.stats["candidates"] == 1
    assert result.stats["tagged_business"] == 1
    assert result.stats["skipped_not_uncertain"] == 1
    mock_sheets.batch_update_lead_activity.assert_called_once_with(
        "Dynamic Lead Sheet",
        {2: LEAD_ACTIVITY_ENTITY_BUSINESS},
    )
