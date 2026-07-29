"""ProfileRefinement — Scrape column → refined column on Dynamic Lead Sheet."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import sheets
from app.config import Settings
from app.refinement.cleaner import clean_profile_text
from app.refinement.emails import resolve_profile_email
from app.refinement.extractor import ProfileExtractorError, extract_profile_fields
from app.refinement.format import format_refined_text
from app.refinement.phones import format_uk_phone_display, normalize_uk_phone
from app.scrape_queue.verify import verify_scrape_matches_business
from app.sheets.columns import (
    COL_BUSINESS_NAME,
    COL_BUSINESS_OWNER,
    COL_LEAD_ACTIVITY,
    COL_PHONE_1,
    COL_PHONE_2,
    COL_REFINED,
    COL_SCRAPE,
    COL_WEBSITE_LINK,
    DYNAMIC_LEAD_HEADERS,
    scrape_failed_activity_label,
)

logger = logging.getLogger(__name__)

_REFINE_PER_ROW_SECS = 10.0

ProgressCallback = Callable[[dict[str, Any], str | None], None]


@dataclass
class RefineResult:
    ok: bool
    message: str
    stats: dict[str, Any]


@dataclass
class RefinedFields:
    phone1: str
    phone2: str
    business_owner: str
    website_link: str
    refined_text: str


class ProfileRefinementService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def ensure_sheets(self) -> None:
        sheets.ensure_worksheet(
            self.settings.sheet_dynamic_lead,
            DYNAMIC_LEAD_HEADERS,
        )

    def run(
        self,
        progress_callback: ProgressCallback | None = None,
    ) -> RefineResult:
        if not self.settings.sheets_configured:
            return RefineResult(
                ok=False,
                message="Service account JSON not found",
                stats={},
            )
        if not self.settings.openrouter_configured:
            return RefineResult(
                ok=False,
                message="OPENROUTER_API_KEY not set in .env",
                stats={},
            )

        self.ensure_sheets()
        sheet = self.settings.sheet_dynamic_lead

        try:
            source_rows = sheets.read_all_with_row_indices(sheet)
        except sheets.SheetsError as exc:
            return RefineResult(ok=False, message=str(exc), stats={})

        pending = self._pending_rows(source_rows)

        stats: dict[str, Any] = {
            "source_rows": len(source_rows),
            "pending": len(pending),
            "total": len(pending),
            "processed": 0,
            "updated": 0,
            "skipped_empty": 0,
            "name_mismatch": 0,
            "errors": 0,
            "batches": 0,
            "per_item_estimate_secs": _REFINE_PER_ROW_SECS,
        }

        def _emit(message: str | None = None) -> None:
            if progress_callback is not None:
                progress_callback(dict(stats), message)

        if not pending:
            return RefineResult(
                ok=True,
                message="No rows need refinement (Scrape filled, refined empty)",
                stats=stats,
            )

        batch_size = max(1, self.settings.refine_batch_size)
        batch_count = 0
        _emit(f"Refining — 0/{stats['total']} profiles")

        for row_index, row in pending:
            scrape_text = str(row.get(COL_SCRAPE) or "").strip()
            if not scrape_text:
                stats["skipped_empty"] += 1
                continue

            business_name = str(row.get(COL_BUSINESS_NAME) or "").strip()

            if business_name:
                name_check = verify_scrape_matches_business(scrape_text, business_name)
                if not name_check.ok:
                    stats["name_mismatch"] += 1
                    logger.warning(
                        "Row %s scrape rejected — %s",
                        row_index,
                        name_check.reason,
                    )
                    sheets.update_row_by_header(
                        sheet,
                        row_index,
                        {
                            COL_SCRAPE: "",
                            COL_LEAD_ACTIVITY: scrape_failed_activity_label(1),
                        },
                    )
                    continue

            try:
                refined = self._refine_one(
                    business_name=business_name,
                    scrape_text=scrape_text,
                )
            except ProfileExtractorError as exc:
                logger.warning("Row %s extraction failed: %s", row_index, exc)
                stats["errors"] += 1
                continue

            stats["processed"] += 1
            batch_count += 1

            sheets.update_row_by_header(
                sheet,
                row_index,
                {
                    COL_REFINED: refined.refined_text,
                    COL_PHONE_1: refined.phone1,
                    COL_PHONE_2: refined.phone2,
                    COL_BUSINESS_OWNER: refined.business_owner,
                    COL_WEBSITE_LINK: refined.website_link,
                },
            )
            stats["updated"] += 1
            _emit(f"Refining — {stats['processed']}/{stats['total']} profiles")

            if batch_count >= batch_size:
                stats["batches"] += 1
                batch_count = 0

        if batch_count > 0:
            stats["batches"] += 1

        message = (
            f"Refined {stats['updated']} row(s) on {sheet} "
            f"({stats['batches']} batch(es))"
        )
        if stats["errors"]:
            message += f" ({stats['errors']} extraction error(s))"
        if stats["name_mismatch"]:
            message += f" ({stats['name_mismatch']} wrong scrape(s) cleared)"

        return RefineResult(ok=True, message=message, stats=stats)

    def _pending_rows(
        self,
        source_rows: list[tuple[int, dict[str, Any]]],
    ) -> list[tuple[int, dict[str, Any]]]:
        pending: list[tuple[int, dict[str, Any]]] = []
        for row_index, row in source_rows:
            scrape_text = str(row.get(COL_SCRAPE) or "").strip()
            refined = str(row.get(COL_REFINED) or "").strip()
            if scrape_text and not refined:
                pending.append((row_index, row))
        return pending

    def _refine_one(
        self,
        *,
        business_name: str,
        scrape_text: str,
    ) -> RefinedFields:
        cleaned = clean_profile_text(scrape_text)
        extracted = extract_profile_fields(
            cleaned,
            api_key=self.settings.openrouter_api_key,
            model=self.settings.openrouter_model,
            base_url=self.settings.openrouter_base_url,
            business_name=business_name,
        )

        phone1 = normalize_uk_phone(extracted.phone1)
        phone2 = normalize_uk_phone(extracted.phone2)
        if phone2 and phone2 == phone1:
            phone2 = ""

        phone_display = format_uk_phone_display(phone1)
        if phone2:
            phone_display = (
                f"{phone_display}, {format_uk_phone_display(phone2)}"
                if phone_display
                else format_uk_phone_display(phone2)
            )

        display_name = business_name or extracted.business_name

        email = resolve_profile_email(
            extracted.email,
            scrape_text=scrape_text,
            website_link=extracted.website_link,
        )

        refined_text = format_refined_text(
            business_name=display_name,
            business_type=extracted.business_type,
            location=extracted.location,
            phone=phone_display,
            email=email,
            description=extracted.description,
        )

        return RefinedFields(
            phone1=phone1,
            phone2=phone2,
            business_owner=extracted.business_owner,
            website_link=extracted.website_link,
            refined_text=refined_text,
        )


def get_profile_refinement(settings: Settings | None = None) -> ProfileRefinementService:
    from app.config import get_settings

    return ProfileRefinementService(settings or get_settings())
