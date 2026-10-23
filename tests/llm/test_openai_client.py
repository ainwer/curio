"""Tests for the OpenAI-compatible adapter using an injected fake SDK client."""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from curio_app.config import Settings
from curio_app.llm.base import LLMOutputInvalid, LLMUnavailable
from curio_app.llm.openai_compat import OpenAICompatClient
from curio_app.llm.parsing import schema_instruction
from curio_app.llm.schemas import QuestionBatch

MODEL = "invented-model"
SYSTEM_TEXT = "System prompt text."
BASE_URL = "https://example.invalid/v1"


def make_completion(content: str) -> object:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def batch_json() -> str:
    questions = [
        {
            "question": f"Why does concept {index} matter?",
            "domain": "fake-domain",
            "concepts": [f"concept-{index}"],
            "source": None,
            "hook": "Invented for tests.",
            "bridge_from": None,
        }
        for index in range(1, 6)
    ]
    return json.dumps({"questions": questions})


def make_stream(chunks: list[object]) -> AsyncIterator:
    async def iterate() -> AsyncIterator:
        for chunk in chunks:
            yield chunk

    return iterate()


class FakeCompletions:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[object] = list(responses or [])

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(
    responses: list[object] | None = None,
) -> tuple[OpenAICompatClient, FakeCompletions]:
    completions = FakeCompletions(list(responses or []))
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = OpenAICompatClient(
        Settings(
            llm_model=MODEL,
            llm_base_url=BASE_URL,
            llm_api_key="sk-test-not-real-0000",
        ),
        client=sdk,  # type: ignore[arg-type]
    )
    return client, completions


GENERATE = {
    "system": SYSTEM_TEXT,
    "messages": [],
    "schema": QuestionBatch,
    "purpose": "generation",
}


async def test_generate_happy_path() -> None:
    client, completions = make_client([make_completion(batch_json())])
    result = await client.generate(**GENERATE)  # type: ignore[arg-type]
    assert isinstance(result, QuestionBatch)
    kwargs = completions.calls[0]
    assert kwargs["model"] == MODEL
    assert kwargs["response_format"] == {"type": "json_object"}
    messages = kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith(SYSTEM_TEXT)
    assert schema_instruction(QuestionBatch) in messages[0]["content"]


async def test_generate_output_invalid_after_retry() -> None:
    client, completions = make_client([make_completion("{not json"), make_completion("[still]")])
    with pytest.raises(LLMOutputInvalid, match="after one retry"):
        await client.generate(**GENERATE)  # type: ignore[arg-type]
    assert len(completions.calls) == 2


async def test_generate_recovers_on_retry() -> None:
    client, completions = make_client([make_completion("{not json"), make_completion(batch_json())])
    result = await client.generate(**GENERATE)  # type: ignore[arg-type]
    assert isinstance(result, QuestionBatch)
    assert len(completions.calls) == 2
    retry_messages = completions.calls[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": "{not json"}
    assert retry_messages[-1]["role"] == "user"
    assert "Your previous output was invalid" in retry_messages[-1]["content"]


async def test_api_error_maps_to_unavailable() -> None:
    request = httpx.Request("POST", "https://example.invalid")
    error = openai.APIStatusError(
        "upstream down", response=httpx.Response(500, request=request), body=None
    )
    client, _completions = make_client([error])
    with pytest.raises(LLMUnavailable, match="APIStatusError"):
        await client.generate(**GENERATE)  # type: ignore[arg-type]


async def test_connection_error_maps_to_unavailable() -> None:
    error = openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    client, _completions = make_client([error])
    with pytest.raises(LLMUnavailable, match="APIConnectionError"):
        await client.generate(**GENERATE)  # type: ignore[arg-type]


async def test_stream_yields_deltas() -> None:
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hi "))])
    chunk2 = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="there."))])
    client, completions = make_client([make_stream([chunk, chunk2])])
    collected = [text async for text in client.stream(system=SYSTEM_TEXT, messages=[])]
    assert collected == ["Hi ", "there."]
    kwargs = completions.calls[0]
    assert kwargs["stream"] is True
    assert kwargs["model"] == "invented-model"


async def test_stream_skips_chunks_without_content() -> None:
    empty = SimpleNamespace(choices=[])
    silent = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))])
    voiced = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="only"))])
    client, _completions = make_client([make_stream([empty, silent, voiced])])
    collected = [text async for text in client.stream(system=SYSTEM_TEXT, messages=[])]
    assert collected == ["only"]


async def test_stream_error_maps_to_unavailable() -> None:
    error = openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    client, _completions = make_client([error])
    with pytest.raises(LLMUnavailable, match="APIConnectionError"):
        async for _text in client.stream(system=SYSTEM_TEXT, messages=[]):
            pass


async def test_missing_api_key_raises_unavailable_at_first_use() -> None:
    client = OpenAICompatClient(Settings(llm_base_url=BASE_URL, llm_api_key=""))
    with pytest.raises(LLMUnavailable, match="not configured"):
        await client.generate(**GENERATE)  # type: ignore[arg-type]


def test_schema_instruction_appended_to_system() -> None:
    instruction = schema_instruction(QuestionBatch)
    assert instruction.startswith("Return only a JSON object matching this JSON Schema:")
