"""Tests for the generation service: runs, suggestions and vault acceptance."""

from datetime import UTC, datetime

import gitutil
import pytest
from books.support import make_ready_book
from conftest import VaultEnv

from curio_app.books.store import BookStore
from curio_app.db import connect
from curio_app.llm.base import LLMUnavailable
from curio_app.services import generation
from curio_app.vault.format import parse_question
from curio_app.vault.git import GitRepo
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriteError, VaultWriter

NOW = datetime(2026, 10, 23, 8, 15, 0, tzinfo=UTC)
QUESTION_PREFIX = "Learning/Questions/"


class LLMUnavailableStub:
    """Stub that raises LLMUnavailable on every generate call."""

    def __init__(self, message: str) -> None:
        self._message = message

    async def generate(self, **_kwargs: object) -> object:
        raise LLMUnavailable(self._message)


class LLMRuntimeErrorStub:
    """Stub that raises an unexpected error on every generate call."""

    async def generate(self, **_kwargs: object) -> object:
        raise RuntimeError("boom")


@pytest.fixture
def vault(vault_env: VaultEnv) -> VaultStore:
    return VaultStore(
        vault_env.settings.vault_path,
        vault_env.settings.vault_questions_dir,
        vault_env.settings.vault_profile_path,
    )


@pytest.fixture
def writer(vault_env: VaultEnv) -> VaultWriter:
    return VaultWriter(
        GitRepo(vault_env.repos.app, branch=gitutil.BRANCH),
        vault_env.repos.app,
        vault_env.db_path,
        vault_env.lock_path,
    )


async def _topic_run(env: VaultEnv, vault: VaultStore, topic: str = "li-ion cells") -> int:
    conn = connect(env.db_path)
    try:
        run_id = generation.start_run(conn, "topic", topic)
    finally:
        conn.close()
    await generation.execute_run(
        run_id,
        db_path=env.db_path,
        llm=env.fake,
        vault=vault,
        books_path=env.settings.books_path,
    )
    return run_id


def _run(env: VaultEnv, run_id: int) -> generation.Run | None:
    conn = connect(env.db_path)
    try:
        return generation.get_run(conn, run_id)
    finally:
        conn.close()


def _latest(env: VaultEnv) -> generation.Run | None:
    conn = connect(env.db_path)
    try:
        return generation.latest_run(conn)
    finally:
        conn.close()


def _commit_count(env: VaultEnv) -> int:
    return int(gitutil.run_git("rev-list", "--count", "HEAD", cwd=env.repos.app))


# --- start_run validation -------------------------------------------------------


def test_start_run_validates_all_modes(db_conn, book_store: BookStore, vault_env: VaultEnv) -> None:
    make_ready_book(book_store, "Invented Book", "## 1 Start\n\nStart body.\n")
    ready = next(book for book in book_store.list() if book.state == "ready")
    chapter = book_store.chapters(ready.id)[0]

    assert generation.start_run(db_conn, "source", str(chapter.id)) >= 1
    with pytest.raises(generation.GenerationInputError):
        generation.start_run(db_conn, "source", "  ")
    with pytest.raises(generation.GenerationInputError):
        generation.start_run(db_conn, "source", "abc")
    with pytest.raises(generation.GenerationInputError):
        generation.start_run(db_conn, "source", "9999")

    assert generation.start_run(db_conn, "topic", " li-ion cells ") >= 1
    with pytest.raises(generation.GenerationInputError):
        generation.start_run(db_conn, "topic", "   ")
    with pytest.raises(generation.GenerationInputError):
        generation.start_run(db_conn, "topic", "x" * 201)

    assert generation.start_run(db_conn, "bridge", "") >= 1
    with pytest.raises(generation.GenerationInputError):
        generation.start_run(db_conn, "bridge", "y" * 201)
    with pytest.raises(generation.GenerationInputError):
        generation.start_run(db_conn, "nope", "x")


# --- execute_run ----------------------------------------------------------------


async def test_execute_run_success_stores_suggestions(
    vault_env: VaultEnv, vault: VaultStore, vault_writer: VaultWriter
) -> None:
    run_id = await _topic_run(vault_env, vault)
    run = _run(vault_env, run_id)

    assert run is not None and run.state == "done" and run.error is None
    assert run.mode == "topic" and run.input == "li-ion cells"
    assert len(run.suggestions) == 8
    for suggestion in run.suggestions:
        assert suggestion.state == "new"
        assert suggestion.question.endswith("?")
        assert suggestion.domain == "fake-domain"
        assert suggestion.concepts and suggestion.hook
        assert suggestion.source is None and suggestion.bridge_from is None
        assert suggestion.question_id is None


