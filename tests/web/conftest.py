"""Fixtures for the web route tests."""

from collections.abc import Iterator

import pytest
from conftest import VaultEnv
from fastapi.testclient import TestClient

from curio_app.main import create_app


@pytest.fixture
def vault_client(vault_env: VaultEnv) -> Iterator[TestClient]:
    """TestClient wired to the seeded vault with the fake LLM injected.

    The context manager keeps one event loop (and portal) for the whole test, so
    background tasks such as assistant streaming turns survive between requests.
    """
    with TestClient(create_app(vault_env.settings, llm=vault_env.fake)) as client:
        yield client
