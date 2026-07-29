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
_scrapesheet_rows_cache: dict[str, tuple[float, list[tuple[int, str, str]]]] = {}


def _cache_ttl_secs() -> float:
    return float(os.getenv("SHEETS_CACHE_TTL_SECS", "120"))


def _scrape_row_cache_ttl() -> float:
    return float(os.getenv("SCRAPE_QUEUE_ROW_CACHE_SECS", "8"))


def is_quota_cooldown() -> bool:
    return time.monotonic() < _quota_cooldown_until


def quota_cooldown_remaining_secs() -> float:
    return max(0.0, _quota_cooldown_until - time.monotonic())


class SheetsError(Exception):
    """Raised when Google Sheets operations fail."""


def is_quota_error(exc: BaseException) -> bool:
    text = str(exc)
    if "[429]" in text or "quota exceeded" in text.lower():
        return True
    return "quota" in text.lower()


def coerce_quota_error(exc: BaseException) -> SheetsError | None:
    """Return SheetsError for quota/API 429 errors; activate cooldown when needed."""
    if isinstance(exc, SheetsError):
        return exc if is_quota_error(exc) else None
    if is_quota_error(exc):
        if not is_quota_cooldown():
            _activate_quota_cooldown()
        return SheetsError("Sheets API quota exceeded — cooling down")
    return None


def _activate_quota_cooldown() -> None:
    global _quota_cooldown_until
    cooldown = float(os.getenv("SHEETS_QUOTA_COOLDOWN_SECS", "90"))
    _quota_cooldown_until = time.monotonic() + cooldown
    logger.warning("Sheets read quota hit — pausing API reads for %.0fs", cooldown)


def invalidate_worksheet_cache(worksheet_name: str, *, lead_index: bool = True) -> None:
    with _cache_lock:
        _header_cache.pop(worksheet_name, None)
        _records_cache.pop(worksheet_name, None)
        _records_cache.pop(f"{worksheet_name}::positional", None)
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
    """Clear scrapesheet row caches (single row + full A:B read)."""
    with _cache_lock:
        _scrape_row_cache.pop(worksheet_name, None)
        _scrapesheet_rows_cache.pop(worksheet_name, None)


def invalidate_all_caches() -> None:
    with _cache_lock:
        _header_cache.clear()
        _records_cache.clear()
        _scrape_row_cache.clear()
        _scrapesheet_rows_cache.clear()
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
        coerced = coerce_quota_error(exc)
        if coerced is not None:
            raise coerced
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
        return _retry_on_quota(get_spreadsheet().worksheet, name)
    except gspread.WorksheetNotFound as exc:
        raise SheetsError(f"Worksheet '{name}' not found") from exc


def ensure_worksheet(name: str, headers: list[str] | None = None) -> gspread.Worksheet:
    spreadsheet = get_spreadsheet()
    try:
        ws = _retry_on_quota(spreadsheet.worksheet, name)
    except gspread.WorksheetNotFound:
        cols = max(len(headers), 11) if headers else 26
        ws = _retry_on_quota(
            spreadsheet.add_worksheet, title=name, rows=1000, cols=cols
        )
        logger.info("Created worksheet '%s'", name)

    if headers:
        existing = _retry_on_quota(ws.row_values, 1)
        if not existing:
            _retry_on_quota(ws.update, [headers], "A1")
            logger.info("Wrote headers to '%s'", name)
        elif existing[: len(headers)] != headers:
            logger.warning(
                "Worksheet '%s' headers differ. Expected %s, found %s",
                name,
                headers,
                existing[: len(headers)],
            )
    return ws


