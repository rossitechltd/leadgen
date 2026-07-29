"""Tests for CSV website qualification output."""

from pathlib import Path
from unittest.mock import patch

from app.qualify.csv_processor import process_leads_csv


def test_process_csv_writes_outputs(tmp_path: Path):
    input_csv = tmp_path / "leads.csv"
    input_csv.write_text(
        "Facebook Link,Business Name,Website Link\n"
        "https://facebook.com/a,Active Biz,https://joesplumbing.co.uk\n"
        "https://facebook.com/b,No Site,\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    with patch("app.qualify.csv_processor.classify_website_link") as mock_classify:
        from app.qualify.website_status import WebsiteStatusCode, WebsiteStatusResult

        def _fake(url, **kwargs):
            if not url:
                return WebsiteStatusResult(
                    status=WebsiteStatusCode.NO_WEBSITE,
                    reason="empty",
                    qualified=True,
                )
            return WebsiteStatusResult(
                status=WebsiteStatusCode.ACTIVE,
                reason="active",
                qualified=False,
                original_url=url,
            )

        mock_classify.side_effect = _fake
        result = process_leads_csv(input_csv, out_dir, timeout=1, max_redirects=5, retries=1)

    assert result.total == 2
    assert result.qualified_count == 1
    assert result.removed_count == 1
    assert len(result.output_paths) == 4
