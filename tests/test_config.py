import pytest
from pydantic import ValidationError

from promise_keeper.config import load_settings


def test_load_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    settings = load_settings()

    assert settings.openai_base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert settings.openai_model == "gemini-3.6-flash"


def test_load_settings_custom_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "custom-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "custom-model")

    settings = load_settings()

    assert settings.openai_api_key == "custom-key"
    assert settings.openai_base_url == "https://example.com/v1"
    assert settings.openai_model == "custom-model"


def test_load_settings_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        load_settings()


def test_load_settings_slack_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("BOT_OAUTH_TOKEN", "xoxb-test")
    monkeypatch.setenv("BOT_APP_TOKEN", "xapp-test")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    settings = load_settings()
    assert settings.slack_bot_token == "xoxb-test"
    assert settings.slack_app_token == "xapp-test"
    assert settings.database_path == "promise_keeper.db"
    assert settings.default_timezone == "UTC"


def test_transport_configuration_does_not_invent_model_credentials(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = load_settings(require_model=False)
    assert settings.openai_api_key is None


def test_enabled_channels_are_explicit(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SLACK_ENABLED_CHANNELS", " C1, C2, ")
    assert load_settings().enabled_channels == ("C1", "C2")


def test_all_public_channels_are_explicit(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SLACK_ENABLED_CHANNELS", "*")
    assert load_settings().enabled_channels == ("*",)


def test_all_public_channels_cannot_mix_with_channel_ids(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SLACK_ENABLED_CHANNELS", "*,C1")
    with pytest.raises(ValidationError):
        load_settings()


def test_unknown_timezone_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("APP_TIMEZONE", "Unknown/Zone")
    with pytest.raises(ValidationError):
        load_settings()


def test_windows_timezone_data_is_available(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("APP_TIMEZONE", "Europe/Berlin")
    assert load_settings().default_timezone == "Europe/Berlin"


@pytest.mark.parametrize("timeout", ["0", "121", "not-a-number"])
def test_model_timeout_is_bounded(monkeypatch, timeout) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ValidationError):
        load_settings()


def test_model_timeout_accepts_valid_high_timeout(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "90")
    assert load_settings().model_timeout_seconds == 90.0
