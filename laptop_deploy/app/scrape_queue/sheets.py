"""Shared Google Sheets access for all apps in this project.

Credentials: service account JSON in project root (default
autoleadverification-e76d53033380.json). Spreadsheet opened by title
(default Lead Manager, override with GOOGLE_SHEET_NAME).
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_SERVICE_ACCOUNT_FILE = "autoleadverification-e76d53033380.json"
DEFAULT_SHEET_NAME = "Lead Manager"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_spreadsheet: gspread.Spreadsheet | None = None


class SheetsError(Exception):
    """Raised when Google Sheets operations fail."""


def service_account_path() -> Path:
    filename = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", DEFAULT_SERVICE_ACCOUNT_FILE)
    path = Path(filename)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def sheet_name() -> str:
    return os.getenv("GOOGLE_SHEET_NAME", DEFAULT_SHEET_NAME)


def is_configured() -> bool:
    return service_account_path().exists()


@lru_cache(maxsize=1)
def get_client() -> gspread.Client:
    key = service_account_path()
    if not key.exists():
        raise SheetsError(
            f"Service account key not found at {key}. "
            f"Place {DEFAULT_SERVICE_ACCOUNT_FILE} in the project root."
        )
    creds = Credentials.from_service_account_file(str(key), scopes=SCOPES)
    return gspread.authorize(creds)


def get_spreadsheet(title: str | None = None) -> gspread.Spreadsheet:
    global _spreadsheet
    name = title or sheet_name()
    if _spreadsheet is None or _spreadsheet.title != name:
        try:
            _spreadsheet = get_client().open(name)
        except gspread.SpreadsheetNotFound as exc:
            raise SheetsError(
                f"Spreadsheet '{name}' not found. Share it with the service account."
            ) from exc
    return _spreadsheet


def get_worksheet(name: str) -> gspread.Worksheet:
    try:
        return get_spreadsheet().worksheet(name)
    except gspread.WorksheetNotFound as exc:
        raise SheetsError(f"Worksheet '{name}' not found") from exc


def ensure_worksheet(name: str, headers: list[str] | None = None) -> gspread.Worksheet:
    spreadsheet = get_spreadsheet()
    try:
        ws = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        cols = max(len(headers), 11) if headers else 26
        ws = spreadsheet.add_worksheet(title=name, rows=1000, cols=cols)
        logger.info("Created worksheet '%s'", name)

    if headers:
        existing = ws.row_values(1)
        if not existing:
            ws.update([headers], "A1")
            logger.info("Wrote headers to '%s'", name)
        elif existing[: len(headers)] != headers:
            logger.warning(
                "Worksheet '%s' headers differ. Expected %s, found %s",
                name,
                headers,
                existing[: len(headers)],
            )
    return ws


def read_all(worksheet_name: str) -> list[dict[str, Any]]:
    """Return all data rows as dicts (header row keys)."""
    return get_worksheet(worksheet_name).get_all_records()


def read_all_with_row_indices(worksheet_name: str) -> list[tuple[int, dict[str, Any]]]:
    """Return (1-based sheet row index, row dict) for each data row."""
    records = get_worksheet(worksheet_name).get_all_records()
    return [(index + 2, record) for index, record in enumerate(records)]


def append_rows(worksheet_name: str, rows: list[list[Any]]) -> None:
    if not rows:
        return
    get_worksheet(worksheet_name).append_rows(rows, value_input_option="USER_ENTERED")


def prepend_rows(worksheet_name: str, rows: list[list[Any]]) -> None:
    """Insert rows directly below the header (row 2), newest-first batches stay on top."""
    if not rows:
        return
    get_worksheet(worksheet_name).insert_rows(
        rows, row=2, value_input_option="USER_ENTERED"
    )


def append_row(worksheet_name: str, values: list[Any]) -> None:
    get_worksheet(worksheet_name).append_row(values, value_input_option="USER_ENTERED")


def update_cell(worksheet_name: str, row: int, col: int, value: Any) -> None:
    get_worksheet(worksheet_name).update_cell(row, col, value)


def _header_column_map(headers: list[str]) -> dict[str, int]:
    """Map lowercase header name → 1-based column index."""
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        key = str(header or "").strip().lower()
        if key:
            mapping[key] = index + 1
    return mapping


def read_cell_by_header(worksheet_name: str, row_index: int, header: str) -> str:
    ws = get_worksheet(worksheet_name)
    headers = ws.row_values(1)
    col = _header_column_map(headers).get(header.strip().lower())
    if not col:
        return ""
    return str(ws.cell(row_index, col).value or "").strip()


def update_row_by_header(
    worksheet_name: str, row_index: int, updates: dict[str, Any]
) -> None:
    ws = get_worksheet(worksheet_name)
    headers = ws.row_values(1)
    header_map = _header_column_map(headers)
    for header, value in updates.items():
        col = header_map.get(str(header).strip().lower())
        if col:
            ws.update_cell(row_index, col, value)


def delete_rows(worksheet_name: str, row_indices: list[int]) -> None:
    """Delete rows by 1-based sheet row index."""
    if not row_indices:
        return
    ws = get_worksheet(worksheet_name)
    for row_index in sorted(row_indices, reverse=True):
        ws.delete_rows(row_index)


def read_row(worksheet_name: str, row_index: int) -> dict[str, Any]:
    """Return one row as a dict keyed by header names."""
    ws = get_worksheet(worksheet_name)
    headers = ws.row_values(1)
    values = ws.row_values(row_index)
    row: dict[str, Any] = {}
    for index, header in enumerate(headers):
        if header:
            row[header] = values[index] if index < len(values) else ""
    return row


def set_row(worksheet_name: str, row_index: int, values: list[Any]) -> None:
    """Overwrite a full row by 1-based index."""
    ws = get_worksheet(worksheet_name)
    headers = ws.row_values(1)
    if not headers:
        raise SheetsError(f"Worksheet '{worksheet_name}' has no headers")
    width = len(headers)
    padded = list(values) + [""] * max(0, width - len(values))
    ws.update([padded[:width]], f"A{row_index}", value_input_option="USER_ENTERED")


def clear_row(worksheet_name: str, row_index: int) -> None:
    """Clear all cells in a row (keeps the row itself)."""
    ws = get_worksheet(worksheet_name)
    headers = ws.row_values(1)
    if not headers:
        return
    ws.update([([""] * len(headers))], f"A{row_index}", value_input_option="USER_ENTERED")
    logger.info("Cleared row %s in '%s'", row_index, worksheet_name)


def move_row_to_top(worksheet_name: str, row_index: int) -> None:
    """Move a data row to row 2 (directly under the header)."""
    if row_index <= 2:
        return
    ws = get_worksheet(worksheet_name)
    row_values = ws.row_values(row_index)
    if not row_values:
        raise SheetsError(f"Row {row_index} is empty in '{worksheet_name}'")
    ws.delete_rows(row_index)
    ws.insert_row(row_values, index=2, value_input_option="USER_ENTERED")
    logger.info("Moved row %s to top of '%s'", row_index, worksheet_name)


def ping() -> dict[str, Any]:
    spreadsheet = get_spreadsheet()
    return {
        "title": spreadsheet.title,
        "id": spreadsheet.id,
        "worksheets": [ws.title for ws in spreadsheet.worksheets()],
    }
