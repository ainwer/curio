"""Route tests for the wrap-up flow: wrap-up, curation view and save."""

import sqlite3
from datetime import UTC, datetime

import gitutil
import pytest
from conftest import VaultEnv
from fastapi.testclient import TestClient

from curio_app.db import connect
from curio_app.domain import Question, Status
from curio_app.services import tutor, wrapup
from curio_app.vault.format import render_question
from curio_app.vault.git import GitRepo
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriter

NOW = datetime(2026, 10, 23, 8, 15, 0, tzinfo=UTC)
QUESTION_ID = "q-2026-10-23-invented-session"
CARD_TYPES = ["why", "estimate", "tradeoff", "transfer", "fact"]


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


def seed_researching(
    vault: VaultStore, env: VaultEnv, *, status: Status = Status.RESEARCHING
) -> Question:
    question = Question(
        id=QUESTION_ID,
        status=status,
        domain="power-electronics",
        concepts=["gate-drivers"],
        created=NOW,
        title="Why do invented gate drivers fail at high speed?",
    ).with_gut_answer("My invented hunch.")
    path = vault.root / vault.relative_question_path(question.id)
    path.write_text(render_question(question), encoding="utf-8")
    gitutil.run_git("add", ".", cwd=env.repos.app)
    gitutil.run_git("commit", "-m", "seed", cwd=env.repos.app)
    return question


def insert_chat(conn: sqlite3.Connection, *, streaming: bool = False) -> None:
    session = conn.execute(
        "SELECT 1 FROM tutor_sessions WHERE question_id = ?", (QUESTION_ID,)
    ).fetchone()
    if session is None:
        conn.execute(
            "INSERT INTO tutor_sessions (question_id, context, created_at) VALUES (?, ?, ?)",
            (QUESTION_ID, "invented context block", NOW.isoformat()),
        )
    conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'user', 'Why would it fail?', 'done', ?)",
        (QUESTION_ID, NOW.isoformat()),
    )
    state = "streaming" if streaming else "done"
    conn.execute(
        "INSERT INTO chat_messages (question_id, role, content, state, created_at)"
        " VALUES (?, 'assistant', 'The invented charge pump explains it.', ?, ?)",
        (QUESTION_ID, state, NOW.isoformat()),
    )
    conn.commit()


def wrap_up_once(vault_client: TestClient, conn: sqlite3.Connection, vault: VaultStore) -> None:
    response = vault_client.post(
        f"/questions/{QUESTION_ID}/wrapup",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303), response.text
    assert wrapup.get_wrapup(conn, QUESTION_ID) is not None


def oob_counter(fragment: str) -> bool:
    """The footer (counter and save button) is swapped out of band after each card action."""
    return (
        'id="curate-footer"' in fragment
        and 'id="curate-counter"' in fragment
        and "hx-swap-oob" in fragment
    )


async def test_wrapup_post_redirects_to_curate(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)

    htmx = vault_client.post(
        f"/questions/{QUESTION_ID}/wrapup", headers={"HX-Request": "true"}, follow_redirects=False
    )

    assert htmx.status_code == 200
    assert htmx.headers["HX-Redirect"] == f"/questions/{QUESTION_ID}?view=curate"
    stored = wrapup.get_wrapup(conn, QUESTION_ID)
    assert stored is not None
    assert len(stored.cards) == 5

    plain = vault_client.post(f"/questions/{QUESTION_ID}/wrapup", follow_redirects=False)
    assert plain.status_code == 303
    assert plain.headers["location"] == f"/questions/{QUESTION_ID}?view=curate"
    conn.close()


async def test_wrapup_post_requires_done_reply(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn, streaming=True)

    response = vault_client.post(f"/questions/{QUESTION_ID}/wrapup", headers={"HX-Request": "true"})

    assert response.status_code == 400
    assert "alert-error" in response.text
    assert "Talk to the tutor first." in response.text
    assert ">Wrap up</span>" in response.text  # the retry button is still there
    assert wrapup.get_wrapup(conn, QUESTION_ID) is None
    conn.close()