def extend_worksheet_headers(worksheet_name: str, headers: list[str]) -> list[str]:
    """Append missing header cells to row 1 without reordering existing columns."""
    if not headers:
        return []

    ws = get_worksheet(worksheet_name)
    existing = list(_retry_on_quota(ws.row_values, 1) or [])
    while existing and not str(existing[-1]).strip():
        existing.pop()

    if not existing:
        _retry_on_quota(ws.update, [headers], "A1")
        invalidate_worksheet_cache(worksheet_name)
        logger.info("Wrote headers to '%s'", worksheet_name)
        return list(headers)

    missing = [header for header in headers if header not in existing]
    if not missing:
        return existing

    new_headers = existing + missing
    end_a1 = gspread.utils.rowcol_to_a1(1, len(new_headers))
    _retry_on_quota(ws.update, [new_headers], f"A1:{end_a1}")
    invalidate_worksheet_cache(worksheet_name)
    logger.info(
        "Extended '%s' headers with %d column(s): %s",
        worksheet_name,
        len(missing),
        missing,
    )
    return new_headers


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
    # Single API read for link, name, scrape, activity (A, B, C, H)
    raw_rows = _retry_on_quota(ws.get, "A2:H")
    if not raw_rows:
        raw_rows = []

    rows: list[tuple[int, str, str, str, int]] = []
    for offset, row_cells in enumerate(raw_rows):
        row_index = offset + 2
        link = str(row_cells[0] if len(row_cells) > 0 else "").strip()
        name = str(row_cells[1] if len(row_cells) > 1 else "").strip()
        scrape_len = len(str(row_cells[2] if len(row_cells) > 2 else "").strip())
        activity = str(row_cells[7] if len(row_cells) > 7 else "").strip()
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


def read_rows_with_sheet_indices(
    worksheet_name: str,
    *,
    use_cache: bool = True,
) -> list[tuple[int, dict[str, Any]]]:
    """
    Read sheet data rows with accurate 1-based row numbers.

    Unlike get_all_records(), blank rows in the middle of the sheet keep
    their real row index (critical for batch_update by row number).
    """
    if use_cache:
        with _cache_lock:
            cached = _records_cache.get(f"{worksheet_name}::positional")
            if cached and (time.monotonic() - cached[0]) < _cache_ttl_secs():
                return cached[1]

    ws = get_worksheet(worksheet_name)
    headers = _worksheet_headers(ws)
    if not headers:
        return []

    ncol = len(headers)
    end_col = gspread.utils.rowcol_to_a1(1, ncol).rstrip("0123456789")
    raw_rows = _retry_on_quota(ws.get, f"A2:{end_col}")
    if not raw_rows:
        raw_rows = []

    rows: list[tuple[int, dict[str, Any]]] = []
    for offset, row_cells in enumerate(raw_rows):
        row_index = offset + 2
        row: dict[str, Any] = {}
        has_data = False
        for col_idx, header in enumerate(headers):
            if not header:
                continue
            value = row_cells[col_idx] if col_idx < len(row_cells) else ""
            if str(value or "").strip():
                has_data = True
            row[header] = value
        if has_data:
            rows.append((row_index, row))

    with _cache_lock:
        _records_cache[f"{worksheet_name}::positional"] = (time.monotonic(), rows)
    return rows


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
    _retry_on_quota(
        get_worksheet(worksheet_name).append_row,
        values,
        value_input_option="USER_ENTERED",
    )


def update_scrape_queue_link(worksheet_name: str, row_index: int, link: str) -> None:
    """Set column A (link). Column B (data) is never touched — MMM triggers on link change."""
    _retry_on_quota(get_worksheet(worksheet_name).update_cell, row_index, 1, link)
    invalidate_scrape_row_cache(worksheet_name)


def clear_scrape_queue_data(worksheet_name: str, row_index: int) -> None:
    """Clear column B (data) on scrapesheet row 2 — removes stale paste before MMM runs."""
    _retry_on_quota(get_worksheet(worksheet_name).update_cell, row_index, 2, "")
    invalidate_scrape_row_cache(worksheet_name)


