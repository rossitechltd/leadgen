"""Tests for deferred entity screen finalize (single sheet write burst)."""

from unittest.mock import MagicMock, patch

import pytest

from app.entity.constants import (
    LEAD_ACTIVITY_ENTITY_BUSINESS,
    LEAD_ACTIVITY_ENTITY_UNCERTAIN,
    LEAD_ACTIVITY_PENDING_SCRAPE,
)
from app.entity.screen import EntityScreenService
from app.scrapers.lead_mapping import normalize_facebook_url


@pytest.fixture
def service():
    settings = MagicMock()
    settings.sheets_configured = True
    settings.openrouter_configured = True
    settings.sheet_dynamic_lead = "Dynamic Lead Sheet"
    settings.entity_screen_auto_person = 0.88
    settings.entity_classify_batch_size = 10
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "test-model"
    settings.openrouter_base_url = "https://openrouter.ai/api/v1"
    settings.scrape_state_path = MagicMock()
    settings.scrape_state_path.parent = MagicMock()
    return EntityScreenService(settings)


def test_apply_sheet_results_resolves_deletes_by_link(service):
    person_link = "https://www.facebook.com/profile.php?id=999"
    norm = normalize_facebook_url(person_link)
    initial_rows = [
        (
            10,
            {
                "Facebook Link": person_link,
                "Business Name": "John Smith",
                "Lead Activity": LEAD_ACTIVITY_PENDING_SCRAPE,
            },
        ),
        (
            11,
            {
                "Facebook Link": "https://www.facebook.com/bizpage",
                "Business Name": "Biz Co",
                "Lead Activity": LEAD_ACTIVITY_PENDING_SCRAPE,
            },
        ),
    ]
    pending_tags = {11: LEAD_ACTIVITY_ENTITY_BUSINESS}
    to_delete_links = {norm}
    stats = {
        "reconciled_uncertain": 0,
        "tagged_uncertain": 0,
    }
    fresh_rows = list(initial_rows)

    with (
        patch.object(service, "_apply_activity_tags") as apply_tags,
        patch.object(service, "_delete_person_rows") as delete_rows,
        patch.object(service, "_sweep_remaining_stragglers"),
        patch(
            "sheets.read_rows_with_sheet_indices",
            return_value=fresh_rows,
        ),
        patch("sheets.invalidate_worksheet_cache"),
    ):
        service._apply_sheet_results(
            "Dynamic Lead Sheet",
            initial_rows,
            stats,
            pending_tags,
            to_delete_links,
        )

    apply_tags.assert_called_once()
    assert apply_tags.call_args[0][1] == {11: LEAD_ACTIVITY_ENTITY_BUSINESS}
    delete_rows.assert_called_once()
    assert delete_rows.call_args[0][1] == [10]
    assert delete_rows.call_args[1]["max_row"] == 11


def test_classification_does_not_write_tags_during_ai(service):
    rows = [
        (
            2,
            {
                "Facebook Link": "https://www.facebook.com/ambiguouspage",
                "Business Name": "Sarah Mitchell",
                "Lead Activity": LEAD_ACTIVITY_PENDING_SCRAPE,
            },
        ),
    ]
    work_items = list(rows)
    stats = {
        "screened": 1,
        "removed_heuristic": 0,
        "removed_ai": 0,
        "tagged_business": 0,
        "tagged_uncertain": 0,
        "reconciled_uncertain": 0,
        "errors": 0,
        "total": 1,
        "processed": 0,
        "current_row": 2,
        "position": 0,
    }
    pending_tags: dict[int, str] = {}
    to_delete_links: set[str] = set()

    mock_result = MagicMock()
    mock_result.row_index = 2
    mock_result.entity_type = "business"
    mock_result.confidence = 0.9
    mock_result.reason = "business name"

    with (
        patch.object(service, "_apply_activity_tags") as apply_tags,
        patch(
            "app.entity.screen.classify_entities_batch",
            return_value=[mock_result],
        ),
    ):
        err = service._run_classification(
            "Dynamic Lead Sheet",
            rows,
            work_items,
            stats,
            pending_tags,
            to_delete_links,
            None,
        )

    assert err is None
    apply_tags.assert_not_called()
    assert pending_tags == {2: LEAD_ACTIVITY_ENTITY_BUSINESS}
    assert stats["processed"] == 1


