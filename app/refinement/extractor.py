"""OpenRouter GPT extraction for profile scrape text."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.qualify.website import normalize_website_url

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You extract structured fields from Facebook page/profile scrape text.

Return ONLY valid JSON with these keys:
- business_name: page or business name (string, empty if unknown)
- business_type: industry or category, e.g. "Maintenance", "Hair salon" (string, empty if unknown)
- location: city, region, or area — use middle dot (·) between multiple places, e.g. "Lincolnshire · Cambridgeshire" (string, empty if unknown)
- phone1: first phone number found (string, empty if none)
- phone2: second different phone number (string, empty if none)
- email: email address (string, empty if none) — when a website domain appears in Links, look for emails on that same domain (e.g. info@example.com)
- description: short summary of what the business does / services offered — marketing tone OK (string, empty if unknown)
- business_owner: person name only — never a business/brand name (string, empty if unknown)
- website_link: single primary website URL or domain only — never multiple sites separated by · (string, empty if none)
- last_post_ago: how long ago the most recent post was, e.g. "2 days", "1 week" (string, empty if unknown)
- followers: follower count as shown on the page, e.g. "370 followers" or "370" (string, empty if unknown)

Rules:
- phone1 and phone2 must be different numbers when both present
- business_owner must be a human person name, not a company or page title
- description should capture services/value proposition from the page bio or posts when visible
- Use empty strings for missing values, not null
"""


@dataclass
class ExtractedProfile:
    business_name: str = ""
    business_type: str = ""
    location: str = ""
    phone1: str = ""
    phone2: str = ""
    email: str = ""
    description: str = ""
    business_owner: str = ""
    website_link: str = ""
    last_post_ago: str = ""
    followers: str = ""


class ProfileExtractorError(Exception):
    """Raised when OpenRouter extraction fails."""


def _parse_response(content: str) -> ExtractedProfile:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProfileExtractorError(f"Invalid JSON from model: {exc}") from exc

    if not isinstance(data, dict):
        raise ProfileExtractorError("Model response was not a JSON object")

    return ExtractedProfile(
        business_name=str(data.get("business_name") or "").strip(),
        business_type=str(data.get("business_type") or "").strip(),
        location=str(data.get("location") or "").strip(),
        phone1=str(data.get("phone1") or "").strip(),
        phone2=str(data.get("phone2") or "").strip(),
        email=str(data.get("email") or "").strip(),
        description=str(data.get("description") or "").strip(),
        business_owner=str(data.get("business_owner") or "").strip(),
        website_link=str(data.get("website_link") or "").strip(),
        last_post_ago=str(data.get("last_post_ago") or "").strip(),
        followers=str(data.get("followers") or "").strip(),
    )


def extract_profile_fields(
    cleaned_text: str,
    *,
    api_key: str,
    model: str,
    base_url: str,
    business_name: str = "",
    timeout: float = 60.0,
) -> ExtractedProfile:
    if not cleaned_text.strip():
        return ExtractedProfile()

    url = base_url.rstrip("/") + "/chat/completions"
    user_content = cleaned_text
    if business_name:
        user_content = f"Business/page name on sheet: {business_name}\n\nProfile text:\n{cleaned_text}"

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
        raise ProfileExtractorError(f"OpenRouter request failed: {exc}") from exc

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProfileExtractorError(f"Unexpected OpenRouter response: {body}") from exc

    extracted = _parse_response(content)
    _sanitize_owner(extracted, business_name)
    if extracted.website_link:
        extracted.website_link = normalize_website_url(extracted.website_link)
    if extracted.phone1 and extracted.phone2 and extracted.phone1 == extracted.phone2:
        extracted.phone2 = ""
    return extracted


def _sanitize_owner(extracted: ExtractedProfile, business_name: str) -> None:
    owner = extracted.business_owner.strip()
    business = business_name.strip()
    if not owner or not business:
        return

    owner_lower = owner.lower()
    business_lower = business.lower()
    if owner_lower == business_lower:
        extracted.business_owner = ""
        return
    if business_lower in owner_lower:
        extracted.business_owner = ""
        return
    business_words = [w for w in _split_words(business_lower) if len(w) > 2]
    if business_words and all(word in owner_lower for word in business_words):
        extracted.business_owner = ""


def _split_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text)
