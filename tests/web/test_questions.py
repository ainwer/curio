"""Route tests for the question page: gut answer, tutor chat and SSE streaming."""

import sqlite3
import time
from datetime import UTC, datetime

import gitutil
import pytest
from conftest import VaultEnv
from fastapi.testclient import TestClient

from curio_app.db import connect
from curio_app.domain import Question, Status
from curio_app.services import tutor
from curio_app.vault.format import render_question
from curio_app.vault.git import GitRepo
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriter

NOW = datetime(2026, 10, 23, 8, 15, 0, tzinfo=UTC)
QUESTION_ID = "q-2026-10-23-invented-question"


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


def seed_question(
    vault: VaultStore,
    env: VaultEnv,
    *,
    status: Status,
    gut_answer: str | None = None,
    insight: str | None = None,
) -> Question:
    question = Question(
        id=QUESTION_ID,
        status=status,
        domain="power-electronics",
        concepts=["gate-drivers"],
        created=NOW,
        title="Why do invented gate drivers fail at high speed?",
        preamble="*Invented hook.*",
    )
    if gut_answer is not None:
        question = question.with_gut_answer(gut_answer)
    if insight is not None:
        question = question.with_insight(insight)
    path = vault.root / vault.relative_question_path(question.id)
    path.write_text(render_question(question), encoding="utf-8")
    gitutil.run_git("add", ".", cwd=env.repos.app)
    gitutil.run_git("commit", "-m", "seed", cwd=env.repos.app)
    return question


def insert_session(conn: sqlite3.Connection, question_id: str = QUESTION_ID) -> None:
    conn.execute(
        "INSERT INTO tutor_sessions (question_id, context, created_at) VALUES (?, ?, ?)",
        (question_id, "invented context block", NOW.isoformat()),
    )
    conn.commit()


