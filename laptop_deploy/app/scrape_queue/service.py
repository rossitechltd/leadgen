"""Scrape Queue — one lead at row 2 on scrapesheet (link + data)."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import sheets
from app.config import Settings, get_settings
from app.scrapers.lead_mapping import normalize_facebook_url
from app.scrape_queue.state import ActiveScrapeState, get_failure_store, get_state_store
from app.scrape_queue.verify import verify_scrape_text
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
    LEAD_ACTIVITY_SCRAPED,
    LEAD_ACTIVITY_SCRAPING,
    scrape_failed_activity_label,
    SCRAPE_SHEET_HEADERS,
    SCRAPE_SHEET_ROW,
)

logger = logging.getLogger(__name__)

PASTE_MAX_WAIT_SECS = 8
PASTE_SANITIZE_MAX_CHARS = 8000


@dataclass
class EnqueueResult:
    ok: bool
    message: str
    source_row: int | None = None
    link: str | None = None


@dataclass
class FinalizeResult:
    ok: bool
    message: str
    action: str = "none"
    source_row: int | None = None
    stats: dict[str, Any] = field(default_factory=dict)


class ScrapeQueueService:
    """
    Pipelined scrape handoff (scrapesheet row 2 — two columns only).

    Column A = link to scrape. MMM triggers when this changes.
    Column B = scrape text (MMM clears before paste; Python never clears B).

    Stage detection:
    - B hash == baseline → waiting (carried-over text from previous paste)
    - B empty → MMM cleared before paste
    - B hash != baseline + stable → new paste landed → write to state.source_row
      then set column A to the NEXT link so MMM starts the next page.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._state = get_state_store(self.settings)
        self._failures = get_failure_store(self.settings)
        self._tick_lead_index: list[tuple[int, str, str, str, int]] | None = None

    def _reset_tick_cache(self) -> None:
        self._tick_lead_index = None

    def _lead_index_rows(
        self, *, include_names: bool = False
    ) -> list[tuple[int, str, str, str, int]]:
        if self._tick_lead_index is not None:
            rows = self._tick_lead_index
        else:
            rows = sheets.read_dynamic_lead_index(
                self.settings.sheet_dynamic_lead,
                include_names=True,
            )
            self._tick_lead_index = rows
        if include_names:
            return rows
        return [
            (r, link, "", activity, scrape_len)
            for r, link, _name, activity, scrape_len in rows
        ]

    def ensure_queue_sheet(self) -> None:
        sheets.ensure_worksheet(self.settings.sheet_scrape_queue, SCRAPE_SHEET_HEADERS)

    def get_status(self) -> dict[str, Any]:
        pending = self.count_pending()
        failed_retryable = self.count_failed_retryable()
        queue_row = self._read_queue_row()
        idle = self.queue_is_idle(queue_row)
        active = self._state.load()
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
                for row_index, link, _name, activity, scrape_len in self._lead_index_rows()
                if self._index_row_needs_scrape(
                    row_index, link, activity, scrape_len
                )
            )
        except sheets.SheetsError:
            return 0

    def count_failed_retryable(self) -> int:
        try:
            return sum(
                1
                for row_index, link, _name, activity, scrape_len in self._lead_index_rows()
                if self._index_row_needs_retry(row_index, link, activity, scrape_len)
            )
        except sheets.SheetsError:
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
        return self._finalize_from_queue_row(self._read_queue_row())

    def tick(self) -> dict[str, Any]:
        self._reset_tick_cache()

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

        queue_row = self._read_queue_row(fresh=True)
        finalize = self._finalize_from_queue_row(queue_row)
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
                finalize = self._finalize_from_queue_row(queue_row)
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
        data_baseline_hash: str | None = None,
        ensure_sheet: bool = True,
        retry_failed: bool = False,
    ) -> EnqueueResult:
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

        if data_baseline_hash is None:
            data_baseline_hash = self._hash_data(current_data)

        if (
            current_link
            and normalize_facebook_url(current_link) == normalize_facebook_url(link)
        ):
            next_pending = self._find_next_lead(exclude_row=source_row)
            if next_pending:
                next_row, next_lead = next_pending
                next_link = str(next_lead.get(COL_FACEBOOK_LINK) or "").strip()
                if normalize_facebook_url(next_link) != normalize_facebook_url(link):
                    source_row = next_row
                    link = next_link
                    business_name = str(next_lead.get("Business Name") or "").strip()
                    logger.info(
                        "Same link in A2 — advancing to row %s (%s)",
                        source_row,
                        link,
                    )
                else:
                    next_pending = None
            if next_pending is None or normalize_facebook_url(current_link) == normalize_facebook_url(link):
                logger.info(
                    "Handoff skipped — sheet already on row %s (%s)",
                    source_row,
                    link,
                )
                sheets.update_row_by_header(
                    self.settings.sheet_dynamic_lead,
                    source_row,
                    {COL_LEAD_ACTIVITY: LEAD_ACTIVITY_SCRAPING},
                )
                self._state.save(
                    ActiveScrapeState(
                        source_row=source_row,
                        attempt=1,
                        link=link,
                        enqueued_at=datetime.now(timezone.utc).isoformat(),
                        business_name=business_name,
                        data_baseline_hash=data_baseline_hash,
                    )
                )
                return EnqueueResult(
                    ok=True,
                    message=f"Already handed off row {source_row}",
                    source_row=source_row,
                    link=link,
                )

        sheets.update_scrape_queue_link(
            self.settings.sheet_scrape_queue,
            SCRAPE_SHEET_ROW,
            link,
        )
        row_updates: dict[str, Any] = {COL_LEAD_ACTIVITY: LEAD_ACTIVITY_SCRAPING}
        if retry_failed:
            row_updates[COL_SCRAPE] = ""
            attempt_num = self._failures.count(source_row) + 1
            logger.info(
                "Retry scrape row %s (attempt %s/%s)",
                source_row,
                attempt_num,
                self.settings.scrape_max_failures,
            )
        else:
            attempt_num = 1
        logger.info("Handoff → row %s (%s)", source_row, link)

        sheets.update_row_by_header(
            self.settings.sheet_dynamic_lead,
            source_row,
            row_updates,
        )
        self._state.save(
            ActiveScrapeState(
                source_row=source_row,
                attempt=1,
                link=link,
                enqueued_at=datetime.now(timezone.utc).isoformat(),
                business_name=business_name,
                data_baseline_hash=data_baseline_hash,
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
        self, pasted_data: str, *, exclude_row: int | None = None
    ) -> EnqueueResult:
        """After a paste is saved, point column A at the next lead immediately."""
        baseline = self._hash_data(pasted_data)
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
            return EnqueueResult(ok=True, message="Pipeline complete — no more leads")

        source_row, lead = pending
        link = str(lead.get(COL_FACEBOOK_LINK) or "").strip()
        business_name = str(lead.get("Business Name") or "").strip()
        return self._start_lead(
            require_idle=False,
            source_row=source_row,
            link=link,
            business_name=business_name,
            data_baseline_hash=baseline,
            ensure_sheet=False,
            retry_failed=self._is_failed_retry(lead, source_row),
        )

    def _finalize_from_queue_row(self, queue_row: dict[str, Any]) -> FinalizeResult:
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

        scrape_text = self._field(queue_row, COL_SCRAPE_DATA)
        data_hash = self._hash_data(scrape_text)

        if scrape_text and active.consumed_data_hash == data_hash:
            return FinalizeResult(
                ok=True,
                message="Paste already saved — waiting for next link handoff",
                action="stabilizing",
                source_row=active.source_row,
            )

        is_new_paste = bool(
            active.data_baseline_hash and data_hash != active.data_baseline_hash
        )

        if not is_new_paste and active.data_baseline_hash and data_hash == active.data_baseline_hash:
            if scrape_text:
                carried = self._handle_carried_over_paste(active, scrape_text)
                if carried is not None:
                    return carried
            return FinalizeResult(
                ok=True,
                message="Waiting for MMM paste (carried-over data)",
                action="stabilizing",
                source_row=active.source_row,
            )

        if not scrape_text:
            if active.data_hash:
                return FinalizeResult(
                    ok=True,
                    message="MMM cleared data before paste",
                    action="waiting",
                    source_row=active.source_row,
                )
            return FinalizeResult(
                ok=True,
                message="Waiting for MMM to paste scrape data",
                action="waiting",
                source_row=active.source_row,
            )

        # Fast pipeline: verify paste from tick read, then advance link and save
        if is_new_paste and len(scrape_text.strip()) >= self.settings.scrape_min_length:
            data_hash = self._hash_data(scrape_text)
            if active.consumed_data_hash == data_hash:
                return FinalizeResult(
                    ok=True,
                    message="Paste already saved",
                    action="stabilizing",
                    source_row=active.source_row,
                )
            verify = verify_scrape_text(
                scrape_text,
                min_length=self.settings.scrape_min_length,
                business_name=active.business_name,
            )
            if verify.ok:
                return self._finalize_success(active, scrape_text)
            if active.attempt < self.settings.scrape_max_attempts:
                return self._finalize_retry(active, verify.reason)
            return self._finalize_failed(active, verify.reason)

        stable = self._wait_for_stable_data(
            active, scrape_text, data_hash, is_new_paste=is_new_paste
        )
        if stable is not None:
            return stable

        # One fresh read before verify (paste may have grown during stabilize)
        _, scrape_text = self._read_scrape_cells(fresh=True)
        data_hash = self._hash_data(scrape_text)
        if scrape_text and active.consumed_data_hash == data_hash:
            return FinalizeResult(
                ok=True,
                message="Paste already saved — waiting for next link handoff",
                action="stabilizing",
                source_row=active.source_row,
            )

        verify = verify_scrape_text(
            scrape_text,
            min_length=self.settings.scrape_min_length,
            business_name=active.business_name,
        )

        if verify.ok:
            return self._finalize_success(active, scrape_text)

        if active.attempt < self.settings.scrape_max_attempts:
            return self._finalize_retry(active, verify.reason)

        return self._finalize_failed(active, verify.reason)

    def _resolve_active(self, queue_row: dict[str, Any]) -> ActiveScrapeState | None:
        link = self._field(queue_row, COL_SCRAPE_LINK)
        if not link:
            return None

        match = self._find_any_row_by_link(link)
        if match is None:
            return None

        source_row, row = match
        activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
        if activity == LEAD_ACTIVITY_SCRAPED:
            return None

        _, current_data = self._read_scrape_cells(fresh=False)
        business_name = str(row.get("Business Name") or "").strip()
        baseline_hash = self._hash_data(current_data)

        state = ActiveScrapeState(
            source_row=source_row,
            attempt=1,
            link=link,
            business_name=business_name,
            data_baseline_hash=baseline_hash,
        )
        self._state.save(state)
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

        if activity == LEAD_ACTIVITY_SCRAPED:
            logger.info(
                "Row %s already scraped — handing off next lead (stale link in A2)",
                source_row,
            )
            handoff = self._handoff_next_lead(pasted, exclude_row=source_row)
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

        if is_scrape_failed_activity(activity):
            if self._is_permanently_failed(source_row, activity):
                logger.info(
                    "Row %s permanently failed — handing off next lead",
                    source_row,
                )
                handoff = self._handoff_next_lead(pasted, exclude_row=source_row)
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
            logger.info("Row %s failed — retrying scrape", source_row)
            return self._resolve_active(queue_row)

        return self._resolve_active(queue_row)

    def _handle_carried_over_paste(
        self, active: ActiveScrapeState, scrape_text: str
    ) -> FinalizeResult | None:
        """
        B unchanged since handoff — wait for MMM to replace previous lead's paste.

        Do not verify business name here: column B still holds the last saved scrape
        until MMM pastes the new page. Name checks run only when B hash changes.
        """
        if not active.enqueued_at:
            return None

        enqueued = datetime.fromisoformat(active.enqueued_at)
        if enqueued.tzinfo is None:
            enqueued = enqueued.replace(tzinfo=timezone.utc)
        since_enqueue = (datetime.now(timezone.utc) - enqueued).total_seconds()
        if since_enqueue < self.settings.scrape_stall_secs:
            return None

        reason = f"no MMM paste after {int(since_enqueue)}s"
        if active.attempt < self.settings.scrape_max_attempts:
            return self._finalize_retry(active, reason)
        return self._finalize_failed(active, reason)

    def _wait_for_stable_data(
        self,
        active: ActiveScrapeState,
        scrape_text: str,
        data_hash: str,
        *,
        is_new_paste: bool,
    ) -> FinalizeResult | None:
        now = datetime.now(timezone.utc)
        active = self._state.load() or active

        if not scrape_text:
            return FinalizeResult(
                ok=True,
                message="MMM cleared data during stabilize",
                action="waiting",
                source_row=active.source_row,
            )

        text_len = len(scrape_text)
        min_length = self.settings.scrape_min_length

        if not is_new_paste and active.enqueued_at:
            enqueued = datetime.fromisoformat(active.enqueued_at)
            if enqueued.tzinfo is None:
                enqueued = enqueued.replace(tzinfo=timezone.utc)
            since_enqueue = (now - enqueued).total_seconds()
            if since_enqueue < self.settings.scrape_min_start_secs:
                return FinalizeResult(
                    ok=True,
                    message=(
                        f"Waiting for MMM to return "
                        f"({since_enqueue:.0f}s / {self.settings.scrape_min_start_secs:.0f}s)"
                    ),
                    action="stabilizing",
                    source_row=active.source_row,
                )

        if text_len >= min_length and not active.paste_first_seen_at:
            active.paste_first_seen_at = now.isoformat()
            active.paste_length = text_len
            active.data_hash = data_hash
            active.data_stable_at = now.isoformat()
            self._state.save(active)
            logger.info(
                "Paste detected for row %s (%d chars) — stabilizing",
                active.source_row,
                text_len,
            )
            return FinalizeResult(
                ok=True,
                message=f"Paste detected ({text_len} chars) — stabilizing",
                action="stabilizing",
                source_row=active.source_row,
            )

        if active.data_hash != data_hash:
            prev_len = active.paste_length or text_len
            length_delta_pct = abs(text_len - prev_len) / max(prev_len, 1)
            active.data_hash = data_hash
            if length_delta_pct > 0.10:
                active.data_stable_at = now.isoformat()
                active.paste_length = text_len
                if text_len >= min_length and not active.paste_first_seen_at:
                    active.paste_first_seen_at = now.isoformat()
                self._state.save(active)
                logger.info(
                    "Paste growing for row %s (%d chars) — stabilizing",
                    active.source_row,
                    text_len,
                )
                return FinalizeResult(
                    ok=True,
                    message=f"Paste growing ({text_len} chars) — stabilizing",
                    action="stabilizing",
                    source_row=active.source_row,
                )
            self._state.save(active)

        active = self._state.load() or active

        if active.data_stable_at:
            stable_at = datetime.fromisoformat(active.data_stable_at)
            if stable_at.tzinfo is None:
                stable_at = stable_at.replace(tzinfo=timezone.utc)
            elapsed = (now - stable_at).total_seconds()
            if elapsed >= self.settings.scrape_stable_secs:
                return None

        if active.paste_first_seen_at and text_len >= min_length:
            seen_at = datetime.fromisoformat(active.paste_first_seen_at)
            if seen_at.tzinfo is None:
                seen_at = seen_at.replace(tzinfo=timezone.utc)
            paste_age = (now - seen_at).total_seconds()
            if paste_age >= PASTE_MAX_WAIT_SECS:
                _, scrape_text2 = self._read_scrape_cells(fresh=True)
                if len(scrape_text2) == text_len:
                    verify = verify_scrape_text(
                        scrape_text2,
                        min_length=min_length,
                        business_name=active.business_name,
                    )
                    if verify.ok:
                        logger.info(
                            "Paste max-wait finalize for row %s (%d chars, %.0fs)",
                            active.source_row,
                            text_len,
                            paste_age,
                        )
                        return None

        elapsed_msg = ""
        if active.data_stable_at:
            stable_at = datetime.fromisoformat(active.data_stable_at)
            if stable_at.tzinfo is None:
                stable_at = stable_at.replace(tzinfo=timezone.utc)
            elapsed = (now - stable_at).total_seconds()
            elapsed_msg = (
                f"({elapsed:.0f}s / {self.settings.scrape_stable_secs:.0f}s)"
            )

        return FinalizeResult(
            ok=True,
            message=f"Paste stabilizing {elapsed_msg}".strip(),
            action="stabilizing",
            source_row=active.source_row,
        )

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
        scraping_row: int | None = None
        pending_row: int | None = None

        for row_index, row_link, _name, activity, scrape_len in self._lead_index_rows():
            if normalize_facebook_url(row_link) != target:
                continue
            act = activity.strip().lower()
            if act == LEAD_ACTIVITY_SCRAPING:
                scraping_row = row_index
            elif self._index_row_needs_scrape(
                row_index, row_link, activity, scrape_len
            ):
                pending_row = row_index

        return scraping_row or pending_row

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

    def _finalize_success(
        self, active: ActiveScrapeState, scrape_text: str
    ) -> FinalizeResult:
        scrape_text = self._sanitize_scrape_text(scrape_text)
        verify = verify_scrape_text(
            scrape_text,
            min_length=self.settings.scrape_min_length,
            business_name=active.business_name,
        )
        if not verify.ok:
            if active.attempt < self.settings.scrape_max_attempts:
                return self._finalize_retry(active, verify.reason)
            return self._finalize_failed(active, verify.reason)

        source_row = active.source_row

        # 1. Bump column A to the next lead immediately (MMM starts next page)
        handoff = self._handoff_next_lead(scrape_text, exclude_row=source_row)
        logger.info("Link advanced → row %s (%s)", handoff.source_row, handoff.link)

        # 2. Save the previous scrape to Dynamic Lead Sheet
        sheets.update_row_by_header(
            self.settings.sheet_dynamic_lead,
            source_row,
            {
                COL_SCRAPE: scrape_text,
                COL_LEAD_ACTIVITY: LEAD_ACTIVITY_SCRAPED,
            },
        )
        logger.info("Paste saved → Dynamic Lead row %s", source_row)
        self._failures.clear(source_row)
        self._reset_tick_cache()

        stats: dict[str, Any] = {
            "attempt": active.attempt,
            "handoff_message": handoff.message,
            "handoff_source_row": handoff.source_row,
            "handoff_link": handoff.link,
        }
        return FinalizeResult(
            ok=True,
            message=f"Link advanced → saved row {source_row} ({handoff.message})",
            action="success",
            source_row=source_row,
            stats=stats,
        )

    def _finalize_retry(self, active: ActiveScrapeState, reason: str) -> FinalizeResult:
        next_attempt = active.attempt + 1
        _, current_data = self._read_scrape_cells(fresh=False)
        self._state.save(
            ActiveScrapeState(
                source_row=active.source_row,
                attempt=next_attempt,
                link=active.link,
                enqueued_at=datetime.now(timezone.utc).isoformat(),
                business_name=active.business_name,
                data_baseline_hash=self._hash_data(current_data),
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

        sheets.update_row_by_header(
            self.settings.sheet_dynamic_lead,
            active.source_row,
            {COL_LEAD_ACTIVITY: activity_label},
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

        handoff = self._handoff_next_lead(
            self._field(self._read_queue_row(), COL_SCRAPE_DATA),
            exclude_row=active.source_row,
        )
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
        self._reset_tick_cache()
        for row_index, row_link, name, activity, scrape_len in self._lead_index_rows(
            include_names=True
        ):
            if exclude_row is not None and row_index == exclude_row:
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
        activity = str(lead.get(COL_LEAD_ACTIVITY) or "").strip()
        if not is_scrape_failed_activity(activity):
            return False
        if source_row is None:
            return True
        return not self._is_permanently_failed(source_row, activity)

    def _is_permanently_failed(self, source_row: int, activity: str = "") -> bool:
        act = (activity or "").strip().lower()
        if act in {LEAD_ACTIVITY_FAILED_3, "scrape_failed_3"}:
            return True
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
        """True when a lead has a link but no real scrape text yet."""
        if not link.strip():
            return False
        if scrape_len >= self.settings.scrape_min_length:
            return False
        act = activity.strip().lower()
        if is_scrape_failed_activity(act):
            return False
        return True

    def _index_row_needs_retry(
        self,
        row_index: int,
        link: str,
        activity: str,
        scrape_len: int,
    ) -> bool:
        """True for scrape_failed_N rows eligible for another queue round."""
        if not link.strip():
            return False
        if not is_scrape_failed_activity(activity):
            return False
        if scrape_len >= self.settings.scrape_min_length:
            return False
        return not self._is_permanently_failed(row_index, activity)

    def _row_needs_scrape(self, row: dict[str, Any]) -> bool:
        if not str(row.get(COL_FACEBOOK_LINK) or "").strip():
            return False
        if self._scrape_filled(row):
            return False
        activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
        if activity in {LEAD_ACTIVITY_SCRAPED} or is_scrape_failed_activity(activity):
            return False
        return activity in {"", LEAD_ACTIVITY_PENDING, LEAD_ACTIVITY_SCRAPING}

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
