import pytest
from pydantic import ValidationError

from promise_keeper.config import load_settings


def test_load_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_VISION_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_EMBEDDING_MODEL", raising=False)

    settings = load_settings()

    assert settings.openai_base_url == "https://api.aptget.nl/v1"
    assert settings.openai_model == "qwen3.8-27b"
    assert settings.openai_vision_model == "qwen3.8-27b-vision"
    assert settings.openai_embedding_model == "qwen3-embeddings"


def test_load_settings_custom_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "custom-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "custom-model")
    monkeypatch.setenv("OPENAI_VISION_MODEL", "custom-vision")
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "custom-embeddings")

    settings = load_settings()

    assert settings.openai_api_key == "custom-key"
    assert settings.openai_base_url == "https://example.com/v1"
    assert settings.openai_model == "custom-model"
    assert settings.openai_vision_model == "custom-vision"
    assert settings.openai_embedding_model == "custom-embeddings"


def test_load_settings_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        load_settings()

    assert exc_info.value.errors()[0]["loc"] == ("openai_api_key",)
