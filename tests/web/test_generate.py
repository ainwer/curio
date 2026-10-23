"""Route tests for the generate page, run fragment and suggestion actions."""

import pytest
from books.support import make_ready_book
from conftest import VaultEnv
from fastapi.testclient import TestClient

from curio_app.books.store import BookStore
from curio_app.services import generation
from curio_app.vault.store import VaultStore


@pytest.fixture
def vault(vault_env: VaultEnv) -> VaultStore:
    return VaultStore(
        vault_env.settings.vault_path,
        vault_env.settings.vault_questions_dir,
        vault_env.settings.vault_profile_path,
    )


async def _completed_topic_run(vault_env: VaultEnv, vault: VaultStore) -> int:
    conn = connect_db(vault_env)
    try:
        run_id = generation.start_run(conn, "topic", "li-ion cells")
    finally:
        conn.close()
    await generation.execute_run(
        run_id,
        db_path=vault_env.db_path,
        llm=vault_env.fake,
        vault=vault,
        books_path=vault_env.settings.books_path,
    )
    return run_id


def connect_db(vault_env: VaultEnv):
    from curio_app.db import connect

    return connect(vault_env.db_path)


def test_generate_page_defaults_to_topic_without_books(vault_client: TestClient) -> None:
    response = vault_client.get("/generate")

    assert response.status_code == 200
    topic_input = '<input type="radio" name="mode-picker" id="pick-topic" value="topic" checked>'
    assert topic_input in response.text
    assert 'id="pick-source" value="source">' in response.text
    assert 'hx-post="/generate" hx-target="#run" hx-swap="innerHTML"' in response.text
    assert "Upload a book" in response.text


def test_generate_page_defaults_to_source_with_ready_book(
    vault_client: TestClient, book_store: BookStore
) -> None:
    make_ready_book(book_store, "Invented Book", "## 1 Start\n\nInvented chapter body.\n")

    response = vault_client.get("/generate")

    assert response.status_code == 200
    source_input = '<input type="radio" name="mode-picker" id="pick-source" value="source" checked>'
    assert source_input in response.text
    assert '<optgroup label="Invented Book">' in response.text
    assert "Uses your knowledge profile." in response.text


def test_generate_page_preselects_requested_chapter(
    vault_client: TestClient, book_store: BookStore
) -> None:
    book_id = make_ready_book(book_store, "Invented Book", "## 1 Start\n\nA body.\n")
    chapter = book_store.chapters(book_id)[0]

    response = vault_client.get(f"/generate?chapter={chapter.id}")

    assert f'<option value="{chapter.id}" selected>' in response.text


def test_generate_page_accepts_mode_query(vault_client: TestClient) -> None:
    response = vault_client.get("/generate?mode=bridge")

    bridge_input = '<input type="radio" name="mode-picker" id="pick-bridge" value="bridge" checked>'
    assert bridge_input in response.text


def test_topic_run_shows_pending_spinner(vault_client: TestClient) -> None:
    response = vault_client.post("/generate", data={"mode": "topic", "topic": "li-ion cells"})

    assert response.status_code == 200
    assert "Generating questions" in response.text
    assert 'hx-get="/generate/runs/1"' in response.text
    assert 'hx-trigger="every 2s"' in response.text
    assert 'hx-swap="outerHTML"' in response.text


def test_topic_run_rejects_blank_topic(vault_client: TestClient) -> None:
    response = vault_client.post("/generate", data={"mode": "topic", "topic": "   "})

    assert response.status_code == 200
    assert "alert-error" in response.text
    assert "Enter a topic of 1-200 characters." in response.text


def test_topic_run_rejects_unknown_chapter(vault_client: TestClient) -> None:
    response = vault_client.post("/generate", data={"mode": "source", "chapter_id": "9999"})

    assert response.status_code == 200
    assert "Choose a chapter of a ready book." in response.text


