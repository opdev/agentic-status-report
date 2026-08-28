"""Resolve person identifiers for collectors."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from status.db.models import Person


@dataclass(frozen=True)
class PersonContext:
    person_id: str
    display_name: str
    jira_email: str | None
    github_login: str | None


def resolve_person(
    person_id: str,
    session: Session | None = None,
    *,
    jira_email: str | None = None,
    github_login: str | None = None,
) -> PersonContext:
    if session is not None:
        row = session.get(Person, person_id)
        if row is not None:
            return PersonContext(
                person_id=row.person_id,
                display_name=row.display_name,
                jira_email=jira_email,
                github_login=github_login or row.github_login,
            )

    return PersonContext(
        person_id=person_id,
        display_name=person_id,
        jira_email=jira_email,
        github_login=github_login or person_id,
    )
