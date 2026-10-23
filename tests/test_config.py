"""Tests for Settings loading."""

import pytest

from lernsystem.config import Settings


def test_env_vars_load(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_EFFORT_GENERATION", "high")
    monkeypatch.setenv("LLM_TUTOR_MODEL", "some-tutor-model")
    monkeypatch.setenv("MAX_UPLOAD_MB", "150")

    settings = Settings()

    assert settings.data_path == tmp_path / "data"
    assert settings.db_path == tmp_path / "data" / "app.db"
    assert settings.books_path == tmp_path / "data" / "books"
    assert settings.vault_path == tmp_path / "vault"
    assert settings.llm_provider == "fake"
    assert settings.llm_effort_generation == "high"
    assert settings.max_upload_mb == 150


def test_empty_secret_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("LLM_API_KEY", "")

    settings = Settings()

    assert settings.anthropic_api_key is None
    assert settings.llm_api_key is None
    assert settings.secret_values() == []


def test_repr_and_str_do_not_leak_secret(secret: str) -> None:
    settings = Settings(anthropic_api_key=secret, llm_api_key=secret)

    assert secret not in repr(settings)
    assert secret not in str(settings)
    assert secret in settings.secret_values()


def test_tutor_model_fallback(secret: str) -> None:
    defaults = Settings(llm_model="main-model", llm_tutor_model="", anthropic_api_key=secret)
    override = Settings(llm_model="main-model", llm_tutor_model="tutor-model")

    assert defaults.tutor_model == "main-model"
    assert override.tutor_model == "tutor-model"
