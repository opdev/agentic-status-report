from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from status.cli import app
from status.skills.schemas import SynthesisOutput


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_lock_and_report_calls_synthesize_report_with_persist_and_deliver(
    runner: CliRunner,
) -> None:
    week = date(2026, 9, 11)
    synthesis = SynthesisOutput(
        week_ending=week.isoformat(),
        markdown="# Sep 11, 2026\n",
    )

    with patch("status.collectors.payload.resolve_week_ending", return_value=week):
        with patch("status.cli.get_session") as session_cm:
            session = MagicMock()
            session_cm.return_value.__enter__.return_value = session
            session_cm.return_value.__exit__.return_value = None
            with patch(
                "status.db.confirm.expire_unconfirmed_participations",
                return_value=2,
            ) as expire_mock:
                with patch("status.cli.synthesize_report", return_value=synthesis) as synth_mock:
                    result = runner.invoke(
                        app,
                        [
                            "run-week",
                            "lock-and-report",
                            "--week",
                            "2026-09-11",
                            "--persist",
                            "--deliver",
                        ],
                    )

    assert result.exit_code == 0
    expire_mock.assert_called_once_with(session, week)
    session.commit.assert_called_once()
    synth_mock.assert_called_once_with(
        week,
        dry_run=False,
        persist=True,
        deliver=True,
        output_path=Path("status-2026-09-11.md"),
    )


def test_lock_and_report_dry_run_skips_expire_and_persist(runner: CliRunner) -> None:
    week = date(2026, 9, 11)
    synthesis = SynthesisOutput(
        week_ending=week.isoformat(),
        markdown="# preview\n",
    )

    with patch("status.collectors.payload.resolve_week_ending", return_value=week):
        with patch("status.cli.get_session") as session_cm:
            with patch("status.db.confirm.expire_unconfirmed_participations") as expire_mock:
                with patch("status.cli.synthesize_report", return_value=synthesis) as synth_mock:
                    result = runner.invoke(
                        app,
                        ["run-week", "lock-and-report", "--week", "2026-09-11", "--dry-run"],
                    )

    assert result.exit_code == 0
    session_cm.assert_not_called()
    expire_mock.assert_not_called()
    synth_mock.assert_called_once_with(
        week,
        dry_run=True,
        persist=False,
        deliver=False,
        output_path=None,
    )
