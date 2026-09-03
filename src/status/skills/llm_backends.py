"""Pluggable LLM backends for the weekly-status drafter."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from enum import Enum
from typing import Any

from pydantic import BaseModel

from status.config import SKILLS_DIR, get_settings
from status.skills.client import SkillClient, SkillError, SkillRef
from status.skills.json_output import JsonOutputError, parse_json_model

from status.skills.payload_summary import summarize_payload_for_openai

log = logging.getLogger(__name__)

DRAFTER_SKILL_DIR = SKILLS_DIR / "weekly-status-drafter"

PROMPT_INSTRUCTION = (
    "Apply the weekly-status-drafter instructions to the collector payload below. "
    "Return only the JSON object defined in the skill as plain text in your reply. "
    "Use markdown links [text](url) in outcome fields for Jira and GitHub evidence."
)

OPENAI_DRAFTER_PROMPT = """\
You draft one engineer's weekly status from grouped Jira and GitHub activity.

Rules:
- One entry per epic in `epic_groups`. Tickets with no epic become one entry with `epic_key: null`.
- Group PRs and commits with their epic; cite every PR URL you mention in `evidence`.
- Write `outcome` in past tense, one or two sentences, with markdown links:
  `[summary phrase](https://redhat.atlassian.net/browse/KEY)` and `[PR title](pr-url)`.
- Set `needs_human: true` when `epic_key` is null or confidence is low.
- Add `flags` for epics in `previous_epics` with no activity this week.
- Add `flags` for PRs with no linked Jira ticket.
- End with a specific `unticketed_prompt` when PRs lack tickets.

Return only JSON (no markdown fences):
{
  "person": "string",
  "week_ending": "YYYY-MM-DD",
  "entries": [{
    "project": "string",
    "epic_key": "string | null",
    "epic_name": "string | null",
    "state": "shipped | progressing | slipped | blocked | quiet",
    "outcome": "one or two linked sentences",
    "evidence": ["JIRA-KEY", "https://github.com/org/repo/pull/N"],
    "blocker": "string | null",
    "ask": "string | null",
    "confidence": "high | medium | low",
    "needs_human": false,
    "why_flagged": "string | null"
  }],
  "flags": ["string"],
  "unticketed_prompt": "string"
}

