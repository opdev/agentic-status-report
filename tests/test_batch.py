from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, call, patch

from status.batch import (
    BatchStageError,
    mark_participation_failure,
    run_batch_operation,
)
from status.db.models import Participation, Person


def _session_context(session: MagicMock) -> MagicMock:
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    return context


def test_run_batch_operation_isolates_people_and_records_stage_failure() -> None:
    week = date(2026, 9, 18)
    first = Person(person_id="first", display_name="First User")
    second = Person(person_id="second", display_name="Second User")

    roster_session = MagicMock()
    first_session = MagicMock()
    first_session.get.return_value = first
    failure_session = MagicMock()
    second_session = MagicMock()
    second_session.get.return_value = second

    contexts = [
        _session_context(roster_session),
        _session_context(first_session),
        _session_context(failure_session),
        _session_context(second_session),
    ]

    def operation(session: MagicMock, person: Person, operation_week: date) -> str:
        assert operation_week == week
        if person.person_id == "first":
            raise BatchStageError("collect_failed", "Jira unavailable")
        assert session is second_session
        return "completed"

    with (
        patch("status.batch.get_session", side_effect=contexts),
        patch("status.batch.get_eligible_persons", return_value=[first, second]),
        patch("status.batch.mark_participation_failure") as mark_failure,
    ):
        results = run_batch_operation(
            week,
            operation,
            "collect-and-draft",
            default_failure_status="draft_failed",
        )

    assert [result.success for result in results] == [False, True]
    assert results[0].error == "Jira unavailable"
    assert results[1].result == "completed"
    mark_failure.assert_called_once_with(
        failure_session,
        "first",
        week,
        status="collect_failed",
        error="Jira unavailable",
    )
    assert contexts[1].__exit__.call_args.args[0] is BatchStageError
    second_session.get.assert_called_once_with(Person, "second")


def test_mark_participation_failure_creates_row_and_truncates_note() -> None:
    session = MagicMock()
    session.get.return_value = None
    error = "x" * 2_100

    mark_participation_failure(
        session,
        "pilot",
        date(2026, 9, 18),
        status="draft_failed",
        error=error,
    )

    row = session.add.call_args.args[0]
    assert isinstance(row, Participation)
    assert row.status == "draft_failed"
    assert row.note == "x" * 2_000
    session.flush.assert_called_once_with()


def test_mark_participation_failure_updates_existing_row() -> None:
    week = date(2026, 9, 18)
    row = Participation(person_id="pilot", week_ending=week, status="sent")
    session = MagicMock()
    session.get.return_value = row

    mark_participation_failure(
        session,
        "pilot",
        week,
        status="nudge_failed",
        error="Slack unavailable",
    )

    assert row.status == "nudge_failed"
    assert row.note == "Slack unavailable"
    assert session.add.call_args_list == []
    session.flush.assert_has_calls([call()])
