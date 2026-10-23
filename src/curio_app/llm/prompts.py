"""Prompt templates and pure message builders."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from jinja2 import Environment, PackageLoader, StrictUndefined

from curio_app.llm.base import Message, TextBlock


@dataclass(frozen=True)
class Excerpt:
    """One retrieved book excerpt for the tutor context."""

    book: str
    chapter: int
    section: str
    text: str


@dataclass(frozen=True)
class NoteRef:
    """One retrieved vault note for the tutor context."""

    title: str
    text: str


@lru_cache(maxsize=1)
def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("curio_app.llm", "prompts"),
        # LLM prompts, not HTML; no autoescaping keeps the verbatim templates intact.
        autoescape=False,  # noqa: S701
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        trim_blocks=False,
    )


def render(name: str, **ctx: object) -> str:
    """Render a prompt template and strip surrounding whitespace."""
    return _environment().get_template(name).render(**ctx).strip()


def question_system(profile: str | None) -> str:
    """System prompt for the question generator."""
    return render("question_generator.system.md.j2", profile=profile)


def question_source_messages(
    *,
    book_title: str,
    chapter_number: int,
    chapter_title: str,
    chapter_text: str,
    existing: list[str],
) -> list[Message]:
    """One user message per chapter; the chapter block carries the cache breakpoint."""
    rendered = render(
        "question_generator.source.md.j2",
        book_title=book_title,
        chapter_number=chapter_number,
        chapter_title=chapter_title,
        chapter_text=chapter_text,
        existing=existing,
    )
    lines = rendered.split("\n")
    boundary = next(i for i, line in enumerate(lines) if line.strip() == "</chapter>")
    chapter_block = "\n".join(lines[: boundary + 1]) + "\n"
    instructions = "\n".join(lines[boundary + 1 :]).strip()
    return [
        {
            "role": "user",
            "content": [
                TextBlock(type="text", text=chapter_block, cache=True),
                TextBlock(type="text", text=instructions),
            ],
        }
    ]


def question_topic_messages(*, topic: str, existing: list[str]) -> list[Message]:
    """One user message for topic-mode generation."""
    text = render("question_generator.topic.md.j2", topic=topic, existing=existing)
    return [{"role": "user", "content": text}]


def question_bridge_messages(*, focus: str | None, existing: list[str]) -> list[Message]:
    """One user message for bridge-mode generation."""
    text = render("question_generator.bridge.md.j2", focus=focus, existing=existing)
    return [{"role": "user", "content": text}]


def tutor_system() -> str:
    """Cached system prompt for the Socratic tutor."""
    return render("tutor.system.md.j2")


def tutor_context(
    *,
    question: str,
    concepts: list[str],
    gut_answer: str,
    excerpts: list[Excerpt],
    notes: list[NoteRef],
) -> str:
    """The frozen first user message of a tutor session."""
    return render(
        "tutor.context.md.j2",
        question=question,
        concepts=concepts,
        gut_answer=gut_answer,
        excerpts=excerpts,
        notes=notes,
    )


def tutor_messages(
    *,
    context: str,
    history: list[tuple[Literal["user", "assistant"], str]],
) -> list[Message]:
    """Context (cached block) plus history; consecutive same-role turns merge.

    The caller guarantees that the list ends with a user message.
    """
    messages: list[Message] = [
        {"role": "user", "content": [TextBlock(type="text", text=context, cache=True)]}
    ]
    for role, text in history:
        previous = messages[-1]
        if previous["role"] != role:
            messages.append({"role": role, "content": text})
            continue
        content = previous["content"]
        if isinstance(content, str):
            previous["content"] = f"{content}\n\n{text}"
        else:
            content.append(TextBlock(type="text", text=f"\n\n{text}"))
    assert messages[-1]["role"] == "user"  # noqa: S101 - caller contract
    return messages


def wrapup_system() -> str:
    """System prompt for the wrap-up."""
    return render("wrapup.system.md.j2")


def wrapup_messages(
    *,
    question: str,
    gut_answer: str,
    transcript: list[tuple[Literal["user", "assistant"], str]],
) -> list[Message]:
    """One user message carrying question, gut answer and the full transcript."""
    turns = [{"role": role, "content": text} for role, text in transcript]
    text = render("wrapup.user.md.j2", question=question, gut_answer=gut_answer, messages=turns)
    return [{"role": "user", "content": text}]
