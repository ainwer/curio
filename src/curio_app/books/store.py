"""Book directories, SQLite-backed metadata, converter status files and chapters."""

import json
import re
import secrets
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from curio_app.books.chapters import split_chapters

BOOK_ID_PATTERN = re.compile(r"^b-[a-z0-9-]{1,40}-[0-9a-f]{6}$")

BOOK_STATE_UPLOADED = "uploaded"
BOOK_STATE_CONVERTING = "converting"
BOOK_STATE_READY = "ready"
BOOK_STATE_FAILED = "failed"

CONVERTER_STATE_DONE = "done"
CONVERTER_STATE_FAILED = "failed"

_SOURCE_FILE = "source.pdf"
_STATUS_FILE = "status.json"
_MARKDOWN_FILE = "book.md"
_CHAPTERS_DIR = "chapters"

_MAX_SLUG_CHARS = 40


@dataclass(frozen=True)
class Book:
    """One uploaded book and its conversion state."""

    id: str
    title: str
    filename: str
    state: str
    error: str | None
    created_at: str


@dataclass(frozen=True)
class ChapterRow:
    """One stored chapter of a ready book."""

    id: int
    book_id: str
    number: int
    title: str
    path: str
    char_count: int


BookList = list[Book]
ChapterList = list[ChapterRow]


