"""Map Apify Facebook group member records to lead sheet rows."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from app.sheets.columns import LEAD_ACTIVITY_PENDING

# Primary link column on the allimported tab (legacy exports used "Facebook Link").
ALL_IMPORTED_LINK_COLUMNS = ("link", "Facebook Link")

_GROUP_USER_PATH = re.compile(r"/groups/\d+/user/(\d+)")


def is_facebook_url(url: str) -> bool:
    return "facebook.com" in (url or "").lower()


def extract_row_link(row: dict[str, Any]) -> str:
    for column in ALL_IMPORTED_LINK_COLUMNS:
        raw = row.get(column)
        if raw:
            return str(raw).strip()
    return ""


def normalize_facebook_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    path = parsed.path.rstrip("/") or "/"
    host = (parsed.netloc or "www.facebook.com").lower()
    if host.startswith("www."):
        host = host[4:]

    group_user = _GROUP_USER_PATH.search(path)
    if group_user:
        return f"https://www.{host}/profile.php?id={group_user.group(1)}"

    if path == "/profile.php" and parsed.query:
        from urllib.parse import parse_qs

        profile_id = parse_qs(parsed.query).get("id", [None])[0]
        if profile_id:
            return f"https://www.{host}/profile.php?id={profile_id}"

    if parsed.query:
        return f"https://www.{host}{path}?{parsed.query}"

    return f"https://www.{host}{path}"


def normalize_facebook_link(url: str) -> str:
    """Normalize only if this is a Facebook URL; otherwise return empty string."""
    if not is_facebook_url(url):
        return ""
    return normalize_facebook_url(url)


def imported_facebook_links(rows: list[dict[str, Any]]) -> set[str]:
    links: set[str] = set()
    for row in rows:
        normalized = normalize_facebook_link(extract_row_link(row))
        if normalized:
            links.add(normalized)
    return links


def member_joined_at(member: dict[str, Any]) -> int:
    """Unix timestamp when the member joined the group (0 if unknown)."""
    value = member.get("joinedAt")
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def sort_members_by_joined_at(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most recently joined first."""
    return sorted(members, key=member_joined_at, reverse=True)


def is_new_member(member: dict[str, Any]) -> bool:
    tags = member.get("memberTags") or []
    return "NEW_MEMBER" in tags


def resolve_page_link(member: dict[str, Any]) -> str:
    work = member.get("work") or {}
    work_url = work.get("url") or ""
    if work_url and "facebook.com" in work_url:
        return normalize_facebook_url(work_url)
    for key in ("profileUrl", "url"):
        value = member.get(key)
        if value:
            return normalize_facebook_url(str(value))
    for key in ("userId", "groupMemberId"):
        value = member.get(key)
        if value:
            return normalize_facebook_url(f"https://www.facebook.com/profile.php?id={value}")
    return ""


def resolve_business_name(member: dict[str, Any]) -> str:
    work = member.get("work") or {}
    if work.get("short_name"):
        return str(work["short_name"]).strip()
    if work.get("text"):
        return str(work["text"]).strip()
    return str(member.get("name") or "").strip()


def member_to_lead(member: dict[str, Any]) -> dict[str, str] | None:
    link = resolve_page_link(member)
    name = resolve_business_name(member)
    if not link:
        return None
    return {"facebook_link": link, "business_name": name}


def lead_row_for_sheet(lead: dict[str, str]) -> list[str]:
    """Dynamic Lead Sheet row — Facebook Link, Business Name, Lead Activity only."""
    return [
        lead["facebook_link"],
        lead["business_name"],
        "",  # Scrape
        "",  # Phone Number 1
        "",  # Phone Number 2
        "",  # Business Owner
        "",  # Website Link
        "",  # Website Status
        "",  # Website Status Reason
        "",  # HTTP Status Code
        "",  # Original Website URL
        "",  # Final URL
        "",  # Redirect Chain
        "",  # Confidence
        "",  # Checked At
        LEAD_ACTIVITY_PENDING,
        "",  # Message1
        "",  # Message2
        "",  # va
        "",  # refined
    ]
