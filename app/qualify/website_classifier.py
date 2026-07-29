"""AI classification of fetched website HTML for Step 5."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You classify a fetched web page for UK lead qualification.

We KEEP leads that have NO proper working website. We REMOVE leads with genuine active business sites.

Return ONLY valid JSON:
- status: one of "genuine_active", "expired_or_parked", "unavailable"
- reason: one short sentence

Definitions:
- genuine_active: Real business website with meaningful public content (services, about, contact, prices, portfolio, booking). Site is live and represents an established business with real pages beyond a single splash screen.
- expired_or_parked: Hosting expired ("Website Expired", "This account has expired", "If you are the site owner, click below to login"), domain parked/for sale, default hosting placeholder, suspended account, OR a minimal placeholder page ("new site is on its way", "we're rebuilding", "coming soon") even if it lists a phone number.
- unavailable: Truly blank, connection error text only, or impossible to determine.

CRITICAL rules:
- ANY hosting expiry / account expired / site owner login page = expired_or_parked (KEEP the lead)
- Login pages for hosting providers are NEVER genuine_active
- "Coming soon", "new site is on its way", "we're rebuilding" = expired_or_parked even with phone/WhatsApp — they need a proper website (KEEP the lead)
- A single short splash page with only "get in touch" + phone is NOT genuine_active — use expired_or_parked
- If the page clearly shows a full working business site (multiple sections: services, about, contact, portfolio) = genuine_active (REMOVE the lead)
- When text includes "Website Expired" or "account has expired" you MUST return expired_or_parked
- Do not return unavailable if the page has clear business content — use genuine_active
- A page with services, contact details, phone/email, and several paragraphs of business copy is genuine_active even if layout is simple
- Prefer genuine_active over unavailable when HTTP fetch returned real HTML with business wording
"""


@dataclass(frozen=True)
class WebsiteClassifyResult:
    status: str
    reason: str = ""


class WebsiteClassifyError(Exception):
    """Raised when website classification API fails."""


def classify_website_html(
    *,
    url: str,
    page_title: str,
    page_text: str,
    business_name: str = "",
    api_key: str,
    model: str,
    base_url: str,
    timeout: float = 45.0,
) -> WebsiteClassifyResult:
    snippet = page_text[:8000].strip()
    if not snippet:
        return WebsiteClassifyResult(
            status="unavailable",
            reason="empty page body",
        )

    user_parts = [f"URL: {url}"]
    if business_name.strip():
        user_parts.append(f"Business name: {business_name.strip()}")
    if page_title.strip():
        user_parts.append(f"Page title: {page_title.strip()}")
    user_parts.append(f"Page text snippet:\n{snippet}")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    api_url = base_url.rstrip("/") + "/chat/completions"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(api_url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise WebsiteClassifyError(f"OpenRouter request failed: {exc}") from exc

    try:
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise WebsiteClassifyError(f"Unexpected classifier response: {body}") from exc

    status = str(data.get("status") or "").strip().lower()
    reason = str(data.get("reason") or "").strip()
    if status not in {"genuine_active", "expired_or_parked", "unavailable"}:
        status = "unavailable"
    return WebsiteClassifyResult(status=status, reason=reason)


def html_to_text(html: str) -> str:
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return match.group(1).strip() if match else ""
