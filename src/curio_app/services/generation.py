"""Question generation runs and the suggestion lifecycle (PLAN.md §7)."""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from curio_app.books.store import BookStore
from curio_app.db import connect
from curio_app.domain import Question, Status
from curio_app.llm.base import LLMClient, LLMError
from curio_app.llm.prompts import (
    question_bridge_messages,
    question_source_messages,
    question_system,
    question_topic_messages,
)
from curio_app.llm.schemas import QuestionBatch
from curio_app.vault.ids import question_id
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import FileChange, VaultWriter

logger = logging.getLogger(__name__)

Mode = Literal["source", "topic", "bridge"]
MODES: tuple[Mode, ...] = ("source", "topic", "bridge")

MAX_TOPIC_CHARS = 200
_UNEXPECTED_ERROR = "Unexpected error."
_SOURCE_MAX_CHARS = 400_000
_EXISTING_MAX = 50

# Referenced until done so the event loop does not garbage-collect the task.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


class GenerationInputError(ValueError):
    """Invalid input for a generation run; the message is user-facing."""


@dataclass(frozen=True)
class Suggestion:
    """One generated question awaiting a decision."""

    id: int
    run_id: int
    question: str
    domain: str
    concepts: list[str]
    source: str | None
    hook: str
    bridge_from: str | None
    state: str
    question_id: str | None


@dataclass(frozen=True)
class Run:
    """One generation run with its pending suggestions."""

    id: int
    mode: str
    input: str
    state: str
    error: str | None
    created_at: str
    suggestions: list[Suggestion]


