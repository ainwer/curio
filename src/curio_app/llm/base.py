"""Provider-neutral LLM interface, message types and error hierarchy."""

from collections.abc import AsyncIterator
from typing import Literal, NotRequired, Protocol, TypedDict, TypeVar

from pydantic import BaseModel


class TextBlock(TypedDict):
    """A plain text block; ``cache`` marks a provider-neutral cache breakpoint."""

    type: Literal["text"]
    text: str
    cache: NotRequired[bool]


class Message(TypedDict):
    """One conversation turn; list content only exists to carry cache breakpoints."""

    role: Literal["user", "assistant"]
    content: str | list[TextBlock]


class LLMError(Exception):
    """Base error; the message is user-safe and never contains keys or raw responses."""


class LLMUnavailable(LLMError):
    """Network problems, auth failures, rate limits or server errors."""


class LLMRefused(LLMError):
    """The model declined the request (``stop_reason == "refusal"``)."""


class LLMOutputInvalid(LLMError):
    """The output could not be parsed or validated after one retry."""


T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    """What the application needs from an LLM provider."""

    async def generate(
        self,
        *,
        system: str,
        messages: list[Message],
        schema: type[T],
        purpose: Literal["generation", "wrapup"],
    ) -> T: ...

    def stream(self, *, system: str, messages: list[Message]) -> AsyncIterator[str]: ...
