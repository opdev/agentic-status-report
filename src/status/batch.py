"""Batch operations for weekly automation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

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


def run_batch_operation(
    week_ending: date,
    operation: Callable[[Session, Person, date], Any],
    operation_name: str,
) -> list[BatchResult]:
    """
    Run an operation for each eligible person with error isolation.
    One person's failure does not abort the batch.
    """
    results: list[BatchResult] = []

    with get_session() as session:
        persons = get_eligible_persons(session)

        if not persons:
            log.warning(f"No eligible persons found for {operation_name}")
            return results

        log.info(
            f"Running {operation_name} for {len(persons)} persons: {[p.person_id for p in persons]}"
        )

        for person in persons:
            try:
                result = operation(session, person, week_ending)
                results.append(
                    BatchResult(
                        person_id=person.person_id,
                        success=True,
                        result=result,
                    )
                )
                log.info(f"{operation_name} succeeded for {person.person_id}")
            except Exception as exc:
                results.append(
                    BatchResult(
                        person_id=person.person_id,
                        success=False,
                        error=str(exc),
                    )
                )
                log.error(f"{operation_name} failed for {person.person_id}: {exc}")

                # Mark participation as send_failed
                try:
                    _mark_send_failed(session, person.person_id, week_ending)
                except Exception as inner_exc:
                    log.error(
                        f"Failed to mark send_failed for {person.person_id}: {inner_exc}"
                    )

    return results


def _mark_send_failed(session: Session, person_id: str, week_ending: date) -> None:
    """Set participation.status = 'send_failed' for this person/week."""
    row = session.get(Participation, (person_id, week_ending))
    if row is None:
        row = Participation(
            person_id=person_id,
            week_ending=week_ending,
            status="send_failed",
        )
        session.add(row)
    else:
        row.status = "send_failed"
    session.commit()
