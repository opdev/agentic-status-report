from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from status.skills.llm_backends import (
    DrafterBackend,
    backend_config_error,
    load_drafter_skill_text,
    openai_chat_completions_url,
)


def test_backend_config_error_openai_requires_endpoint_fields() -> None:
    with patch("status.skills.llm_backends.get_settings") as mock_settings:
        settings = MagicMock()
        settings.openai_api_key = "key"
        settings.openai_base_url = None
        settings.openai_model = "model"
        mock_settings.return_value = settings
        assert backend_config_error(DrafterBackend.OPENAI) == "OPENAI_BASE_URL not set"


def test_load_drafter_skill_text_reads_local_skill() -> None:
    text = load_drafter_skill_text()
    assert "weekly-status-drafter" in text.lower() or "entries" in text


def test_drafter_backend_parse_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown drafter backend"):
        DrafterBackend.parse("anthropic")


def test_openai_chat_completions_url_appends_v1_path() -> None:
    base = "http://example.com/agentic-workflow/model-route"
    assert openai_chat_completions_url(base) == (
        "http://example.com/agentic-workflow/model-route/v1/chat/completions"
    )
    assert openai_chat_completions_url(base + "/v1") == (
        "http://example.com/agentic-workflow/model-route/v1/chat/completions"
    )
