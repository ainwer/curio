"""Shared fixtures for the test suite."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from curio_app.config import Settings
from curio_app.main import create_app

TEST_SECRET = "sk-test-not-real-0000"


@pytest.fixture
def secret() -> str:
    return TEST_SECRET


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_path=tmp_path / "data",
        vault_path=tmp_path / "vault",
        llm_provider="fake",
        anthropic_api_key=TEST_SECRET,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
