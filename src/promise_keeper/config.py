"""Environment-backed application configuration."""

import os

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    """Validated model API settings."""

    model_config = ConfigDict(frozen=True)

    openai_api_key: str = Field(min_length=1)
    openai_base_url: str = "https://api.aptget.nl/v1"
    openai_model: str = "qwen3.8-27b"
    openai_vision_model: str = "qwen3.8-27b-vision"
    openai_embedding_model: str = "qwen3-embeddings"

    # Slack transport settings (optional for offline testing, required for Slack adapter)
    slack_bot_token: str | None = None
    slack_app_token: str | None = None

    # Application settings
    database_path: str = "promise_keeper.db"
    default_timezone: str = "UTC"


def load_settings(require_model: bool = True) -> Settings:
    """Load and validate application settings."""
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key and not require_model:
        openai_key = "slack-transport-mode"

    values = {
        "openai_api_key": openai_key,
        "openai_base_url": os.environ.get("OPENAI_BASE_URL"),
        "openai_model": os.environ.get("OPENAI_MODEL"),
        "openai_vision_model": os.environ.get("OPENAI_VISION_MODEL"),
        "openai_embedding_model": os.environ.get("OPENAI_EMBEDDING_MODEL"),
        "slack_bot_token": os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("BOT_OAUTH_TOKEN"),
        "slack_app_token": os.environ.get("SLACK_APP_TOKEN") or os.environ.get("BOT_APP_TOKEN"),
        "database_path": os.environ.get("DATABASE_PATH"),
        "default_timezone": os.environ.get("APP_TIMEZONE") or os.environ.get("DEFAULT_TIMEZONE"),
    }
    return Settings(**{key: value for key, value in values.items() if value is not None})
