"""Phase 2 entity classify — post-scrape batched AI + heuristics."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import sheets
from app.config import Settings, get_settings
from app.entity.classifier_batch import ClassifyBatchError, EntityLeadInput, classify_entities_batch
from app.entity.constants import LEAD_ACTIVITY_ENTITY_BUSINESS
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

_CLASSIFY_AI_BATCH_SECS = 28.0
_CLASSIFY_HEURISTIC_SECS = 0.5

ProgressCallback = Callable[[dict[str, Any], str | None], None]


@dataclass
class ClassifyRunResult:
    ok: bool
    message: str
    stats: dict[str, Any]


class EntityClassifyService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _has_scrape_data(self, row: dict[str, Any]) -> bool:
        scrape = str(row.get(COL_SCRAPE) or "").strip()
        return len(scrape) >= self.settings.scrape_min_length

    def run(
        self,
        progress_callback: ProgressCallback | None = None,
    ) -> ClassifyRunResult:
        if not self.settings.sheets_configured:
            return ClassifyRunResult(ok=False, message="Service account JSON not found", stats={})
        if not self.settings.openrouter_configured:
            return ClassifyRunResult(
                ok=False, message="OPENROUTER_API_KEY not set in .env", stats={}
            )

        sheet = self.settings.sheet_dynamic_lead
        sheets.ensure_worksheet(sheet, DYNAMIC_LEAD_HEADERS)

        try:
            rows = sheets.read_all_with_row_indices(sheet)
        except sheets.SheetsError as exc:
            return ClassifyRunResult(ok=False, message=str(exc), stats={})

        stats: dict[str, Any] = {
            "candidates": 0,
            "removed_heuristic": 0,
            "removed_ai": 0,
            "kept_business": 0,
            "skipped_no_scrape": 0,
            "skipped_confident_business": 0,
            "errors": 0,
            "batches": 0,
            "processed": 0,
        }
        to_delete: list[int] = []
        ai_queue: list[tuple[int, dict[str, Any]]] = []
        batch_size = self.settings.entity_classify_batch_size
        threshold = self.settings.entity_classify_auto_person

        def _emit(message: str | None = None) -> None:
            if progress_callback is not None:
                progress_callback(dict(stats), message)

        for row_index, row in rows:
            link = str(row.get(COL_FACEBOOK_LINK) or "").strip()
            if not link:
                continue

            scrape_text = str(row.get(COL_SCRAPE) or "").strip()
            refined_text = str(row.get(COL_REFINED) or "").strip()
            activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()

            if not scrape_text and not self._has_scrape_data(row):
                stats["skipped_no_scrape"] += 1
                continue

            stats["candidates"] += 1
            name = str(row.get(COL_BUSINESS_NAME) or "").strip()

            if is_personal_profile_text(scrape_text) or is_personal_profile_text(refined_text):
                to_delete.append(row_index)
                stats["removed_heuristic"] += 1
                stats["processed"] += 1
                logger.info(
                    "Entity classify heuristic person row %s (%s)",
                    row_index,
                    name,
                )
                continue

            if activity == LEAD_ACTIVITY_ENTITY_BUSINESS:
                stats["skipped_confident_business"] += 1
                stats["kept_business"] += 1
                stats["processed"] += 1
                continue

            ai_queue.append((row_index, row))

        stats["total"] = stats["candidates"]
        stats["per_item_estimate_secs"] = _CLASSIFY_HEURISTIC_SECS
        if stats["total"]:
            _emit(f"Entity classify — {stats['processed']}/{stats['total']} leads")

        if ai_queue:
            stats["per_item_estimate_secs"] = max(
                _CLASSIFY_AI_BATCH_SECS / max(batch_size, 1), 1.5
            )
            _emit(
                f"Entity classify AI — {stats['processed']}/{stats['total']} "
                f"({len(ai_queue)} batched)"
            )

        for start in range(0, len(ai_queue), batch_size):
            batch = ai_queue[start : start + batch_size]
            stats["batches"] += 1
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
                logger.warning("Entity classify batch failed: %s", exc)
                stats["errors"] += len(batch)
                stats["kept_business"] += len(batch)
                stats["processed"] += len(batch)
                _emit(
                    f"Entity classify — {stats['processed']}/{stats['total']} "
                    f"(batch error)"
                )
                continue

            result_map = {r.row_index: r for r in results}
            removed_in_batch = 0
            for row_index, row in batch:
                name = str(row.get(COL_BUSINESS_NAME) or "")
                result = result_map.get(row_index)
                stats["processed"] += 1
                if result is None:
                    stats["kept_business"] += 1
                    continue

                if result.entity_type == "person" and result.confidence >= threshold:
                    to_delete.append(row_index)
                    stats["removed_ai"] += 1
                    removed_in_batch += 1
                    logger.info(
                        "Entity classify AI person row %s (%s): %s",
                        row_index,
                        name,
                        result.reason,
                    )
                else:
                    stats["kept_business"] += 1

            logger.info(
                "Entity classify batch %s — %s person removed of %s",
                stats["batches"],
                removed_in_batch,
                len(batch),
            )
            _emit(
                f"Entity classify — {stats['processed']}/{stats['total']} "
                f"({stats['removed_heuristic'] + stats['removed_ai']} personal removed)"
            )

        if to_delete:
            sheets.delete_rows(sheet, to_delete)
            sheets.invalidate_worksheet_cache(sheet)

        removed = stats["removed_heuristic"] + stats["removed_ai"]
        message = (
            f"Classified {stats['candidates']} scraped lead(s): "
            f"removed {removed} personal, kept {stats['kept_business']} business"
        )
        if stats["errors"]:
            message += f" ({stats['errors']} batch error(s))"

        return ClassifyRunResult(ok=True, message=message, stats=stats)


def get_entity_classify_service(settings: Settings | None = None) -> EntityClassifyService:
    return EntityClassifyService(settings or get_settings())
