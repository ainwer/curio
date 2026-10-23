"""Book upload, conversion status and book detail routes."""

import sqlite3
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from curio_app.books.converter import (
    CONVERTER_UNREACHABLE_MESSAGE,
    ConverterClient,
    ConverterUnavailable,
    get_converter,
)
from curio_app.books.store import (
    BOOK_STATE_CONVERTING,
    BOOK_STATE_FAILED,
    BOOK_STATE_READY,
    Book,
    BookStore,
)
from curio_app.config import Settings
from curio_app.db import get_db

router = APIRouter(tags=["books"])

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_PDF_MAGIC = b"%PDF-"
_MAX_TITLE_CHARS = 200
_READ_CHARS_PER_MINUTE = 1500
_NOT_PDF_MESSAGE = "The selected file is not a PDF."


@dataclass(frozen=True)
class ChapterView:
    """View model of one chapter for the book page."""

    chapter_id: int
    number: int
    title: str
    minutes: int


def get_store_conn(
    request: Request, conn: Annotated[sqlite3.Connection, Depends(get_db)]
) -> BookStore:
    """FastAPI dependency for BookStore."""
    settings: Settings = request.app.state.settings
    return BookStore(conn, settings.books_path)


def _render_books(
    request: Request, books: list[Book], error: str | None, *, status_code: int = 200
) -> Response:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="books.html",
        context={"active_tab": "books", "books": books, "error": error},
        status_code=status_code,
    )


def _load_book(store: BookStore, book_id: str) -> Book:
    book = store.get(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Not found")
    return book


@router.get("/books")
async def books_page(
    request: Request, store: Annotated[BookStore, Depends(get_store_conn)]
) -> Response:
    """Books page with the upload form and the list of books."""
    return _render_books(request, store.list(), None)


@router.post("/books")
async def upload_book(
    request: Request,
    store: Annotated[BookStore, Depends(get_store_conn)],
    converter: Annotated[ConverterClient, Depends(get_converter)],
    title: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Stream the upload to source.pdf and hand the book to the converter."""
    stripped_title = title.strip()
    if not stripped_title:
        return _render_books(request, store.list(), "Please enter a title.", status_code=400)
    if len(stripped_title) > _MAX_TITLE_CHARS:
        return _render_books(
            request,
            store.list(),
            "The title is too long. The maximum is 200 characters.",
            status_code=400,
        )
    if file is None or not file.filename:
        return _render_books(request, store.list(), "Please choose a PDF file.", status_code=400)
    settings: Settings = request.app.state.settings
    max_upload_mb = settings.max_upload_mb
    max_bytes = max_upload_mb * 1024 * 1024
    book = store.create(stripped_title, file.filename)
    error = await _save_pdf(file, store.source_path(book.id), max_bytes, max_upload_mb)
    if error is not None:
        store.delete(book.id)
        await file.close()
        return _render_books(request, store.list(), error, status_code=400)
    try:
        converter.start(book.id)
    except ConverterUnavailable:
        store.set_state(book.id, BOOK_STATE_FAILED, CONVERTER_UNREACHABLE_MESSAGE)
    else:
        store.set_state(book.id, BOOK_STATE_CONVERTING)
    await file.close()
    return RedirectResponse(url="/books", status_code=303)


async def _save_pdf(file: UploadFile, destination: Path, max_bytes: int, max_mb: int) -> str | None:
    error: str | None = None
    header = bytearray()
    written = 0
    with destination.open("wb") as out:
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            if len(header) < len(_PDF_MAGIC):
                header.extend(chunk[: len(_PDF_MAGIC) - len(header)])
                if len(header) == len(_PDF_MAGIC) and bytes(header) != _PDF_MAGIC:
                    error = _NOT_PDF_MESSAGE
                    break
            written += len(chunk)
            if written > max_bytes:
                error = f"The file is larger than the limit of {max_mb} MB."
                break
            out.write(chunk)
    if error is None and len(header) < len(_PDF_MAGIC):
        error = _NOT_PDF_MESSAGE
    return error


@router.get("/books/{book_id}/status")
async def book_status(
    request: Request,
    book_id: str,
    store: Annotated[BookStore, Depends(get_store_conn)],
) -> Response:
    """Sync one book with the converter status and return its row fragment."""
    book = _load_book(store, book_id)
    current = store.sync(book.id)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="partials/book_row.html",
        context={"book": current},
    )


@router.get("/books/{book_id}")
async def book_page(
    request: Request,
    book_id: str,
    store: Annotated[BookStore, Depends(get_store_conn)],
) -> Response:
    """Book page with state, error handling and the chapter list."""
    book = _load_book(store, book_id)
    chapters: list[ChapterView] = []
    if book.state == BOOK_STATE_READY:
        for row in store.chapters(book.id):
            minutes = max(1, ceil(row.char_count / _READ_CHARS_PER_MINUTE))
            chapters.append(
                ChapterView(
                    chapter_id=row.id,
                    number=row.number,
                    title=row.title,
                    minutes=minutes,
                )
            )
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="book.html",
        context={"active_tab": "books", "book": book, "chapters": chapters},
    )


@router.post("/books/{book_id}/retry")
async def retry_conversion(
    book_id: str,
    store: Annotated[BookStore, Depends(get_store_conn)],
    converter: Annotated[ConverterClient, Depends(get_converter)],
) -> Response:
    """Start the conversion again for a failed book."""
    book = _load_book(store, book_id)
    try:
        converter.start(book.id)
    except ConverterUnavailable:
        store.set_state(book.id, BOOK_STATE_FAILED, CONVERTER_UNREACHABLE_MESSAGE)
    else:
        store.set_state(book.id, BOOK_STATE_CONVERTING)
    return RedirectResponse(url=f"/books/{book.id}", status_code=303)
