"""Question page by status: gut answer, streamed tutor chat and the done view."""

import asyncio
import contextlib
import html
import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.templating import Jinja2Templates

from curio_app.books.store import BookStore
from curio_app.config import Settings
from curio_app.db import get_db
from curio_app.domain import Question, Status
from curio_app.llm.base import LLMClient
from curio_app.services import tutor
from curio_app.services.streams import StreamEvent, StreamRegistry, TurnStream
from curio_app.vault.store import QuestionNotFound, VaultStore
from curio_app.vault.writer import VaultWriter
from curio_app.web.deps import (
    get_book_store,
    get_llm,
    get_stream_registry,
    get_templates,
    get_vault_store,
    get_vault_writer,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["questions"])

_PING_SECONDS = 15.0
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def get_settings_from_app(request: Request) -> Settings:
    """Application settings from app.state."""
    settings: Settings = request.app.state.settings
    return settings


@router.get("/questions/{question_id}")
async def question_page(
    request: Request,
    question_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    writer: Annotated[VaultWriter, Depends(get_vault_writer)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """One question: gut answer form (inbox), tutor chat (researching) or read-only view."""
    question = _question(vault, question_id)
    context = _page_context(conn, question, writer)
    return templates.TemplateResponse(request=request, name="question.html", context=context)


@router.post("/questions/{question_id}/gut-answer")
async def submit_gut_answer(
    request: Request,
    question_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    writer: Annotated[VaultWriter, Depends(get_vault_writer)],
    books: Annotated[BookStore, Depends(get_book_store)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
    llm: Annotated[LLMClient, Depends(get_llm)],
    registry: Annotated[StreamRegistry, Depends(get_stream_registry)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    gut_answer: Annotated[str, Form()] = "",
) -> Response:
    """Commit the gut answer, create the tutor session and start the first tutor turn."""
    question = _question(vault, question_id)
    try:
        vault_error = await tutor.submit_gut_answer(
            conn,
            question,
            gut_answer,
            vault=vault,
            writer=writer,
            books=books,
            now=datetime.now(UTC),
        )
    except tutor.GutAnswerError as exc:
        context = _page_context(conn, question, writer)
        context["form_error"] = str(exc)
        return templates.TemplateResponse(
            request=request, name="question.html", context=context, status_code=400
        )
    _start_stream(conn, registry, settings, llm, question_id)
    if vault_error is not None:
        logger.warning("Vault write failed for question %s: %s", question_id, vault_error)
    return RedirectResponse(url=f"/questions/{question_id}", status_code=303)


@router.post("/questions/{question_id}/messages")
async def post_message(
    question_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
    llm: Annotated[LLMClient, Depends(get_llm)],
    registry: Annotated[StreamRegistry, Depends(get_stream_registry)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    text: Annotated[str, Form()] = "",
) -> Response:
    """Store a user message and return the user bubble plus the live assistant bubble."""
    _question(vault, question_id)
    try:
        user_id, assistant_id = tutor.start_user_turn(conn, question_id, text)
    except tutor.TutorInputError as exc:
        return HTMLResponse(
            _render(templates, "partials/chat_error.html", message=str(exc)), status_code=400
        )
    _start_stream_id(registry, settings, llm, question_id, assistant_id)
    user_message = tutor.ChatMessage(
        id=user_id, role="user", content=text.strip(), state="done", created_at=""
    )
    fragment = _render(
        templates,
        "partials/chat_turn.html",
        question_id=question_id,
        user_message=user_message,
        stream_id=assistant_id,
    )
    return HTMLResponse(fragment)


@router.get("/questions/{question_id}/messages/{message_id}/stream")
async def stream_message(
    question_id: str,
    message_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
    registry: Annotated[StreamRegistry, Depends(get_stream_registry)],
) -> Response:
    """SSE stream of one assistant turn; the payload survives a dropped connection."""
    _question(vault, question_id)
    message = tutor.message(conn, question_id, message_id)
    if message is None or message.role != "assistant":
        raise HTTPException(status_code=404, detail="Not found")
    turn = registry.get(message_id)
    if turn is None or turn.done:
        event = "error" if message.state != "done" else "done"
        payload = _bubble_html(templates, message, question_id, turn.error if turn else None)
        return StreamingResponse(
            _single_event(event, payload), media_type="text/event-stream", headers=_SSE_HEADERS
        )
    return StreamingResponse(
        _turn_events(conn, templates, question_id, message_id, turn),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/questions/{question_id}/messages/{message_id}/retry")
async def retry_message(
    question_id: str,
    message_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
    llm: Annotated[LLMClient, Depends(get_llm)],
    registry: Annotated[StreamRegistry, Depends(get_stream_registry)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> Response:
    """Retry a failed assistant turn; returns the fresh live bubble."""
    _question(vault, question_id)
    message = tutor.message(conn, question_id, message_id)
    if message is None or message.role != "assistant":
        raise HTTPException(status_code=404, detail="Not found")
    tutor.retry_assistant(conn, message_id)
    _start_stream_id(registry, settings, llm, question_id, message_id)
    return HTMLResponse(
        _render(
            templates,
            "partials/bubble_live.html",
            question_id=question_id,
            stream_id=message_id,
        )
    )


async def _turn_events(
    conn: sqlite3.Connection,
    templates: Jinja2Templates,
    question_id: str,
    message_id: int,
    turn: TurnStream,
) -> AsyncIterator[str]:
    """SSE frames for a live turn; a comment ping keeps idle connections open."""
    queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in turn.subscribe():
                queue.put_nowait(event)
        finally:
            queue.put_nowait(None)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_PING_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if event is None:
                return
            name, payload = event
            if name == "token":
                yield _frame(name, html.escape(payload, quote=False))
                continue
            message = tutor.message(conn, question_id, message_id)
            error_text = turn.error if turn.error is not None else None
            html_payload = (
                _bubble_html(templates, message, question_id, error_text)
                if message is not None
                else payload
            )
            yield _frame(name, html_payload)
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task


def _frame(name: str, payload: str) -> str:
    """One SSE event: event line, one data line per payload line, blank line."""
    lines = "".join(f"data: {line}\n" for line in payload.split("\n"))
    return f"event: {name}\n{lines}\n"


def _single_event(name: str, payload: str) -> AsyncIterator[str]:
    async def body() -> AsyncIterator[str]:
        yield _frame(name, payload)

    return body()


def _start_stream(
    conn: sqlite3.Connection,
    registry: StreamRegistry,
    settings: Settings,
    llm: LLMClient,
    question_id: str,
) -> None:
    message_id = tutor.streaming_message_id(conn, question_id)
    if message_id is not None:
        _start_stream_id(registry, settings, llm, question_id, message_id)


def _start_stream_id(
    registry: StreamRegistry, settings: Settings, llm: LLMClient, question_id: str, message_id: int
) -> None:
    registry.start(message_id, db_path=settings.db_path, llm=llm, question_id=question_id)


def _question(vault: VaultStore, question_id: str) -> Question:
    try:
        return vault.get(question_id)
    except QuestionNotFound as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc


def _page_context(
    conn: sqlite3.Connection, question: Question, writer: VaultWriter
) -> dict[str, object]:
    """Template context for the question page, shaped by the effective status."""
    status = tutor.effective_status(question, conn)
    context: dict[str, object] = {
        "active_tab": "inbox",
        "question": question,
        "created": question.created.date().isoformat(),
        "effective_status": status,
        "form_error": None,
    }
    if status is Status.RESEARCHING:
        context.update(
            gut_answer=question.gut_answer() or "",
            messages=tutor.history(conn, question.id),
            live_stream_id=tutor.streaming_message_id(conn, question.id),
            vault_warning=_has_draft(writer, question.id),
        )
    elif status is Status.DONE:
        context.update(
            gut_answer=question.gut_answer() or "",
            insight=question.insight() or "",
            cards=question.cards(),
        )
    return context


def _has_draft(writer: VaultWriter, question_id: str) -> bool:
    return any(draft.question_id == question_id for draft in writer.list_drafts())


def _render(templates: Jinja2Templates, name: str, **context: object) -> str:
    return templates.env.get_template(name).render(**context)


def _bubble_html(
    templates: Jinja2Templates, message: tutor.ChatMessage, question_id: str, error_text: str | None
) -> str:
    return _render(
        templates,
        "partials/bubble.html",
        message=message,
        question_id=question_id,
        error_text=error_text,
    )
