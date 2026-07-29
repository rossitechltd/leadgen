from __future__ import annotations

import logging
from typing import Any

import sheets
from app.config import get_settings
from app.leads.parse_upload import LeadImportError, parse_lead_list
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus
from app.scrapers.lead_mapping import (
    imported_facebook_links,
    lead_row_for_sheet,
    normalize_facebook_url,
)
from app.scrape_queue import get_scrape_queue
from app.sheets.columns import DYNAMIC_LEAD_HEADERS

logger = logging.getLogger(__name__)


def _existing_links(settings) -> tuple[set[str], set[str]]:
    dynamic: set[str] = set()
    imported: set[str] = set()
    try:
        for row in sheets.read_all(settings.sheet_dynamic_lead):
            raw = row.get("Facebook Link") or ""
            if raw:
                dynamic.add(normalize_facebook_url(str(raw)))
    except sheets.SheetsError as exc:
        logger.warning("Could not read %s for dedupe: %s", settings.sheet_dynamic_lead, exc)

    try:
        imported = imported_facebook_links(sheets.read_all(settings.sheet_all_imported))
    except sheets.SheetsError as exc:
        logger.warning("Could not read %s for dedupe: %s", settings.sheet_all_imported, exc)

    return dynamic, imported


def import_leads_from_text(text: str, *, log: list[str] | None = None) -> dict[str, Any]:
    settings = get_settings()
    try:
        parsed = parse_lead_list(text)
    except LeadImportError as exc:
        raise

    dynamic_links, imported_links = _existing_links(settings)
    stats: dict[str, Any] = {
        "parsed": len(parsed),
        "rows_appended": 0,
        "duplicate_dynamic": 0,
        "duplicate_imported": 0,
        "duplicate_run": 0,
        "no_link_skipped": 0,
    }
    rows_to_append: list[list[str]] = []
    seen: set[str] = set()

    for lead in parsed:
        link = lead["facebook_link"]
        if link in seen:
            stats["duplicate_run"] += 1
            continue
        if link in dynamic_links:
            stats["duplicate_dynamic"] += 1
            continue
        if link in imported_links:
            stats["duplicate_imported"] += 1
            continue
        seen.add(link)
        rows_to_append.append(lead_row_for_sheet(lead))

    if rows_to_append:
        sheets.ensure_worksheet(settings.sheet_dynamic_lead, DYNAMIC_LEAD_HEADERS)
        sheets.prepend_rows(settings.sheet_dynamic_lead, rows_to_append)
        get_scrape_queue().refresh_lead_index()
        stats["rows_appended"] = len(rows_to_append)
        msg = f"Imported {len(rows_to_append)} lead(s) to Dynamic Lead Sheet"
        if log is not None:
            log.append(msg)
        logger.info(msg)
    else:
        msg = (
            f"No new leads to import (parsed={stats['parsed']}, "
            f"dup_sheet={stats['duplicate_dynamic']}, "
            f"dup_imported={stats['duplicate_imported']})"
        )
        if log is not None:
            log.append(msg)

    stats["message"] = msg
    return stats


def run(ctx: PipelineContext) -> StepResult:
    """Step 1: Import uploaded lead list (Facebook Link + Business Name)."""
    settings = get_settings()
    step_opts = dict(ctx.step_options.get("step1") or {})
    content = step_opts.get("content") or step_opts.get("text") or ""

    if not settings.sheets_configured:
        ctx.add_log(
            f"Step 1: Google Sheets key not found at {settings.service_account_path}"
        )
        return StepResult(
            status=StepStatus.FAILED,
            message=f"Service account JSON not found: {settings.service_account_path.name}",
            stats={},
        )

    if not content.strip():
        pending = get_scrape_queue().count_pending()
        retryable = get_scrape_queue().count_failed_retryable()
        if pending + retryable > 0:
            msg = f"{pending + retryable} lead(s) ready on Dynamic Lead Sheet for page scrape"
            ctx.add_log(f"Step 1: {msg}")
            return StepResult(
                status=StepStatus.SUCCESS,
                message=msg,
                stats={"pending_on_sheet": pending, "retryable": retryable},
            )
        ctx.add_log("Step 1: no upload content — use dashboard Upload leads")
        return StepResult(
            status=StepStatus.SKIPPED,
            message="Upload a lead list on the dashboard (or pass content in step1 options)",
            stats={},
        )

    try:
        stats = import_leads_from_text(content, log=ctx.log)
    except LeadImportError as exc:
        ctx.add_log(f"Step 1: {exc}")
        return StepResult(status=StepStatus.FAILED, message=str(exc), stats={})

    appended = stats.get("rows_appended", 0)
    if appended == 0:
        return StepResult(
            status=StepStatus.SKIPPED,
            message=stats.get("message", "No new leads imported"),
            stats=stats,
        )

    return StepResult(
        status=StepStatus.SUCCESS,
        message=stats.get("message", f"Imported {appended} lead(s)"),
        stats=stats,
    )
