"""BM25 search over chapter sections of ready books."""

import math
import re
from collections import Counter
from dataclasses import dataclass, replace

from curio_app.books.chapters import split_sections
from curio_app.books.store import BookStore

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")

_K1 = 1.5
_B = 0.75
_TRUNCATION_MARK = " [\u2026]"

_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "into",
        "is",
        "it",
        "its",
        "not",
        "of",
        "on",
        "or",
        "than",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "you",
        "your",
    }
)


@dataclass(frozen=True)
class Hit:
    """One search result pointing at a section of a chapter."""

    book_title: str
    chapter_number: int
    chapter_title: str
    section: str
    text: str
    score: float


@dataclass(frozen=True)
class _Document:
    book_title: str
    chapter_number: int
    chapter_title: str
    section: str
    text: str
    term_counts: dict[str, int]
    length: int


def tokenize(text: str) -> list[str]:
    """Lowercase tokens of at least two characters, without stopwords."""
    tokens = _TOKEN_PATTERN.findall(text.lower())
    return [token for token in tokens if len(token) >= 2 and token not in _STOPWORDS]


def search(query: str, store: BookStore, *, limit: int = 4, max_chars: int = 6000) -> list[Hit]:
    """Rank sections of ready books with BM25 and trim the total text to max_chars."""
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms:
        return []
    documents = _corpus(store)
    if not documents:
        return []
    doc_count = len(documents)
    average_length = sum(document.length for document in documents) / doc_count
    if average_length <= 0:
        return []
    document_frequency: dict[str, int] = {}
    for document in documents:
        for term in document.term_counts:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    hits: list[Hit] = []
    for document in documents:
        score = 0.0
        for term in terms:
            term_count = document.term_counts.get(term, 0)
            if term_count == 0:
                continue
            frequency = document_frequency.get(term, 0)
            idf = math.log((doc_count - frequency + 0.5) / (frequency + 0.5) + 1)
            normalizer = 1 - _B + _B * document.length / average_length
            score += idf * term_count * (_K1 + 1) / (term_count + _K1 * normalizer)
        if score > 0:
            hits.append(
                Hit(
                    book_title=document.book_title,
                    chapter_number=document.chapter_number,
                    chapter_title=document.chapter_title,
                    section=document.section,
                    text=document.text,
                    score=score,
                )
            )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return _trim(hits[:limit], max_chars)


def _trim(hits: list[Hit], max_chars: int) -> list[Hit]:
    trimmed: list[Hit] = []
    remaining = max_chars
    for hit in hits:
        if remaining <= 0:
            trimmed.append(replace(hit, text=""))
            continue
        text = hit.text
        if len(text) <= remaining:
            remaining -= len(text)
        else:
            budget = max(0, remaining - len(_TRUNCATION_MARK))
            text = text[:budget].rstrip() + _TRUNCATION_MARK
            remaining = 0
        trimmed.append(replace(hit, text=text))
    return trimmed


def _corpus(store: BookStore) -> list[_Document]:
    documents: list[_Document] = []
    for book in store.list():
        if book.state != "ready":
            continue
        for chapter in store.chapters(book.id):
            loaded = store.chapter(chapter.id)
            if loaded is None:
                continue
            _, _, chapter_text = loaded
            for section in split_sections(chapter_text):
                tokens = tokenize(f"{section.heading}\n{section.text}")
                documents.append(
                    _Document(
                        book_title=book.title,
                        chapter_number=chapter.number,
                        chapter_title=chapter.title,
                        section=section.heading,
                        text=section.text,
                        term_counts=dict(Counter(tokens)),
                        length=len(tokens),
                    )
                )
    return documents
