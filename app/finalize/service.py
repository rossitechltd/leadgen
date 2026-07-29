"""Step 6 — move qualified leads to a dated Finalised sheet and clear Dynamic Lead."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sheets
from app.config import Settings
from app.sheets.columns import COL_VA, DYNAMIC_LEAD_HEADERS

logger = logging.getLogger(__name__)

_FINALISED_SUFFIX_RE = re.compile(r"^(.+ Finalised leads)(?: \((\d+)\))?$")


@dataclass
class FinalizeResult:
    ok: bool
    message: str
    stats: dict[str, Any]


def _is_qualified(row: dict[str, Any]) -> bool:
    return str(row.get(COL_VA) or "").strip().lower() == "qualified"


def _row_to_values(row: dict[str, Any]) -> list[str]:
    return [str(row.get(header) or "") for header in DYNAMIC_LEAD_HEADERS]


def allocate_finalised_sheet_name(existing_titles: list[str], *, now: datetime | None = None) -> str:
    """e.g. 25/07 Finalised leads, or 25/07 Finalised leads (1) on repeat same day."""
    stamp = (now or datetime.now()).strftime("%d/%m")
    base = f"{stamp} Finalised leads"
    if base not in existing_titles:
        return base

    used_suffixes: set[int] = set()
    for title in existing_titles:
        match = _FINALISED_SUFFIX_RE.match(title)
        if not match or match.group(1) != base:
            continue
        suffix = match.group(2)
        if suffix is None:
            used_suffixes.add(0)
        else:
            used_suffixes.add(int(suffix))

    n = 1
    while n in used_suffixes:
        n += 1
    return f"{base} ({n})"


class FinalizeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self) -> FinalizeResult:
        if not self.settings.sheets_configured:
            return FinalizeResult(
                ok=False,
                message="Service account JSON not found",
                stats={},
            )

        dynamic_sheet = self.settings.sheet_dynamic_lead
        stats: dict[str, Any] = {
            "dynamic_rows": 0,
            "qualified": 0,
            "moved": 0,
            "cleared_rows": 0,
            "destination_sheet": "",
        }

        try:
            dynamic_rows = sheets.read_all_with_row_indices(dynamic_sheet)
        except sheets.SheetsError as exc:
            return FinalizeResult(ok=False, message=str(exc), stats=stats)

        stats["dynamic_rows"] = len(dynamic_rows)
        qualified_rows = [row for _, row in dynamic_rows if _is_qualified(row)]
        stats["qualified"] = len(qualified_rows)

        if not qualified_rows:
            return FinalizeResult(
                ok=True,
                message="No qualified leads on Dynamic Lead Sheet",
                stats=stats,
            )

        try:
            sheet_name = allocate_finalised_sheet_name(sheets.list_worksheet_titles())
            sheets.ensure_worksheet(sheet_name, DYNAMIC_LEAD_HEADERS)
            row_values = [_row_to_values(row) for row in qualified_rows]
            sheets.append_rows(sheet_name, row_values)
            stats["moved"] = len(row_values)
            stats["destination_sheet"] = sheet_name
            stats["cleared_rows"] = sheets.clear_worksheet_data(dynamic_sheet)
        except sheets.SheetsError as exc:
            logger.exception("Finalize failed")
            return FinalizeResult(ok=False, message=str(exc), stats=stats)

        message = (
            f"Moved {stats['moved']} qualified lead(s) to '{sheet_name}' "
            f"and cleared Dynamic Lead Sheet"
        )
        return FinalizeResult(ok=True, message=message, stats=stats)


def get_finalize_service(settings: Settings | None = None) -> FinalizeService:
    from app.config import get_settings

    return FinalizeService(settings or get_settings())
