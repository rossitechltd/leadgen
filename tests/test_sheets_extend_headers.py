"""Tests for extend_worksheet_headers."""

from unittest.mock import MagicMock, patch

import sheets


def test_extend_worksheet_headers_appends_missing():
    ws = MagicMock()
    existing = [
        "Facebook Link",
        "Business Name",
        "Website Link",
        "refined",
    ]
    full = existing + ["va", "Website Status"]

    with (
        patch("sheets.get_worksheet", return_value=ws),
        patch("sheets._retry_on_quota", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("sheets.invalidate_worksheet_cache"),
        patch.object(ws, "row_values", return_value=existing),
    ):
        result = sheets.extend_worksheet_headers("Sheet", full)

    assert result == full
    ws.update.assert_called_once()
    assert ws.update.call_args[0][0] == [full]


def test_extend_worksheet_headers_noop_when_complete():
    ws = MagicMock()
    headers = ["A", "B", "va"]

    with (
        patch("sheets.get_worksheet", return_value=ws),
        patch("sheets._retry_on_quota", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("sheets.invalidate_worksheet_cache"),
        patch.object(ws, "row_values", return_value=headers),
    ):
        result = sheets.extend_worksheet_headers("Sheet", headers)

    assert result == headers
    ws.update.assert_not_called()
