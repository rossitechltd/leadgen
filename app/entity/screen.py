"""Phase 1 entity screen — heuristics + batched name/link AI."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import sheets
from app.config import Settings, get_settings
from app.entity.classifier_batch import ClassifyBatchError, EntityLeadInput, classify_entities_batch
from app.entity.constants import (
    LEAD_ACTIVITY_ENTITY_BUSINESS,
    LEAD_ACTIVITY_ENTITY_UNCERTAIN,
    LEAD_ACTIVITY_PENDING_SCRAPE,
)
from app.entity.heuristics import heuristic_screen
from app.notifications.telegram import notify_attention
from app.operator_attention import add_attention_item, remove_attention_by_kind
from app.scrapers.lead_mapping import normalize_facebook_url
from app.sheets.columns import (
    COL_FACEBOOK_LINK,
    COL_BUSINESS_NAME,
    COL_LEAD_ACTIVITY,
    DYNAMIC_LEAD_HEADERS,
    LEAD_ACTIVITY_SCRAPED,
    LEAD_ACTIVITY_SCRAPING,
    is_scrape_failed_activity,
)

logger = logging.getLogger(__name__)

_QUOTA_WAIT_MAX_SECS = 300.0
_BUSINESS_TAG_THRESHOLD = 0.85
_SCREEN_AI_BATCH_SECS = 22.0
_SCREEN_HEURISTIC_SECS = 0.4
_PROGRESS_EMIT_INTERVAL = 1

# Lead Activity values that mean entity screen already ran for this row.
_SCREENED_TAGS = frozenset(
    {LEAD_ACTIVITY_ENTITY_BUSINESS, LEAD_ACTIVITY_ENTITY_UNCERTAIN}
)
_ENTITY_TAGS = _SCREENED_TAGS


def _needs_reconcile_tag(activity: str) -> bool:
    """Only fix rows never entity-screened (pending_scrape etc.), not re-tag classified rows."""
    act = (activity or "").strip().lower()
    if act in _SCREENED_TAGS:
        return False
    if act in {
        "",
        LEAD_ACTIVITY_PENDING_SCRAPE,
        LEAD_ACTIVITY_SCRAPING.lower(),
        LEAD_ACTIVITY_SCRAPED.lower(),
    }:
        return True
    if is_scrape_failed_activity(act):
        return True
    return False


ProgressCallback = Callable[[dict[str, Any], str | None], None]


@dataclass
class ScreenRunResult:
    ok: bool
    message: str
    stats: dict[str, Any]


_QUOTA_ATTENTION_KIND = "entity-screen-quota"


class EntityScreenService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._quota_pause_alerted = False

    def _alert_quota_pause(self, detail: str) -> None:
        if self._quota_pause_alerted:
            return
        self._quota_pause_alerted = True
        body = (
            f"{detail}\n"
            "Google Sheets quota hit — Step 3 will retry automatically."
        )
        try:
            add_attention_item(
                kind=_QUOTA_ATTENTION_KIND,
                title="Entity Screen paused (Sheets quota)",
                body=body,
                item_id=_QUOTA_ATTENTION_KIND,
            )
            notify_attention(
                "Entity Screen paused — Sheets quota",
                body,
                context="Step 3 Entity Screen",
            )
        except Exception as exc:
            logger.warning("Entity screen quota alert failed: %s", exc)

    def _clear_quota_alert(self) -> None:
        self._quota_pause_alerted = False
        remove_attention_by_kind(_QUOTA_ATTENTION_KIND)

    def _checkpoint_path(self) -> Path:
        return self.settings.scrape_state_path.parent / "entity_screen_pending.json"

    def _save_checkpoint(
        self,
        sheet: str,
        pending_tags: dict[int, str],
        to_delete_links: set[str],
        stats: dict[str, Any],
        *,
        tags_applied: bool = False,
    ) -> None:
        path = self._checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "sheet_name": sheet,
                    "pending_tags": {str(k): v for k, v in pending_tags.items()},
                    "to_delete_links": sorted(to_delete_links),
                    "stats": stats,
                    "tags_applied": tags_applied,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _load_checkpoint(self) -> dict[str, Any] | None:
        path = self._checkpoint_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("sheet_name"):
                return raw
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.warning("Invalid entity screen checkpoint %s: %s", path, exc)
        return None

    def _clear_checkpoint(self) -> None:
        path = self._checkpoint_path()
        if path.exists():
            path.unlink()

    def _wait_for_quota(self, max_secs: float = _QUOTA_WAIT_MAX_SECS) -> bool:
        deadline = time.monotonic() + max_secs
        while sheets.is_quota_cooldown() and time.monotonic() < deadline:
            secs = sheets.quota_cooldown_remaining_secs()
            self._alert_quota_pause(
                f"Waiting for Sheets quota ({secs:.0f}s remaining)."
            )
            logger.info("Entity screen waiting for Sheets quota (%.0fs left)", secs)
            time.sleep(min(secs + 1.0, 30.0))
        return not sheets.is_quota_cooldown()

    def _with_quota_retry(
        self,
        operation: Callable[[], Any],
        *,
        label: str,
        max_attempts: int = 8,
    ) -> Any:
        for attempt in range(max_attempts):
            if sheets.is_quota_cooldown():
                if not self._wait_for_quota():
                    raise sheets.SheetsError(
                        "Sheets API quota exceeded — cooling down"
                    )
            try:
                return operation()
            except sheets.SheetsError as exc:
                coerced = sheets.coerce_quota_error(exc)
                if coerced is not None and attempt < max_attempts - 1:
                    logger.warning(
                        "Entity screen %s hit quota (attempt %s/%s)",
                        label,
                        attempt + 1,
                        max_attempts,
                    )
                    if not self._wait_for_quota():
                        raise coerced
                    continue
                raise
        raise sheets.SheetsError("Sheets API quota exceeded — cooling down")

    def _apply_activity_tags(
        self,
        sheet: str,
        activity_updates: dict[int, str],
    ) -> None:
        if not activity_updates:
            return
        self._with_quota_retry(
            lambda: sheets.batch_update_lead_activity(sheet, activity_updates),
            label="activity tags",
        )

    def _delete_person_rows(
        self,
        sheet: str,
        row_indices: list[int],
        *,
        max_row: int,
    ) -> None:
        if not row_indices:
            return
        self._with_quota_retry(
            lambda: sheets.delete_rows(sheet, row_indices, max_row=max_row),
            label="person row deletes",
        )

    def _reconcile_into_pending(
        self,
        rows: list[tuple[int, dict[str, Any]]],
        pending_tags: dict[int, str],
        to_delete_links: set[str],
        stats: dict[str, Any],
    ) -> None:
        """Tag stragglers in memory before the single finalize tag write."""
        fixes = 0
        for row_index, row in rows:
            link = str(row.get(COL_FACEBOOK_LINK) or "").strip()
            if not link:
                continue
            norm = normalize_facebook_url(link)
            if norm in to_delete_links:
                continue
            if row_index in pending_tags:
                continue
            activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip()
            if not _needs_reconcile_tag(activity):
                continue
            pending_tags[row_index] = LEAD_ACTIVITY_ENTITY_UNCERTAIN
            stats["reconciled_uncertain"] += 1
            stats["tagged_uncertain"] += 1
            fixes += 1
        if fixes:
            logger.info(
                "Entity screen reconciled %s untagged row(s) as entity_uncertain",
                fixes,
            )

    def _apply_sheet_results(
        self,
        sheet: str,
        initial_rows: list[tuple[int, dict[str, Any]]],
        stats: dict[str, Any],
        pending_tags: dict[int, str],
        to_delete_links: set[str],
        *,
        tags_already_applied: bool = False,
    ) -> None:
        """Single finalize: reconcile in memory, one tag batch, link-resolved deletes."""
        if not tags_already_applied:
            self._reconcile_into_pending(
                initial_rows, pending_tags, to_delete_links, stats
            )

            if pending_tags:
                self._apply_activity_tags(sheet, pending_tags)

            self._save_checkpoint(
                sheet,
                {},
                to_delete_links,
                stats,
                tags_applied=True,
            )

        fresh_rows = self._with_quota_retry(
            lambda: sheets.read_rows_with_sheet_indices(sheet, use_cache=False),
            label="finalize read",
        )
        link_to_row: dict[str, int] = {}
        max_row = 1
        for row_index, row in fresh_rows:
            max_row = max(max_row, row_index)
            link = str(row.get(COL_FACEBOOK_LINK) or "").strip()
            if link:
                link_to_row[normalize_facebook_url(link)] = row_index

        rows_to_delete = sorted(
            {
                link_to_row[link]
                for link in to_delete_links
                if link in link_to_row
            }
        )
        unresolved = len(to_delete_links) - len(rows_to_delete)
        if unresolved:
            logger.warning(
                "Entity screen %d delete link(s) not found on sheet",
                unresolved,
            )

        if rows_to_delete:
            self._delete_person_rows(sheet, rows_to_delete, max_row=max_row)
            self._save_checkpoint(sheet, {}, set(), stats, tags_applied=True)

        self._sweep_remaining_stragglers(sheet, stats)

        sheets.invalidate_worksheet_cache(sheet)
        self._clear_quota_alert()

    def _sweep_remaining_stragglers(
        self,
        sheet: str,
        stats: dict[str, Any],
    ) -> None:
        """
        After tag/delete pass — no linked row may remain on pending_scrape.

        Rows still pending_scrape are usually personal profiles whose delete failed;
        re-attempt person heuristics, then delete or tag business/uncertain.
        """
        rows = self._with_quota_retry(
            lambda: sheets.read_rows_with_sheet_indices(sheet, use_cache=False),
            label="straggler sweep read",
        )
        person_threshold = self.settings.entity_screen_auto_person
        to_delete: list[int] = []
        tag_fixes: dict[int, str] = {}
        swept = 0

        for row_index, row in rows:
            link = str(row.get(COL_FACEBOOK_LINK) or "").strip()
            if not link:
                continue
            activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip()
            if not _needs_reconcile_tag(activity):
                continue

            name = str(row.get(COL_BUSINESS_NAME) or "").strip()
            heuristic = heuristic_screen(name, link)
            act_lower = activity.lower()

            if (
                heuristic.entity_type == "person"
                and heuristic.confidence >= person_threshold
            ):
                to_delete.append(row_index)
                stats["removed_heuristic"] = stats.get("removed_heuristic", 0) + 1
                stats["removed_sweep"] = stats.get("removed_sweep", 0) + 1
                swept += 1
                logger.info(
                    "Entity screen sweep delete person row %s (%s): %s",
                    row_index,
                    name,
                    heuristic.reason,
                )
                continue

            if (
                act_lower == LEAD_ACTIVITY_PENDING_SCRAPE
                and heuristic.entity_type == "person"
                and heuristic.confidence >= 0.85
            ):
                to_delete.append(row_index)
                stats["removed_heuristic"] = stats.get("removed_heuristic", 0) + 1
                stats["removed_sweep"] = stats.get("removed_sweep", 0) + 1
                swept += 1
                logger.info(
                    "Entity screen sweep delete stale pending_scrape row %s (%s)",
                    row_index,
                    name,
                )
                continue

            if (
                heuristic.entity_type == "business"
                and heuristic.confidence >= _BUSINESS_TAG_THRESHOLD
            ):
                tag_fixes[row_index] = LEAD_ACTIVITY_ENTITY_BUSINESS
                stats["tagged_business"] = stats.get("tagged_business", 0) + 1
            else:
                tag_fixes[row_index] = LEAD_ACTIVITY_ENTITY_UNCERTAIN
                stats["tagged_uncertain"] = stats.get("tagged_uncertain", 0) + 1
            swept += 1
            logger.info(
                "Entity screen sweep tagged row %s (%s) → %s",
                row_index,
                name,
                tag_fixes[row_index],
            )

        if tag_fixes:
            self._apply_activity_tags(sheet, tag_fixes)
        if to_delete:
            max_row = max((row_index for row_index, _ in rows), default=1)
            self._delete_person_rows(sheet, sorted(to_delete), max_row=max_row)

        if swept:
            logger.info("Entity screen straggler sweep handled %s row(s)", swept)

    def _queue_person_delete(
        self,
        row_index: int,
        link: str,
        to_delete_links: set[str],
        pending_tags: dict[int, str],
        stats: dict[str, Any],
        *,
        via_ai: bool = False,
        reason: str = "",
        name: str = "",
    ) -> None:
        norm = normalize_facebook_url(link)
        if norm:
            to_delete_links.add(norm)
        pending_tags.pop(row_index, None)
        if via_ai:
            stats["removed_ai"] += 1
        else:
            stats["removed_heuristic"] += 1
        if reason:
            logger.info(
                "Entity screen person row %s (%s): %s",
                row_index,
                name,
                reason,
            )

    def _tag_or_queue_person(
        self,
        row_index: int,
        row: dict[str, Any],
        to_delete_links: set[str],
        pending_tags: dict[int, str],
        stats: dict[str, Any],
        *,
        via_ai: bool = False,
    ) -> bool:
        """If heuristics say person, queue delete instead of uncertain tag."""
        name = str(row.get(COL_BUSINESS_NAME) or "").strip()
        link = str(row.get(COL_FACEBOOK_LINK) or "").strip()
        heuristic = heuristic_screen(name, link)
        if (
            heuristic.entity_type == "person"
            and heuristic.confidence >= self.settings.entity_screen_auto_person
        ):
            self._queue_person_delete(
                row_index,
                link,
                to_delete_links,
                pending_tags,
                stats,
                via_ai=via_ai,
                reason=heuristic.reason,
                name=name,
            )
            return True
        return False

    def _build_work_items(
        self,
        rows: list[tuple[int, dict[str, Any]]],
    ) -> list[tuple[int, dict[str, Any]]]:
        """Rows needing screen (top-to-bottom). Skips only confident entity_business."""
        work: list[tuple[int, dict[str, Any]]] = []
        for row_index, row in rows:
            link = str(row.get(COL_FACEBOOK_LINK) or "").strip()
            if not link:
                continue
            activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
            if activity == LEAD_ACTIVITY_ENTITY_BUSINESS:
                continue
            work.append((row_index, row))
        work.sort(key=lambda item: item[0])
        return work

    def _run_classification(
        self,
        sheet: str,
        rows: list[tuple[int, dict[str, Any]]],
        work_items: list[tuple[int, dict[str, Any]]],
        stats: dict[str, Any],
        pending_tags: dict[int, str],
        to_delete_links: set[str],
        progress_callback: ProgressCallback | None,
    ) -> sheets.SheetsError | None:
        person_threshold = self.settings.entity_screen_auto_person
        batch_size = self.settings.entity_classify_batch_size
        ai_queue: list[tuple[int, dict[str, Any]]] = []

        def _sync_position() -> None:
            stats["position"] = stats["processed"]

        def _progress_message() -> str:
            removed = stats["removed_heuristic"] + stats["removed_ai"]
            row = stats["current_row"]
            pos = stats["position"]
            total = stats["total"]
            return (
                f"Entity screen — {pos}/{total} top-to-bottom "
                f"(sheet row {row}, {removed} personal removed)"
            )

        def _emit(force: bool = False) -> None:
            if progress_callback is None:
                return
            if not force and stats["position"] % _PROGRESS_EMIT_INTERVAL != 0:
                return
            progress_callback(dict(stats), _progress_message())

        stats["per_item_estimate_secs"] = _SCREEN_HEURISTIC_SECS
        if work_items:
            _emit(force=True)

        try:
            for position, (row_index, row) in enumerate(work_items, start=1):
                stats["position"] = position
                stats["current_row"] = row_index
                name = str(row.get(COL_BUSINESS_NAME) or "").strip()
                link = str(row.get(COL_FACEBOOK_LINK) or "").strip()

                heuristic = heuristic_screen(name, link)
                if (
                    heuristic.entity_type == "person"
                    and heuristic.confidence >= person_threshold
                ):
                    self._queue_person_delete(
                        row_index,
                        link,
                        to_delete_links,
                        pending_tags,
                        stats,
                        reason=heuristic.reason,
                        name=name,
                    )
                    stats["processed"] += 1
                    _sync_position()
                    _emit()
                    continue

                if (
                    heuristic.entity_type == "business"
                    and heuristic.confidence >= _BUSINESS_TAG_THRESHOLD
                ):
                    pending_tags[row_index] = LEAD_ACTIVITY_ENTITY_BUSINESS
                    stats["tagged_business"] += 1
                    stats["processed"] += 1
                    _sync_position()
                    _emit()
                    logger.info(
                        "Entity screen tagged business row %s (%s): %s",
                        row_index,
                        name,
                        heuristic.reason,
                    )
                    continue

                ai_queue.append((row_index, row))

            if ai_queue:
                stats["current_row"] = ai_queue[0][0]
                _emit(force=True)

            if ai_queue:
                ai_queue.sort(key=lambda item: item[0])
                stats["per_item_estimate_secs"] = max(
                    _SCREEN_AI_BATCH_SECS / max(batch_size, 1), 1.5
                )
                _emit(force=True)

                for start in range(0, len(ai_queue), batch_size):
                    batch = ai_queue[start : start + batch_size]
                    batch_end_row = batch[-1][0]
                    stats["current_row"] = batch[0][0]
                    inputs = [
                        EntityLeadInput(
                            row_index=row_index,
                            business_name=str(row.get(COL_BUSINESS_NAME) or ""),
                            facebook_link=str(row.get(COL_FACEBOOK_LINK) or ""),
                        )
                        for row_index, row in batch
                    ]
                    try:
                        results = classify_entities_batch(
                            inputs,
                            mode="screen",
                            api_key=self.settings.openrouter_api_key,
                            model=self.settings.openrouter_model,
                            base_url=self.settings.openrouter_base_url,
                        )
                    except ClassifyBatchError as exc:
                        logger.warning("Entity screen AI batch failed: %s", exc)
                        stats["errors"] += len(batch)
                        for row_index, row in batch:
                            if self._tag_or_queue_person(
                                row_index,
                                row,
                                to_delete_links,
                                pending_tags,
                                stats,
                            ):
                                stats["processed"] += 1
                                _sync_position()
                                continue
                            pending_tags[row_index] = LEAD_ACTIVITY_ENTITY_UNCERTAIN
                            stats["tagged_uncertain"] += 1
                            stats["processed"] += 1
                            _sync_position()
                        stats["current_row"] = batch_end_row
                        _emit(force=True)
                        continue

                    result_map = {r.row_index: r for r in results}
                    for row_index, row in batch:
                        stats["current_row"] = row_index
                        name = str(row.get(COL_BUSINESS_NAME) or "")
                        link = str(row.get(COL_FACEBOOK_LINK) or "").strip()
                        result = result_map.get(row_index)
                        if result is None:
                            if self._tag_or_queue_person(
                                row_index,
                                row,
                                to_delete_links,
                                pending_tags,
                                stats,
                                via_ai=True,
                            ):
                                stats["processed"] += 1
                                _sync_position()
                                _emit()
                                continue
                            pending_tags[row_index] = LEAD_ACTIVITY_ENTITY_UNCERTAIN
                            stats["tagged_uncertain"] += 1
                            stats["processed"] += 1
                            _sync_position()
                            _emit()
                            continue

                        if (
                            result.entity_type == "person"
                            and result.confidence >= person_threshold
                        ):
                            self._queue_person_delete(
                                row_index,
                                link,
                                to_delete_links,
                                pending_tags,
                                stats,
                                via_ai=True,
                                reason=result.reason,
                                name=name,
                            )
                            stats["processed"] += 1
                            _sync_position()
                            _emit()
                        elif (
                            result.entity_type == "business"
                            and result.confidence >= _BUSINESS_TAG_THRESHOLD
                        ):
                            pending_tags[row_index] = LEAD_ACTIVITY_ENTITY_BUSINESS
                            stats["tagged_business"] += 1
                            stats["processed"] += 1
                            _sync_position()
                            _emit()
                        else:
                            if self._tag_or_queue_person(
                                row_index,
                                row,
                                to_delete_links,
                                pending_tags,
                                stats,
                                via_ai=True,
                            ):
                                stats["processed"] += 1
                                _sync_position()
                                _emit()
                                continue
                            pending_tags[row_index] = LEAD_ACTIVITY_ENTITY_UNCERTAIN
                            stats["tagged_uncertain"] += 1
                            stats["processed"] += 1
                            _sync_position()
                            _emit()

                    stats["current_row"] = batch_end_row
                    _emit(force=True)

        except sheets.SheetsError as exc:
            logger.warning("Entity screen classification interrupted: %s", exc)
            return exc

        self._ensure_work_items_resolved(
            work_items,
            pending_tags,
            to_delete_links,
            stats,
            person_threshold,
        )
        return None

    def _ensure_work_items_resolved(
        self,
        work_items: list[tuple[int, dict[str, Any]]],
        pending_tags: dict[int, str],
        to_delete_links: set[str],
        stats: dict[str, Any],
        person_threshold: float,
    ) -> None:
        """Every screened row must be tagged or queued for delete before finalize."""
        gaps = 0
        for row_index, row in work_items:
            link = str(row.get(COL_FACEBOOK_LINK) or "").strip()
            norm = normalize_facebook_url(link) if link else ""
            if norm and norm in to_delete_links:
                continue
            if row_index in pending_tags:
                continue
            gaps += 1
            if self._tag_or_queue_person(
                row_index,
                row,
                to_delete_links,
                pending_tags,
                stats,
            ):
                continue
            pending_tags[row_index] = LEAD_ACTIVITY_ENTITY_UNCERTAIN
            stats["tagged_uncertain"] += 1
            logger.warning(
                "Entity screen safety tag row %s as entity_uncertain",
                row_index,
            )
        if gaps:
            logger.warning(
                "Entity screen resolved %s unclassified work item(s) before finalize",
                gaps,
            )

    def _success_message(self, stats: dict[str, Any]) -> str:
        removed = stats["removed_heuristic"] + stats["removed_ai"]
        message = (
            f"Screened {stats['screened']} lead(s): removed {removed} personal, "
            f"tagged {stats['tagged_business']} business, "
            f"{stats['tagged_uncertain']} uncertain"
        )
        if stats["reconciled_uncertain"]:
            message += f" ({stats['reconciled_uncertain']} reconciled)"
        if stats.get("removed_sweep"):
            message += f" ({stats['removed_sweep']} sweep)"
        if stats["errors"]:
            message += f" ({stats['errors']} batch error(s))"
        return message

    def run(
        self,
        progress_callback: ProgressCallback | None = None,
    ) -> ScreenRunResult:
        if not self.settings.sheets_configured:
            return ScreenRunResult(ok=False, message="Service account JSON not found", stats={})
        if not self.settings.openrouter_configured:
            return ScreenRunResult(
                ok=False, message="OPENROUTER_API_KEY not set in .env", stats={}
            )

        sheet = self.settings.sheet_dynamic_lead
        self._clear_quota_alert()
        sheets.ensure_worksheet(sheet, DYNAMIC_LEAD_HEADERS)

        try:
            rows = sheets.read_rows_with_sheet_indices(sheet, use_cache=False)
        except sheets.SheetsError as exc:
            return ScreenRunResult(ok=False, message=str(exc), stats={})

        checkpoint = self._load_checkpoint()
        if checkpoint and checkpoint.get("sheet_name") == sheet:
            cp_stats = checkpoint.get("stats") or {}
            if (
                cp_stats.get("processed") == cp_stats.get("total")
                and cp_stats.get("total", 0) > 0
            ):
                logger.info(
                    "Entity screen resuming finalize from checkpoint "
                    "(%s/%s classified)",
                    cp_stats.get("processed"),
                    cp_stats.get("total"),
                )
                stats = dict(cp_stats)
                pending_tags = {
                    int(k): v
                    for k, v in (checkpoint.get("pending_tags") or {}).items()
                }
                to_delete_links = set(checkpoint.get("to_delete_links") or [])
                tags_applied = bool(checkpoint.get("tags_applied"))
                if progress_callback:
                    progress_callback(
                        dict(stats),
                        "Entity screen — applying sheet updates…",
                    )
                try:
                    self._apply_sheet_results(
                        sheet,
                        rows,
                        stats,
                        pending_tags,
                        to_delete_links,
                        tags_already_applied=tags_applied,
                    )
                    self._clear_checkpoint()
                except sheets.SheetsError as exc:
                    return ScreenRunResult(ok=False, message=str(exc), stats=stats)
                notify_attention(
                    "Entity screen complete",
                    self._success_message(stats),
                    context="Step 3 Entity Screen",
                )
                return ScreenRunResult(
                    ok=True,
                    message=self._success_message(stats),
                    stats=stats,
                )

        work_items = self._build_work_items(rows)
        stats: dict[str, Any] = {
            "screened": len(work_items),
            "removed_heuristic": 0,
            "removed_ai": 0,
            "tagged_business": 0,
            "tagged_uncertain": 0,
            "reconciled_uncertain": 0,
            "removed_sweep": 0,
            "errors": 0,
            "total": len(work_items),
            "processed": 0,
            "current_row": work_items[0][0] if work_items else 0,
            "position": 0,
        }
        pending_tags: dict[int, str] = {}
        to_delete_links: set[str] = set()

        if not work_items:
            return ScreenRunResult(
                ok=True,
                message="No leads to screen on Dynamic Lead Sheet",
                stats=stats,
            )

        processing_error = self._run_classification(
            sheet,
            rows,
            work_items,
            stats,
            pending_tags,
            to_delete_links,
            progress_callback,
        )

        self._save_checkpoint(sheet, pending_tags, to_delete_links, stats)

        if progress_callback:
            progress_callback(
                dict(stats),
                "Entity screen — applying sheet updates…",
            )

        try:
            self._apply_sheet_results(
                sheet,
                rows,
                stats,
                pending_tags,
                to_delete_links,
            )
            self._clear_checkpoint()
        except sheets.SheetsError as exc:
            if processing_error is not None:
                return ScreenRunResult(
                    ok=False,
                    message=f"{processing_error}; finalize also failed: {exc}",
                    stats=stats,
                )
            return ScreenRunResult(ok=False, message=str(exc), stats=stats)

        if processing_error is not None:
            return ScreenRunResult(ok=False, message=str(processing_error), stats=stats)

        notify_attention(
            "Entity screen complete",
            self._success_message(stats),
            context="Step 3 Entity Screen",
        )
        return ScreenRunResult(
            ok=True,
            message=self._success_message(stats),
            stats=stats,
        )


def get_entity_screen_service(settings: Settings | None = None) -> EntityScreenService:
    return EntityScreenService(settings or get_settings())
