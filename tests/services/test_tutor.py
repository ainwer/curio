"""Tests for the tutor service: gut answer submission and chat message state."""

import re
import sqlite3
from datetime import UTC, datetime

import gitutil
import pytest
from books.support import make_ready_book
from conftest import VaultEnv

from curio_app.books.store import BookStore
from curio_app.db import connect
from curio_app.domain import Question, Status
from curio_app.services import tutor
from curio_app.vault.format import render_question
from curio_app.vault.git import GitRepo
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriter

NOW = datetime(2026, 10, 23, 8, 15, 0, tzinfo=UTC)

BOOK_MD = (
    "# Invented Drivers\n\n"
    "## Gate drive basics\n"
    "Invented gate drivers fail at high speed when the charge pump cannot keep up.\n\n"
    "## Layout rules\n"
    "Keep the invented driver gate loop short and decouple the supply nearby.\n"
)

QUESTION_ID = "q-2026-10-23-invented-gate-drivers"


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
    make_ready_book(BookStore(db_conn, vault_env.settings.books_path), "Invented Drivers", BOOK_MD)
    return BookStore(db_conn, vault_env.settings.books_path)


def seed_question(
    vault: VaultStore,
    env: VaultEnv,
    *,
    status: Status = Status.INBOX,
    concepts: list[str] | None = None,
) -> Question:
    question = Question(
        id=QUESTION_ID,
        status=status,
        domain="power-electronics",
        concepts=concepts or ["gate-drivers"],
        created=NOW,
        title="Why do invented gate drivers fail at high speed?",
    )
    path = vault.root / vault.relative_question_path(question.id)
    path.write_text(render_question(question), encoding="utf-8")
    gitutil.run_git("add", ".", cwd=env.repos.app)
    gitutil.run_git("commit", "-m", "seed", cwd=env.repos.app)
    return question


def seed_concept_note(env: VaultEnv) -> None:
    note = env.repos.app / "Notes" / "Gate Drivers.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("Invented note about gate drivers for tests.\n", encoding="utf-8")


def question_file(vault: VaultStore) -> str:
    return (vault.root / vault.relative_question_path(QUESTION_ID)).read_text(encoding="utf-8")


async def test_gut_answer_success_writes_vault_and_session(
    vault_env: VaultEnv,
    vault: VaultStore,
    writer: VaultWriter,
    books: BookStore,
) -> None:
    seed_concept_note(vault_env)
    question = seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)

    error = await tutor.submit_gut_answer(
        conn,
        question,
        "  The charge pump is too slow.  ",
        vault=vault,
        writer=writer,
        books=books,
        now=NOW,
    )

    assert error is None
    text = question_file(vault)
    assert "status: researching" in text
    assert "## Gut answer" in text
    assert "The charge pump is too slow." in text
    session = conn.execute(
        "SELECT context, created_at FROM tutor_sessions WHERE question_id = ?", (QUESTION_ID,)
    ).fetchone()
    assert session is not None
    context = str(session["context"])
    assert "The charge pump is too slow." in context
    assert "Invented Drivers" in context  # book excerpt from the ready book
    assert "Invented note about gate drivers" in context  # linked note via concept slug
    assert str(session["created_at"]) == NOW.isoformat()
    messages = tutor.history(conn, QUESTION_ID)
    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert messages[0].state == "streaming"
    assert messages[0].content == ""
    assert tutor.effective_status(vault.get(QUESTION_ID), conn) is Status.RESEARCHING
    conn.close()


async def test_gut_answer_vault_failure_still_creates_session(
    vault_env: VaultEnv,
    vault: VaultStore,
    writer: VaultWriter,
    books: BookStore,
) -> None:
    question = seed_question(vault, vault_env)
    stray = vault_env.repos.app / "Learning" / "stray-note.md"
    stray.write_text("invented stray content\n", encoding="utf-8")
    conn = connect(vault_env.db_path)

    error = await tutor.submit_gut_answer(
        conn,
        question,
        "A hunch about invented drivers.",
        vault=vault,
        writer=writer,
        books=books,
        now=NOW,
    )

    assert error is not None
    assert "uncommitted changes" in error
    session = conn.execute(
        "SELECT 1 FROM tutor_sessions WHERE question_id = ?", (QUESTION_ID,)
    ).fetchone()
    assert session is not None
    assert tutor.effective_status(vault.get(QUESTION_ID), conn) is Status.RESEARCHING
    messages = tutor.history(conn, QUESTION_ID)
    assert messages[0].state == "streaming"
    # The draft is kept for the retry banner.
    assert writer.list_drafts()[0].question_id == QUESTION_ID
    conn.close()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", tutor.GUT_ANSWER_ERROR),
        ("   ", tutor.GUT_ANSWER_ERROR),
        ("x" * 1001, tutor.GUT_ANSWER_ERROR),
    ],
)
async def test_gut_answer_validation_rejects_input(
    vault_env: VaultEnv,
    vault: VaultStore,
    writer: VaultWriter,
    books: BookStore,
    text: str,
    message: str,
) -> None:
    question = seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)

    with pytest.raises(tutor.GutAnswerError, match=re.escape(message)):
        await tutor.submit_gut_answer(
            conn, question, text, vault=vault, writer=writer, books=books, now=NOW
        )

    assert conn.execute("SELECT 1 FROM tutor_sessions").fetchall() == []
    conn.close()


