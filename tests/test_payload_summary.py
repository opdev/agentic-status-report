from __future__ import annotations

from status.skills.payload_summary import summarize_payload_for_openai


def test_summarize_payload_for_openai_groups_by_epic() -> None:
    payload = {
        "person": "yoza",
        "week_start": "2026-08-22",
        "week_end": "2026-08-28",
        "jira_issues": [
            {
                "key": "EET-5519",
                "summary": "Agentic Weekly Status Pipeline",
                "status": "In Progress",
                "project": "EET",
                "epic_key": None,
                "epic_name": None,
                "is_assignee": True,
            }
        ],
        "pull_requests": [
            {
                "url": "https://github.com/opdev/agentic-status-report/pull/18",
                "title": "Improve drafter linked outcomes",
                "repo": "opdev/agentic-status-report",
                "state": "merged",
                "linked_issue_keys": [],
            }
        ],
        "commits": [],
        "previous_entries": [
            {
                "epic_key": "EET-5493",
                "epic_name": "OpenShift Cluster Management Bot",
                "week_ending": "2026-08-14",
            }
        ],
    }

    summary = summarize_payload_for_openai(payload)
    assert summary["person"] == "yoza"
    assert len(summary["epic_groups"]) == 1
    group = summary["epic_groups"][0]
    assert group["epic_key"] is None
    assert group["jira_issues"][0]["key"] == "EET-5519"
    assert group["pull_requests"][0]["title"] == "Improve drafter linked outcomes"
    assert summary["previous_epics"][0]["epic_key"] == "EET-5493"
