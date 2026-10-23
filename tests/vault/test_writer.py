"""Tests for VaultWriter: git protocol, locking, and vault_drafts handling."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gitutil
import pytest

from curio_app import db
from curio_app.vault.git import GitRepo, VaultPushRejected
from curio_app.vault.writer import FileChange, VaultWriteError, VaultWriter, WriteResult

_DIRTY_MESSAGE = (
    "The vault checkout has uncommitted changes. Clean it up on the server, then retry."
)
_CONFLICT_MESSAGE = "The vault changed remotely in a way that conflicts with this save."
_QUESTION_PATH = "Learning/Questions/q-demo.md"


@dataclass(frozen=True)
class App:
    """Resolved writer fixture around one temporary repository topology."""

    repos: gitutil.RepoSet
    repo: GitRepo
    writer: VaultWriter
    db_path: Path


@pytest.fixture
def app(tmp_path: Path) -> App:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH)
    return build_app(tmp_path, setup, repo)


def build_app(tmp_path: Path, setup: gitutil.RepoSet, repo: GitRepo) -> App:
    db_path = gitutil.make_db(tmp_path / "data" / "app.db")
    writer = VaultWriter(
        repo=repo,
        root=setup.app,
        db_path=db_path,
        lock_path=tmp_path / "locks" / "vault.lock",
    )
    return App(repos=setup, repo=repo, writer=writer, db_path=db_path)


@dataclass(frozen=True)
class Race:
    """A change the other device commits and pushes at an interception point."""

    b_path: Path
    rel_path: str
    content: str


class RaceAtCommitRepo(GitRepo):
    """Commits normally, then lets the other device land its change before the push."""

    def __init__(self, *, race: Race, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._race = race
        self._raced = False

    def commit_paths(self, paths: Sequence[str], message: str) -> str | None:
        commit = super().commit_paths(paths, message)
        if not self._raced:
            self._raced = True
            gitutil.commit_and_push(self._race.b_path, self._race.rel_path, self._race.content)
        return commit


class RaceAtPushRepo(GitRepo):
    """First push: the other device lands a non-conflicting change, then push is rejected."""

    def __init__(self, *, race: Race, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._race = race
        self._raced = False

    def push(self) -> None:
        if not self._raced:
            self._raced = True
            gitutil.commit_and_push(self._race.b_path, self._race.rel_path, self._race.content)
            raise VaultPushRejected("The vault rejected the push.", "rejected: simulated")
        super().push()


def read_draft_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = db.connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM vault_drafts ORDER BY created_at, question_id"
        ).fetchall()
    finally:
        conn.close()


async def test_happy_path(app: App) -> None:
    change = FileChange(
        question_id="q-demo",
        path=_QUESTION_PATH,
        content="# Demo\n\nSome invented text.\n",
    )

    result = await app.writer.write([change], "curio: add question q-demo")

    assert result.commit == app.repo.head()
    assert result.commit == gitutil.head_of(app.repos.remote)
    assert result.pushed
    assert app.repo.is_clean()
    assert (app.repos.app / _QUESTION_PATH).read_text(encoding="utf-8") == change.content
    identity = gitutil.run_git("log", "-1", "--format=%an %ae %cn %ce", cwd=app.repos.app)
    assert identity == "curio curio@localhost curio curio@localhost"
    subject = gitutil.run_git("log", "-1", "--format=%s", cwd=app.repos.app)
    assert subject == "curio: add question q-demo"
    (app.repos.app / "unrelated.md").write_text("scratch\n", encoding="utf-8")
    changed = gitutil.run_git("show", "--name-only", "--format=", "HEAD", cwd=app.repos.app)
    assert changed.splitlines() == [_QUESTION_PATH]
    assert not read_draft_rows(app.db_path)
    assert app.writer.list_drafts() == []


async def test_unchanged_content_is_noop(app: App) -> None:
    gitutil.commit_and_push(
        app.repos.app, _QUESTION_PATH, "# Demo\n", message="curio: add question q-demo"
    )
    seed_head = gitutil.head_of(app.repos.app)

    result = await app.writer.write(
        [FileChange(question_id="q-demo", path=_QUESTION_PATH, content="# Demo\n")],
        "curio: redo q-demo",
    )

    assert result == WriteResult(commit=None, pushed=False)
    assert gitutil.head_of(app.repos.app) == seed_head
    assert gitutil.head_of(app.repos.remote) == seed_head
    assert not read_draft_rows(app.db_path)
    assert len(gitutil.run_git("log", "--format=%s", cwd=app.repos.app).splitlines()) == 2


async def test_remote_ahead_rebases_and_pushes(app: App) -> None:
    gitutil.commit_and_push(app.repos.other, "Notes/Other.md", "## Other\n", message="device b")

    result = await app.writer.write(
        [FileChange(question_id="q-ahead", path="Learning/Questions/q-ahead.md", content="# X\n")],
        "curio: add question q-ahead",
    )

    assert result.commit == app.repo.head()
    assert result.commit == gitutil.head_of(app.repos.remote)
    assert result.pushed
    assert app.repo.is_clean()
    subjects = gitutil.run_git("log", "--format=%s", cwd=app.repos.app).splitlines()
    assert "curio: add question q-ahead" in subjects
    assert "device b" in subjects
    assert (app.repos.app / "Notes/Other.md").read_text(encoding="utf-8") == "## Other\n"
    assert not read_draft_rows(app.db_path)


async def test_conflict_stores_draft_and_leaves_repo_clean(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    race = Race(b_path=setup.other, rel_path=_QUESTION_PATH, content="B version\n")
    repo = RaceAtCommitRepo(race=race, path=setup.app, branch=gitutil.BRANCH)
    app_obj = build_app(tmp_path, setup, repo)
    before = gitutil.head_of(setup.app)
    change = FileChange(
        question_id="q-demo", path=_QUESTION_PATH, content="A version from the app\n"
    )

    with pytest.raises(VaultWriteError) as error:
        await app_obj.writer.write([change], "curio: gut answer q-demo")

    assert str(error.value) == _CONFLICT_MESSAGE
    assert error.value.detail
    assert app_obj.repo.is_clean()
    assert gitutil.head_of(setup.app) == before
    assert not (setup.app / ".git" / "rebase-merge").exists()
    assert not (setup.app / ".git" / "rebase-apply").exists()
    assert gitutil.head_of(setup.remote) == gitutil.head_of(setup.other)
    rows = read_draft_rows(app_obj.db_path)
    assert len(rows) == 1
    assert rows[0]["question_id"] == change.question_id
    assert rows[0]["path"] == change.path
    assert rows[0]["content"] == change.content
    assert rows[0]["commit_message"] == "curio: gut answer q-demo"
    assert rows[0]["error"] == _CONFLICT_MESSAGE
    created_at = datetime.fromisoformat(rows[0]["created_at"])
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() == UTC.utcoffset(created_at)
    drafts = app_obj.writer.list_drafts()
    assert [draft.question_id for draft in drafts] == [change.question_id]
    assert drafts[0].path == change.path
    assert drafts[0].error == _CONFLICT_MESSAGE


async def test_dirty_tree_stores_draft_and_keeps_file(app: App) -> None:
    before = gitutil.head_of(app.repos.app)
    (app.repos.app / "scratch.md").write_text("user scratch\n", encoding="utf-8")
    change = FileChange(
        question_id="q-dirty", path="Learning/Questions/q-dirty.md", content="# Dirty\n"
    )

    with pytest.raises(VaultWriteError) as error:
        await app.writer.write([change], "curio: add question q-dirty")

    assert str(error.value) == _DIRTY_MESSAGE
    assert error.value.detail == ""
    assert (app.repos.app / "scratch.md").read_text(encoding="utf-8") == "user scratch\n"
    assert gitutil.head_of(app.repos.app) == before
    rows = read_draft_rows(app.db_path)
    assert len(rows) == 1
    assert rows[0]["content"] == "# Dirty\n"


async def test_push_rejected_once_then_recovers(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    race = Race(b_path=setup.other, rel_path="Notes/Other.md", content="## Other\n")
    repo = RaceAtPushRepo(race=race, path=setup.app, branch=gitutil.BRANCH)
    app_obj = build_app(tmp_path, setup, repo)

    result = await app_obj.writer.write(
        [FileChange(question_id="q-race", path="Learning/Questions/q-race.md", content="# X\n")],
        "curio: add question q-race",
    )

    assert result.commit == app_obj.repo.head()
    assert result.commit == gitutil.head_of(setup.remote)
    assert result.pushed
    assert app_obj.repo.is_clean()
    assert (setup.app / "Notes/Other.md").read_text(encoding="utf-8") == "## Other\n"
    assert (setup.app / "Learning/Questions/q-race.md").read_text(encoding="utf-8") == "# X\n"
    assert not read_draft_rows(app_obj.db_path)


async def test_retry_drafts_commits_and_clears_table(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    race = Race(b_path=setup.other, rel_path=_QUESTION_PATH, content="B version\n")
    repo = RaceAtCommitRepo(race=race, path=setup.app, branch=gitutil.BRANCH)
    app_obj = build_app(tmp_path, setup, repo)
    change = FileChange(question_id="q-demo", path=_QUESTION_PATH, content="A version\n")
    with pytest.raises(VaultWriteError):
        await app_obj.writer.write([change], "curio: gut answer q-demo")
    assert read_draft_rows(app_obj.db_path)
    gitutil.pull_rebase(setup.app)

    result = await app_obj.writer.retry_drafts()

    assert result.commit == app_obj.repo.head()
    assert result.commit == gitutil.head_of(setup.remote)
    assert result.pushed
    assert (setup.app / _QUESTION_PATH).read_text(encoding="utf-8") == "A version\n"
    assert read_draft_rows(app_obj.db_path) == []
    subject = gitutil.run_git("log", "-1", "--format=%s", cwd=setup.app)
    assert subject == "curio: retry saving 1 file(s)"
    assert app_obj.writer.list_drafts() == []


async def test_retry_drafts_without_rows_is_noop(app: App) -> None:
    assert await app.writer.retry_drafts() == WriteResult(commit=None, pushed=False)
    assert app.repo.is_clean()


def test_file_change_rejects_escaping_paths() -> None:
    for bad_path in ("../x.md", "/abs.md", "a/../../x.md", "."):
        with pytest.raises(VaultWriteError):
            FileChange(question_id="q-x", path=bad_path, content="x")
