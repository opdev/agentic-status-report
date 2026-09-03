"""Draft generation from collector payloads."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from status.config import get_settings
from status.db import get_session
from status.db.draft import persist_draft_output
from status.skills.llm_backends import (
    DrafterBackend,
    LlmBackendError,
    backend_config_error,
    invoke_drafter_backend,
    prompt_version_for,
)
from status.skills.evidence import (
    build_evidence_labels,
    enrich_outcome_links,
    filter_evidence_to_payload,
    issue_summary_index,
    jira_keys_from_evidence,
    payload_jira_keys,
    pr_url_index,
)
from status.skills.schemas import DraftEntry, DraftOutput

log = logging.getLogger(__name__)

DRAFTER_INSTRUCTION = (
    "Use the weekly-status-drafter skill on the payload below. "
    "Return only the JSON output defined in the skill as plain text in your reply. "
    "Use markdown links [text](url) in outcome fields for Jira and GitHub evidence."
)


class DraftPersistError(RuntimeError):
    """Raised when draft rows cannot be written to the ledger."""


@dataclass(frozen=True)
class DraftRunResult:
    draft: DraftOutput
    prompt_version: str
    persisted_entry_ids: list[str]
    superseded_count: int


def load_fixture(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"Fixture not found: {resolved}\n"
            "Create one with:\n"
            "  status collect --person <id> --week YYYY-MM-DD "
            "--save-fixture fixtures/payload.json\n"
            "Or omit --fixture and pass --person and --week to collect live."
        )
    return json.loads(resolved.read_text(encoding="utf-8"))


def week_ending_from_payload(payload: dict[str, Any]) -> date:
    raw = payload.get("week_end") or payload.get("week_ending")
    if not raw:
        raise ValueError("collector payload missing week_end")
    return date.fromisoformat(str(raw))


def _empty_draft(payload: dict[str, Any], flags: list[str]) -> DraftOutput:
    week_ending = week_ending_from_payload(payload).isoformat()
    return DraftOutput.model_validate(
        {
            "person": payload.get("person", "unknown"),
            "week_ending": week_ending,
            "entries": [],
            "flags": flags,
            "unticketed_prompt": "",
        }
    )


def _normalize_draft(draft: DraftOutput, payload: dict[str, Any]) -> DraftOutput:
    week_ending = week_ending_from_payload(payload).isoformat()
    if draft.week_ending == week_ending and draft.person == payload.get("person"):
        return draft
    return draft.model_copy(
        update={
            "person": payload.get("person", draft.person),
            "week_ending": week_ending,
        }
    )


def attach_evidence_labels(draft: DraftOutput, payload: dict[str, Any]) -> DraftOutput:
    """Fill per-key link phrases from collector Jira summaries."""
    summaries = issue_summary_index(list(payload.get("jira_issues") or []))
    if not summaries:
        return draft

    enriched: list[DraftEntry] = []
    for entry in draft.entries:
        labels = build_evidence_labels(
            entry.evidence,
            summaries,
            epic_key=entry.epic_key,
            epic_name=entry.epic_name,
        )
        if labels == entry.evidence_labels:
            enriched.append(entry)
        else:
            enriched.append(entry.model_copy(update={"evidence_labels": labels}))
    return draft.model_copy(update={"entries": enriched})


def _detect_gap_flags(
    draft: DraftOutput,
    payload: dict[str, Any],
) -> tuple[list[str], str]:
    """Deterministic gap flags small models often omit."""
    flags = list(draft.flags)
    unticketed_prompt = draft.unticketed_prompt.strip()

    previous_epics: dict[str, str] = {}
    for entry in payload.get("previous_entries") or []:
        epic_key = entry.get("epic_key")
        if epic_key:
            previous_epics[str(epic_key)] = str(entry.get("epic_name") or epic_key)

    active_epics: set[str] = set()
    cited_pr_urls: set[str] = set()
    for entry in draft.entries:
        if entry.epic_key:
            active_epics.add(entry.epic_key)
        for key in jira_keys_from_evidence(entry.evidence):
            active_epics.add(key)
        for item in entry.evidence:
            if item.startswith("http"):
                cited_pr_urls.add(item)

    for epic_key, epic_name in previous_epics.items():
        if epic_key not in active_epics:
            message = f"{epic_key} ({epic_name}) had no activity this week."
            if message not in flags:
                flags.append(message)

    unticketed_prs: list[dict[str, Any]] = []
    for pr in payload.get("pull_requests") or []:
        if pr.get("linked_issue_keys"):
            continue
        url = pr.get("url")
        title = str(pr.get("title") or "untitled PR")
        message = f'PR "{title}" has no linked Jira ticket.'
        if message not in flags:
            flags.append(message)
        if url and url not in cited_pr_urls:
            unticketed_prs.append(pr)

    if not unticketed_prompt and unticketed_prs:
        titles = [str(pr.get("title") or "a pull request") for pr in unticketed_prs[:2]]
        if len(titles) == 1:
            unticketed_prompt = (
                f'Should "{titles[0]}" be tied to a Jira ticket for this week\'s status?'
            )
        else:
            unticketed_prompt = (
                f'Nothing here links "{titles[0]}" or "{titles[1]}" to a ticket — '
                "which epic should they roll up to?"
            )

    flags = list(dict.fromkeys(flags))
    return flags, unticketed_prompt


def postprocess_draft(draft: DraftOutput, payload: dict[str, Any]) -> DraftOutput:
    """Filter evidence to this person's payload and enrich outcomes with Jira links."""
    settings = get_settings()
    jira_base_url = settings.jira_base_url or "https://redhat.atlassian.net"
    allowed_keys = payload_jira_keys(payload)
    pr_titles = pr_url_index(list(payload.get("pull_requests") or []))
    processed: list[DraftEntry] = []

    for entry in draft.entries:
        evidence = filter_evidence_to_payload(
            entry.evidence,
            allowed_jira_keys=allowed_keys,
            pull_requests=list(payload.get("pull_requests") or []),
            commits=list(payload.get("commits") or []),
        )
        labels = {
            key: label
            for key, label in entry.evidence_labels.items()
            if key in allowed_keys
        }
        outcome = enrich_outcome_links(
            entry.outcome,
            entry.state,
            evidence,
            labels,
            pr_titles,
            jira_base_url=jira_base_url,
        )

        updates: dict[str, Any] = {
            "evidence": evidence,
            "evidence_labels": labels,
            "outcome": outcome,
        }
        if entry.epic_key is None and not entry.needs_human:
            updates["needs_human"] = True
            if not entry.why_flagged:
                updates["why_flagged"] = (
                    "No epic on this work — which initiative should it roll up to?"
                )
        if len(evidence) < len(entry.evidence):
            updates["needs_human"] = True
            if not entry.why_flagged:
                updates["why_flagged"] = (
                    "Some cited tickets were removed because they are not assigned to you "
                    "or were not in this week's collector data — OK to keep?"
                )
        processed.append(entry.model_copy(update=updates))

    flags, unticketed_prompt = _detect_gap_flags(
        draft.model_copy(update={"entries": processed}),
        payload,
    )
    return draft.model_copy(
        update={
            "entries": processed,
            "flags": flags,
            "unticketed_prompt": unticketed_prompt,
        }
    )