def test_checkpoint_resume_skips_classification(service, tmp_path):
    service.settings.scrape_state_path = tmp_path / "active.json"
    checkpoint = {
        "sheet_name": "Dynamic Lead Sheet",
        "pending_tags": {"3": LEAD_ACTIVITY_ENTITY_UNCERTAIN},
        "to_delete_links": [
            normalize_facebook_url("https://www.facebook.com/profile.php?id=1"),
        ],
        "stats": {
            "screened": 1,
            "removed_heuristic": 1,
            "removed_ai": 0,
            "tagged_business": 0,
            "tagged_uncertain": 0,
            "reconciled_uncertain": 0,
            "errors": 0,
            "total": 1,
            "processed": 1,
            "current_row": 2,
            "position": 1,
        },
    }
    service._save_checkpoint(
        "Dynamic Lead Sheet",
        {3: LEAD_ACTIVITY_ENTITY_UNCERTAIN},
        {normalize_facebook_url("https://www.facebook.com/profile.php?id=1")},
        checkpoint["stats"],
    )

    rows = [
        (
            2,
            {
                "Facebook Link": "https://www.facebook.com/profile.php?id=1",
                "Business Name": "Person",
                "Lead Activity": LEAD_ACTIVITY_PENDING_SCRAPE,
            },
        ),
    ]

    with (
        patch("sheets.ensure_worksheet"),
        patch(
            "sheets.read_rows_with_sheet_indices",
            return_value=rows,
        ),
        patch.object(service, "_apply_sheet_results") as apply_results,
        patch.object(service, "_run_classification") as run_classify,
    ):
        result = service.run()

    run_classify.assert_not_called()
    apply_results.assert_called_once()
    assert result.ok is True
    assert not service._checkpoint_path().exists()


def test_sweep_remaining_stragglers_tags_pending_scrape(service):
    rows = [
        (
            5,
            {
                "Facebook Link": "https://www.facebook.com/somebizpage",
                "Business Name": "Ambiguous Co",
                "Lead Activity": LEAD_ACTIVITY_PENDING_SCRAPE,
            },
        ),
    ]
    stats = {
        "removed_heuristic": 0,
        "tagged_business": 0,
        "tagged_uncertain": 0,
        "removed_sweep": 0,
    }

    with (
        patch.object(service, "_with_quota_retry", return_value=rows),
        patch.object(service, "_apply_activity_tags") as apply_tags,
        patch.object(service, "_delete_person_rows") as delete_rows,
    ):
        service._sweep_remaining_stragglers("Dynamic Lead Sheet", stats)

    apply_tags.assert_called_once()
    assert apply_tags.call_args[0][1] == {5: LEAD_ACTIVITY_ENTITY_UNCERTAIN}
    delete_rows.assert_not_called()
    assert stats["tagged_uncertain"] == 1


def test_sweep_remaining_stragglers_deletes_stale_person_pending_scrape(service):
    rows = [
        (
            8,
            {
                "Facebook Link": "https://www.facebook.com/profile.php?id=123",
                "Business Name": "Jane Doe",
                "Lead Activity": LEAD_ACTIVITY_PENDING_SCRAPE,
            },
        ),
    ]
    stats = {
        "removed_heuristic": 0,
        "tagged_business": 0,
        "tagged_uncertain": 0,
        "removed_sweep": 0,
    }

    with (
        patch.object(service, "_with_quota_retry", return_value=rows),
        patch.object(service, "_apply_activity_tags") as apply_tags,
        patch.object(service, "_delete_person_rows") as delete_rows,
    ):
        service._sweep_remaining_stragglers("Dynamic Lead Sheet", stats)

    delete_rows.assert_called_once()
    assert delete_rows.call_args[0][1] == [8]
    apply_tags.assert_not_called()
    assert stats["removed_sweep"] == 1
