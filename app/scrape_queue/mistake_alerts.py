"""Scrape paste mistake alerts — disabled (no Telegram / dashboard attention items)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.operator_attention import remove_attention_item
from app.scrape_queue.ownership import PasteOwnerResult

logger = logging.getLogger(__name__)

KIND = "scrape_paste_mistake"
COOLDOWN_SECS = 600

_lock = threading.Lock()
_sent_keys: dict[str, float] = {}


def _mistake_item_id(source_row: int, code: str) -> str:
    return f"scrape-mistake-{code}-{source_row}"


def _dedupe_key(source_row: int, code: str) -> str:
    return f"{code}:{source_row}"


def resolve_mistake_alerts(source_row: int) -> None:
    """Clear any legacy mistake attention items for this lead."""
    for code in ("wrong_lead", "not_saved", "stall_failed", "unmatched"):
        remove_attention_item(_mistake_item_id(source_row, code))
    with _lock:
        for code in ("wrong_lead", "not_saved", "stall_failed", "unmatched"):
            _sent_keys.pop(_dedupe_key(source_row, code), None)


def notify_scrape_paste_mistake(
    *,
    source_row: int,
    business_name: str,
    link: str,
    code: str,
    detail: str,
    ownership: PasteOwnerResult | None = None,
    paste_chars: int = 0,
) -> dict[str, Any] | None:
    """Scrape paste mistake alerts are disabled — log only, no Telegram or attention queue."""
    logger.debug(
        "[scrape-mistake] suppressed %s row %s (%s): %s",
        code,
        source_row,
        business_name,
        detail,
    )
    return None


def alert_from_ownership(
    *,
    source_row: int,
    business_name: str,
    link: str,
    ownership: PasteOwnerResult,
    paste_chars: int,
) -> None:
    """Scrape paste mistake alerts are disabled."""
    return None
