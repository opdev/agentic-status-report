"""Database query helpers."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from status.db.models import Flag, Participation, Person, StatusEntry


def get_current_entries_for_week(session: Session, week_ending: date) -> list[StatusEntry]:
    stmt = select(StatusEntry).where(
        StatusEntry.week_ending == week_ending,
        StatusEntry.is_current.is_(True),
    )
    return list(session.scalars(stmt).all())


def get_confirmed_entries_for_week(session: Session, week_ending: date) -> list[StatusEntry]:
    stmt = select(StatusEntry).where(
        StatusEntry.week_ending == week_ending,
        StatusEntry.is_current.is_(True),
        StatusEntry.confirmed_at.is_not(None),
    )
    return list(session.scalars(stmt).all())


def get_participation_for_week(session: Session, week_ending: date) -> list[Participation]:
    stmt = select(Participation).where(Participation.week_ending == week_ending)
    return list(session.scalars(stmt).all())


def get_previous_confirmed_entries(
    session: Session,
    person_id: str,
    before_week_ending: date,
    *,
    weeks: int = 3,
) -> list[dict]:
    """Return confirmed entries from the prior N weeks for drafter context."""
    earliest = before_week_ending - timedelta(weeks=weeks)
    stmt = (
        select(StatusEntry)
        .where(
            StatusEntry.person_id == person_id,
            StatusEntry.week_ending < before_week_ending,
            StatusEntry.week_ending >= earliest,
            StatusEntry.is_current.is_(True),
            StatusEntry.confirmed_at.is_not(None),
        )
        .order_by(desc(StatusEntry.week_ending))
    )
    rows = list(session.scalars(stmt).all())
    return [
        {
            "project": row.project,
            "epic_key": row.epic_key,
            "epic_name": row.epic_name_snapshot,
            "state": row.state,
            "outcome": row.outcome,
            "evidence": row.evidence,
            "blocker": row.blocker,
            "ask": row.ask,
            "week_ending": row.week_ending.isoformat(),
        }
        for row in rows
    ]


def get_unacknowledged_flags_for_week(session: Session, week_ending: date) -> list[Flag]:
    stmt = select(Flag).where(
        Flag.week_ending == week_ending,
        Flag.acknowledged.is_(False),
    )
    return list(session.scalars(stmt).all())


def get_person(session: Session, person_id: str) -> Person | None:
    return session.get(Person, person_id)


def get_active_persons(session: Session) -> list[Person]:
    """Query all active persons (Person.active = true)."""
    stmt = select(Person).where(Person.active.is_(True))
    return list(session.scalars(stmt).all())


def filter_pilot_persons(persons: list[Person], pilot_ids: list[str]) -> list[Person]:
    """
    Filter persons by pilot allowlist if non-empty.
    If pilot_ids is empty, return all persons.
    """
    if not pilot_ids:
        return persons
    pilot_set = set(pilot_ids)
    return [p for p in persons if p.person_id in pilot_set]


def get_eligible_persons(session: Session) -> list[Person]:
    """
    Get persons eligible for batch operations:
    - Query Person.active = true
    - Filter by PILOT_PERSON_IDS env if non-empty
    """
    from status.config import get_settings

    settings = get_settings()

    active_persons = get_active_persons(session)
    return filter_pilot_persons(active_persons, settings.pilot_person_id_list)
