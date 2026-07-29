"""Scrape queue activity detection for adaptive polling."""

from __future__ import annotations

from app.config import get_settings


def scrape_queue_is_active() -> bool:
    """True when scrape state exists or scrapesheet row 2 has a link."""
    settings = get_settings()
    if settings.scrape_state_path.exists():
        return True
    try:
        import sheets

        link, _ = sheets.read_scrape_queue_row(
            settings.sheet_scrape_queue, 2, use_cache=True
        )
        return bool(link.strip())
    except Exception:
        return False
