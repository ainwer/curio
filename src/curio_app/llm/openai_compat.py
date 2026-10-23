"""OpenAI-compatible provider adapter."""

from collections.abc import AsyncIterator
from typing import Literal

import openai
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionChunk,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from curio_app.config import Settings
from curio_app.llm.base import (
    LLMError,
    LLMOutputInvalid,
    LLMUnavailable,
    Message,
    T,
    TextBlock,
)
from curio_app.llm.parsing import parse_output, schema_instruction


class OpenAICompatClient:
    """Provider adapter for any OpenAI-compatible chat completions endpoint."""

    def __init__(self, settings: Settings, client: openai.AsyncOpenAI | None = None) -> None:
        self._settings = settings
        self._injected = client
        self._created: openai.AsyncOpenAI | None = None

    def _sdk(self) -> openai.AsyncOpenAI:
        if self._injected is not None:
            return self._injected
        if self._created is None:
            key = self._settings.llm_api_key
            if key is None or not key.get_secret_value():
                raise LLMUnavailable("OpenAI-compatible API key is not configured.")
            self._created = openai.AsyncOpenAI(
                base_url=self._settings.llm_base_url,
                api_key=key.get_secret_value(),
            )
        return self._created

    async def generate(
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        purpose: Literal["generation", "wrapup"],
    ) -> T:
        try:
            return await self._generate(system, messages, schema)
        except LLMError:
            raise
        except openai.APIError as error:
            raise _unavailable(error) from None

    async def stream(self, *, system: str, messages: list[Message]) -> AsyncIterator[str]:
        try:
            async for text in self._stream(system, messages):
                yield text
        except LLMError:
            raise
        except openai.APIError as error:
            raise _unavailable(error) from None

    async def _generate(self, system: str, messages: list[Message], schema: type[T]) -> T:
        client = self._sdk()
        system_message: ChatCompletionSystemMessageParam = {
            "role": "system",
            "content": f"{system}\n\n{schema_instruction(schema)}",
        }
        request_messages = [system_message, *[_to_chat_message(m) for m in messages]]
        text = await self._complete(client, request_messages)
        try:
            return parse_output(text, schema)
        except LLMOutputInvalid as error:
            assistant_turn: ChatCompletionAssistantMessageParam = {
                "role": "assistant",
                "content": text,
            }
            user_turn: ChatCompletionUserMessageParam = {
                "role": "user",
                "content": f"Your previous output was invalid: {error}. Return only valid JSON.",
            }
            retry_messages = [*request_messages, assistant_turn, user_turn]
            try:
                return parse_output(await self._complete(client, retry_messages), schema)
            except LLMOutputInvalid:
                raise LLMOutputInvalid(
                    "The model output could not be parsed after one retry."
                ) from None

    async def _complete(
        self,
        client: openai.AsyncOpenAI,
        messages: list[
            ChatCompletionSystemMessageParam
            | ChatCompletionUserMessageParam
            | ChatCompletionAssistantMessageParam
        ],
    ) -> str:
        completion = await client.chat.completions.create(
            model=self._settings.llm_model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        choices = completion.choices
        return (choices[0].message.content if choices else None) or ""

    async def _stream(self, system: str, messages: list[Message]) -> AsyncIterator[str]:
        client = self._sdk()
        system_message: ChatCompletionSystemMessageParam = {"role": "system", "content": system}
        request_messages = [system_message, *[_to_chat_message(m) for m in messages]]
        completion_stream: openai.AsyncStream[
            ChatCompletionChunk
        ] = await client.chat.completions.create(
            model=self._settings.tutor_model,
            messages=request_messages,
            stream=True,
        )
        async for chunk in completion_stream:
            for choice in chunk.choices or []:
                content = choice.delta.content
                if content:
                    yield content


def _unavailable(error: Exception) -> LLMUnavailable:
    return LLMUnavailable(f"The language model is unavailable ({type(error).__name__}).")


def _to_chat_message(
    message: Message,
) -> ChatCompletionUserMessageParam | ChatCompletionAssistantMessageParam:
    content = _flatten(message["content"])
    if message["role"] == "user":
        return {"role": "user", "content": content}
    return {"role": "assistant", "content": content}


def _flatten(content: str | list[TextBlock]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content)
