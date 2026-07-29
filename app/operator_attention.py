"""In-memory operator attention queue with JSON persistence."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_items: dict[str, dict[str, Any]] = {}


def _attention_path() -> Path:
    settings = get_settings()
    return settings.operator_attention_path


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _save() -> None:
    path = _attention_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": list(_items.values())}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def restore_attention_queue() -> int:
    """Load persisted items into memory. Returns count restored."""
    global _items
    path = _attention_path()
    with _lock:
        if not path.exists():
            _items = {}
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                _items = {}
                return 0
            _items = {
                str(item["id"]): item
                for item in items
                if isinstance(item, dict) and item.get("id")
            }
            return len(_items)
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Could not restore attention queue from %s: %s", path, exc)
            _items = {}
            return 0


def get_attention_queue() -> list[dict[str, Any]]:
    with _lock:
        return sorted(_items.values(), key=lambda item: item.get("created_at") or "")


def add_attention_item(
    *,
    kind: str,
    title: str,
    body: str,
    detail: str | None = None,
    item_id: str | None = None,
) -> dict[str, Any]:
    """Upsert by id when item_id is fixed (e.g. step1-cookie-refresh)."""
    with _lock:
        existing_id = item_id
        if existing_id and existing_id in _items:
            item = dict(_items[existing_id])
            item.update(
                {
                    "kind": kind,
                    "title": title,
                    "body": body,
                    "detail": detail or body,
                    "updated_at": _now_iso(),
                }
            )
            _items[existing_id] = item
        else:
            new_id = item_id or str(uuid.uuid4())
            item = {
                "id": new_id,
                "kind": kind,
                "title": title,
                "body": body,
                "detail": detail or body,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            _items[new_id] = item
        _save()
        return dict(item)


def remove_attention_item(item_id: str) -> bool:
    with _lock:
        if item_id not in _items:
            return False
        del _items[item_id]
        _save()
        return True


def remove_attention_by_kind(kind: str) -> int:
    with _lock:
        to_remove = [item_id for item_id, item in _items.items() if item.get("kind") == kind]
        for item_id in to_remove:
            del _items[item_id]
        if to_remove:
            _save()
        return len(to_remove)


def clear_attention_queue() -> int:
    with _lock:
        count = len(_items)
        _items.clear()
        if count:
            _save()
        return count
