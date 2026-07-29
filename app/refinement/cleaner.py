"""Clean raw profile scrape text before AI extraction."""

from __future__ import annotations

import re

_FACEBOOK_WORD = re.compile(r"facebook", re.IGNORECASE)


def clean_profile_text(text: str) -> str:
    """Remove the word Facebook (case-insensitive) before AI extraction."""
    if not text:
        return ""
    cleaned = _FACEBOOK_WORD.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()
