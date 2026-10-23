"""Generate page, run fragments and suggestion actions."""

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from starlette.templating import Jinja2Templates

from curio_app.books.store import BOOK_STATE_READY, Book, BookStore, ChapterRow
from curio_app.config import Settings
from curio_app.db import get_db
from curio_app.llm.base import LLMClient
from curio_app.services import generation
from curio_app.services.generation import GenerationInputError, Run
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriteError, VaultWriter
from curio_app.web.deps import (
    get_book_store,
    get_llm,
    get_templates,
    get_vault_store,
    get_vault_writer,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["generate"])

BookChapters = tuple[Book, list[ChapterRow]]


def _render(
    templates: Jinja2Templates,
    request: Request,
    name: str,
    context: dict[str, object],
) -> Response:
    return templates.TemplateResponse(request=request, name=name, context=context)


@router.get("/generate")
async def generate_page(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    book_store: Annotated[BookStore, Depends(get_book_store)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Generate page with the mode segments, one form per mode and the latest run."""
    selected_chapter = _requested_chapter(book_store, request.query_params.get("chapter"))
    has_ready_books = any(book.state == BOOK_STATE_READY for book in book_store.list())
    mode = request.query_params.get("mode")
    if mode not in generation.MODES:
        mode = "source" if selected_chapter is not None or has_ready_books else "topic"
    book_options: list[BookChapters] = [
        (book, book_store.chapters(book.id))
        for book in book_store.list()
        if book.state == BOOK_STATE_READY and book_store.chapters(book.id)
    ]
    context: dict[str, object] = {
        "active_tab": "generate",
        "mode": mode,
        "book_options": book_options,
        "has_ready_books": has_ready_books,
        "selected_chapter": selected_chapter,
        "run": generation.latest_run(conn),
    }
    return _render(templates, request, "generate.html", context)


@router.post("/generate")
async def start_generation(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    llm: Annotated[LLMClient, Depends(get_llm)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
    mode: Annotated[str, Form()] = "",
    chapter_id: Annotated[str, Form()] = "",
    topic: Annotated[str, Form()] = "",
    focus: Annotated[str, Form()] = "",
) -> Response:
    """Record a run and start the LLM generation in the background."""
    input_value = {"source": chapter_id, "topic": topic, "focus": focus}.get(mode)
    try:
        if input_value is None:
            raise GenerationInputError("Choose one of Source, Topic or Bridge.")
        run_id = generation.start_run(conn, generation.mode_value(mode), input_value)
    except GenerationInputError as exc:
        return _render(templates, request, "partials/run.html", {"run_error": str(exc)})
    settings: Settings = request.app.state.settings
    generation.schedule_execution(
        run_id, db_path=settings.db_path, llm=llm, vault=vault, books_path=settings.books_path
    )
    run = generation.get_run(conn, run_id)
    return _render(templates, request, "partials/run.html", {"run": run})


@router.get("/generate/runs/{run_id}")
async def run_fragment(
    request: Request,
    run_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Fragment of one run: spinner while running, error, or the suggestion cards."""
    run = generation.get_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _render(templates, request, "partials/run.html", {"run": run})


@router.post("/suggestions/accept")
async def accept_selected(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    writer: Annotated[VaultWriter, Depends(get_vault_writer)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
    suggestion_id: Annotated[list[int] | None, Form()] = None,
) -> Response:
    """Write every selected suggestion into the vault as one commit."""
    context: dict[str, object] = {}
    try:
        await generation.accept(
            conn, suggestion_id or [], vault=vault, writer=writer, now=datetime.now(UTC)
        )
    except VaultWriteError as exc:
        logger.warning("Vault write failed: %s", exc.detail)
        context["run_error"] = exc.message
    run: Run | None = generation.latest_run(conn)
    if run is not None:
        context["run"] = run
    return _render(templates, request, "partials/run.html", context)


@router.post("/suggestions/{suggestion_id}/accept")
async def accept_suggestion(
    request: Request,
    suggestion_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    writer: Annotated[VaultWriter, Depends(get_vault_writer)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Write one suggestion into the vault and swap its card."""
    try:
        await generation.accept(
            conn, [suggestion_id], vault=vault, writer=writer, now=datetime.now(UTC)
        )
    except VaultWriteError as exc:
        logger.warning("Vault write failed: %s", exc.detail)
        suggestion = generation.get_suggestion(conn, suggestion_id)
        if suggestion is None:
            raise HTTPException(status_code=404, detail="Not found") from exc
        return _render(
            templates,
            request,
            "partials/suggestion.html",
            {"s": suggestion, "suggestion_error": exc.message},
        )
    suggestion = generation.get_suggestion(conn, suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _render(templates, request, "partials/suggestion.html", {"s": suggestion})


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(
    suggestion_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Response:
    """Drop one suggestion; the empty response removes its card."""
    generation.dismiss(conn, suggestion_id)
    return Response(status_code=200)


def _requested_chapter(book_store: BookStore, raw: str | None) -> int | None:
    """The requested chapter id as int when it exists in a ready book."""
    if not raw:
        return None
    try:
        chapter_id = int(raw)
    except ValueError:
        return None
    ready_chapter_ids = {
        chapter.id
        for book in book_store.list()
        if book.state == BOOK_STATE_READY
        for chapter in book_store.chapters(book.id)
    }
    return chapter_id if chapter_id in ready_chapter_ids else None
