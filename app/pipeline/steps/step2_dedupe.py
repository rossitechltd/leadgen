from __future__ import annotations

import logging
from typing import Any

import sheets
from app.config import get_settings
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus
from app.scrapers.lead_mapping import imported_facebook_links, normalize_facebook_url

logger = logging.getLogger(__name__)


def _imported_links(settings) -> set[str]:
    try:
        rows = sheets.read_all(settings.sheet_all_imported)
    except sheets.SheetsError as exc:
        raise RuntimeError(
            f"Could not read '{settings.sheet_all_imported}' sheet: {exc}"
        ) from exc
    return imported_facebook_links(rows)


def run(ctx: PipelineContext) -> StepResult:
    """
    Step 2: Remove leads from Dynamic Lead Sheet that already exist in allimported.

    Matches on normalized Facebook Link — these are leads already contacted/imported.
    """
    settings = get_settings()

    if not settings.sheets_configured:
        ctx.add_log(
            f"Step 2: Google Sheets key not found at {settings.service_account_path}"
        )
        return StepResult(
            status=StepStatus.FAILED,
            message=f"Service account JSON not found: {settings.service_account_path.name}",
            stats={},
        )

    try:
        imported = _imported_links(settings)
    except RuntimeError as exc:
        ctx.add_log(f"Step 2: {exc}")
        return StepResult(status=StepStatus.FAILED, message=str(exc), stats={})

    ctx.add_log(
        f"Step 2: loaded {len(imported)} Facebook link(s) from "
        f"{settings.sheet_all_imported} (column: link)"
    )

    try:
        dynamic_rows = sheets.read_all_with_row_indices(settings.sheet_dynamic_lead)
    except sheets.SheetsError as exc:
        ctx.add_log(f"Step 2: could not read Dynamic Lead Sheet: {exc}")
        return StepResult(
            status=StepStatus.FAILED,
            message=f"Could not read {settings.sheet_dynamic_lead}",
            stats={},
        )

    stats: dict[str, Any] = {
        "checked": len(dynamic_rows),
        "imported_links": len(imported),
        "duplicates_found": 0,
        "removed": 0,
        "kept": 0,
    }
    rows_to_delete: list[int] = []

    for row_index, row in dynamic_rows:
        raw = row.get("Facebook Link") or ""
        if not raw:
            stats["kept"] += 1
            continue

        link = normalize_facebook_url(str(raw))
        if link in imported:
            stats["duplicates_found"] += 1
            rows_to_delete.append(row_index)
            ctx.add_log(f"Step 2: duplicate — {row.get('Business Name') or link}")
        else:
            stats["kept"] += 1

    if rows_to_delete:
        try:
            sheets.delete_rows(settings.sheet_dynamic_lead, rows_to_delete)
            stats["removed"] = len(rows_to_delete)
            ctx.add_log(
                f"Step 2: removed {len(rows_to_delete)} duplicate row(s) from "
                f"{settings.sheet_dynamic_lead}"
            )
        except sheets.SheetsError as exc:
            ctx.add_log(f"Step 2: failed to delete rows: {exc}")
            return StepResult(
                status=StepStatus.FAILED,
                message=f"Failed to delete duplicate rows: {exc}",
                stats=stats,
            )
    else:
        ctx.add_log("Step 2: no duplicates found in Dynamic Lead Sheet")

    message = (
        f"Removed {stats['removed']} duplicate(s); {stats['kept']} lead(s) remain"
        if stats["removed"]
        else f"No duplicates — {stats['kept']} lead(s) in Dynamic Lead Sheet"
    )

    return StepResult(status=StepStatus.SUCCESS, message=message, stats=stats)