async def test_run_fragment_lists_suggestion_cards(
    vault_client: TestClient,
    vault_env: VaultEnv,
    vault: VaultStore,
) -> None:
    run_id = await _completed_topic_run(vault_env, vault)

    response = vault_client.get(f"/generate/runs/{run_id}")

    assert response.status_code == 200
    assert response.text.count('class="suggestion card') == 8
    assert '<input class="suggestion-check" type="checkbox" name="suggestion_id"' in response.text
    assert "<span data-selected-count>8</span>" in response.text
    assert "Add selected" in response.text
    assert 'hx-post="/suggestions/accept"' in response.text
    assert 'hx-include="input.suggestion-check:checked"' in response.text
    assert 'hx-post="/suggestions/1/accept"' in response.text
    assert 'hx-post="/suggestions/2/dismiss"' in response.text


async def test_accept_route_swaps_card_to_added(
    vault_client: TestClient,
    vault_env: VaultEnv,
    vault: VaultStore,
) -> None:
    run_id = await _completed_topic_run(vault_env, vault)
    conn = connect_db(vault_env)
    try:
        run = generation.get_run(conn, run_id)
        assert run is not None
        suggestion_id = run.suggestions[0].id
    finally:
        conn.close()

    response = vault_client.post(f"/suggestions/{suggestion_id}/accept")

    assert response.status_code == 200
    assert "Added" in response.text
    assert 'href="/questions/' in response.text
    fragment = vault_client.get(f"/generate/runs/{run_id}")
    assert fragment.text.count("is-added") == 1
    assert fragment.text.count('class="suggestion card') == 8


async def test_accept_route_write_failure_shows_alert(
    vault_client: TestClient,
    vault_env: VaultEnv,
    vault: VaultStore,
) -> None:
    run_id = await _completed_topic_run(vault_env, vault)
    conn = connect_db(vault_env)
    try:
        run = generation.get_run(conn, run_id)
        assert run is not None
        suggestion_id = run.suggestions[0].id
    finally:
        conn.close()
    stray = vault_env.repos.app / "Learning" / "stray-note.md"
    stray.write_text("invented stray content\n", encoding="utf-8")

    response = vault_client.post(f"/suggestions/{suggestion_id}/accept")

    assert response.status_code == 200
    assert "alert-error" in response.text
    assert "uncommitted changes" in response.text
    stray.unlink()


async def test_dismiss_route_removes_card(
    vault_client: TestClient,
    vault_env: VaultEnv,
    vault: VaultStore,
) -> None:
    run_id = await _completed_topic_run(vault_env, vault)
    conn = connect_db(vault_env)
    try:
        run = generation.get_run(conn, run_id)
        assert run is not None
        suggestion_id = run.suggestions[0].id
    finally:
        conn.close()

    response = vault_client.post(f"/suggestions/{suggestion_id}/dismiss")

    assert response.status_code == 200
    assert response.text == ""
    fragment = vault_client.get(f"/generate/runs/{run_id}")
    assert fragment.text.count('class="suggestion card') == 7


async def test_bulk_accept_writes_all_selected(
    vault_client: TestClient,
    vault_env: VaultEnv,
    vault: VaultStore,
) -> None:
    run_id = await _completed_topic_run(vault_env, vault)
    conn = connect_db(vault_env)
    try:
        run = generation.get_run(conn, run_id)
        assert run is not None
        ids = [suggestion.id for suggestion in run.suggestions[:2]]
    finally:
        conn.close()

    form_data = {"suggestion_id": [str(sid) for sid in ids]}
    response = vault_client.post("/suggestions/accept", data=form_data)

    assert response.status_code == 200
    assert response.text.count("is-added") == 2
    page = vault_client.get("/")
    assert "Why does fake concept" in page.text


async def test_bulk_accept_without_selection_writes_nothing(
    vault_client: TestClient,
    vault_env: VaultEnv,
    vault: VaultStore,
) -> None:
    await _completed_topic_run(vault_env, vault)
    response = vault_client.post("/suggestions/accept")

    assert response.status_code == 200
    assert response.text.count("is-added") == 0
    assert "<span data-selected-count>8</span>" in response.text