def run_drafter(
    payload: dict[str, Any],
    *,
    dry_run: bool = False,
    backend: DrafterBackend | None = None,
) -> DraftOutput:
    settings = get_settings()
    if dry_run:
        return _empty_draft(payload, flags=["dry-run: no skill invocation"])

    resolved = backend or DrafterBackend.parse(settings.drafter_llm_backend)
    config_error = backend_config_error(resolved)
    if config_error:
        return _empty_draft(payload, flags=[config_error])

    instruction = DRAFTER_INSTRUCTION
    regeneration_notes = str(payload.get("regeneration_notes") or "").strip()
    if regeneration_notes:
        instruction = (
            f"{instruction}\n\nThe user asked to regenerate this draft with this guidance: "
            f"{regeneration_notes}"
        )

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            result = invoke_drafter_backend(
                resolved,
                payload,
                DraftOutput,
                instruction=instruction,
            )
            assert isinstance(result, DraftOutput)
            normalized = _normalize_draft(result, payload)
            labeled = attach_evidence_labels(normalized, payload)
            return postprocess_draft(labeled, payload)
        except LlmBackendError as exc:
            last_error = exc
            log.warning("drafter attempt %s failed (%s): %s", attempt + 1, resolved.value, exc)

    flag = (
        f"drafter failed after retry ({resolved.value}): {last_error}"
        if last_error
        else f"drafter failed after retry ({resolved.value})"
    )
    return _empty_draft(payload, flags=[flag])


