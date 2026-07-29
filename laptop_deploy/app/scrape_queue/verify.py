"""Scrape result verification for Step 3 queue."""

from __future__ import annotations

import re
from dataclasses import dataclass

FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"log\s*in",
        r"login",
        r"sign\s*in",
        r"content isn'?t available",
        r"page not found",
        r"this page isn'?t available",
        r"something went wrong",
        r"checkpoint",
        r"you must log in",
    )
)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str = ""


def verify_scrape_text(
    text: str, *, min_length: int = 50, business_name: str = ""
) -> VerifyResult:
    cleaned = (text or "").strip()
    if not cleaned:
        return VerifyResult(ok=False, reason="empty scrape")

    if len(cleaned) < min_length:
        return VerifyResult(ok=False, reason=f"too short ({len(cleaned)} chars)")

    for pattern in FAILURE_PATTERNS:
        if pattern.search(cleaned):
            return VerifyResult(ok=False, reason=f"matched error pattern: {pattern.pattern}")

    if business_name.strip():
        name_check = verify_scrape_matches_business(cleaned, business_name)
        if not name_check.ok:
            return name_check

    return VerifyResult(ok=True)


def verify_scrape_matches_business(text: str, business_name: str) -> VerifyResult:
    """Reject scrape text that does not contain the business/page name (case-insensitive)."""
    name = (business_name or "").strip()
    if len(name) < 2:
        return VerifyResult(ok=True)

    haystack = text.lower()
    name_lower = name.lower()
    normalized_name = re.sub(r"\s+", " ", name_lower).strip()
    normalized_haystack = re.sub(r"\s+", " ", haystack)

    if normalized_name and normalized_name in normalized_haystack:
        return VerifyResult(ok=True)

    tokens = [t for t in re.split(r"\W+", name_lower) if len(t) > 2]
    if not tokens:
        short_tokens = [t for t in re.split(r"\W+", name_lower) if len(t) >= 2]
        if short_tokens and all(t in haystack for t in short_tokens):
            return VerifyResult(ok=True)
        return VerifyResult(
            ok=False,
            reason=f"scrape text does not mention '{business_name}'",
        )

    missing = [t for t in tokens if t not in haystack]
    if missing:
        return VerifyResult(
            ok=False,
            reason=f"scrape text does not mention '{business_name}'",
        )

    return VerifyResult(ok=True)
