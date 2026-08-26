"""Skill upload, versioning, and invocation."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel

from status.skills.json_output import JsonOutputError, parse_json_model

if TYPE_CHECKING:
    import anthropic

log = logging.getLogger(__name__)

CODE_EXECUTION_TOOL = {"type": "code_execution_20250825", "name": "code_execution"}
MAX_PAUSE_RESUMES = 5
FOLLOW_UP_TOKEN_CAP = 16384


def _import_anthropic():
    import anthropic as anthropic_module

    try:
        from anthropic.lib import files_from_dir
    except ImportError as exc:
        raise SkillError(
            "anthropic>=0.49 is required for Skills API support. "
            "Run: pip install -U 'anthropic>=0.49'"
        ) from exc
    return anthropic_module, files_from_dir


@dataclass(frozen=True)
class SkillRef:
    skill_id: str
    version: str = "latest"
    type: Literal["custom", "anthropic"] = "custom"

    def as_container_entry(self) -> dict[str, str]:
        return {"type": self.type, "skill_id": self.skill_id, "version": self.version}

    @property
    def prompt_version(self) -> str:
        return f"{self.skill_id}@{self.version}"


class SkillClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-5",
        max_tokens: int = 8000,
    ) -> None:
        anthropic_module, _ = _import_anthropic()
        self._anthropic = anthropic_module
        self._client = anthropic_module.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def _skills_api(self):
        return getattr(self._client, "skills", None) or self._client.beta.skills

    def upload(self, skill_dir: Path, display_name: str | None = None) -> str:
        _, files_from_dir = _import_anthropic()
        kwargs: dict[str, Any] = {"files": files_from_dir(str(skill_dir))}
        if display_name:
            kwargs["display_name"] = display_name
        skill = self._skills_api().create(**kwargs)
        log.info("created skill %s from %s", skill.id, skill_dir)
        return skill.id

    def publish_version(self, skill_id: str, skill_dir: Path) -> str:
        _, files_from_dir = _import_anthropic()
        version = self._skills_api().versions.create(
            skill_id=skill_id,
            files=files_from_dir(str(skill_dir)),
        )
        label = _skill_version_label(version)
        log.info("published %s version %s", skill_id, label)
        return label

    def list_custom(self) -> list[Any]:
        return list(self._skills_api().list(source="custom"))

    def invoke_json(
        self,
        skill: SkillRef,
        payload: dict[str, Any],
        instruction: str,
        schema: type[BaseModel],
        *,
        code_execution: bool = True,
    ) -> BaseModel:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"{instruction}\n\n<payload>\n{json.dumps(payload)}\n</payload>",
            }
        ]

        container: dict[str, Any] = {"skills": [skill.as_container_entry()]}
        response = self._create(messages, container, code_execution=code_execution)
        response = self._resume_to_completion(messages, response, skill, code_execution=code_execution)

        try:
            return self._parse(response, schema, skill)
        except SkillError as exc:
            if not _should_json_follow_up(str(exc)):
                raise
            return self._json_follow_up(messages, response, skill, schema)

    def _json_follow_up(
        self,
        messages: list[dict[str, Any]],
        response,
        skill: SkillRef,
        schema: type[BaseModel],
    ) -> BaseModel:
        log.warning("%s returned no text; requesting JSON-only follow-up", skill.skill_id)
        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Reply with ONLY the complete final JSON object as plain text in a text block. "
                    "Do not run code and do not include any preamble, analysis, or markdown fences. "
                    "If your prior reply was truncated, return the full JSON object from the start."
                ),
            }
        )
        follow_up_tokens = min(max(self._max_tokens, 8192), FOLLOW_UP_TOKEN_CAP)
        container = {"id": response.container.id, "skills": [skill.as_container_entry()]}

        for code_execution in (False, True):
            try:
                follow_up = self._create(
                    messages,
                    container,
                    code_execution=code_execution,
                    max_tokens=follow_up_tokens,
                )
            except self._anthropic.BadRequestError as exc:
                if not code_execution and "code execution" in str(exc).lower():
                    log.warning("%s follow-up requires code execution; retrying", skill.skill_id)
                    continue
                raise SkillError(f"{skill.skill_id} follow-up rejected: {exc}") from exc

            follow_up = self._resume_to_completion(
                messages,
                follow_up,
                skill,
                code_execution=code_execution,
                max_tokens=follow_up_tokens,
            )
            try:
                return self._parse(follow_up, schema, skill)
            except SkillError as exc:
                if "no text output" not in str(exc):
                    raise
                log.warning(
                    "%s follow-up still had no text (code_execution=%s); stop_reason=%s",
                    skill.skill_id,
                    code_execution,
                    follow_up.stop_reason,
                )

        raise SkillError(f"{skill.skill_id} returned no text output after JSON follow-up")

    def _resume_to_completion(
        self,
        messages: list[dict[str, Any]],
        response,
        skill: SkillRef,
        *,
        code_execution: bool = True,
        max_tokens: int | None = None,
    ):
        for _ in range(MAX_PAUSE_RESUMES):
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
            response = self._create(
                messages,
                {"id": response.container.id, "skills": [skill.as_container_entry()]},
                code_execution=code_execution,
                max_tokens=max_tokens,
            )
        else:
            raise SkillError(f"{skill.skill_id} did not settle after {MAX_PAUSE_RESUMES} resumes")
        return response

    def _create(
        self,
        messages: list[dict[str, Any]],
        container: dict[str, Any],
        *,
        code_execution: bool = True,
        max_tokens: int | None = None,
    ):
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "container": container,
            "messages": messages,
        }
        if code_execution:
            kwargs["tools"] = [CODE_EXECUTION_TOOL]
        try:
            return self._client.beta.messages.create(**kwargs)
        except self._anthropic.BadRequestError as exc:
            if "skill" in str(exc).lower():
                raise SkillError(f"skill rejected: {exc}") from exc
            raise

    def _parse(self, response, schema: type[BaseModel], skill: SkillRef) -> BaseModel:
        text = "\n".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()

        if not text:
            block_types = [getattr(block, "type", None) for block in response.content]
            log.warning(
                "%s returned no text output; blocks=%s stop_reason=%s",
                skill.skill_id,
                block_types,
                response.stop_reason,
            )
            raise SkillError(f"{skill.skill_id} returned no text output")

        try:
            return parse_json_model(text, schema, source=skill.skill_id)
        except JsonOutputError as exc:
            raise SkillError(str(exc)) from exc


class SkillError(RuntimeError):
    """Raised when a skill returns something we refuse to persist."""


def _should_json_follow_up(message: str) -> bool:
    return any(
        phrase in message
        for phrase in (
            "no text output",
            "non-JSON output",
            "failed validation",
        )
    )


def _skill_version_label(version: Any) -> str:
    """Return a pin-able version string across Anthropic SDK response shapes."""
    for attr in ("version", "id"):
        value = getattr(version, attr, None)
        if value:
            return str(value)
    if hasattr(version, "model_dump"):
        data = version.model_dump()
        label = data.get("version") or data.get("id")
        if label:
            return str(label)
    return str(version)
