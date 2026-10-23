"""Tests for schema post-validation."""

import pytest

from curio_app.llm.schemas import CardDraft, CardType, GeneratedQuestion, QuestionBatch, Wrapup


def question_item(text: str = "Why does a diode get hot?") -> GeneratedQuestion:
    return GeneratedQuestion(
        question=text,
        domain="domain",
        concepts=["concept"],
        source=None,
        hook="Because it matters.",
        bridge_from=None,
    )


def card(style: CardType = CardType.WHY, front: str = "Why does it work?") -> CardDraft:
    return CardDraft(type=style, front=front, back="Mechanism in two sentences.", source=None)


def card_with_source(style: CardType, source: str | None) -> CardDraft:
    return CardDraft(type=style, front="Why does it work?", back="Back.", source=source)


def test_kebab_normalisation() -> None:
    question = GeneratedQuestion(
        question="Why does a diode get hot?",
        domain="Power Electronics",
        concepts=["Reverse Recovery", "physics", "reverse recovery"],
        source=None,
        hook="h",
        bridge_from=None,
    )
    assert question.domain == "power-electronics"
    assert question.concepts == ["reverse-recovery", "physics"]


def test_kebab_strips_forbidden_characters() -> None:
    question = GeneratedQuestion(
        question="Why 50/50?",
        domain="Power/Electronics Test",
        concepts=["a  b!!  c"],
        source=None,
        hook="h",
        bridge_from=None,
    )
    assert question.domain == "powerelectronics-test"
    assert question.concepts == ["a-b-c"]


def test_question_must_end_with_question_mark() -> None:
    with pytest.raises(ValueError, match="must end with"):
        GeneratedQuestion(
            question="No question mark here.",
            domain="d",
            concepts=["a"],
            source=None,
            hook="h",
            bridge_from=None,
        )


def test_question_strip_whitespace() -> None:
    question = question_item("  Why does a diode get hot? \n")
    assert question.question == "Why does a diode get hot?"


def test_concepts_bounds() -> None:
    base = {
        "question": "Why?",
        "domain": "d",
        "source": None,
        "hook": "h",
        "bridge_from": None,
    }
    with pytest.raises(ValueError, match="1-4"):
        GeneratedQuestion(concepts=[], **base)
    with pytest.raises(ValueError, match="1-4"):
        GeneratedQuestion(concepts=["one", "two", "three", "four", "five"], **base)
    ok = GeneratedQuestion(concepts=["one", "one", "ONE"], **base)
    assert ok.concepts == ["one"]


def test_concepts_strip_to_empty_fails() -> None:
    with pytest.raises(ValueError, match="1-4"):
        GeneratedQuestion(
            question="Why?",
            domain="d",
            concepts=["???"],
            source=None,
            hook="h",
            bridge_from=None,
        )


def test_whitespace_becomes_dash_per_algorithm() -> None:
    question = GeneratedQuestion(
        question="Why?",
        domain="d",
        concepts=["  "],
        source=None,
        hook="h",
        bridge_from=None,
    )
    assert question.concepts == ["-"]


def test_hook_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="Hook"):
        GeneratedQuestion(
            question="Why?",
            domain="d",
            concepts=["a"],
            source=None,
            hook="   ",
            bridge_from=None,
        )


def test_empty_strings_become_none() -> None:
    question = GeneratedQuestion(
        question="Why?",
        domain="d",
        concepts=["a"],
        source="",
        hook="h",
        bridge_from=None,
    )
    assert question.source is None


def test_batch_count_bounds() -> None:
    assert (
        len(QuestionBatch(questions=[question_item(f"Why {i}?") for i in range(5)]).questions) == 5
    )
    with pytest.raises(ValueError, match="5-10"):
        QuestionBatch(questions=[question_item(f"Why {i}?") for i in range(4)])
    with pytest.raises(ValueError, match="5-10"):
        QuestionBatch(questions=[question_item(f"Why {i}?") for i in range(11)])


def test_batch_dedupes_case_insensitive() -> None:
    same = question_item("Why does a diode get hot?")
    upper = question_item("WHY DOES A DIODE GET HOT?")
    others = [question_item(f"Why {i}?") for i in range(9)]
    batch = QuestionBatch(questions=[same, upper, *others])
    assert len(batch.questions) == 10
    assert batch.questions[0].question == "Why does a diode get hot?"


def test_card_rules() -> None:
    with pytest.raises(ValueError, match="front"):
        card(front="Not a question.")
    with pytest.raises(ValueError, match="back"):
        CardDraft(type=CardType.FACT, front="Why?", back="  ", source=None)
    draft = CardDraft(type=CardType.FACT, front=" Why? ", back=" b ", source=" ")
    assert draft.source is None
    assert draft.type is CardType.FACT


def test_card_source_empty_becomes_none() -> None:
    draft = card_with_source(CardType.WHY, "  ")
    assert draft.source is None


def test_card_invalid_type_rejected() -> None:
    with pytest.raises(ValueError):
        CardDraft.model_validate({"type": "joke", "front": "Why?", "back": "b", "source": None})


def test_wrapup_card_count_and_answer() -> None:
    base = {"answer": "Answer.", "misconceptions": []}
    with pytest.raises(ValueError, match="4-6"):
        Wrapup(cards=[card() for _ in range(3)], **base)
    with pytest.raises(ValueError, match="4-6"):
        Wrapup(cards=[card() for _ in range(7)], **base)
    wrapup = Wrapup(cards=[card() for _ in range(5)], **base)
    assert len(wrapup.cards) == 5


def test_wrapup_answer_not_empty() -> None:
    with pytest.raises(ValueError, match="Answer"):
        Wrapup(answer="  ", misconceptions=["x"], cards=[card() for _ in range(4)])


def test_wrapup_may_have_empty_misconceptions() -> None:
    wrapup = Wrapup(answer="Answer.", misconceptions=[], cards=[card() for _ in range(4)])
    assert wrapup.misconceptions == []


def test_extra_fields_ignored() -> None:
    payload = dict(question_item().model_dump(), unexpected_field="ignored")
    parsed = GeneratedQuestion.model_validate(payload)
    assert parsed.question == "Why does a diode get hot?"
