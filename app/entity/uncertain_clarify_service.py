"""Clarify entity_uncertain leads after refine using scrape + refined text."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import sheets
from app.config import Settings, get_settings
from app.entity.classifier_batch import ClassifyBatchError, EntityLeadInput, classify_entities_batch
from app.entity.constants import (
    LEAD_ACTIVITY_ENTITY_BUSINESS,
    LEAD_ACTIVITY_ENTITY_UNCERTAIN,
)
from app.qualify.personal import is_personal_profile_text
from app.sheets.columns import (
    COL_BUSINESS_NAME,
    COL_FACEBOOK_LINK,
    COL_LEAD_ACTIVITY,
    COL_REFINED,
    COL_SCRAPE,
    DYNAMIC_LEAD_HEADERS,
)

logger = logging.getLogger(__name__)

_BUSINESS_TAG_THRESHOLD = 0.85
_CLARIFY_AI_BATCH_SECS = 28.0
_CLARIFY_HEURISTIC_SECS = 0.5

ProgressCallback = Callable[[dict[str, Any], str | None], None]


@dataclass
class UncertainClarifyResult:
    ok: bool
    message: str
    stats: dict[str, Any]


class EntityUncertainClarifyService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self,
        progress_callback: ProgressCallback | None = None,
    ) -> UncertainClarifyResult:
        if not self.settings.sheets_configured:
            return UncertainClarifyResult(
                ok=False, message="Service account JSON not found", stats={}
            )
        if not self.settings.openrouter_configured:
            return UncertainClarifyResult(
                ok=False, message="OPENROUTER_API_KEY not set in .env", stats={}
            )

        sheet = self.settings.sheet_dynamic_lead
        sheets.ensure_worksheet(sheet, DYNAMIC_LEAD_HEADERS)

        try:
            rows = sheets.read_all_with_row_indices(sheet)
        except sheets.SheetsError as exc:
            return UncertainClarifyResult(ok=False, message=str(exc), stats={})

        stats: dict[str, Any] = {
            "candidates": 0,
            "removed_heuristic": 0,
            "removed_ai": 0,
            "tagged_business": 0,
            "still_uncertain": 0,
            "skipped_not_uncertain": 0,
            "errors": 0,
            "batches": 0,
            "processed": 0,
            "total": 0,
        }
        to_delete: list[int] = []
        tag_updates: dict[int, str] = {}
        ai_queue: list[tuple[int, dict[str, Any]]] = []
        batch_size = self.settings.entity_classify_batch_size
        person_threshold = self.settings.entity_classify_auto_person

        def _emit(message: str | None = None) -> None:
            if progress_callback is not None:
                progress_callback(dict(stats), message)

        for row_index, row in rows:
            link = str(row.get(COL_FACEBOOK_LINK) or "").strip()
            if not link:
                continue

            activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
            if activity != LEAD_ACTIVITY_ENTITY_UNCERTAIN:
                stats["skipped_not_uncertain"] += 1
                continue

            scrape_text = str(row.get(COL_SCRAPE) or "").strip()
            refined_text = str(row.get(COL_REFINED) or "").strip()
            stats["candidates"] += 1
            stats["total"] += 1
            name = str(row.get(COL_BUSINESS_NAME) or "").strip()

            if is_personal_profile_text(scrape_text) or is_personal_profile_text(refined_text):
                to_delete.append(row_index)
                stats["removed_heuristic"] += 1
                stats["processed"] += 1
                logger.info(
                    "Uncertain clarify heuristic person row %s (%s)",
                    row_index,
                    name,
                )
                continue

            ai_queue.append((row_index, row))

        stats["per_item_estimate_secs"] = _CLARIFY_HEURISTIC_SECS
        if stats["total"]:
            _emit(f"Entity clarify — {stats['processed']}/{stats['total']} uncertain leads")

        if ai_queue:
            stats["per_item_estimate_secs"] = max(
                _CLARIFY_AI_BATCH_SECS / max(batch_size, 1), 1.5
            )
            _emit(
                f"Entity clarify AI — {stats['processed']}/{stats['total']} "
                f"({len(ai_queue)} batched)"
            )

        for start in range(0, len(ai_queue), batch_size):
            batch = ai_queue[start : start + batch_size]
            stats["batches"] += 1
            stats["current_row"] = batch[0][0]
            inputs = [
                EntityLeadInput(
                    row_index=row_index,
                    business_name=str(row.get(COL_BUSINESS_NAME) or ""),
                    facebook_link=str(row.get(COL_FACEBOOK_LINK) or ""),
                    scrape_text=str(row.get(COL_SCRAPE) or ""),
                    refined_text=str(row.get(COL_REFINED) or ""),
                )
                for row_index, row in batch
            ]
            try:
                results = classify_entities_batch(
                    inputs,
                    mode="full",
                    api_key=self.settings.openrouter_api_key,
                    model=self.settings.openrouter_model,
                    base_url=self.settings.openrouter_base_url,
                )
            except ClassifyBatchError as exc:
                logger.warning("Uncertain clarify batch failed: %s", exc)
                stats["errors"] += len(batch)
                for row_index, _row in batch:
                    stats["still_uncertain"] += 1
                    stats["processed"] += 1
                _emit(
                    f"Entity clarify — {stats['processed']}/{stats['total']} "
                    f"(batch error)"
                )
                continue

            result_map = {r.row_index: r for r in results}
            for row_index, row in batch:
                stats["current_row"] = row_index
                name = str(row.get(COL_BUSINESS_NAME) or "")
                result = result_map.get(row_index)
                stats["processed"] += 1

                if result is None:
                    stats["still_uncertain"] += 1
                    continue

                if (
                    result.entity_type == "person"
                    and result.confidence >= person_threshold
                ):
                    to_delete.append(row_index)
                    stats["removed_ai"] += 1
                    logger.info(
                        "Uncertain clarify AI person row %s (%s): %s",
                        row_index,
                        name,
                        result.reason,
                    )
                elif (
                    result.entity_type == "business"
                    and result.confidence >= _BUSINESS_TAG_THRESHOLD
                ):
                    tag_updates[row_index] = LEAD_ACTIVITY_ENTITY_BUSINESS
                    stats["tagged_business"] += 1
                    logger.info(
                        "Uncertain clarify business row %s (%s): %s",
                        row_index,
                        name,
                        result.reason,
                    )
                else:
                    stats["still_uncertain"] += 1
                    logger.info(
                        "Uncertain clarify still uncertain row %s (%s): %s",
                        row_index,
                        name,
                        result.reason or "low confidence",
                    )

            _emit(
                f"Entity clarify — {stats['processed']}/{stats['total']} "
                f"({stats['tagged_business']} business, "
                f"{stats['removed_heuristic'] + stats['removed_ai']} personal removed)"
            )

        if tag_updates:
            sheets.batch_update_lead_activity(sheet, tag_updates)

        if to_delete:
            sheets.delete_rows(sheet, to_delete)
            sheets.invalidate_worksheet_cache(sheet)

        removed = stats["removed_heuristic"] + stats["removed_ai"]
        if stats["candidates"] == 0:
            message = "No entity_uncertain leads to clarify"
        else:
            message = (
                f"Clarified {stats['candidates']} uncertain lead(s): "
                f"tagged {stats['tagged_business']} business, "
                f"removed {removed} personal, "
                f"{stats['still_uncertain']} still uncertain"
            )
        if stats["errors"]:
            message += f" ({stats['errors']} batch error(s))"

        return UncertainClarifyResult(ok=True, message=message, stats=stats)


def get_entity_uncertain_clarify_service(
    settings: Settings | None = None,
) -> EntityUncertainClarifyService:
    return EntityUncertainClarifyService(settings or get_settings())
