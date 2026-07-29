"""Tests for scrapesheet gating outside Step 4."""

import threading
from unittest.mock import MagicMock, patch

from app.scrape_queue.service import ScrapeQueueService


def _minimal_service() -> ScrapeQueueService:
    service = ScrapeQueueService.__new__(ScrapeQueueService)
    service.settings = MagicMock()
    service.settings.sheet_scrape_queue = "scrapesheet"
    service.settings.sheet_dynamic_lead = "Dynamic Lead Sheet"
    service._tick_lock = threading.Lock()
    service._tick_lead_index = None
    service._state = MagicMock()
    service._state.load.return_value = None
    service._reset_tick_cache = MagicMock()
    service._handled = MagicMock()
    service.queue_is_idle = MagicMock(return_value=True)
    service._read_queue_row = MagicMock(return_value={})
    return service


def test_tick_clears_scrapesheet_when_step4_not_active():
    service = _minimal_service()
    with patch.object(service, "_scrapesheet_session_active", return_value=False):
        with patch("app.scrape_queue.service.sheets") as mock_sheets:
            mock_sheets.read_scrapesheet_rows.return_value = [
                (2, "https://facebook.com/stale", ""),
            ]
            service._clear_scrapesheet_links = MagicMock()
            result = service.tick()

    service._clear_scrapesheet_links.assert_called_once()
    assert result["finalize"].action == "idle"
    assert result["enqueue"] is None


def test_start_lead_skipped_outside_step4_session():
    service = _minimal_service()
    with patch.object(service, "_scrapesheet_session_active", return_value=False):
        result = service._start_lead(require_idle=True)

    assert result.message == "Scrapesheet idle until Step 4 runs"
    assert result.source_row is None


def test_sync_scrapesheet_pastes_saves_off_row_paste():
    service = _minimal_service()
    service.settings.scrape_min_length = 50
    service._paste_is_ready = MagicMock(return_value=True)
    service._find_any_row_by_link = MagicMock(
        return_value=(
            5,
            {"Business Name": "SoundWave Radio Plymouth", "Facebook Link": "x"},
        )
    )
    service._scrape_len_for_row = MagicMock(return_value=0)
    service._sanitize_scrape_text = MagicMock(return_value="soundwave radio plymouth " + "x" * 60)
    service._write_scrape_to_dynamic_lead = MagicMock(return_value=120)
    service._reset_tick_cache = MagicMock()

    with patch("app.scrape_queue.service.evaluate_paste_for_intended") as eval_paste:
        with patch("app.scrape_queue.service.paste_belongs_to_intended", return_value=True):
            with patch("app.scrape_queue.service.sheets") as mock_sheets:
                mock_sheets.read_scrapesheet_rows.return_value = [
                    (2, "https://facebook.com/a", ""),
                    (4, "https://facebook.com/soundwave", "paste " + "y" * 80),
                ]
                count = service._sync_scrapesheet_pastes_to_leads()

    assert count == 1
    service._write_scrape_to_dynamic_lead.assert_called_once()
    _, kwargs = service._write_scrape_to_dynamic_lead.call_args
    assert kwargs.get("ownership_verified") is True
    mock_sheets.clear_scrape_queue_data.assert_called_once_with("scrapesheet", 4)
