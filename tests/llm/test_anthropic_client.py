"""Tests for the Anthropic adapter using an injected fake SDK client."""

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from curio_app.config import Settings
from curio_app.llm.anthropic import AnthropicClient
from curio_app.llm.base import LLMOutputInvalid, LLMRefused, LLMUnavailable, Message, TextBlock
from curio_app.llm.schemas import GeneratedQuestion, QuestionBatch

MODEL = "claude-sonnet-5-5"
SYSTEM_TEXT = "System prompt text."
TEST_KEY = "sk-test-not-real-0000"


class FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


def make_response(
    stop_reason: str = "end_turn", parsed: object = None, text: str = "{}"
) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason, parsed_output=parsed, content=[FakeTextBlock(text)]
    )


def make_http_response(status: int) -> httpx.Response:
    request = httpx.Request("POST", "https://example.invalid")
    return httpx.Response(status, request=request)


def sdk_errors() -> dict[str, Exception]:
    return {
        "APIConnectionError": anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://example.invalid")
        ),
        "RateLimitError": anthropic.RateLimitError(
            message="no", response=make_http_response(429), body=None
        ),
        "AuthenticationError": anthropic.AuthenticationError(
            message="no", response=make_http_response(401), body=None
        ),
        "InternalServerError": anthropic.InternalServerError(
            message="no", response=make_http_response(500), body=None
        ),
        "APIStatusError": anthropic.APIStatusError(
            message="no", response=make_http_response(418), body=None
        ),
    }


class FakeMessageStream:
    def __init__(self, events: list[object], final: object) -> None:
        self._events = list(events)
        self._final = final
        self._index = 0

    async def __aenter__(self) -> "FakeMessageStream":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def __aiter__(self) -> "FakeMessageStream":
        return self

    async def __anext__(self) -> object:
        if self._index >= len(self._events):
            raise StopAsyncIteration
        item = self._events[self._index]
        self._index += 1
        return item

    async def get_final_message(self) -> object:
        return self._final


class RaisingStream:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self) -> "RaisingStream":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def __aiter__(self) -> "RaisingStream":
        return self

    async def __anext__(self) -> object:
        raise self._error

    async def get_final_message(self) -> object:
        raise AssertionError("never reached")


class FakeMessages:
    def __init__(self) -> None:
        self.parse_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.parse_responses: list[object] = []
        self.stream_result: object = FakeMessageStream([], make_response())

    async def parse(self, **kwargs: Any) -> object:
        self.parse_calls.append(kwargs)
        item = self.parse_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, **kwargs: Any) -> object:
        self.stream_calls.append(kwargs)
        if isinstance(self.stream_result, Exception):
            raise self.stream_result
        return self.stream_result


class FakeAnthropic:
    def __init__(self, messages: FakeMessages) -> None:
        self.beta = SimpleNamespace(messages=messages)


def make_batch() -> QuestionBatch:
    questions = [
        GeneratedQuestion(
            question=f"Why does concept {index} matter?",
            domain="fake-domain",
            concepts=[f"concept-{index}"],
            source=None,
            hook="Invented for tests.",
            bridge_from=None,
        )
        for index in range(1, 6)
    ]
    return QuestionBatch(questions=questions)


def source_messages() -> list[Message]:
    chapter: TextBlock = {"type": "text", "text": "<chapter>invented</chapter>", "cache": True}
    plain: TextBlock = {"type": "text", "text": "Write questions."}
    return [{"role": "user", "content": [chapter, plain]}]


def make_client(
    fake: FakeAnthropic, parse_responses: list[object] | None = None, **overrides: Any
) -> AnthropicClient:
    if parse_responses is not None:
        fake.beta.messages.parse_responses = parse_responses
    return AnthropicClient(
        Settings(anthropic_api_key=TEST_KEY, llm_model=MODEL, **overrides), client=fake
    )


GENERATE = {
    "system": SYSTEM_TEXT,
    "messages": [],
    "schema": QuestionBatch,
    "purpose": "generation",
}


async def test_generate_happy_path_records_kwargs_and_returns_batch() -> None:
    fake = FakeAnthropic(FakeMessages())
    client = make_client(
        fake,
        [make_response(parsed=make_batch())],
        llm_effort_generation="high",
        llm_fallback="default",
    )  # type: ignore[arg-type]
    result = await client.generate(**{**GENERATE, "messages": source_messages()})  # type: ignore[arg-type]
    assert isinstance(result, QuestionBatch)
    kwargs = fake.beta.messages.parse_calls[0]
    assert kwargs["model"] == MODEL
    assert kwargs["max_tokens"] == 16000
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["system"] == [
        {"type": "text", "text": SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}}
    ]
    assert kwargs["messages"][0]["content"][0] == {
        "type": "text",
        "text": "<chapter>invented</chapter>",
        "cache_control": {"type": "ephemeral"},
    }
    assert kwargs["messages"][0]["content"][1] == {"type": "text", "text": "Write questions."}
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"


async def test_generate_without_fallback_omits_beta_args() -> None:
    fake = FakeAnthropic(FakeMessages())
    client = make_client(fake, [make_response(parsed=make_batch())], llm_fallback="off")
    await client.generate(**GENERATE)  # type: ignore[arg-type]
    kwargs = fake.beta.messages.parse_calls[0]
    assert "betas" not in kwargs
    assert "fallbacks" not in kwargs
    assert kwargs["model"] == MODEL


