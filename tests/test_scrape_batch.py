"""Tests for Step 4 link-only scrape batch collection."""

from unittest.mock import MagicMock, patch

from app.scrape_queue.service import ScrapeQueueService


def _service_with_rows(
    rows: list[tuple[int, str, str, str, int]],
) -> ScrapeQueueService:
    service = ScrapeQueueService.__new__(ScrapeQueueService)
    service.settings = MagicMock()
    service.settings.scrape_min_length = 50
    service.settings.scrape_max_failures = 3
    service._failures = MagicMock()
    service._failures.count.return_value = 0
    service._state = MagicMock()
    service._state.load.return_value = None
    service._lead_index_rows = MagicMock(return_value=rows)
    return service


def test_collect_scrape_batch_includes_empty_activity():
    service = _service_with_rows(
        [
            (2, "https://www.facebook.com/a", "Biz A", "", 0),
        ]
    )
    batch = service.collect_scrape_batch()
    assert len(batch) == 1
    assert batch[0][0] == 2


def test_collect_scrape_batch_includes_entity_business_with_existing_scrape():
    service = _service_with_rows(
        [
            (
                3,
                "https://www.facebook.com/b",
                "Biz B",
                "entity_business",
                200,
            ),
        ]
    )
    batch = service.collect_scrape_batch()
    assert len(batch) == 1
    assert batch[0][1].endswith("/b")


def test_collect_scrape_batch_includes_legacy_scrape_failed():
    service = _service_with_rows(
        [
            (
                4,
                "https://www.facebook.com/c",
                "Biz C",
                "scrape_failed_2",
                0,
            ),
        ]
    )
    batch = service.collect_scrape_batch()
    assert len(batch) == 1
    assert batch[0][0] == 4


def test_collect_scrape_batch_skips_rows_without_link():
    service = _service_with_rows(
        [
            (5, "", "No Link", "entity_business", 0),
            (6, "https://www.facebook.com/d", "Biz D", "entity_uncertain", 0),
        ]
    )
    batch = service.collect_scrape_batch()
    assert len(batch) == 1
    assert batch[0][0] == 6


def test_index_row_needs_scrape_ignores_lead_activity():
    service = _service_with_rows([])
    assert service._index_row_needs_scrape(
        2, "https://facebook.com/x", "random_tag", 0
    )
    assert not service._index_row_needs_scrape(
        2, "https://facebook.com/x", "random_tag", 100
    )


def test_populate_clears_scrape_column():
    service = ScrapeQueueService.__new__(ScrapeQueueService)
    service.settings = MagicMock()
    service.settings.sheet_scrape_queue = "scrapesheet"
    service.settings.sheet_dynamic_lead = "Dynamic Lead Sheet"
    service.settings.scrape_min_length = 50
    service.settings.scrape_max_failures = 3
    service._state = MagicMock()
    service._handled = MagicMock()
    service._failures = MagicMock()
    service._failures.clear_all = MagicMock()
    service._failures.count.return_value = 0
    service._reset_tick_cache = MagicMock()
    service.ensure_queue_sheet = MagicMock()
    service._start_lead = MagicMock(
        return_value=MagicMock(ok=True, message="ok", source_row=2, link="x")
    )
    service._lead_index_rows = MagicMock(
        return_value=[
            (2, "https://www.facebook.com/a", "A", "entity_business", 80),
        ]
    )

    with patch("app.scrape_queue.service.sheets") as mock_sheets:
        mock_sheets.reset_scrapesheet_links.return_value = 1
        result = service.populate_scrapesheet_queue()

    mock_sheets.reset_scrapesheet_links.assert_any_call("scrapesheet", [])
    mock_sheets.reset_scrapesheet_links.assert_any_call(
        "scrapesheet", ["https://www.facebook.com/a"]
    )
    mock_sheets.batch_clear_scrape_column.assert_called_once_with(
        "Dynamic Lead Sheet",
        [2],
    )
    assert result.count == 1
    assert result.target_rows == {2}
