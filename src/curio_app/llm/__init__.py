"""LLM provider layer: prompts, output parsing and provider adapters."""

from curio_app.llm.base import (
    LLMClient,
    LLMError,
    LLMOutputInvalid,
    LLMRefused,
    LLMUnavailable,
    Message,
    TextBlock,
)
from curio_app.llm.factory import create_llm_client
from curio_app.llm.prompts import Excerpt, NoteRef
from curio_app.llm.schemas import (
    CardDraft,
    CardType,
    GeneratedQuestion,
    QuestionBatch,
    Wrapup,
)

__all__ = [
    "CardDraft",
    "CardType",
    "Excerpt",
    "GeneratedQuestion",
    "LLMClient",
    "LLMError",
    "LLMOutputInvalid",
    "LLMRefused",
    "LLMUnavailable",
    "Message",
    "NoteRef",
    "QuestionBatch",
    "TextBlock",
    "Wrapup",
    "create_llm_client",
]
