"""Scrape result verification for Step 3 queue."""

from __future__ import annotations

import re
import unicodedata
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

# Sparse personal profiles often paste without the account name visible.
SPARSE_PROFILE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bposts\b",
        r"\bphotos\b",
        r"\bprivacy\b",
        r"followers?",
        r"\bfollowing\b",
        r"no posts",
        r"about\b",
        r"personal details",
        r"contact info",
    )
)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str = ""


def fold_unicode_text(text: str) -> str:
    """Lowercase ASCII-ish text for matching (MMM pastes fancy Unicode FB labels)."""
    folded = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return stripped.lower()


def looks_like_sparse_facebook_scrape(text: str) -> bool:
    """
    True when paste looks like a real FB profile/page scrape without a visible name.
    MMM often pastes only follower stats + Photos/Posts/Privacy for sparse accounts.
    """
    folded = fold_unicode_text(text)
    if not folded.strip():
        return False
    hits = sum(1 for pattern in SPARSE_PROFILE_PATTERNS if pattern.search(folded))
    return hits >= 2


def verify_scrape_text(
    text: str,
    *,
    min_length: int = 50,
    business_name: str = "",
    lenient_name: bool = False,
    allow_sparse_profile: bool = False,
) -> VerifyResult:
    cleaned = (text or "").strip()
    if not cleaned:
        return VerifyResult(ok=False, reason="empty scrape")

    if len(cleaned) < min_length:
        return VerifyResult(ok=False, reason=f"too short ({len(cleaned)} chars)")

    folded = fold_unicode_text(cleaned)
    for pattern in FAILURE_PATTERNS:
        if pattern.search(folded):
            return VerifyResult(ok=False, reason=f"matched error pattern: {pattern.pattern}")

    if business_name.strip():
        skip_name = lenient_name and len(cleaned) >= max(min_length * 3, 200)
        if not skip_name:
            name_check = verify_scrape_matches_business(
                cleaned,
                business_name,
                allow_sparse_profile=allow_sparse_profile,
            )
            if not name_check.ok:
                return name_check

    return VerifyResult(ok=True)


def verify_scrape_matches_business(
    text: str,
    business_name: str,
    *,
    allow_sparse_profile: bool = False,
) -> VerifyResult:
    """Reject scrape text that does not contain the business/page name (case-insensitive)."""
    name = (business_name or "").strip()
    if len(name) < 2:
        return VerifyResult(ok=True)

    haystack = fold_unicode_text(text)
    name_lower = name.lower()
    normalized_name = re.sub(r"\s+", " ", name_lower).strip()
    normalized_haystack = re.sub(r"\s+", " ", haystack)

    if normalized_name and normalized_name in normalized_haystack:
        return VerifyResult(ok=True)

    tokens = [t for t in re.split(r"\W+", name_lower) if len(t) > 2]
    if not tokens:
        short_tokens = [t for t in re.split(r"\W+", name_lower) if len(t) >= 2]
        if short_tokens and all(
            re.search(rf"\b{re.escape(t)}\b", haystack) for t in short_tokens
        ):
            return VerifyResult(ok=True)
    else:
        missing = [
            t for t in tokens if not re.search(rf"\b{re.escape(t)}\b", haystack)
        ]
        if not missing:
            return VerifyResult(ok=True)

    # First-name / nickname: "Matt" on FB for "Matthew Zakrzewski"
    if len(tokens) >= 2 and re.search(rf"\b{re.escape(tokens[0])}\b", haystack):
        return VerifyResult(ok=True)

    if allow_sparse_profile and looks_like_sparse_facebook_scrape(text):
        return VerifyResult(ok=True, reason="sparse profile — name not in paste")

    return VerifyResult(
        ok=False,
        reason=f"scrape text does not mention '{business_name}'",
    )
