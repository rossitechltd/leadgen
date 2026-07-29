"""Scrape queue state persisted outside the 2-column scrapesheet."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class ActiveScrapeState:
    """Tracks which Dynamic Lead row the scrapesheet data column belongs to."""

    source_row: int
    attempt: int
    link: str
    enqueued_at: str | None = None
    business_name: str = ""
    # Hash of column B when this lead's link was set — new paste = hash differs from baseline
    data_baseline_hash: str = ""
    # Hash of paste already written to Dynamic Lead (prevents double finalize)
    consumed_data_hash: str = ""
    data_hash: str = ""
    data_stable_at: str | None = None
    paste_first_seen_at: str | None = None
    paste_length: int = 0


class ScrapeStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ActiveScrapeState | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return ActiveScrapeState(
                source_row=int(raw["source_row"]),
                attempt=int(raw.get("attempt", 1)),
                link=str(raw.get("link", "")),
                enqueued_at=raw.get("enqueued_at"),
                business_name=str(raw.get("business_name", "")),
                data_baseline_hash=str(raw.get("data_baseline_hash", "")),
                consumed_data_hash=str(raw.get("consumed_data_hash", "")),
                data_hash=str(raw.get("data_hash", "")),
                data_stable_at=raw.get("data_stable_at"),
                paste_first_seen_at=raw.get("paste_first_seen_at"),
                paste_length=int(raw.get("paste_length", 0)),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Invalid scrape state file %s: %s", self.path, exc)
            return None

    def save(self, state: ActiveScrapeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class ScrapeFailureStore:
    """Per-row scrape failure counts (queue rounds, not bad-paste session retries)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._counts: dict[int, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._counts = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._counts = {int(k): int(v) for k, v in raw.items()}
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Invalid scrape failures file %s: %s", self.path, exc)
            self._counts = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._counts, indent=2), encoding="utf-8"
        )

    def count(self, source_row: int, *, activity: str = "") -> int:
        return self._counts.get(source_row, 0)

    def increment(self, source_row: int) -> int:
        total = self._counts.get(source_row, 0) + 1
        self._counts[source_row] = total
        self._save()
        return total

    def clear(self, source_row: int) -> None:
        if source_row in self._counts:
            del self._counts[source_row]
            self._save()


def get_state_store(settings: Settings | None = None) -> ScrapeStateStore:
    settings = settings or get_settings()
    return ScrapeStateStore(settings.scrape_state_path)


def get_failure_store(settings: Settings | None = None) -> ScrapeFailureStore:
    settings = settings or get_settings()
    path = settings.scrape_state_path.parent / "failures.json"
    return ScrapeFailureStore(path)
