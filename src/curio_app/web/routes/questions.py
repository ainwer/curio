"""Question page by status: gut answer, tutor chat, wrap-up curation and the done view."""

import asyncio
import contextlib
import dataclasses
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
from curio_app.domain import CardType, Question, Status
from curio_app.llm.base import LLMClient
from curio_app.services import tutor
from curio_app.services import wrapup as wrapups
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

_CARD_TYPE_LABELS: tuple[tuple[str, str], ...] = tuple(
    (card_type.value, card_type.value.capitalize()) for card_type in CardType
)


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
    view: str = "",
) -> Response:
    """One question: gut answer form (inbox), tutor chat or curation (researching), done view."""
    question = _load(conn, vault, question_id)
    if not _curate_allowed(conn, question, view):
        return RedirectResponse(url=f"/questions/{question_id}", status_code=303)
    context = _page_context(conn, question, writer, view)
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


@router.post("/questions/{question_id}/wrapup")
async def wrap_up_question(
    request: Request,
    question_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
    llm: Annotated[LLMClient, Depends(get_llm)],
) -> Response:
    """Summarize the session and (re)place the card proposals, then show the curation view."""
    question = _load(conn, vault, question_id)
    try:
        await wrapups.create_wrapup(conn, question, llm=llm)
    except wrapups.WrapupError as exc:
        zone = _wrapup_zone_fragment(templates, conn, question, zone_error=str(exc))
        return HTMLResponse(zone, status_code=400)
    logger.info("Wrap-up stored for question %s", question_id)
    return _redirect(request, f"/questions/{question_id}?view=curate")


