"""Tests for sheet row delete helpers."""

from sheets import _contiguous_row_runs


def test_contiguous_row_runs_single_and_groups():
    assert _contiguous_row_runs([5]) == [(5, 5)]
    assert _contiguous_row_runs([5, 6, 10, 89]) == [(5, 6), (10, 10), (89, 89)]


def test_contiguous_row_runs_delete_order_high_first():
    runs = _contiguous_row_runs([5, 6, 10, 89])
    ordered = sorted(runs, key=lambda run: run[1], reverse=True)
    assert ordered == [(89, 89), (10, 10), (5, 6)]
