"""Ledger revisions for human-edited draft entries."""

from __future__ import annotations

from datetime import date, datetime, timezone
from sqlalchemy.orm import Session

from status.db.draft import get_current_drafts
from status.db.models import EntrySource, StatusEntry


class EditValidationError(ValueError):
    """Raised when edit payload would violate ledger constraints."""


def _supersede_entry(session: Session, entry: StatusEntry) -> None:
    if not entry.is_current:
        return
    entry.is_current = False
    session.flush()


def _clone_edited_entry(
    entry: StatusEntry,
    *,
    outcome: str,
    ask: str | None,
    source: str,
) -> StatusEntry:
    return StatusEntry(
        week_ending=entry.week_ending,
        person_id=entry.person_id,
        epic_key=entry.epic_key,
        epic_name_snapshot=entry.epic_name_snapshot,
        project=entry.project,
        state=entry.state,
        outcome=outcome,
        blocker=entry.blocker,
        ask=ask,
        draft_outcome=entry.draft_outcome,
        source=source,
        confidence=entry.confidence,
        needs_human=False,
        prompt_version=entry.prompt_version,
        evidence=entry.evidence,
        extra=entry.extra,
        revision=entry.revision + 1,
        supersedes_entry_id=entry.entry_id,
        is_current=True,
        drafted_at=entry.drafted_at,
        confirmed_at=None,
    )


def persist_edited_entries(
    session: Session,
    person_id: str,
    week_ending: date,
    *,
    edited_outcomes: dict[str, str],
    unticketed_work: str | None,
    existing_unticketed_entry_id: str | None,
) -> list[StatusEntry]:
    """Apply per-entry edits without touching unchanged current rows."""
    current_entries = get_current_drafts(session, person_id, week_ending)
    if not current_entries:
        return []

    by_id = {str(entry.entry_id): entry for entry in current_entries}
    new_entries: list[StatusEntry] = []

    for entry_id, raw_outcome in edited_outcomes.items():
        entry = by_id.get(entry_id)
        if entry is None or not entry.is_current:
            continue

        outcome = raw_outcome.strip()
        if not outcome:
            _supersede_entry(session, entry)
            continue

        if outcome == entry.outcome.strip():
            continue

        _supersede_entry(session, entry)
        new_entry = _clone_edited_entry(
            entry,
            outcome=outcome,
            ask=entry.ask,
            source=EntrySource.DRAFTED_EDITED.value,
        )
        session.add(new_entry)
        new_entries.append(new_entry)

    unticketed_text = unticketed_work.strip() if unticketed_work else ""
    existing_unticketed: StatusEntry | None = None
    if existing_unticketed_entry_id:
        candidate = by_id.get(existing_unticketed_entry_id)
        if candidate is not None and candidate.epic_key is None:
            existing_unticketed = candidate

    if existing_unticketed is not None:
        if not unticketed_text:
            if existing_unticketed.is_current:
                _supersede_entry(session, existing_unticketed)
        elif unticketed_text != existing_unticketed.outcome.strip():
            if existing_unticketed.is_current:
                _supersede_entry(session, existing_unticketed)
            new_entries.append(
                _add_unticketed_entry(session, person_id, week_ending, unticketed_text)
            )
    elif unticketed_text:
        new_entries.append(_add_unticketed_entry(session, person_id, week_ending, unticketed_text))

    session.flush()
    return new_entries


def _add_unticketed_entry(
    session: Session,
    person_id: str,
    week_ending: date,
    outcome: str,
) -> StatusEntry:
    if not outcome.strip():
        raise EditValidationError("unticketed work outcome cannot be blank")
    edited_at = datetime.now(timezone.utc)
    row = StatusEntry(
        week_ending=week_ending,
        person_id=person_id,
        epic_key=None,
        epic_name_snapshot=None,
        project="Unticketed",
        state="progressing",
        outcome=outcome.strip(),
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
    session.add(row)
    return row
