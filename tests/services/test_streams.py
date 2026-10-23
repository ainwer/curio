"""Tests for background assistant-turn streaming."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import gitutil
import pytest
from conftest import VaultEnv
from fastapi.testclient import TestClient

from curio_app.books.store import BookStore
from curio_app.db import connect
from curio_app.domain import Question, Status
from curio_app.llm.base import LLMUnavailable
from curio_app.llm.fake import FakeLLMClient
from curio_app.llm.prompts import tutor_system
from curio_app.main import create_app
from curio_app.services import tutor
from curio_app.services.streams import StreamRegistry
from curio_app.vault.format import render_question
from curio_app.vault.git import GitRepo
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriter

NOW = datetime(2026, 10, 23, 8, 15, 0, tzinfo=UTC)
QUESTION_ID = "q-2026-10-23-invented-stream"
FAKE_REPLY = "This is a fake tutor reply about your gut answer. What do you think happens next? "


class FailingStreamClient:
    """Stub LLM that raises mid-stream after one chunk."""

    async def generate(self, **_kwargs: object) -> object:
        raise NotImplementedError

    async def stream(self, *, system: str, messages: list) -> AsyncIterator[str]:
        yield "Partial invented answer. "
        raise LLMUnavailable("The tutor is unreachable right now.")


class SlowStreamClient:
    """Stub LLM that yields three chunks slowly."""

    async def generate(self, **_kwargs: object) -> object:
        raise NotImplementedError

    async def stream(self, *, system: str, messages: list) -> AsyncIterator[str]:
        for word in ("one ", "two ", "three "):
            yield word
            await asyncio.sleep(0.01)


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


@pytest.fixture
def books(vault_env: VaultEnv, db_conn: sqlite3.Connection) -> BookStore:
    return BookStore(db_conn, vault_env.settings.books_path)


async def setup_question(
    vault_env: VaultEnv,
    vault: VaultStore,
    writer: VaultWriter,
    books: BookStore,
    gut: str = "Invented hunch.",
) -> str:
    question = Question(
        id=QUESTION_ID,
        status=Status.INBOX,
        domain="power-electronics",
        concepts=["gate-drivers"],
        created=NOW,
        title="Why do invented gate drivers fail at high speed?",
    )
    path = vault.root / vault.relative_question_path(question.id)
    path.write_text(render_question(question), encoding="utf-8")
    gitutil.run_git("add", ".", cwd=vault_env.repos.app)
    gitutil.run_git("commit", "-m", "seed", cwd=vault_env.repos.app)
    conn = connect(vault_env.db_path)
    try:
        await tutor.submit_gut_answer(
            conn, question, gut, vault=vault, writer=writer, books=books, now=NOW
        )
    finally:
        conn.close()
    return question.id


def streaming_assistant_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM chat_messages WHERE question_id = ? AND role = 'assistant'"
        " AND state = 'streaming' ORDER BY id DESC",
        (QUESTION_ID,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


async def test_fake_llm_turn_completes_and_persists(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, books: BookStore
) -> None:
    qid = await setup_question(vault_env, vault, writer, books)
    fake = FakeLLMClient()
    registry = StreamRegistry()
    conn = connect(vault_env.db_path)
    message_id = streaming_assistant_id(conn)

    stream = registry.start(message_id, db_path=vault_env.db_path, llm=fake, question_id=qid)
    events = [event async for event in stream.subscribe()]

    assert events[0][0] == "token"
    assert events[-1] == ("done", "")
    row = conn.execute(
        "SELECT content, state FROM chat_messages WHERE id = ?", (message_id,)
    ).fetchone()
    assert (str(row["state"]), str(row["content"])) == ("done", FAKE_REPLY)
    conn.close()


async def test_history_passed_to_llm_excludes_failed_and_streaming(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, books: BookStore
) -> None:
    qid = await setup_question(vault_env, vault, writer, books)
    fake = FakeLLMClient()
    registry = StreamRegistry()
    conn = connect(vault_env.db_path)
    first_assistant = streaming_assistant_id(conn)
    # The first tutor reply finished; the user asked again; one older turn failed.
    conn.execute(
        "UPDATE chat_messages SET state = 'done', content = 'First reply.' WHERE id = ?",
        (first_assistant,),
    )
    conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'user', 'Second question.', 'done', ?),"
        " (?, 'assistant', 'partial', 'failed', ?)",
        (QUESTION_ID, NOW.isoformat(), QUESTION_ID, NOW.isoformat()),
    )
    _, next_assistant = tutor.start_user_turn(conn, qid, "What if the loop is short?")
    conn.commit()

    registry.start(next_assistant, db_path=vault_env.db_path, llm=fake, question_id=qid)
    while not [c for c in fake.calls if c.get("kind") == "stream"]:
        await asyncio.sleep(0.01)
    conn.close()

    call = fake.calls[-1]
    assert call["system"] == tutor_system()
    messages = call["messages"]
    assert [str(message["role"]) for message in messages] == ["user", "assistant", "user"]
    context = messages[0]["content"]
    assert isinstance(context, list)
    assert "Invented hunch." in context[0]["text"]
    second_user = messages[2]["content"]
    assert isinstance(second_user, str)
    assert second_user == "Second question.\n\nWhat if the loop is short?"


async def test_llm_failure_marks_turn_failed_with_partial_text(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, books: BookStore
) -> None:
    qid = await setup_question(vault_env, vault, writer, books)
    conn = connect(vault_env.db_path)
    message_id = streaming_assistant_id(conn)
    registry = StreamRegistry()

    stream = registry.start(
        message_id, db_path=vault_env.db_path, llm=FailingStreamClient(), question_id=qid
    )
    events = [event async for event in stream.subscribe()]

    assert events[-1] == ("error", "The tutor is unreachable right now.")
    assert stream.done
    assert stream.error == "The tutor is unreachable right now."
    row = conn.execute(
        "SELECT content, state FROM chat_messages WHERE id = ?", (message_id,)
    ).fetchone()
    assert (str(row["state"]), str(row["content"])) == ("failed", "Partial invented answer. ")
    conn.close()


async def test_late_subscriber_receives_full_text_then_done(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, books: BookStore
) -> None:
    qid = await setup_question(vault_env, vault, writer, books)
    conn = connect(vault_env.db_path)
    message_id = streaming_assistant_id(conn)
    registry = StreamRegistry()
    stream = registry.start(
        message_id, db_path=vault_env.db_path, llm=SlowStreamClient(), question_id=qid
    )
    while not stream.done:
        await asyncio.sleep(0.005)
    registry.get(message_id)
    conn.close()

    events = [event async for event in stream.subscribe()]

    assert events == [("token", "one two three "), ("done", "")]


async def test_subscriber_joins_mid_stream(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, books: BookStore
) -> None:
    qid = await setup_question(vault_env, vault, writer, books)
    conn = connect(vault_env.db_path)
    message_id = streaming_assistant_id(conn)
    registry = StreamRegistry()
    stream = registry.start(
        message_id, db_path=vault_env.db_path, llm=SlowStreamClient(), question_id=qid
    )
    await asyncio.sleep(0.004)
    text_at_join = stream.text
    conn.close()

    events = [event async for event in stream.subscribe()]

    assert events[0] == ("token", text_at_join)
    assert events[-1] == ("done", "")
    assert "".join(text for name, text in events if name == "token") == "one two three "


def test_startup_marks_stale_streaming_failed(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, books: BookStore
) -> None:
    asyncio.run(setup_question(vault_env, vault, writer, books))
    conn = connect(vault_env.db_path)
    assert streaming_assistant_id(conn) is not None
    conn.close()

    with TestClient(create_app(vault_env.settings)) as client:
        assert client.get("/healthz").status_code == 200
    conn = connect(vault_env.db_path)
    states = {
        str(row["state"])
        for row in conn.execute(
            "SELECT state FROM chat_messages WHERE question_id = ?", (QUESTION_ID,)
        ).fetchall()
    }
    assert states == {"failed"}
    conn.close()


async def test_registry_get_returns_stream(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter, books: BookStore
) -> None:
    qid = await setup_question(vault_env, vault, writer, books)
    conn = connect(vault_env.db_path)
    message_id = streaming_assistant_id(conn)
    conn.close()
    registry = StreamRegistry()

    assert registry.get(message_id) is None
    stream = registry.start(
        message_id, db_path=vault_env.db_path, llm=FakeLLMClient(), question_id=qid
    )
    assert registry.get(message_id) is stream
