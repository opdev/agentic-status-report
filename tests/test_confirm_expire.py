from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from status.db.confirm import (
    expire_unconfirmed_participations,
    get_unconfirmed_participations,
    increment_reminder_count,
)
from status.db.models import Participation, Person


def test_expire_unconfirmed_sets_status_expired() -> None:
    """Test that expire_unconfirmed changes status to 'expired'."""
    session = MagicMock()
    p1 = MagicMock(status='sent')
    p2 = MagicMock(status='sent')
    session.scalars.return_value.all.return_value = [p1, p2]

    count = expire_unconfirmed_participations(session, date(2026, 8, 14))

    assert count == 2
    assert p1.status == 'expired'
    assert p2.status == 'expired'
    session.flush.assert_called_once()


def test_expire_unconfirmed_only_affects_sent_status() -> None:
    """Test that only 'sent' status participations are expired."""
    session = MagicMock()
    p1 = MagicMock(status='sent')
    session.scalars.return_value.all.return_value = [p1]

    count = expire_unconfirmed_participations(session, date(2026, 8, 14))

    assert count == 1
    assert p1.status == 'expired'


def test_expire_unconfirmed_returns_zero_when_no_participations() -> None:
    """Test that zero is returned when no participations to expire."""
    session = MagicMock()
    session.scalars.return_value.all.return_value = []

    count = expire_unconfirmed_participations(session, date(2026, 8, 14))

    assert count == 0


def test_get_unconfirmed_participations_filters_by_reminder_count() -> None:
    """Test that get_unconfirmed filters by max_reminders."""
    session = MagicMock()
    person1 = Person(person_id="p1", display_name="P1", active=True)
    participation1 = Participation(
        person_id="p1",
        week_ending=date(2026, 8, 14),
        status='sent',
        reminder_count=0,
    )
    person2 = Person(person_id="p2", display_name="P2", active=True)
    participation2 = Participation(
        person_id="p2",
        week_ending=date(2026, 8, 14),
        status='sent',
        reminder_count=1,
    )

    session.execute.return_value.all.return_value = [
        (person1, participation1),
        (person2, participation2),
    ]

    result = get_unconfirmed_participations(session, date(2026, 8, 14), max_reminders=2)

    assert len(result) == 2


def test_get_unconfirmed_participations_excludes_max_reminders() -> None:
    """Test that persons at max reminders are excluded."""
    session = MagicMock()
    person1 = Person(person_id="p1", display_name="P1", active=True)
    participation1 = Participation(
        person_id="p1",
        week_ending=date(2026, 8, 14),
        status='sent',
        reminder_count=1,
    )

    session.execute.return_value.all.return_value = [(person1, participation1)]

    result = get_unconfirmed_participations(session, date(2026, 8, 14), max_reminders=2)

    assert len(result) == 1


def test_increment_reminder_count_increments() -> None:
    """Test that reminder count is incremented."""
    session = MagicMock()
    row = MagicMock(reminder_count=0)
    session.get.return_value = row

    increment_reminder_count(session, "p1", date(2026, 8, 14))

    assert row.reminder_count == 1
    session.flush.assert_called_once()


def test_increment_reminder_count_increments_from_non_zero() -> None:
    """Test that reminder count increments from non-zero value."""
    session = MagicMock()
    row = MagicMock(reminder_count=1)
    session.get.return_value = row

    increment_reminder_count(session, "p1", date(2026, 8, 14))

    assert row.reminder_count == 2


def test_increment_reminder_count_no_op_when_no_participation() -> None:
    """Test that increment is no-op when participation doesn't exist."""
    session = MagicMock()
    session.get.return_value = None

    increment_reminder_count(session, "p1", date(2026, 8, 14))

    session.flush.assert_not_called()
