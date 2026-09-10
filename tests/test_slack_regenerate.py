from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from status.db.confirm import record_regeneration
from status.db.models import Participation
from status.slack.blocks import build_regenerate_modal


def test_build_regenerate_modal_includes_message_metadata() -> None:
    modal = build_regenerate_modal(
        person_id="pilot",
        week_ending=date(2026, 8, 14),
        channel="C123",
        message_ts="1234.5678",
    )
    metadata = modal["private_metadata"]
    assert '"channel": "C123"' in metadata
    assert '"message_ts": "1234.5678"' in metadata


def test_record_regeneration_sets_participation_fields() -> None:
    session = MagicMock()
    session.get.return_value = None

    row = record_regeneration(
        session,
        "pilot",
        date(2026, 8, 14),
        reason="missed_work",
        notes="Include the cluster bot work.",
    )

    assert isinstance(row, Participation)
    assert row.regenerated is True
    assert row.regenerate_reason == "missed_work: Include the cluster bot work."
    assert row.note == "Include the cluster bot work."
    session.add.assert_called_once()


def test_run_regenerate_background_updates_message_and_records_participation() -> None:
    from status.slack.handlers import _run_regenerate_background

    client = MagicMock()
    draft_result = MagicMock(persisted_entry_ids=["a"], superseded_count=2)

    with (
        patch("status.slack.handlers.get_session") as mock_session_ctx,
        patch("status.slack.handlers.record_regeneration") as mock_record,
        patch("status.collectors.run_collect", return_value={"person": "pilot", "week_end": "2026-08-14"}),
        patch("status.skills.drafter.draft_and_persist", return_value=draft_result),
        patch("status.slack.handlers._update_message") as mock_update,
    ):
        session = MagicMock()
        mock_session_ctx.return_value.__enter__.return_value = session

        _run_regenerate_background(
            person_id="pilot",
            week_ending=date(2026, 8, 14),
            reason="wrong_grouping",
            notes="Group by initiative",
            channel="C123",
            message_ts="1234.5678",
            slack_user_id="U123",
            display_name="Pilot User",
            client=client,
        )

    mock_record.assert_called_once()
    mock_update.assert_called_once()
    client.chat_postEphemeral.assert_called_once()
