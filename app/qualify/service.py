"""AI Qualify — filter Dynamic Lead Sheet rows (Step 7)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import sheets
from app.config import Settings
from app.qualify.website_status import (
    REMOVE_LEAD_STATUSES,
    WebsiteStatusCode,
    classify_website_link,
    status_counts,
)
from app.refinement.phones import normalize_uk_phone
from app.sheets.columns import (
    COL_BUSINESS_NAME,
    COL_LEAD_ACTIVITY,
    COL_PHONE_1,
    COL_PHONE_2,
    COL_REFINED,
    COL_SCRAPE,
    COL_VA,
    COL_WEBSITE_LINK,
    COL_WEBSITE_STATUS,
    DYNAMIC_LEAD_HEADERS,
    LEAD_ACTIVITY_PENDING,
)

logger = logging.getLogger(__name__)

_QUALIFY_PER_ROW_SECS = 12.0
_SHEET_FLUSH_ROWS = 25
_REMOVE_STATUSES: frozenset[str] = frozenset(s.value for s in REMOVE_LEAD_STATUSES)

ProgressCallback = Callable[[dict[str, Any], str | None], None]

_PHONE_BULLET = re.compile(
    r"•\s*Phone:\s*([^\n•]+)",
    re.IGNORECASE,
)
_PHONE_IN_TEXT = re.compile(
    r"(?:\+?44[\s(]*(?:0)?\s*|\+?44\s*|0)\d[\d\s]{8,14}",
    re.IGNORECASE,
)


@dataclass
class QualifyResult:
    ok: bool
    message: str
    stats: dict[str, Any]


@dataclass(frozen=True)
class RowDecision:
    keep: bool
    reason: str
    website_status: str = ""


@dataclass(frozen=True)
class RowEvaluation:
    decision: RowDecision
    website_status: Any


class AIQualifyService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self,
        progress_callback: ProgressCallback | None = None,
    ) -> QualifyResult:
        if not self.settings.sheets_configured:
            return QualifyResult(ok=False, message="Service account JSON not found", stats={})
        if not self.settings.openrouter_configured:
            return QualifyResult(ok=False, message="OPENROUTER_API_KEY not set in .env", stats={})

        sheet = self.settings.sheet_dynamic_lead
        sheets.ensure_worksheet(sheet, DYNAMIC_LEAD_HEADERS)
        sheets.extend_worksheet_headers(sheet, DYNAMIC_LEAD_HEADERS)
        sheets.invalidate_worksheet_cache(sheet)

        try:
            rows = sheets.read_rows_with_sheet_indices(sheet, use_cache=False)
        except sheets.SheetsError as exc:
            return QualifyResult(ok=False, message=str(exc), stats={})

        valid_row_indices = {row_index for row_index, _ in rows}

        candidates = self._candidate_rows(rows)
        stats: dict[str, Any] = {
            "sheet_rows": len(rows),
            "candidates": len(candidates),
            "total": len(candidates),
            "processed": 0,
            "kept": 0,
            "removed": 0,
            "removed_no_phone": 0,
            "removed_active_website": 0,
            "removed_business_redirect": 0,
            "removed_parked": 0,
            "removed_incomplete": 0,
            "requalified_removed": 0,
            "errors": 0,
            "website_status_counts": {},
            "per_item_estimate_secs": max(
                self.settings.qualify_website_timeout_secs + 2.0,
                _QUALIFY_PER_ROW_SECS,
            ),
        }
        to_delete: list[int] = []
        status_results: list[Any] = []
        pending_sheet_updates: dict[int, dict[str, Any]] = {}

        def _flush_sheet_updates() -> None:
            if not pending_sheet_updates:
                return
            sheets.batch_update_rows_by_header(sheet, pending_sheet_updates)
            pending_sheet_updates.clear()

        def _flush_deletes() -> None:
            if not to_delete:
                return
            indices = list(
                dict.fromkeys(
                    idx for idx in to_delete if idx in valid_row_indices
                )
            )
            skipped = len(to_delete) - len(indices)
            if skipped:
                logger.warning(
                    "AI Qualify skipped %d stale row index(es) for delete",
                    skipped,
                )
            if indices:
                sheets.delete_rows(sheet, indices)
            to_delete.clear()

        def _queue_sheet_update(row_index: int, updates: dict[str, Any]) -> None:
            merged = pending_sheet_updates.setdefault(row_index, {})
            merged.update(updates)
            if len(pending_sheet_updates) >= _SHEET_FLUSH_ROWS:
                _flush_sheet_updates()

        def _emit(message: str | None = None) -> None:
            if progress_callback is not None:
                progress_callback(dict(stats), message)

        if stats["total"]:
            _emit(f"Qualifying — 0/{stats['total']} leads")

        try:
            for row_index, row in candidates:
                sheet_status = self._sheet_status_value(row)
                if sheet_status in _REMOVE_STATUSES:
                    stats["removed"] += 1
                    self._incr_removed_website_stat(stats, sheet_status)
                    to_delete.append(row_index)
                    stats["processed"] += 1
                    _emit(
                        f"Qualifying — {stats['processed']}/{stats['total']} "
                        f"({stats['kept']} kept, {stats['removed']} removed)"
                    )
                    continue

                already_qualified = self._is_qualified(row)
                try:
                    evaluation = self._evaluate_row(row, already_qualified=already_qualified)
                except Exception as exc:
                    logger.warning("Row %s qualify failed: %s", row_index, exc)
                    stats["errors"] += 1
                    stats["processed"] += 1
                    _emit(
                        f"Qualifying — {stats['processed']}/{stats['total']} "
                        f"({stats['errors']} error(s))"
                    )
                    continue

                label = row.get(COL_BUSINESS_NAME) or row_index
                decision = evaluation.decision
                website_status = evaluation.website_status
                status_results.append(website_status)
                website_link = str(row.get(COL_WEBSITE_LINK) or "").strip()

                row_updates = dict(website_status.as_row_fields())
                if decision.keep and not already_qualified:
                    row_updates[COL_VA] = "qualified"
                _queue_sheet_update(row_index, row_updates)

                logger.info(
                    "Website status %s | %s | %s | qualified=%s | %s",
                    label,
                    website_link,
                    website_status.status.value,
                    website_status.qualified,
                    website_status.reason,
                )

                if decision.keep:
                    stats["kept"] += 1
                    logger.info("Qualified row %s (%s): %s", row_index, label, decision.reason)
                else:
                    stats["removed"] += 1
                    if already_qualified:
                        stats["requalified_removed"] += 1
                    to_delete.append(row_index)
                    reason_lower = decision.reason.lower()
                    if "no phone" in reason_lower or "incomplete" in reason_lower:
                        stats["removed_no_phone"] += 1
                        if "incomplete" in reason_lower:
                            stats["removed_incomplete"] += 1
                    elif website_status.status == WebsiteStatusCode.ACTIVE:
                        stats["removed_active_website"] += 1
                    elif website_status.status == WebsiteStatusCode.BUSINESS_WEBSITE_REDIRECT:
                        stats["removed_business_redirect"] += 1
                    elif website_status.status == WebsiteStatusCode.PARKED:
                        stats["removed_parked"] += 1
                    logger.info("Rejected row %s (%s): %s", row_index, label, decision.reason)

                stats["processed"] += 1
                _emit(
                    f"Qualifying — {stats['processed']}/{stats['total']} "
                    f"({stats['kept']} kept, {stats['removed']} removed)"
                )

            _flush_sheet_updates()
            _flush_deletes()
            sweep_counts = self._final_sweep_remove_leads(sheet)
            if any(sweep_counts.values()):
                self._apply_sweep_stats(stats, sweep_counts)
        except sheets.SheetsError as exc:
            stats["website_status_counts"] = status_counts(status_results)
            msg = str(exc)
            logger.warning("AI Qualify paused on Sheets error: %s", msg)
            try:
                _flush_sheet_updates()
                _flush_deletes()
                sweep_counts = self._final_sweep_remove_leads(sheet)
                if any(sweep_counts.values()):
                    self._apply_sweep_stats(stats, sweep_counts)
            except sheets.SheetsError as cleanup_exc:
                logger.warning(
                    "AI Qualify cleanup after Sheets error also failed: %s",
                    cleanup_exc,
                )
            return QualifyResult(ok=False, message=msg, stats=stats)

        stats["website_status_counts"] = status_counts(status_results)

        message = (
            f"Qualified {stats['kept']} lead(s), removed {stats['removed']} from {sheet}"
        )
        if stats.get("sweep_removed_active") or stats.get("sweep_removed_redirect") or stats.get(
            "sweep_removed_parked"
        ):
            parts = []
            if stats.get("sweep_removed_active"):
                parts.append(f"{stats['sweep_removed_active']} ACTIVE sweep")
            if stats.get("sweep_removed_redirect"):
                parts.append(f"{stats['sweep_removed_redirect']} redirect sweep")
            if stats.get("sweep_removed_parked"):
                parts.append(f"{stats['sweep_removed_parked']} PARKED sweep")
            message += f" ({', '.join(parts)})"
        if stats["requalified_removed"]:
            message += f" ({stats['requalified_removed']} wrongly qualified removed)"
        if stats["errors"]:
            message += f" ({stats['errors']} error(s))"

        return QualifyResult(ok=True, message=message, stats=stats)

    def _is_qualified(self, row: dict[str, Any]) -> bool:
        return str(row.get(COL_VA) or "").strip().lower() == "qualified"

    def _sheet_status_value(self, row: dict[str, Any]) -> str:
        return str(row.get(COL_WEBSITE_STATUS) or "").strip().upper()

    def _incr_removed_website_stat(self, stats: dict[str, Any], sheet_status: str) -> None:
        if sheet_status == WebsiteStatusCode.ACTIVE.value:
            stats["removed_active_website"] += 1
        elif sheet_status == WebsiteStatusCode.BUSINESS_WEBSITE_REDIRECT.value:
            stats["removed_business_redirect"] += 1
        elif sheet_status == WebsiteStatusCode.PARKED.value:
            stats["removed_parked"] += 1

    def _apply_sweep_stats(self, stats: dict[str, Any], sweep_counts: dict[str, int]) -> None:
        active = sweep_counts.get("active", 0)
        redirect = sweep_counts.get("redirect", 0)
        parked = sweep_counts.get("parked", 0)
        if active:
            stats["sweep_removed_active"] = active
        if redirect:
            stats["sweep_removed_redirect"] = redirect
        if parked:
            stats["sweep_removed_parked"] = parked
        stats["removed"] += active + redirect + parked
        stats["removed_active_website"] += active
        stats["removed_business_redirect"] += redirect
        stats["removed_parked"] += parked

    def _final_sweep_remove_leads(self, sheet: str) -> dict[str, int]:
        """Delete rows still marked ACTIVE / redirect / PARKED."""
        sheets.invalidate_worksheet_cache(sheet)
        rows = sheets.read_rows_with_sheet_indices(sheet, use_cache=False)
        to_remove: list[int] = []
        counts = {"active": 0, "redirect": 0, "parked": 0}
        for row_index, row in rows:
            sheet_status = self._sheet_status_value(row)
            if sheet_status == WebsiteStatusCode.ACTIVE.value:
                counts["active"] += 1
                to_remove.append(row_index)
            elif sheet_status == WebsiteStatusCode.BUSINESS_WEBSITE_REDIRECT.value:
                counts["redirect"] += 1
                to_remove.append(row_index)
            elif sheet_status == WebsiteStatusCode.PARKED.value:
                counts["parked"] += 1
                to_remove.append(row_index)
        if not to_remove:
            return counts
        logger.info(
            "AI Qualify final sweep removing %d row(s) (%d ACTIVE, %d redirect, %d PARKED)",
            len(to_remove),
            counts["active"],
            counts["redirect"],
            counts["parked"],
        )
        sheets.delete_rows(sheet, to_remove)
        return counts

    def _candidate_rows(
        self, rows: list[tuple[int, dict[str, Any]]]
    ) -> list[tuple[int, dict[str, Any]]]:
        candidates: list[tuple[int, dict[str, Any]]] = []
        for row_index, row in rows:
            if self._is_unscraped_new_lead(row):
                continue
            candidates.append((row_index, row))
        return candidates

    def _is_unscraped_new_lead(self, row: dict[str, Any]) -> bool:
        scrape = str(row.get(COL_SCRAPE) or "").strip()
        refined = str(row.get(COL_REFINED) or "").strip()
        if scrape or refined:
            return False

        activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
        if not activity or activity == LEAD_ACTIVITY_PENDING:
            return True
        return False

    def _phones_from_text(self, text: str) -> list[str]:
        phones: list[str] = []
        seen: set[str] = set()
        for match in _PHONE_IN_TEXT.finditer(text or ""):
            normalized = normalize_uk_phone(match.group(0))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            phones.append(normalized)
        return phones

    def _row_phones(self, row: dict[str, Any], scrape_text: str = "") -> tuple[str, str]:
        phone1 = str(row.get(COL_PHONE_1) or "").strip()
        phone2 = str(row.get(COL_PHONE_2) or "").strip()

        if phone1 or phone2:
            return phone1, phone2

        refined = str(row.get(COL_REFINED) or "")
        match = _PHONE_BULLET.search(refined)
        if match:
            raw = match.group(1).strip()
            if raw.lower() != "notfound":
                return raw, ""

        for normalized in self._phones_from_text(scrape_text):
            return normalized, ""

        return phone1, phone2

    def _classify_website(self, row: dict[str, Any]) -> Any:
        website_link = str(row.get(COL_WEBSITE_LINK) or "").strip()
        return classify_website_link(
            website_link,
            timeout=self.settings.qualify_website_timeout_secs,
            max_redirects=self.settings.qualify_max_redirects,
            retries=self.settings.qualify_fetch_retries,
        )

    def _evaluate_row(
        self, row: dict[str, Any], *, already_qualified: bool = False
    ) -> RowEvaluation:
        scrape_text = str(row.get(COL_SCRAPE) or "").strip()
        status = self._classify_website(row)

        if not status.qualified:
            if status.status in REMOVE_LEAD_STATUSES:
                label = status.status.value.lower().replace("_", " ")
                return RowEvaluation(
                    decision=RowDecision(
                        keep=False,
                        reason=f"{label} — {status.reason}",
                        website_status=status.status.value,
                    ),
                    website_status=status,
                )

        if already_qualified:
            return RowEvaluation(
                decision=RowDecision(
                    keep=True,
                    reason="already qualified — website status allows lead",
                    website_status=status.status.value,
                ),
                website_status=status,
            )

        phone1, phone2 = self._row_phones(row, scrape_text)
        refined_text = str(row.get(COL_REFINED) or "").strip()
        if not phone1 and not phone2:
            if scrape_text and not refined_text:
                return RowEvaluation(
                    decision=RowDecision(
                        keep=False,
                        reason="incomplete — scraped but not refined and no phone",
                        website_status=status.status.value,
                    ),
                    website_status=status,
                )
            return RowEvaluation(
                decision=RowDecision(
                    keep=False,
                    reason="no phone number",
                    website_status=status.status.value,
                ),
                website_status=status,
            )

        return RowEvaluation(
            decision=RowDecision(
                keep=True,
                reason=f"business with phone, website status {status.status.value}",
                website_status=status.status.value,
            ),
            website_status=status,
        )


def get_ai_qualify_service(settings: Settings | None = None) -> AIQualifyService:
    from app.config import get_settings

    return AIQualifyService(settings or get_settings())
