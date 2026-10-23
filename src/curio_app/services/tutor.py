"""Gut answer submission and the Socratic tutor chat state (PLAN.md §2, flows 2-3)."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from curio_app.books.search import search
from curio_app.books.store import BookStore
from curio_app.domain import Question, Status
from curio_app.llm.prompts import Excerpt, NoteRef, tutor_context
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import FileChange, VaultWriteError, VaultWriter

GUT_ANSWER_MAX_CHARS = 1000
MESSAGE_MAX_CHARS = 4000

GUT_ANSWER_ERROR = "Write a short gut answer first (max 1000 characters)."
GUT_ANSWER_DONE_ERROR = "This question is already being researched."
MESSAGE_EMPTY_ERROR = "Write a message first (max 4000 characters)."
MESSAGE_BUSY_ERROR = "Wait for the tutor to finish replying."


class GutAnswerError(ValueError):
    """Invalid gut answer or question state; the message is user-facing."""


class TutorInputError(ValueError):
    """Invalid chat message input; the message is user-facing."""


@dataclass(frozen=True)
class ChatMessage:
    """One chat message row."""

    id: int
    role: str
    content: str
    state: str
    created_at: str


def effective_status(q: Question, conn: sqlite3.Connection) -> Status:
    """The visible status: INBOX with a tutor session means a vault draft is pending."""
    if q.status is Status.INBOX:
        row = conn.execute("SELECT 1 FROM tutor_sessions WHERE question_id = ?", (q.id,)).fetchone()
        if row is not None:
            return Status.RESEARCHING
    return q.status


async def submit_gut_answer(
    conn: sqlite3.Connection,
    q: Question,
    text: str,
    *,
    vault: VaultStore,
    writer: VaultWriter,
    books: BookStore,
    now: datetime,
) -> str | None:
    """Commit the gut answer to the vault, freeze the tutor context, open the first turn.

    Returns the vault error message when the commit fails (the flow continues with the
    draft; the inbox banner and question page offer a retry), or ``None`` on success.
    Raises :class:`GutAnswerError` for invalid input or state.
    """
    stripped = text.strip()
    if not 1 <= len(stripped) <= GUT_ANSWER_MAX_CHARS:
        raise GutAnswerError(GUT_ANSWER_ERROR)
    if effective_status(q, conn) is not Status.INBOX:
        raise GutAnswerError(GUT_ANSWER_DONE_ERROR)

    updated = q.with_gut_answer(stripped).model_copy(update={"status": Status.RESEARCHING})
    path, content = vault.render(updated)
    vault_error: str | None = None
    try:
        await writer.write([FileChange(q.id, path, content)], f"curio: gut answer {q.id}")
    except VaultWriteError as exc:
        vault_error = exc.message

    query = f"{q.title} {stripped} {' '.join(q.concepts)}"
    excerpts = [
        Excerpt(book=hit.book_title, chapter=hit.chapter_number, section=hit.section, text=hit.text)
        for hit in search(query, books)
    ]
    notes = [NoteRef(title=note.title, text=note.text) for note in vault.linked_notes(updated)]
    context = tutor_context(
        question=q.title,
        concepts=q.concepts,
        gut_answer=stripped,
        excerpts=excerpts,
        notes=notes,
    )
    conn.execute(
        "INSERT INTO tutor_sessions (question_id, context, created_at) VALUES (?, ?, ?)",
        (q.id, context, now.isoformat()),
    )
    start_assistant_turn(conn, q.id)
    conn.commit()
    return vault_error


def history(conn: sqlite3.Connection, question_id: str) -> list[ChatMessage]:
    """All chat messages of a question, oldest first."""
    rows = conn.execute(
        "SELECT id, role, content, state, created_at FROM chat_messages"
        " WHERE question_id = ? ORDER BY id",
        (question_id,),
    ).fetchall()
    return [_message_from_row(row) for row in rows]


def start_user_turn(conn: sqlite3.Connection, question_id: str, text: str) -> tuple[int, int]:
    """Store a user message plus an empty streaming assistant message; return both ids.

    Raises :class:`TutorInputError` for empty or too long text and while an assistant
    message of the question is still streaming.
    """
    stripped = text.strip()
    if not 1 <= len(stripped) <= MESSAGE_MAX_CHARS:
        raise TutorInputError(MESSAGE_EMPTY_ERROR)
    busy = conn.execute(
        "SELECT 1 FROM chat_messages WHERE question_id = ? AND role = 'assistant'"
        " AND state = 'streaming' LIMIT 1",
        (question_id,),
    ).fetchone()
    if busy is not None:
        raise TutorInputError(MESSAGE_BUSY_ERROR)
    created_at = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'user', ?, 'done', ?)",
        (question_id, stripped, created_at),
    )
    user_id = int(cursor.lastrowid or 0)
    assistant_id = start_assistant_turn(conn, question_id)
    conn.commit()
    return user_id, assistant_id


def retry_assistant(conn: sqlite3.Connection, message_id: int) -> None:
    """Reset a failed assistant message to streaming with cleared content."""
    row = conn.execute(
        "SELECT role, state FROM chat_messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None or str(row["role"]) != "assistant" or str(row["state"]) != "failed":
        return
    conn.execute(
        "UPDATE chat_messages SET state = 'streaming', content = '' WHERE id = ?",
        (message_id,),
    )
    conn.commit()


def start_assistant_turn(conn: sqlite3.Connection, question_id: str) -> int:
    """Insert an empty streaming assistant message and return its id.

    The caller starts the LLM stream for it via the stream registry.
    """
    cursor = conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'assistant', '', 'streaming', ?)",
        (question_id, datetime.now(UTC).isoformat()),
    )
    return int(cursor.lastrowid or 0)


def streaming_message_id(conn: sqlite3.Connection, question_id: str) -> int | None:
    """Id of the latest streaming assistant message of a question, or None."""
    row = conn.execute(
        "SELECT id FROM chat_messages WHERE question_id = ? AND role = 'assistant'"
        " AND state = 'streaming' ORDER BY id DESC LIMIT 1",
        (question_id,),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def message(conn: sqlite3.Connection, question_id: str, message_id: int) -> ChatMessage | None:
    """One assistant chat message belonging to the question, or None."""
    row = conn.execute(
        "SELECT id, role, content, state, created_at FROM chat_messages"
        " WHERE id = ? AND question_id = ?",
        (message_id, question_id),
    ).fetchone()
    return _message_from_row(row) if row is not None else None


def _message_from_row(row: sqlite3.Row) -> ChatMessage:
    return ChatMessage(
        id=int(row["id"]),
        role=str(row["role"]),
        content=str(row["content"]),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
    )
