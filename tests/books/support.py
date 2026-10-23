"""Shared helpers and fakes for the books feature tests."""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from curio_app.books.store import BookStore

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

PDF_BYTES = b"%PDF-1.4 invented minimal pdf content for tests only"


@dataclass
class FakeConverter:
    """Fake converter client recording starts or raising an error."""

    started: list[str] = field(default_factory=list)
    error: Exception | None = None

    def start(self, book_id: str) -> None:
        if self.error is not None:
            raise self.error
        self.started.append(book_id)


def write_converter_output(
    store: BookStore,
    book_id: str,
    markdown: str,
    *,
    state: str = "done",
    error: str | None = None,
) -> None:
    """Pretend the converter produced its files for one book."""
    book_dir = store.source_path(book_id).parent
    status = {"state": state, "error": error}
    (book_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    (book_dir / "book.md").write_text(markdown, encoding="utf-8")


def upload(
    client: "TestClient",
    *,
    title: str = "Test Book",
    content: bytes = PDF_BYTES,
    filename: str = "book.pdf",
) -> "object":
    """POST /books with a multipart form, without following the redirect."""
    return client.post(
        "/books",
        data={"title": title},
        files={"file": (filename, content, "application/pdf")},
        follow_redirects=False,
    )


def make_ready_book(store: BookStore, title: str, markdown: str) -> str:
    """Create a book and push it through the converter output and finalize step."""
    book = store.create(title, "book.pdf")
    write_converter_output(store, book.id, markdown)
    store.finalize(book.id)
    return book.id
