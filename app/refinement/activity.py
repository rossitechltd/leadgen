"""Lead Activity score (1–10) from post recency and follower count."""

from __future__ import annotations

import re
from typing import Any

_FRIENDS_ONLY = re.compile(r"\bfriends\b", re.IGNORECASE)
_FOLLOWERS = re.compile(r"\bfollowers\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")


def parse_follower_count(followers: Any) -> int:
    if followers is None:
        return 0
    text = str(followers).strip()
    if not text:
        return 0
    match = _DIGITS.search(text.replace(",", ""))
    return int(match.group()) if match else 0


def parse_last_post_days(last_post_ago: Any) -> int | None:
    if last_post_ago is None:
        return None
    text = str(last_post_ago).strip().lower()
    if not text:
        return None

    if any(word in text for word in ("just now", "today", "now", "minute", "hour")):
        return 0
    if "yesterday" in text:
        return 1

    match = re.search(
        r"(\d+)\s*(second|minute|hour|day|week|month|year)s?",
        text,
    )
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("second") or unit.startswith("minute") or unit.startswith("hour"):
        return 0
    if unit.startswith("day"):
        return amount
    if unit.startswith("week"):
        return amount * 7
    if unit.startswith("month"):
        return amount * 30
    if unit.startswith("year"):
        return amount * 365
    return None


def is_personal_page_signal(profile_text: str, followers: Any) -> bool:
    """
    Personal-page signals (e.g. friends, not followers) force score 0.
    """
    text = profile_text or ""
    follower_count = parse_follower_count(followers)

    if _FRIENDS_ONLY.search(text) and not _FOLLOWERS.search(text):
        return True

    text_lower = text.lower()
    if "friends" in text_lower and "followers" not in text_lower:
        return True

    if follower_count == 0 and "following" in text_lower and "followers" not in text_lower:
        return True

    return False


def compute_lead_activity_score(
    last_post_ago: Any,
    followers: Any,
    profile_text: str,
) -> int:
    """
    Score 1–10 from post recency + followers.

    Recent post (~7 days) + 40+ followers ≈ 10.
    No recent activity + few/no followers ≈ 1.
    Personal-page signals → 0.
    """
    if is_personal_page_signal(profile_text, followers):
        return 0

    days = parse_last_post_days(last_post_ago)
    follower_count = parse_follower_count(followers)

    if days is None and follower_count == 0:
        return 1

    if days is not None and days <= 7 and follower_count >= 40:
        return 10

    recency = 1
    if days is not None:
        if days <= 7:
            recency = 10
        elif days <= 14:
            recency = 7
        elif days <= 30:
            recency = 4
        else:
            recency = 1

    follow = 1
    if follower_count >= 40:
        follow = 10
    elif follower_count >= 20:
        follow = 6
    elif follower_count >= 5:
        follow = 3
    elif follower_count > 0:
        follow = 2

    score = round((recency + follow) / 2)
    return max(1, min(10, score))
