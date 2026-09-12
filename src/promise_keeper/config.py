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


def load_settings() -> Settings:
    """Load and validate application settings."""
    values = {
        "openai_api_key": os.environ.get("OPENAI_API_KEY"),
        "openai_base_url": os.environ.get("OPENAI_BASE_URL"),
        "openai_model": os.environ.get("OPENAI_MODEL"),
        "openai_vision_model": os.environ.get("OPENAI_VISION_MODEL"),
        "openai_embedding_model": os.environ.get("OPENAI_EMBEDDING_MODEL"),
    }
    return Settings(**{key: value for key, value in values.items() if value is not None})
