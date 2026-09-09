from __future__ import annotations

from status.config import Settings


def test_jira_auth_email_prefers_api_email() -> None:
    settings = Settings(
        _env_file=None,
        JIRA_API_EMAIL="api@redhat.com",
        JIRA_EMAIL="legacy@redhat.com",
    )
    assert settings.jira_auth_email == "api@redhat.com"


def test_jira_auth_email_falls_back_to_legacy_alias() -> None:
    settings = Settings(_env_file=None, JIRA_EMAIL="legacy@redhat.com")
    assert settings.jira_auth_email == "legacy@redhat.com"
