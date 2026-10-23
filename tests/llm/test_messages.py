"""Tests for message assembly: cache breakpoints, merging, ordering."""

import pytest

from curio_app.llm.base import Message
from curio_app.llm.prompts import (
    question_bridge_messages,
    question_source_messages,
    question_topic_messages,
    tutor_context,
    tutor_messages,
    wrapup_messages,
)

CH = {
    "book": "Invented Book",
    "number": 3,
    "title": "One-Way Conduction",
    "text": "Invented chapter text.",
}


def test_source_message_puts_cache_on_chapter_block() -> None:
    messages = question_source_messages(
        book_title=CH["book"],
        chapter_number=CH["number"],
        chapter_title=CH["title"],
        chapter_text=CH["text"],
        existing=[],
    )
    assert len(messages) == 1
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0]["cache"] is True
    assert "cache" not in content[1]
    assert content[0]["text"].startswith("<chapter book=")
    assert content[0]["text"].endswith("</chapter>\n")
    assert content[1]["text"].startswith("Write 8–10 question cards")  # noqa: RUF001


def test_source_existing_questions_landed_in_instructions() -> None:
    messages = question_source_messages(
        book_title=CH["book"],
        chapter_number=CH["number"],
        chapter_title=CH["title"],
        chapter_text=CH["text"],
        existing=["Old one?"],
    )
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert "Avoid duplicating these existing questions:" in content[1]["text"]
    assert "Old one?" in content[1]["text"]
    assert "<chapter" not in content[1]["text"]


def test_topic_and_bridge_are_plain_text_messages() -> None:
    topic = question_topic_messages(topic="Switching Losses", existing=["Why?"])
    assert topic[0]["role"] == "user"
    assert isinstance(topic[0]["content"], str)
    assert topic[0]["content"].startswith("Topic: Switching Losses")

    bridge = question_bridge_messages(focus="X", existing=[])
    assert bridge[0]["role"] == "user"
    assert isinstance(bridge[0]["content"], str)
    assert bridge[0]["content"].startswith("Focus on this new area: X")


def test_tutor_messages_cache_flag_on_context_block() -> None:
    messages = tutor_messages(context="CONTEXT", history=[("assistant", "Hi."), ("user", "Back?")])
    first = messages[0]
    assert isinstance(first["content"], list)
    assert first["content"][0] == {"type": "text", "text": "CONTEXT", "cache": True}


def test_tutor_history_merges_consecutive_same_roles() -> None:
    history = [
        ("assistant", "A1"),
        ("user", "U1"),
        ("user", "U2"),
        ("assistant", "A2\nA2b"),
        ("user", "U3"),
    ]
    messages = tutor_messages(context="CONTEXT", history=list(history))
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    texts = [m["content"] for m in messages[1:]]
    assert texts == ["A1", "U1\n\nU2", "A2\nA2b", "U3"]


def test_tutor_leading_user_history_merges_into_context_block() -> None:
    messages = tutor_messages(context="CONTEXT", history=[("user", "Hello again")])
    first = messages[0]
    content = first["content"]
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0]["cache"] is True
    assert content[1] == {"type": "text", "text": "\n\nHello again"}


def test_tutor_empty_history_ends_with_context_user_message() -> None:
    messages = tutor_messages(context="CONTEXT", history=[])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


def test_tutor_requires_last_message_to_be_user() -> None:
    with pytest.raises(AssertionError):
        tutor_messages(context="CONTEXT", history=[("assistant", "Only me.")])


def test_tutor_messages_end_with_user_for_realistic_history() -> None:
    history: list[tuple[str, str]] = [
        ("assistant", "Guiding question?"),
        ("user", "My guess."),
    ]
    messages = tutor_messages(
        context="CONTEXT",
        history=list(history),  # type: ignore[arg-type]
    )
    assert messages[-1]["role"] == "user"


def test_wrapup_message_contains_transcript_markup() -> None:
    messages = wrapup_messages(
        question="Why?", gut_answer="gut", transcript=[("user", "u"), ("assistant", "a")]
    )
    content = messages[0]["content"]
    assert isinstance(content, str)
    assert "<question>Why?</question>" in content
    assert "<gut_answer>gut</gut_answer>" in content
    assert "<user>u</user>" in content
    assert "<assistant>a</assistant>" in content
    assert content.endswith("</transcript>")


def test_message_type_supports_list_content() -> None:
    message: Message = {
        "role": "user",
        "content": [  # type: ignore[typeddict-item]
            {"type": "text", "text": "x", "cache": True}
        ],
    }
    assert message["content"][0]["cache"] is True  # type: ignore[index]


def test_tutor_context_is_verbatim_in_first_message() -> None:
    context = tutor_context(
        question="Why does x do y?",
        concepts=["x-concept"],
        gut_answer="Because.",
        excerpts=[],
        notes=[],
    )
    messages = tutor_messages(context=context, history=[])
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == context
