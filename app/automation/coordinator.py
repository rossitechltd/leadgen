"""Orchestrates Loop 1 (scrape) and Loop 2 (move-to-top) handoff."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import sheets

from app.automation.mmm_bridge import MmmBridge
from app.automation.signals import CoordinatorPhase, MmmCommand, MmmSignal
from app.config import Settings, get_settings
from app.sheets.columns import (
    COL_BUSINESS_NAME,
    COL_FACEBOOK_LINK,
    COL_LEAD_ACTIVITY,
    COL_WEBSITE_SCRAPE,
    LEAD_ACTIVITY_PENDING,
    LEAD_ACTIVITY_SCRAPED,
    LEAD_ACTIVITY_SCRAPING,
)

logger = logging.getLogger(__name__)


class CoordinatorError(Exception):
    """Raised when MMM coordination fails or times out."""


@dataclass
class MoveToTopResult:
    ok: bool
    message: str
    row_index: int | None = None
    moved_via: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)


class PageScrapeCoordinator:
    """
    Loop 1 runs continuously in Mini Mouse Macro (scrape row 2).
    Before reordering the sheet, Python waits for Loop 1 to reach a SAFE point
    (between scrapes), runs Loop 2 once (or moves via Sheets API), then resumes Loop 1.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self._bridge = MmmBridge(
            coord_dir=self.settings.mmm_coord_dir,
            udp_host=self.settings.mmm_udp_host,
            udp_send_port=self.settings.mmm_udp_send_port,
            udp_recv_port=self.settings.mmm_udp_recv_port,
            enable_udp_listener=self.settings.mmm_udp_enabled,
        )
        self._phase = CoordinatorPhase.IDLE
        if self.settings.mmm_udp_enabled:
            self._bridge.start_listener()

    @property
    def phase(self) -> CoordinatorPhase:
        return self._phase

    def get_status(self) -> dict[str, Any]:
        pending = self._count_pending_rows() if self.settings.sheets_configured else None
        return {
            "phase": self._phase.value,
            "bridge": self._bridge.status.to_dict(),
            "pending_scrape_rows": pending,
            "move_via": self.settings.mmm_move_via,
            "coord_dir": str(self.settings.mmm_coord_dir),
            "udp": {
                "enabled": self.settings.mmm_udp_enabled,
                "send": f"{self.settings.mmm_udp_host}:{self.settings.mmm_udp_send_port}",
                "recv": f"{self.settings.mmm_udp_host}:{self.settings.mmm_udp_recv_port}",
            },
        }

    def start_loop1(self) -> dict[str, Any]:
        with self._lock:
            self._phase = CoordinatorPhase.LOOP1_RUNNING
            self._bridge.set_phase(self._phase.value, "Loop 1 scrape started")
            self._bridge.clear_signal()
            self._bridge.send_command(MmmCommand.START_LOOP1)
        return {"ok": True, "phase": self._phase.value}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._bridge.send_command(MmmCommand.STOP)
            self._phase = CoordinatorPhase.IDLE
            self._bridge.set_phase(self._phase.value, "Stopped")
        return {"ok": True, "phase": self._phase.value}

    def report_mmm_signal(self, signal: str, detail: str = "") -> dict[str, Any]:
        self._bridge.report_signal(signal, detail)
        return {"ok": True, "signal": signal.upper(), "detail": detail}

    def move_to_top(
        self,
        row_index: int | None = None,
        *,
        wait_safe: bool = True,
    ) -> MoveToTopResult:
        """
        Wait for Loop 1 SAFE → move lead to row 2 → resume Loop 1.

        If row_index is omitted, picks the first pending row that is not already row 2.
        """
        if not self.settings.sheets_configured:
            return MoveToTopResult(
                ok=False,
                message=f"Sheets not configured ({self.settings.service_account_path.name})",
            )

        try:
            target_row, row_data = self._resolve_target_row(row_index)
        except CoordinatorError as exc:
            return MoveToTopResult(ok=False, message=str(exc), row_index=row_index)

        if target_row is None:
            return MoveToTopResult(ok=True, message="No row needs moving to top")

        if target_row <= 2:
            return MoveToTopResult(
                ok=True,
                message="Lead already at top (row 2)",
                row_index=target_row,
                moved_via="none",
            )

        label = row_data.get(COL_BUSINESS_NAME) or row_data.get(COL_FACEBOOK_LINK) or target_row
        logger.info("Move to top requested for row %s (%s)", target_row, label)

        with self._lock:
            try:
                should_wait = wait_safe and (
                    self.settings.mmm_move_via == "macro"
                    or self._phase == CoordinatorPhase.LOOP1_RUNNING
                )
                if should_wait:
                    self._wait_for_loop1_safe()

                moved_via = self._perform_move(target_row)

                self._phase = CoordinatorPhase.LOOP1_RUNNING
                self._bridge.clear_signal()
                self._bridge.send_command(MmmCommand.RESUME_LOOP1)
                self._bridge.set_phase(
                    self._phase.value,
                    f"Resumed Loop 1 after moving row {target_row}",
                    last_moved_row=target_row,
                )

                return MoveToTopResult(
                    ok=True,
                    message=f"Moved row {target_row} to top and resumed Loop 1",
                    row_index=target_row,
                    moved_via=moved_via,
                )
            except CoordinatorError as exc:
                self._bridge.set_phase("error", str(exc))
                return MoveToTopResult(ok=False, message=str(exc), row_index=target_row)

    def process_pending_queue(self, *, max_moves: int = 1) -> dict[str, Any]:
        """
        Ensure the next pending scrape lead is at row 2.

        Call this after Step 1 prepends new leads so Loop 1 always scrapes the right row.
        """
        results: list[dict[str, Any]] = []
        for _ in range(max_moves):
            pending = self._find_pending_not_at_top()
            if not pending:
                break
            row_index, _row = pending
            result = self.move_to_top(row_index)
            results.append(
                {
                    "row_index": result.row_index,
                    "ok": result.ok,
                    "message": result.message,
                    "moved_via": result.moved_via,
                }
            )
            if not result.ok:
                break
        return {
            "ok": all(r["ok"] for r in results) if results else True,
            "moves": results,
            "count": len(results),
        }

    def _wait_for_loop1_safe(self) -> None:
        self._phase = CoordinatorPhase.WAITING_SAFE
        self._bridge.set_phase(self._phase.value, "Waiting for Loop 1 safe point")
        self._bridge.clear_signal()
        self._bridge.send_command(MmmCommand.REQUEST_SAFE)

        signal = self._bridge.wait_for_signal(
            MmmSignal.SAFE,
            timeout_secs=self.settings.mmm_safe_timeout_secs,
        )
        if signal != MmmSignal.SAFE:
            raise CoordinatorError(
                f"Timed out waiting for Loop 1 SAFE ({self.settings.mmm_safe_timeout_secs}s). "
                "Ensure MMM Loop 1 signals SAFE between scrapes when REQUEST_SAFE is received."
            )

        self._phase = CoordinatorPhase.LOOP1_SAFE
        self._bridge.set_phase(self._phase.value, "Loop 1 at safe point")

    def _perform_move(self, row_index: int) -> str:
        via = self.settings.mmm_move_via.lower()
        if via == "sheets":
            sheets.move_row_to_top(self.settings.sheet_dynamic_lead, row_index)
            sheets.update_row_by_header(
                self.settings.sheet_dynamic_lead,
                2,
                {COL_LEAD_ACTIVITY: LEAD_ACTIVITY_PENDING},
            )
            return "sheets"

        self._phase = CoordinatorPhase.LOOP2_RUNNING
        self._bridge.set_phase(self._phase.value, f"Running Loop 2 for row {row_index}")
        self._bridge.clear_signal()
        self._bridge.send_command(MmmCommand.RUN_LOOP2, str(row_index))

        signal = self._bridge.wait_for_signal(
            MmmSignal.LOOP2_DONE,
            timeout_secs=self.settings.mmm_loop2_timeout_secs,
        )
        if signal != MmmSignal.LOOP2_DONE:
            raise CoordinatorError(
                f"Timed out waiting for Loop 2 DONE ({self.settings.mmm_loop2_timeout_secs}s)"
            )
        return "macro"

    def _resolve_target_row(
        self, row_index: int | None
    ) -> tuple[int | None, dict[str, Any]]:
        if row_index is not None:
            rows = {
                idx: row
                for idx, row in sheets.read_all_with_row_indices(
                    self.settings.sheet_dynamic_lead
                )
            }
            if row_index not in rows:
                raise CoordinatorError(f"Row {row_index} not found in Dynamic Lead Sheet")
            return row_index, rows[row_index]

        pending = self._find_pending_not_at_top()
        if not pending:
            return None, {}
        return pending

    def _find_pending_not_at_top(self) -> tuple[int, dict[str, Any]] | None:
        for row_index, row in sheets.read_all_with_row_indices(
            self.settings.sheet_dynamic_lead
        ):
            if row_index <= 2:
                continue
            if self._row_needs_scrape(row):
                return row_index, row
        return None

    def _row_needs_scrape(self, row: dict[str, Any]) -> bool:
        scrape = str(row.get(COL_WEBSITE_SCRAPE) or "").strip()
        if scrape:
            return False
        activity = str(row.get(COL_LEAD_ACTIVITY) or "").strip().lower()
        if activity == LEAD_ACTIVITY_SCRAPED:
            return False
        if activity == LEAD_ACTIVITY_SCRAPING:
            return True
        return bool(str(row.get(COL_FACEBOOK_LINK) or "").strip())

    def _count_pending_rows(self) -> int:
        try:
            rows = sheets.read_all(self.settings.sheet_dynamic_lead)
        except sheets.SheetsError:
            return 0
        return sum(1 for row in rows if self._row_needs_scrape(row))

    def shutdown(self) -> None:
        self._bridge.stop_listener()


_coordinator: PageScrapeCoordinator | None = None


def get_coordinator() -> PageScrapeCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = PageScrapeCoordinator()
    return _coordinator