def clear_scrape_queue_row(worksheet_name: str, row_index: int) -> None:
    """Clear link (A) and data (B) on scrapesheet row 2."""
    _retry_on_quota(
        get_worksheet(worksheet_name).update,
        f"A{row_index}:B{row_index}",
        [["", ""]],
        value_input_option="USER_ENTERED",
    )
    invalidate_scrape_row_cache(worksheet_name)


def reset_scrapesheet_links(worksheet_name: str, links: list[str]) -> int:
    """Replace scrapesheet data with link column (column B empty). Returns row count."""
    cleared = clear_worksheet_data(worksheet_name)
    if cleared:
        logger.info("Cleared %d scrapesheet row(s) before repopulating", cleared)
    if not links:
        invalidate_scrape_row_cache(worksheet_name)
        return 0
    append_rows(worksheet_name, [[link, ""] for link in links])
    invalidate_scrape_row_cache(worksheet_name)
    logger.info("Populated scrapesheet with %d link(s)", len(links))
    return len(links)


def update_cell(worksheet_name: str, row: int, col: int, value: Any) -> None:
    _retry_on_quota(get_worksheet(worksheet_name).update_cell, row, col, value)
    invalidate_worksheet_cache(worksheet_name)


def _build_header_maps(headers: list[str]) -> tuple[dict[str, int], dict[str, int]]:
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
    return header_map, header_map_lower


def _resolve_header_col(
    header: str,
    header_map: dict[str, int],
    header_map_lower: dict[str, int],
) -> int | None:
    col = header_map.get(header)
    if col is None:
        col = header_map_lower.get(header.strip().lower())
    return col


def update_row_by_header(
    worksheet_name: str, row_index: int, updates: dict[str, Any]
) -> None:
    batch_update_rows_by_header(worksheet_name, {row_index: updates})


_BATCH_ROW_UPDATE_CHUNK = 200


def batch_update_rows_by_header(
    worksheet_name: str,
    row_updates: dict[int, dict[str, Any]],
) -> None:
    """Update many rows in few batch_update calls (one worksheet/header read)."""
    if not row_updates:
        return

    ws = get_worksheet(worksheet_name)
    headers = _worksheet_headers(ws)
    header_map, header_map_lower = _build_header_maps(headers)

    batch: list[dict[str, Any]] = []
    scrape_len_patches: dict[int, int] = {}

    for row_index, updates in sorted(row_updates.items()):
        for header, value in updates.items():
            col = _resolve_header_col(header, header_map, header_map_lower)
            if col is None:
                logger.warning(
                    "Column '%s' not found in '%s' — skipped (headers: %s)",
                    header,
                    worksheet_name,
                    headers,
                )
                continue
            if str(header).strip().lower() == "scrape":
                scrape_len_patches[row_index] = len(str(value).strip())
            batch.append(
                {
                    "range": gspread.utils.rowcol_to_a1(row_index, col),
                    "values": [[value]],
                }
            )

    if not batch:
        return

    for offset in range(0, len(batch), _BATCH_ROW_UPDATE_CHUNK):
        chunk = batch[offset : offset + _BATCH_ROW_UPDATE_CHUNK]
        _retry_on_quota(
            ws.batch_update,
            chunk,
            value_input_option="USER_ENTERED",
        )

    for row_index, scrape_len in scrape_len_patches.items():
        patch_lead_index_row(worksheet_name, row_index, scrape_len=scrape_len)

    invalidate_worksheet_cache(worksheet_name)


_BATCH_ACTIVITY_CHUNK = 80


