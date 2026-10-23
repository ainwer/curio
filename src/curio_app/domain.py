"""Domain models for study questions, sections and knowledge cards (PLAN.md §4)."""

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, field_validator

QUESTION_ID_PATTERN = r"^q-\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*$"
CARD_ID_PATTERN = r"^c-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
KEBAB_CASE_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"

KNOWN_SECTION_HEADINGS: tuple[str, ...] = ("Gut answer", "Insight", "Cards")


class Status(StrEnum):
    """Lifecycle state of a question."""

    INBOX = "inbox"
    RESEARCHING = "researching"
    DONE = "done"


class CardType(StrEnum):
    """Knowledge card kinds (PLAN.md §7)."""

    WHY = "why"
    ESTIMATE = "estimate"
    TRADEOFF = "tradeoff"
    TRANSFER = "transfer"
    FACT = "fact"


class Card(BaseModel):
    """One knowledge card inside the "Cards" section."""

    id: str
    type: CardType
    front: str
    back: str
    source: str | None = None

    @field_validator("id")
    @classmethod
    def _card_id(cls, value: str) -> str:
        if re.fullmatch(CARD_ID_PATTERN, value) is None:
            raise ValueError(f"invalid card id '{value}'")
        return value

    @field_validator("front", "back")
    @classmethod
    def _stripped_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("front and back must not be empty")
        return stripped

    @field_validator("source", mode="before")
    @classmethod
    def _empty_source_to_none(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value

    @field_validator("source")
    @classmethod
    def _no_quotes_or_newlines(cls, value: str | None) -> str | None:
        if value is not None and ('"' in value or "\n" in value):
            raise ValueError("source must not contain '\"' or newline")
        return value


class Section(BaseModel):
    """One "## Heading" block with its raw body text."""

    heading: str
    body: str


class Question(BaseModel):
    """One study-question file in the vault."""

    id: str
    status: Status
    domain: str
    concepts: list[str] = []
    source: str | None = None
    created: datetime
    title: str
    preamble: str = ""
    sections: list[Section] = []
    extra_frontmatter: dict[str, str | list[str]] = {}

    @field_validator("id")
    @classmethod
    def _question_id(cls, value: str) -> str:
        if re.fullmatch(QUESTION_ID_PATTERN, value) is None:
            raise ValueError(f"invalid question id '{value}'")
        return value

    @field_validator("domain")
    @classmethod
    def _domain_kebab_case(cls, value: str) -> str:
        if re.fullmatch(KEBAB_CASE_PATTERN, value) is None:
            raise ValueError(f"invalid domain '{value}'")
        return value

    @field_validator("concepts")
    @classmethod
    def _concepts_kebab_case(cls, value: list[str]) -> list[str]:
        for concept in value:
            if re.fullmatch(KEBAB_CASE_PATTERN, concept) is None:
                raise ValueError(f"invalid concept '{concept}'")
        return value

    @field_validator("source", mode="before")
    @classmethod
    def _empty_source_to_none(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value

    @field_validator("source")
    @classmethod
    def _no_quotes_or_newlines(cls, value: str | None) -> str | None:
        if value is not None and ('"' in value or "\n" in value):
            raise ValueError("source must not contain '\"' or newline")
        return value

    @field_validator("created")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("created must be timezone-aware")
        return value

    def gut_answer(self) -> str | None:
        """Body of the "Gut answer" section, or None when absent or empty."""
        return self._section_body("Gut answer")

    def insight(self) -> str | None:
        """Body of the "Insight" section, or None when absent or empty."""
        return self._section_body("Insight")

    def cards(self) -> list[Card]:
        """Cards parsed from the "Cards" section; empty when absent or empty."""
        from curio_app.vault.format import parse_cards

        body = self._section_body("Cards")
        if body is None:
            return []
        return parse_cards(body)

    def with_gut_answer(self, text: str) -> "Question":
        """Return a copy with the "Gut answer" section set (or inserted)."""
        return self._with_section("Gut answer", text.strip())

    def with_insight(self, text: str) -> "Question":
        """Return a copy with the "Insight" section set (or inserted)."""
        return self._with_section("Insight", text.strip())

    def with_cards(self, cards: list[Card]) -> "Question":
        """Return a copy with the "Cards" section rendered from cards."""
        from curio_app.vault.format import render_cards

        return self._with_section("Cards", render_cards(cards))

    def _section_body(self, heading: str) -> str | None:
        for section in self.sections:
            if section.heading == heading:
                return section.body if section.body.strip() else None
        return None

    def _with_section(self, heading: str, body: str) -> "Question":
        sections = list(self.sections)
        for index, section in enumerate(sections):
            if section.heading == heading:
                sections[index] = section.model_copy(update={"body": body})
                return self.model_copy(update={"sections": sections})
        known_index = KNOWN_SECTION_HEADINGS.index(heading)
        preceding = -1
        for index, section in enumerate(sections):
            if (
                section.heading in KNOWN_SECTION_HEADINGS
                and KNOWN_SECTION_HEADINGS.index(section.heading) < known_index
            ):
                preceding = index
        if preceding >= 0:
            sections.insert(preceding + 1, Section(heading=heading, body=body))
            return self.model_copy(update={"sections": sections})
        for index, section in enumerate(sections):
            if (
                section.heading in KNOWN_SECTION_HEADINGS
                and KNOWN_SECTION_HEADINGS.index(section.heading) > known_index
            ):
                sections.insert(index, Section(heading=heading, body=body))
                return self.model_copy(update={"sections": sections})
        sections.append(Section(heading=heading, body=body))
        return self.model_copy(update={"sections": sections})
