"""Rule-based person vs business signals (no API)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_PERSONAL_NAME = re.compile(
    r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}$"
)
_BUSINESS_WORDS = re.compile(
    r"\b("
    r"plumbing|cleaning|builders?|construction|services?|ltd|limited|"
    r"company|co\.|corp|inc|group|solutions?|contractor|roofing|"
    r"electrical|landscaping|garage|mot|dental|clinic|salon|studio|"
    r"cafe|restaurant|shop|store|hire|removals?|decorat"
    r")\b",
    re.I,
)
_PEOPLE_PATH = re.compile(r"/people/", re.I)


@dataclass(frozen=True)
class HeuristicResult:
    entity_type: str  # "business", "person", or ""
    confidence: float
    reason: str


def _is_profile_id_url(link: str) -> bool:
    parsed = urlparse(link if "://" in link else f"https://{link}")
    path = (parsed.path or "").lower()
    if "profile.php" in path:
        query = parse_qs(parsed.query)
        return bool(query.get("id"))
    return False


def heuristic_screen(business_name: str, facebook_link: str) -> HeuristicResult:
    """
    Phase 1: name + link only. Returns empty entity_type if no strong signal.
    """
    name = (business_name or "").strip()
    link = (facebook_link or "").strip().lower()

    if _PEOPLE_PATH.search(link):
        return HeuristicResult(
            entity_type="person",
            confidence=0.95,
            reason="Facebook /people/ URL — personal profile",
        )

    if _is_profile_id_url(link) and name:
        if _PERSONAL_NAME.match(name) and not _BUSINESS_WORDS.search(name):
            return HeuristicResult(
                entity_type="person",
                confidence=0.9,
                reason="profile.php URL with personal name pattern",
            )

    if name and _BUSINESS_WORDS.search(name):
        return HeuristicResult(
            entity_type="business",
            confidence=0.85,
            reason="business keywords in name",
        )

    if name and _PERSONAL_NAME.match(name) and not _BUSINESS_WORDS.search(name):
        if _is_profile_id_url(link) or "profile.php" in link:
            return HeuristicResult(
                entity_type="person",
                confidence=0.88,
                reason="personal name on profile URL",
            )

    return HeuristicResult(entity_type="", confidence=0.0, reason="")