def make_book_id(title: str) -> str:
    """Return a valid book id derived from the title and a random suffix."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:_MAX_SLUG_CHARS].strip("-")
    if not slug:
        slug = "book"
    return f"b-{slug}-{secrets.token_hex(3)}"


def _row_to_book(row: sqlite3.Row) -> Book:
    return Book(
        id=str(row["id"]),
        title=str(row["title"]),
        filename=str(row["filename"]),
        state=str(row["state"]),
        error=row["error"],
        created_at=str(row["created_at"]),
    )


def _row_to_chapter(row: sqlite3.Row) -> ChapterRow:
    return ChapterRow(
        id=int(row["id"]),
        book_id=str(row["book_id"]),
        number=int(row["number"]),
        title=str(row["title"]),
        path=str(row["path"]),
        char_count=int(row["char_count"]),
    )


class BookStore:
    """File layout under books_path plus the books/chapters tables."""

    def __init__(self, conn: sqlite3.Connection, books_path: Path) -> None:
        self._conn = conn
        self._books_path = books_path

    def create(self, title: str, filename: str) -> Book:
        """Create a book row and its data directory; state is uploaded."""
        book_id = make_book_id(title)
        created_at = datetime.now(UTC).isoformat()
        self._book_dir(book_id).mkdir(parents=True, exist_ok=False)
        self._conn.execute(
            "INSERT INTO books (id, title, filename, state, error, created_at)"
            " VALUES (?, ?, ?, ?, NULL, ?)",
            (book_id, title, filename, BOOK_STATE_UPLOADED, created_at),
        )
        self._conn.commit()
        return Book(
            id=book_id,
            title=title,
            filename=filename,
            state=BOOK_STATE_UPLOADED,
            error=None,
            created_at=created_at,
        )

    def delete(self, book_id: str) -> None:
        """Remove the book row (with cascade) and its whole data directory."""
        book_dir = self._book_dir(book_id)
        self._conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        self._conn.commit()
        shutil.rmtree(book_dir, ignore_errors=True)

    def source_path(self, book_id: str) -> Path:
        """Path of the uploaded PDF for one book."""
        return self._book_dir(book_id) / _SOURCE_FILE

    def list(self) -> BookList:
        """All books, newest first."""
        rows = self._conn.execute(
            "SELECT id, title, filename, state, error, created_at"
            " FROM books ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        return [_row_to_book(row) for row in rows]

    def get(self, book_id: str) -> Book | None:
        """One book by id, or None."""
        row = self._conn.execute(
            "SELECT id, title, filename, state, error, created_at FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()
        return None if row is None else _row_to_book(row)

    def set_state(self, book_id: str, state: str, error: str | None = None) -> None:
        """Update the conversion state and error of one book."""
        self._book_dir(book_id)
        self._conn.execute(
            "UPDATE books SET state = ?, error = ? WHERE id = ?",
            (state, error, book_id),
        )
        self._conn.commit()

    def read_converter_status(self, book_id: str) -> tuple[str, str | None] | None:
        """The converter's status.json as (state, error), or None when absent or unreadable."""
        self._book_dir(book_id)
        status_path = self._book_dir(book_id) / _STATUS_FILE
        try:
            raw = status_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        state = data.get("state")
        if not isinstance(state, str):
            return None
        error = data.get("error")
        if not isinstance(error, str):
            error = None
        return state, error

    def finalize(self, book_id: str) -> ChapterList:
        """Split book.md, write chapter files, replace chapter rows and mark ready."""
        book = self.get(book_id)
        if book is None:
            raise ValueError("unknown book id")
        markdown_path = self._book_dir(book.id) / _MARKDOWN_FILE
        markdown = markdown_path.read_text(encoding="utf-8")
        chapters = split_chapters(markdown, book.title)
        chapter_dir = self._book_dir(book.id) / _CHAPTERS_DIR
        chapter_dir.mkdir(parents=True, exist_ok=True)
        for old in chapter_dir.glob("*.md"):
            old.unlink()
        for chapter in chapters:
            self.chapter_file_path(book.id, chapter.number).write_text(
                chapter.text, encoding="utf-8"
            )
        rows: list[ChapterRow] = []
        with self._conn:
            self._conn.execute("DELETE FROM chapters WHERE book_id = ?", (book.id,))
            for chapter in chapters:
                relative_path = self.relative_chapter_path(book.id, chapter.number)
                char_count = len(chapter.text)
                cursor = self._conn.execute(
                    "INSERT INTO chapters (book_id, number, title, path, char_count)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (book.id, chapter.number, chapter.title, relative_path, char_count),
                )
                rows.append(
                    ChapterRow(
                        id=int(cursor.lastrowid or 0),
                        book_id=book.id,
                        number=chapter.number,
                        title=chapter.title,
                        path=relative_path,
                        char_count=char_count,
                    )
                )
            self._conn.execute(
                "UPDATE books SET state = ?, error = NULL WHERE id = ?",
                (BOOK_STATE_READY, book.id),
            )
        return rows

    def chapters(self, book_id: str) -> ChapterList:
        """All chapter rows of one book, ordered by chapter number."""
        self._book_dir(book_id)
        rows = self._conn.execute(
            "SELECT id, book_id, number, title, path, char_count"
            " FROM chapters WHERE book_id = ? ORDER BY number",
            (book_id,),
        ).fetchall()
        return [_row_to_chapter(row) for row in rows]

    def chapter(self, chapter_id: int) -> tuple[Book, ChapterRow, str] | None:
        """One chapter with its book and full text, or None."""
        row = self._conn.execute(
            "SELECT id, book_id, number, title, path, char_count FROM chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
        if row is None:
            return None
        chapter = _row_to_chapter(row)
        book = self.get(chapter.book_id)
        if book is None:
            return None
        text = self.chapter_file_path(book.id, chapter.number).read_text(encoding="utf-8")
        return book, chapter, text

    def sync(self, book_id: str) -> Book:
        """Apply the converter status.json to a converting book and return the current one."""
        book = self.get(book_id)
        if book is None:
            raise ValueError("unknown book id")
        if book.state != BOOK_STATE_CONVERTING:
            return book
        status = self.read_converter_status(book.id)
        if status is None:
            return book
        state, error = status
        if state == CONVERTER_STATE_DONE:
            self.finalize(book.id)
        elif state == CONVERTER_STATE_FAILED:
            self.set_state(book.id, BOOK_STATE_FAILED, error)
        current = self.get(book.id)
        if current is None:
            raise ValueError("unknown book id")
        return current

    def _book_dir(self, book_id: str) -> Path:
        if BOOK_ID_PATTERN.fullmatch(book_id) is None:
            raise ValueError("invalid book id")
        return self._books_path / book_id

    def chapter_file_path(self, book_id: str, number: int) -> Path:
        """File path of one chapter, built from the validated book id."""
        return self._book_dir(book_id) / _CHAPTERS_DIR / f"{number:02d}.md"

    def relative_chapter_path(self, book_id: str, number: int) -> str:
        """Chapter file path relative to the books root, as stored in the chapters table."""
        return f"{book_id}/{_CHAPTERS_DIR}/{number:02d}.md"
