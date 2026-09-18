"""Tests for the language window parser."""
from app.llm import _parse_window_hours


def test_pm_to_pm():
    assert _parse_window_hours("from 1 PM to 3 PM") == [13, 14]


def test_noon_to_pm():
    assert _parse_window_hours("wash the panels from noon until 2 PM") == [12, 13]


def test_am_to_am():
    assert _parse_window_hours("2 AM until 5 AM") == [2, 3, 4]


def test_am_to_noon():
    assert _parse_window_hours("from 10 AM until noon") == [10, 11]


def test_24h_clock():
    assert _parse_window_hours("between 13:00 and 15:00") == [13, 14]


def test_no_window_returns_none():
    assert _parse_window_hours("The cafeteria menu changes tomorrow.") is None
