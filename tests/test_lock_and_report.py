from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from status.cli import app
from status.skills.schemas import SynthesisOutput


def test_lock_and_report_calls_synthesis_with_persistence_and_delivery() -> None:
    week = date(2026, 9, 18)
    synthesis = SynthesisOutput(
        week_ending=week.isoformat(),
        markdown="# Sep 18, 2026\n",
    )

    with (
        patch("status.collectors.payload.resolve_week_ending", return_value=week),
        patch("status.cli.get_session") as session_context,
        patch(
            "status.db.confirm.expire_unconfirmed_participations",
            return_value=2,
        ) as expire,
        patch("status.cli.synthesize_report", return_value=synthesis) as synthesize,
    ):
        session = MagicMock()
        session_context.return_value.__enter__.return_value = session
        result = CliRunner().invoke(
            app,
            [
                "run-week",
                "lock-and-report",
                "--week",
                "2026-09-18",
                "--persist",
                "--deliver",
            ],
        )

    assert result.exit_code == 0
    expire.assert_called_once_with(session, week)
    synthesize.assert_called_once_with(
        week,
        dry_run=False,
        persist=True,
        deliver=True,
        output_path=Path("status-2026-09-18.md"),
    )


def test_lock_and_report_dry_run_skips_expiration_and_side_effects() -> None:
    week = date(2026, 9, 18)
    synthesis = SynthesisOutput(week_ending=week.isoformat(), markdown="# preview\n")

    with (
        patch("status.collectors.payload.resolve_week_ending", return_value=week),
        patch("status.cli.get_session") as session_context,
        patch("status.db.confirm.expire_unconfirmed_participations") as expire,
        patch("status.cli.synthesize_report", return_value=synthesis) as synthesize,
    ):
        result = CliRunner().invoke(
            app,
            ["run-week", "lock-and-report", "--week", "2026-09-18", "--dry-run"],
        )

    assert result.exit_code == 0
    session_context.assert_not_called()
    expire.assert_not_called()
    synthesize.assert_called_once_with(
        week,
        dry_run=True,
        persist=False,
        deliver=False,
        output_path=None,
    )
