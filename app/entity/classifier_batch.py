"""Batched OpenRouter entity classification (business vs person)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

logger = logging.getLogger(__name__)

SCREEN_SYSTEM_PROMPT = """You classify Facebook leads as business or personal using ONLY the display name and URL.

Return ONLY valid JSON with a "results" array. Each item:
- id: same integer id from the input
- entity_type: "business" or "person"
- confidence: number 0.0 to 1.0 (how sure you are)
- reason: one short sentence

Rules:
- "business" = company, shop, service, brand, sole trader with a business name
- "person" = individual personal profile, not a commercial target
- If commercial-looking but unclear, prefer "business" with lower confidence
- Sole traders with trade names count as "business"
"""

FULL_SYSTEM_PROMPT = """You classify Facebook leads as business or personal using name, URL, and scraped page text.

Return ONLY valid JSON with a "results" array. Each item:
- id: same integer id from the input
- entity_type: "business" or "person"
- confidence: number 0.0 to 1.0
- reason: one short sentence

Rules:
- "business" = company, shop, service provider, brand, or commercial page
- "person" = individual profile (friends list, personal details, not a business)
- Sole traders with business names count as "business"
- If scrape shows friends/personal details without business signals → "person"
"""


@dataclass(frozen=True)
class EntityLeadInput:
    row_index: int
    business_name: str = ""
    facebook_link: str = ""
    scrape_text: str = ""
    refined_text: str = ""


@dataclass(frozen=True)
class EntityClassifyResult:
    row_index: int
    entity_type: str
    confidence: float
    reason: str = ""


class ClassifyBatchError(Exception):
    """Raised when batch classification API fails."""


def _parse_results(data: Any, expected_ids: set[int]) -> list[EntityClassifyResult]:
    if isinstance(data, dict) and "results" in data:
        raw_list = data["results"]
    elif isinstance(data, list):
        raw_list = data
    else:
        raw_list = []

    parsed: list[EntityClassifyResult] = []
    seen: set[int] = set()
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        try:
            row_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if row_id not in expected_ids or row_id in seen:
            continue
        entity_type = str(item.get("entity_type") or "").strip().lower()
        if entity_type not in {"business", "person"}:
            entity_type = "business"
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        reason = str(item.get("reason") or "").strip()
        parsed.append(
            EntityClassifyResult(
                row_index=row_id,
                entity_type=entity_type,
                confidence=confidence,
                reason=reason,
            )
        )
        seen.add(row_id)
    return parsed


def _format_screen_lead(lead: EntityLeadInput) -> str:
    parts = [f"ID {lead.row_index}"]
    if lead.business_name:
        parts.append(f"Name: {lead.business_name}")
    if lead.facebook_link:
        parts.append(f"URL: {lead.facebook_link}")
    return "\n".join(parts)


def _format_full_lead(lead: EntityLeadInput) -> str:
    parts = [f"ID {lead.row_index}"]
    if lead.business_name:
        parts.append(f"Name: {lead.business_name}")
    if lead.facebook_link:
        parts.append(f"URL: {lead.facebook_link}")
    if lead.refined_text.strip():
        parts.append(f"Refined:\n{lead.refined_text.strip()[:2000]}")
    if lead.scrape_text.strip():
        parts.append(f"Scrape:\n{lead.scrape_text.strip()[:3000]}")
    return "\n".join(parts)


def classify_entities_batch(
    leads: Sequence[EntityLeadInput],
    *,
    mode: str,
    api_key: str,
    model: str,
    base_url: str,
    timeout: float = 90.0,
) -> list[EntityClassifyResult]:
    """Classify up to N leads in one OpenRouter request."""
    if not leads:
        return []

    expected_ids = {lead.row_index for lead in leads}
    system = SCREEN_SYSTEM_PROMPT if mode == "screen" else FULL_SYSTEM_PROMPT
    formatter = _format_screen_lead if mode == "screen" else _format_full_lead

    blocks = [formatter(lead) for lead in leads]
    user_content = (
        f"Classify each lead ({len(leads)} total). Return JSON with a results array.\n\n"
        + "\n\n---\n\n".join(blocks)
    )

    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
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
        raise ClassifyBatchError(f"OpenRouter request failed: {exc}") from exc

    try:
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ClassifyBatchError(f"Unexpected batch classifier response: {body}") from exc

    return _parse_results(data, expected_ids)
