"""OpenRouter GPT person vs business classification for Step 5."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You classify Facebook leads as a business we should contact or a personal profile to skip.

Return ONLY valid JSON:
- entity_type: "business" or "person"
- reason: one short sentence

Rules:
- entity_type "business" = company, shop, service provider, brand, or commercial page we could sell to
- entity_type "person" = individual personal profile, hobby page, or clearly not a business target
- If unclear but looks commercial (services, prices, opening hours, business email), prefer "business"
- Sole traders with a business name count as "business"
"""


@dataclass(frozen=True)
class ClassifyResult:
    entity_type: str
    reason: str = ""


class ClassifyError(Exception):
    """Raised when classification API fails."""


def classify_entity(
    *,
    business_name: str,
    refined_text: str,
    scrape_text: str = "",
    api_key: str,
    model: str,
    base_url: str,
    timeout: float = 60.0,
) -> ClassifyResult:
    parts: list[str] = []
    if business_name.strip():
        parts.append(f"Business name: {business_name.strip()}")
    if refined_text.strip():
        parts.append(f"Refined profile:\n{refined_text.strip()}")
    if scrape_text.strip():
        snippet = scrape_text.strip()[:4000]
        parts.append(f"Raw scrape snippet:\n{snippet}")

    if not parts:
        return ClassifyResult(entity_type="business", reason="no context — default business")

    user_content = "\n\n".join(parts)
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise ClassifyError(f"OpenRouter request failed: {exc}") from exc

    try:
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ClassifyError(f"Unexpected classifier response: {body}") from exc

    entity_type = str(data.get("entity_type") or "").strip().lower()
    reason = str(data.get("reason") or "").strip()
    if entity_type not in {"business", "person"}:
        entity_type = "business"
    return ClassifyResult(entity_type=entity_type, reason=reason)
