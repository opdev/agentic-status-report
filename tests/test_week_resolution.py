from __future__ import annotations

from datetime import date, timedelta

import pytest

from status.collectors.payload import resolve_week_ending


def test_resolve_week_ending_auto_mode_returns_most_recent_friday() -> None:
    """Test that 'auto' mode returns most recent Friday."""
    result = resolve_week_ending("auto")
    assert result.weekday() == 4  # Friday
    assert result <= date.today()


def test_resolve_week_ending_none_returns_most_recent_friday() -> None:
    """Test that None returns most recent Friday."""
    result = resolve_week_ending(None)
    assert result.weekday() == 4  # Friday
    assert result <= date.today()


def test_resolve_week_ending_explicit_date_validates_friday() -> None:
    """Test that explicit date must be a Friday."""
    # Use a known Friday
    friday = date(2026, 8, 14)
    assert friday.weekday() == 4  # Verify it's Friday

    result = resolve_week_ending(friday.isoformat())
    assert result == friday


def test_resolve_week_ending_non_friday_raises_error() -> None:
    """Test that non-Friday date raises ValueError."""
    monday = date(2026, 8, 10)
    assert monday.weekday() == 0  # Monday

    with pytest.raises(ValueError, match="must be a Friday"):
        resolve_week_ending(monday.isoformat())


def test_resolve_week_ending_today_if_today_is_friday() -> None:
    """Test that if today is Friday, auto mode returns today."""
    today = date.today()

    if today.weekday() == 4:  # Today is Friday
        result = resolve_week_ending("auto")
        assert result == today
    else:
        # Calculate most recent Friday (in the past)
        days_since_friday = (today.weekday() - 4) % 7
        expected = today - timedelta(days=days_since_friday)
        result = resolve_week_ending("auto")
        assert result == expected


def test_resolve_week_ending_case_insensitive() -> None:
    """Test that 'AUTO', 'Auto', 'auto' all work."""
    result1 = resolve_week_ending("AUTO")
    result2 = resolve_week_ending("Auto")
    result3 = resolve_week_ending("auto")

    assert result1 == result2 == result3
    assert result1.weekday() == 4
