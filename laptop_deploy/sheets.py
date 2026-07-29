"""Shared Google Sheets access for all apps in this project.

Credentials: service account JSON in project root (default
autoleadverification-e76d53033380.json). Spreadsheet opened by title
(default Lead Manager, override with GOOGLE_SHEET_NAME).
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from threading import Lock
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
_cache_lock = Lock()
_header_cache: dict[str, tuple[float, list[str]]] = {}
_records_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_lead_index_cache: dict[str, tuple[float, list[tuple[int, str, str, str]]]] = {}
_quota_cooldown_until: float = 0.0
_scrape_row_cache: dict[str, tuple[float, tuple[str, str]]] = {}


def _cache_ttl_secs() -> float:
    return float(os.getenv("SHEETS_CACHE_TTL_SECS", "120"))


def _scrape_row_cache_ttl() -> float:
    return float(os.getenv("SCRAPE_QUEUE_ROW_CACHE_SECS", "8"))


def is_quota_cooldown() -> bool:
    return time.monotonic() < _quota_cooldown_until


def quota_cooldown_remaining_secs() -> float:
    return max(0.0, _quota_cooldown_until - time.monotonic())


def _activate_quota_cooldown() -> None:
    global _quota_cooldown_until
    cooldown = float(os.getenv("SHEETS_QUOTA_COOLDOWN_SECS", "90"))
    _quota_cooldown_until = time.monotonic() + cooldown
    logger.warning("Sheets read quota hit — pausing API reads for %.0fs", cooldown)


def invalidate_worksheet_cache(worksheet_name: str, *, lead_index: bool = True) -> None:
    with _cache_lock:
        _header_cache.pop(worksheet_name, None)
        _records_cache.pop(worksheet_name, None)
        _scrape_row_cache.pop(worksheet_name, None)
        if lead_index:
            _lead_index_cache.pop(worksheet_name, None)


def invalidate_lead_index_cache(worksheet_name: str) -> None:
    with _cache_lock:
        _lead_index_cache.pop(worksheet_name, None)


def patch_lead_index_row(
    worksheet_name: str,
    row_index: int,
    *,
    activity: str | None = None,
    link: str | None = None,
    scrape_len: int | None = None,
) -> None:
    """Update one row in the cached lead index without a full sheet re-read."""
    with _cache_lock:
        cached = _lead_index_cache.get(worksheet_name)
        if not cached:
            return
        mono, rows = cached
        updated: list[tuple[int, str, str, str, int]] = []
        found = False
        for row in rows:
            r, row_link, name, row_activity = row[0], row[1], row[2], row[3]
            row_scrape_len = row[4] if len(row) > 4 else 0
            if r == row_index:
                found = True
                updated.append(
                    (
                        r,
                        link if link is not None else row_link,
                        name,
                        activity if activity is not None else row_activity,
                        scrape_len if scrape_len is not None else row_scrape_len,
                    )
                )
            else:
                updated.append((r, row_link, name, row_activity, row_scrape_len))
        if found:
            _lead_index_cache[worksheet_name] = (mono, updated)


def invalidate_scrape_row_cache(worksheet_name: str) -> None:
    """Clear only scrapesheet row-2 cache (not full worksheet records)."""
    with _cache_lock:
        _scrape_row_cache.pop(worksheet_name, None)


def invalidate_all_caches() -> None:
    with _cache_lock:
        _header_cache.clear()
        _records_cache.clear()
        _scrape_row_cache.clear()
        _lead_index_cache.clear()


def _retry_on_quota(func, *args, **kwargs) -> Any:
    """Call Sheets API once; on 429 activate cooldown instead of hammering retries."""
    if is_quota_cooldown():
        raise SheetsError(
            f"Sheets API quota cooldown ({quota_cooldown_remaining_secs():.0f}s remaining)"
        )
    try:
        return func(*args, **kwargs)
    except gspread.exceptions.APIError as exc:
        if "[429]" in str(exc) or "Quota exceeded" in str(exc):
            _activate_quota_cooldown()
            raise SheetsError("Sheets API quota exceeded — cooling down") from exc
        raise


def _worksheet_headers(ws: gspread.Worksheet, *, use_cache: bool = True) -> list[str]:
    name = ws.title
    if use_cache:
        with _cache_lock:
            cached = _header_cache.get(name)
            if cached and (time.monotonic() - cached[0]) < _cache_ttl_secs():
                return cached[1]

    headers = _retry_on_quota(ws.row_values, 1)
    with _cache_lock:
        _header_cache[name] = (time.monotonic(), headers)
    return headers


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


def read_dynamic_lead_index(
    worksheet_name: str, *, use_cache: bool = True, include_names: bool = True
) -> list[tuple[int, str, str, str, int]]:
    """
    Lightweight lead index: (row_index, facebook_link, business_name, activity, scrape_len).

    Reads columns A, B, C (length only), H (cached).
    """
    if use_cache:
        with _cache_lock:
            cached = _lead_index_cache.get(worksheet_name)
            if cached and (time.monotonic() - cached[0]) < _cache_ttl_secs():
                rows = cached[1]
                if include_names:
                    return rows
                return [
                    (r, link, "", activity, scrape_len)
                    for r, link, _name, activity, scrape_len in rows
                ]

    ws = get_worksheet(worksheet_name)
    links = _retry_on_quota(ws.col_values, 1)
    names = _retry_on_quota(ws.col_values, 2)
    scrapes = _retry_on_quota(ws.col_values, 3)
    activities = _retry_on_quota(ws.col_values, 8)

    rows: list[tuple[int, str, str, str, int]] = []
    max_len = max(len(links), len(names), len(scrapes), len(activities))
    for i in range(1, max_len):
        row_index = i + 1
        link = str(links[i] if i < len(links) else "").strip()
        name = str(names[i] if i < len(names) else "").strip()
        scrape_len = len(str(scrapes[i] if i < len(scrapes) else "").strip())
        activity = str(activities[i] if i < len(activities) else "").strip()
        if link or name or activity or scrape_len:
            rows.append((row_index, link, name, activity, scrape_len))

    with _cache_lock:
        _lead_index_cache[worksheet_name] = (time.monotonic(), rows)

    if include_names:
        return rows
    return [
        (r, link, "", activity, scrape_len)
        for r, link, _name, activity, scrape_len in rows
    ]


def read_business_name_cell(worksheet_name: str, row_index: int) -> str:
    """Read one business name (column B) — single small cell read."""
    ws = get_worksheet(worksheet_name)
    cell = _retry_on_quota(ws.cell, row_index, 2)
    return str(cell.value or "").strip()


def read_all(worksheet_name: str) -> list[dict[str, Any]]:
    """Return all data rows as dicts (header row keys)."""
    return _read_all_records(worksheet_name, use_cache=True)


def _read_all_records(worksheet_name: str, *, use_cache: bool) -> list[dict[str, Any]]:
    if use_cache:
        with _cache_lock:
            cached = _records_cache.get(worksheet_name)
            if cached and (time.monotonic() - cached[0]) < _cache_ttl_secs():
                return cached[1]

    records = _retry_on_quota(get_worksheet(worksheet_name).get_all_records)
    with _cache_lock:
        _records_cache[worksheet_name] = (time.monotonic(), records)
    return records


def read_all_with_row_indices(
    worksheet_name: str, *, use_cache: bool = True
) -> list[tuple[int, dict[str, Any]]]:
    """Return (1-based sheet row index, row dict) for each data row."""
    records = _read_all_records(worksheet_name, use_cache=use_cache)
    return [(index + 2, record) for index, record in enumerate(records)]


def append_rows(worksheet_name: str, rows: list[list[Any]]) -> None:
    if not rows:
        return
    _retry_on_quota(
        get_worksheet(worksheet_name).append_rows,
        rows,
        value_input_option="USER_ENTERED",
    )
    invalidate_worksheet_cache(worksheet_name)


def prepend_rows(worksheet_name: str, rows: list[list[Any]]) -> None:
    """Insert rows directly below the header (row 2), newest-first batches stay on top."""
    if not rows:
        return
    _retry_on_quota(
        get_worksheet(worksheet_name).insert_rows,
        rows,
        row=2,
        value_input_option="USER_ENTERED",
    )
    invalidate_worksheet_cache(worksheet_name)


def append_row(worksheet_name: str, values: list[Any]) -> None:
    get_worksheet(worksheet_name).append_row(values, value_input_option="USER_ENTERED")


def update_scrape_queue_link(worksheet_name: str, row_index: int, link: str) -> None:
    """Set column A (link). Column B (data) is never touched — MMM triggers on link change."""
    _retry_on_quota(get_worksheet(worksheet_name).update_cell, row_index, 1, link)
    invalidate_scrape_row_cache(worksheet_name)


def update_cell(worksheet_name: str, row: int, col: int, value: Any) -> None:
    _retry_on_quota(get_worksheet(worksheet_name).update_cell, row, col, value)
    invalidate_worksheet_cache(worksheet_name)


def update_row_by_header(
    worksheet_name: str, row_index: int, updates: dict[str, Any]
) -> None:
    ws = get_worksheet(worksheet_name)
    headers = _worksheet_headers(ws)
    header_map: dict[str, int] = {}
    header_map_lower: dict[str, int] = {}
    for i, h in enumerate(headers):
        if not h:
            continue
        if h not in header_map:
            header_map[h] = i + 1
        key = h.strip().lower()
        if key not in header_map_lower:
            header_map_lower[key] = i + 1

    batch: list[dict[str, Any]] = []
    scrape_len_update: int | None = None
    for header, value in updates.items():
        col = header_map.get(header)
        if col is None:
            col = header_map_lower.get(header.strip().lower())
        if col is None:
            logger.warning(
                "Column '%s' not found in '%s' — skipped (headers: %s)",
                header,
                worksheet_name,
                headers,
            )
            continue
        if str(header).strip().lower() == "scrape":
            scrape_len_update = len(str(value).strip())
        batch.append(
            {
                "range": gspread.utils.rowcol_to_a1(row_index, col),
                "values": [[value]],
            }
        )

    if not batch:
        return

    _retry_on_quota(
        ws.batch_update,
        batch,
        value_input_option="USER_ENTERED",
    )

    update_keys = {str(k).strip().lower() for k in updates}
    scrape_only = update_keys <= {"scrape"}
    patches_index = bool(
        update_keys & {"lead activity", "facebook link"}
    )

    if scrape_len_update is not None:
        patch_lead_index_row(
            worksheet_name, row_index, scrape_len=scrape_len_update
        )

    if scrape_only:
        invalidate_worksheet_cache(worksheet_name, lead_index=False)
    elif patches_index:
        activity_val: str | None = None
        for key, value in updates.items():
            if str(key).strip().lower() == "lead activity":
                activity_val = str(value)
                break
        if activity_val is not None:
            patch_lead_index_row(
                worksheet_name, row_index, activity=activity_val
            )
        invalidate_worksheet_cache(worksheet_name, lead_index=False)
    else:
        invalidate_worksheet_cache(worksheet_name)


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
    headers = _worksheet_headers(ws)
    values = _retry_on_quota(ws.row_values, row_index)
    row: dict[str, Any] = {}
    for index, header in enumerate(headers):
        if header:
            row[header] = values[index] if index < len(values) else ""
    return row


def read_scrape_queue_row(
    worksheet_name: str, row_index: int = 2, *, use_cache: bool = True
) -> tuple[str, str]:
    """Read link + data from scrapesheet row 2 (one API call, cached briefly)."""
    if use_cache:
        with _cache_lock:
            cached = _scrape_row_cache.get(worksheet_name)
            if cached and (time.monotonic() - cached[0]) < _scrape_row_cache_ttl():
                return cached[1]

    ws = get_worksheet(worksheet_name)
    values = _retry_on_quota(ws.get, f"A{row_index}:B{row_index}")
    if not values:
        row = ("", "")
    else:
        cells = values[0]
        row = (
            str(cells[0] if len(cells) > 0 else "").strip(),
            str(cells[1] if len(cells) > 1 else "").strip(),
        )
    with _cache_lock:
        _scrape_row_cache[worksheet_name] = (time.monotonic(), row)
    return row


def read_scrape_queue_cells(worksheet_name: str, row_index: int = 2) -> tuple[str, str]:
    """Backward-compatible alias for read_scrape_queue_row."""
    return read_scrape_queue_row(worksheet_name, row_index)


def clear_scrape_queue_link(worksheet_name: str, row_index: int) -> None:
    """Clear link (column A); scrape data in column B is left untouched."""
    _retry_on_quota(get_worksheet(worksheet_name).update_cell, row_index, 1, "")
    invalidate_scrape_row_cache(worksheet_name)


def set_row(worksheet_name: str, row_index: int, values: list[Any]) -> None:
    """Overwrite a full row by 1-based index."""
    ws = get_worksheet(worksheet_name)
    headers = _worksheet_headers(ws)
    if not headers:
        raise SheetsError(f"Worksheet '{worksheet_name}' has no headers")
    width = len(headers)
    padded = list(values) + [""] * max(0, width - len(values))
    _retry_on_quota(
        ws.update,
        [padded[:width]],
        f"A{row_index}",
        value_input_option="USER_ENTERED",
    )
    invalidate_worksheet_cache(worksheet_name)


def clear_row(worksheet_name: str, row_index: int) -> None:
    """Clear all cells in a row (keeps the row itself)."""
    ws = get_worksheet(worksheet_name)
    headers = _worksheet_headers(ws)
    if not headers:
        return
    _retry_on_quota(
        ws.update,
        [([""] * len(headers))],
        f"A{row_index}",
        value_input_option="USER_ENTERED",
    )
    invalidate_worksheet_cache(worksheet_name)
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


_ping_cache: tuple[float, dict[str, Any]] | None = None


def ping() -> dict[str, Any]:
    global _ping_cache
    if _ping_cache and (time.monotonic() - _ping_cache[0]) < 60.0:
        return _ping_cache[1]
    spreadsheet = get_spreadsheet()
    result = {
        "title": spreadsheet.title,
        "id": spreadsheet.id,
        "worksheets": [ws.title for ws in spreadsheet.worksheets()],
    }
    _ping_cache = (time.monotonic(), result)
    return result
