"""Assign random outreach templates to Message1 for qualified leads."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import sheets
from app.config import Settings
from app.outreach.messages import build_outreach_message, resolve_first_name
from app.qualify.website_status import REMOVE_LEAD_STATUSES
from app.sheets.columns import (
    COL_BUSINESS_NAME,
    COL_MESSAGE_1,
    COL_REFINED,
    COL_SCRAPE,
    COL_VA,
    COL_WEBSITE_STATUS,
    DYNAMIC_LEAD_HEADERS,
)

logger = logging.getLogger(__name__)

_SHEET_FLUSH_ROWS = 25
_REMOVE_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in REMOVE_LEAD_STATUSES)


@dataclass
class OutreachResult:
    ok: bool
    message: str
    stats: dict[str, Any]


ProgressCallback = Callable[[dict[str, Any], str | None], None]


class OutreachMessageService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self,
        progress_callback: ProgressCallback | None = None,
    ) -> OutreachResult:
        if not self.settings.sheets_configured:
            return OutreachResult(ok=False, message="Service account JSON not found", stats={})

        sheet = self.settings.sheet_dynamic_lead
        sheets.ensure_worksheet(sheet, DYNAMIC_LEAD_HEADERS)
        sheets.extend_worksheet_headers(sheet, DYNAMIC_LEAD_HEADERS)
        sheets.invalidate_worksheet_cache(sheet)

        try:
            rows = sheets.read_rows_with_sheet_indices(sheet, use_cache=False)
        except sheets.SheetsError as exc:
            return OutreachResult(ok=False, message=str(exc), stats={})

        targets = [(idx, row) for idx, row in rows if self._is_outreach_target(row)]
        stats: dict[str, Any] = {
            "sheet_rows": len(rows),
            "targets": len(targets),
            "total": len(targets),
            "processed": 0,
            "updated": 0,
            "with_name": 0,
            "with_there": 0,
            "va_backfilled": 0,
            "sweep_filled": 0,
        }
        pending_updates: dict[int, dict[str, Any]] = {}

        def _flush() -> None:
            if not pending_updates:
                return
            sheets.batch_update_rows_by_header(sheet, pending_updates)
            pending_updates.clear()

        def _emit(message: str | None = None) -> None:
            if progress_callback is not None:
                progress_callback(dict(stats), message)

        if stats["total"]:
            _emit(f"Outreach messages — 0/{stats['total']} leads")

        try:
            for row_index, row in targets:
                self._queue_message_update(
                    row_index,
                    row,
                    pending_updates,
                    stats,
                    count_sweep=False,
                )
                stats["processed"] += 1

                if len(pending_updates) >= _SHEET_FLUSH_ROWS:
                    _flush()

                _emit(
                    f"Outreach messages — {stats['processed']}/{stats['total']} "
                    f"({stats['updated']} written)"
                )

            _flush()
            sweep_filled = self._sweep_missing_messages(sheet, stats)
            if sweep_filled:
                stats["sweep_filled"] = sweep_filled
        except sheets.SheetsError as exc:
            logger.warning("Outreach messages paused on Sheets error: %s", exc)
            try:
                _flush()
                sweep_filled = self._sweep_missing_messages(sheet, stats)
                if sweep_filled:
                    stats["sweep_filled"] = sweep_filled
            except sheets.SheetsError as cleanup_exc:
                logger.warning("Outreach sweep after error also failed: %s", cleanup_exc)
            return OutreachResult(ok=False, message=str(exc), stats=stats)

        if not targets:
            return OutreachResult(
                ok=True,
                message="No leads to assign outreach messages",
                stats=stats,
            )

        message = (
            f"Assigned outreach Message1 to {stats['updated']} lead(s) on {sheet}"
        )
        if stats.get("sweep_filled"):
            message += f" ({stats['sweep_filled']} filled in final sweep)"
        return OutreachResult(ok=True, message=message, stats=stats)

    def _is_outreach_target(self, row: dict[str, Any]) -> bool:
        """Surviving scraped leads — not only rows where va was written."""
        scrape = str(row.get(COL_SCRAPE) or "").strip()
        refined = str(row.get(COL_REFINED) or "").strip()
        if not scrape and not refined:
            return False
        status = str(row.get(COL_WEBSITE_STATUS) or "").strip().upper()
        if status in _REMOVE_STATUS_VALUES:
            return False
        return True

    def _queue_message_update(
        self,
        row_index: int,
        row: dict[str, Any],
        pending_updates: dict[int, dict[str, Any]],
        stats: dict[str, Any],
        *,
        count_sweep: bool,
    ) -> None:
        business_name = str(row.get(COL_BUSINESS_NAME) or "").strip()
        first_name = resolve_first_name(row, business_name=business_name)
        message = build_outreach_message(first_name)
        updates: dict[str, Any] = {COL_MESSAGE_1: message}
        if not str(row.get(COL_VA) or "").strip():
            updates[COL_VA] = "qualified"
            stats["va_backfilled"] += 1
        pending_updates[row_index] = updates
        stats["updated"] += 1
        if count_sweep:
            stats["sweep_filled"] += 1
        if first_name == "there":
            stats["with_there"] += 1
        else:
            stats["with_name"] += 1

    def _sweep_missing_messages(self, sheet: str, stats: dict[str, Any]) -> int:
        """Fill Message1 on any outreach target still missing a message."""
        sheets.invalidate_worksheet_cache(sheet)
        rows = sheets.read_rows_with_sheet_indices(sheet, use_cache=False)
        pending: dict[int, dict[str, Any]] = {}
        swept = 0
        for row_index, row in rows:
            if not self._is_outreach_target(row):
                continue
            if str(row.get(COL_MESSAGE_1) or "").strip():
                continue
            self._queue_message_update(
                row_index,
                row,
                pending,
                stats,
                count_sweep=True,
            )
            swept += 1
            if len(pending) >= _SHEET_FLUSH_ROWS:
                sheets.batch_update_rows_by_header(sheet, pending)
                pending.clear()
        if pending:
            sheets.batch_update_rows_by_header(sheet, pending)
        if swept:
            logger.info("Outreach final sweep filled %d missing Message1 row(s)", swept)
        return swept


def get_outreach_message_service(settings: Settings | None = None) -> OutreachMessageService:
    from app.config import get_settings

    return OutreachMessageService(settings or get_settings())
