"""Tests for the BookStore."""

import re

from books.support import write_converter_output
from curio_app.books.store import BOOK_ID_PATTERN, BookStore, make_book_id

TWO_CHAPTER_MARKDOWN = "## 1 Intro\n\nIntro body.\n\n## 2 Diodes\n\nDiode body.\n"


def test_make_book_id_matches_pattern() -> None:
    book_id = make_book_id("Power Electronics!")

    assert re.fullmatch(BOOK_ID_PATTERN, book_id) is not None
    assert book_id.startswith("b-power-electronics-")


def test_make_book_id_ascii_and_length() -> None:
    slug_book_id = make_book_id("Über Netzwerke! 42")
    long_book_id = make_book_id("A Very Long Book Title That Exceeds Forty Characters In Total")

    assert re.fullmatch(BOOK_ID_PATTERN, slug_book_id) is not None
    assert re.fullmatch(BOOK_ID_PATTERN, long_book_id) is not None
    suffix = long_book_id.rsplit("-", 1)[0]
    assert len(suffix) <= 2 + 40


def test_create_list_get(store: BookStore, settings) -> None:
    first = store.create("Power Electronics", "book.pdf")
    second = store.create("Analog Filters", "filters.pdf")

    assert first.state == "uploaded"
    assert first.error is None
    assert first.filename == "book.pdf"
    books = store.list()
    assert [book.id for book in books] == [second.id, first.id]
    assert store.get(first.id) == first
    assert store.get("b-none-000000") is None
    assert store.source_path(first.id) == settings.books_path / first.id / "source.pdf"


def test_finalize_writes_chapter_files_and_rows(store: BookStore) -> None:
    book = store.create("Chapters Book", "b.pdf")
    write_converter_output(store, book.id, TWO_CHAPTER_MARKDOWN)

    rows = store.finalize(book.id)

    assert [row.number for row in rows] == [1, 2]
    assert [row.title for row in rows] == ["Intro", "Diodes"]
    assert rows[0].path == f"{book.id}/chapters/01.md"
    chapter_dir = store.source_path(book.id).parent / "chapters"
    assert (chapter_dir / "01.md").read_text(encoding="utf-8").endswith("Intro body.")
    current = store.get(book.id)
    assert current is not None
    assert current.state == "ready"
    stored = store.chapters(book.id)
    assert [row.title for row in stored] == ["Intro", "Diodes"]
    assert stored[0].char_count == len("## 1 Intro\n\nIntro body.")


def test_sync_done_finalizes(store: BookStore) -> None:
    book = store.create("Sync Book", "b.pdf")
    store.set_state(book.id, "converting")
    write_converter_output(store, book.id, TWO_CHAPTER_MARKDOWN, state="done")

    result = store.sync(book.id)

    assert result.state == "ready"
    assert store.chapters(book.id)


def test_sync_failed_sets_error(store: BookStore) -> None:
    book = store.create("Sync Book", "b.pdf")
    store.set_state(book.id, "converting")
    write_converter_output(store, book.id, TWO_CHAPTER_MARKDOWN, state="failed", error="Boom")

    result = store.sync(book.id)

    assert result.state == "failed"
    assert result.error == "Boom"
    assert store.chapters(book.id) == []


def test_sync_leaves_converting_without_result(store: BookStore) -> None:
    book = store.create("Idle Book", "b.pdf")
    store.set_state(book.id, "converting")

    without_status = store.sync(book.id)

    assert without_status.state == "converting"
    write_converter_output(store, book.id, TWO_CHAPTER_MARKDOWN, state="queued")
    with_queued = store.sync(book.id)
    assert with_queued.state == "converting"


def test_chapter_returns_text(store: BookStore) -> None:
    book = store.create("Read Book", "b.pdf")
    write_converter_output(store, book.id, TWO_CHAPTER_MARKDOWN)
    store.finalize(book.id)
    rows = store.chapters(book.id)

    loaded = store.chapter(rows[1].id)

    assert loaded is not None
    loaded_book, loaded_chapter, text = loaded
    assert loaded_book.id == book.id
    assert loaded_chapter.number == 2
    assert text == "## 2 Diodes\n\nDiode body."
    assert store.chapter(999999) is None
