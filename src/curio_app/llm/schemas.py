"""Output schemas the LLM must produce, with post-validation."""

import re

from pydantic import BaseModel, ConfigDict, field_validator

from curio_app.domain import CardType

__all__ = ["CardDraft", "CardType", "GeneratedQuestion", "QuestionBatch", "Wrapup", "kebab"]


def kebab(value: str) -> str:
    """Normalise a name to kebab-case: lowercase, spaces/underscores become dashes."""
    lower = value.lower()
    dashed = re.sub(r"[\s_]+", "-", lower)
    return re.sub(r"[^a-z0-9-]", "", dashed)


def _optional_not_empty(value: str | None) -> str | None:
    """Map an empty string to ``None``; otherwise strip."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class GeneratedQuestion(BaseModel):
    """One question card proposed by the question generator."""

    model_config = ConfigDict(extra="ignore")

    question: str
    domain: str
    concepts: list[str]
    source: str | None
    hook: str
    bridge_from: str | None

    @field_validator("question", mode="after")
    @classmethod
    def _question_ends_with_question_mark(cls, value: str) -> str:
        value = value.strip()
        if not value.endswith("?"):
            raise ValueError("Question must end with '?'.")
        return value

    @field_validator("domain", mode="after")
    @classmethod
    def _domain_kebab(cls, value: str) -> str:
        return kebab(value)

    @field_validator("concepts", mode="after")
    @classmethod
    def _concepts_kebab_deduped(cls, value: list[str]) -> list[str]:
        concepts: list[str] = []
        for item in value:
            name = kebab(item)
            if name and name not in concepts:
                concepts.append(name)
        if not 1 <= len(concepts) <= 4:
            raise ValueError("One question targets 1-4 concepts.")
        return concepts

    @field_validator("hook", mode="after")
    @classmethod
    def _hook_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Hook must not be empty.")
        return value

    @field_validator("source", mode="after")
    @classmethod
    def _source_or_empty(cls, value: str | None) -> str | None:
        return _optional_not_empty(value)

    @field_validator("bridge_from", mode="after")
    @classmethod
    def _bridge_from_or_empty(cls, value: str | None) -> str | None:
        return _optional_not_empty(value)


class QuestionBatch(BaseModel):
    """A list of generated questions; exact count per mode is a prompt instruction."""

    model_config = ConfigDict(extra="ignore")

    questions: list[GeneratedQuestion]

    @field_validator("questions", mode="after")
    @classmethod
    def _dedupe_and_count(cls, value: list[GeneratedQuestion]) -> list[GeneratedQuestion]:
        seen: set[str] = set()
        deduped: list[GeneratedQuestion] = []
        for question in value:
            key = question.question.casefold().strip()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(question)
        if not 5 <= len(deduped) <= 10:
            raise ValueError("A question batch must contain 5-10 questions.")
        return deduped


class CardDraft(BaseModel):
    """One knowledge card proposal for curation."""

    model_config = ConfigDict(extra="ignore")

    type: CardType
    front: str
    back: str
    source: str | None

    @field_validator("front", mode="after")
    @classmethod
    def _front_ends_with_question_mark(cls, value: str) -> str:
        value = value.strip()
        if not value.endswith("?"):
            raise ValueError("Card front must end with '?'.")
        return value

    @field_validator("back", mode="after")
    @classmethod
    def _back_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Card back must not be empty.")
        return value

    @field_validator("source", mode="after")
    @classmethod
    def _source_or_empty(cls, value: str | None) -> str | None:
        return _optional_not_empty(value)


class Wrapup(BaseModel):
    """Wrap-up result: the answer, corrected misconceptions and card proposals."""

    model_config = ConfigDict(extra="ignore")

    answer: str
    misconceptions: list[str]
    cards: list[CardDraft]

    @field_validator("answer", mode="after")
    @classmethod
    def _answer_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Answer must not be empty.")
        return value

    @field_validator("cards", mode="after")
    @classmethod
    def _cards_count(cls, value: list[CardDraft]) -> list[CardDraft]:
        if not 4 <= len(value) <= 6:
            raise ValueError("A wrap-up must propose 4-6 cards.")
        return value
