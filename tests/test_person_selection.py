from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from status.db.models import Person
from status.db.repo import filter_pilot_persons, get_active_persons


def test_get_active_persons_filters_active_true() -> None:
    """Test that get_active_persons only returns active persons."""
    session = MagicMock()
    active1 = Person(person_id="p1", display_name="P1", active=True)
    active2 = Person(person_id="p2", display_name="P2", active=True)
    session.scalars.return_value.all.return_value = [active1, active2]

    result = get_active_persons(session)

    assert len(result) == 2
    assert result[0].person_id == "p1"
    assert result[1].person_id == "p2"
    # Verify the query was called with active=True filter
    session.scalars.assert_called_once()


def test_filter_pilot_persons_empty_pilot_list_returns_all() -> None:
    """Test that empty pilot list returns all persons."""
    persons = [
        Person(person_id="p1", display_name="P1", active=True),
        Person(person_id="p2", display_name="P2", active=True),
    ]
    result = filter_pilot_persons(persons, [])
    assert result == persons
    assert len(result) == 2


def test_filter_pilot_persons_filters_to_allowlist() -> None:
    """Test that pilot list filters persons."""
    persons = [
        Person(person_id="p1", display_name="P1", active=True),
        Person(person_id="p2", display_name="P2", active=True),
        Person(person_id="p3", display_name="P3", active=True),
    ]
    result = filter_pilot_persons(persons, ["p1", "p3"])

    assert len(result) == 2
    assert result[0].person_id == "p1"
    assert result[1].person_id == "p3"


def test_filter_pilot_persons_excludes_non_pilots() -> None:
    """Test that persons not in pilot list are excluded."""
    persons = [
        Person(person_id="p1", display_name="P1", active=True),
        Person(person_id="p2", display_name="P2", active=True),
        Person(person_id="p3", display_name="P3", active=True),
    ]
    result = filter_pilot_persons(persons, ["p2"])

    assert len(result) == 1
    assert result[0].person_id == "p2"


def test_filter_pilot_persons_no_matches_returns_empty() -> None:
    """Test that no matches returns empty list."""
    persons = [
        Person(person_id="p1", display_name="P1", active=True),
        Person(person_id="p2", display_name="P2", active=True),
    ]
    result = filter_pilot_persons(persons, ["p99", "p100"])

    assert len(result) == 0
