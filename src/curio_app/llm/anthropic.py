"""Anthropic provider adapter using the beta structured-output namespace."""

from collections.abc import AsyncIterator
from typing import Any, Literal

import anthropic
from anthropic.types.beta import (
    BetaAssistantMessageParam,
    BetaMessageParam,
    BetaTextBlockParam,
    BetaUserMessageParam,
)
from anthropic.types.beta.parsed_beta_message import ParsedBetaMessage
from pydantic import ValidationError

from curio_app.config import Settings
from curio_app.llm.base import (
    LLMError,
    LLMOutputInvalid,
    LLMRefused,
    LLMUnavailable,
    Message,
    T,
    TextBlock,
)
from curio_app.llm.parsing import _describe_validation_error

_GENERATION_MAX_TOKENS = 16000
_TUTOR_MAX_TOKENS = 8000
_FALLBACK_BETAS = ("server-side-fallback-2026-07-01",)
_FALLBACKS = "default"
_SDK_ERROR_TYPES: tuple[type[Exception], ...] = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.AuthenticationError,
    anthropic.InternalServerError,
    anthropic.APIStatusError,
)


class AnthropicClient:
    """Provider adapter for Anthropic's structured-output beta API."""

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._settings = settings
        self._injected = client
        self._created: anthropic.AsyncAnthropic | None = None

    def _sdk(self) -> anthropic.AsyncAnthropic:
        if self._injected is not None:
            return self._injected
        if self._created is None:
            key = self._settings.anthropic_api_key
            if key is None or not key.get_secret_value():
                raise LLMUnavailable("Anthropic API key is not configured.")
            self._created = anthropic.AsyncAnthropic(api_key=key.get_secret_value(), max_retries=2)
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
        except _SDK_ERROR_TYPES as error:
            raise _unavailable(error) from None

    async def stream(self, *, system: str, messages: list[Message]) -> AsyncIterator[str]:
        try:
            async for text in self._stream(system, messages):
                yield text
        except LLMError:
            raise
        except _SDK_ERROR_TYPES as error:
            raise _unavailable(error) from None

    async def _generate(self, system: str, messages: list[Message], schema: type[T]) -> T:
        client = self._sdk()
        request_messages = _to_api_messages(messages)
        parsed, note, raw = await self._attempt(client, system, request_messages, schema)
        if parsed is not None:
            return parsed
        retry_messages: list[BetaMessageParam] = list(request_messages)
        if raw:
            assistant_turn: BetaAssistantMessageParam = {"role": "assistant", "content": raw}
            retry_messages.append(assistant_turn)
        user_turn: BetaUserMessageParam = {
            "role": "user",
            "content": f"Your previous output was invalid: {note}. Return only valid JSON.",
        }
        retry_messages.append(user_turn)
        parsed, _note, _raw = await self._attempt(client, system, retry_messages, schema)
        if parsed is None:
            raise LLMOutputInvalid("The model output could not be parsed after one retry.")
        return parsed

    async def _attempt(
        self,
        client: anthropic.AsyncAnthropic,
        system: str,
        request_messages: list[BetaMessageParam],
        schema: type[T],
    ) -> tuple[T | None, str, str]:
        """Run one parse call; returns (instance, note, raw text), never raises on bad output."""
        try:
            response = await _parse(client, system, request_messages, schema, self)
        except ValidationError as error:
            # The SDK validates the text against the schema itself and raises on mismatch.
            return None, _describe_validation_error(error), ""
        if response.stop_reason == "refusal":
            raise LLMRefused("The model declined this request.")
        parsed, note = _validated(response.parsed_output, schema)
        return parsed, note, _raw_text(response) if parsed is None else ""

    async def _stream(self, system: str, messages: list[Message]) -> AsyncIterator[str]:
        client = self._sdk()
        async with client.beta.messages.stream(
            model=self._settings.tutor_model,
            max_tokens=_TUTOR_MAX_TOKENS,
            system=_system_block(system),
            messages=_to_api_messages(messages),
            output_config={"effort": self._settings.llm_effort_tutor},
            **self._fallback_kwargs(),
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield delta.text
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            raise LLMRefused("The model declined this request.")

    def _fallback_kwargs(self) -> dict[str, Any]:
        if self._settings.llm_fallback == "default":
            return {"betas": list(_FALLBACK_BETAS), "fallbacks": _FALLBACKS}
        return {}


def _unavailable(error: Exception) -> LLMUnavailable:
    return LLMUnavailable(f"The language model is unavailable ({type(error).__name__}).")


async def _parse(
    client: anthropic.AsyncAnthropic,
    system: str,
    request_messages: list[BetaMessageParam],
    schema: type[T],
    provider: AnthropicClient,
) -> ParsedBetaMessage[T]:
    return await client.beta.messages.parse(
        model=provider._settings.llm_model,
        max_tokens=_GENERATION_MAX_TOKENS,
        system=_system_block(system),
        messages=request_messages,
        output_format=schema,
        output_config={"effort": provider._settings.llm_effort_generation},
        **provider._fallback_kwargs(),
    )


def _system_block(system: str) -> list[BetaTextBlockParam]:
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _to_api_messages(messages: list[Message]) -> list[BetaMessageParam]:
    converted: list[BetaMessageParam] = []
    for message in messages:
        content = _to_api_content(message["content"])
        if message["role"] == "user":
            converted.append({"role": "user", "content": content})
        else:
            converted.append({"role": "assistant", "content": content})
    return converted


def _to_api_content(content: str | list[TextBlock]) -> str | list[BetaTextBlockParam]:
    if isinstance(content, str):
        return content
    blocks: list[BetaTextBlockParam] = []
    for block in content:
        entry: BetaTextBlockParam = {"type": "text", "text": block["text"]}
        if block.get("cache"):
            entry["cache_control"] = {"type": "ephemeral"}
        blocks.append(entry)
    return blocks


def _validated(parsed: object, schema: type[T]) -> tuple[T | None, str]:
    """Validate a parsed output; returns the instance or a short failure note."""
    if parsed is None:
        return None, "no structured output present"
    try:
        return schema.model_validate(parsed), ""
    except ValidationError as error:
        return None, _describe_validation_error(error)


def _raw_text(response: object) -> str:
    content = getattr(response, "content", [])
    parts: list[str] = []
    if isinstance(content, (list, tuple)):
        for block in content:
            if getattr(block, "type", None) == "text":
                parts.append(str(getattr(block, "text", "")))
    return "\n".join(parts)
