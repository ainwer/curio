"""Fixtures for the web route tests."""

import pytest
from conftest import VaultEnv
from fastapi.testclient import TestClient

from curio_app.main import create_app


@pytest.fixture
def vault_client(vault_env: VaultEnv) -> TestClient:
    """TestClient wired to the seeded vault with the fake LLM injected."""
    return TestClient(create_app(vault_env.settings, llm=vault_env.fake))
