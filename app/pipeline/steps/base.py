from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

logger = logging.getLogger("app.pipeline")


class StepStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING = "waiting"
    ABANDONED = "abandoned"


@dataclass
class StepResult:
    status: StepStatus
    message: str
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineContext:
    """Shared context passed to each pipeline step."""

    sheets_client: Any
    settings: Any
    log: list[str] = field(default_factory=list)
    step_options: dict[str, Any] = field(default_factory=dict)
    progress_reporter: Callable[[dict[str, Any], str | None], None] | None = None
    abandon_checker: Callable[[], bool] | None = None

    def add_log(self, message: str) -> None:
        self.log.append(message)
        logger.info(message)

    def report_progress(
        self, stats: dict[str, Any], message: str | None = None
    ) -> None:
        """Push live stats to the dashboard while a long-running step executes."""
        if self.progress_reporter is not None:
            self.progress_reporter(stats, message)

    def is_abandoned(self) -> bool:
        return self.abandon_checker is not None and self.abandon_checker()


class PipelineStep(Protocol):
    id: int
    name: str
    description: str

    def run(self, ctx: PipelineContext) -> StepResult: ...
