"""Tests for Step 4 batch paste vs saved progress."""

from unittest.mock import MagicMock, patch

from app.scrape_queue.service import ScrapeQueueService


def _service() -> ScrapeQueueService:
    service = ScrapeQueueService.__new__(ScrapeQueueService)
    service.settings = MagicMock()
    service.settings.sheet_scrape_queue = "scrapesheet"
    service.settings.scrape_min_length = 50
    service._state = MagicMock()
    service._state.load.return_value = None
    service._is_permanently_failed = MagicMock(return_value=False)
    service._row_has_scrape_data = MagicMock(side_effect=lambda n: n >= 50)
    service._row_is_active_scrape = MagicMock(return_value=False)
    service._tick_lead_index: list[tuple[int, str, str, str, int]] | None = None
    service._lead_index_rows = MagicMock(
        return_value=[
            (3, "https://facebook.com/a", "Biz A", "", 0),
            (4, "https://facebook.com/b", "Biz B", "", 100),
        ]
    )
    return service


def test_scrapesheet_batch_progress_counts_pasted_and_saved():
    service = _service()
    target = {3, 4}

    with patch("app.scrape_queue.service.sheets") as mock_sheets:
        mock_sheets.read_scrapesheet_rows.return_value = [
            (2, "https://facebook.com/a", "x" * 80),
            (3, "https://facebook.com/b", ""),
        ]
        service._find_any_row_by_link = MagicMock(
            side_effect=lambda link: (
                (3, {"Business Name": "Biz A"})
                if "a" in link
                else (4, {"Business Name": "Biz B"})
            )
        )
        result = service.get_scrapesheet_batch_progress(target)

    assert result["scraped"] == 1
    assert result["pasted"] == 1
    assert result["display_done"] == 1
