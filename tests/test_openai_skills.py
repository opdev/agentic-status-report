from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel

from status.skills.openai_skills import (
    OpenAISkillRef,
    OpenAISkillsClient,
    _extract_output_text,
    _structured_output_schema,
    skill_dir_to_zip_bytes,
)


class ExampleOutput(BaseModel):
    name: str
    note: str | None = None
    tags: list[str] = []


def test_skill_dir_to_zip_has_single_top_level_folder(tmp_path: Path) -> None:
    skill_dir = tmp_path / "weekly-status-drafter"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: test\n---\n", encoding="utf-8")

    data = skill_dir_to_zip_bytes(skill_dir)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
    assert names == ["weekly-status-drafter/SKILL.md"]


def test_extract_output_text_prefers_output_text_field() -> None:
    payload = {"output_text": '{"person": "yoza"}'}
    assert _extract_output_text(payload) == '{"person": "yoza"}'


def test_extract_output_text_reads_message_blocks() -> None:
    payload = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "hello"}],
            }
        ]
    }
    assert _extract_output_text(payload) == "hello"


def test_structured_output_schema_is_strict_and_requires_nullable_fields() -> None:
    schema = _structured_output_schema(ExampleOutput)

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["name", "note", "tags"]
    assert "default" not in schema["properties"]["note"]
    assert "default" not in schema["properties"]["tags"]


def test_invoke_json_mounts_pinned_skill_and_requests_structured_output() -> None:
    response = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"name":"weekly","note":null,"tags":[]}',
                    }
                ],
            }
        ],
    }
    client = OpenAISkillsClient("test-key", model="gpt-test")

    with patch("status.skills.openai_skills.request_json", return_value=response) as request:
        result = client.invoke_json(
            OpenAISkillRef("skill_123", "7"),
            {"week": "2026-09-18"},
            "Use the weekly skill. Return JSON.",
            ExampleOutput,
            max_output_tokens=1234,
        )

    assert result == ExampleOutput(name="weekly", note=None, tags=[])
    body = request.call_args.kwargs["body"]
    assert body["model"] == "gpt-test"
    assert body["store"] is False
    assert body["max_output_tokens"] == 1234
    assert body["tools"][0]["environment"]["skills"] == [
        {"type": "skill_reference", "skill_id": "skill_123", "version": 7}
    ]
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
