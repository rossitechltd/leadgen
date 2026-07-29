#!/usr/bin/env python3
"""Classify website status for leads in a CSV and write filtered outputs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import get_settings
from app.qualify.csv_processor import format_summary, process_leads_csv


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify website status for leads in a CSV file."
    )
    parser.add_argument("input_csv", type=Path, help="Input leads CSV path")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("data/website_qualify_output"),
        help="Directory for output CSV files",
    )
    args = parser.parse_args()
    settings = get_settings()

    if not args.input_csv.is_file():
        print(f"Input file not found: {args.input_csv}", file=sys.stderr)
        return 1

    result = process_leads_csv(
        args.input_csv,
        args.output_dir,
        timeout=settings.qualify_website_timeout_secs,
        max_redirects=settings.qualify_max_redirects,
        retries=settings.qualify_fetch_retries,
    )
    print(format_summary(result))
    if result.errors:
        print("\nErrors:", file=sys.stderr)
        for err in result.errors[:20]:
            print(f"  {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
