"""Deterministic fake LLM client for tests, dev and screenshots."""

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Literal

from curio_app.llm.base import Message, T
from curio_app.llm.schemas import CardDraft, CardType, GeneratedQuestion, QuestionBatch, Wrapup

_STYLES: tuple[CardType, ...] = (
    CardType.WHY,
    CardType.ESTIMATE,
    CardType.TRADEOFF,
    CardType.TRANSFER,
    CardType.FACT,
)
_STREAM_TEXT = "This is a fake tutor reply about your gut answer. What do you think happens next?"


class FakeLLMClient:
    """Canned deterministic outputs built from the input; never touches the network."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        purpose: Literal["generation", "wrapup"],
    ) -> T:
        self.calls.append(
            {
                "system": system,
                "messages": messages,
                "schema": schema.__name__,
                "purpose": purpose,
            }
        )
        if schema is QuestionBatch:
            return self._question_batch(messages)  # type: ignore[return-value]
        if schema is Wrapup:
            return self._wrapup()  # type: ignore[return-value]
        raise NotImplementedError(f"No fake output defined for {schema.__name__}.")

    async def stream(self, *, system: str, messages: list[Message]) -> AsyncIterator[str]:
        self.calls.append({"system": system, "messages": messages, "kind": "stream"})
        for word in _STREAM_TEXT.split(" "):
            yield word + " "
            await asyncio.sleep(0)

    def _question_batch(self, messages: list[Message]) -> QuestionBatch:
        text = _last_user_text(messages)
        topic = re.sub(r"\s+", " ", text)[:40]
        questions = [
            GeneratedQuestion(
                question=f"Why does fake concept {index} matter in {topic}?",
                domain="fake-domain",
                concepts=[f"concept-{index}"],
                hook="Invented for tests.",
                source="Fake Book, ch. 1" if "<chapter" in text else None,
                bridge_from="known-area" if "bridge" in text else None,
            )
            for index in range(1, 9)
        ]
        return QuestionBatch(questions=questions)

    def _wrapup(self) -> Wrapup:
        cards = [
            CardDraft(
                type=style,
                front=f"Fake card {index}: how does the mechanism behave?",
                back="Invented for tests.",
                source=None,
            )
            for index, style in enumerate(_STYLES, start=1)
        ]
        return Wrapup(
            answer="Fake answer.",
            misconceptions=["I thought A, but B."],
            cards=cards,
        )


def _last_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message["role"] != "user":
            continue
        content = message["content"]
        if isinstance(content, str):
            return content
        return "\n".join(block["text"] for block in content)
    return ""
