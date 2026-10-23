"""Tests for the wrap-up service: proposals, curation and the vault save."""

import sqlite3
from datetime import UTC, datetime

import gitutil
import pytest
from conftest import VaultEnv

from curio_app.db import connect
from curio_app.domain import Card, CardType, Question, Status
from curio_app.llm.base import LLMRefused
from curio_app.llm.fake import FakeLLMClient
from curio_app.services import tutor, wrapup
from curio_app.vault.format import parse_question, render_question
from curio_app.vault.git import GitRepo
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriter

NOW = datetime(2026, 10, 23, 8, 15, 0, tzinfo=UTC)
QUESTION_ID = "q-2026-10-23-invented-curation"


@pytest.fixture
def vault(vault_env: VaultEnv) -> VaultStore:
    return VaultStore(
        vault_env.settings.vault_path,
        vault_env.settings.vault_questions_dir,
        vault_env.settings.vault_profile_path,
    )


@pytest.fixture
def writer(vault_env: VaultEnv) -> VaultWriter:
    return VaultWriter(
        GitRepo(vault_env.repos.app, branch=gitutil.BRANCH),
        vault_env.repos.app,
        vault_env.db_path,
        vault_env.lock_path,
    )


def seed_question(
    vault: VaultStore,
    env: VaultEnv,
    *,
    status: Status = Status.RESEARCHING,
    with_cards: list[Card] | None = None,
) -> Question:
    question = Question(
        id=QUESTION_ID,
        status=status,
        domain="power-electronics",
        concepts=["gate-drivers"],
        created=NOW,
        title="Why do invented gate drivers fail at high speed?",
    )
    question = question.with_gut_answer("My invented hunch.")
    if with_cards is not None:
        question = question.with_cards(with_cards)
    path = vault.root / vault.relative_question_path(question.id)
    path.write_text(render_question(question), encoding="utf-8")
    gitutil.run_git("add", ".", cwd=env.repos.app)
    gitutil.run_git("commit", "-m", "seed", cwd=env.repos.app)
    return question


def seed_session_and_chat(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO tutor_sessions (question_id, context, created_at) VALUES (?, ?, ?)",
        (QUESTION_ID, "invented context block", NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'user', 'Why would it fail?', 'done', ?)",
        (QUESTION_ID, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'assistant', 'The invented charge pump explains it.', 'done', ?)",
        (QUESTION_ID, NOW.isoformat()),
    )
    conn.commit()


def draft_states(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT id, state FROM card_drafts WHERE question_id = ?", (QUESTION_ID,)
    ).fetchall()
    return {str(row["id"]): str(row["state"]) for row in rows}


async def test_create_wrapup_stores_proposals_and_answer(
    vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    seed_session_and_chat(conn)
    fake = FakeLLMClient()

    await wrapup.create_wrapup(conn, vault.get(QUESTION_ID), llm=fake)

    stored = wrapup.get_wrapup(conn, QUESTION_ID)
    assert stored is not None
    assert stored.answer == "Fake answer."
    assert stored.misconceptions == ["I thought A, but B."]
    assert [(card.position, card.state) for card in stored.cards] == [
        (1, "proposed"),
        (2, "proposed"),
        (3, "proposed"),
        (4, "proposed"),
        (5, "proposed"),
    ]
    assert all(card.id.startswith("c-") for card in stored.cards)
    assert stored.cards[0].front == "Fake card 1: how does the mechanism behave?"
    user_content = fake.calls[0]["messages"][0]["content"]  # type: ignore[index]
    text = user_content if isinstance(user_content, str) else str(user_content)
    assert "Why do invented gate drivers fail at high speed?" in text
    assert "My invented hunch." in text
    assert "Why would it fail?" in text
    conn.close()


async def test_create_wrapup_requires_done_assistant(
    vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    conn.execute(
        "INSERT INTO tutor_sessions (question_id, context, created_at) VALUES (?, ?, ?)",
        (QUESTION_ID, "invented context block", NOW.isoformat()),
    )
    conn.commit()

    with pytest.raises(wrapup.WrapupError, match=r"Talk to the tutor first\."):
        await wrapup.create_wrapup(conn, vault.get(QUESTION_ID), llm=FakeLLMClient())

    conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'user', 'Still no reply.', 'done', ?)",
        (QUESTION_ID, NOW.isoformat()),
    )
    conn.commit()

    with pytest.raises(wrapup.WrapupError, match=r"Talk to the tutor first\."):
        await wrapup.create_wrapup(conn, vault.get(QUESTION_ID), llm=FakeLLMClient())

    conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'assistant', 'Now finished.', 'streaming', ?)",
        (QUESTION_ID, NOW.isoformat()),
    )
    conn.commit()

    with pytest.raises(wrapup.WrapupError, match=r"Talk to the tutor first\."):
        await wrapup.create_wrapup(conn, vault.get(QUESTION_ID), llm=FakeLLMClient())

    assert wrapup.get_wrapup(conn, QUESTION_ID) is None
    conn.close()


