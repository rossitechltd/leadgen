"""Scrape Queue — one lead at row 2 on scrapesheet (link + data)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import sheets
from app.config import Settings, get_settings
from app.scrapers.lead_mapping import normalize_facebook_url
from app.scrape_queue.state import ActiveScrapeState, get_state_store
from app.scrape_queue.verify import verify_scrape_text
from app.sheets.columns import (
    COL_FACEBOOK_LINK,
    COL_LEAD_ACTIVITY,
    COL_SCRAPE,
    COL_SCRAPE_DATA,
    COL_SCRAPE_LINK,
    LEAD_ACTIVITY_FAILED,
    LEAD_ACTIVITY_PENDING,
    LEAD_ACTIVITY_SCRAPED,
    LEAD_ACTIVITY_SCRAPING,
    SCRAPE_SHEET_HEADERS,
    SCRAPE_SHEET_ROW,
)

logger = logging.getLogger(__name__)


@dataclass
class EnqueueResult:
    ok: bool
    message: str
    source_row: int | None = None
    link: str | None = None


@dataclass
class FinalizeResult:
    ok: bool
    message: str
    action: str = "none"
    source_row: int | None = None
    stats: dict[str, Any] = field(default_factory=dict)


class ScrapeQueueService:
    """Manages scrapesheet row 2 and write-back to Dynamic Lead Sheet."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._state = get_state_store(self.settings)

    def ensure_queue_sheet(self) -> None:
        sheets.ensure_worksheet(self.settings.sheet_scrape_queue, SCRAPE_SHEET_HEADERS)

    def get_status(self) -> dict[str, Any]:
        pending = self.count_pending()
        queue_row = self._read_queue_row()
        idle = self.queue_is_idle(queue_row)
        active = self._state.load()
        return {
            "pending": pending,
            "queue_idle": idle,
            "queue_row": queue_row if not idle else None,
            "active_state": active.__dict__ if active else None,
            "sheet_scrape_queue": self.settings.sheet_scrape_queue,
            "sheet_dynamic_lead": self.settings.sheet_dynamic_lead,
        }

    def count_pending(self) -> int:
        try:
            rows = sheets.read_all_with_row_indices(self.settings.sheet_dynamic_lead)
        except sheets.SheetsError:
            return 0
        return sum(1 for _row_index, row in rows if self._row_needs_scrape(row))

    def queue_is_idle(self, queue_row: dict[str, Any] | None = None) -> bool:
        row = queue_row if queue_row is not None else self._read_queue_row()
        return not self._field(row, COL_SCRAPE_LINK)

    def enqueue_next_lead(self) -> EnqueueResult:
        self.ensure_queue_sheet()
        queue_row = self._read_queue_row()
        if not self.queue_is_idle(queue_row):
            active = self._state.load()
            return EnqueueResult(
                ok=True,
                message="scrapesheet row 2 already has a link",
                source_row=active.source_row if active else None,
                link=self._field(queue_row, COL_SCRAPE_LINK) or None,
            )

        pending = self._find_next_pending()
        if not pending:
            return EnqueueResult(ok=True, message="No pending leads to enqueue")

        source_row, lead = pending
        link = str(lead.get(COL_FACEBOOK_LINK) or "").strip()
        sheets.set_row(
            self.settings.sheet_scrape_queue,
            SCRAPE_SHEET_ROW,
            [link, ""],
        )
        sheets.update_row_by_header(
            self.settings.sheet_dynamic_lead,
            source_row,
            {COL_LEAD_ACTIVITY: LEAD_ACTIVITY_SCRAPING},
        )
        self._state.save(ActiveScrapeState(source_row=source_row, attempt=1, link=link))
        label = lead.get("Business Name") or link or source_row
        logger.info("Enqueued row %s (%s) to scrapesheet", source_row, label)
        return EnqueueResult(
            ok=True,
            message=f"Enqueued row {source_row} on scrapesheet",
            source_row=source_row,
            link=link,
        )

    def finalize_if_ready(self) -> FinalizeResult:
        self.ensure_queue_sheet()
        queue_row = self._read_queue_row()
        if self.queue_is_idle(queue_row):
            self._state.clear()
            return FinalizeResult(ok=True, message="scrapesheet idle", action="idle")

        scrape_text = self._field(queue_row, COL_SCRAPE_DATA)
        if not scrape_text:
            return FinalizeResult(ok=True, message="Waiting for scrape data", action="waiting")

        active = self._resolve_active(queue_row)
        if active is None:
            link = self._field(queue_row, COL_SCRAPE_LINK)
            return FinalizeResult(
                ok=False,
                message=(
                    f"Scrape data present but no matching Dynamic Lead row for link: {link}"
                ),
                action="error",
            )

        verify = verify_scrape_text(
            scrape_text, min_length=self.settings.scrape_min_length
        )

        if verify.ok:
            return self._finalize_success(active, scrape_text)

        if active.attempt < self.settings.scrape_max_attempts:
            return self._finalize_retry(active, verify.reason)

        return self._finalize_failed(active, verify.reason)

    def tick(self) -> dict[str, Any]:
        """Worker tick: finalize when data appears, enqueue when row 2 is empty."""
        finalize = self.finalize_if_ready()
        enqueue = None
        if finalize.action in {"success", "failed"}:
            time.sleep(self.settings.scrape_queue_delay_secs)
            enqueue = self.enqueue_next_lead()
        elif finalize.action in {"idle", "waiting", "none"} and self.queue_is_idle():
            enqueue = self.enqueue_next_lead()
        elif finalize.action == "error":
            logger.error("Finalize stuck: %s", finalize.message)
        return {"finalize": finalize, "enqueue": enqueue}

    def _resolve_active(self, queue_row: dict[str, Any]) -> ActiveScrapeState | None:
        """Find Dynamic Lead row from local state or by matching link in the sheet."""
        link = self._field(queue_row, COL_SCRAPE_LINK)
        if not link:
            return None

        stored = self._state.load()
        if stored and normalize_facebook_url(stored.link) == normalize_facebook_url(link):
            return stored

        source_row = self._find_source_row_by_link(link)
        if source_row is None:
            return None

        attempt = stored.attempt if stored else 1
        state = ActiveScrapeState(source_row=source_row, attempt=attempt, link=link)
        self._state.save(state)
        return state

    def _find_source_row_by_link(self, link: str) -> int | None:
        target = normalize_facebook_url(link)
        scraping_row: int | None = None
        pending_row: int | None = None

        for row_index, row in sheets.read_all_with_row_indices(
            self.settings.sheet_dynamic_lead
        ):
            row_link = normalize_facebook_url(str(row.get(COL_FACEBOOK_LINK) or ""))
            if row_link != target:
                continue
            activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
            if activity == LEAD_ACTIVITY_SCRAPING:
                scraping_row = row_index
            elif self._row_needs_scrape(row):
                pending_row = row_index

        return scraping_row or pending_row

    @staticmethod
    def _field(row: dict[str, Any], name: str) -> str:
        """Read a column case-insensitively (handles link vs Link)."""
        target = name.strip().lower()
        for key, value in row.items():
            if str(key).strip().lower() == target:
                return str(value or "").strip()
        return str(row.get(name) or "").strip()

    def _finalize_success(
        self, active: ActiveScrapeState, scrape_text: str
    ) -> FinalizeResult:
        sheets.update_row_by_header(
            self.settings.sheet_dynamic_lead,
            active.source_row,
            {
                COL_SCRAPE: scrape_text,
                COL_LEAD_ACTIVITY: LEAD_ACTIVITY_SCRAPED,
            },
        )
        self._clear_queue_row()
        logger.info("Scrape saved for Dynamic Lead row %s", active.source_row)
        return FinalizeResult(
            ok=True,
            message=f"Scrape saved for row {active.source_row}",
            action="success",
            source_row=active.source_row,
            stats={"attempt": active.attempt},
        )

    def _finalize_retry(self, active: ActiveScrapeState, reason: str) -> FinalizeResult:
        next_attempt = active.attempt + 1
        sheets.update_row_by_header(
            self.settings.sheet_scrape_queue,
            SCRAPE_SHEET_ROW,
            {COL_SCRAPE_DATA: ""},
        )
        self._state.save(
            ActiveScrapeState(
                source_row=active.source_row,
                attempt=next_attempt,
                link=active.link,
            )
        )
        logger.warning(
            "Scrape failed for row %s (attempt %s): %s — retrying",
            active.source_row,
            active.attempt,
            reason,
        )
        return FinalizeResult(
            ok=True,
            message=f"Scrape failed ({reason}) — retry {next_attempt}",
            action="retry",
            source_row=active.source_row,
            stats={"attempt": next_attempt, "reason": reason},
        )

    def _finalize_failed(self, active: ActiveScrapeState, reason: str) -> FinalizeResult:
        sheets.update_row_by_header(
            self.settings.sheet_dynamic_lead,
            active.source_row,
            {COL_LEAD_ACTIVITY: LEAD_ACTIVITY_FAILED},
        )
        self._clear_queue_row()
        logger.error(
            "Scrape failed permanently for row %s: %s", active.source_row, reason
        )
        return FinalizeResult(
            ok=True,
            message=f"Marked row {active.source_row} as scrape_failed ({reason})",
            action="failed",
            source_row=active.source_row,
            stats={"reason": reason},
        )

    def _clear_queue_row(self) -> None:
        sheets.clear_row(self.settings.sheet_scrape_queue, SCRAPE_SHEET_ROW)
        self._state.clear()

    def _read_queue_row(self) -> dict[str, Any]:
        try:
            return sheets.read_row(self.settings.sheet_scrape_queue, SCRAPE_SHEET_ROW)
        except sheets.SheetsError:
            return {}

    def _find_next_pending(self) -> tuple[int, dict[str, Any]] | None:
        for row_index, row in sheets.read_all_with_row_indices(
            self.settings.sheet_dynamic_lead
        ):
            if self._row_needs_scrape(row):
                return row_index, row
        return None

    def _row_needs_scrape(self, row: dict[str, Any]) -> bool:
        if not str(row.get(COL_FACEBOOK_LINK) or "").strip():
            return False
        if self._scrape_filled(row):
            return False
        activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
        if activity in {LEAD_ACTIVITY_SCRAPED, LEAD_ACTIVITY_FAILED}:
            return False
        return activity in {"", LEAD_ACTIVITY_PENDING, LEAD_ACTIVITY_SCRAPING}

    def _scrape_filled(self, row: dict[str, Any]) -> bool:
        for key in (COL_SCRAPE, "Website Link Scrape"):
            if str(row.get(key) or "").strip():
                return True
        return False


_service: ScrapeQueueService | None = None


def get_scrape_queue() -> ScrapeQueueService:
    global _service
    if _service is None:
        _service = ScrapeQueueService()
    return _service