async def test_execute_run_passes_system_prompt_and_existing(
    vault_env: VaultEnv, vault: VaultStore, db_conn, vault_writer: VaultWriter
) -> None:
    # Write one question into the vault first; its title must be part of the
    # "existing" prompt section of the next run.
    conn = connect(vault_env.db_path)
    try:
        seed_run = generation.start_run(conn, "topic", "seed topic")
    finally:
        conn.close()
    await generation.execute_run(
        seed_run,
        db_path=vault_env.db_path,
        llm=vault_env.fake,
        vault=vault,
        books_path=vault_env.settings.books_path,
    )
    seed = _latest(vault_env)
    assert seed is not None
    await generation.accept(
        db_conn,
        [seed.suggestions[0].id],
        vault=vault,
        writer=vault_writer,
        now=NOW,
    )

    before = len(vault_env.fake.calls)
    await _topic_run(vault_env, vault, topic="soldering irons")
    call = vault_env.fake.calls[before]
    assert call["purpose"] == "generation"
    system = str(call["system"])
    assert "<knowledge_profile>" in system
    assert "analog power stages" in system
    message = call["messages"][-1]
    content = message["content"]
    text = content if isinstance(content, str) else "\n".join(block["text"] for block in content)
    assert "Soldering irons" in text or "soldering irons" in text
    seed_title = seed.suggestions[0].question
    assert seed_title in text


async def test_execute_run_source_mode_uses_chapter(
    vault_env: VaultEnv, vault: VaultStore, book_store: BookStore
) -> None:
    make_ready_book(book_store, "Invented Book", "## 1 Start\n\nInvented chapter body.\n")
    ready = next(book for book in book_store.list() if book.state == "ready")
    chapter = book_store.chapters(ready.id)[0]
    conn = connect(vault_env.db_path)
    try:
        run_id = generation.start_run(conn, "source", str(chapter.id))
    finally:
        conn.close()
    await generation.execute_run(
        run_id,
        db_path=vault_env.db_path,
        llm=vault_env.fake,
        vault=vault,
        books_path=vault_env.settings.books_path,
    )
    run = _run(vault_env, run_id)
    assert run is not None and run.state == "done"
    assert len(run.suggestions) == 8
    fake = vault_env.fake
    last_call = fake.calls[-1]
    assert isinstance(last_call["messages"], list)
    first_message = last_call["messages"][-1]
    assert "<chapter" in repr(first_message["content"])


async def test_execute_run_llm_failure_marks_run(vault_env: VaultEnv, vault: VaultStore) -> None:
    conn = connect(vault_env.db_path)
    try:
        run_id = generation.start_run(conn, "topic", "li-ion cells")
    finally:
        conn.close()
    stub = LLMUnavailableStub("Generation is not available right now.")
    await generation.execute_run(
        run_id,
        db_path=vault_env.db_path,
        llm=stub,  # type: ignore[arg-type]
        vault=vault,
        books_path=vault_env.settings.books_path,
    )
    run = _run(vault_env, run_id)
    assert run is not None and run.state == "failed"
    assert run.error == "Generation is not available right now."
    assert run.suggestions == []


async def test_execute_run_unexpected_error_is_generic(
    vault_env: VaultEnv, vault: VaultStore, caplog: pytest.LogCaptureFixture
) -> None:
    conn = connect(vault_env.db_path)
    try:
        run_id = generation.start_run(conn, "topic", "li-ion cells")
    finally:
        conn.close()
    await generation.execute_run(
        run_id,
        db_path=vault_env.db_path,
        llm=LLMRuntimeErrorStub(),  # type: ignore[arg-type]
        vault=vault,
        books_path=vault_env.settings.books_path,
    )
    run = _run(vault_env, run_id)
    assert run is not None and run.state == "failed"
    assert run.error == "Unexpected error."
    assert "RuntimeError" in caplog.text
    assert "boom" not in caplog.text


async def test_execute_run_bridge_requires_profile(vault_env: VaultEnv, vault: VaultStore) -> None:
    (vault_env.settings.vault_path / vault.profile_path).unlink()
    conn = connect(vault_env.db_path)
    try:
        run_id = generation.start_run(conn, "bridge", "wireless coils")
    finally:
        conn.close()
    await generation.execute_run(
        run_id,
        db_path=vault_env.db_path,
        llm=vault_env.fake,
        vault=vault,
        books_path=vault_env.settings.books_path,
    )
    run = _run(vault_env, run_id)
    assert run is not None and run.state == "failed"
    assert run.error == "No knowledge profile found at Learning/Knowledge-Profile.md."


# --- accept / dismiss -----------------------------------------------------------


async def test_accept_single_writes_question_and_commits(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, db_conn
) -> None:
    run_id = await _topic_run(vault_env, vault)
    run = _run(vault_env, run_id)
    assert run is not None
    suggestion = run.suggestions[0]
    base_remote = gitutil.head_of(vault_env.repos.remote)

    question_ids = await generation.accept(
        db_conn, [suggestion.id], vault=vault, writer=writer, now=NOW
    )

    after = _run(vault_env, run_id)
    assert after is not None
    expected = [first.question_id for first in after.suggestions[:1]]
    assert question_ids == expected
    qid = question_ids[0]
    path = vault_env.repos.app / QUESTION_PREFIX / f"{qid}.md"
    assert path.is_file()
    question = parse_question(path.read_text(encoding="utf-8"))
    assert question.status.value == "inbox"
    assert question.title == suggestion.question
    assert question.domain == suggestion.domain
    assert question.concepts == suggestion.concepts
    assert question.created == NOW
    assert question.preamble == "*Invented for tests.*"
    message = gitutil.run_git("log", "-1", "--format=%s", cwd=vault_env.repos.app)
    assert message == f"curio: add question {qid}"
    assert gitutil.head_of(vault_env.repos.remote) == gitutil.head_of(vault_env.repos.app)
    assert gitutil.head_of(vault_env.repos.remote) != base_remote
    updated = _run(vault_env, run_id)
    assert updated is not None
    assert updated.suggestions[0].state == "accepted"
    assert updated.suggestions[0].question_id == qid


