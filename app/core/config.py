from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+asyncpg://support:support@localhost:5432/support"
    redis_url: str = "redis://localhost:6379/0"
    admin_api_key: SecretStr = SecretStr("")
    bot_api_key: SecretStr = SecretStr("")
    operator_keys: dict[str, str] = Field(default_factory=dict)
    bot_token: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    polza_ai_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-4.1-mini"
    embedding_model: str = "text-embedding-3-small"
    similarity_threshold: float = Field(default=0.35, ge=0, le=1)
    top_k: int = Field(default=5, ge=1, le=10)
    history_limit: int = Field(default=10, ge=1, le=20)
    backend_url: str = "http://localhost:8000"
    log_level: str = "INFO"


@lru_cache
def settings() -> Settings:
    return Settings()