Example entry outcome:
Working on [agentic weekly status pipeline](https://redhat.atlassian.net/browse/EET-5519);
merged [Improve drafter linked outcomes](https://github.com/opdev/agentic-status-report/pull/18).
"""


class DrafterBackend(str, Enum):
    SKILLS = "skills"
    MESSAGES = "messages"
    OPENAI = "openai"

    @classmethod
    def parse(cls, value: str) -> DrafterBackend:
        normalized = value.strip().lower()
        try:
            return cls(normalized)
        except ValueError as exc:
            supported = ", ".join(member.value for member in cls)
            raise ValueError(f"Unknown drafter backend {value!r}; use one of: {supported}") from exc


class LlmBackendError(RuntimeError):
    """Raised when an LLM backend cannot complete a drafter call."""


def load_drafter_skill_text() -> str:
    skill_path = DRAFTER_SKILL_DIR / "SKILL.md"
    if not skill_path.is_file():
        raise LlmBackendError(f"drafter skill file not found: {skill_path}")
    return skill_path.read_text(encoding="utf-8")


def backend_config_error(backend: DrafterBackend) -> str | None:
    settings = get_settings()
    if backend is DrafterBackend.SKILLS:
        if not settings.anthropic_api_key:
            return "ANTHROPIC_API_KEY not set"
        if not settings.drafter_skill_id:
            return "DRAFTER_SKILL_ID not set"
        return None
    if backend is DrafterBackend.MESSAGES:
        if not settings.anthropic_api_key:
            return "ANTHROPIC_API_KEY not set"
        return None
    if backend is DrafterBackend.OPENAI:
        if not settings.openai_api_key:
            return "OPENAI_API_KEY not set"
        if not settings.openai_base_url:
            return "OPENAI_BASE_URL not set"
        if not settings.openai_model:
            return "OPENAI_MODEL not set"
        return None
    return f"unsupported backend: {backend}"


def prompt_version_for(backend: DrafterBackend) -> str:
    settings = get_settings()
    if backend is DrafterBackend.SKILLS and settings.drafter_skill_id:
        return SkillRef(
            skill_id=settings.drafter_skill_id,
            version=settings.drafter_skill_version,
        ).prompt_version
    if backend is DrafterBackend.MESSAGES:
        return f"messages:{settings.claude_model}"
    if backend is DrafterBackend.OPENAI:
        return f"openai:{settings.openai_model}"
    return backend.value


def invoke_drafter_backend(
    backend: DrafterBackend,
    payload: dict[str, Any],
    schema: type[BaseModel],
    *,
    instruction: str | None = None,
) -> BaseModel:
    config_error = backend_config_error(backend)
    if config_error:
        raise LlmBackendError(config_error)

    if backend is DrafterBackend.SKILLS:
        return _invoke_skills_backend(payload, schema, instruction=instruction)
    if backend is DrafterBackend.MESSAGES:
        return _invoke_messages_backend(payload, schema, instruction=instruction)
    return _invoke_openai_backend(payload, schema, instruction=instruction)


def _invoke_skills_backend(
    payload: dict[str, Any],
    schema: type[BaseModel],
    *,
    instruction: str | None,
) -> BaseModel:
    settings = get_settings()
    assert settings.anthropic_api_key and settings.drafter_skill_id
    client = SkillClient(api_key=settings.anthropic_api_key, model=settings.claude_model)
    skill = SkillRef(
        skill_id=settings.drafter_skill_id,
        version=settings.drafter_skill_version,
    )
    try:
        result = client.invoke_json(
            skill,
            payload,
            instruction or PROMPT_INSTRUCTION,
            schema,
        )
    except SkillError as exc:
        raise LlmBackendError(str(exc)) from exc
    if not isinstance(result, schema):
        raise LlmBackendError(f"skills backend returned unexpected type: {type(result)}")
    return result


def _invoke_messages_backend(
    payload: dict[str, Any],
    schema: type[BaseModel],
    *,
    instruction: str | None,
) -> BaseModel:
    settings = get_settings()
    assert settings.anthropic_api_key
    skill_text = load_drafter_skill_text()
    user_text = json.dumps(payload, indent=2)
    system_text = f"{skill_text}\n\n{instruction or PROMPT_INSTRUCTION}"

    try:
        import anthropic
    except ImportError as exc:
        raise LlmBackendError("anthropic package is required for messages backend") from exc

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=settings.drafter_messages_max_tokens,
        system=system_text,
        messages=[{"role": "user", "content": user_text}],
    )
    text = _anthropic_text(response)
    try:
        return parse_json_model(text, schema, source="messages")
    except JsonOutputError as exc:
        raise LlmBackendError(str(exc)) from exc


def _openai_ssl_context() -> ssl.SSLContext | None:
    settings = get_settings()
    if settings.openai_ssl_verify:
        return None
    return ssl._create_unverified_context()


def openai_chat_completions_url(base_url: str) -> str:
    """Resolve an OpenAI-compatible chat completions URL from a flexible base URL."""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _invoke_openai_backend(
    payload: dict[str, Any],
    schema: type[BaseModel],
    *,
    instruction: str | None,
) -> BaseModel:
    settings = get_settings()
    assert settings.openai_api_key and settings.openai_base_url and settings.openai_model
    summarized = summarize_payload_for_openai(payload)
    user_text = json.dumps(summarized, indent=2)
    system_text = f"{OPENAI_DRAFTER_PROMPT}\n\n{instruction or PROMPT_INSTRUCTION}"
    url = openai_chat_completions_url(settings.openai_base_url)
    body = {
        "model": settings.openai_model,
        "max_tokens": settings.drafter_max_tokens,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=600,
            context=_openai_ssl_context(),
        ) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LlmBackendError(f"openai backend HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise LlmBackendError(f"openai backend request failed: {exc}") from exc

    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmBackendError(f"openai backend returned unexpected response: {raw!r}") from exc

    try:
        return parse_json_model(str(text), schema, source="openai")
    except JsonOutputError as exc:
        raise LlmBackendError(str(exc)) from exc


def _anthropic_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()