async def test_create_wrapup_rerun_keeps_accepted_and_replaces_rest(
    vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    seed_session_and_chat(conn)
    await wrapup.create_wrapup(conn, vault.get(QUESTION_ID), llm=FakeLLMClient())
    first = wrapup.get_wrapup(conn, QUESTION_ID)
    assert first is not None
    wrapup.accept_card(conn, first.cards[0].id)
    wrapup.discard_card(conn, first.cards[1].id)

    await wrapup.create_wrapup(conn, vault.get(QUESTION_ID), llm=FakeLLMClient())

    second = wrapup.get_wrapup(conn, QUESTION_ID)
    assert second is not None
    states = draft_states(conn)
    assert states[first.cards[0].id] == "accepted"  # kept, same id
    positions = {card.id: card.position for card in second.cards}
    assert positions[first.cards[0].id] == 1
    new_ids = [card.id for card in second.cards if card.id != first.cards[0].id]
    assert len(new_ids) == 5
    assert all(states[new_id] == "proposed" for new_id in new_ids)
    assert [positions[new_id] for new_id in new_ids] == [2, 3, 4, 5, 6]
    assert first.cards[1].id not in states  # discarded draft replaced
    assert len(second.cards) == 6
    conn.close()


async def test_card_states_and_edit_validation(vault_env: VaultEnv, vault: VaultStore) -> None:
    seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    seed_session_and_chat(conn)
    await wrapup.create_wrapup(conn, vault.get(QUESTION_ID), llm=FakeLLMClient())
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    target_id = view.cards[0].id

    wrapup.accept_card(conn, target_id)
    accepted = wrapup.get_card(conn, target_id)
    assert accepted is not None and accepted.state == "accepted"
    wrapup.discard_card(conn, target_id)
    discarded = wrapup.get_card(conn, target_id)
    assert discarded is not None and discarded.state == "discarded"
    wrapup.propose_card(conn, target_id)
    proposed = wrapup.get_card(conn, target_id)
    assert proposed is not None and proposed.state == "proposed"

    wrapup.update_card(
        conn,
        target_id,
        type=CardType.ESTIMATE,
        front="  What is the invented order of magnitude?  ",
        back="Invented back text.",
        source=" Invented Book, ch. 2 ",
    )
    edited = wrapup.get_card(conn, target_id)
    assert edited is not None
    assert edited.id == target_id  # id unchanged
    assert edited.state == "accepted"
    assert edited.type is CardType.ESTIMATE
    assert edited.front == "What is the invented order of magnitude?"
    assert edited.back == "Invented back text."
    assert edited.source == " Invented Book, ch. 2 "

    _assert_update_fails(conn, target_id, front="", match="card front")
    _assert_update_fails(conn, target_id, front="x" * 301, match="card front")
    _assert_update_fails(conn, target_id, back="", match="card back")
    _assert_update_fails(conn, target_id, back="x" * 1001, match="card back")
    _assert_update_fails(conn, target_id, source='a"b', match="source")
    _assert_update_fails(conn, target_id, source="a\nb", match="source")
    _assert_update_fails(conn, target_id, source="x" * 121, match="source")
    after_failure = wrapup.get_card(conn, target_id)
    assert after_failure is not None
    assert after_failure.state == "accepted"
    assert after_failure.front == "What is the invented order of magnitude?"
    conn.close()


def _assert_update_fails(
    conn: sqlite3.Connection,
    card_id_value: str,
    *,
    front: str = "Front?",
    back: str = "Back.",
    source: str | None = None,
    match: str,
) -> None:
    with pytest.raises(wrapup.WrapupError, match=match):
        wrapup.update_card(
            conn, card_id_value, type=CardType.WHY, front=front, back=back, source=source
        )


def test_render_insight_variants() -> None:
    assert wrapup.render_insight("Right for one reason.", ["I thought X, but Y."]) == (
        "**Answer:** Right for one reason.\n\n**Where I was wrong:**\n- I thought X, but Y."
    )
    assert wrapup.render_insight("Right.", ["a", "b"]) == (
        "**Answer:** Right.\n\n**Where I was wrong:**\n- a\n- b"
    )
    assert wrapup.render_insight("The gut answer was exact.", []) == (
        "**Answer:** The gut answer was exact.\n\n"
        "**Where I was wrong:** Nothing, the gut answer held up."
    )


async def test_save_writes_done_file_with_accepted_cards(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter
) -> None:
    question = seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    seed_session_and_chat(conn)
    await wrapup.create_wrapup(conn, question, llm=FakeLLMClient())
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    accept_ids = [view.cards[0].id, view.cards[2].id]
    for accept_id in accept_ids:
        wrapup.accept_card(conn, accept_id)
    wrapup.discard_card(conn, view.cards[1].id)

    error = await wrapup.save(conn, question, vault=vault, writer=writer)

    assert error is None
    app_head = gitutil.head_of(vault_env.repos.app)
    assert gitutil.run_git("rev-parse", "origin/main", cwd=vault_env.repos.app) == app_head
    text = (vault.root / vault.relative_question_path(QUESTION_ID)).read_text(encoding="utf-8")
    stored = parse_question(text)
    assert stored.status is Status.DONE
    assert stored.insight() == wrapup.render_insight("Fake answer.", ["I thought A, but B."])
    cards = stored.cards()
    assert [card.id for card in cards] == accept_ids  # exactly the accepted draft ids
    assert cards[0].type is CardType.WHY
    assert cards[1].type is CardType.TRADEOFF
    assert "My invented hunch." in text  # gut answer survives the save
    conn.close()


async def test_save_keeps_ids_and_updates_in_place(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter
) -> None:
    question = seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    seed_session_and_chat(conn)
    await wrapup.create_wrapup(conn, question, llm=FakeLLMClient())
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    accept_id = view.cards[0].id
    wrapup.accept_card(conn, accept_id)
    assert await wrapup.save(conn, question, vault=vault, writer=writer) is None
    assert [card.id for card in vault.get(QUESTION_ID).cards()] == [accept_id]

    wrapup.update_card(
        conn, accept_id, type=CardType.FACT, front="Edited front?", back="Edited back.", source=None
    )
    assert await wrapup.save(conn, vault.get(QUESTION_ID), vault=vault, writer=writer) is None

    cards = vault.get(QUESTION_ID).cards()
    assert [card.id for card in cards] == [accept_id]  # same id, no duplicate
    assert cards[0].front == "Edited front?"
    assert cards[0].back == "Edited back."
    assert cards[0].type is CardType.FACT
    commits = gitutil.run_git("log", "--oneline", cwd=vault_env.repos.app)
    assert commits.count(f"curio: cards {QUESTION_ID}") == 2
    conn.close()


async def test_save_requires_accepted_cards(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter
) -> None:
    question = seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    seed_session_and_chat(conn)
    await wrapup.create_wrapup(conn, question, llm=FakeLLMClient())
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    wrapup.discard_card(conn, view.cards[0].id)

    with pytest.raises(wrapup.WrapupError, match=r"Accept at least one card\."):
        await wrapup.save(conn, question, vault=vault, writer=writer)

    text = (vault.root / vault.relative_question_path(QUESTION_ID)).read_text(encoding="utf-8")
    assert "status: researching" in text
    conn.close()


async def test_save_vault_failure_keeps_draft_and_status(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter
) -> None:
    question = seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    seed_session_and_chat(conn)
    await wrapup.create_wrapup(conn, question, llm=FakeLLMClient())
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    accept_id = view.cards[0].id
    wrapup.accept_card(conn, accept_id)
    stray = vault_env.repos.app / "Learning" / "stray-note.md"
    stray.write_text("invented stray content\n", encoding="utf-8")

    message = await wrapup.save(conn, vault.get(QUESTION_ID), vault=vault, writer=writer)

    expected = "The vault checkout has uncommitted changes. Clean it up on the server, then retry."
    assert message == expected
    row = conn.execute(
        "SELECT content, commit_message FROM vault_drafts WHERE question_id = ?", (QUESTION_ID,)
    ).fetchone()
    assert row is not None
    assert "status: done" in str(row["content"])
    assert str(row["commit_message"]) == f"curio: cards {QUESTION_ID}"
    file_question = parse_question(
        (vault.root / vault.relative_question_path(QUESTION_ID)).read_text(encoding="utf-8")
    )
    assert file_question.status is Status.RESEARCHING
    assert tutor.effective_status(file_question, conn) is Status.DONE
    assert draft_states(conn)[accept_id] == "accepted"  # the curation survives the failure
    stray.unlink()
    conn.close()


async def test_create_wrapup_llm_error_maps_to_wrapup_error(
    vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    seed_session_and_chat(conn)

    class _RefusingClient(FakeLLMClient):
        async def generate(  # type: ignore[no-untyped-def]
            self, *, system: str, messages: list, schema: type, purpose: str
        ):
            raise LLMRefused("The model declined this request.")

    with pytest.raises(wrapup.WrapupError, match="declined"):
        await wrapup.create_wrapup(
            conn,
            vault.get(QUESTION_ID),
            llm=_RefusingClient(),  # type: ignore[arg-type]
        )
    assert wrapup.get_wrapup(conn, QUESTION_ID) is None
    assert wrapup.list_cards(conn, QUESTION_ID) == []
    conn.close()


async def test_failed_save_then_retry_keeps_edited_cards(
    vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter
) -> None:
    question = seed_question(vault, vault_env)
    conn = connect(vault_env.db_path)
    seed_session_and_chat(conn)
    await wrapup.create_wrapup(conn, question, llm=FakeLLMClient())
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    first_id = view.cards[0].id
    second_id = view.cards[1].id
    wrapup.accept_card(conn, first_id)
    stray = vault_env.repos.app / "Learning" / "stray-note.md"
    stray.write_text("invented stray content\n", encoding="utf-8")
    message = await wrapup.save(conn, question, vault=vault, writer=writer)
    assert message is not None

    wrapup.accept_card(conn, second_id)
    wrapup.update_card(
        conn, second_id, type=CardType.FACT, front="Edited front?", back="Edited back.", source=None
    )
    stray.unlink()
    assert await wrapup.save(conn, vault.get(QUESTION_ID), vault=vault, writer=writer) is None

    cards = vault.get(QUESTION_ID).cards()
    assert {card.id for card in cards} == {first_id, second_id}
    assert cards[1].front == "Edited front?"
    conn.close()
