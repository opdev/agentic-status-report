"""Persist synthesizer output and audit chain."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from status.db.models import ReportEntry, ReportRun, StatusEntry
from status.skills.schemas import SynthesisOutput


def entry_citation_key(entry: StatusEntry) -> str:
    epic = entry.epic_key or "unticketed"
    return f"{entry.person_id}:{epic}"


def build_citation_index(entries: list[StatusEntry]) -> dict[str, StatusEntry]:
    return {entry_citation_key(entry): entry for entry in entries}


def resolve_cited_entries(
    citations: list[str],
    index: dict[str, StatusEntry],
    confirmed_entries: list[StatusEntry],
) -> list[StatusEntry]:
    """Map skill citations (person:epic or entry UUID strings) to ledger rows."""
    by_id = {str(entry.entry_id): entry for entry in confirmed_entries}
    resolved: list[StatusEntry] = []
    seen: set[UUID] = set()

    for citation in citations:
        entry: StatusEntry | None = None
        if citation in index:
            entry = index[citation]
        elif citation in by_id:
            entry = by_id[citation]
        if entry is None or entry.entry_id in seen:
            continue
        seen.add(entry.entry_id)
        resolved.append(entry)

    return resolved


def supersede_previous_runs(session: Session, week_ending: date) -> None:
    session.execute(
        update(ReportRun)
        .where(
            ReportRun.week_ending == week_ending,
            ReportRun.superseded.is_(False),
        )
        .values(superseded=True)
    )


def persist_report_run(
    session: Session,
    week_ending: date,
    output: SynthesisOutput,
    *,
    prompt_version: str,
    model: str,
    confirmed_entries: list[StatusEntry],
    output_uri: str | None = None,
    delivered: bool = False,
) -> ReportRun:
    supersede_previous_runs(session, week_ending)
    run = ReportRun(
        week_ending=week_ending,
        prompt_version=prompt_version,
        model=model,
        output_uri=output_uri,
        delivered_at=datetime.now(timezone.utc) if delivered else None,
    )
    session.add(run)
    session.flush()

    index = build_citation_index(confirmed_entries)
    cited = resolve_cited_entries(output.entries_cited, index, confirmed_entries)
    if not cited and confirmed_entries:
        cited = confirmed_entries

    for entry in cited:
        session.add(
            ReportEntry(
                run_id=run.run_id,
                entry_id=entry.entry_id,
                section=None,
            )
        )

    session.flush()
    return run


def latest_report_run(session: Session, week_ending: date) -> ReportRun | None:
    stmt = (
        select(ReportRun)
        .where(
            ReportRun.week_ending == week_ending,
            ReportRun.superseded.is_(False),
        )
        .order_by(ReportRun.generated_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()
