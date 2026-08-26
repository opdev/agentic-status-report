from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from status.db.draft import (
    dedupe_draft_entries,
    persist_draft_output,
    supersede_current_drafts,
    supersede_unconfirmed_drafts,
)
from status.skills.schemas import DraftEntry, DraftOutput


def test_dedupe_draft_entries_keeps_last_per_epic() -> None:
    first = DraftEntry(
        project="EET",
        epic_key="EET-5519",
        epic_name="Pipeline",
        state="progressing",
        outcome="First draft.",
        evidence=["EET-5520"],
        confidence="high",
    )
    second = DraftEntry(
        project="EET",
        epic_key="EET-5519",
        epic_name="Pipeline",
        state="shipped",
        outcome="Second draft.",
        evidence=["EET-5521"],
        confidence="high",
    )
    deduped = dedupe_draft_entries([first, second])
    assert len(deduped) == 1
    assert deduped[0].outcome == "Second draft."


def test_supersede_unconfirmed_drafts_marks_rows_not_current() -> None:
    session = MagicMock()
    session.execute.return_value.rowcount = 2

    count = supersede_unconfirmed_drafts(session, "pilot", date(2026, 8, 14))

    assert count == 2
    session.execute.assert_called_once()


def test_supersede_current_drafts_includes_confirmed_rows() -> None:
    session = MagicMock()
    session.execute.return_value.rowcount = 3

    count = supersede_current_drafts(session, "pilot", date(2026, 8, 14))

    assert count == 3
    session.execute.assert_called_once()


def test_persist_draft_output_creates_status_entries() -> None:
    session = MagicMock()
    session.get.return_value = None
    session.execute.return_value.rowcount = 0
    session.scalars.return_value.first.return_value = None
    session.scalars.return_value.all.return_value = []

    draft = DraftOutput(
        person="pilot",
        week_ending="2026-08-14",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-5493",
                epic_name="OpenShift Cluster Management Bot",
                state="progressing",
                outcome="Shipped destroy and scheduling features.",
                evidence=["EET-5500", "https://github.com/example-org/example-repo/pull/26"],
                confidence="high",
            )
        ],
        flags=["No calendar signal this week."],
        unticketed_prompt="Anything outside Jira?",
    )

    rows, superseded = persist_draft_output(
        session,
        draft,
        prompt_version="skill_test@latest",
        collection_errors=["github: timeout"],
    )

    assert superseded == 0
    assert len(rows) == 1
    assert rows[0].outcome == "Shipped destroy and scheduling features."
    assert rows[0].epic_key == "EET-5493"
