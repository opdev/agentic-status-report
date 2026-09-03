"""Pre-group collector payloads for small-model drafter prompts."""

from __future__ import annotations

from typing import Any


def _epic_bucket(epic_key: str | None, project: str) -> str:
    if epic_key:
        return str(epic_key)
    return f"__no_epic__:{project}"


def summarize_payload_for_openai(payload: dict[str, Any]) -> dict[str, Any]:
    """Collapse raw Jira/PR/commit lists into epic-oriented groups."""
    groups: dict[str, dict[str, Any]] = {}

    def ensure_group(bucket: str, *, project: str, epic_key: str | None, epic_name: str | None) -> dict[str, Any]:
        if bucket not in groups:
            groups[bucket] = {
                "project": project,
                "epic_key": epic_key,
                "epic_name": epic_name,
                "jira_issues": [],
                "pull_requests": [],
                "commits": [],
            }
        return groups[bucket]

    issue_by_key: dict[str, dict[str, Any]] = {}
    for issue in payload.get("jira_issues") or []:
        key = issue.get("key")
        if not key:
            continue
        issue_by_key[str(key)] = issue
        project = str(issue.get("project") or "unknown")
        epic_key = issue.get("epic_key")
        epic_name = issue.get("epic_name")
        bucket = _epic_bucket(str(epic_key) if epic_key else None, project)
        group = ensure_group(
            bucket,
            project=project,
            epic_key=str(epic_key) if epic_key else None,
            epic_name=str(epic_name) if epic_name else None,
        )
        group["jira_issues"].append(
            {
                "key": key,
                "summary": issue.get("summary"),
                "status": issue.get("status"),
                "is_assignee": issue.get("is_assignee"),
            }
        )

    def assign_pr_or_commit(
        item: dict[str, Any],
        *,
        field: str,
        title_key: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        linked = [str(key) for key in (item.get("linked_issue_keys") or []) if key]
        target_buckets: set[str] = set()
        for key in linked:
            issue = issue_by_key.get(key)
            if issue:
                project = str(issue.get("project") or "unknown")
                epic_key = issue.get("epic_key")
                target_buckets.add(_epic_bucket(str(epic_key) if epic_key else None, project))
        if not target_buckets:
            if groups:
                if len(groups) == 1:
                    target_buckets = set(groups.keys())
                else:
                    with_issues = [bucket for bucket, group in groups.items() if group["jira_issues"]]
                    if len(with_issues) == 1:
                        target_buckets = {with_issues[0]}
            if not target_buckets:
                repo = str(item.get("repo") or "")
                project = repo.split("/")[-1] if "/" in repo else "unknown"
                target_buckets.add(_epic_bucket(None, project))

        row = {title_key: item.get(title_key), "url": item.get("url"), **(extra or {})}
        for bucket in target_buckets:
            group = groups.get(bucket)
            if group is None:
                project = bucket.split(":", 1)[-1] if bucket.startswith("__no_epic__:") else "unknown"
                epic_key = None if bucket.startswith("__no_epic__:") else bucket
                group = ensure_group(bucket, project=project, epic_key=epic_key, epic_name=None)
            group[field].append(row)

    for pr in payload.get("pull_requests") or []:
        assign_pr_or_commit(
            pr,
            field="pull_requests",
            title_key="title",
            extra={"state": pr.get("state"), "merged_at": pr.get("merged_at")},
        )

    for commit in payload.get("commits") or []:
        assign_pr_or_commit(
            commit,
            field="commits",
            title_key="summary",
            extra={"committed_at": commit.get("committed_at")},
        )

    previous_epics = [
        {
            "epic_key": entry.get("epic_key"),
            "epic_name": entry.get("epic_name"),
            "week_ending": entry.get("week_ending"),
            "state": entry.get("state"),
        }
        for entry in payload.get("previous_entries") or []
        if entry.get("epic_key")
    ]

    return {
        "person": payload.get("person"),
        "week_start": payload.get("week_start"),
        "week_end": payload.get("week_end"),
        "epic_groups": list(groups.values()),
        "previous_epics": previous_epics,
    }
