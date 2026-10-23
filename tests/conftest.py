"""Shared fixtures for the test suite."""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import gitutil
import pytest
from fastapi.testclient import TestClient

from curio_app.books.store import BookStore
from curio_app.config import Settings
from curio_app.db import connect
from curio_app.llm.fake import FakeLLMClient
from curio_app.main import create_app
from curio_app.vault.git import GitRepo
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriter

TEST_SECRET = "sk-test-not-real-0000"

VAULT_PROFILE_TEXT = """\
# Knowledge profile

- Knows: analog power stages (buck, flyback) and bench measurements.
- Knows: small Python automation scripts at home.
- Learning: digital control loops and firmware basics.
- Uses: a home lab bench with a variac and an oscilloscope.
- Wants: to size magnetics and plan EMI filters with more confidence.
"""


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


@dataclass(frozen=True)
class VaultEnv:
    """Test environment: git-backed vault clone, fake LLM and migrated database."""

    settings: Settings
    repos: gitutil.RepoSet
    fake: FakeLLMClient
    db_path: Path
    lock_path: Path


def seed_vault(repos: gitutil.RepoSet) -> None:
    """Seed the app clone with the knowledge profile and an empty questions dir."""
    (repos.app / "Learning" / "Questions").mkdir(parents=True)
    (repos.app / "Learning" / "Knowledge-Profile.md").write_text(
        VAULT_PROFILE_TEXT, encoding="utf-8"
    )
    (repos.app / "Learning" / "Questions" / ".gitkeep").write_text("", encoding="utf-8")
    gitutil.run_git("add", ".", cwd=repos.app)
    gitutil.run_git("commit", "-m", "curio: seed vault", cwd=repos.app)
    gitutil.run_git("push", "origin", "HEAD:main", cwd=repos.app)


def make_vault_env(tmp_path: Path) -> VaultEnv:
    """Build a seeded vault environment for generation and inbox tests."""
    repos = gitutil.make_repos(tmp_path)
    seed_vault(repos)
    settings = Settings(
        data_path=tmp_path / "data",
        vault_path=repos.app,
        llm_provider="fake",
        anthropic_api_key=TEST_SECRET,
    )
    db_path = gitutil.make_db(settings.db_path)
    return VaultEnv(
        settings=settings,
        repos=repos,
        fake=FakeLLMClient(),
        db_path=db_path,
        lock_path=tmp_path / "data" / "vault.lock",
    )


@pytest.fixture
def vault_env(tmp_path: Path) -> VaultEnv:
    return make_vault_env(tmp_path)


@pytest.fixture
def vault_client(vault_env: VaultEnv) -> TestClient:
    """TestClient for the app pointed at a seeded git vault, fake LLM injected."""
    return TestClient(create_app(vault_env.settings, llm=vault_env.fake))


@pytest.fixture
def db_conn(vault_env: VaultEnv) -> Iterator[sqlite3.Connection]:
    conn = connect(vault_env.db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def vault_store(vault_env: VaultEnv) -> VaultStore:
    return VaultStore(
        vault_env.settings.vault_path,
        vault_env.settings.vault_questions_dir,
        vault_env.settings.vault_profile_path,
    )


@pytest.fixture
def vault_writer(vault_env: VaultEnv) -> VaultWriter:
    return VaultWriter(
        GitRepo(vault_env.repos.app, branch=gitutil.BRANCH),
        vault_env.repos.app,
        vault_env.db_path,
        vault_env.lock_path,
    )


@pytest.fixture
def book_store(vault_env: VaultEnv, db_conn: sqlite3.Connection) -> BookStore:
    return BookStore(db_conn, vault_env.settings.books_path)
