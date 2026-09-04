from __future__ import annotations

from unittest.mock import patch

from status.collectors.jira import _discover_jira_auth_email, jira_auth_email
from status.config import Settings


def test_jira_auth_email_discovers_from_roster() -> None:
    _discover_jira_auth_email.cache_clear()
    settings = Settings(
        _env_file=None,
        JIRA_BASE_URL="https://redhat.atlassian.net",
        JIRA_API_TOKEN="token",
    )
    with patch("status.collectors.jira._probe_jira_auth", side_effect=[False, True]):
        with patch(
            "status.collectors.jira.roster_jira_email_addresses",
            return_value=["other@redhat.com", "yoza@redhat.com"],
        ):
            assert jira_auth_email(settings) == "yoza@redhat.com"
    _discover_jira_auth_email.cache_clear()