def start_run(conn: sqlite3.Connection, mode: Mode, input_value: str) -> int:
    """Validate the input, record a running run and return its id."""
    if mode not in MODES:
        raise GenerationInputError("Choose one of Source, Topic or Bridge.")
    stored = input_value.strip()
    if mode == "source":
        if _chapter_id(conn, stored) is None:
            raise GenerationInputError("Choose a chapter of a ready book.")
    elif mode == "topic":
        if not 1 <= len(stored) <= MAX_TOPIC_CHARS:
            raise GenerationInputError("Enter a topic of 1-200 characters.")
    elif len(stored) > MAX_TOPIC_CHARS:
        raise GenerationInputError("The focus can be at most 200 characters.")
    cursor = conn.execute(
        "INSERT INTO generation_runs (mode, input, state, error, created_at)"
        " VALUES (?, ?, 'running', NULL, ?)",
        (mode, stored, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def schedule_execution(
    run_id: int,
    *,
    db_path: Path,
    llm: LLMClient,
    vault: VaultStore,
    books_path: Path,
) -> None:
    """Execute a run in the background on the current event loop."""
    task = asyncio.create_task(
        execute_run(run_id, db_path=db_path, llm=llm, vault=vault, books_path=books_path)
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def execute_run(
    run_id: int,
    *,
    db_path: Path,
    llm: LLMClient,
    vault: VaultStore,
    books_path: Path,
) -> None:
    """Build the prompt, call the LLM and store the proposed questions."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, mode, input FROM generation_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return
        try:
            batch = await _generate_batch(
                conn,
                mode=mode_value(row["mode"]),
                input_value=str(row["input"]),
                llm=llm,
                vault=vault,
                books_path=books_path,
            )
        except GenerationInputError as exc:
            _fail_run(conn, run_id, str(exc))
        except LLMError as exc:
            _fail_run(conn, run_id, str(exc))
        except Exception as exc:
            logger.warning("Generation run %s failed: %s", run_id, type(exc).__name__)
            _fail_run(conn, run_id, _UNEXPECTED_ERROR)
        else:
            _store_batch(conn, run_id, batch)
            conn.execute(
                "UPDATE generation_runs SET state = 'done', error = NULL WHERE id = ?", (run_id,)
            )
            conn.commit()
    finally:
        conn.close()


def get_run(conn: sqlite3.Connection, run_id: int) -> Run | None:
    """One run with its live (not dismissed) suggestions, or None."""
    row = conn.execute(
        "SELECT id, mode, input, state, error, created_at FROM generation_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    suggestion_rows = conn.execute(
        "SELECT id, run_id, question, domain, concepts, source, hook, bridge_from, state,"
        " question_id FROM suggestions WHERE run_id = ? AND state != 'dismissed' ORDER BY id",
        (run_id,),
    ).fetchall()
    return Run(
        id=int(row["id"]),
        mode=str(row["mode"]),
        input=str(row["input"]),
        state=str(row["state"]),
        error=row["error"],
        created_at=str(row["created_at"]),
        suggestions=[_suggestion_from_row(source) for source in suggestion_rows],
    )


def latest_run(conn: sqlite3.Connection) -> Run | None:
    """The newest run, or None."""
    row = conn.execute("SELECT id FROM generation_runs ORDER BY id DESC LIMIT 1").fetchone()
    return get_run(conn, int(row["id"])) if row is not None else None


def get_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> Suggestion | None:
    """One suggestion row, or None."""
    row = conn.execute(
        "SELECT id, run_id, question, domain, concepts, source, hook, bridge_from, state,"
        " question_id FROM suggestions WHERE id = ?",
        (suggestion_id,),
    ).fetchone()
    return _suggestion_from_row(row) if row is not None else None


async def accept(
    conn: sqlite3.Connection,
    suggestion_ids: list[int],
    *,
    vault: VaultStore,
    writer: VaultWriter,
    now: datetime,
) -> list[str]:
    """Write the selected suggestions into the vault and mark them accepted.

    Returns their question ids in the given order, including already accepted
    ones. Dismissed suggestions and unknown ids are skipped. Raises
    VaultWriteError without any state change when the commit or push fails.
    """
    created = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    existing_ids = vault.existing_ids()
    prepared: list[tuple[Suggestion, FileChange]] = []
    written_ids: list[str] = []
    for suggestion_id in suggestion_ids:
        row = conn.execute(
            "SELECT id, run_id, question, domain, concepts, source, hook, bridge_from, state,"
            " question_id FROM suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        if row is None:
            continue
        if row["state"] == "accepted":
            if row["question_id"]:
                written_ids.append(str(row["question_id"]))
            continue
        if row["state"] != "new":
            continue
        suggestion = _suggestion_from_row(row)
        qid = question_id(created.date(), suggestion.question, existing_ids)
        existing_ids.add(qid)
        question = Question(
            id=qid,
            status=Status.INBOX,
            domain=suggestion.domain,
            concepts=suggestion.concepts,
            source=suggestion.source,
            created=created,
            title=suggestion.question,
            preamble=_preamble(suggestion.hook, suggestion.bridge_from),
        )
        path, content = vault.render(question)
        change = FileChange(question_id=qid, path=path, content=content)
        prepared.append((suggestion, change))
        written_ids.append(qid)
    if not prepared:
        return written_ids
    message = (
        f"curio: add question {prepared[0][1].question_id}"
        if len(prepared) == 1
        else f"curio: add {len(prepared)} questions"
    )
    await writer.write([change for _, change in prepared], message)
    for suggestion, change in prepared:
        conn.execute(
            "UPDATE suggestions SET state = 'accepted', question_id = ? WHERE id = ?",
            (change.question_id, suggestion.id),
        )
    conn.commit()
    return written_ids


def dismiss(conn: sqlite3.Connection, suggestion_id: int) -> None:
    """Hide one suggestion from all run views."""
    conn.execute("UPDATE suggestions SET state = 'dismissed' WHERE id = ?", (suggestion_id,))
    conn.commit()


async def _generate_batch(
    conn: sqlite3.Connection,
    *,
    mode: Mode,
    input_value: str,
    llm: LLMClient,
    vault: VaultStore,
    books_path: Path,
) -> QuestionBatch:
    """Assemble prompt messages and call the LLM for one batch of questions."""
    profile = vault.read_profile()
    existing = [question.title for question in vault.list_questions()[0][:_EXISTING_MAX]]
    if mode == "source":
        loaded = BookStore(conn, books_path).chapter(int(input_value))
        if loaded is None:
            raise GenerationInputError("That chapter is no longer available.")
        book, chapter, text = loaded
        messages = question_source_messages(
            book_title=book.title,
            chapter_number=chapter.number,
            chapter_title=chapter.title,
            chapter_text=text[:_SOURCE_MAX_CHARS],
            existing=existing,
        )
    elif mode == "topic":
        messages = question_topic_messages(topic=input_value, existing=existing)
    else:
        if profile is None:
            raise GenerationInputError(f"No knowledge profile found at {vault.profile_path}.")
        messages = question_bridge_messages(focus=input_value or None, existing=existing)
    return await llm.generate(
        system=question_system(profile),
        messages=messages,
        schema=QuestionBatch,
        purpose="generation",
    )


def _store_batch(conn: sqlite3.Connection, run_id: int, batch: QuestionBatch) -> None:
    for generated in batch.questions:
        conn.execute(
            "INSERT INTO suggestions"
            " (run_id, question, domain, concepts, source, hook, bridge_from, state, question_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'new', NULL)",
            (
                run_id,
                generated.question,
                generated.domain,
                json.dumps(generated.concepts),
                generated.source,
                generated.hook,
                generated.bridge_from,
            ),
        )


def _fail_run(conn: sqlite3.Connection, run_id: int, error: str) -> None:
    conn.execute(
        "UPDATE generation_runs SET state = 'failed', error = ? WHERE id = ?", (error, run_id)
    )
    conn.commit()


def _chapter_id(conn: sqlite3.Connection, raw: str) -> int | None:
    """Numeric id of a chapter that belongs to a ready book, or None."""
    try:
        chapter_id = int(raw)
    except ValueError:
        return None
    row = conn.execute(
        "SELECT 1 FROM chapters JOIN books ON books.id = chapters.book_id"
        " WHERE chapters.id = ? AND books.state = 'ready'",
        (chapter_id,),
    ).fetchone()
    return chapter_id if row is not None else None


def mode_value(raw: str) -> Mode:
    """Cast a stored or submitted mode string to the Mode literal."""
    for mode in MODES:
        if raw == mode:
            return mode
    raise GenerationInputError("Choose one of Source, Topic or Bridge.")


def _preamble(hook: str, bridge_from: str | None) -> str:
    preamble = f"*{hook}*"
    if bridge_from:
        return f"{preamble}\n\nBuilds on: {bridge_from}"
    return preamble


def _suggestion_from_row(row: sqlite3.Row) -> Suggestion:
    concepts = row["concepts"]
    return Suggestion(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        question=str(row["question"]),
        domain=str(row["domain"]),
        concepts=json.loads(str(concepts)),
        source=row["source"],
        hook=str(row["hook"]),
        bridge_from=row["bridge_from"],
        state=str(row["state"]),
        question_id=row["question_id"],
    )
