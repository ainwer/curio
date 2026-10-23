"""Tests for BM25 search over ready books."""

from books.support import make_ready_book
from curio_app.books.search import search, tokenize
from curio_app.books.store import BookStore

RANKING_MARKDOWN = (
    "## 1 Rectifiers\n\n"
    "The bridge rectifier uses four diodes. The rectifier output pulses twice per cycle.\n\n"
    "## 2 Rectifier alternatives\n\n"
    "A synchronous rectifier replaces the bridge rectifier diodes with transistors.\n"
)
CAPACITOR_MARKDOWN = "## 1 Filters\n\nA capacitor smooths the voltage of the supply.\n"
MANY_SECTIONS_MARKDOWN = (
    "## 1 Grid basics\n\ncapacitor bank text\n\n"
    "## 2 Details\n\ncapacitor sizing text\n\n"
    "### Tolerances\n\ncapacitor tolerance text\n\n"
    "### Aging\n\ncapacitor aging text\n\n"
    "### Standards\n\ncapacitor standards text\n\n"
    "### Safety\n\ncapacitor safety text\n"
)
LONG_MARKDOWN = (
    "## 1 Long\n\ncapacitor " + "a" * 90 + "\n\n## 2 Long\n\ncapacitor " + "b" * 90 + "\n"
)


def test_tokenize_filters_stopwords_and_short_tokens() -> None:
    tokens = tokenize("Hello, big-ego_road! a THE 42 x")

    assert tokens == ["hello", "big-ego_road", "42"]


def test_empty_query_returns_no_hits(store: BookStore) -> None:
    assert search("", store) == []


def test_ranking_prefers_section_with_query_terms(store: BookStore) -> None:
    make_ready_book(store, "Power Electronics", RANKING_MARKDOWN)

    hits = search("rectifier", store)

    assert [hit.chapter_number for hit in hits] == [2, 1]
    assert hits[0].section == "2 Rectifier alternatives"
    assert hits[0].book_title == "Power Electronics"
    assert hits[0].chapter_title == "Rectifier alternatives"


def test_limit_restricts_number_of_hits(store: BookStore) -> None:
    make_ready_book(store, "Grid Theory", MANY_SECTIONS_MARKDOWN)

    default_hits = search("capacitor", store)
    all_hits = search("capacitor", store, limit=6)

    assert len(default_hits) == 4
    assert len(all_hits) == 6
    assert all("capacitor" in hit.text or "Capacitor" in hit.text for hit in all_hits)


def test_max_chars_truncation(store: BookStore) -> None:
    make_ready_book(store, "Long Book", LONG_MARKDOWN)

    hits = search("capacitor", store, limit=4, max_chars=150)

    assert len(hits) == 2
    assert hits[0].text == "capacitor " + "a" * 90
    assert hits[1].text.endswith(" […]")
    assert len(hits[1].text) == 50
    assert len(hits[0].text) + len(hits[1].text) <= 150


def test_only_ready_books_searched(store: BookStore, settings, db_conn) -> None:
    ready_id = make_ready_book(store, "Capacitor Theory", CAPACITOR_MARKDOWN)
    hidden = store.create("Hidden Book", "hidden.pdf")
    (settings.books_path / hidden.id / "chapters").mkdir()
    (settings.books_path / hidden.id / "chapters" / "01.md").write_text(
        "capacitor text here", encoding="utf-8"
    )
    db_conn.execute(
        "INSERT INTO chapters (book_id, number, title, path, char_count)"
        " VALUES (?, 1, 'Fake', 'ignored', 19)",
        (hidden.id,),
    )
    db_conn.commit()

    hits = search("capacitor", store)

    assert len(hits) == 1
    assert hits[0].book_title == "Capacitor Theory"
    assert store.get(ready_id) is not None
