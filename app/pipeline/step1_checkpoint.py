"""Persist Step 1 group scrape progress for cookie-pause resume."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def _checkpoint_path() -> Path:
    return get_settings().step1_checkpoint_path


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_checkpoint() -> dict[str, Any] | None:
    path = _checkpoint_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if not data.get("group_urls"):
            return None
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read Step 1 checkpoint %s: %s", path, exc)
        return None


def save_checkpoint(data: dict[str, Any]) -> None:
    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["updated_at"] = _now_iso()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def clear_checkpoint() -> bool:
    path = _checkpoint_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def has_checkpoint() -> bool:
    return load_checkpoint() is not None