async def test_generate_purpose_is_accepted_for_generation_and_wrapup() -> None:
    fake = FakeAnthropic(FakeMessages())
    responses = [make_response(parsed=make_batch()), make_response(parsed=make_batch())]
    client = make_client(fake, responses)
    for purpose in ("generation", "wrapup"):
        result = await client.generate(
            system=SYSTEM_TEXT, messages=[], schema=QuestionBatch, purpose=purpose
        )
        assert isinstance(result, QuestionBatch)


async def test_stream_uses_tutor_model_and_effort() -> None:
    fake = FakeAnthropic(FakeMessages())
    fake.beta.messages.stream_result = FakeMessageStream(
        [
            SimpleNamespace(
                type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="Hello ")
            ),
            SimpleNamespace(
                type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="there.")
            ),
        ],
        make_response(stop_reason="end_turn"),
    )
    client = make_client(
        fake, llm_tutor_model="claude-haiku-5", llm_effort_tutor="low", llm_fallback="default"
    )  # type: ignore[arg-type]
    collected = [text async for text in client.stream(system=SYSTEM_TEXT, messages=[])]
    assert collected == ["Hello ", "there."]
    kwargs = fake.beta.messages.stream_calls[0]
    assert kwargs["model"] == "claude-haiku-5"
    assert kwargs["max_tokens"] == 8000
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"


async def test_generate_refusal() -> None:
    fake = FakeAnthropic(FakeMessages())
    client = make_client(fake, [make_response(stop_reason="refusal")])
    with pytest.raises(LLMRefused):
        await client.generate(**GENERATE)  # type: ignore[arg-type]
    assert len(fake.beta.messages.parse_calls) == 1


async def test_stream_refusal() -> None:
    fake = FakeAnthropic(FakeMessages())
    fake.beta.messages.stream_result = FakeMessageStream([], make_response(stop_reason="refusal"))
    client = make_client(fake)  # type: ignore[arg-type]
    with pytest.raises(LLMRefused):
        async for _text in client.stream(system=SYSTEM_TEXT, messages=[]):
            pass


async def test_generate_retries_once_on_invalid_output() -> None:
    fake = FakeAnthropic(FakeMessages())
    client = make_client(
        fake, [make_response(text="raw junk from the model"), make_response(parsed=make_batch())]
    )
    result = await client.generate(**{**GENERATE, "messages": source_messages()})  # type: ignore[arg-type]
    assert isinstance(result, QuestionBatch)
    assert len(fake.beta.messages.parse_calls) == 2
    retry_messages = fake.beta.messages.parse_calls[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": "raw junk from the model"}
    assert retry_messages[-1]["role"] == "user"
    assert retry_messages[-1]["content"].startswith("Your previous output was invalid:")
    assert retry_messages[-1]["content"].endswith("Return only valid JSON.")


async def test_generate_output_invalid_after_retry() -> None:
    fake = FakeAnthropic(FakeMessages())
    client = make_client(fake, [make_response(), make_response()])
    with pytest.raises(LLMOutputInvalid, match="after one retry"):
        await client.generate(**GENERATE)  # type: ignore[arg-type]


async def test_generate_retries_on_validation_failure_of_wrong_shape() -> None:
    payload = {"questions": "not-a-list"}
    fake = FakeAnthropic(FakeMessages())
    client = make_client(
        fake,
        [
            SimpleNamespace(
                stop_reason="end_turn",
                parsed_output=payload,
                content=[FakeTextBlock(_dumps(payload))],
            ),
            make_response(parsed=make_batch()),
        ],
    )
    result = await client.generate(**GENERATE)  # type: ignore[arg-type]
    assert isinstance(result, QuestionBatch)
    retry_user_turn = fake.beta.messages.parse_calls[1]["messages"][-1]
    assert "questions" in retry_user_turn["content"]


def _dumps(value: object) -> str:
    import json

    return json.dumps(value)


@pytest.mark.parametrize("name", list(sdk_errors()))
async def test_sdk_errors_map_to_llm_unavailable(name: str) -> None:
    fake = FakeAnthropic(FakeMessages())
    client = make_client(fake, [sdk_errors()[name]])
    with pytest.raises(LLMUnavailable, match=name):
        await client.generate(**GENERATE)  # type: ignore[arg-type]


async def test_stream_sdk_error_maps_to_unavailable() -> None:
    fake = FakeAnthropic(FakeMessages())
    fake.beta.messages.stream_result = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://example.invalid")
    )
    client = make_client(fake)  # type: ignore[arg-type]
    with pytest.raises(LLMUnavailable, match="APIConnectionError"):
        async for _text in client.stream(system=SYSTEM_TEXT, messages=[]):
            pass


async def test_missing_api_key_raises_unavailable_at_first_use() -> None:
    client = AnthropicClient(Settings(anthropic_api_key=""))
    with pytest.raises(LLMUnavailable, match="not configured"):
        await client.generate(**GENERATE)  # type: ignore[arg-type]


async def test_sdk_validation_error_triggers_one_retry() -> None:
    fake = FakeAnthropic(FakeMessages())
    try:
        QuestionBatch.model_validate({"questions": []})
    except ValidationError as error:
        sdk_error = error
    client = make_client(fake, [sdk_error, make_response(parsed=make_batch())])

    result = await client.generate(
        system=SYSTEM_TEXT, messages=[], schema=QuestionBatch, purpose="generation"
    )

    assert isinstance(result, QuestionBatch)
    retry_messages = fake.beta.messages.parse_calls[1]["messages"]
    assert retry_messages[-1]["role"] == "user"
    assert "previous output was invalid" in retry_messages[-1]["content"]
