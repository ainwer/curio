"""Golden-file tests for prompt templates and message builders."""

import json
from pathlib import Path
from typing import Any

import pytest

from curio_app.llm.prompts import (
    Excerpt,
    NoteRef,
    question_bridge_messages,
    question_source_messages,
    question_system,
    question_topic_messages,
    tutor_context,
    tutor_messages,
    tutor_system,
    wrapup_messages,
    wrapup_system,
)

GOLDEN = Path(__file__).parent / "golden"

PROFILE = "Invented profile: tinkerer with ebike battery packs and GaN dev boards."
EXISTING = ["Why do diodes get hot in forward mode?", "How much current flows backwards?"]
BOOK = "Invented Book"
CHAPTER_TITLE = "One-Way Conduction"
CHAPTER_TEXT = "Invented chapter text about one-way conduction and reverse recovery."
TOPIC = "Switching Losses"
FOCUS = "GaN Transistors"
QUESTION = "Why does reverse recovery make converters get hot?"
CONCEPTS = ["reverse-recovery", "switching-losses"]
GUT = "I guess the current flows backwards and heats something."
EXCERPTS = [
    Excerpt(BOOK, 3, "Diodes", "Invented excerpt about reverse recovery."),
    Excerpt(BOOK, 7, "Topologies", "Invented excerpt about half-bridge topologies."),
]
NOTES = [
    NoteRef("Reverse-Recovery", "Invented note summary."),
    NoteRef("Half-Bridge", "Invented half-bridge notes."),
]
HISTORY = [
    ("assistant", "Right idea: think stored charge. Where does it have to go first?"),
    ("user", "It has to leave the diode?"),
    ("user", "And how quickly does it leave?"),
    ("assistant", "Exactly. What sets the amount of charge?"),
    ("user", "The forward current times something? I will guess the forward current."),
]
TRANSCRIPT = [
    ("user", GUT),
    ("assistant", "Right direction; the stored charge explains the shape."),
]


def golden_text(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8").removesuffix("\n")


def golden_json(name: str) -> Any:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))


# --- question generator: system -------------------------------------------------


def test_question_system_with_profile_matches_golden() -> None:
    assert question_system(PROFILE) == golden_text("question_system.profile.golden")


def test_question_system_without_profile_matches_golden() -> None:
    assert question_system(None) == golden_text("question_system.noprofile.golden")


def test_question_system_contains_verbatim_card_rules() -> None:
    text = question_system(PROFILE)
    assert "Card rules:" in text
    assert 'Give the source as "<Book>, ch. <n>"' in text
    assert 'Prefer "why", "how much / estimate"' in text


def test_question_system_no_profile_placeholder() -> None:
    assert "No profile provided." in question_system(None)
    assert PROFILE not in question_system(None)


# --- question generator: source mode --------------------------------------------


def test_question_source_with_existing_matches_golden() -> None:
    messages = question_source_messages(
        book_title=BOOK,
        chapter_number=3,
        chapter_title=CHAPTER_TITLE,
        chapter_text=CHAPTER_TEXT,
        existing=EXISTING,
    )
    assert jsonable(messages) == golden_json("question_source_messages.existing.golden.json")


def test_question_source_fresh_matches_golden() -> None:
    messages = question_source_messages(
        book_title=BOOK,
        chapter_number=3,
        chapter_title=CHAPTER_TITLE,
        chapter_text=CHAPTER_TEXT,
        existing=[],
    )
    assert jsonable(messages) == golden_json("question_source_messages.fresh.golden.json")


# --- question generator: topic mode ----------------------------------------------


def test_question_topic_with_existing_matches_golden() -> None:
    assert jsonable(question_topic_messages(topic=TOPIC, existing=EXISTING)) == (
        golden_json("question_topic_messages.existing.golden.json")
    )
    assert jsonable(question_topic_messages(topic=TOPIC, existing=[])) == (
        golden_json("question_topic_messages.fresh.golden.json")
    )


# --- question generator: bridge mode ---------------------------------------------


def test_question_bridge_matches_golden() -> None:
    assert jsonable(question_bridge_messages(focus=FOCUS, existing=EXISTING)) == (
        golden_json("question_bridge_messages.focus.golden.json")
    )
    assert jsonable(question_bridge_messages(focus=None, existing=[])) == (
        golden_json("question_bridge_messages.nofocus.golden.json")
    )


def test_question_bridge_without_focus_picks_areas() -> None:
    text = question_bridge_messages(focus=None, existing=[])[0]["content"]
    assert isinstance(text, str)
    assert "Pick new areas adjacent to the profile." in text
    assert FOCUS not in text


# --- tutor ------------------------------------------------------------------------


def test_tutor_system_matches_golden() -> None:
    assert tutor_system() == golden_text("tutor_system.golden")


def test_tutor_context_full_matches_golden() -> None:
    context = tutor_context(
        question=QUESTION, concepts=CONCEPTS, gut_answer=GUT, excerpts=EXCERPTS, notes=NOTES
    )
    assert context == golden_text("tutor_context.full.golden")


def test_tutor_context_empty_excerpts_and_notes_matches_golden() -> None:
    context = tutor_context(
        question=QUESTION, concepts=CONCEPTS, gut_answer=GUT, excerpts=[], notes=[]
    )
    assert context == golden_text("tutor_context.empty.golden")
    assert "None found." in context
    assert "None linked." in context


def test_tutor_messages_match_golden() -> None:
    messages = tutor_messages(
        context=tutor_context(
            question=QUESTION, concepts=CONCEPTS, gut_answer=GUT, excerpts=EXCERPTS, notes=NOTES
        ),
        history=HISTORY,
    )
    assert jsonable(messages) == golden_json("tutor_messages.golden.json")


# --- wrap-up -----------------------------------------------------------------------


def test_wrapup_system_matches_golden() -> None:
    system = wrapup_system()
    assert system == golden_text("wrapup_system.golden")
    assert "curate down to 2–3" in system  # noqa: RUF001
    assert "Card rules:" in system


def test_wrapup_messages_match_golden() -> None:
    messages = wrapup_messages(question=QUESTION, gut_answer=GUT, transcript=TRANSCRIPT)
    assert jsonable(messages) == golden_json("wrapup_messages.golden.json")


# --- structural guarantees on every render ------------------------------------------


@pytest.mark.parametrize(
    ("template", "context"),
    [
        ("question_generator.system.md.j2", {"profile": PROFILE}),
        ("tutor.system.md.j2", {}),
        ("wrapup.system.md.j2", {}),
    ],
)
def test_rendered_templates_do_not_end_with_newline(template: str, context: dict[str, Any]) -> None:
    from curio_app.llm.prompts import render

    rendered = render(template, **context)
    assert rendered == rendered.strip()


def test_render_uses_strict_undefined() -> None:
    from pytest import raises

    from curio_app.llm.prompts import render

    with raises(Exception, match="question") as info:
        render("tutor.context.md.j2")  # type: ignore[call-arg]
    assert not isinstance(info.value, KeyError)
