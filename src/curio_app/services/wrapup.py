"""Wrap-up generation, card curation and the vault save (PLAN.md §2, flows 4-6)."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from curio_app.domain import Card, CardType, Question, Status
from curio_app.llm.base import LLMClient, LLMError
from curio_app.llm.prompts import wrapup_messages, wrapup_system
from curio_app.llm.schemas import Wrapup
from curio_app.vault.ids import card_id
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import FileChange, VaultWriteError, VaultWriter

FRONT_MAX_CHARS = 300
BACK_MAX_CHARS = 1000
SOURCE_MAX_CHARS = 120

TALK_FIRST_ERROR = "Talk to the tutor first."
NO_ACCEPTED_ERROR = "Accept at least one card."

FRONT_ERROR = f"The card front must be 1-{FRONT_MAX_CHARS} characters."
BACK_ERROR = f"The card back must be 1-{BACK_MAX_CHARS} characters."
SOURCE_ERROR = f"The source must be at most {SOURCE_MAX_CHARS} characters without quotes."

Role = Literal["user", "assistant"]

_WRAPUP_MESSAGE = "Summarizing the research failed. Please try again."


class WrapupError(ValueError):
    """Invalid wrap-up input or state; the message is user-facing."""


@dataclass(frozen=True)
class DraftCard:
    """One card draft row of a question."""

    id: str
    question_id: str
    position: int
    type: CardType
    front: str
    back: str
    source: str | None
    state: str


@dataclass(frozen=True)
class WrapupView:
    """The stored wrap-up with its card drafts, ordered by position."""

    answer: str
    misconceptions: list[str]
    cards: list[DraftCard]


async def create_wrapup(conn: sqlite3.Connection, q: Question, *, llm: LLMClient) -> None:
    """Run the wrap-up LLM call and (re)place the proposal drafts for the question.

    Raises :class:`WrapupError` when no assistant turn is finished yet or the
    LLM call fails.
    """
    rows = conn.execute(
        "SELECT role, content, state FROM chat_messages WHERE question_id = ? ORDER BY id",
        (q.id,),
    ).fetchall()
    if not any(str(row["role"]) == "assistant" and str(row["state"]) == "done" for row in rows):
        raise WrapupError(TALK_FIRST_ERROR)
    transcript: list[tuple[Role, str]] = [
        (_role(str(row["role"])), str(row["content"]))
        for row in rows
        if str(row["state"]) == "done"
    ]
    try:
        result = await llm.generate(
            system=wrapup_system(),
            messages=wrapup_messages(
                question=q.title, gut_answer=q.gut_answer() or "", transcript=transcript
            ),
            schema=Wrapup,
            purpose="wrapup",
        )
    except LLMError as exc:
        raise WrapupError(str(exc)) from exc

    conn.execute(
        "INSERT INTO wrapups (question_id, answer, misconceptions, created_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(question_id) DO UPDATE SET"
        " answer = excluded.answer, misconceptions = excluded.misconceptions,"
        " created_at = excluded.created_at",
        (q.id, result.answer, json.dumps(result.misconceptions), datetime.now(UTC).isoformat()),
    )
    conn.execute(
        "DELETE FROM card_drafts WHERE question_id = ? AND state IN ('proposed','discarded')",
        (q.id,),
    )
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) FROM card_drafts WHERE question_id = ?",
        (q.id,),
    ).fetchone()
    base = int(row[0]) if row is not None else 0
    for draft in result.cards:
        base += 1
        conn.execute(
            "INSERT INTO card_drafts (id, question_id, position, type, front, back, source, state)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed')",
            (card_id(), q.id, base, draft.type.value, draft.front, draft.back, draft.source),
        )
    conn.commit()


def get_wrapup(conn: sqlite3.Connection, question_id: str) -> WrapupView | None:
    """The stored wrap-up with its card drafts, or None when never wrapped up."""
    row = conn.execute(
        "SELECT answer, misconceptions FROM wrapups WHERE question_id = ?", (question_id,)
    ).fetchone()
    if row is None:
        return None
    misconception_data = json.loads(str(row["misconceptions"]))
    return WrapupView(
        answer=str(row["answer"]),
        misconceptions=[str(item) for item in misconception_data],
        cards=list_cards(conn, question_id),
    )


def list_cards(conn: sqlite3.Connection, question_id: str) -> list[DraftCard]:
    """Card drafts of a question, ordered by position."""
    rows = conn.execute(
        "SELECT id, question_id, position, type, front, back, source, state FROM card_drafts"
        " WHERE question_id = ? ORDER BY position, rowid",
        (question_id,),
    ).fetchall()
    return [_card_from_row(row) for row in rows]


def get_card(conn: sqlite3.Connection, card_id_value: str) -> DraftCard | None:
    """One card draft by id, or None."""
    row = conn.execute(
        "SELECT id, question_id, position, type, front, back, source, state FROM card_drafts"
        " WHERE id = ?",
        (card_id_value,),
    ).fetchone()
    return _card_from_row(row) if row is not None else None


def accept_card(conn: sqlite3.Connection, card_id_value: str) -> None:
    """Mark one card draft accepted."""
    _set_state(conn, card_id_value, "accepted")


def discard_card(conn: sqlite3.Connection, card_id_value: str) -> None:
    """Mark one card draft discarded."""
    _set_state(conn, card_id_value, "discarded")


def propose_card(conn: sqlite3.Connection, card_id_value: str) -> None:
    """Re-propose a discarded card draft (undo)."""
    _set_state(conn, card_id_value, "proposed")


def update_card(
    conn: sqlite3.Connection,
    card_id_value: str,
    *,
    type: CardType,
    front: str,
    back: str,
    source: str | None,
) -> None:
    """Validate the edited fields, store them and accept the draft; the id stays.

    Raises :class:`WrapupError` for empty or too long fields or an invalid source.
    """
    stripped_front = front.strip()
    stripped_back = back.strip()
    if not stripped_front or len(stripped_front) > FRONT_MAX_CHARS:
        raise WrapupError(FRONT_ERROR)
    if not stripped_back or len(stripped_back) > BACK_MAX_CHARS:
        raise WrapupError(BACK_ERROR)
    if source is not None and not source.strip():
        source = None
    if source is not None and ('"' in source or "\n" in source or len(source) > SOURCE_MAX_CHARS):
        raise WrapupError(SOURCE_ERROR)
    conn.execute(
        "UPDATE card_drafts SET type = ?, front = ?, back = ?, source = ?, state = 'accepted'"
        " WHERE id = ?",
        (type.value, stripped_front, stripped_back, source, card_id_value),
    )
    conn.commit()


def render_insight(answer: str, misconceptions: list[str]) -> str:
    """The "Insight" section body from the wrap-up answer and misconceptions."""
    if misconceptions:
        items = "\n".join(f"- {item}" for item in misconceptions)
        wrong = f"\n\n**Where I was wrong:**\n{items}"
    else:
        wrong = "\n\n**Where I was wrong:** Nothing, the gut answer held up."
    return f"**Answer:** {answer}{wrong}"


async def save(
    conn: sqlite3.Connection, q: Question, *, vault: VaultStore, writer: VaultWriter
) -> str | None:
    """Write insight and the curated cards to the vault as a done question file.

    Returns the vault error message when the commit or push fails (the rendered
    file stays queued in ``vault_drafts`` and the inbox banner retries it), or
    ``None`` on success. Raises :class:`WrapupError` without accepted drafts.
    """
    accepted = [card for card in list_cards(conn, q.id) if card.state == "accepted"]
    if not accepted:
        raise WrapupError(NO_ACCEPTED_ERROR)
    kept_ids = {card.id for card in list_cards(conn, q.id)}
    kept = [card for card in q.cards() if card.id not in kept_ids]
    cards = [
        *kept,
        *(
            Card(
                id=draft.id,
                type=draft.type,
                front=draft.front,
                back=draft.back,
                source=draft.source,
            )
            for draft in accepted
        ),
    ]
    view = get_wrapup(conn, q.id)
    if view is None:
        raise WrapupError(TALK_FIRST_ERROR)
    updated = (
        q.with_insight(render_insight(view.answer, view.misconceptions))
        .with_cards(cards)
        .model_copy(update={"status": Status.DONE})
    )
    path, content = vault.render(updated)
    try:
        await writer.write([FileChange(q.id, path, content)], f"curio: cards {q.id}")
    except VaultWriteError as exc:
        return exc.message
    return None


def _set_state(conn: sqlite3.Connection, card_id_value: str, state: str) -> None:
    conn.execute("UPDATE card_drafts SET state = ? WHERE id = ?", (state, card_id_value))
    conn.commit()


def _card_from_row(row: sqlite3.Row) -> DraftCard:
    return DraftCard(
        id=str(row["id"]),
        question_id=str(row["question_id"]),
        position=int(row["position"]),
        type=CardType(str(row["type"])),
        front=str(row["front"]),
        back=str(row["back"]),
        source=row["source"],
        state=str(row["state"]),
    )


def _role(value: str) -> Role:
    return "assistant" if value == "assistant" else "user"
