from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from status.skills.client import SkillClient, SkillError, SkillRef, _skill_version_label
from status.skills.schemas import SynthesisOutput


class SkillVersionStub:
    """Older SDK shape: id only, no version field."""

    id = "skillver_01TEST"


class VersionCreateResponseStub:
    version = "1759178010641129"
    id = "skillver_01TEST"


def test_skill_version_label_prefers_version_field() -> None:
    assert _skill_version_label(VersionCreateResponseStub()) == "1759178010641129"


def test_skill_version_label_falls_back_to_id() -> None:
    assert _skill_version_label(SkillVersionStub()) == "skillver_01TEST"


def test_json_follow_up_retries_when_skills_require_code_execution() -> None:
    class FakeBadRequestError(Exception):
        pass

    skill = SkillRef(skill_id="skill_test")
    schema = SynthesisOutput
    bad_exc = FakeBadRequestError(
        "container: skills can only be used when a code execution tool is enabled"
    )

    first_response = MagicMock()
    first_response.content = [MagicMock(type="text", text='{"person_id":"yoza"}')]
    first_response.container.id = "container_1"
    first_response.stop_reason = "end_turn"

    follow_up_response = MagicMock()
    follow_up_response.content = [
        MagicMock(
            type="text",
            text=(
                '{"week_ending":"2026-09-04","markdown":"# Report","sections_used":[],'
                '"entries_cited":[],"non_responders":[],"asks":[]}'
            ),
        )
    ]
    follow_up_response.container.id = "container_1"
    follow_up_response.stop_reason = "end_turn"

    with patch("status.skills.client._import_anthropic", return_value=(MagicMock(BadRequestError=FakeBadRequestError), MagicMock())):
        client = SkillClient(api_key="test-key")
    client._anthropic.BadRequestError = FakeBadRequestError

    create_mock = MagicMock(side_effect=[bad_exc, follow_up_response])
    client._client = MagicMock()
    client._client.beta.messages.create = create_mock

    with patch.object(client, "_resume_to_completion", side_effect=lambda _m, r, _s, **_: r):
        result = client._json_follow_up([], first_response, skill, schema)

    assert isinstance(result, SynthesisOutput)
    assert create_mock.call_count == 2
    assert "tools" not in create_mock.call_args_list[0].kwargs
    assert create_mock.call_args_list[1].kwargs["tools"] is not None


def test_create_does_not_wrap_code_execution_bad_request_as_skill_error() -> None:
    class FakeBadRequestError(Exception):
        pass

    with patch("status.skills.client._import_anthropic", return_value=(MagicMock(BadRequestError=FakeBadRequestError), MagicMock())):
        client = SkillClient(api_key="test-key")
    client._anthropic.BadRequestError = FakeBadRequestError
    client._client = MagicMock()
    client._client.beta.messages.create.side_effect = FakeBadRequestError(
        "skills can only be used when a code execution tool is enabled"
    )

    with pytest.raises(FakeBadRequestError):
        client._create([], {"skills": [{"type": "custom", "skill_id": "x", "version": "latest"}]}, code_execution=False)
