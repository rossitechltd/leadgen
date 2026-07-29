"""Tests for scrape paste mistake alerts (disabled)."""

from app.scrape_queue.mistake_alerts import (
    _dedupe_key,
    _sent_keys,
    alert_from_ownership,
    notify_scrape_paste_mistake,
    resolve_mistake_alerts,
)
from app.scrape_queue.ownership import PasteOwnershipStatus, PasteOwnerResult


def test_mistake_alerts_do_not_send_telegram():
    result = notify_scrape_paste_mistake(
        source_row=5,
        business_name="Test Biz",
        link="https://facebook.com/test",
        code="stall_failed",
        detail="No valid paste saved after 10 polls",
        paste_chars=1200,
    )
    assert result is None


def test_alert_from_ownership_is_no_op():
    alert_from_ownership(
        source_row=5,
        business_name="Test Biz",
        link="https://facebook.com/test",
        ownership=PasteOwnerResult(
            status=PasteOwnershipStatus.WRONG_LEAD,
            wrong_lead_row=8,
            wrong_lead_name="Other Biz",
        ),
        paste_chars=1200,
    )


def test_resolve_mistake_alerts_clears_dedupe_keys():
    _sent_keys.clear()
    _sent_keys[_dedupe_key(5, "wrong_lead")] = 1.0
    resolve_mistake_alerts(5)
    assert _dedupe_key(5, "wrong_lead") not in _sent_keys
