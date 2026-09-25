"""Batch operations for weekly automation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from status.db import get_session
from status.db.models import Participation, Person
from status.db.repo import get_eligible_persons

log = logging.getLogger(__name__)


@dataclass
class BatchResult:
    """Result of a batch operation."""

    person_id: str
    success: bool
    error: str | None = None
    result: Any | None = None


class BatchStageError(RuntimeError):
    """An operation failure with a participation state suitable for persistence."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def run_batch_operation(
    week_ending: date,
    operation: Callable[[Session, Person, date], Any],
    operation_name: str,
    *,
    default_failure_status: str,
    record_failures: bool = True,
) -> list[BatchResult]:
    """
    Run an operation for each eligible person with error isolation.
    One person's failure does not abort the batch.
    """
    results: list[BatchResult] = []

    with get_session() as roster_session:
        person_ids = [person.person_id for person in get_eligible_persons(roster_session)]

    if not person_ids:
        log.warning("No eligible persons found for %s", operation_name)
        return results

    log.info("Running %s for %s persons: %s", operation_name, len(person_ids), person_ids)

    for person_id in person_ids:
        try:
            # Let the context manager roll back an operation failure before the
            # next person gets a fresh session and transaction.
            with get_session() as session:
                person = session.get(Person, person_id)
                if person is None:
                    raise BatchStageError(
                        default_failure_status,
                        f"person disappeared during {operation_name}: {person_id}",
                    )
                result = operation(session, person, week_ending)
            results.append(
                BatchResult(
                    person_id=person_id,
                    success=True,
                    result=result,
                )
            )
            log.info("%s succeeded for %s", operation_name, person_id)
        except Exception as exc:  # noqa: BLE001 - isolate each person's operation
            results.append(
                BatchResult(
                    person_id=person_id,
                    success=False,
                    error=str(exc),
                )
            )
            log.error("%s failed for %s: %s", operation_name, person_id, exc)

            if record_failures:
                failure_status = (
                    exc.status if isinstance(exc, BatchStageError) else default_failure_status
                )
                try:
                    with get_session() as failure_session:
                        mark_participation_failure(
                            failure_session,
                            person_id,
                            week_ending,
                            status=failure_status,
                            error=str(exc),
                        )
                except Exception as inner_exc:  # noqa: BLE001 - retain original failure
                    log.error(
                        "Failed to record %s for %s: %s",
                        failure_status,
                        person_id,
                        inner_exc,
                    )

    return results


def mark_participation_failure(
    session: Session,
    person_id: str,
    week_ending: date,
    *,
    status: str,
    error: str,
) -> None:
    """Record the failed workflow stage without committing another person's work."""
    row = session.get(Participation, (person_id, week_ending))
    if row is None:
        row = Participation(
            person_id=person_id,
            week_ending=week_ending,
            status=status,
        )
        session.add(row)
    else:
        row.status = status
    row.note = error[:2_000]
    session.flush()
