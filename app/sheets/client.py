"""Thin facade over the shared root sheets module."""

from __future__ import annotations

from typing import Any

import sheets as shared_sheets

from app.config import Settings, get_settings
from app.sheets.columns import DYNAMIC_LEAD_HEADERS, READY_TO_CONTACT_HEADERS

SheetsError = shared_sheets.SheetsError


class SheetsClient:
    """Pipeline-facing wrapper; all auth lives in project-root sheets.py."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def get_all_records(self, worksheet_name: str) -> list[dict[str, Any]]:
        return shared_sheets.read_all(worksheet_name)

    def append_row(self, worksheet_name: str, values: list[Any]) -> None:
        shared_sheets.append_row(worksheet_name, values)

    def append_rows(self, worksheet_name: str, rows: list[list[Any]]) -> None:
        shared_sheets.append_rows(worksheet_name, rows)

    def update_cell(self, worksheet_name: str, row: int, col: int, value: Any) -> None:
        shared_sheets.update_cell(worksheet_name, row, col, value)

    def update_row_by_header(
        self, worksheet_name: str, row_index: int, updates: dict[str, Any]
    ) -> None:
        shared_sheets.update_row_by_header(worksheet_name, row_index, updates)

    def delete_rows(self, worksheet_name: str, row_indices: list[int]) -> None:
        shared_sheets.delete_rows(worksheet_name, row_indices)

    def get_dynamic_lead_sheet(self):
        return shared_sheets.ensure_worksheet(
            self.settings.sheet_dynamic_lead, DYNAMIC_LEAD_HEADERS
        )

    def get_all_imported_sheet(self):
        return shared_sheets.get_worksheet(self.settings.sheet_all_imported)

    def get_ready_to_contact_sheet(self):
        return shared_sheets.ensure_worksheet(
            self.settings.sheet_ready_to_contact, READY_TO_CONTACT_HEADERS
        )

    def ping(self) -> dict[str, Any]:
        return shared_sheets.ping()


def get_sheets_client() -> SheetsClient:
    return SheetsClient()
