from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch
from uuid import uuid4

from status.db.edit import persist_edited_entries
from status.db.models import EntrySource, Flag, StatusEntry
from status.slack.blocks import (
    EDIT_MODAL_MAX_TICKETED,
    build_edit_modal,
    parse_edit_submission_values,
)


def _entry(
    *,
    epic_key: str | None = "EET-1",
    outcome: str = "Original outcome.",
    ask: str | None = None,
) -> StatusEntry:
    return StatusEntry(
        entry_id=uuid4(),
        week_ending=date(2026, 8, 14),
        person_id="pilot",
        epic_key=epic_key,
        epic_name_snapshot="Pipeline" if epic_key else None,
        project="EET",
        state="progressing",
        outcome=outcome,
        ask=ask,
        source=EntrySource.DRAFTED.value,
        is_current=True,
        revision=1,
    )


def test_build_edit_modal_respects_slack_input_block_limit() -> None:
    entries = [_entry(epic_key=f"EET-{idx}") for idx in range(EDIT_MODAL_MAX_TICKETED + 4)]
    modal = build_edit_modal(
        person_id="pilot",
        week_ending=date(2026, 8, 14),
        entries=entries,
        flags=[],
        channel="C123",
        message_ts="1234.5678",
    )
    input_blocks = [block for block in modal["blocks"] if block["type"] == "input"]
    assert len(input_blocks) <= 10
    assert len(input_blocks) == EDIT_MODAL_MAX_TICKETED + 1


def test_build_edit_modal_prefills_unticketed_from_flag() -> None:
    flag = Flag(
        flag_id=uuid4(),
        week_ending=date(2026, 8, 14),
        person_id="pilot",
        flag_type="unticketed",
        message="Meetings and design reviews.",
        acknowledged=False,
    )
    modal = build_edit_modal(
        person_id="pilot",
        week_ending=date(2026, 8, 14),
        entries=[_entry()],
        flags=[flag],
        channel="C123",
        message_ts="1234.5678",
    )
    unticketed_block = next(
        block for block in modal["blocks"] if block.get("block_id") == "unticketed_work"
    )
    assert unticketed_block["element"]["initial_value"] == "Meetings and design reviews."


def test_parse_edit_submission_values_treats_cleared_field_as_drop() -> None:
    entry_id = str(uuid4())
    values = {
        f"entry_{entry_id}": {"outcome_value": {}},
    }
    edited, _ = parse_edit_submission_values(values, entry_ids=[entry_id])
    assert edited[entry_id] == ""


def test_parse_edit_submission_values_treats_missing_block_as_drop() -> None:
    entry_id = str(uuid4())
    edited, _ = parse_edit_submission_values({}, entry_ids=[entry_id])
    assert edited[entry_id] == ""


def test_parse_edit_submission_values_maps_entry_ids() -> None:
    entry_id = str(uuid4())
    values = {
        f"entry_{entry_id}": {"outcome_value": {"value": "Updated outcome."}},
        "unticketed_work": {"unticketed_value": {"value": "Side project"}},
    }
    edited, unticketed = parse_edit_submission_values(values, entry_ids=[entry_id])
    assert edited[entry_id] == "Updated outcome."
    assert unticketed == "Side project"


def test_build_edit_modal_has_missed_work_field() -> None:
    modal = build_edit_modal(
        person_id="pilot",
        week_ending=date(2026, 8, 14),
        entries=[_entry()],
        flags=[],
        channel="C123",
        message_ts="1234.5678",
    )
    missed_block = next(
        block for block in modal["blocks"] if block.get("block_id") == "unticketed_work"
    )
    assert missed_block["label"]["text"] == "Missed or additional work this week"
    assert "leadership_asks" not in str(modal)


def test_persist_edited_entries_creates_drafted_edited_revision() -> None:
    entry = _entry()
    session = MagicMock()
    session.flush = MagicMock()

    with patch("status.db.edit.get_current_drafts", return_value=[entry]):
        new_rows = persist_edited_entries(
            session,
            "pilot",
            date(2026, 8, 14),
            edited_outcomes={str(entry.entry_id): "Edited outcome."},
            unticketed_work=None,
            existing_unticketed_entry_id=None,
        )

    assert entry.is_current is False
    assert len(new_rows) == 1
    assert new_rows[0].source == EntrySource.DRAFTED_EDITED.value
    assert new_rows[0].outcome == "Edited outcome."
    assert new_rows[0].supersedes_entry_id == entry.entry_id
    assert new_rows[0].revision == 2
    session.add.assert_called_once()


def test_persist_edited_entries_drop_epic_supersedes_without_reinsert() -> None:
    entry = _entry()
    session = MagicMock()
    session.flush = MagicMock()

    with patch("status.db.edit.get_current_drafts", return_value=[entry]):
        new_rows = persist_edited_entries(
            session,
            "pilot",
            date(2026, 8, 14),
            edited_outcomes={str(entry.entry_id): ""},
            unticketed_work=None,
            existing_unticketed_entry_id=None,
        )

    assert entry.is_current is False
    assert new_rows == []
    session.add.assert_not_called()


def test_persist_edited_entries_adds_missed_work() -> None:
    entry = _entry()
    session = MagicMock()
    session.flush = MagicMock()

    with patch("status.db.edit.get_current_drafts", return_value=[entry]):
        new_rows = persist_edited_entries(
            session,
            "pilot",
            date(2026, 8, 14),
            edited_outcomes={str(entry.entry_id): entry.outcome},
            unticketed_work="Partner sync and design review.",
            existing_unticketed_entry_id=None,
        )

    assert len(new_rows) == 1
    assert new_rows[0].epic_key is None
    assert new_rows[0].outcome == "Partner sync and design review."
    assert new_rows[0].source == EntrySource.HUMAN_WRITTEN.value
    session.add.assert_called_once()


def test_persist_edited_entries_leaves_unchanged_rows_current() -> None:
    entry = _entry()
    session = MagicMock()
    session.flush = MagicMock()

    with patch("status.db.edit.get_current_drafts", return_value=[entry]):
        new_rows = persist_edited_entries(
            session,
            "pilot",
            date(2026, 8, 14),
            edited_outcomes={str(entry.entry_id): entry.outcome},
            unticketed_work=None,
            existing_unticketed_entry_id=None,
        )

    assert entry.is_current is True
    assert new_rows == []
    session.add.assert_not_called()
