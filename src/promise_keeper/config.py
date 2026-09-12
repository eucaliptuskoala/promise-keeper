"""Environment-backed model, Slack and application settings."""

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    openai_api_key: str | None = Field(default=None, min_length=1, repr=False)
    openai_base_url: str = "https://api.aptget.nl/v1"
    openai_model: str = Field(default="qwen3.8-27b", min_length=1)
    openai_vision_model: str = "qwen3.8-27b-vision"
    openai_embedding_model: str = "qwen3-embeddings"
    model_timeout_seconds: float = Field(default=20, gt=0, le=60)
    slack_bot_token: str | None = Field(default=None, min_length=1, repr=False)
    slack_app_token: str | None = Field(default=None, min_length=1, repr=False)
    enabled_channels: tuple[str, ...] = ()
    database_path: str = Field(default="promise_keeper.db", min_length=1)
    default_timezone: str = "UTC"

    @field_validator("enabled_channels")
    @classmethod
    def validate_enabled_channels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if "*" in value and value != ("*",):
            raise ValueError("Use * alone for all public channels, or provide channel IDs")
        return value

    @field_validator("default_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        if value != "UTC":
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError:
                raise ValueError(
                    "Unknown timezone or missing timezone data; configure UTC or install tzdata",
                ) from None
        return value


def load_settings(require_model: bool = True) -> Settings:
    values = {
        "openai_api_key": os.environ.get("OPENAI_API_KEY"),
        "openai_base_url": os.environ.get("OPENAI_BASE_URL"),
        "openai_model": os.environ.get("OPENAI_MODEL"),
        "openai_vision_model": os.environ.get("OPENAI_VISION_MODEL"),
        "openai_embedding_model": os.environ.get("OPENAI_EMBEDDING_MODEL"),
        "model_timeout_seconds": os.environ.get("MODEL_TIMEOUT_SECONDS"),
        "slack_bot_token": os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("BOT_OAUTH_TOKEN"),
        "slack_app_token": os.environ.get("SLACK_APP_TOKEN") or os.environ.get("BOT_APP_TOKEN"),
        "database_path": os.environ.get("DATABASE_PATH"),
        "default_timezone": os.environ.get("APP_TIMEZONE") or os.environ.get("DEFAULT_TIMEZONE"),
        "enabled_channels": tuple(
            channel.strip()
            for channel in os.environ.get("SLACK_ENABLED_CHANNELS", "").split(",")
            if channel.strip()
        ),
    }
    settings = Settings(**{key: value for key, value in values.items() if value is not None})
    if require_model and settings.openai_api_key is None:
        raise ValueError("OPENAI_API_KEY is required for model interpretation")
    return settings
