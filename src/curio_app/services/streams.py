"""Background assistant turns: the LLM streams into SQLite, browsers subscribe via SSE.

Streaming never depends on the browser connection: a dropped phone connection does not
lose the reply, and a late subscriber receives the text accumulated so far.
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal, cast

from curio_app.db import connect
from curio_app.llm.base import LLMClient, LLMError, Message
from curio_app.llm.prompts import tutor_messages, tutor_system

logger = logging.getLogger(__name__)

StreamEventName = Literal["token", "done", "error"]
StreamEvent = tuple[StreamEventName, str]

_PERSIST_INTERVAL_SECONDS = 1.0
_UNEXPECTED_ERROR = "Unexpected error."


class TurnStream:
    """One running assistant turn and its subscribers.

    ``subscribe`` yields one ``token`` event with the text accumulated so far (a single
    chunk), then live chunks, then a final ``done`` or ``error`` event.
    """

    def __init__(self) -> None:
        self.text = ""
        self.done = False
        self.error: str | None = None
        self._subscribers: set[asyncio.Queue[StreamEvent]] = set()

    async def subscribe(self) -> AsyncIterator[StreamEvent]:
        """Follow the turn from the current position."""
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        if self.done:
            if self.text:
                yield ("token", self.text)
            if self.error is not None:
                yield ("error", self.error)
            else:
                yield ("done", "")
            return
        self._subscribers.add(queue)
        try:
            if self.text:
                yield ("token", self.text)
            while True:
                event = await queue.get()
                yield event
                if event[0] in ("done", "error"):
                    return
        finally:
            self._subscribers.discard(queue)

    def _append(self, chunk: str) -> None:
        self.text += chunk
        self._publish(("token", chunk))

    def _finish(self, error: str | None) -> None:
        self.done = True
        self.error = error
        self._publish(("error", error) if error is not None else ("done", ""))

    def _publish(self, event: StreamEvent) -> None:
        for queue in self._subscribers:
            queue.put_nowait(event)


class StreamRegistry:
    """Owns the background stream task of every running assistant turn."""

    def __init__(self) -> None:
        self._streams: dict[int, TurnStream] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def start(
        self,
        message_id: int,
        *,
        db_path: Path,
        llm: LLMClient,
        question_id: str,
    ) -> TurnStream:
        """Start a background turn for *message_id*; returns its live stream."""
        stream = TurnStream()
        self._streams[message_id] = stream
        task = asyncio.create_task(
            run_turn(message_id, stream, db_path=db_path, llm=llm, question_id=question_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return stream

    def get(self, message_id: int) -> TurnStream | None:
        """The live stream of a message, or None."""
        return self._streams.get(message_id)


async def run_turn(
    message_id: int,
    stream: TurnStream,
    *,
    db_path: Path,
    llm: LLMClient,
    question_id: str,
) -> None:
    """Stream one assistant turn into the database; never raises."""
    conn = connect(db_path)
    try:
        try:
            context = _context(conn, question_id)
            history = _done_history(conn, question_id, before=message_id)
            messages = tutor_messages(context=context, history=history)
            system = tutor_system()
            await _stream_chunks(conn, message_id, stream, llm, system, messages)
        except LLMError as exc:
            logger.warning("Tutor turn %s failed: %s", message_id, type(exc).__name__)
            _persist(conn, message_id, stream.text, "failed")
            stream._finish(str(exc))
        except Exception as exc:
            logger.warning("Tutor turn %s failed: %s", message_id, type(exc).__name__)
            _persist(conn, message_id, stream.text, "failed")
            stream._finish(_UNEXPECTED_ERROR)
        else:
            stream._finish(None)
    finally:
        conn.close()


def mark_stale_streams_failed(conn: sqlite3.Connection) -> None:
    """Mark messages left streaming by a previous process as failed (startup)."""
    conn.execute("UPDATE chat_messages SET state = 'failed' WHERE state = 'streaming'")
    conn.commit()


async def _stream_chunks(
    conn: sqlite3.Connection,
    message_id: int,
    stream: TurnStream,
    llm: LLMClient,
    system: str,
    messages: list[Message],
) -> None:
    """Run the LLM stream, publish chunks and persist the text at most every second."""
    last_persist = time.monotonic()
    async for chunk in llm.stream(system=system, messages=messages):
        stream._append(chunk)
        now = time.monotonic()
        if now - last_persist >= _PERSIST_INTERVAL_SECONDS:
            _persist(conn, message_id, stream.text, "streaming")
            last_persist = now
    _persist(conn, message_id, stream.text, "done")


def _context(conn: sqlite3.Connection, question_id: str) -> str:
    row = conn.execute(
        "SELECT context FROM tutor_sessions WHERE question_id = ?", (question_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No tutor session for {question_id}")
    return str(row["context"])


def _done_history(
    conn: sqlite3.Connection, question_id: str, *, before: int
) -> list[tuple[Literal["user", "assistant"], str]]:
    rows = conn.execute(
        "SELECT role, content FROM chat_messages"
        " WHERE question_id = ? AND state = 'done' AND id < ? ORDER BY id",
        (question_id, before),
    ).fetchall()
    return [
        (cast("Literal['user', 'assistant']", str(row["role"])), str(row["content"]))
        for row in rows
    ]


def _persist(conn: sqlite3.Connection, message_id: int, content: str, state: str) -> None:
    """Persist content and state; a failure here must not lose the stream outcome."""
    try:
        conn.execute(
            "UPDATE chat_messages SET content = ?, state = ? WHERE id = ?",
            (content, state, message_id),
        )
        conn.commit()
    except sqlite3.Error:
        logger.warning("Tutor turn %s could not persist content", message_id)
