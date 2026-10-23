"""Tests for the deterministic fake LLM client."""

import pytest

from curio_app.llm.base import Message, TextBlock
from curio_app.llm.fake import FakeLLMClient
from curio_app.llm.schemas import CardType, GeneratedQuestion, QuestionBatch, Wrapup

STREAM_TEXT = "This is a fake tutor reply about your gut answer. What do you think happens next?"


def source_mode_messages() -> list[Message]:
    chapter: TextBlock = {"type": "text", "text": "<chapter>invented chapter</chapter>"}
    plain: TextBlock = {"type": "text", "text": "Write questions."}
    return [{"role": "user", "content": [chapter, plain]}]


async def test_generate_question_batch_from_source() -> None:
    client = FakeLLMClient()
    system = "System."
    batch = await client.generate(
        system=system,
        messages=source_mode_messages(),
        schema=QuestionBatch,
        purpose="generation",
    )
    assert isinstance(batch, QuestionBatch)
    assert len(batch.questions) == 8
    first = batch.questions[0]
    assert first.question.startswith("Why does fake concept 1 matter in ")
    assert first.domain == "fake-domain"
    assert first.concepts == ["concept-1"]
    assert first.hook == "Invented for tests."
    assert first.source == "Fake Book, ch. 1"
    assert first.bridge_from is None
    assert client.calls[0]["schema"] == "QuestionBatch"
    assert client.calls[0]["system"] == system


async def test_generate_question_batch_topic_mode() -> None:
    client = FakeLLMClient()
    messages: list[Message] = [
        {"role": "user", "content": "Switching   losses\nin  GaN inverters."}
    ]
    batch = await client.generate(
        system="s", messages=messages, schema=QuestionBatch, purpose="generation"
    )
    assert isinstance(batch, QuestionBatch)
    for question in batch.questions:
        assert question.source is None
    assert batch.questions[0].question == (
        "Why does fake concept 1 matter in Switching losses in GaN inverters.?"
    )


async def test_generate_question_batch_bridge_mode() -> None:
    client = FakeLLMClient()
    messages: list[Message] = [{"role": "user", "content": "bridge questions about inverters."}]
    batch = await client.generate(
        system="s", messages=messages, schema=QuestionBatch, purpose="generation"
    )
    assert isinstance(batch, QuestionBatch)
    for question in batch.questions:
        assert question.bridge_from == "known-area"
        assert question.source is None


async def test_generate_wrapup() -> None:
    client = FakeLLMClient()
    wrapup = await client.generate(
        system="s",
        messages=[{"role": "user", "content": "transcript"}],
        schema=Wrapup,
        purpose="wrapup",
    )
    assert isinstance(wrapup, Wrapup)
    assert wrapup.answer == "Fake answer."
    assert wrapup.misconceptions == ["I thought A, but B."]
    assert [c.type for c in wrapup.cards] == [
        CardType.WHY,
        CardType.ESTIMATE,
        CardType.TRADEOFF,
        CardType.TRANSFER,
        CardType.FACT,
    ]
    assert len(wrapup.cards) == 5
    for card in wrapup.cards:
        assert card.front.endswith("?")


async def test_generate_unknown_schema_raises() -> None:
    client = FakeLLMClient()
    with pytest.raises(NotImplementedError):
        await client.generate(
            system="s", messages=[], schema=GeneratedQuestion, purpose="generation"
        )


async def test_stream_yields_words_with_trailing_spaces() -> None:
    client = FakeLLMClient()
    collected = [chunk async for chunk in client.stream(system="s", messages=[])]
    assert "".join(collected) == STREAM_TEXT + " "
    assert client.calls[0] == {"system": "s", "messages": [], "kind": "stream"}
    assert "gut answer" in STREAM_TEXT
