"""Tests for scrapesheet status review and Step 4 Telegram hooks."""

from unittest.mock import MagicMock, patch

from app.scrape_queue.service import ScrapeQueueService


def test_get_scrapesheet_status_counts_links_and_paste():
    service = ScrapeQueueService.__new__(ScrapeQueueService)
    service.settings = MagicMock()
    service.settings.sheet_scrape_queue = "scrapesheet"
    service.settings.scrape_min_length = 50

    with patch("app.scrape_queue.service.sheets") as mock_sheets:
        mock_sheets.read_scrapesheet_rows.return_value = [
            (2, "https://facebook.com/a", ""),
            (3, "https://facebook.com/b", "x" * 60),
            (4, "", "orphan paste"),
        ]
        status = service.get_scrapesheet_status()

    assert status["rows_with_link"] == 2
    assert status["rows_with_paste"] == 1
    assert status["rows_need_scraping"] == 1


def test_clear_scrapesheet_after_batch():
    service = ScrapeQueueService.__new__(ScrapeQueueService)
    service.settings = MagicMock()
    service.settings.sheet_scrape_queue = "scrapesheet"
    service._state = MagicMock()
    service._handled = MagicMock()
    service._reset_tick_cache = MagicMock()

    with patch("app.scrape_queue.service.sheets") as mock_sheets:
        service.clear_scrapesheet_after_batch()
        mock_sheets.reset_scrapesheet_links.assert_called_once_with("scrapesheet", [])

    service._state.clear.assert_called_once()
    service._handled.clear.assert_called_once()


def test_notify_step4_scrape_calls_send():
    from app.notifications.telegram import notify_step4_scrape

    with patch("app.notifications.telegram.send_telegram", return_value={"ok": True}) as send:
        result = notify_step4_scrape(
            "Waiting for scraping to start",
            "24 lead(s) loaded on scrapesheet.",
        )
    assert result["ok"]
    send.assert_called_once()
    assert "Waiting for scraping to start" in send.call_args[0][0]