def insert_message(
    conn: sqlite3.Connection,
    *,
    question_id: str = QUESTION_ID,
    role: str,
    content: str,
    state: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (question_id, role, content, state, NOW.isoformat()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def latest_assistant(db_path) -> tuple[int, str, str]:
    """Id, state and content of the newest assistant message."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, state, content FROM chat_messages WHERE role = 'assistant'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        return int(row["id"]), str(row["state"]), str(row["content"])
    finally:
        conn.close()


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_question_page_inbox_shows_gut_form(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.INBOX)

    page = vault_client.get(f"/questions/{QUESTION_ID}")

    assert page.status_code == 200
    assert "invented gate drivers fail" in page.text
    assert ">inbox</span>" in page.text
    assert ">gate-drivers</span>" in page.text
    assert "Invented hook." in page.text
    assert "Your gut answer" in page.text
    assert "Commit to a hunch before you research. The tutor starts from your answer." in page.text
    assert f'action="/questions/{QUESTION_ID}/gut-answer"' in page.text
    assert 'name="gut_answer"' in page.text
    assert 'maxlength="1000"' in page.text
    assert "required" in page.text
    assert "autofocus" in page.text
    assert "Start research" in page.text


def test_question_page_researching_shows_chat(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="My invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    user_id = insert_message(conn, role="user", content="Tell me why.", state="done")
    assistant_id = insert_message(
        conn, role="assistant", content="First invented reply.", state="done"
    )
    failed_id = insert_message(conn, role="assistant", content="partial invented", state="failed")
    conn.close()

    page = vault_client.get(f"/questions/{QUESTION_ID}")

    assert page.status_code == 200
    assert ">researching</span>" in page.text
    assert "Your gut answer" in page.text
    assert "My invented hunch." in page.text
    assert '<div id="chat" class="chat-log">' in page.text
    assert f'id="msg-{user_id}"' in page.text
    assert "bubble-user" in page.text
    assert f'id="msg-{assistant_id}"' in page.text
    assert "bubble-assistant" in page.text
    assert "First invented reply." in page.text
    assert f'id="msg-{failed_id}"' in page.text
    assert "The tutor reply failed." in page.text
    assert f'hx-post="/questions/{QUESTION_ID}/messages/{failed_id}/retry"' in page.text
    assert ">Retry</button>" in page.text
    assert f'hx-post="/questions/{QUESTION_ID}/messages"' in page.text
    assert 'hx-target="#chat"' in page.text
    assert 'hx-swap="beforeend"' in page.text
    assert 'name="text"' in page.text
    assert 'maxlength="4000"' in page.text


def test_question_page_researching_shows_live_bubble(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="Invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    message_id = insert_message(conn, role="assistant", content="", state="streaming")
    conn.close()

    page = vault_client.get(f"/questions/{QUESTION_ID}")

    assert f'sse-connect="/questions/{QUESTION_ID}/messages/{message_id}/stream"' in page.text
    assert 'sse-close="done"' in page.text
    assert 'sse-swap="token"' in page.text
    assert 'sse-swap="done,error"' in page.text
    assert 'hx-swap="outerHTML"' in page.text
    assert "is-streaming" in page.text


def test_question_page_shows_vault_warning(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="Invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    conn.execute(
        "INSERT INTO vault_drafts (question_id, path, content, commit_message, error, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            QUESTION_ID,
            "Learning/Questions/q.md",
            "invented",
            "curio: gut answer q",
            "dirty",
            NOW.isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    page = vault_client.get(f"/questions/{QUESTION_ID}")

    assert "Saved locally; the vault sync will be retried from the inbox." in page.text


def test_question_page_done_is_read_only(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(
        vault,
        vault_env,
        status=Status.DONE,
        gut_answer="Invented hunch.",
        insight="Speed comes from **less** charge.",
    )

    page = vault_client.get(f"/questions/{QUESTION_ID}")

    assert page.status_code == 200
    assert ">done</span>" in page.text
    assert "Your gut answer" in page.text
    assert "Invented hunch." in page.text
    assert "<strong>less</strong>" in page.text
    assert "composer" not in page.text
    assert 'name="gut_answer"' not in page.text


def test_question_page_unknown_is_404(vault_client: TestClient) -> None:
    response = vault_client.get(
        "/questions/q-2026-01-01-does-not-exist", headers={"accept": "text/html"}
    )

    assert response.status_code == 404
    assert "Not found" in response.text


def test_gut_answer_post_happy_path(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.INBOX)

    response = vault_client.post(
        f"/questions/{QUESTION_ID}/gut-answer",
        data={"gut_answer": "  The charge pump is too slow.  "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/questions/{QUESTION_ID}"
    text = (vault.root / vault.relative_question_path(QUESTION_ID)).read_text(encoding="utf-8")
    assert "status: researching" in text
    assert "## Gut answer" in text
    assert "The charge pump is too slow." in text
    message_id, _, _ = latest_assistant(vault_env.db_path)
    assert wait_until(lambda: latest_assistant(vault_env.db_path)[1] == "done")
    # The first tutor turn streamed and was persisted by the fake LLM.
    _, state, content = latest_assistant(vault_env.db_path)
    assert state == "done"
    assert content.startswith("This is a fake tutor reply")
    log = gitutil.run_git("log", "--oneline", cwd=vault_env.repos.app)
    assert f"curio: gut answer {QUESTION_ID}" in log

    page = vault_client.get(f"/questions/{QUESTION_ID}")
    assert ">researching</span>" in page.text
    assert "This is a fake tutor reply" in page.text
    assert f'id="msg-{message_id}"' in page.text


def test_gut_answer_post_starts_stream_immediately(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.INBOX)

    vault_client.post(
        f"/questions/{QUESTION_ID}/gut-answer",
        data={"gut_answer": "My invented hunch."},
        follow_redirects=True,
    )

    _message_id, state, content = latest_assistant(vault_env.db_path)
    assert state in ("streaming", "done")
    if state == "done":
        assert content.startswith("This is a fake tutor reply")


def test_gut_answer_post_validation_error(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.INBOX)

    empty = vault_client.post(f"/questions/{QUESTION_ID}/gut-answer", data={"gut_answer": "   "})
    too_long = vault_client.post(
        f"/questions/{QUESTION_ID}/gut-answer", data={"gut_answer": "x" * 1001}
    )

    assert empty.status_code == 400
    assert tutor.GUT_ANSWER_ERROR in empty.text
    assert "alert-error" in empty.text
    assert too_long.status_code == 400
    conn = connect(vault_env.db_path)
    assert conn.execute("SELECT 1 FROM tutor_sessions").fetchall() == []
    conn.close()


def test_gut_answer_post_twice_is_rejected(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.INBOX)
    conn = connect(vault_env.db_path)
    insert_session(conn)
    conn.close()

    response = vault_client.post(
        f"/questions/{QUESTION_ID}/gut-answer", data={"gut_answer": "A hunch."}
    )

    assert response.status_code == 400
    assert tutor.GUT_ANSWER_DONE_ERROR in response.text


def test_gut_answer_post_vault_failure_shows_warning(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.INBOX)
    stray = vault_env.repos.app / "Learning" / "stray-note.md"
    stray.write_text("invented stray content\n", encoding="utf-8")

    response = vault_client.post(
        f"/questions/{QUESTION_ID}/gut-answer",
        data={"gut_answer": "Invented hunch despite trouble."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    conn = connect(vault_env.db_path)
    session = conn.execute(
        "SELECT 1 FROM tutor_sessions WHERE question_id = ?", (QUESTION_ID,)
    ).fetchone()
    drafts = conn.execute("SELECT question_id FROM vault_drafts").fetchall()
    conn.close()
    assert session is not None
    assert [str(row[0]) for row in drafts] == [QUESTION_ID]
    stray.unlink()

    page = vault_client.get(f"/questions/{QUESTION_ID}")
    assert "Saved locally; the vault sync will be retried from the inbox." in page.text


def test_message_post_returns_bubbles(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="Invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    conn.close()

    response = vault_client.post(
        f"/questions/{QUESTION_ID}/messages",
        data={"text": "  How fast can the gate switch?  "},
    )

    assert response.status_code == 200
    assert "How fast can the gate switch?" in response.text
    assert "bubble-user" in response.text
    assert "is-streaming" in response.text
    assert 'sse-connect="/questions/' in response.text


def test_message_post_validation_error(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="Invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    conn.close()

    response = vault_client.post(f"/questions/{QUESTION_ID}/messages", data={"text": ""})

    assert response.status_code == 400
    assert "alert-error" in response.text
    assert tutor.MESSAGE_EMPTY_ERROR in response.text


def test_message_post_busy_error(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="Invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    insert_message(conn, role="assistant", content="", state="streaming")
    conn.close()

    response = vault_client.post(f"/questions/{QUESTION_ID}/messages", data={"text": "Next."})

    assert response.status_code == 400
    assert tutor.MESSAGE_BUSY_ERROR in response.text


def test_stream_for_finished_message_returns_done_event(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="Invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    message_id = insert_message(
        conn, role="assistant", content="Finished invented reply.", state="done"
    )
    conn.close()

    response = vault_client.get(f"/questions/{QUESTION_ID}/messages/{message_id}/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert "event: done" in response.text
    assert "bubble-assistant" in response.text
    assert "Finished invented reply." in response.text
    assert "token" not in response.text  # no live chunks for a finished message


def test_stream_for_failed_message_returns_error_event(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="Invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    message_id = insert_message(conn, role="assistant", content="partial invented", state="failed")
    conn.close()

    response = vault_client.get(f"/questions/{QUESTION_ID}/messages/{message_id}/stream")

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "The tutor reply failed." in response.text


def test_stream_for_unknown_message_is_404(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="Invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    conn.close()

    response = vault_client.get(f"/questions/{QUESTION_ID}/messages/999/stream")

    assert response.status_code == 404


def test_stream_for_live_message_streams_tokens_then_done(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.INBOX)
    vault_client.post(
        f"/questions/{QUESTION_ID}/gut-answer",
        data={"gut_answer": "Invented hunch."},
        follow_redirects=False,
    )
    message_id, state, _ = latest_assistant(vault_env.db_path)
    assert state in ("streaming", "done")

    response = vault_client.get(f"/questions/{QUESTION_ID}/messages/{message_id}/stream")

    assert response.status_code == 200
    # Whether the client joins early (live chunks) or late (one combined token),
    # it always sees the tutor reply and exactly one closing done event.
    assert "event: done" in response.text
    assert "bubble-assistant" in response.text
    assert response.text.count("event: done") == 1


def test_retry_route_returns_live_bubble(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env, status=Status.RESEARCHING, gut_answer="Invented hunch.")
    conn = connect(vault_env.db_path)
    insert_session(conn)
    failed_id = insert_message(conn, role="assistant", content="partial invented", state="failed")
    conn.close()

    response = vault_client.post(f"/questions/{QUESTION_ID}/messages/{failed_id}/retry")

    assert response.status_code == 200
    assert f'sse-connect="/questions/{QUESTION_ID}/messages/{failed_id}/stream"' in response.text
    assert "is-streaming" in response.text
    assert wait_until(lambda: latest_assistant(vault_env.db_path)[1] == "done")

    retry_page = vault_client.get(f"/questions/{QUESTION_ID}")
    assert "The tutor reply failed." not in retry_page.text


def test_gut_answer_page_renders_math_in_preamble(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    question = Question(
        id="q-2026-10-23-invented-math",
        status=Status.INBOX,
        domain="power-electronics",
        concepts=["gate-drivers"],
        created=NOW,
        title="Why does invented math render?",
        preamble="Charge is $Q = C \\cdot V$ in the invented setup.",
    )
    path = vault.root / vault.relative_question_path(question.id)
    path.write_text(render_question(question), encoding="utf-8")

    page = vault_client.get(f"/questions/{question.id}")

    assert page.status_code == 200
    assert "$Q = C \\cdot V$" in page.text
    assert "<em>" not in page.text
