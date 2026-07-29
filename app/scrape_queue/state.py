"""Scrape queue state persisted outside the 2-column scrapesheet."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

PHASE_AWAITING_CLEAR = "awaiting_clear"
PHASE_AWAITING_PASTE = "awaiting_paste"
PHASE_READY = "ready"


@dataclass
class ActiveScrapeState:
    """Tracks scrape-queue ownership: source_row/link = lead currently in column A."""

    source_row: int
    attempt: int
    link: str
    business_name: str = ""
    phase: str = PHASE_AWAITING_CLEAR
    link_set_at: str | None = None
    baseline_b_hash: str = ""
    last_b_hash: str = ""
    consumed_paste_hash: str = ""
    poll_count_since_link: int = 0

    # Legacy — loaded for migration only, not saved on new writes
    enqueued_at: str | None = None


class ScrapeStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ActiveScrapeState | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return _load_active_state(raw)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Invalid scrape state file %s: %s", self.path, exc)
            return None

    def save(self, state: ActiveScrapeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(state)
        # Drop legacy field from persisted JSON
        data.pop("enqueued_at", None)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


def _load_active_state(raw: dict) -> ActiveScrapeState:
    """Load state from JSON, migrating legacy epoch/mutation fields."""
    source_row = int(raw["source_row"])
    attempt = int(raw.get("attempt", 1))
    link = str(raw.get("link", ""))
    business_name = str(raw.get("business_name", ""))

    link_set_at = raw.get("link_set_at") or raw.get("enqueued_at")

    baseline_b_hash = str(
        raw.get("baseline_b_hash") or raw.get("data_baseline_hash", "")
    )
    last_b_hash = str(
        raw.get("last_b_hash") or raw.get("last_polled_b_hash", "")
    )
    consumed_paste_hash = str(
        raw.get("consumed_paste_hash") or raw.get("consumed_data_hash", "")
    )
    poll_count = int(raw.get("poll_count_since_link", 0))

    phase = str(raw.get("phase", "")).strip()
    if not phase:
        b_cleared = bool(raw.get("b_cleared_since_link", False))
        mutated = bool(raw.get("paste_mutated_since_link", False))
        paste_advanced = int(raw.get("paste_advanced_epoch", 0))
        link_epoch = int(raw.get("link_epoch", 1))
        if paste_advanced >= link_epoch and mutated:
            phase = PHASE_READY
        elif b_cleared:
            phase = PHASE_AWAITING_PASTE
        else:
            phase = PHASE_AWAITING_CLEAR

    return ActiveScrapeState(
        source_row=source_row,
        attempt=attempt,
        link=link,
        business_name=business_name,
        phase=phase,
        link_set_at=link_set_at,
        baseline_b_hash=baseline_b_hash,
        last_b_hash=last_b_hash,
        consumed_paste_hash=consumed_paste_hash,
        poll_count_since_link=poll_count,
        enqueued_at=raw.get("enqueued_at"),
    )


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

    def clear_all(self) -> None:
        self._counts = {}
        if self.path.exists():
            self.path.unlink()


def get_state_store(settings: Settings | None = None) -> ScrapeStateStore:
    settings = settings or get_settings()
    return ScrapeStateStore(settings.scrape_state_path)


def get_failure_store(settings: Settings | None = None) -> ScrapeFailureStore:
    settings = settings or get_settings()
    path = settings.scrape_state_path.parent / "failures.json"
    return ScrapeFailureStore(path)


class ScrapeHandledStore:
    """Rows whose scrapesheet link cycle completed (background poller, no sheet write)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: set[int] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._rows = set()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._rows = {int(x) for x in raw}
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Invalid handled rows file %s: %s", self.path, exc)
            self._rows = set()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(sorted(self._rows), indent=2), encoding="utf-8"
        )

    def contains(self, source_row: int) -> bool:
        return source_row in self._rows

    def add(self, source_row: int) -> None:
        if source_row not in self._rows:
            self._rows.add(source_row)
            self._save()

    def clear(self) -> None:
        self._rows = set()
        if self.path.exists():
            self.path.unlink()


def get_handled_store(settings: Settings | None = None) -> ScrapeHandledStore:
    settings = settings or get_settings()
    path = settings.scrape_state_path.parent / "handled_rows.json"
    return ScrapeHandledStore(path)
