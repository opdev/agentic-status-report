"""Resolve person identifiers for collectors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from status.db.models import Person

REPO_ROOT = Path(__file__).resolve().parents[3]
ROSTER_PATH = REPO_ROOT / "fixtures" / "eet-persons.json"


@dataclass(frozen=True)
class PersonContext:
    person_id: str
    display_name: str
    jira_email: str | None
    github_login: str | None


@lru_cache
def _roster_jira_emails() -> dict[str, str]:
    """person_id and github_login -> jira_email from the EET roster fixture."""
    if not ROSTER_PATH.is_file():
        return {}
    rows = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    emails: dict[str, str] = {}
    for row in rows:
        email = row.get("jira_email")
        if not email:
            continue
        if row.get("person_id"):
            emails[str(row["person_id"])] = str(email)
        if row.get("github_login"):
            emails[str(row["github_login"])] = str(email)
    return emails


def roster_jira_email_addresses() -> list[str]:
    """Unique Jira emails from the EET roster fixture."""
    return sorted(set(_roster_jira_emails().values()))


def _roster_jira_email(person_id: str, github_login: str | None) -> str | None:
    roster = _roster_jira_emails()
    return roster.get(person_id) or (roster.get(github_login) if github_login else None)


def _resolve_jira_email(
    person_id: str,
    github_login: str | None,
    *,
    override: str | None,
    stored: str | None,
) -> str | None:
    return override or stored or _roster_jira_email(person_id, github_login)


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
            resolved_github = github_login or row.github_login
            return PersonContext(
                person_id=row.person_id,
                display_name=row.display_name,
                jira_email=_resolve_jira_email(
                    row.person_id,
                    resolved_github,
                    override=jira_email,
                    stored=row.jira_email,
                ),
                github_login=resolved_github,
            )

    resolved_github = github_login or person_id
    return PersonContext(
        person_id=person_id,
        display_name=person_id,
        jira_email=_resolve_jira_email(
            person_id,
            resolved_github,
            override=jira_email,
            stored=None,
        ),
        github_login=resolved_github,
    )
