"""Facebook group list from config and bookmarks export."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.scrapers.apify_members import ApifyConfigError

logger = logging.getLogger(__name__)

_GROUP_LINK_RE = re.compile(
    r"<A\s+HREF=\"(https?://(?:www\.)?facebook\.com/groups/[^\"]+)\"[^>]*>([^<]*)</A>",
    re.IGNORECASE,
)


def normalize_group_url(raw: str) -> str:
    """Canonical group URL without /members or trailing slash."""
    url = (raw or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if "facebook.com" not in (parsed.netloc or "").lower():
        return url.rstrip("/")
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/members"):
        path = path[: -len("/members")]
    return f"https://www.facebook.com{path}"


def _clean_group_name(raw: str) -> str:
    name = html_lib.unescape(raw or "").strip()
    name = re.sub(r"^\s*/{2,3}\s*", "", name)
    name = re.sub(r"\s*\|\s*Facebook\s*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^\(\d+\+\)\s*", "", name)
    return name.strip() or "Facebook group"


def parse_bookmarks_html(html_text: str) -> list[dict[str, str]]:
    """Extract Facebook group links from a Chrome bookmarks HTML export."""
    groups: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _GROUP_LINK_RE.finditer(html_text):
        url = normalize_group_url(match.group(1))
        if not url or "/groups/" not in url.lower():
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        groups.append({"name": _clean_group_name(match.group(2)), "url": url})
    return groups


def sync_groups_from_bookmarks(bookmarks_path: Path, groups_path: Path) -> list[dict[str, str]]:
    """Parse bookmarks HTML and write config/groups.json."""
    if not bookmarks_path.exists():
        raise ApifyConfigError(f"Bookmarks file not found: {bookmarks_path}")
    groups = parse_bookmarks_html(bookmarks_path.read_text(encoding="utf-8"))
    if not groups:
        raise ApifyConfigError(f"No Facebook group links found in {bookmarks_path}")

    payload = {
        "source": bookmarks_path.name,
        "groups": groups,
    }
    groups_path.parent.mkdir(parents=True, exist_ok=True)
    groups_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("Wrote %s groups to %s", len(groups), groups_path)
    return groups


def load_groups(groups_path: Path) -> list[dict[str, str]]:
    """Load group name + URL entries from groups.json."""
    if not groups_path.exists():
        raise ApifyConfigError(
            f"Groups file not found: {groups_path}\n"
            "Copy config/groups.example.json to config/groups.json or sync from bookmarks."
        )
    text = groups_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ApifyConfigError(f"Groups file is empty: {groups_path}")

    if groups_path.suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            raw_groups = data.get("groups") or data.get("groupUrls") or []
        elif isinstance(data, list):
            raw_groups = data
        else:
            raise ApifyConfigError("groups.json must be a list or {\"groups\": [...]}")
    else:
        raw_groups = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        ]

    groups: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_groups:
        if isinstance(item, str):
            url = normalize_group_url(item)
            name = url.rsplit("/", 1)[-1] if url else "Facebook group"
            entry = {"name": name, "url": url}
        elif isinstance(item, dict):
            url = normalize_group_url(str(item.get("url") or item.get("href") or ""))
            name = str(item.get("name") or item.get("title") or "").strip() or url
            entry = {"name": name, "url": url}
        else:
            continue
        if not url or "/groups/" not in url.lower():
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        groups.append(entry)

    if not groups:
        raise ApifyConfigError("No group URLs found in groups config.")
    logger.info("Loaded %s group(s) from %s", len(groups), groups_path)
    return groups


def load_group_urls(groups_path: Path) -> list[str]:
    return [g["url"] for g in load_groups(groups_path)]
