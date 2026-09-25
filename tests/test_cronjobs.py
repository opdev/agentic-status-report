from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("filename", "schedule", "deadline"),
    [
        ("cronjob-collect-and-draft.yaml", "30 8 * * 5", 1_800),
        ("cronjob-send-drafts.yaml", "0 9 * * 5", 600),
        ("cronjob-nudge.yaml", "0 14 * * 5", 600),
        ("cronjob-lock-and-report.yaml", "0 9 * * 1", 1_800),
    ],
)
def test_cronjobs_use_eastern_wall_clock_time_and_deadlines(
    filename: str,
    schedule: str,
    deadline: int,
) -> None:
    manifest = (Path("deploy") / filename).read_text(encoding="utf-8")

    assert f'schedule: "{schedule}"' in manifest
    assert 'timeZone: "America/New_York"' in manifest
    assert f"activeDeadlineSeconds: {deadline}" in manifest


def test_lock_and_report_cronjob_persists_and_delivers() -> None:
    manifest = Path("deploy/cronjob-lock-and-report.yaml").read_text(encoding="utf-8")

    assert '"--deliver", "--persist"' in manifest