def _persist_with_retry(
    draft: DraftOutput,
    *,
    prompt_version: str,
    collection_errors: list[str],
    max_attempts: int = 8,
    retry_delay_seconds: float = 3.0,
) -> tuple[list[str], int]:
    """Write draft rows after skill invocation; reconnect if port-forward dropped."""
    last_error: OperationalError | None = None
    for attempt in range(max_attempts):
        try:
            with get_session() as session:
                rows, superseded_count = persist_draft_output(
                    session,
                    draft,
                    prompt_version=prompt_version,
                    collection_errors=collection_errors,
                )
                entry_ids = [str(row.entry_id) for row in rows]
                return entry_ids, superseded_count
        except OperationalError as exc:
            last_error = exc
            if attempt >= max_attempts - 1:
                break
            log.warning(
                "persist attempt %s/%s failed (%s); retrying in %ss "
                "(keep port-forward running or restart scripts/port-forward-db.sh)",
                attempt + 1,
                max_attempts,
                exc,
                retry_delay_seconds,
            )
            time.sleep(retry_delay_seconds)
        except IntegrityError as exc:
            raise DraftPersistError(
                "Could not save draft entries — a current row already exists for one "
                "or more epics this week. Re-run `status draft`; if this persists, "
                "check for stale is_current rows in Postgres. "
                f"Details: {exc.orig}"
            ) from exc

    assert last_error is not None
    raise last_error


def draft_and_persist(
    payload: dict[str, Any],
    *,
    dry_run: bool = False,
    persist: bool = True,
    session: Session | None = None,
) -> DraftRunResult:
    """Run the drafter skill, then persist — DB is touched only at the end."""
    settings = get_settings()
    draft = run_drafter(payload, dry_run=dry_run)

    if dry_run:
        prompt_version = "dry-run"
    else:
        try:
            backend = DrafterBackend.parse(settings.drafter_llm_backend)
            prompt_version = prompt_version_for(backend)
        except ValueError:
            prompt_version = settings.drafter_llm_backend

    if dry_run or not persist:
        return DraftRunResult(
            draft=draft,
            prompt_version=prompt_version,
            persisted_entry_ids=[],
            superseded_count=0,
        )

    collection_errors = list(payload.get("collection_errors") or [])
    if session is not None:
        try:
            rows, superseded_count = persist_draft_output(
                session,
                draft,
                prompt_version=prompt_version,
                collection_errors=collection_errors,
            )
        except IntegrityError as exc:
            raise DraftPersistError(
                "Could not save draft entries — a current row already exists for one "
                "or more epics this week. Re-run `status draft` after resolving the conflict."
            ) from exc
        persisted_entry_ids = [str(row.entry_id) for row in rows]
    else:
        persisted_entry_ids, superseded_count = _persist_with_retry(
            draft,
            prompt_version=prompt_version,
            collection_errors=collection_errors,
        )
    return DraftRunResult(
        draft=draft,
        prompt_version=prompt_version,
        persisted_entry_ids=persisted_entry_ids,
        superseded_count=superseded_count,
    )
