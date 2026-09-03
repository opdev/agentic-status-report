from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DraftEntry(BaseModel):
    project: str
    epic_key: str | None = None
    epic_name: str | None = None
    state: Literal["shipped", "progressing", "slipped", "blocked", "quiet"]
    outcome: str
    evidence: list[str] = Field(min_length=1)
    evidence_labels: dict[str, str] = Field(default_factory=dict)
    blocker: str | None = None
    ask: str | None = None
    confidence: Literal["high", "medium", "low"]
    needs_human: bool = False
    why_flagged: str | None = None

    @field_validator("state", mode="before")
    @classmethod
    def _normalize_state(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower().replace("-", " ").replace("_", " ")
        aliases = {
            "in progress": "progressing",
            "done": "shipped",
            "complete": "shipped",
            "completed": "shipped",
            "shipped": "shipped",
            "progressing": "progressing",
            "slipped": "slipped",
            "blocked": "blocked",
            "quiet": "quiet",
        }
        return aliases.get(normalized, value)


class DraftOutput(BaseModel):
    person: str
    week_ending: str
    entries: list[DraftEntry] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    unticketed_prompt: str = ""

    @field_validator("entries", "flags", mode="before")
    @classmethod
    def _coerce_lists(cls, value: list[DraftEntry] | list[str] | None) -> list[DraftEntry] | list[str]:
        if value is None:
            return []
        return value

    @field_validator("unticketed_prompt", mode="before")
    @classmethod
    def _coerce_unticketed_prompt(cls, value: str | None) -> str:
        return "" if value is None else value


class SynthesisParticipation(BaseModel):
    person_id: str
    display_name: str
    status: Literal["confirmed", "expired", "on_leave", "sent", "send_failed"]


class SynthesisFlag(BaseModel):
    message: str
    person_id: str | None = None
    epic_key: str | None = None


class SynthesisEntry(BaseModel):
    person_id: str
    display_name: str
    project: str
    epic_key: str | None = None
    epic_name: str | None = None
    state: Literal["shipped", "progressing", "slipped", "blocked", "quiet"]
    outcome: str
    blocker: str | None = None
    ask: str | None = None
    evidence: list[str] = Field(default_factory=list)


class SynthesisInput(BaseModel):
    week_ending: str
    entries: list[SynthesisEntry] = Field(default_factory=list)
    participation: list[SynthesisParticipation] = Field(default_factory=list)
    flags: list[SynthesisFlag] = Field(default_factory=list)


class SynthesisOutput(BaseModel):
    week_ending: str
    markdown: str
    sections_used: list[str] = Field(default_factory=list)
    entries_cited: list[str] = Field(default_factory=list)
    non_responders: list[str] = Field(default_factory=list)
    asks: list[str] = Field(default_factory=list)
