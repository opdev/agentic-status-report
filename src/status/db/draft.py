"""Persist drafter output to the ledger."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from status.db.models import EntrySource, Epic, Flag, Participation, Person, StatusEntry
from status.skills.schemas import DraftEntry, DraftOutput

log = logging.getLogger(__name__)


def ensure_person(session: Session, person_id: str, *, display_name: str | None = None) -> Person:
    person = session.get(Person, person_id)
    if person is not None:
        return person

    person = Person(person_id=person_id, display_name=display_name or person_id)
    session.add(person)
    session.flush()
    return person


def upsert_epic(session: Session, epic_key: str, epic_name: str, project: str) -> None:
    epic = session.get(Epic, epic_key)
    now = datetime.now(timezone.utc)
    if epic is None:
        session.add(
            Epic(
                epic_key=epic_key,
                current_name=epic_name,
                project=project,
                last_seen_at=now,
            )
        )
        return

    epic.current_name = epic_name
    epic.project = project
    epic.last_seen_at = now


def _parse_week_ending(value: str) -> date:
    return date.fromisoformat(value)


def supersede_unconfirmed_drafts(session: Session, person_id: str, week_ending: date) -> int:
    """Mark unconfirmed current drafts as non-current. Returns rows superseded."""
    result = session.execute(
        update(StatusEntry)
        .where(
            StatusEntry.person_id == person_id,
            StatusEntry.week_ending == week_ending,
            StatusEntry.is_current.is_(True),
            StatusEntry.confirmed_at.is_(None),
        )
        .values(is_current=False)
    )
    return int(result.rowcount or 0)


def supersede_current_drafts(session: Session, person_id: str, week_ending: date) -> int:
    """Mark all current rows non-current so a full re-draft can be persisted."""
    result = session.execute(
        update(StatusEntry)
        .where(
            StatusEntry.person_id == person_id,
            StatusEntry.week_ending == week_ending,
            StatusEntry.is_current.is_(True),
        )
        .values(is_current=False)
    )
    return int(result.rowcount or 0)


def acknowledge_draft_flags(session: Session, person_id: str, week_ending: date) -> int:
    """Acknowledge prior draft-run flags for this person/week before re-drafting."""
    result = session.execute(
        update(Flag)
        .where(
            Flag.person_id == person_id,
            Flag.week_ending == week_ending,
            Flag.acknowledged.is_(False),
        )
        .values(acknowledged=True)
    )
    return int(result.rowcount or 0)


def _entry_extra(entry: DraftEntry) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if entry.why_flagged:
        extra["why_flagged"] = entry.why_flagged
    return extra


def _status_entry_from_draft(
    entry: DraftEntry,
    *,
    person_id: str,
    week_ending: date,
    prompt_version: str,
    drafted_at: datetime,
    supersedes: StatusEntry | None = None,
) -> StatusEntry:
    revision = 1 if supersedes is None else supersedes.revision + 1
    return StatusEntry(
        week_ending=week_ending,
        person_id=person_id,
        epic_key=entry.epic_key,
        epic_name_snapshot=entry.epic_name,
        project=entry.project,
        state=entry.state,
        outcome=entry.outcome,
        blocker=entry.blocker,
        ask=entry.ask,
        draft_outcome=entry.outcome,
        source=EntrySource.DRAFTED.value,
        confidence=entry.confidence,
        needs_human=entry.needs_human,
        prompt_version=prompt_version,
        evidence=list(entry.evidence),
        extra=_entry_extra(entry),
        revision=revision,
        supersedes_entry_id=supersedes.entry_id if supersedes else None,
        is_current=True,
        drafted_at=drafted_at,
        confirmed_at=None,
    )


def _find_superseded_row(
    session: Session,
    person_id: str,
    week_ending: date,
    epic_key: str | None,
) -> StatusEntry | None:
    """Find the most recent superseded row for the same epic grain."""
    stmt = (
        select(StatusEntry)
        .where(
            StatusEntry.person_id == person_id,
            StatusEntry.week_ending == week_ending,
            StatusEntry.is_current.is_(False),
            StatusEntry.epic_key == epic_key if epic_key else StatusEntry.epic_key.is_(None),
        )
        .order_by(StatusEntry.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _reset_participation_for_redraft(
    session: Session,
    person_id: str,
    week_ending: date,
) -> None:
    row = session.get(Participation, (person_id, week_ending))
    if row is None:
        return
    if row.status == "confirmed":
        row.status = "sent"
        row.confirmed_at = None


def dedupe_draft_entries(entries: list[DraftEntry]) -> list[DraftEntry]:
    """Keep one entry per epic; unticketed rows are keyed by project + outcome."""
    seen: dict[str | tuple[str, ...], DraftEntry] = {}
    order: list[str | tuple[str, ...]] = []
    for entry in entries:
        key: str | tuple[str, ...]
        if entry.epic_key:
            key = entry.epic_key
        else:
            key = ("unticketed", entry.project, entry.outcome)
        if key not in seen:
            order.append(key)
        seen[key] = entry
    return [seen[key] for key in order]


def persist_draft_output(
    session: Session,
    draft: DraftOutput,
    *,
    prompt_version: str,
    collection_errors: list[str] | None = None,
) -> tuple[list[StatusEntry], int]:
    """Replace current drafts for (person, week) with a new draft revision."""
    week_ending = _parse_week_ending(draft.week_ending)
    ensure_person(session, draft.person)
    superseded_count = supersede_current_drafts(session, draft.person, week_ending)
    session.flush()
    _reset_participation_for_redraft(session, draft.person, week_ending)

    entries = dedupe_draft_entries(draft.entries)
    if len(entries) < len(draft.entries):
        log.warning(
            "dropped %s duplicate draft entries for %s week %s",
            len(draft.entries) - len(entries),
            draft.person,
            week_ending.isoformat(),
        )

    acknowledge_draft_flags(session, draft.person, week_ending)

    drafted_at = datetime.now(timezone.utc)
    saved: list[StatusEntry] = []

    for entry in entries:
        if entry.epic_key and entry.epic_name:
            upsert_epic(session, entry.epic_key, entry.epic_name, entry.project)

        supersedes = _find_superseded_row(session, draft.person, week_ending, entry.epic_key)
        row = _status_entry_from_draft(
            entry,
            person_id=draft.person,
            week_ending=week_ending,
            prompt_version=prompt_version,
            drafted_at=drafted_at,
            supersedes=supersedes,
        )
        session.add(row)
        saved.append(row)

    _persist_flags(
        session,
        draft,
        week_ending,
        entries=entries,
        collection_errors=collection_errors or [],
    )
    session.flush()
    return saved, superseded_count


def _persist_flags(
    session: Session,
    draft: DraftOutput,
    week_ending: date,
    *,
    entries: list[DraftEntry],
    collection_errors: list[str],
) -> None:
    for message in draft.flags:
        session.add(
            Flag(
                week_ending=week_ending,
                person_id=draft.person,
                flag_type="draft",
                message=message,
            )
        )

    if draft.unticketed_prompt.strip():
        session.add(
            Flag(
                week_ending=week_ending,
                person_id=draft.person,
                flag_type="unticketed",
                message=draft.unticketed_prompt.strip(),
            )
        )

    for message in collection_errors:
        session.add(
            Flag(
                week_ending=week_ending,
                person_id=draft.person,
                flag_type="collection",
                message=message,
            )
        )

    for entry in entries:
        if entry.why_flagged:
            session.add(
                Flag(
                    week_ending=week_ending,
                    person_id=draft.person,
                    epic_key=entry.epic_key,
                    flag_type="entry",
                    message=entry.why_flagged,
                )
            )


def get_current_drafts(session: Session, person_id: str, week_ending: date) -> list[StatusEntry]:
    stmt = select(StatusEntry).where(
        StatusEntry.person_id == person_id,
        StatusEntry.week_ending == week_ending,
        StatusEntry.is_current.is_(True),
        StatusEntry.confirmed_at.is_(None),
    )
    return list(session.scalars(stmt).all())


def persist_edited_entries(
    session: Session,
    person_id: str,
    week_ending: date,
    *,
    edited_outcomes: dict[int, str],  # index -> new outcome
    dropped_indices: set[int],
    unticketed_work: str | None,
    leadership_asks: str | None,
) -> list[StatusEntry]:
    """Create new 'drafted_edited' revisions for changed entries.

    Args:
        session: Database session
        person_id: Person ID
        week_ending: Week ending date
        edited_outcomes: Map of entry index to new outcome text
        dropped_indices: Set of indices to remove
        unticketed_work: Optional unticketed work description
        leadership_asks: Optional leadership asks

    Returns:
        List of newly created/updated entries
    """
    # Get current drafts in order
    current_entries = get_current_drafts(session, person_id, week_ending)
    if not current_entries:
        return []

    # Supersede all current drafts
    for entry in current_entries:
        entry.is_current = False

    edited_at = datetime.now(timezone.utc)
    new_entries: list[StatusEntry] = []

    # Process each entry
    for idx, entry in enumerate(current_entries):
        # Skip dropped entries
        if idx in dropped_indices:
            continue

        # Check if outcome was edited
        new_outcome = edited_outcomes.get(idx)
        if new_outcome and new_outcome.strip() != entry.outcome.strip():
            # Create edited revision
            new_entry = StatusEntry(
                week_ending=week_ending,
                person_id=person_id,
                epic_key=entry.epic_key,
                epic_name_snapshot=entry.epic_name_snapshot,
                project=entry.project,
                state=entry.state,
                outcome=new_outcome.strip(),
                blocker=entry.blocker,
                ask=leadership_asks if leadership_asks and leadership_asks.strip() else entry.ask,
                draft_outcome=entry.draft_outcome,  # Preserve original draft
                source=EntrySource.DRAFTED_EDITED.value,
                confidence=entry.confidence,
                needs_human=False,  # Human just reviewed it
                prompt_version=entry.prompt_version,
                evidence=entry.evidence,
                extra=entry.extra,
                revision=entry.revision + 1,
                supersedes_entry_id=entry.entry_id,
                is_current=True,
                drafted_at=entry.drafted_at,
                confirmed_at=None,
            )
            session.add(new_entry)
            new_entries.append(new_entry)
        else:
            # No change, keep as current but update asks if provided
            entry.is_current = True
            if leadership_asks and leadership_asks.strip():
                entry.ask = leadership_asks.strip()
            new_entries.append(entry)

    # Add unticketed work as a new entry if provided
    if unticketed_work and unticketed_work.strip():
        unticketed_entry = StatusEntry(
            week_ending=week_ending,
            person_id=person_id,
            epic_key=None,
            epic_name_snapshot=None,
            project="Unticketed",
            state="progressing",
            outcome=unticketed_work.strip(),
            blocker=None,
            ask=None,
            draft_outcome=None,
            source=EntrySource.HUMAN_WRITTEN.value,
            confidence="high",
            needs_human=False,
            prompt_version="manual",
            evidence=[],
            extra={},
            revision=1,
            supersedes_entry_id=None,
            is_current=True,
            drafted_at=edited_at,
            confirmed_at=None,
        )
        session.add(unticketed_entry)
        new_entries.append(unticketed_entry)

    session.flush()
    return new_entries
