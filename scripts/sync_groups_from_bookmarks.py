#!/usr/bin/env python3
"""Sync config/groups.json from a Chrome bookmarks HTML export."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.config import get_settings
from app.scrapers.facebook_groups import sync_groups_from_bookmarks


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Facebook groups from bookmarks HTML")
    parser.add_argument(
        "bookmarks",
        nargs="?",
        default="bookmarks_25_07_2026.html",
        help="Path to bookmarks HTML export",
    )
    args = parser.parse_args()

    settings = get_settings()
    groups = sync_groups_from_bookmarks(Path(args.bookmarks), settings.fb_groups_path)
    print(f"Synced {len(groups)} groups to {settings.fb_groups_path}")


if __name__ == "__main__":
    main()
