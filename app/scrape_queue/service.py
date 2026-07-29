"""Scrape Queue — one lead at row 2 on scrapesheet (link + data)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sheets
from app.config import Settings, get_settings
from app.scrapers.lead_mapping import normalize_facebook_url
from app.scrape_queue.state import (
    ActiveScrapeState,
    get_failure_store,
    get_handled_store,
    get_state_store,
    PHASE_AWAITING_CLEAR,
    PHASE_AWAITING_PASTE,
    PHASE_READY,
)
from app.scrape_queue.mistake_alerts import (
    alert_from_ownership,
    notify_scrape_paste_mistake,
    resolve_mistake_alerts,
)
from app.scrape_queue.ownership import (
    b_is_empty,
    evaluate_paste_for_intended,
    evaluate_paste_for_link_matched_row,
    hash_paste_text,
    ownership_action_label,
    paste_belongs_to_intended,
    PasteOwnershipStatus,
)
from app.entity.constants import (
    LEAD_ACTIVITY_ENTITY_BUSINESS,
    LEAD_ACTIVITY_ENTITY_UNCERTAIN,
)
from app.sheets.columns import (
    COL_FACEBOOK_LINK,
    COL_LEAD_ACTIVITY,
    COL_SCRAPE,
    COL_SCRAPE_DATA,
    COL_SCRAPE_LINK,
    is_scrape_failed_activity,
    LEAD_ACTIVITY_FAILED,
    LEAD_ACTIVITY_FAILED_3,
    LEAD_ACTIVITY_PENDING,
    scrape_failed_activity_label,
    SCRAPE_SHEET_HEADERS,
    SCRAPE_SHEET_ROW,
)

logger = logging.getLogger(__name__)

PASTE_SANITIZE_MAX_CHARS = 8000
PASTE_DETECT_MIN_CHARS = 15
B_EMPTY_MAX_CHARS = 2
# Dynamic Lead write row = owner_row (scrapesheet owner maps 1:1 to sheet row)
SCRAPE_WRITE_ROW_OFFSET = 0


@dataclass
class EnqueueResult:
    ok: bool
    message: str
    source_row: int | None = None
    link: str | None = None
    saved_chars: int = 0
    saved_to_row: int | None = None


@dataclass
class FinalizeResult:
    ok: bool
    message: str
    action: str = "none"
    source_row: int | None = None
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class PopulateScrapeSheetResult:
    ok: bool
    message: str
    count: int = 0
    target_rows: set[int] = field(default_factory=set)
    enqueue: EnqueueResult | None = None


class ScrapeQueueService:
    """
    Scrape handoff chain (scrapesheet row 2 — link in A, paste in B).

    1. Column A set to lead X → MMM scrapes and pastes into B
    2. When B is ready → copy B to Dynamic Lead row X, then change A to lead Y
    3. MMM reacts to link change → repeat
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._state = get_state_store(self.settings)
        self._failures = get_failure_store(self.settings)
        self._handled = get_handled_store(self.settings)
        self._tick_lead_index: list[tuple[int, str, str, str, int]] | None = None
        self._tick_lock = threading.Lock()
        self._last_list_progress: dict[str, Any] | None = None
        self._last_batch_progress: dict[str, Any] | None = None
        self._last_dashboard_sync_mono = 0.0
        self._dashboard_sync_interval_secs = 10.0
        self._manual_complete_requested = False

    def request_manual_scrape_complete(self) -> dict[str, Any]:
        """
        Mark scraping done in the app only — clears handoff state, does not read or
        write Dynamic Lead Sheet or scrapesheet data (you copy scrape data yourself).
        """
        with self._tick_lock:
            self._manual_complete_requested = True
        self._clear_step4_batch()
        self._state.clear()
        self._handled.clear()
        self._reset_tick_cache()
        logger.info(
            "Manual scrape complete — queue state cleared, spreadsheets not modified"
        )
        return {
            "ok": True,
            "message": "Scrape marked complete. No data moved between spreadsheets.",
        }

    def is_manual_complete_requested(self) -> bool:
        with self._tick_lock:
            return self._manual_complete_requested

    def clear_manual_complete_request(self) -> None:
        with self._tick_lock:
            self._manual_complete_requested = False

    def _reset_tick_cache(self) -> None:
        self._tick_lead_index = None

    def refresh_lead_index(self) -> None:
        """Drop cached lead rows so the next read reflects sheet changes (e.g. Step 1 append)."""
        self._reset_tick_cache()
        sheets.invalidate_lead_index_cache(self.settings.sheet_dynamic_lead)

    def _lead_index_rows(
        self, *, include_names: bool = False, use_tick_cache: bool = True
    ) -> list[tuple[int, str, str, str, int]]:
        if use_tick_cache and self._tick_lead_index is not None:
            rows = self._tick_lead_index
        else:
            rows = sheets.read_dynamic_lead_index(
                self.settings.sheet_dynamic_lead,
                include_names=True,
                use_cache=use_tick_cache,
            )
            if use_tick_cache:
                self._tick_lead_index = rows
        if include_names:
            return rows
        return [
            (r, link, "", activity, scrape_len)
            for r, link, _name, activity, scrape_len in rows
        ]

    def _scrape_len_in_rows(
        self,
        rows: list[tuple[int, str, str, str, int]],
        row_index: int,
    ) -> int:
        for r, _link, _name, _activity, scrape_len in rows:
            if r == row_index:
                return scrape_len
        return 0

    def _maybe_dashboard_sync(self) -> int:
        """Throttled scrapesheet→Dynamic Lead sync for dashboard polls (not every 1s)."""
        if not self._scrapesheet_session_active():
            return 0
        if sheets.is_quota_cooldown():
            return 0
        now = time.monotonic()
        if now - self._last_dashboard_sync_mono < self._dashboard_sync_interval_secs:
            return 0
        self._last_dashboard_sync_mono = now
        try:
            synced = self._sync_scrapesheet_pastes_to_leads()
            if synced:
                logger.info(
                    "Dashboard sync: %d scrapesheet paste(s) saved to Dynamic Lead",
                    synced,
                )
            return synced
        except sheets.SheetsError as exc:
            logger.warning("Dashboard scrapesheet sync skipped: %s", exc)
            return 0

    def _scrapesheet_status_from_rows(
        self, sheet_rows: list[tuple[int, str, str]]
    ) -> dict[str, Any]:
        min_len = self.settings.scrape_min_length
        with_link = [(idx, link, data) for idx, link, data in sheet_rows if link]
        rows_with_paste = sum(
            1 for _, _, data in with_link if len(data.strip()) >= min_len
        )
        total = len(with_link)
        return {
            "total_rows": total,
            "rows_with_link": total,
            "rows_with_paste": rows_with_paste,
            "rows_need_scraping": max(0, total - rows_with_paste),
        }

    def _read_scrapesheet_rows_cached(self) -> list[tuple[int, str, str]]:
        return sheets.read_scrapesheet_rows(
            self.settings.sheet_scrape_queue, use_cache=True
        )

    def _pasted_target_rows(
        self,
        target_rows: set[int],
        sheet_rows: list[tuple[int, str, str]],
    ) -> set[int]:
        min_len = self.settings.scrape_min_length
        pasted_rows: set[int] = set()
        for _sheet_row_idx, link, raw_data in sheet_rows:
            if len((raw_data or "").strip()) < min_len:
                continue
            match = self._find_any_row_by_link(link)
            if match is None:
                continue
            source_row, _ = match
            if source_row in target_rows:
                pasted_rows.add(source_row)
        return pasted_rows

    def _apply_run_progress_fields(
        self,
        progress: dict[str, Any],
        *,
        run_total: int,
        step4_waiting: bool,
        target_set: set[int],
        batch: dict[str, Any],
        sheet_rows: list[tuple[int, str, str]] | None = None,
    ) -> None:
        progress["run_active"] = True
        progress["run_waiting"] = step4_waiting
        progress["run_total"] = run_total
        progress["run_scraped"] = int(batch["scraped"])
        progress["run_failed"] = int(batch["failed"])
        progress["run_pasted"] = int(batch.get("pasted", 0))
        progress["run_display_done"] = int(batch.get("display_done", 0))
        progress["run_remaining"] = int(batch.get("remaining", 0))
        progress["run_current"] = int(batch["done"]) + (
            1 if batch["scraping"] or batch["pending"] else 0
        )
        progress["run_done"] = int(batch["done"])
        progress["current_row"] = batch.get("current_row")
        progress["current_business_name"] = batch.get("current_business_name", "")
        if sheet_rows is not None:
            sheet_status = self._scrapesheet_status_from_rows(sheet_rows)
        else:
            sheet_status = self.get_scrapesheet_status()
        progress["scrape_sheet_total"] = sheet_status.get("rows_with_link", 0)
        progress["scrape_sheet_need_scraping"] = sheet_status.get(
            "rows_need_scraping", 0
        )
        progress["scrape_sheet_with_paste"] = sheet_status.get("rows_with_paste", 0)

    def _scrape_len_for_row(self, row_index: int, *, fresh: bool = False) -> int:
        for r, _link, _name, _activity, scrape_len in self._lead_index_rows(
            include_names=True, use_tick_cache=not fresh
        ):
            if r == row_index:
                return scrape_len
        return 0

    def _baseline_for_new_owner(self, current_data: str) -> str:
        """Hash of column B when column A is set — new paste = hash differs from this."""
        return hash_paste_text(self._sanitize_scrape_text(current_data))

    def _paste_is_ready(self, scrape_text: str) -> bool:
        raw = (scrape_text or "").strip()
        if len(raw) >= PASTE_DETECT_MIN_CHARS:
            return True
        cleaned = self._sanitize_scrape_text(scrape_text)
        return len(cleaned.strip()) >= PASTE_DETECT_MIN_CHARS

    def _stall_poll_limit(
        self,
        active: ActiveScrapeState,
        ownership_reason: str = "",
    ) -> int:
        """Polls before stall-fail — longer while MMM must clear stale column B."""
        poll_secs = max(float(self.settings.scrape_active_poll_secs), 1.0)
        stall_secs = max(float(self.settings.scrape_stall_secs), 90.0)
        if active.phase == PHASE_AWAITING_CLEAR:
            stall_secs = max(stall_secs * 4, 300.0)
        elif "carried-over" in (ownership_reason or "").lower():
            stall_secs = max(stall_secs * 2, 180.0)
        return max(int(stall_secs / poll_secs), 24)

    def _ownership_rows(self) -> list[tuple[int, str, str, str, int]]:
        return self._lead_index_rows(include_names=True, use_tick_cache=True)

    def _evaluate_paste(self, active: ActiveScrapeState, scrape_text: str):
        cleaned = self._sanitize_scrape_text(scrape_text)
        return evaluate_paste_for_intended(
            cleaned,
            active.source_row,
            active.business_name,
            self._ownership_rows(),
            min_length=self.settings.scrape_min_length,
            phase=active.phase,
            baseline_b_hash=active.baseline_b_hash,
            consumed_paste_hash=active.consumed_paste_hash,
            trust_link=True,
        )

    def _update_phase_from_b(
        self, active: ActiveScrapeState, scrape_text: str
    ) -> ActiveScrapeState:
        """Advance phase based on column B (MMM clear-then-paste cycle)."""
        data_hash = hash_paste_text(self._sanitize_scrape_text(scrape_text))
        active.last_b_hash = data_hash
        active.poll_count_since_link += 1

        if b_is_empty(scrape_text):
            if active.phase == PHASE_AWAITING_CLEAR:
                active.phase = PHASE_AWAITING_PASTE
                logger.info(
                    "Column B cleared for row %s — ready for paste",
                    active.source_row,
                )
            return active

        if active.phase == PHASE_AWAITING_CLEAR:
            # MMM pasted without observable clear — still evaluate ownership
            active.phase = PHASE_AWAITING_PASTE

        ownership = self._evaluate_paste(active, scrape_text)
        if paste_belongs_to_intended(ownership, active.source_row):
            active.phase = PHASE_READY
        return active

    def _reconcile_active_with_column_a(
        self,
        queue_row: dict[str, Any],
        active: ActiveScrapeState | None,
    ) -> ActiveScrapeState | None:
        """Trust scrapesheet column A when active.json drifts from the live link."""
        a_link = self._field(queue_row, COL_SCRAPE_LINK)
        if not a_link:
            return active
        if active and normalize_facebook_url(active.link) == normalize_facebook_url(a_link):
            if active.source_row and active.business_name:
                return active
            match = self._find_any_row_by_link(a_link)
            if match is None:
                return active
            source_row, row = match
            if source_row == active.source_row:
                active.business_name = str(row.get("Business Name") or "").strip()
                self._state.save(active)
                return active

        match = self._find_any_row_by_link(a_link)
        if match is None:
            return active

        source_row, row = match
        business_name = str(row.get("Business Name") or "").strip()
        prev = active
        if prev and prev.source_row == source_row and prev.business_name == business_name:
            prev.link = a_link
            self._state.save(prev)
            return prev

        logger.warning(
            "Active state drift — A2 row %s (%s), state was row %s (%s)",
            source_row,
            business_name or a_link[:40],
            prev.source_row if prev else None,
            prev.business_name if prev else None,
        )

        _, column_b = self._read_scrape_cells(fresh=True)
        baseline_hash = self._baseline_for_new_owner(column_b)
        b_cleared = b_is_empty(column_b)
        phase = PHASE_AWAITING_PASTE if b_cleared else PHASE_AWAITING_CLEAR
        state = ActiveScrapeState(
            source_row=source_row,
            attempt=1,
            link=a_link,
            business_name=business_name,
            phase=phase,
            link_set_at=datetime.now(timezone.utc).isoformat(),
            baseline_b_hash=baseline_hash if not b_cleared else "",
            last_b_hash=baseline_hash,
            consumed_paste_hash="",
            poll_count_since_link=0,
        )
        self._state.save(state)
        return state

    def reset_from_sheet(self) -> bool:
        """Rebuild active.json from live scrapesheet A/B (Step 3 start)."""
        self._state.clear()
        self._reset_tick_cache()
        queue_row = self._read_queue_row(fresh=True)
        if self.queue_is_idle(queue_row):
            return False
        active = self._resolve_active(queue_row)
        if active is None:
            return False
        _, column_b = self._read_scrape_cells(fresh=True)
        active = self._update_phase_from_b(active, column_b)
        self._state.save(active)
        logger.info(
            "Reset scrape state from sheet — row %s (%s) phase=%s",
            active.source_row,
            active.business_name,
            active.phase,
        )
        return True

    def _step4_batch_path(self) -> Path:
        return self.settings.scrape_state_path.parent / "step4_batch.json"

    def _save_step4_batch(self, target_rows: set[int]) -> None:
        path = self._step4_batch_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "target_rows": sorted(target_rows),
                    "total_to_scrape": len(target_rows),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _load_step4_batch(self) -> dict[str, Any] | None:
        path = self._step4_batch_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("target_rows"):
                return raw
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.warning("Invalid step4 batch file %s: %s", path, exc)
        return None

    def _clear_step4_batch(self) -> None:
        path = self._step4_batch_path()
        if path.exists():
            path.unlink()

    def _pipeline_page_scrape_active(self) -> bool:
        """Only Step 4 should write Scrape column on Dynamic Lead Sheet."""
        from app.pipeline.runner import get_pipeline_runner

        runner = get_pipeline_runner()
        return runner.state.is_running and runner.state.current_step_id == 4

    def _scrapesheet_session_active(self) -> bool:
        """Scrapesheet handoff is only allowed during an active Step 4 batch."""
        from app.pipeline.runner import get_pipeline_runner
        from app.pipeline.steps.base import StepStatus

        runner = get_pipeline_runner()
        if runner.state.is_running and runner.state.current_step_id == 4:
            return True

        step4 = next((s for s in runner.state.steps if s.id == 4), None)
        if not step4 or not step4.stats:
            return False

        total = int(step4.stats.get("total_to_scrape") or 0)
        if total <= 0:
            return False

        if step4.status == StepStatus.RUNNING:
            return True

        if step4.status == StepStatus.WAITING:
            scraped = int(step4.stats.get("scraped_count") or 0)
            failed = int(step4.stats.get("failed_count") or 0)
            return scraped + failed < total

        batch = self._load_step4_batch()
        if batch:
            target_rows = {int(r) for r in batch.get("target_rows", [])}
            if target_rows:
                progress = self.get_batch_progress(target_rows)
                return int(progress.get("done", 0)) < len(target_rows)

        return False

    def _clear_scrapesheet_links(self) -> None:
        """Remove all scrapesheet queue rows (link + paste columns)."""
        sheets.reset_scrapesheet_links(self.settings.sheet_scrape_queue, [])
        sheets.invalidate_scrape_row_cache(self.settings.sheet_scrape_queue)

    def _row_has_scrape_data(self, scrape_len: int) -> bool:
        return scrape_len >= self.settings.scrape_min_length

    def _row_is_active_scrape(self, row_index: int) -> bool:
        active = self._state.load()
        return active is not None and active.source_row == row_index

    def clear_handled_rows(self) -> None:
        """Reset background link-rotation memory (call when Step 3 starts)."""
        self._handled.clear()

    def wipe_scrapesheet(self) -> None:
        """Clear scrapesheet row 2 (link + data) and all scrape queue runtime state."""
        sheets.clear_scrape_queue_row(
            self.settings.sheet_scrape_queue, SCRAPE_SHEET_ROW
        )
        self._state.clear()
        self._handled.clear()
        self._failures.clear_all()
        self._reset_tick_cache()
        logger.info("Wiped scrapesheet row %s and scrape queue state", SCRAPE_SHEET_ROW)

    def collect_scrape_batch(self) -> list[tuple[int, str, str, str]]:
        """Every row with a Facebook link (source_row, link, business_name, activity)."""
        batch: list[tuple[int, str, str, str]] = []
        for row_index, row_link, name, activity, _scrape_len in self._lead_index_rows(
            include_names=True, use_tick_cache=False
        ):
            if not row_link.strip():
                continue
            batch.append(
                (row_index, row_link.strip(), name.strip(), activity.strip())
            )
        return batch

    def get_batch_progress(self, target_rows: set[int]) -> dict[str, Any]:
        """Progress for a Step 4 batch keyed by Dynamic Lead row indices."""
        if not target_rows:
            return {
                "scraped": 0,
                "failed": 0,
                "pending": 0,
                "scraping": 0,
                "done": 0,
                "current_row": None,
                "current_business_name": "",
            }

        scraped = 0
        failed = 0
        pending = 0
        scraping = 0
        current_row: int | None = None
        current_name = ""

        for row_index, _link, name, _activity, scrape_len in self._lead_index_rows(
            include_names=True, use_tick_cache=True
        ):
            if row_index not in target_rows:
                continue
            if self._is_permanently_failed(row_index):
                failed += 1
            elif self._row_has_scrape_data(scrape_len):
                scraped += 1
            elif self._row_is_active_scrape(row_index):
                scraping += 1
                if current_row is None:
                    current_row = row_index
                    current_name = name
            else:
                pending += 1

        active = self._state.load()
        if active and active.source_row in target_rows:
            current_row = active.source_row
            current_name = active.business_name or current_name

        return {
            "scraped": scraped,
            "failed": failed,
            "pending": pending,
            "scraping": scraping,
            "done": scraped + failed,
            "remaining": max(0, len(target_rows) - scraped - failed),
            "current_row": current_row,
            "current_business_name": current_name,
        }

    def get_scrapesheet_batch_progress(self, target_rows: set[int]) -> dict[str, Any]:
        """Batch progress: Dynamic Lead saves plus scrapesheet paste activity."""
        try:
            batch = self.get_batch_progress(target_rows)
            if not target_rows:
                result = {
                    **batch,
                    "pasted": 0,
                    "display_done": 0,
                }
                self._last_batch_progress = result
                return result

            sheet_rows: list[tuple[int, str, str]] = []
            try:
                sheet_rows = self._read_scrapesheet_rows_cached()
            except sheets.SheetsError as exc:
                logger.warning(
                    "get_scrapesheet_batch_progress paste read failed: %s", exc
                )

            pasted = len(self._pasted_target_rows(target_rows, sheet_rows))
            saved_done = int(batch["done"])
            display_done = min(len(target_rows), max(saved_done, pasted))

            result = {
                **batch,
                "pasted": pasted,
                "display_done": display_done,
            }
            self._last_batch_progress = result
            return result
        except sheets.SheetsError as exc:
            logger.warning("get_scrapesheet_batch_progress failed: %s", exc)
            if self._last_batch_progress is not None:
                stale = dict(self._last_batch_progress)
                stale["stale"] = True
                return stale
            raise

    def get_scrapesheet_status(self) -> dict[str, Any]:
        """Count scrapesheet rows: links in A, paste in B, rows still needing MMM paste."""
        try:
            rows = self._read_scrapesheet_rows_cached()
            return self._scrapesheet_status_from_rows(rows)
        except sheets.SheetsError as exc:
            logger.warning("get_scrapesheet_status failed: %s", exc)
            if self._last_list_progress:
                return {
                    "total_rows": self._last_list_progress.get("scrape_sheet_total", 0),
                    "rows_with_link": self._last_list_progress.get(
                        "scrape_sheet_total", 0
                    ),
                    "rows_with_paste": self._last_list_progress.get(
                        "scrape_sheet_with_paste", 0
                    ),
                    "rows_need_scraping": self._last_list_progress.get(
                        "scrape_sheet_need_scraping", 0
                    ),
                    "error": str(exc),
                }
            return {
                "total_rows": 0,
                "rows_with_link": 0,
                "rows_with_paste": 0,
                "rows_need_scraping": 0,
                "error": str(exc),
            }

    def _sync_scrapesheet_pastes_to_leads(self) -> int:
        """
        Save ready pastes from any scrapesheet row into Dynamic Lead Scrape column.

        MMM may paste into the row that matches column A (not only row 2). Row 2 still
        drives handoff; other rows are synced here by link match.
        """
        try:
            sheet_rows = self._read_scrapesheet_rows_cached()
        except sheets.SheetsError as exc:
            logger.warning("scrapesheet paste sync failed: %s", exc)
            return 0

        saved_count = 0
        min_len = self.settings.scrape_min_length
        ownership_rows = self._lead_index_rows(include_names=True, use_tick_cache=True)

        for sheet_row_idx, link, raw_data in sheet_rows:
            if not link.strip():
                continue
            if len((raw_data or "").strip()) < min_len:
                continue
            if not self._paste_is_ready(raw_data):
                continue

            match = self._find_any_row_by_link(link)
            if match is None:
                logger.info(
                    "Scrapesheet row %s link not on Dynamic Lead — skipped",
                    sheet_row_idx,
                )
                continue

            source_row, row = match
            business_name = str(row.get("Business Name") or "").strip()

            if self._row_has_scrape_data(
                self._scrape_len_in_rows(ownership_rows, source_row)
            ):
                if sheet_row_idx != SCRAPE_SHEET_ROW:
                    sheets.clear_scrape_queue_data(
                        self.settings.sheet_scrape_queue, sheet_row_idx
                    )
                continue

            cleaned = self._sanitize_scrape_text(raw_data)
            ownership = evaluate_paste_for_link_matched_row(
                cleaned,
                source_row,
                business_name,
                ownership_rows,
                min_length=min_len,
            )
            if not paste_belongs_to_intended(ownership, source_row):
                logger.info(
                    "Scrapesheet row %s paste not saved — %s",
                    sheet_row_idx,
                    ownership_action_label(ownership),
                )
                continue

            try:
                saved_chars = self._write_scrape_to_dynamic_lead(
                    source_row,
                    raw_data,
                    business_name=business_name,
                    ownership_verified=True,
                )
            except sheets.SheetsError as exc:
                logger.warning(
                    "Scrapesheet row %s save failed: %s",
                    sheet_row_idx,
                    exc,
                )
                continue

            if not saved_chars:
                continue

            saved_count += 1
            self._reset_tick_cache()
            ownership_rows = self._lead_index_rows(
                include_names=True, use_tick_cache=True
            )
            logger.info(
                "Synced scrapesheet row %s → Dynamic Lead owner %s (%d chars)",
                sheet_row_idx,
                source_row,
                saved_chars,
            )

            if sheet_row_idx == SCRAPE_SHEET_ROW:
                active = self._state.load()
                if active and active.source_row == source_row:
                    active.phase = PHASE_READY
                    active.consumed_paste_hash = hash_paste_text(cleaned)
                    self._state.save(active)
            else:
                sheets.clear_scrape_queue_data(
                    self.settings.sheet_scrape_queue, sheet_row_idx
                )

        return saved_count

    def clear_scrapesheet_after_batch(self) -> None:
        """Remove all scrapesheet queue rows and reset scrape handoff state."""
        self._clear_scrapesheet_links()
        self._clear_step4_batch()
        self._state.clear()
        self._handled.clear()
        self._reset_tick_cache()
        logger.info("Scrapesheet cleared after Step 4 batch complete")

    def populate_scrapesheet_queue(self) -> PopulateScrapeSheetResult:
        """
        Write every lead needing scrape to the scrapesheet (column A, row 2 down)
        and bootstrap row 2 for MMM.
        """
        self.ensure_queue_sheet()
        self._state.clear()
        self._handled.clear()
        self._failures.clear_all()
        self._reset_tick_cache()

        self._clear_scrapesheet_links()
        logger.info("Scrapesheet cleared before Step 4 link load")

        batch = self.collect_scrape_batch()
        if not batch:
            self._clear_step4_batch()
            return PopulateScrapeSheetResult(
                ok=True,
                message="No Facebook links on Dynamic Lead Sheet",
                count=0,
            )

        target_rows = {row_index for row_index, _, _, _ in batch}
        self._save_step4_batch(target_rows)
        sheets.batch_clear_scrape_column(
            self.settings.sheet_dynamic_lead,
            sorted(target_rows),
        )
        self._reset_tick_cache()

        links = [link for _, link, _, _ in batch]
        sheet_rows = sheets.reset_scrapesheet_links(
            self.settings.sheet_scrape_queue, links
        )

        first_row, first_link, first_name, _ = batch[0]
        enqueue = self._start_lead(
            require_idle=False,
            source_row=first_row,
            link=first_link,
            business_name=first_name,
            ensure_sheet=False,
            retry_failed=self._failures.count(first_row) > 0,
        )

        message = (
            f"Populated scrapesheet with {sheet_rows} link(s); "
            f"MMM starts at row {first_row} ({first_name or first_link[:40]})"
        )
        logger.info(message)
        return PopulateScrapeSheetResult(
            ok=enqueue.ok,
            message=message,
            count=sheet_rows,
            target_rows=target_rows,
            enqueue=enqueue,
        )

    def get_list_progress(self) -> dict[str, Any]:
        """How far through the uploaded Dynamic Lead list page scraping has progressed."""
        from app.pipeline.runner import get_pipeline_runner
        from app.pipeline.steps.base import StepStatus

        runner = get_pipeline_runner()
        current_step = runner.state.current_step_id if runner.state.is_running else None
        if current_step is not None and current_step != 4:
            if self._last_list_progress:
                cached = dict(self._last_list_progress)
                if sheets.is_quota_cooldown():
                    cached["quota_cooldown"] = True
                    cached["quota_cooldown_secs"] = sheets.quota_cooldown_remaining_secs()
                return cached

        self._maybe_dashboard_sync()

        total = 0
        scraped = 0
        failed = 0
        pending = 0
        scraping = 0
        list_error: str | None = None

        try:
            for row_index, link, _name, activity, scrape_len in self._lead_index_rows(
                include_names=True, use_tick_cache=True
            ):
                if not link.strip():
                    continue
                total += 1
                if self._is_permanently_failed(row_index):
                    failed += 1
                elif self._row_has_scrape_data(scrape_len):
                    scraped += 1
                elif self._row_is_active_scrape(row_index):
                    scraping += 1
                else:
                    pending += 1
        except sheets.SheetsError as exc:
            logger.warning("get_list_progress lead index failed: %s", exc)
            list_error = str(exc)
            if self._last_list_progress:
                total = int(self._last_list_progress.get("total") or 0)
                scraped = int(self._last_list_progress.get("scraped") or 0)
                failed = int(self._last_list_progress.get("failed") or 0)
                pending = int(self._last_list_progress.get("pending") or 0)
                scraping = int(self._last_list_progress.get("scraping") or 0)

        active = self._state.load()
        try:
            status = self.get_status()
        except sheets.SheetsError as exc:
            logger.warning("get_list_progress status failed: %s", exc)
            status = {"queue_idle": True}
            if not list_error:
                list_error = str(exc)

        done = scraped + failed

        progress: dict[str, Any] = {
            "total": total,
            "scraped": scraped,
            "failed": failed,
            "pending": pending,
            "scraping": scraping,
            "done": done,
            "queue_idle": status.get("queue_idle", True),
            "current_business_name": active.business_name if active else "",
            "current_link": active.link if active else "",
            "current_row": active.source_row if active else None,
        }
        if list_error:
            progress["error"] = list_error
            if sheets.is_quota_cooldown():
                progress["quota_cooldown"] = True
                progress["quota_cooldown_secs"] = sheets.quota_cooldown_remaining_secs()

        runner = get_pipeline_runner()
        step4 = next((s for s in runner.state.steps if s.id == 4), None)
        batch_file = self._load_step4_batch()
        sheet_rows: list[tuple[int, str, str]] | None = None
        try:
            sheet_rows = self._read_scrapesheet_rows_cached()
        except sheets.SheetsError:
            sheet_rows = None

        if step4 and step4.stats:
            stats = step4.stats
            run_total = int(stats.get("total_to_scrape") or 0)
            target_rows = stats.get("target_rows")
            step4_running = step4.status == StepStatus.RUNNING
            step4_waiting = step4.status == StepStatus.WAITING
            run_done_from_stats = int(stats.get("scraped_count") or 0) + int(
                stats.get("failed_count") or 0
            )
            batch_incomplete = run_total > 0 and run_done_from_stats < run_total
            track_run = step4_running or (step4_waiting and batch_incomplete)
            if track_run and isinstance(target_rows, list) and target_rows:
                target_set = set(int(r) for r in target_rows)
                try:
                    batch = self.get_scrapesheet_batch_progress(target_set)
                except sheets.SheetsError:
                    batch = self._last_batch_progress or {
                        "scraped": int(stats.get("scraped_count") or 0),
                        "failed": int(stats.get("failed_count") or 0),
                        "pasted": int(stats.get("scrapesheet_pasted_count") or 0),
                        "display_done": int(stats.get("display_done_count") or 0),
                        "done": run_done_from_stats,
                        "remaining": max(0, run_total - run_done_from_stats),
                        "scraping": int(stats.get("scraping_count") or 0),
                        "pending": int(stats.get("pending_count") or 0),
                        "current_row": stats.get("current_row"),
                        "current_business_name": stats.get("current_business_name", ""),
                    }
                self._apply_run_progress_fields(
                    progress,
                    run_total=run_total,
                    step4_waiting=step4_waiting,
                    target_set=target_set,
                    batch=batch,
                    sheet_rows=sheet_rows,
                )
            elif track_run and run_total > 0:
                progress["run_active"] = True
                progress["run_waiting"] = step4_waiting
                progress["run_total"] = run_total
                progress["run_scraped"] = int(stats.get("scraped_count") or 0)
                progress["run_failed"] = int(stats.get("failed_count") or 0)
                progress["run_pasted"] = int(stats.get("scrapesheet_pasted_count") or 0)
                progress["run_display_done"] = int(stats.get("display_done_count") or 0)
                progress["run_current"] = int(stats.get("current_lead") or 1)
                progress["run_done"] = progress["run_scraped"] + progress["run_failed"]
                if sheet_rows is not None:
                    sheet_status = self._scrapesheet_status_from_rows(sheet_rows)
                    progress["scrape_sheet_total"] = sheet_status.get("rows_with_link", 0)
                    progress["scrape_sheet_need_scraping"] = sheet_status.get(
                        "rows_need_scraping", 0
                    )
                    progress["scrape_sheet_with_paste"] = sheet_status.get(
                        "rows_with_paste", 0
                    )
        elif batch_file and step4 and step4.status in (
            StepStatus.RUNNING,
            StepStatus.WAITING,
        ):
            target_rows_set = {int(r) for r in batch_file.get("target_rows", [])}
            run_total = int(batch_file.get("total_to_scrape") or len(target_rows_set))
            if target_rows_set and run_total > 0:
                try:
                    batch = self.get_scrapesheet_batch_progress(target_rows_set)
                except sheets.SheetsError:
                    batch = self._last_batch_progress
                if batch:
                    done_count = int(batch["done"])
                    if done_count < run_total:
                        self._apply_run_progress_fields(
                            progress,
                            run_total=run_total,
                            step4_waiting=True,
                            target_set=target_rows_set,
                            batch=batch,
                            sheet_rows=sheet_rows,
                        )

        self._last_list_progress = progress
        return progress

    def ensure_queue_sheet(self) -> None:
        sheets.ensure_worksheet(self.settings.sheet_scrape_queue, SCRAPE_SHEET_HEADERS)

    def get_status(self) -> dict[str, Any]:
        active = self._state.load()
        pending = self.count_pending()
        failed_retryable = self.count_failed_retryable()
        if active:
            idle = False
            link, data = sheets.read_scrape_queue_row(
                self.settings.sheet_scrape_queue,
                SCRAPE_SHEET_ROW,
                use_cache=True,
            )
            queue_row = {
                COL_SCRAPE_LINK: link or active.link,
                COL_SCRAPE_DATA: data,
            }
        else:
            queue_row = self._read_queue_row()
            idle = self.queue_is_idle(queue_row)
        return {
            "pending": pending,
            "failed_retryable": failed_retryable,
            "queue_idle": idle,
            "queue_row": queue_row if not idle else None,
            "active_state": active.__dict__ if active else None,
            "sheet_scrape_queue": self.settings.sheet_scrape_queue,
            "sheet_dynamic_lead": self.settings.sheet_dynamic_lead,
        }

    def count_pending(self) -> int:
        try:
            return sum(
                1
                for row_index, link, _name, activity, scrape_len in self._lead_index_rows(
                    use_tick_cache=True
                )
                if self._index_row_needs_scrape(
                    row_index, link, activity, scrape_len
                )
            )
        except sheets.SheetsError as exc:
            logger.warning("count_pending failed: %s", exc)
            return 0

    def count_failed_retryable(self) -> int:
        try:
            return sum(
                1
                for row_index, link, _name, activity, scrape_len in self._lead_index_rows(
                    use_tick_cache=True
                )
                if self._index_row_needs_retry(row_index, link, activity, scrape_len)
            )
        except sheets.SheetsError as exc:
            logger.warning("count_failed_retryable failed: %s", exc)
            return 0

    def queue_is_idle(self, queue_row: dict[str, Any] | None = None) -> bool:
        row = queue_row if queue_row is not None else self._read_queue_row()
        return not self._field(row, COL_SCRAPE_LINK)

    def enqueue_next_lead(self) -> EnqueueResult:
        """Bootstrap first link when scrapesheet row 2 has no link."""
        return self._start_lead(require_idle=True, ensure_sheet=False)

    def ensure_next_lead_queued(self) -> EnqueueResult:
        """
        Manual Step 3 — refresh sheet index, clear stale A2, enqueue next lead.

        Use when tick/bootstrap did not hand off (stale cache, stuck scrapesheet link).
        """
        sheets.invalidate_lead_index_cache(self.settings.sheet_dynamic_lead)
        self._reset_tick_cache()

        if self._state.load() is not None:
            active = self._state.load()
            return EnqueueResult(
                ok=True,
                message=f"Scrape in progress for row {active.source_row}",
                source_row=active.source_row,
                link=active.link,
            )

        queue_row = self._read_queue_row(fresh=True)
        if not self.queue_is_idle(queue_row):
            logger.info("Clearing stale scrapesheet link before enqueue")
            sheets.clear_scrape_queue_link(
                self.settings.sheet_scrape_queue, SCRAPE_SHEET_ROW
            )
            self._state.clear()

        pending = self.count_pending()
        retryable = self.count_failed_retryable()
        if pending == 0 and retryable == 0:
            return EnqueueResult(ok=True, message="No leads need scraping")

        if pending == 0 and retryable > 0:
            logger.info(
                "Retry round — %s scrape_failed lead(s) eligible for retry",
                retryable,
            )

        return self.enqueue_next_lead()

    def advance_to_next_lead(self) -> EnqueueResult:
        """Manual/API — advance link to next pending lead without clearing data."""
        _, current_data = self._read_scrape_cells(fresh=False)
        return self._handoff_next_lead(pasted_data=current_data)

    def finalize_if_ready(self) -> FinalizeResult:
        queue_row = self._read_queue_row()
        return self._finalize_from_queue_row(
            queue_row,
            scrape_text=self._field(queue_row, COL_SCRAPE_DATA),
        )

    def tick(self) -> dict[str, Any]:
        with self._tick_lock:
            try:
                return self._tick_impl()
            except sheets.SheetsError as exc:
                return {
                    "finalize": FinalizeResult(
                        ok=True,
                        message=str(exc),
                        action="cooldown",
                    ),
                    "enqueue": None,
                }
            except Exception as exc:
                coerced = sheets.coerce_quota_error(exc)
                if coerced is not None:
                    return {
                        "finalize": FinalizeResult(
                            ok=True,
                            message=str(coerced),
                            action="cooldown",
                        ),
                        "enqueue": None,
                    }
                raise

    def _tick_impl(self) -> dict[str, Any]:
        if not self._scrapesheet_session_active():
            try:
                rows = sheets.read_scrapesheet_rows(self.settings.sheet_scrape_queue)
                has_data = any(
                    link.strip() or data.strip() for _, link, data in rows
                )
            except sheets.SheetsError:
                has_data = not self.queue_is_idle(self._read_queue_row(fresh=False))
            if has_data:
                logger.info(
                    "Clearing scrapesheet — Step 4 scrape session not active"
                )
                self._clear_scrapesheet_links()
                self._state.clear()
            return {
                "finalize": FinalizeResult(
                    ok=True,
                    message="scrapesheet idle",
                    action="idle",
                ),
                "enqueue": None,
            }

        active = self._state.load()
        # Only drop lead-index cache when bootstrapping idle queue (not every poll)
        if active is None:
            self._reset_tick_cache()

        synced = self._sync_scrapesheet_pastes_to_leads()
        if synced:
            logger.info(
                "Synced %d scrapesheet paste(s) to Dynamic Lead Sheet",
                synced,
            )

        if sheets.is_quota_cooldown():
            return {
                "finalize": FinalizeResult(
                    ok=True,
                    message=(
                        f"Sheets quota cooldown "
                        f"({sheets.quota_cooldown_remaining_secs():.0f}s)"
                    ),
                    action="cooldown",
                ),
                "enqueue": None,
            }

        queue_row = self._read_queue_row(fresh=False)
        scrape_text = self._field(queue_row, COL_SCRAPE_DATA)
        finalize = self._finalize_from_queue_row(queue_row, scrape_text=scrape_text)
        enqueue = None

        if finalize.action in {"success", "failed"}:
            handoff_msg = finalize.stats.get("handoff_message")
            if handoff_msg:
                enqueue = EnqueueResult(
                    ok=True,
                    message=handoff_msg,
                    source_row=finalize.stats.get("handoff_source_row"),
                    link=finalize.stats.get("handoff_link"),
                )
        elif finalize.action in {"idle", "waiting", "stabilizing", "none", "retry"}:
            pass
        elif finalize.action == "error":
            logger.error("Finalize stuck: %s — attempting recovery", finalize.message)
            recovery = self._recover_stale_link(queue_row)
            if isinstance(recovery, FinalizeResult):
                finalize = recovery
            elif recovery is not None:
                scrape_text = self._field(queue_row, COL_SCRAPE_DATA)
                finalize = self._finalize_from_queue_row(
                    queue_row, scrape_text=scrape_text
                )
            else:
                pasted = self._field(queue_row, COL_SCRAPE_DATA)
                handoff = self._handoff_next_lead(pasted)
                finalize = FinalizeResult(
                    ok=True,
                    message=f"Unknown A2 link — {handoff.message}",
                    action="success" if handoff.source_row else "idle",
                    stats={
                        "handoff_message": handoff.message,
                        "handoff_source_row": handoff.source_row,
                        "handoff_link": handoff.link,
                    },
                )
            if finalize.action in {"success", "failed"}:
                handoff_msg = finalize.stats.get("handoff_message")
                if handoff_msg:
                    enqueue = EnqueueResult(
                        ok=True,
                        message=handoff_msg,
                        source_row=finalize.stats.get("handoff_source_row"),
                        link=finalize.stats.get("handoff_link"),
                    )

        bootstrap = self._bootstrap_next_lead(queue_row)
        if bootstrap is not None and (enqueue is None or enqueue.source_row is None):
            enqueue = bootstrap

        return {"finalize": finalize, "enqueue": enqueue}

    def _bootstrap_next_lead(self, queue_row: dict[str, Any]) -> EnqueueResult | None:
        """Start the next pending or retryable-failed lead when scrapesheet is idle."""
        if not self._scrapesheet_session_active():
            return None
        if self._state.load() is not None:
            return None
        if not self.queue_is_idle(queue_row):
            return None

        self._reset_tick_cache()
        pending = self.count_pending()
        retryable = self.count_failed_retryable()
        if pending == 0 and retryable == 0:
            return None

        if pending == 0 and retryable > 0:
            logger.info(
                "Retry round — %s scrape_failed lead(s) eligible for retry",
                retryable,
            )

        return self.enqueue_next_lead()

    def _start_lead(
        self,
        *,
        require_idle: bool,
        source_row: int | None = None,
        link: str | None = None,
        business_name: str | None = None,
        ensure_sheet: bool = True,
        retry_failed: bool = False,
    ) -> EnqueueResult:
        if not self._scrapesheet_session_active():
            return EnqueueResult(
                ok=True,
                message="Scrapesheet idle until Step 4 runs",
            )

        if ensure_sheet:
            self.ensure_queue_sheet()

        current_link, current_data = sheets.read_scrape_queue_row(
            self.settings.sheet_scrape_queue, SCRAPE_SHEET_ROW, use_cache=True
        )

        if require_idle and current_link:
            active = self._state.load()
            return EnqueueResult(
                ok=True,
                message="scrapesheet row 2 already has a link",
                source_row=active.source_row if active else None,
                link=current_link,
            )

        if source_row is None or link is None:
            pending = self._find_next_lead()
            if not pending:
                if current_link:
                    sheets.clear_scrape_queue_link(
                        self.settings.sheet_scrape_queue, SCRAPE_SHEET_ROW
                    )
                self._state.clear()
                return EnqueueResult(ok=True, message="Queue complete — no more leads")
            source_row, lead = pending
            link = str(lead.get(COL_FACEBOOK_LINK) or "").strip()
            business_name = str(lead.get("Business Name") or "").strip()
            retry_failed = self._is_failed_retry(lead, source_row)

        link = link.strip()
        business_name = business_name or ""

        if (
            current_link
            and normalize_facebook_url(current_link) == normalize_facebook_url(link)
        ):
            scrape_len = self._scrape_len_for_row(source_row)
            if scrape_len >= self.settings.scrape_min_length:
                logger.warning(
                    "Row %s already scraped (%d chars) but same link in A2 — advancing to next lead",
                    source_row,
                    scrape_len,
                )
                alt = self._find_next_lead(exclude_row=source_row)
                if alt:
                    alt_row, alt_lead = alt
                    alt_link = str(alt_lead.get(COL_FACEBOOK_LINK) or "").strip()
                    return self._start_lead(
                        require_idle=False,
                        source_row=alt_row,
                        link=alt_link,
                        business_name=str(alt_lead.get("Business Name") or "").strip(),
                        ensure_sheet=False,
                        retry_failed=self._is_failed_retry(alt_lead, alt_row),
                    )
                if current_link:
                    sheets.clear_scrape_queue_link(
                        self.settings.sheet_scrape_queue, SCRAPE_SHEET_ROW
                    )
                self._state.clear()
                return EnqueueResult(ok=True, message="Queue complete — no more leads")

            next_pending = self._find_next_lead(exclude_row=source_row)
            if next_pending:
                next_row, next_lead = next_pending
                next_link = str(next_lead.get(COL_FACEBOOK_LINK) or "").strip()
                if normalize_facebook_url(next_link) != normalize_facebook_url(link):
                    return self._start_lead(
                        require_idle=False,
                        source_row=next_row,
                        link=next_link,
                        business_name=str(next_lead.get("Business Name") or "").strip(),
                        ensure_sheet=False,
                        retry_failed=self._is_failed_retry(next_lead, next_row),
                    )

        sheets.update_scrape_queue_link(
            self.settings.sheet_scrape_queue,
            SCRAPE_SHEET_ROW,
            link,
        )
        sheets.clear_scrape_queue_data(
            self.settings.sheet_scrape_queue,
            SCRAPE_SHEET_ROW,
        )
        verify_link, post_link_data = sheets.read_scrape_queue_row(
            self.settings.sheet_scrape_queue, SCRAPE_SHEET_ROW, use_cache=False
        )
        if normalize_facebook_url(verify_link) != normalize_facebook_url(link):
            logger.error(
                "Link write verification failed — wanted %s got %s",
                link,
                verify_link,
            )
            raise sheets.SheetsError(
                f"Scrapesheet link did not update (wanted row {source_row})"
            )
        logger.info("Column A updated → row %s (%s)", source_row, link[:60])

        b_cleared_after_link = b_is_empty(post_link_data)
        baseline_b_hash = ""
        phase = PHASE_AWAITING_PASTE
        if b_cleared_after_link:
            logger.info(
                "Column B cleared for row %s — awaiting MMM paste",
                source_row,
            )
        else:
            logger.warning(
                "Column B still has content after clear for row %s (%d chars) — "
                "ownership may reject until MMM replaces it",
                source_row,
                len(post_link_data.strip()),
            )
            baseline_b_hash = self._baseline_for_new_owner(post_link_data)
            phase = PHASE_AWAITING_CLEAR

        row_updates: dict[str, Any] = {}
        if retry_failed:
            row_updates[COL_SCRAPE] = ""
            logger.info(
                "Retry scrape row %s (attempt %s/%s)",
                source_row,
                self._failures.count(source_row) + 1,
                self.settings.scrape_max_failures,
            )
        logger.info("Handoff → row %s (%s)", source_row, link)

        if row_updates and self._pipeline_page_scrape_active():
            sheets.update_row_by_header(
                self.settings.sheet_dynamic_lead,
                source_row,
                row_updates,
            )
        elif row_updates:
            logger.debug(
                "Retry clear scrape (Step 4 not running) — skipping sheet write for row %s",
                source_row,
            )

        prev_state = self._state.load()
        attempt_num = (
            (self._failures.count(source_row) + 1)
            if retry_failed
            else (prev_state.attempt if prev_state and prev_state.source_row == source_row else 1)
        )

        self._state.save(
            ActiveScrapeState(
                source_row=source_row,
                attempt=attempt_num,
                link=link,
                business_name=business_name,
                phase=phase,
                link_set_at=datetime.now(timezone.utc).isoformat(),
                baseline_b_hash=baseline_b_hash,
                last_b_hash=baseline_b_hash,
                consumed_paste_hash="",
                poll_count_since_link=0,
            )
        )
        label = business_name or link or source_row
        if retry_failed:
            msg = (
                f"Retry {attempt_num}/{self.settings.scrape_max_failures} "
                f"for row {source_row} ({label})"
            )
        else:
            msg = f"Link set for row {source_row} ({label})"
        return EnqueueResult(
            ok=True,
            message=msg,
            source_row=source_row,
            link=link,
        )

    def _handoff_next_lead(
        self,
        pasted_data: str = "",
        *,
        exclude_row: int | None = None,
        business_name: str = "",
    ) -> EnqueueResult:
        """
        Save column B to owner row, then change column A to next lead.
        Never advances A unless save succeeds (or exclude_row is None).
        """
        saved_chars = 0
        saved_to_row: int | None = None

        if exclude_row is not None:
            _, scrape_text = self._read_scrape_cells(fresh=True)
            if not (scrape_text or "").strip() and pasted_data:
                scrape_text = pasted_data
            active = self._state.load()
            biz = business_name or (active.business_name if active else "")
            try:
                saved_chars = self._write_scrape_to_dynamic_lead(
                    exclude_row,
                    scrape_text,
                    business_name=biz,
                    ownership_verified=True,
                )
            except sheets.SheetsError as exc:
                logger.warning(
                    "Chain save failed for owner row %s — link unchanged: %s",
                    exclude_row,
                    exc,
                )
                return EnqueueResult(
                    ok=True,
                    message=f"Save failed for row {exclude_row}: {exc}",
                    source_row=exclude_row,
                )

            if saved_chars:
                saved_to_row = self._dynamic_lead_write_row(exclude_row)
                logger.info(
                    "Chain: column B → Dynamic Lead row %s (owner %s, %d chars) → changing link",
                    saved_to_row,
                    exclude_row,
                    saved_chars,
                )
                if active:
                    active.consumed_paste_hash = hash_paste_text(
                        self._sanitize_scrape_text(scrape_text)
                    )
                    self._state.save(active)
            else:
                logger.warning(
                    "Chain blocked — paste in B not saved for row %s (%s)",
                    exclude_row,
                    biz or "unknown",
                )
                _, scrape_text = self._read_scrape_cells(fresh=True)
                paste_text = scrape_text or pasted_data
                ownership = self._evaluate_paste(
                    active or ActiveScrapeState(
                        source_row=exclude_row,
                        attempt=1,
                        link="",
                        business_name=biz,
                    ),
                    paste_text,
                )
                if ownership.status == PasteOwnershipStatus.WRONG_LEAD:
                    alert_from_ownership(
                        source_row=exclude_row,
                        business_name=biz,
                        link=active.link if active else "",
                        ownership=ownership,
                        paste_chars=len((paste_text or "").strip()),
                    )
                else:
                    notify_scrape_paste_mistake(
                        source_row=exclude_row,
                        business_name=biz,
                        link=active.link if active else "",
                        code="not_saved",
                        detail=(
                            "Paste on scrapesheet column B was not written to "
                            f"Dynamic Lead row {self._dynamic_lead_write_row(exclude_row)} — "
                            f"{ownership_action_label(ownership)}"
                        ),
                        paste_chars=len((paste_text or "").strip()),
                    )
                return EnqueueResult(
                    ok=True,
                    message=(
                        f"Paste rejected for row {exclude_row} — "
                        "waiting for MMM to paste matching scrape"
                    ),
                    source_row=exclude_row,
                )

        pending = self._find_next_lead(exclude_row=exclude_row)
        if not pending and exclude_row is not None:
            pending = self._find_next_lead()
        if not pending:
            queue_row = self._read_queue_row()
            if not self.queue_is_idle(queue_row):
                sheets.clear_scrape_queue_link(
                    self.settings.sheet_scrape_queue, SCRAPE_SHEET_ROW
                )
            self._state.clear()
            return EnqueueResult(
                ok=True,
                message="Pipeline complete — no more leads",
                saved_chars=saved_chars,
                saved_to_row=saved_to_row,
            )

        source_row, lead = pending
        link = str(lead.get(COL_FACEBOOK_LINK) or "").strip()
        lead_business_name = str(lead.get("Business Name") or "").strip()
        result = self._start_lead(
            require_idle=False,
            source_row=source_row,
            link=link,
            business_name=lead_business_name,
            ensure_sheet=False,
            retry_failed=self._is_failed_retry(lead, source_row),
        )
        result.saved_chars = saved_chars
        result.saved_to_row = saved_to_row
        return result

    def _finalize_from_queue_row(
        self,
        queue_row: dict[str, Any],
        *,
        scrape_text: str | None = None,
    ) -> FinalizeResult:
        if self.queue_is_idle(queue_row):
            if self._state.load() is not None:
                self._state.clear()
            return FinalizeResult(ok=True, message="scrapesheet idle", action="idle")

        active = self._state.load()
        if active is None:
            active = self._resolve_active(queue_row)
        if active is None:
            recovery = self._recover_stale_link(queue_row)
            if isinstance(recovery, FinalizeResult):
                return recovery
            active = recovery
        if active is None:
            link = self._field(queue_row, COL_SCRAPE_LINK)
            return FinalizeResult(
                ok=False,
                message=f"No scrape state for link: {link}",
                action="error",
            )

        active = self._reconcile_active_with_column_a(queue_row, active)
        if active is None:
            return FinalizeResult(ok=True, message="scrapesheet idle", action="idle")

        if scrape_text is None:
            _, scrape_text = self._read_scrape_cells(fresh=True)

        active = self._update_phase_from_b(active, scrape_text)
        self._state.save(active)

        if b_is_empty(scrape_text):
            return FinalizeResult(
                ok=True,
                message="Waiting for MMM to paste scrape data",
                action="waiting",
                source_row=active.source_row,
            )

        if not self._paste_is_ready(scrape_text):
            return FinalizeResult(
                ok=True,
                message=f"Paste growing ({len(scrape_text.strip())} chars) — waiting",
                action="stabilizing",
                source_row=active.source_row,
            )

        paste_hash = hash_paste_text(self._sanitize_scrape_text(scrape_text))
        if active.baseline_b_hash and paste_hash == active.baseline_b_hash:
            return FinalizeResult(
                ok=True,
                message=(
                    "Waiting for MMM paste — column B unchanged since link handoff "
                    f"(poll {active.poll_count_since_link})"
                ),
                action="waiting",
                source_row=active.source_row,
            )

        ownership = self._evaluate_paste(active, scrape_text)
        action_label = ownership_action_label(ownership)

        if paste_belongs_to_intended(ownership, active.source_row):
            logger.info(
                "SAVE row %s %s (%s) — %d chars",
                active.source_row,
                active.business_name,
                action_label,
                len(scrape_text.strip()),
            )
            return self._advance_on_paste(active, scrape_text)

        if ownership.status == PasteOwnershipStatus.WRONG_LEAD:
            logger.info(
                "WAIT row %s %s — %s",
                active.source_row,
                active.business_name,
                action_label,
            )
            alert_from_ownership(
                source_row=active.source_row,
                business_name=active.business_name,
                link=active.link,
                ownership=ownership,
                paste_chars=len(scrape_text.strip()),
            )
            return FinalizeResult(
                ok=True,
                message=f"Waiting for MMM paste — {action_label}",
                action="stabilizing",
                source_row=active.source_row,
            )

        if "not matched" in (ownership.reason or "").lower():
            alert_from_ownership(
                source_row=active.source_row,
                business_name=active.business_name,
                link=active.link,
                ownership=ownership,
                paste_chars=len(scrape_text.strip()),
            )

        if active.phase == PHASE_AWAITING_CLEAR:
            return FinalizeResult(
                ok=True,
                message=(
                    f"Waiting for MMM to clear column B "
                    f"(poll {active.poll_count_since_link})"
                ),
                action="waiting",
                source_row=active.source_row,
            )

        stall_polls = self._stall_poll_limit(active, ownership.reason)
        if active.poll_count_since_link >= stall_polls:
            return self._finalize_stall_timeout(active, ownership.reason)

        return FinalizeResult(
            ok=True,
            message=f"Waiting for MMM paste — {action_label}",
            action="stabilizing",
            source_row=active.source_row,
        )

    def _finalize_stall_timeout(
        self, active: ActiveScrapeState, reason: str
    ) -> FinalizeResult:
        logger.warning(
            "Stall timeout row %s (%s) after %s polls — %s",
            active.source_row,
            active.business_name,
            active.poll_count_since_link,
            reason,
        )
        notify_scrape_paste_mistake(
            source_row=active.source_row,
            business_name=active.business_name,
            link=active.link,
            code="stall_failed",
            detail=f"No valid paste saved after {active.poll_count_since_link} polls ({reason})",
        )
        return self._finalize_failed(
            active,
            f"no valid paste after {active.poll_count_since_link} polls ({reason})",
        )

    def _advance_on_paste(
        self,
        active: ActiveScrapeState,
        scrape_text: str,
    ) -> FinalizeResult:
        """Save column B to owner row, then change column A (handoff chain)."""
        active = self._state.load() or active
        owner_row = active.source_row

        try:
            handoff = self._handoff_next_lead(
                scrape_text,
                exclude_row=owner_row,
                business_name=active.business_name,
            )
        except sheets.SheetsError as exc:
            logger.error("Handoff chain failed for row %s: %s", owner_row, exc)
            return FinalizeResult(
                ok=False,
                message=f"Handoff failed for row {owner_row}: {exc}",
                action="error",
                source_row=owner_row,
            )

        saved_chars = handoff.saved_chars
        if not saved_chars:
            return FinalizeResult(
                ok=True,
                message=handoff.message,
                action="stabilizing",
                source_row=owner_row,
            )

        saved_to = handoff.saved_to_row
        save_note = f"saved {saved_chars} chars to row {saved_to}"
        logger.info(
            "Handoff chain row %s — %s; column A → row %s (%s)",
            owner_row,
            save_note,
            handoff.source_row,
            handoff.link or "idle",
        )

        if not self._pipeline_page_scrape_active():
            self._handled.add(owner_row)

        self._failures.clear(owner_row)
        self._reset_tick_cache()
        resolve_mistake_alerts(owner_row)

        stats: dict[str, Any] = {
            "attempt": active.attempt,
            "saved_chars": saved_chars,
            "saved_to_row": saved_to,
            "handoff_message": handoff.message,
            "handoff_source_row": handoff.source_row,
            "handoff_link": handoff.link,
        }
        return FinalizeResult(
            ok=True,
            message=f"Chain row {owner_row} ({save_note}) — {handoff.message}",
            action="success",
            source_row=owner_row,
            stats=stats,
        )

    def _dynamic_lead_write_row(self, owner_row: int) -> int | None:
        """Map queue owner row to Dynamic Lead Sheet row."""
        target = owner_row + SCRAPE_WRITE_ROW_OFFSET
        if target < 2:
            logger.warning(
                "Invalid write target row %s (owner %s)",
                target,
                owner_row,
            )
            return None
        return target

    def _write_scrape_to_dynamic_lead(
        self,
        source_row: int,
        scrape_text: str,
        *,
        business_name: str = "",
        ownership_verified: bool = False,
    ) -> int:
        """Copy scrapesheet column B into Dynamic Lead Sheet Scrape for the owner lead."""
        write_row = self._dynamic_lead_write_row(source_row)
        if write_row is None:
            return 0

        cleaned = self._sanitize_scrape_text(scrape_text)
        if len(cleaned.strip()) < self.settings.scrape_min_length:
            logger.info(
                "Skipping Dynamic Lead write for row %s — paste too short (%d chars)",
                write_row,
                len(cleaned.strip()),
            )
            return 0

        if not ownership_verified:
            active = self._state.load()
            phase = active.phase if active and active.source_row == source_row else PHASE_READY
            baseline = active.baseline_b_hash if active and active.source_row == source_row else ""
            consumed = active.consumed_paste_hash if active and active.source_row == source_row else ""

            ownership = evaluate_paste_for_intended(
                cleaned,
                source_row,
                business_name,
                self._ownership_rows(),
                min_length=self.settings.scrape_min_length,
                phase=phase,
                baseline_b_hash=baseline,
                consumed_paste_hash=consumed,
            )
            if not paste_belongs_to_intended(ownership, source_row):
                label = ownership_action_label(ownership)
                logger.warning(
                    "Skipping Dynamic Lead write for row %s — %s",
                    write_row,
                    label,
                )
                alert_from_ownership(
                    source_row=source_row,
                    business_name=business_name,
                    link="",
                    ownership=ownership,
                    paste_chars=len(cleaned.strip()),
                )
                return 0

        row_updates: dict[str, Any] = {COL_SCRAPE: cleaned}

        sheets.update_row_by_header(
            self.settings.sheet_dynamic_lead,
            write_row,
            row_updates,
        )
        logger.info(
            "Scrape saved → Dynamic Lead row %s (owner %s, %d chars)",
            write_row,
            source_row,
            len(cleaned),
        )
        return len(cleaned)

    def _resolve_active(self, queue_row: dict[str, Any]) -> ActiveScrapeState | None:
        link = self._field(queue_row, COL_SCRAPE_LINK)
        if not link:
            return None

        match = self._find_any_row_by_link(link)
        if match is None:
            return None

        source_row, row = match
        if self._row_has_scrape_data(self._scrape_len_for_row(source_row)):
            return None

        business_name = str(row.get("Business Name") or "").strip()

        state = ActiveScrapeState(
            source_row=source_row,
            attempt=1,
            link=link,
            business_name=business_name,
            phase=PHASE_AWAITING_CLEAR,
            link_set_at=datetime.now(timezone.utc).isoformat(),
            baseline_b_hash="",
            last_b_hash="",
            consumed_paste_hash="",
            poll_count_since_link=0,
        )
        self._state.save(state)
        logger.info(
            "Recovered active state from A link → row %s (awaiting B clear/paste)",
            source_row,
        )
        return state

    def _recover_stale_link(self, queue_row: dict[str, Any]) -> FinalizeResult | ActiveScrapeState | None:
        """
        Recover when scrapesheet has a link but active.json was lost (restart)
        or MMM re-scraped a row that was already saved.
        """
        link = self._field(queue_row, COL_SCRAPE_LINK)
        if not link:
            return None

        match = self._find_any_row_by_link(link)
        if match is None:
            return None

        source_row, row = match
        activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
        pasted = self._field(queue_row, COL_SCRAPE_DATA)

        if self._row_has_scrape_data(self._scrape_len_for_row(source_row)):
            logger.info(
                "Row %s already scraped — handing off next lead (stale link in A2)",
                source_row,
            )
            handoff = self._handoff_next_lead(exclude_row=None)
            return FinalizeResult(
                ok=True,
                message=f"Row {source_row} already scraped → {handoff.message}",
                action="success",
                source_row=source_row,
                stats={
                    "handoff_message": handoff.message,
                    "handoff_source_row": handoff.source_row,
                    "handoff_link": handoff.link,
                },
            )

        if self._is_permanently_failed(source_row):
            logger.info(
                "Row %s permanently failed — handing off next lead",
                source_row,
            )
            handoff = self._handoff_next_lead(exclude_row=None)
            return FinalizeResult(
                ok=True,
                message=f"Row {source_row} permanently failed → {handoff.message}",
                action="failed",
                source_row=source_row,
                stats={
                    "handoff_message": handoff.message,
                    "handoff_source_row": handoff.source_row,
                    "handoff_link": handoff.link,
                },
            )
        if self._failures.count(source_row) > 0 or is_scrape_failed_activity(activity):
            logger.info("Row %s failed — retrying scrape", source_row)
            return self._resolve_active(queue_row)

        return self._resolve_active(queue_row)

    def _read_scrape_cells(self, *, fresh: bool = False) -> tuple[str, str]:
        link, data = sheets.read_scrape_queue_row(
            self.settings.sheet_scrape_queue,
            SCRAPE_SHEET_ROW,
            use_cache=not fresh,
        )
        # Stale cache can show empty A while MMM already has a link — never trust empty from cache
        if not fresh and not link.strip():
            link, data = sheets.read_scrape_queue_row(
                self.settings.sheet_scrape_queue,
                SCRAPE_SHEET_ROW,
                use_cache=False,
            )
        return link, data

    def _read_scrape_data_fresh(self) -> tuple[str, str]:
        _, scrape_text = self._read_scrape_cells(fresh=True)
        return scrape_text, self._hash_data(scrape_text)

    @staticmethod
    def _sanitize_scrape_text(text: str) -> str:
        """Collapse MMM noise (repeated lines, Facebook spam) before saving."""
        lines = (text or "").splitlines()
        cleaned: list[str] = []
        seen: set[str] = set()
        facebook_run = 0

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()
            if lower == "facebook":
                facebook_run += 1
                if facebook_run > 2:
                    continue
            else:
                facebook_run = 0
            key = lower
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(stripped)

        result = "\n".join(cleaned).strip()
        if len(result) > PASTE_SANITIZE_MAX_CHARS:
            result = result[:PASTE_SANITIZE_MAX_CHARS]
        return result

    @staticmethod
    def _normalize_scrape_text(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    @staticmethod
    def _hash_data(text: str) -> str:
        normalized = ScrapeQueueService._normalize_scrape_text(text)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    def _find_any_row_by_link(self, link: str) -> tuple[int, dict[str, Any]] | None:
        """Find Dynamic Lead row by Facebook link regardless of activity."""
        target = normalize_facebook_url(link)
        for row_index, row_link, name, activity, scrape_len in self._lead_index_rows(
            include_names=True
        ):
            if normalize_facebook_url(row_link) == target:
                return row_index, {
                    COL_FACEBOOK_LINK: row_link,
                    "Business Name": name,
                    COL_LEAD_ACTIVITY: activity,
                }
        return None

    def _find_source_row_by_link(self, link: str) -> int | None:
        target = normalize_facebook_url(link)
        active = self._state.load()
        if active and normalize_facebook_url(active.link) == target:
            return active.source_row

        pending_row: int | None = None
        for row_index, row_link, _name, activity, scrape_len in self._lead_index_rows():
            if normalize_facebook_url(row_link) != target:
                continue
            if self._index_row_needs_scrape(
                row_index, row_link, activity, scrape_len
            ):
                pending_row = row_index

        return pending_row

    @staticmethod
    def _field(row: dict[str, Any], name: str) -> str:
        target = name.strip().lower()
        for key, value in row.items():
            if str(key).strip().lower() == target:
                return str(value or "").strip()
        return str(row.get(name) or "").strip()

    def _read_queue_row(self, *, fresh: bool = False) -> dict[str, Any]:
        try:
            link, data = self._read_scrape_cells(fresh=fresh)
            return {
                COL_SCRAPE_LINK: link,
                COL_SCRAPE_DATA: data,
            }
        except sheets.SheetsError:
            return {}

    def _finalize_retry(self, active: ActiveScrapeState, reason: str) -> FinalizeResult:
        next_attempt = active.attempt + 1
        self._state.save(
            ActiveScrapeState(
                source_row=active.source_row,
                attempt=next_attempt,
                link=active.link,
                business_name=active.business_name,
                phase=active.phase,
                link_set_at=active.link_set_at,
                baseline_b_hash=active.baseline_b_hash,
                last_b_hash=active.last_b_hash,
                consumed_paste_hash=active.consumed_paste_hash,
                poll_count_since_link=active.poll_count_since_link,
            )
        )
        logger.warning(
            "Bad paste row %s (attempt %s): %s — same link, waiting for MMM re-paste",
            active.source_row,
            active.attempt,
            reason,
        )
        return FinalizeResult(
            ok=True,
            message=f"Bad paste ({reason}) — retry {next_attempt}, link unchanged",
            action="retry",
            source_row=active.source_row,
            stats={"attempt": next_attempt, "reason": reason},
        )

    def _finalize_failed(self, active: ActiveScrapeState, reason: str) -> FinalizeResult:
        failure_count = self._failures.increment(active.source_row)
        max_failures = self.settings.scrape_max_failures
        permanent = failure_count >= max_failures

        activity_label = scrape_failed_activity_label(
            failure_count, max_failures
        )

        self._reset_tick_cache()

        if permanent:
            logger.error(
                "Scrape permanently failed row %s after %s attempts: %s",
                active.source_row,
                failure_count,
                reason,
            )
            fail_msg = (
                f"Row {active.source_row} {activity_label} after {failure_count} attempts"
            )
        else:
            logger.warning(
                "Scrape failed row %s → %s (%s/%s): %s — will retry later",
                active.source_row,
                activity_label,
                failure_count,
                max_failures,
                reason,
            )
            fail_msg = (
                f"Row {active.source_row} → {activity_label} ({failure_count}/{max_failures}) "
                f"— {reason}"
            )

        handoff = self._handoff_next_lead(exclude_row=None)
        return FinalizeResult(
            ok=True,
            message=f"{fail_msg} → {handoff.message}",
            action="failed",
            source_row=active.source_row,
            stats={
                "reason": reason,
                "failure_count": failure_count,
                "permanent": permanent,
                "handoff_message": handoff.message,
                "handoff_source_row": handoff.source_row,
                "handoff_link": handoff.link,
            },
        )

    def _find_next_lead(
        self, exclude_row: int | None = None
    ) -> tuple[int, dict[str, Any]] | None:
        """First pending_scrape row in sheet order (row 2 downward), then scrape_failed retries."""
        self._reset_tick_cache()
        active = self._state.load()
        in_flight_row = active.source_row if active else None
        step4 = self._pipeline_page_scrape_active()
        for row_index, row_link, name, activity, scrape_len in self._lead_index_rows(
            include_names=True
        ):
            if exclude_row is not None and row_index == exclude_row:
                continue
            if in_flight_row is not None and row_index == in_flight_row:
                continue
            if not step4 and self._handled.contains(row_index):
                continue
            if self._index_row_needs_scrape(
                row_index, row_link, activity, scrape_len
            ):
                return row_index, {
                    COL_FACEBOOK_LINK: row_link,
                    "Business Name": name,
                    COL_LEAD_ACTIVITY: activity,
                }
        for row_index, row_link, name, activity, scrape_len in self._lead_index_rows(
            include_names=True
        ):
            if exclude_row is not None and row_index == exclude_row:
                continue
            if in_flight_row is not None and row_index == in_flight_row:
                continue
            if not step4 and self._handled.contains(row_index):
                continue
            if self._index_row_needs_retry(
                row_index, row_link, activity, scrape_len
            ):
                return row_index, {
                    COL_FACEBOOK_LINK: row_link,
                    "Business Name": name,
                    COL_LEAD_ACTIVITY: activity,
                }
        return None

    def _is_failed_retry(
        self, lead: dict[str, Any], source_row: int | None = None
    ) -> bool:
        if source_row is None:
            return False
        return (
            self._failures.count(source_row) > 0
            and not self._is_permanently_failed(source_row)
        )

    def _is_permanently_failed(self, source_row: int) -> bool:
        return (
            self._failures.count(source_row)
            >= self.settings.scrape_max_failures
        )

    def _index_row_needs_scrape(
        self,
        row_index: int,
        link: str,
        activity: str,
        scrape_len: int,
    ) -> bool:
        """True when row has a link, no scrape data yet, and is not permanently failed."""
        if not link.strip():
            return False
        if self._row_has_scrape_data(scrape_len):
            return False
        if self._is_permanently_failed(row_index):
            return False
        return True

    def _index_row_needs_retry(
        self,
        row_index: int,
        link: str,
        activity: str,
        scrape_len: int,
    ) -> bool:
        """True for rows with scrape failures still eligible for another attempt."""
        if not link.strip():
            return False
        if self._row_has_scrape_data(scrape_len):
            return False
        if self._is_permanently_failed(row_index):
            return False
        return self._failures.count(row_index) > 0

    def _row_needs_scrape(self, row: dict[str, Any]) -> bool:
        if not str(row.get(COL_FACEBOOK_LINK) or "").strip():
            return False
        if self._scrape_filled(row):
            return False
        return True

    def _scrape_filled(self, row: dict[str, Any]) -> bool:
        for key in (COL_SCRAPE, "Website Link Scrape"):
            if str(row.get(key) or "").strip():
                return True
        return False


_service: ScrapeQueueService | None = None


def get_scrape_queue() -> ScrapeQueueService:
    global _service
    if _service is None:
        _service = ScrapeQueueService()
    return _service
