"""CSV batch processing with website status classification."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.qualify.website_status import (
    NOT_QUALIFIED_STATUSES,
    WebsiteStatusResult,
    classify_website_link,
    status_counts,
)
from app.sheets.columns import COL_BUSINESS_NAME, COL_WEBSITE_LINK

logger = logging.getLogger(__name__)

QUALIFIED_COLUMN = "Qualified"


@dataclass
class CsvProcessResult:
    total: int = 0
    qualified_count: int = 0
    removed_count: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    output_paths: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        rows = [{k: (v or "") for k, v in row.items()} for row in reader]
        return list(reader.fieldnames), rows


def _write_csv(path: Path, headers: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _website_link_from_row(row: dict[str, str]) -> str:
    for key in (COL_WEBSITE_LINK, "Website Link", "website link", "Website"):
        value = row.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _business_name_from_row(row: dict[str, str]) -> str:
    for key in (COL_BUSINESS_NAME, "Business Name", "business name"):
        value = row.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _extend_headers(headers: list[str]) -> list[str]:
    extra = [
        COL_WEBSITE_LINK if COL_WEBSITE_LINK not in headers else None,
        "Website Status",
        "Website Status Reason",
        "HTTP Status Code",
        "Original Website URL",
        "Final URL",
        "Redirect Chain",
        "Confidence",
        "Checked At",
        QUALIFIED_COLUMN,
    ]
    out = list(headers)
    for col in extra:
        if col and col not in out:
            out.append(col)
    return out


def process_leads_csv(
    input_path: Path,
    output_dir: Path,
    *,
    timeout: float = 15.0,
    max_redirects: int = 10,
    retries: int = 3,
) -> CsvProcessResult:
    """
    Classify each lead's Website Link and write four output CSVs:

    - all_classified.csv — every row with status fields
    - qualified_leads.csv — qualified website status
    - removed_leads.csv — ACTIVE / BUSINESS_WEBSITE_REDIRECT / PARKED
    - manual_review.csv — MANUAL_REVIEW only
    """
    headers, rows = _read_csv(input_path)
    out_headers = _extend_headers(headers)
    result = CsvProcessResult(total=len(rows))

    classified_rows: list[dict[str, str]] = []
    qualified_rows: list[dict[str, str]] = []
    removed_rows: list[dict[str, str]] = []
    manual_rows: list[dict[str, str]] = []
    status_results: list[WebsiteStatusResult] = []

    for row in rows:
        website_link = _website_link_from_row(row)
        try:
            status = classify_website_link(
                website_link,
                timeout=timeout,
                max_redirects=max_redirects,
                retries=retries,
            )
        except Exception as exc:
            logger.warning("Row classify failed for %s: %s", website_link, exc)
            result.errors.append(f"{_business_name_from_row(row)}: {exc}")
            status = classify_website_link("", timeout=timeout)

        status_results.append(status)
        enriched = dict(row)
        enriched.update(status.as_row_fields())
        enriched[QUALIFIED_COLUMN] = "true" if status.qualified else "false"

        classified_rows.append(enriched)
        if status.qualified:
            qualified_rows.append(enriched)
            result.qualified_count += 1
        else:
            removed_rows.append(enriched)
            result.removed_count += 1
        if status.status.value == "MANUAL_REVIEW":
            manual_rows.append(enriched)

    result.status_counts = status_counts(status_results)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "all_classified": output_dir / f"all_classified_{stamp}.csv",
        "qualified_leads": output_dir / f"qualified_leads_{stamp}.csv",
        "removed_leads": output_dir / f"removed_leads_{stamp}.csv",
        "manual_review": output_dir / f"manual_review_{stamp}.csv",
    }
    _write_csv(paths["all_classified"], out_headers, classified_rows)
    _write_csv(paths["qualified_leads"], out_headers, qualified_rows)
    _write_csv(paths["removed_leads"], out_headers, removed_rows)
    _write_csv(paths["manual_review"], out_headers, manual_rows)
    result.output_paths = {k: str(v) for k, v in paths.items()}
    return result


def format_summary(result: CsvProcessResult) -> str:
    lines = [
        f"Total Leads: {result.total}",
        "",
        "Qualified:",
    ]
    for status, count in sorted(result.status_counts.items()):
        if status not in {s.value for s in NOT_QUALIFIED_STATUSES}:
            lines.append(f"  {status}: {count}")
    lines.append("")
    lines.append("Removed:")
    for status in ("ACTIVE", "BUSINESS_WEBSITE_REDIRECT", "PARKED"):
        count = result.status_counts.get(status, 0)
        if count:
            lines.append(f"  {status}: {count}")
    lines.append("")
    lines.append(f"Qualified leads: {result.qualified_count}")
    lines.append(f"Removed leads: {result.removed_count}")
    if result.output_paths:
        lines.append("")
        lines.append("Output files:")
        for name, path in result.output_paths.items():
            lines.append(f"  {name}: {path}")
    return "\n".join(lines)