def batch_update_lead_activity(
    worksheet_name: str, updates: dict[int, str]
) -> None:
    """Set Lead Activity for many rows in chunked batch_update calls."""
    if not updates:
        return

    ws = get_worksheet(worksheet_name)
    headers = _worksheet_headers(ws)
    col: int | None = None
    for i, h in enumerate(headers):
        if str(h).strip().lower() == "lead activity":
            col = i + 1
            break
    if col is None:
        raise SheetsError("Lead Activity column not found in worksheet headers")

    items = list(updates.items())
    for start in range(0, len(items), _BATCH_ACTIVITY_CHUNK):
        chunk = items[start : start + _BATCH_ACTIVITY_CHUNK]
        batch = [
            {
                "range": gspread.utils.rowcol_to_a1(row_index, col),
                "values": [[activity]],
            }
            for row_index, activity in chunk
        ]
        _retry_on_quota(
            ws.batch_update,
            batch,
            value_input_option="USER_ENTERED",
        )

    for row_index, activity in updates.items():
        patch_lead_index_row(worksheet_name, row_index, activity=activity)
    invalidate_worksheet_cache(worksheet_name, lead_index=False)


_BATCH_SCRAPE_CHUNK = 80


def batch_clear_scrape_column(worksheet_name: str, row_indices: list[int]) -> None:
    """Clear Scrape column for many rows in chunked batch_update calls."""
    if not row_indices:
        return

    ws = get_worksheet(worksheet_name)
    headers = _worksheet_headers(ws)
    col: int | None = None
    for i, h in enumerate(headers):
        key = str(h).strip().lower()
        if key in {"scrape", "website link scrape"}:
            col = i + 1
            break
    if col is None:
        raise SheetsError("Scrape column not found in worksheet headers")

    unique = sorted(set(row_indices))
    for start in range(0, len(unique), _BATCH_SCRAPE_CHUNK):
        chunk = unique[start : start + _BATCH_SCRAPE_CHUNK]
        batch = [
            {
                "range": gspread.utils.rowcol_to_a1(row_index, col),
                "values": [[""]],
            }
            for row_index in chunk
        ]
        _retry_on_quota(
            ws.batch_update,
            batch,
            value_input_option="USER_ENTERED",
        )

    for row_index in unique:
        patch_lead_index_row(worksheet_name, row_index, scrape_len=0)
    invalidate_worksheet_cache(worksheet_name, lead_index=False)


_DELETE_BATCH_CHUNK = 25


def _delete_chunk_pause_secs() -> float:
    return float(os.getenv("SHEETS_DELETE_CHUNK_PAUSE_SECS", "4"))


def _delete_chunk_pause() -> None:
    pause = _delete_chunk_pause_secs()
    if pause > 0:
        time.sleep(pause)


def _positional_max_data_row(
    ws: gspread.Worksheet,
    headers: list[str] | None = None,
) -> int:
    """
    Max 1-based sheet row index for data rows, aligned with
    read_rows_with_sheet_indices (not get_all_values, which can undercount).
    """
    if headers is None:
        headers = _worksheet_headers(ws)
    if not headers:
        return 1
    ncol = len(headers)
    end_col = gspread.utils.rowcol_to_a1(1, ncol).rstrip("0123456789")
    raw_rows = _retry_on_quota(ws.get, f"A2:{end_col}")
    if not raw_rows:
        return 1
    return len(raw_rows) + 1


def _contiguous_row_runs(sorted_indices: list[int]) -> list[tuple[int, int]]:
    """Turn sorted 1-based row indices into inclusive (low, high) runs."""
    if not sorted_indices:
        return []
    runs: list[tuple[int, int]] = []
    start = end = sorted_indices[0]
    for row_index in sorted_indices[1:]:
        if row_index == end + 1:
            end = row_index
        else:
            runs.append((start, end))
            start = end = row_index
    runs.append((start, end))
    return runs


def _batch_delete_single_rows(
    ws: gspread.Worksheet,
    row_indices: list[int],
    *,
    max_row: int,
) -> None:
    """Delete many non-contiguous rows in chunked batchUpdate calls."""
    if not row_indices:
        return
    valid = [row_index for row_index in row_indices if 2 <= row_index <= max_row]
    if len(valid) < len(row_indices):
        logger.warning(
            "batch delete skipped %d out-of-range row index(es) (max row %d)",
            len(row_indices) - len(valid),
            max_row,
        )
    if not valid:
        return
    requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "ROWS",
                    "startIndex": row_index - 1,
                    "endIndex": row_index,
                }
            }
        }
        for row_index in sorted(valid, reverse=True)
    ]
    for start in range(0, len(requests), _DELETE_BATCH_CHUNK):
        chunk = requests[start : start + _DELETE_BATCH_CHUNK]
        _retry_on_quota(
            ws.client.batch_update,
            ws.spreadsheet_id,
            {"requests": chunk},
        )
        if start + _DELETE_BATCH_CHUNK < len(requests):
            _delete_chunk_pause()


