"""Detect personal Facebook profiles from scrape text."""

from __future__ import annotations

import re

_FRIENDS = re.compile(r"\bfriends\b", re.IGNORECASE)
_FRIEND_COUNT = re.compile(r"\b\d+\s+friends?\b", re.IGNORECASE)
_FOLLOWERS = re.compile(r"\bfollowers\b", re.IGNORECASE)


def is_personal_profile_text(text: str) -> bool:
    """
    Heuristic personal-page signals in raw scrape text.

    Examples: "1 friend", "Personal details", friends without followers.
    """
    if not (text or "").strip():
        return False

    haystack = text
    haystack_lower = haystack.lower()

    if "personal details" in haystack_lower:
        return True

    if _FRIEND_COUNT.search(haystack) and not _FOLLOWERS.search(haystack):
        return True

    if _FRIENDS.search(haystack) and not _FOLLOWERS.search(haystack):
        return True

    if "following" in haystack_lower and "followers" not in haystack_lower:
        return True

    return False
