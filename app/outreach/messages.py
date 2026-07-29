"""Outreach message templates for Step 8."""

from __future__ import annotations

import random
import re

OUTREACH_TEMPLATES: tuple[str, ...] = (
    (
        "Hi {firstname}! Found your Facebook page and noticed you're missing a website. "
        "I actually went ahead and built one for you - I'd love to share it, mind if I send it over?"
    ),
    (
        "Hey {firstname}! Came across your page on Facebook and thought I'd do something a bit different. "
        "I went ahead and built you a website to help get some more eyes on your business - "
        "would you mind if I sent it over?"
    ),
    (
        "Hey {firstname}! Saw your Facebook page and noticed you don't have a website.\n\n"
        "I actually went ahead and built you one - can I send it over?"
    ),
)

_FIRST_NAME_PLACEHOLDER = "{firstname}"
_INVALID_OWNER_VALUES = frozenset({"", "notfound", "unknown", "n/a", "none"})
_OWNER_PREFIX_RE = re.compile(
    r"^(?:mr|mrs|ms|miss|dr|prof)\.?\s+",
    re.IGNORECASE,
)


def resolve_first_name(row: dict, *, business_name: str = "") -> str:
    """First token from Business Owner, or 'there' when unavailable."""
    from app.sheets.columns import COL_BUSINESS_OWNER

    owner = str(row.get(COL_BUSINESS_OWNER) or "").strip()
    if not owner or owner.lower() in _INVALID_OWNER_VALUES:
        return "there"

    owner = _OWNER_PREFIX_RE.sub("", owner).strip()
    if not owner:
        return "there"

    first = owner.split()[0].strip(".,!?\"'")
    if not first or first.lower() in _INVALID_OWNER_VALUES:
        return "there"

    business = (business_name or "").strip()
    if business and first.lower() == business.lower():
        return "there"

    return first


def build_outreach_message(first_name: str, *, template: str | None = None) -> str:
    name = (first_name or "").strip() or "there"
    chosen = template if template is not None else random.choice(OUTREACH_TEMPLATES)
    return chosen.replace(_FIRST_NAME_PLACEHOLDER, name)
