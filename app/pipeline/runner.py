from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import sheets
from app.config import get_settings
from app.pipeline.steps import (
    step1_import_leads,
    step2_dedupe,
    step3_entity_screen,
    step3_page_scrape,
    step4_refine,
    step5_ai_qualify,
    step6_entity_clarify,
    step6_finalize,
    step7_outreach_messages,
)
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus
from app.sheets.client import SheetsClient, get_sheets_client

logger = logging.getLogger(__name__)


@dataclass
class StepDefinition:
    id: int
    name: str
    description: str
    run_fn: Callable[[PipelineContext], StepResult]
    estimated_duration_secs: int = 60


STEP_DEFINITIONS: list[StepDefinition] = [
    StepDefinition(
        id=1,
        name="Import Leads",
        description="Upload Facebook Link + Business Name list to Dynamic Lead Sheet (use dashboard Upload leads)",
        run_fn=step1_import_leads.run,
        estimated_duration_secs=30,
    ),
    StepDefinition(
        id=2,
        name="Dedupe",
        description="Check links against allimported sheet and remove duplicates",
        run_fn=step2_dedupe.run,
        estimated_duration_secs=20,
    ),
    StepDefinition(
        id=3,
        name="Entity Screen",
        description="Remove obvious personal profiles (name + link heuristics + batched AI) before page scrape",
        run_fn=step3_entity_screen.run,
        estimated_duration_secs=45,
    ),
    StepDefinition(
        id=4,
        name="Page Scrape",
        description="Manual only — scrape with MMM and paste into your sheet (not run from this app)",
        run_fn=step3_page_scrape.run,
        estimated_duration_secs=120,
    ),
    StepDefinition(
        id=5,
        name="Refine",
        description="Extract structured fields from Scrape → refined on Dynamic Lead Sheet",
        run_fn=step4_refine.run,
        estimated_duration_secs=120,
    ),
    StepDefinition(
        id=6,
        name="Entity Clarify",
        description="Re-classify entity_uncertain leads using scrape + refined data (remove personal, tag business)",
        run_fn=step6_entity_clarify.run,
        estimated_duration_secs=60,
    ),
    StepDefinition(
        id=7,
        name="AI Qualify",
        description="Check phone + website status; remove ACTIVE, PARKED, and redirect leads",
        run_fn=step5_ai_qualify.run,
        estimated_duration_secs=90,
    ),
    StepDefinition(
        id=8,
        name="Outreach Messages",
        description="Random outreach template → Message1 for surviving leads ({firstname} or there)",
        run_fn=step7_outreach_messages.run,
        estimated_duration_secs=30,
    ),
    StepDefinition(
        id=9,
        name="Finalize",
        description="Move qualified leads to a dated Finalised sheet and clear Dynamic Lead Sheet",
        run_fn=step6_finalize.run,
        estimated_duration_secs=30,
    ),
]

STEP_MAP = {s.id: s for s in STEP_DEFINITIONS}

STOP_PIPELINE_STATUSES = frozenset(
    {StepStatus.FAILED, StepStatus.WAITING, StepStatus.ABANDONED}
)


@dataclass
class StepState:
    id: int
    name: str
    description: str
    status: StepStatus = StepStatus.IDLE
    message: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class PipelineState:
    is_running: bool = False
    current_step_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    last_run_at: str | None = None
    trigger: str | None = None
    running_step_ids: list[int] = field(default_factory=list)
    steps: list[StepState] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    last_run_outcome: str | None = None
    last_run_outcome_message: str | None = None

    def __post_init__(self) -> None:
        if not self.steps:
            self.steps = [
                StepState(id=s.id, name=s.name, description=s.description)
                for s in STEP_DEFINITIONS
            ]


class PipelineRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = PipelineState()
        self._max_logs = 200
        self._abandon_requested = False

    def request_abandon(self) -> dict[str, Any]:
        """Signal the active pipeline run to stop at the next safe checkpoint."""
        if not self._state.is_running:
            return {"ok": False, "error": "No pipeline run in progress"}
        self._abandon_requested = True
        self._append_logs(
            ["Pipeline abandon requested — stopping at next checkpoint"]
        )
        return {"ok": True, "message": "Abandon requested"}

    def mark_step_manual_complete(
        self,
        step_id: int,
        message: str,
        stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mark a step success without re-running it (e.g. manual scrape done off-app)."""
        if step_id not in STEP_MAP:
            return {"ok": False, "error": f"Unknown step id: {step_id}"}
        step_state = next(s for s in self._state.steps if s.id == step_id)
        if self._state.is_running and self._state.current_step_id == step_id:
            return {
                "ok": True,
                "message": "Step is running — signalled via scrape queue",
            }
        step_state.status = StepStatus.SUCCESS
        step_state.message = message
        step_state.stats = stats or {"manual_complete": True}
        step_state.finished_at = datetime.now().isoformat(timespec="seconds")
        self._append_logs([f"Step {step_id} marked complete manually: {message}"])
        return {"ok": True, "message": message}

    @property
    def state(self) -> PipelineState:
        return self._state

    def _reset_step_states(self) -> None:
        self._state.steps = [
            StepState(id=s.id, name=s.name, description=s.description)
            for s in STEP_DEFINITIONS
        ]

    def _append_logs(self, messages: list[str]) -> None:
        for message in messages:
            logger.info(message)
        self._state.logs.extend(messages)
        if len(self._state.logs) > self._max_logs:
            self._state.logs = self._state.logs[-self._max_logs :]

    def _summarize_run(self, step_ids: list[int]) -> tuple[str, str]:
        ran = [s for s in self._state.steps if s.id in step_ids]
        if not ran:
            return "unknown", "No steps ran"

        failed = next((s for s in ran if s.status == StepStatus.FAILED), None)
        if failed:
            return (
                "failed",
                f"Stopped at step {failed.id} ({failed.name}): {failed.message}",
            )

        waiting = next((s for s in ran if s.status == StepStatus.WAITING), None)
        if waiting:
            return (
                "waiting",
                f"Paused at step {waiting.id} ({waiting.name}): {waiting.message}",
            )

        abandoned = next((s for s in ran if s.status == StepStatus.ABANDONED), None)
        if abandoned:
            return (
                "abandoned",
                f"Abandoned during step {abandoned.id} ({abandoned.name})",
            )

        not_run = [
            s
            for s in ran
            if s.status == StepStatus.SKIPPED
            and s.message.startswith("Not run —")
        ]
        if not_run:
            completed = [s for s in ran if s not in not_run]
            last_id = max((s.id for s in completed), default=0)
            return (
                "stopped",
                f"Stopped after step {last_id} — {len(not_run)} step(s) not run",
            )

        success = sum(1 for s in ran if s.status == StepStatus.SUCCESS)
        skipped = sum(1 for s in ran if s.status == StepStatus.SKIPPED)
        total = len(ran)

        if success == total:
            return (
                "completed_all",
                f"All {total} steps completed successfully",
            )

        if success == 0 and skipped == total:
            return (
                "no_work",
                f"No work performed — all {total} steps skipped",
            )

        return (
            "partial",
            f"{success} completed, {skipped} skipped (of {total} steps)",
        )

    def get_status(self) -> dict[str, Any]:
        steps_payload = [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "status": s.status.value,
                "message": s.message,
                "stats": s.stats,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
                "estimated_duration_secs": STEP_MAP[s.id].estimated_duration_secs,
            }
            for s in self._state.steps
        ]
        summary = {
            "success": sum(1 for s in self._state.steps if s.status == StepStatus.SUCCESS),
            "skipped": sum(1 for s in self._state.steps if s.status == StepStatus.SKIPPED),
            "failed": sum(1 for s in self._state.steps if s.status == StepStatus.FAILED),
            "waiting": sum(1 for s in self._state.steps if s.status == StepStatus.WAITING),
            "abandoned": sum(
                1 for s in self._state.steps if s.status == StepStatus.ABANDONED
            ),
            "idle": sum(1 for s in self._state.steps if s.status == StepStatus.IDLE),
            "running": sum(1 for s in self._state.steps if s.status == StepStatus.RUNNING),
        }
        return {
            "is_running": self._state.is_running,
            "current_step_id": self._state.current_step_id,
            "started_at": self._state.started_at,
            "finished_at": self._state.finished_at,
            "last_run_at": self._state.last_run_at,
            "trigger": self._state.trigger,
            "running_step_ids": list(self._state.running_step_ids),
            "outcome": self._state.last_run_outcome,
            "outcome_message": self._state.last_run_outcome_message,
            "outcome_summary": summary,
            "abandon_requested": self._abandon_requested,
            "steps": steps_payload,
        }

    def get_logs(self, limit: int = 100) -> list[str]:
        return self._state.logs[-limit:]

    def run_all(self, trigger: str = "manual", options: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "Pipeline is already running"}

        try:
            return self._execute_steps(
                step_ids=[s.id for s in STEP_DEFINITIONS],
                trigger=trigger,
                options=options or {},
            )
        finally:
            self._lock.release()

    def run_step(
        self,
        step_id: int,
        trigger: str = "manual",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if step_id not in STEP_MAP:
            return {"ok": False, "error": f"Unknown step id: {step_id}"}

        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "Pipeline is already running"}

        try:
            return self._execute_steps(
                step_ids=[step_id],
                trigger=trigger,
                options=options or {},
            )
        finally:
            self._lock.release()

    def _execute_steps(
        self,
        step_ids: list[int],
        trigger: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        sheets: SheetsClient | None = None

        try:
            if settings.sheets_configured:
                sheets = get_sheets_client()
                sheets.ping()
            else:
                logger.warning("Sheets not configured — running steps without Sheets connection")
        except Exception as exc:
            logger.warning("Sheets connection failed: %s", exc)

        now = datetime.now().isoformat(timespec="seconds")
        self._abandon_requested = False
        self._state.is_running = True
        self._state.started_at = now
        self._state.finished_at = None
        self._state.trigger = trigger
        self._state.running_step_ids = list(step_ids)
        self._reset_step_states()
        self._append_logs([f"Pipeline started ({trigger}) at {now}"])

        progress_step_state: StepState | None = None

        def _report_step_progress(
            stats_update: dict[str, Any], message: str | None = None
        ) -> None:
            if progress_step_state is None:
                return
            progress_step_state.stats = dict(stats_update)
            if message:
                progress_step_state.message = message

        ctx = PipelineContext(
            sheets_client=sheets,
            settings=settings,
            step_options=dict(options or {}),
            progress_reporter=_report_step_progress,
            abandon_checker=lambda: self._abandon_requested,
        )
        results: list[dict[str, Any]] = []
        completed_ids: set[int] = set()
        not_run_message = "Not run — previous step did not finish"

        for step_id in step_ids:
            if self._abandon_requested:
                step_state = next(s for s in self._state.steps if s.id == step_id)
                if step_state.status == StepStatus.IDLE:
                    step_state.status = StepStatus.SKIPPED
                    step_state.message = "Not run — pipeline abandoned"
                    step_state.finished_at = datetime.now().isoformat(timespec="seconds")
                    completed_ids.add(step_id)
                    results.append(
                        {
                            "step_id": step_id,
                            "status": StepStatus.SKIPPED.value,
                            "message": step_state.message,
                        }
                    )
                continue

            step_def = STEP_MAP[step_id]
            step_state = next(s for s in self._state.steps if s.id == step_id)
            progress_step_state = step_state

            step_state.status = StepStatus.RUNNING
            step_state.message = "Running..."
            step_state.stats = {}
            step_state.started_at = datetime.now().isoformat(timespec="seconds")
            step_state.finished_at = None
            self._state.current_step_id = step_id
            self._append_logs([f"Starting step {step_id}: {step_def.name}"])

            try:
                result = step_def.run_fn(ctx)
                step_state.status = result.status
                step_state.message = result.message
                step_state.stats = result.stats
                step_state.finished_at = datetime.now().isoformat(timespec="seconds")
                self._append_logs(
                    [f"Step {step_id} finished: {result.status.value} — {result.message}"]
                )
                results.append(
                    {
                        "step_id": step_id,
                        "status": result.status.value,
                        "message": result.message,
                    }
                )
                completed_ids.add(step_id)
                if result.status in STOP_PIPELINE_STATUSES:
                    self._append_logs(
                        [
                            f"Pipeline stopped — step {step_id} returned {result.status.value}"
                        ]
                    )
                    break
            except Exception as exc:
                coerced = sheets.coerce_quota_error(exc)
                if coerced is not None:
                    logger.warning("Step %s paused on Sheets quota: %s", step_id, coerced)
                    step_state.status = StepStatus.WAITING
                    step_state.message = str(coerced)
                    step_state.finished_at = datetime.now().isoformat(timespec="seconds")
                    self._append_logs([f"Step {step_id} waiting: {coerced}"])
                    results.append(
                        {
                            "step_id": step_id,
                            "status": StepStatus.WAITING.value,
                            "message": step_state.message,
                        }
                    )
                    completed_ids.add(step_id)
                    self._append_logs(
                        [f"Pipeline stopped — step {step_id} hit Sheets quota"]
                    )
                    break
                logger.exception("Step %s failed", step_id)
                step_state.status = StepStatus.FAILED
                step_state.message = str(exc)
                step_state.finished_at = datetime.now().isoformat(timespec="seconds")
                self._append_logs([f"Step {step_id} failed: {exc}"])
                results.append({"step_id": step_id, "status": "failed", "message": str(exc)})
                completed_ids.add(step_id)
                self._append_logs([f"Pipeline stopped — step {step_id} raised an error"])
                break

        if self._abandon_requested:
            not_run_message = "Not run — pipeline abandoned"

        for step_id in step_ids:
            if step_id in completed_ids:
                continue
            step_state = next(s for s in self._state.steps if s.id == step_id)
            step_state.status = StepStatus.SKIPPED
            step_state.message = not_run_message
            step_state.finished_at = datetime.now().isoformat(timespec="seconds")
            results.append(
                {
                    "step_id": step_id,
                    "status": StepStatus.SKIPPED.value,
                    "message": step_state.message,
                }
            )

        self._append_logs(ctx.log)
        finished = datetime.now().isoformat(timespec="seconds")
        outcome, outcome_message = self._summarize_run(step_ids)
        self._state.last_run_outcome = outcome
        self._state.last_run_outcome_message = outcome_message
        self._state.is_running = False
        self._state.current_step_id = None
        self._state.running_step_ids = []
        self._state.finished_at = finished
        self._state.last_run_at = finished
        self._abandon_requested = False
        self._append_logs([f"Pipeline finished at {finished} — {outcome_message}"])

        return {
            "ok": True,
            "results": results,
            "finished_at": finished,
            "outcome": outcome,
            "outcome_message": outcome_message,
        }


_runner: PipelineRunner | None = None


def get_pipeline_runner() -> PipelineRunner:
    global _runner
    if _runner is None:
        _runner = PipelineRunner()
    return _runner