async def test_researching_page_shows_wrapup_actions(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)

    plain = vault_client.get(f"/questions/{QUESTION_ID}")

    assert f'hx-post="/questions/{QUESTION_ID}/wrapup"' in plain.text
    assert ">Wrap up</span>" in plain.text
    assert ">Summarizing…</span>" in plain.text
    assert "hx-confirm" not in plain.text
    assert "Review cards" not in plain.text
    conn.close()


async def test_researching_page_without_done_reply_disables_wrapup(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    conn.execute(
        "INSERT INTO tutor_sessions (question_id, context, created_at) VALUES (?, ?, ?)",
        (QUESTION_ID, "invented context block", NOW.isoformat()),
    )
    conn.commit()

    plain = vault_client.get(f"/questions/{QUESTION_ID}")

    assert f'hx-post="/questions/{QUESTION_ID}/wrapup"' not in plain.text
    assert 'class="btn btn-primary" disabled>' in plain.text
    assert "The tutor has to finish a reply first." in plain.text
    conn.close()


async def test_researching_page_after_wrapup_links_curate(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)

    wrap_up_once(vault_client, conn, vault)
    page = vault_client.get(f"/questions/{QUESTION_ID}")

    assert "Review cards" in page.text
    assert f'href="/questions/{QUESTION_ID}?view=curate"' in page.text
    assert ">Wrap up again</span>" in page.text
    assert 'hx-confirm="Replace the proposed cards?"' in page.text
    conn.close()


async def test_curate_view_requires_researching_and_wrapup(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter
) -> None:
    question = seed_researching(vault, vault_env, status=Status.INBOX)

    response = vault_client.get(f"/questions/{QUESTION_ID}?view=curate", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/questions/{QUESTION_ID}"

    conn = connect(vault_env.db_path)
    insert_chat(conn)
    response = vault_client.get(f"/questions/{QUESTION_ID}?view=curate", follow_redirects=False)
    assert response.status_code == 303

    await wrapup.create_wrapup(conn, question, llm=vault_env.fake)
    response = vault_client.get(f"/questions/{QUESTION_ID}?view=curate", follow_redirects=False)
    assert response.status_code == 200
    assert "Answer" in response.text
    conn.close()


async def test_curate_view_lists_cards_and_sections(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)
    wrap_up_once(vault_client, conn, vault)

    page = vault_client.get(f"/questions/{QUESTION_ID}?view=curate")

    assert "Fake answer." in page.text
    assert "Where you were wrong" in page.text
    assert "I thought A, but B." in page.text
    assert "Back to chat" in page.text
    assert 'id="curate-counter"' in page.text
    assert "0 accepted · aim for 2\u20133" in page.text
    assert f'action="/questions/{QUESTION_ID}/save"' in page.text
    assert "Save to vault" in page.text
    assert '<button type="submit" class="btn btn-primary" disabled' in page.text
    assert 'class="card card-draft is-proposed"' in page.text
    for card in wrapup.get_wrapup(conn, QUESTION_ID).cards:  # type: ignore[union-attr]
        assert f'id="draft-{card.id}"' in page.text
    for action in ("accept", "discard", "edit"):
        assert page.text.count("/" + action + '"') >= 5  # one button per proposed card
    conn.close()


async def test_card_accept_and_discard_actions(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)
    wrap_up_once(vault_client, conn, vault)
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    card_id = view.cards[0].id

    accepted = vault_client.post(
        f"/questions/{QUESTION_ID}/cards/{card_id}/accept", follow_redirects=False
    )

    assert accepted.status_code == 200
    assert f'id="draft-{card_id}"' in accepted.text
    assert 'class="card card-draft is-accepted"' in accepted.text
    assert "chip-accepted" in accepted.text
    assert oob_counter(accepted.text)
    assert "1 accepted · aim for 2\u20133" in accepted.text
    assert 'class="chip"' in accepted.text

    undo_target = view.cards[1].id
    discarded = vault_client.post(f"/questions/{QUESTION_ID}/cards/{undo_target}/discard")
    assert 'class="card card-draft is-discarded"' in discarded.text
    assert "Discarded" in discarded.text
    assert oob_counter(discarded.text)
    assert "1 accepted · aim for 2\u20133" in discarded.text
    assert f"cards/{undo_target}/propose" in discarded.text

    undone = vault_client.post(f"/questions/{QUESTION_ID}/cards/{undo_target}/propose")
    assert 'class="card card-draft is-proposed"' in undone.text
    assert f"cards/{undo_target}/discard" in undone.text
    assert wrapup.get_card(conn, undo_target) is not None
    assert wrapup.get_card(conn, undo_target).state == "proposed"  # type: ignore[union-attr]
    conn.close()


async def test_card_edit_flow(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)
    wrap_up_once(vault_client, conn, vault)
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    card_id = view.cards[0].id

    form = vault_client.post(f"/questions/{QUESTION_ID}/cards/{card_id}/edit")

    assert form.status_code == 200
    assert "card_edit_form" not in form.text
    assert 'name="type_value"' in form.text
    assert "<select" in form.text
    for value in CARD_TYPES:
        assert f'value="{value}"' in form.text
    assert 'name="front"' in form.text
    assert 'maxlength="300"' in form.text
    assert 'name="back"' in form.text
    assert 'maxlength="1000"' in form.text
    assert 'name="source"' in form.text
    assert 'maxlength="120"' in form.text
    assert 'name="edit_submitted" value="1"' in form.text
    assert ">Save</button>" in form.text
    assert f'hx-get="/questions/{QUESTION_ID}/cards/{card_id}"' in form.text

    saved = vault_client.post(
        f"/questions/{QUESTION_ID}/cards/{card_id}/edit",
        data={
            "edit_submitted": "1",
            "type_value": "transfer",
            "front": "Edited front?",
            "back": "Edited back.",
            "source": "Invented Book, ch. 3",
        },
    )

    assert saved.status_code == 200
    assert 'class="card card-draft is-accepted"' in saved.text
    assert "Edited front?" in saved.text
    assert oob_counter(saved.text)
    assert "1 accepted · aim for 2\u20133" in saved.text
    row = wrapup.get_card(conn, card_id)
    assert row is not None
    assert row.type.value == "transfer"
    assert row.front == "Edited front?"
    assert row.source == "Invented Book, ch. 3"

    canceled = vault_client.get(f"/questions/{QUESTION_ID}/cards/{card_id}")

    assert 'class="card card-draft is-accepted"' in canceled.text
    assert 'name="front"' not in canceled.text
    conn.close()


async def test_card_edit_validation_error(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)
    wrap_up_once(vault_client, conn, vault)
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    card_id = view.cards[0].id

    response = vault_client.post(
        f"/questions/{QUESTION_ID}/cards/{card_id}/edit",
        data={"edit_submitted": "1", "type_value": "why", "front": "   ", "back": "Back."},
    )

    assert response.status_code == 400
    assert "alert-error" in response.text
    assert "card front must be 1-300 characters." in response.text
    assert 'name="front"' in response.text  # the form stays open
    conn.close()


async def test_card_of_other_question_is_404(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)
    wrap_up_once(vault_client, conn, vault)
    other = wrapup.get_wrapup(conn, QUESTION_ID)
    assert other is not None
    card_id = other.cards[0].id

    for suffix in ("accept", "discard", "propose", "edit"):
        response = vault_client.post(
            f"/questions/q-2026-10-22-invented-stub/cards/{card_id}/{suffix}"
        )
        assert response.status_code == 404
    conn.close()


async def test_save_route_writes_and_redirects_to_done(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)
    wrap_up_once(vault_client, conn, vault)
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    accept_id = view.cards[0].id
    vault_client.post(f"/questions/{QUESTION_ID}/cards/{accept_id}/accept")
    vault_client.post(f"/questions/{QUESTION_ID}/cards/{view.cards[1].id}/discard")

    response = vault_client.post(f"/questions/{QUESTION_ID}/save", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/questions/{QUESTION_ID}"
    text = (vault.root / vault.relative_question_path(QUESTION_ID)).read_text(encoding="utf-8")
    assert "status: done" in text
    stored_cards = vault.get(QUESTION_ID).cards()
    assert [card.id for card in stored_cards] == [accept_id]

    done_page = vault_client.get(f"/questions/{QUESTION_ID}")

    assert ">done</span>" in done_page.text
    assert "Fake answer." in done_page.text
    assert "<strong>Answer:</strong>" in done_page.text  # the markdown renders the lead-in
    assert "Fake card 1: how does the mechanism behave?" in done_page.text
    assert "composer" not in done_page.text
    conn.close()


async def test_save_route_without_accepted_shows_error(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)
    wrap_up_once(vault_client, conn, vault)
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    vault_client.post(f"/questions/{QUESTION_ID}/cards/{view.cards[0].id}/discard")

    response = vault_client.post(f"/questions/{QUESTION_ID}/save", follow_redirects=False)

    assert response.status_code == 400
    assert "Accept at least one card." in response.text
    assert "alert-error" in response.text
    conn.close()


async def test_save_route_vault_failure_shows_warning_and_done(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter
) -> None:
    seed_researching(vault, vault_env)
    conn = connect(vault_env.db_path)
    insert_chat(conn)
    wrap_up_once(vault_client, conn, vault)
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    accept_id = view.cards[0].id
    vault_client.post(f"/questions/{QUESTION_ID}/cards/{accept_id}/accept")
    stray = vault_env.repos.app / "Learning" / "stray-note.md"
    stray.write_text("invented stray content\n", encoding="utf-8")

    response = vault_client.post(f"/questions/{QUESTION_ID}/save", follow_redirects=False)

    assert response.status_code == 303
    stray.unlink()
    row = conn.execute(
        "SELECT content FROM vault_drafts WHERE question_id = ?", (QUESTION_ID,)
    ).fetchone()
    assert row is not None
    assert "status: done" in str(row["content"])

    done_page = vault_client.get(f"/questions/{QUESTION_ID}")

    assert ">done</span>" in done_page.text  # the draft's own status wins
    assert "Fake answer." in done_page.text
    assert "Fake card 1: how does the mechanism behave?" in done_page.text
    assert "Saved locally; the vault sync will be retried from the inbox." in done_page.text
    conn.close()


async def test_save_preserves_gut_answer_from_pending_draft(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore, writer: VaultWriter
) -> None:
    """A failed gut-answer commit keeps its draft; the wrap-up save builds on that draft."""
    question = Question(
        id=QUESTION_ID,
        status=Status.INBOX,
        domain="power-electronics",
        concepts=["gate-drivers"],
        created=NOW,
        title="Why do invented gate drivers fail at high speed?",
    )
    path = vault.root / vault.relative_question_path(question.id)
    path.write_text(render_question(question), encoding="utf-8")
    gitutil.run_git("add", ".", cwd=vault_env.repos.app)
    gitutil.run_git("commit", "-m", "seed", cwd=vault_env.repos.app)
    stray = vault_env.repos.app / "Learning" / "stray-note.md"
    stray.write_text("invented stray content\n", encoding="utf-8")

    conn = connect(vault_env.db_path)
    from curio_app.books.store import BookStore

    vault_error = await tutor.submit_gut_answer(
        conn,
        question,
        "My invented hunch.",
        vault=vault,
        writer=writer,
        books=BookStore(conn, vault_env.settings.books_path),
        now=NOW,
    )
    assert vault_error is not None
    row = conn.execute(
        "SELECT content FROM vault_drafts WHERE question_id = ?", (QUESTION_ID,)
    ).fetchone()
    assert row is not None and "My invented hunch." in str(row["content"])

    insert_chat(conn)
    response = vault_client.post(f"/questions/{QUESTION_ID}/wrapup", headers={"HX-Request": "true"})
    assert response.status_code == 200
    view = wrapup.get_wrapup(conn, QUESTION_ID)
    assert view is not None
    accept_id = view.cards[0].id
    vault_client.post(f"/questions/{QUESTION_ID}/cards/{accept_id}/accept")

    stray.unlink()
    saved = vault_client.post(f"/questions/{QUESTION_ID}/save", follow_redirects=False)
    assert saved.status_code == 303

    text = (vault.root / vault.relative_question_path(QUESTION_ID)).read_text(encoding="utf-8")
    assert "status: done" in text
    assert "My invented hunch." in text  # the gut answer from the pending draft survived
    assert "Fake card 1: how does the mechanism behave?" in text
    done_page = vault_client.get(f"/questions/{QUESTION_ID}")
    assert ">done</span>" in done_page.text
    assert ">inbox</span>" not in done_page.text
    conn.close()
