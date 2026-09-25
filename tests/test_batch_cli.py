from __future__ import annotations

from datetime import date
from unittest.mock import patch

from typer.testing import CliRunner

from status.batch import BatchResult
from status.cli import app


def test_collect_and_draft_exits_nonzero_on_partial_failure() -> None:
    failure = BatchResult(person_id="pilot", success=False, error="collector failed")

    with (
        patch("status.collectors.payload.resolve_week_ending", return_value=date(2026, 9, 18)),
        patch("status.cli.run_batch_operation", return_value=[failure]) as run_batch,
    ):
        result = CliRunner().invoke(
            app,
            ["run-week", "collect-and-draft", "--week", "2026-09-18", "--dry-run"],
        )

    assert result.exit_code == 1
    assert "Failed: ['pilot']" in result.stdout
    assert run_batch.call_args.kwargs == {
        "default_failure_status": "draft_failed",
        "record_failures": False,
    }
