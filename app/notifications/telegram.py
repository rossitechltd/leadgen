"""Telegram Bot API alerts for operator attention items."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.operator_attention import get_attention_queue

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LEN = 4096
BODY_SNIPPET_LEN = 120


def normalize_chat_id(raw: str) -> str:
    value = (raw or "").strip()
    if value.startswith("--"):
        return "-" + value[1:]
    return value


def is_telegram_configured() -> bool:
    settings = get_settings()
    return settings.telegram_configured


def get_telegram_poll_secs() -> int:
    return get_settings().telegram_poll_ms // 1000


def dashboard_url() -> str:
    return get_settings().dashboard_url


def send_telegram(text: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.telegram_configured:
        logger.info(
            "[telegram] Disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
        )
        return {"ok": False, "skipped": True}

    message = (text or "").strip()
    if not message:
        return {"ok": False, "error": "empty message"}

    if len(message) > MAX_MESSAGE_LEN:
        message = message[: MAX_MESSAGE_LEN - 1] + "…"

    url = TELEGRAM_API.format(token=settings.telegram_bot_token)
    payload = {
        "chat_id": normalize_chat_id(settings.telegram_chat_id),
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, json=payload)
            data = response.json()
    except httpx.HTTPError as exc:
        logger.warning("[telegram] send failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    if not data.get("ok"):
        error = data.get("description") or "Telegram API error"
        logger.warning("[telegram] send failed: %s", error)
        return {"ok": False, "error": error}

    return {"ok": True}


def _snippet(text: str, limit: int = BODY_SNIPPET_LEN) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def notify_attention(title: str, body: str, context: str | None = None) -> dict[str, Any]:
    """Immediate alert — one Telegram message per event."""
    header = f"⚠️ {context or title}"
    lines = [header]
    if context and title != context:
        lines.append(title)
    if body:
        lines.append(body)
    lines.append("")
    lines.append(dashboard_url())
    return send_telegram("\n".join(lines))


def format_attention_digest(items: list[dict[str, Any]]) -> str:
    count = len(items)
    lines = [f"📋 Attention needed ({count} pending)", ""]
    for item in items:
        title = item.get("title") or item.get("kind") or "Item"
        body = _snippet(item.get("body") or item.get("detail") or "")
        lines.append(f"• {title}")
        if body:
            lines.append(f'  "{body}"')
        lines.append("")
    lines.append(dashboard_url())
    return "\n".join(lines).strip()


def poll_attention_queue() -> dict[str, Any]:
    """Send digest if any attention items remain. Re-sends every poll interval."""
    items = get_attention_queue()
    if not items:
        return {"ok": True, "skipped": True, "reason": "empty"}

    text = format_attention_digest(items)
    result = send_telegram(text)
    if result.get("ok"):
        logger.info("[telegram] Attention digest sent (%s item(s))", len(items))
    return result


def telegram_review_poll() -> None:
    try:
        poll_attention_queue()
    except Exception as exc:
        logger.warning("[telegram] Poll error: %s", exc)


def notify_step4_scrape(title: str, body: str) -> dict[str, Any]:
    """One-shot Telegram for Step 4 scrape milestones (ready / started / complete)."""
    lines = [f"📋 Step 4 Page Scrape — {title}", "", body, "", dashboard_url()]
    return send_telegram("\n".join(lines))
