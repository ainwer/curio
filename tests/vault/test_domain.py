"""Tests for domain models: accessors, section insertion and validation."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from curio_app.domain import Card, CardType, Question, Section, Status


def question(**overrides: Question) -> Question:
    defaults: dict[str, object] = {
        "id": "q-2026-10-23-why-do-batteries-age",
        "status": Status.INBOX,
        "domain": "power-electronics",
        "created": datetime(2026, 10, 23, 8, 0, 0, tzinfo=UTC),
        "title": "Why do batteries age?",
    }
    defaults.update(overrides)
    return Question(**defaults)  # type: ignore[arg-type]


def section(heading: str, body: str = "Some body text.") -> Section:
    return Section(heading=heading, body=body)


def test_gut_answer_and_insight_absent_or_empty() -> None:
    q = question()
    assert q.gut_answer() is None
    assert q.insight() is None
    filled = q.with_gut_answer("First thoughts.")
    assert filled.gut_answer() == "First thoughts."
    assert filled.with_gut_answer("").gut_answer() is None


def test_with_gut_answer_replaces_in_place() -> None:
    q = question(sections=[section("Gut answer", "wrong draft"), section("Insight")])
    updated = q.with_gut_answer("better draft")
    assert updated.gut_answer() == "better draft"
    assert [s.heading for s in updated.sections] == ["Gut answer", "Insight"]
    assert q.gut_answer() == "wrong draft"


def test_setting_inserts_after_preceding_known_section() -> None:
    q = question(
        sections=[
            section("Overview"),
            section("Gut answer"),
            section("Research notes"),
            section("Cards"),
        ]
    )
    updated = q.with_insight("Now I see.")
    assert [s.heading for s in updated.sections] == [
        "Overview",
        "Gut answer",
        "Insight",
        "Research notes",
        "Cards",
    ]


def test_setting_inserts_before_following_known_section() -> None:
    q = question(sections=[section("Research notes"), section("Insight"), section("Cards")])
    updated = q.with_gut_answer("Gut feelings first.")
    assert [s.heading for s in updated.sections] == [
        "Research notes",
        "Gut answer",
        "Insight",
        "Cards",
    ]


def test_setting_inserts_after_last_known_section() -> None:
    q = question(sections=[section("Gut answer"), section("Extra material")])
    updated = q.with_cards([])
    assert [s.heading for s in updated.sections] == ["Gut answer", "Cards", "Extra material"]
    assert updated.cards() == []


def test_setting_appends_when_no_known_section_exists() -> None:
    q = question(sections=[section("Overview"), section("Literature")])
    updated = q.with_gut_answer("Guess.")
    assert [s.heading for s in updated.sections] == ["Overview", "Literature", "Gut answer"]
    updated = updated.with_insight("And?")
    assert [s.heading for s in updated.sections] == [
        "Overview",
        "Literature",
        "Gut answer",
        "Insight",
    ]


def test_known_sections_fill_in_order_on_empty_question() -> None:
    updated = question().with_gut_answer("g").with_insight("i").with_cards([])
    assert [s.heading for s in updated.sections] == ["Gut answer", "Insight", "Cards"]


def test_cards_accessor_parses_and_with_cards_renders() -> None:
    card = Card(
        id="c-7e8f90a1-b2c3-4d45-96e7-f8091a2b3c4d",
        type=CardType.WHY,
        front="Front text?",
        back="Back text.",
    )
    updated = question().with_cards([card])
    assert updated.sections[0].heading == "Cards"
    assert updated.cards() == [card]
    assert updated.sections[0].body.startswith(
        "<!-- card id=c-7e8f90a1-b2c3-4d45-96e7-f8091a2b3c4d type=why -->"
    )


def test_cards_accessor_returns_empty_when_section_absent_or_empty() -> None:
    assert question().cards() == []
    assert question(sections=[section("Cards", "")]).cards() == []


def test_naive_created_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        question(created=datetime(2026, 10, 23, 8, 0, 0))


def test_question_source_with_quote_is_rejected() -> None:
    with pytest.raises(ValidationError):
        question(source='An "invented" book')


def test_question_id_domain_and_concepts_are_validated() -> None:
    with pytest.raises(ValidationError):
        question(id="not-a-question-id")
    with pytest.raises(ValidationError):
        question(domain="Power Electronics")
    with pytest.raises(ValidationError):
        question(concepts=["Fine Concept"])


def test_card_validations() -> None:
    with pytest.raises(ValidationError, match="invalid card id"):
        Card(id="c-not-a-uuid", type=CardType.FACT, front="front", back="back")
    with pytest.raises(ValidationError):
        Card(
            id="c-8f90a1b2-c3d4-4e56-97f8-a9b0c1d2e3f4",
            type=CardType.FACT,
            front="   ",
            back="back",
        )
    with pytest.raises(ValidationError):
        Card(
            id="c-8f90a1b2-c3d4-4e56-97f8-a9b0c1d2e3f4",
            type=CardType.FACT,
            front="front",
            back="back",
            source='Say "hello"',
        )


def test_card_front_and_back_are_stripped() -> None:
    card = Card(
        id="c-8f90a1b2-c3d4-4e56-97f8-a9b0c1d2e3f4",
        type=CardType.FACT,
        front="  spaced front  ",
        back="\nspaced back\n",
    )
    assert card.front == "spaced front"
    assert card.back == "spaced back"
