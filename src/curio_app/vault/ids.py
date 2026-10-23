"""Id generation for questions and cards (PLAN.md §4)."""

import re
import unicodedata
import uuid
from datetime import date

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_words: int = 6, max_len: int = 50) -> str:
    """ASCII kebab-case slug: NFKD, lowercase, at most max_words words, at most max_len chars."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    words = [w for w in _NON_ALNUM.sub("-", lowered).split("-") if w]
    slug = "-".join(words[:max_words])
    if len(slug) > max_len:
        head = slug[:max_len]
        cut = head.rfind("-")
        slug = head[:cut] if cut > 0 else head
    return slug or "question"


def question_id(created: date, text: str, existing: set[str]) -> str:
    """Return "q-YYYY-MM-DD-<slug>"; on collision append "-2", "-3", ... until unused."""
    base = f"q-{created.isoformat()}-{slugify(text)}"
    if base not in existing:
        return base
    counter = 2
    while f"{base}-{counter}" in existing:
        counter += 1
    return f"{base}-{counter}"


def card_id() -> str:
    """Return "c-" followed by a random uuid4."""
    return f"c-{uuid.uuid4()}"