async def test_accept_many_is_one_commit(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, db_conn
) -> None:
    run_id = await _topic_run(vault_env, vault)
    run = _run(vault_env, run_id)
    assert run is not None
    ids = [suggestion.id for suggestion in run.suggestions[:3]]
    base_commits = _commit_count(vault_env)

    question_ids = await generation.accept(db_conn, ids, vault=vault, writer=writer, now=NOW)

    assert len(question_ids) == 3
    assert len(set(question_ids)) == 3
    assert _commit_count(vault_env) == base_commits + 1
    message = gitutil.run_git("log", "-1", "--format=%s", cwd=vault_env.repos.app)
    assert message == "curio: add 3 questions"
    for qid in question_ids:
        assert (vault_env.repos.app / QUESTION_PREFIX / f"{qid}.md").is_file()
    updated = _run(vault_env, run_id)
    assert updated is not None
    assert all(suggestion.state == "accepted" for suggestion in updated.suggestions[:3])


async def test_accept_same_suggestion_twice_is_idempotent(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, db_conn
) -> None:
    run_id = await _topic_run(vault_env, vault)
    run = _run(vault_env, run_id)
    assert run is not None
    suggestion_id = run.suggestions[0].id
    first_ids = await generation.accept(
        db_conn, [suggestion_id], vault=vault, writer=writer, now=NOW
    )
    head_after_first = gitutil.head_of(vault_env.repos.remote)

    second_ids = await generation.accept(
        db_conn, [suggestion_id], vault=vault, writer=writer, now=NOW
    )

    assert second_ids == first_ids
    assert gitutil.head_of(vault_env.repos.remote) == head_after_first


async def test_accept_write_failure_stores_draft_and_keeps_new(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, db_conn
) -> None:
    run_id = await _topic_run(vault_env, vault)
    run = _run(vault_env, run_id)
    assert run is not None
    suggestion_id = run.suggestions[0].id
    stray = vault_env.repos.app / "Learning" / "stray-note.md"
    stray.write_text("invented stray content\n", encoding="utf-8")

    with pytest.raises(VaultWriteError):
        await generation.accept(db_conn, [suggestion_id], vault=vault, writer=writer, now=NOW)

    updated = _run(vault_env, run_id)
    assert updated is not None
    assert updated.suggestions[0].state == "new"
    conn = connect(vault_env.db_path)
    try:
        drafts = conn.execute("SELECT * FROM vault_drafts").fetchall()
    finally:
        conn.close()
    assert len(drafts) == 1

    stray.unlink()
    question_ids = await generation.accept(
        db_conn, [suggestion_id], vault=vault, writer=writer, now=NOW
    )
    assert question_ids
    assert (vault_env.repos.app / QUESTION_PREFIX / f"{question_ids[0]}.md").is_file()


async def test_accept_skips_dismissed_suggestions(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, db_conn
) -> None:
    run_id = await _topic_run(vault_env, vault)
    run = _run(vault_env, run_id)
    assert run is not None
    dismissed, kept = run.suggestions[0].id, run.suggestions[1].id
    generation.dismiss(db_conn, dismissed)
    base_commits = _commit_count(vault_env)

    question_ids = await generation.accept(
        db_conn, [dismissed, kept], vault=vault, writer=writer, now=NOW
    )

    assert len(question_ids) == 1
    assert _commit_count(vault_env) == base_commits + 1


async def test_dismiss_hides_suggestion(vault_env: VaultEnv, vault: VaultStore) -> None:
    run_id = await _topic_run(vault_env, vault, topic="thermal runaway")
    conn = connect(vault_env.db_path)
    try:
        run = generation.get_run(conn, run_id)
        assert run is not None
        dismissed = run.suggestions[0].id
        generation.dismiss(conn, dismissed)
        updated = generation.get_run(conn, run_id)
        assert updated is not None
        assert [suggestion.id for suggestion in updated.suggestions] == [
            suggestion.id for suggestion in run.suggestions[1:]
        ]
        row = conn.execute("SELECT state FROM suggestions WHERE id = ?", (dismissed,)).fetchone()
        assert row is not None and row["state"] == "dismissed"
    finally:
        conn.close()


def test_get_run_unknown_returns_none_and_latest_empty(db_conn) -> None:
    assert generation.get_run(db_conn, 1234) is None
    assert generation.latest_run(db_conn) is None