@router.post("/questions/{question_id}/save")
async def save_question(
    request: Request,
    question_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    writer: Annotated[VaultWriter, Depends(get_vault_writer)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Write insight and accepted cards to the vault; a pending draft survives failures."""
    question = _load(conn, vault, question_id)
    try:
        vault_error = await wrapups.save(conn, question, vault=vault, writer=writer)
    except wrapups.WrapupError as exc:
        context = _page_context(conn, question, writer, view="curate")
        context["form_error"] = str(exc)
        return templates.TemplateResponse(
            request=request, name="question.html", context=context, status_code=400
        )
    if vault_error is not None:
        logger.warning("Vault write failed for question %s: %s", question_id, vault_error)
    return RedirectResponse(url=f"/questions/{question_id}", status_code=303)


@router.post("/questions/{question_id}/cards/{card_id}/accept")
async def accept_card(
    question_id: str,
    card_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Accept one card draft; returns the updated card partial plus the counter."""
    _owned_card(conn, question_id, card_id)
    wrapups.accept_card(conn, card_id)
    return _card_response(templates, conn, question_id, card_id)


@router.post("/questions/{question_id}/cards/{card_id}/discard")
async def discard_card(
    question_id: str,
    card_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Discard one card draft; returns the updated card partial plus the counter."""
    _owned_card(conn, question_id, card_id)
    wrapups.discard_card(conn, card_id)
    return _card_response(templates, conn, question_id, card_id)


@router.post("/questions/{question_id}/cards/{card_id}/propose")
async def propose_card(
    question_id: str,
    card_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Re-propose a discarded card draft (undo); returns the partial plus the counter."""
    _owned_card(conn, question_id, card_id)
    wrapups.propose_card(conn, card_id)
    return _card_response(templates, conn, question_id, card_id)


@router.get("/questions/{question_id}/cards/{card_id}")
async def show_card(
    question_id: str,
    card_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """The card partial on its own (canceling the inline edit form)."""
    card = _owned_card(conn, question_id, card_id)
    return HTMLResponse(_card_fragment(templates, question_id, card))


@router.post("/questions/{question_id}/cards/{card_id}/edit")
async def edit_card(
    question_id: str,
    card_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
    edit_submitted: Annotated[str, Form()] = "",
    type_value: Annotated[str, Form()] = "",
    front: Annotated[str, Form()] = "",
    back: Annotated[str, Form()] = "",
    source: Annotated[str, Form()] = "",
) -> Response:
    """Show the inline edit form, or store the edited card (which accepts it)."""
    card = _owned_card(conn, question_id, card_id)
    if edit_submitted != "1":
        return HTMLResponse(_render_edit_form(templates, question_id, card, error=None))
    try:
        card_type = CardType(type_value)
    except ValueError:
        card_type = card.type
    try:
        wrapups.update_card(conn, card_id, type=card_type, front=front, back=back, source=source)
    except wrapups.WrapupError as exc:
        edited = dataclasses.replace(
            card,
            type=card_type,
            front=front,
            back=back,
            source=_optional_source(source),
        )
        return HTMLResponse(
            _render_edit_form(templates, question_id, edited, error=str(exc)), status_code=400
        )
    return _card_response(templates, conn, question_id, card_id)


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


def _load(conn: sqlite3.Connection, vault: VaultStore, question_id: str) -> Question:
    """The current question content: a pending vault draft wins over the vault file."""
    try:
        return tutor.effective_question(vault, question_id, conn)
    except QuestionNotFound as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc


def _curate_allowed(conn: sqlite3.Connection, question: Question, view: str) -> bool:
    """The curation view needs a researching question with a stored wrap-up."""
    return not (
        view == "curate"
        and not (
            tutor.effective_status(question, conn) is Status.RESEARCHING
            and wrapups.get_wrapup(conn, question.id) is not None
        )
    )


def _redirect(request: Request, url: str) -> Response:
    """303 for plain clients; HX-Redirect for HTMX requests (the XHR would follow a 303)."""
    if request.headers.get("HX-Request") == "true":
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = url
        return response
    return RedirectResponse(url=url, status_code=303)


def _page_context(
    conn: sqlite3.Connection, question: Question, writer: VaultWriter, view: str = ""
) -> dict[str, object]:
    """Template context for the question page, shaped by the effective status."""
    status = tutor.effective_status(question, conn)
    context: dict[str, object] = {
        "active_tab": "inbox",
        "question": question,
        "created": question.created.date().isoformat(),
        "effective_status": status,
        "form_error": None,
        "view": view,
    }
    pending_draft = _has_draft(writer, question.id)
    if status is Status.RESEARCHING and view == "curate":
        wrapup = wrapups.get_wrapup(conn, question.id)
        if wrapup is not None:
            context.update(
                wrapup=wrapup,
                accepted_count=sum(1 for card in wrapup.cards if card.state == "accepted"),
                zone_error=None,
            )
            return context
    if status is Status.RESEARCHING:
        context.update(
            gut_answer=question.gut_answer() or "",
            messages=tutor.history(conn, question.id),
            live_stream_id=tutor.streaming_message_id(conn, question.id),
            can_wrap=_has_done_assistant(conn, question.id),
            has_wrapup=wrapups.get_wrapup(conn, question.id) is not None,
            zone_error=None,
            vault_warning=pending_draft,
        )
    elif status is Status.DONE:
        context.update(
            gut_answer=question.gut_answer() or "",
            insight=question.insight() or "",
            cards=question.cards(),
            vault_warning=pending_draft,
        )
    return context


def _has_draft(writer: VaultWriter, question_id: str) -> bool:
    return any(draft.question_id == question_id for draft in writer.list_drafts())


def _has_done_assistant(conn: sqlite3.Connection, question_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM chat_messages WHERE question_id = ? AND role = 'assistant'"
        " AND state = 'done' LIMIT 1",
        (question_id,),
    ).fetchone()
    return row is not None


def _wrapup_zone_fragment(
    templates: Jinja2Templates, conn: sqlite3.Connection, question: Question, zone_error: str
) -> str:
    return _render(
        templates,
        "partials/wrapup_zone.html",
        question=question,
        can_wrap=_has_done_assistant(conn, question.id),
        has_wrapup=wrapups.get_wrapup(conn, question.id) is not None,
        zone_error=zone_error,
    )


def _owned_card(
    conn: sqlite3.Connection, question_id: str, card_id_value: str
) -> wrapups.DraftCard:
    card = wrapups.get_card(conn, card_id_value)
    if card is None or card.question_id != question_id:
        raise HTTPException(status_code=404, detail="Not found")
    return card


def _card_response(
    templates: Jinja2Templates, conn: sqlite3.Connection, question_id: str, card_id_value: str
) -> Response:
    """The updated card partial plus an out-of-band swap of the footer (counter + save button)."""
    card = _owned_card(conn, question_id, card_id_value)
    accepted = sum(1 for item in wrapups.list_cards(conn, question_id) if item.state == "accepted")
    fragment = _card_fragment(templates, question_id, card)
    footer = _render(
        templates,
        "partials/curate_footer.html",
        accepted_count=accepted,
        question_id=question_id,
        oob=True,
    )
    return HTMLResponse(fragment + footer)


def _card_fragment(templates: Jinja2Templates, question_id: str, card: wrapups.DraftCard) -> str:
    return _render(templates, "partials/card_draft.html", card=card, question_id=question_id)


def _render_edit_form(
    templates: Jinja2Templates, question_id: str, card: wrapups.DraftCard, error: str | None
) -> str:
    return _render(
        templates,
        "partials/card_edit_form.html",
        card=card,
        question_id=question_id,
        error=error,
        card_types=_CARD_TYPE_LABELS,
    )


def _optional_source(source: str) -> str | None:
    return source if source.strip() else None


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