def delete_rows(
    worksheet_name: str,
    row_indices: list[int],
    *,
    max_row: int | None = None,
) -> None:
    """Delete rows by 1-based sheet row index (batched to reduce API calls)."""
    if not row_indices:
        return

    ws = get_worksheet(worksheet_name)
    if max_row is None:
        headers = _worksheet_headers(ws)
        max_row = min(ws.row_count, _positional_max_data_row(ws, headers))
    unique = sorted(
        {
            row_index
            for row_index in row_indices
            if 2 <= row_index <= max_row
        }
    )
    if not unique:
        return

    skipped = len(set(row_indices)) - len(unique)
    if skipped:
        logger.warning(
            "delete_rows skipped %d out-of-range index(es) on '%s' (max row %d)",
            skipped,
            worksheet_name,
            max_row,
        )

    runs = _contiguous_row_runs(unique)
    multi_runs: list[tuple[int, int]] = []
    singles: list[int] = []
    for low, high in runs:
        if low == high:
            singles.append(low)
        else:
            multi_runs.append((low, high))

    sorted_multi = sorted(multi_runs, key=lambda run: run[1], reverse=True)
    for idx, (low, high) in enumerate(sorted_multi):
        if low > max_row:
            continue
        high = min(high, max_row)
        _retry_on_quota(ws.delete_rows, low, high)
        if idx + 1 < len(sorted_multi):
            _delete_chunk_pause()

    if singles:
        _batch_delete_single_rows(ws, sorted(singles, reverse=True), max_row=max_row)

    invalidate_worksheet_cache(worksheet_name)


def clear_worksheet_data(worksheet_name: str) -> int:
    """Delete all data rows below the header. Returns number of rows removed."""
    ws = get_worksheet(worksheet_name)
    values = _retry_on_quota(ws.get_all_values)
    if len(values) <= 1:
        invalidate_worksheet_cache(worksheet_name)
        return 0
    end_row = len(values)
    _retry_on_quota(ws.delete_rows, 2, end_row)
    invalidate_worksheet_cache(worksheet_name)
    logger.info("Cleared %d data row(s) from '%s'", end_row - 1, worksheet_name)
    return end_row - 1


def list_worksheet_titles() -> list[str]:
    return [ws.title for ws in get_spreadsheet().worksheets()]


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


def read_scrapesheet_rows(
    worksheet_name: str, *, use_cache: bool = True
) -> list[tuple[int, str, str]]:
    """Read all scrapesheet data rows: (1-based row index, link, data)."""
    if use_cache:
        with _cache_lock:
            cached = _scrapesheet_rows_cache.get(worksheet_name)
            if cached:
                age = time.monotonic() - cached[0]
                if age < _scrape_row_cache_ttl() or is_quota_cooldown():
                    return list(cached[1])

    ws = get_worksheet(worksheet_name)
    raw_rows = _retry_on_quota(ws.get, "A2:B")
    if not raw_rows:
        rows: list[tuple[int, str, str]] = []
    else:
        rows = []
        for offset, cells in enumerate(raw_rows):
            row_index = offset + 2
            link = str(cells[0] if len(cells) > 0 else "").strip()
            data = str(cells[1] if len(cells) > 1 else "").strip()
            if link or data:
                rows.append((row_index, link, data))

    with _cache_lock:
        _scrapesheet_rows_cache[worksheet_name] = (time.monotonic(), rows)
    return list(rows)


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
