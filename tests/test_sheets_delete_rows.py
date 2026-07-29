"""Tests for delete_rows row-index validation."""

from unittest.mock import MagicMock, patch

import sheets


def test_delete_rows_accepts_max_row_param():
    """Explicit max_row skips positional read and validates indices."""
    ws = MagicMock()
    ws.row_count = 5000

    with (
        patch("sheets.get_worksheet", return_value=ws),
        patch("sheets._retry_on_quota", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("sheets._batch_delete_single_rows") as batch_singles,
        patch("sheets.invalidate_worksheet_cache"),
    ):
        sheets.delete_rows("Dynamic Lead Sheet", [1500, 1600, 1800], max_row=2000)

    batch_singles.assert_called_once()
    deleted = batch_singles.call_args[0][1]
    assert deleted == [1800, 1600, 1500]
    ws.get.assert_not_called()


def test_delete_rows_uses_positional_max_when_max_row_omitted():
    ws = MagicMock()
    ws.row_count = 5000

    with (
        patch("sheets.get_worksheet", return_value=ws),
        patch("sheets._worksheet_headers", return_value=["A"]),
        patch("sheets._positional_max_data_row", return_value=2000),
        patch("sheets._retry_on_quota", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("sheets._batch_delete_single_rows") as batch_singles,
        patch("sheets.invalidate_worksheet_cache"),
    ):
        sheets.delete_rows("Sheet", [1500, 1600, 1800])

    batch_singles.assert_called_once()
    deleted = batch_singles.call_args[0][1]
    assert deleted == [1800, 1600, 1500]
    assert batch_singles.call_args[1]["max_row"] == 2000


def test_delete_rows_skips_indices_beyond_positional_max():
    ws = MagicMock()
    ws.row_count = 5000

    with (
        patch("sheets.get_worksheet", return_value=ws),
        patch("sheets._worksheet_headers", return_value=["A"]),
        patch("sheets._positional_max_data_row", return_value=383),
        patch("sheets._retry_on_quota", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("sheets._batch_delete_single_rows") as batch_singles,
        patch("sheets.invalidate_worksheet_cache"),
    ):
        sheets.delete_rows("Sheet", [100, 448])

    batch_singles.assert_called_once()
    deleted = batch_singles.call_args[0][1]
    assert deleted == [100]
    assert batch_singles.call_args[1]["max_row"] == 383


def test_delete_rows_skips_truly_out_of_range():
    ws = MagicMock()
    ws.row_count = 11

    with (
        patch("sheets.get_worksheet", return_value=ws),
        patch("sheets._retry_on_quota", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("sheets._batch_delete_single_rows") as batch_singles,
        patch("sheets.invalidate_worksheet_cache"),
    ):
        sheets.delete_rows("Sheet", [5, 99], max_row=11)

    batch_singles.assert_called_once()
    deleted = batch_singles.call_args[0][1]
    assert deleted == [5]


def test_batch_delete_single_rows_pauses_between_chunks():
    ws = MagicMock()
    row_indices = list(range(100, 50, -1))

    with (
        patch("sheets._retry_on_quota", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("sheets._delete_chunk_pause") as pause,
    ):
        sheets._batch_delete_single_rows(ws, row_indices, max_row=500)

    assert pause.call_count >= 1
