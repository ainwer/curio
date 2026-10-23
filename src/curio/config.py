"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["anthropic", "openai_compatible", "fake"]
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """Runtime configuration; all env names match .env.example."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    vault_path: Path = Path("/vault")
    data_path: Path = Path("/data")
    vault_ssh_key_file: Path = Path("/run/secrets/vault_deploy_key")
    vault_questions_dir: str = "Learning/Questions"
    vault_profile_path: str = "Learning/Knowledge-Profile.md"
    vault_git_branch: str = "main"

    llm_provider: ProviderName = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    llm_tutor_model: str = ""  # empty -> llm_model
    llm_effort_generation: EffortLevel = "medium"
    llm_effort_tutor: EffortLevel = "low"
    llm_fallback: Literal["default", "off"] = "default"
    anthropic_api_key: SecretStr | None = None
    llm_base_url: str = ""
    llm_api_key: SecretStr | None = None

    converter_url: str = "http://converter:8001"
    max_upload_mb: int = 200
    log_level: str = "INFO"

    @field_validator("anthropic_api_key", "llm_api_key", mode="before")
    @classmethod
    def _empty_secret_to_none(cls, value: str | None) -> str | None:
        """Treat an empty environment value as not configured."""
        if value is not None and not value.strip():
            return None
        return value

    @property
    def db_path(self) -> Path:
        return self.data_path / "app.db"

    @property
    def books_path(self) -> Path:
        return self.data_path / "books"

    @property
    def tutor_model(self) -> str:
        return self.llm_tutor_model or self.llm_model

    def secret_values(self) -> list[str]:
        """Non-empty secret strings, used for log redaction."""
        values: list[str] = []
        for secret in (self.anthropic_api_key, self.llm_api_key):
            value = secret.get_secret_value() if secret is not None else ""
            if value:
                values.append(value)
        return values


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, cached."""
    return Settings()