async def test_gut_answer_rejected_when_already_researching(
    vault_env: VaultEnv,
    vault: VaultStore,
    writer: VaultWriter,
    books: BookStore,
) -> None:
    question = seed_question(vault, vault_env, status=Status.RESEARCHING)
    conn = connect(vault_env.db_path)

    with pytest.raises(tutor.GutAnswerError, match=tutor.GUT_ANSWER_DONE_ERROR):
        await tutor.submit_gut_answer(
            conn, question, "A hunch.", vault=vault, writer=writer, books=books, now=NOW
        )

    conn.close()


def test_effective_status_covers_pending_draft(
    vault_env: VaultEnv, db_conn: sqlite3.Connection, vault: VaultStore
) -> None:
    question = seed_question(vault, vault_env)
    db_conn.execute(
        "INSERT INTO tutor_sessions (question_id, context, created_at) VALUES (?, ?, ?)",
        (QUESTION_ID, "invented context", NOW.isoformat()),
    )
    db_conn.commit()

    assert tutor.effective_status(question, db_conn) is Status.RESEARCHING


def test_start_user_turn_validation_and_busy_state(
    vault_env: VaultEnv, db_conn: sqlite3.Connection, vault: VaultStore
) -> None:
    seed_question(vault, vault_env)  # no session: status stays INBOX for this test
    db_conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'assistant', '', 'streaming', ?)",
        (QUESTION_ID, NOW.isoformat()),
    )
    db_conn.commit()

    with pytest.raises(tutor.TutorInputError, match=tutor.MESSAGE_BUSY_ERROR):
        tutor.start_user_turn(db_conn, QUESTION_ID, "Next message.")
    with pytest.raises(tutor.TutorInputError, match=re.escape(tutor.MESSAGE_EMPTY_ERROR)):
        tutor.start_user_turn(db_conn, QUESTION_ID, "   ")
    with pytest.raises(tutor.TutorInputError, match=re.escape(tutor.MESSAGE_EMPTY_ERROR)):
        tutor.start_user_turn(db_conn, QUESTION_ID, "y" * 4001)


def test_start_user_turn_inserts_both_messages(
    vault_env: VaultEnv, db_conn: sqlite3.Connection, vault: VaultStore
) -> None:
    seed_question(vault, vault_env)
    db_conn.commit()

    user_id, assistant_id = tutor.start_user_turn(
        db_conn, QUESTION_ID, "  What about the gate loop?  "
    )

    assert assistant_id == user_id + 1
    messages = tutor.history(db_conn, QUESTION_ID)
    assert [(m.role, m.state, m.content) for m in messages] == [
        ("user", "done", "What about the gate loop?"),
        ("assistant", "streaming", ""),
    ]


def test_retry_assistant_resets_failed_message(
    vault_env: VaultEnv, db_conn: sqlite3.Connection, vault: VaultStore
) -> None:
    seed_question(vault, vault_env)
    db_conn.commit()
    user_id, assistant_id = tutor.start_user_turn(db_conn, QUESTION_ID, "First try.")
    db_conn.execute(
        "UPDATE chat_messages SET state = 'failed', content = 'partial invented' WHERE id = ?",
        (assistant_id,),
    )
    db_conn.commit()

    tutor.retry_assistant(db_conn, assistant_id)

    row = db_conn.execute(
        "SELECT content, state FROM chat_messages WHERE id = ?", (assistant_id,)
    ).fetchone()
    assert (str(row["state"]), str(row["content"])) == ("streaming", "")

    tutor.retry_assistant(db_conn, user_id)
    tutor.retry_assistant(db_conn, assistant_id + 999)
    done_row = db_conn.execute(
        "SELECT state FROM chat_messages WHERE id = ?", (user_id,)
    ).fetchone()
    assert str(done_row["state"]) == "done"
