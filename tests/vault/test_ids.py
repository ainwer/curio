"""Tests for slug, question id and card id generation."""

import re
from datetime import date

from curio_app.domain import CARD_ID_PATTERN
from curio_app.vault.ids import card_id, question_id, slugify

DAY = date(2026, 10, 23)


def test_slugify_umlauts() -> None:
    assert slugify("Wärmeleitfähigkeit") == "warmeleitfahigkeit"


def test_slugify_punctuation_and_mixed() -> None:
    assert (
        slugify("Why do SiC MOSFETs switch faster? — a quick look!.")
        == "why-do-sic-mosfets-switch-faster"
    )


def test_slugify_limits_words() -> None:
    assert (
        slugify("one two three four five six seven eight one two three")
        == "one-two-three-four-five-six"
    )


def test_slugify_limits_length_at_word_boundary() -> None:
    slug = slugify("magnetics capacitor inductor transformer converter resistor thermal")
    assert slug == "magnetics-capacitor-inductor-transformer"
    assert len(slug) <= 50
    assert slugify("alpha beta", max_len=5) == "alpha"
    assert slugify("ab-cdefghi", max_len=9) == "ab"


def test_slugify_long_word_cut() -> None:
    assert slugify("a" * 60) == "a" * 50
    assert slugify("Wärmeleitfähigkeit von Siliziumkarbid", max_len=10) == "warmeleitf"


def test_slugify_empty_falls_back() -> None:
    assert slugify("") == "question"
    assert slugify("??? !!!") == "question"


def test_question_id_includes_date_and_slug() -> None:
    assert question_id(DAY, "Why do batteries age?", set()) == "q-2026-10-23-why-do-batteries-age"


def test_question_id_collisions_get_suffixes() -> None:
    existing = {"q-2026-10-23-why", "q-2026-10-23-why-2"}
    assert question_id(DAY, "why?", existing) == "q-2026-10-23-why-3"
    assert question_id(DAY, "why?", {"q-2026-10-23-why"}) == "q-2026-10-23-why-2"
    assert question_id(DAY, "why?", {"q-2026-10-23-other"}) == "q-2026-10-23-why"


def test_question_id_with_empty_text() -> None:
    assert question_id(DAY, "???", set()) == "q-2026-10-23-question"


def test_card_id_matches_card_pattern() -> None:
    first, second = card_id(), card_id()
    assert re.fullmatch(CARD_ID_PATTERN, first)
    assert re.fullmatch(CARD_ID_PATTERN, second)
    assert first != second
