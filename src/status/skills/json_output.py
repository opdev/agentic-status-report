"""Parse JSON model output from LLM text responses."""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class JsonOutputError(RuntimeError):
    """Raised when model output cannot be parsed or validated."""


def extract_json_text(response_text: str) -> str:
    return FENCE_RE.sub("", response_text.strip()).strip()


def extract_json_object(text: str) -> str:
    """Pull the first complete JSON object out of prose + JSON model output."""
    cleaned = extract_json_text(text)
    start = cleaned.find("{")
    if start < 0:
        return cleaned

    decoder = json.JSONDecoder()
    try:
        _, end = decoder.raw_decode(cleaned, start)
        return cleaned[start:end]
    except json.JSONDecodeError:
        return cleaned


def parse_json_model(response_text: str, schema: type[BaseModel], *, source: str = "model") -> BaseModel:
    cleaned = extract_json_object(response_text)
    if not cleaned:
        raise JsonOutputError(f"{source} returned empty text")

    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.error("unparseable output from %s: %.500s", source, cleaned)
        raise JsonOutputError(f"{source} returned non-JSON output") from exc

    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        log.error("schema violation from %s: %s", source, exc)
        raise JsonOutputError(f"{source} output failed validation") from exc
