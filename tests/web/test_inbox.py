"""Route tests for the inbox page, filters, vault retry and the question page."""

from datetime import UTC, datetime

import gitutil
import pytest
from conftest import VaultEnv
from fastapi.testclient import TestClient

from curio_app.db import connect
from curio_app.domain import Question, Status
from curio_app.services import generation
from curio_app.vault.format import render_question
from curio_app.vault.git import GitRepo
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriter

NOW = datetime(2026, 10, 23, 8, 15, 0, tzinfo=UTC)


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


async def _accepted_question(
    vault_env: VaultEnv,
    vault: VaultStore,
    writer: VaultWriter,
    *,
    expect_failure: bool = False,
) -> str | None:
    conn = connect(vault_env.db_path)
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
    conn = connect(vault_env.db_path)
    try:
        run = generation.get_run(conn, run_id)
        assert run is not None
        suggestion_id = run.suggestions[0].id
        if expect_failure:
            stray = vault_env.repos.app / "Learning" / "stray-note.md"
            stray.write_text("invented stray content\n", encoding="utf-8")
            from curio_app.vault.writer import VaultWriteError

            with pytest.raises(VaultWriteError):
                await generation.accept(conn, [suggestion_id], vault=vault, writer=writer, now=NOW)
            return None
        question_ids = await generation.accept(
            conn, [suggestion_id], vault=vault, writer=writer, now=NOW
        )
        return question_ids[0]
    finally:
        conn.close()


def _seed_question(
    vault: VaultStore,
    *,
    qid: str,
    title: str,
    domain: str,
    status: Status,
) -> Question:
    question = Question(
        id=qid,
        status=status,
        domain=domain,
        concepts=["invented"],
        created=NOW,
        title=title,
    )
    path = vault.root / vault.relative_question_path(question.id)
    path.write_text(render_question(question), encoding="utf-8")
    return question


def test_inbox_lists_questions_from_the_vault(
    vault_client: TestClient,
    vault_env: VaultEnv,
    vault: VaultStore,
    writer: VaultWriter,
) -> None:
    qid = _run_async(_accepted_question(vault_env, vault, writer))

    page = vault_client.get("/")

    assert page.status_code == 200
    assert "Why does fake concept" in page.text
    assert f'href="/questions/{qid}"' in page.text
    assert "fake-domain" in page.text
    assert ">inbox</span>" in page.text
    assert "2026-10-23" in page.text


def test_inbox_filter_chips_and_status_filtering(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    _seed_question(
        vault,
        qid="q-2026-01-01-invented-research",
        title="Why does an invented coil saturate?",
        domain="control-systems",
        status=Status.RESEARCHING,
    )
    _seed_question(
        vault,
        qid="q-2026-01-02-invented-finished",
        title="What limits an invented capacitor?",
        domain="power-electronics",
        status=Status.DONE,
    )

    default_page = vault_client.get("/")
    all_page = vault_client.get("/?status=all")
    done_page = vault_client.get("/?status=done")
    inbox_page = vault_client.get("/?status=inbox")

    assert default_page.text.count('class="question-row card"') == 1
    assert "saturate" in default_page.text
    assert "invented capacitor" not in default_page.text
    assert all_page.text.count('class="question-row card"') == 2
    assert done_page.text.count('class="question-row card"') == 1
    assert "invented capacitor" in done_page.text
    assert ">done</span>" in done_page.text
    assert inbox_page.text.count('class="question-row card"') == 0
    assert "No questions match these filters." in inbox_page.text
    assert 'href="/?status=open"' in all_page.text
    assert 'href="/?status=done"' in all_page.text
    assert 'href="/?status=all"' in all_page.text
    assert '&amp;domain=control-systems"' in all_page.text
    assert '&amp;domain=power-electronics"' in all_page.text


def test_inbox_domain_filter(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    _seed_question(
        vault,
        qid="q-2026-01-01-invented-control",
        title="Why does an invented loop oscillate?",
        domain="control-systems",
        status=Status.INBOX,
    )
    _seed_question(
        vault,
        qid="q-2026-01-02-invented-filter",
        title="What limits an invented filter?",
        domain="power-electronics",
        status=Status.INBOX,
    )

    page = vault_client.get("/?domain=control-systems")
    other = vault_client.get("/?domain=power-electronics")

    assert page.text.count('class="question-row card"') == 1
    assert "loop oscillate" in page.text
    assert other.text.count('class="question-row card"') == 1
    assert "invented filter" in other.text


def test_inbox_empty_state_when_no_questions(vault_client: TestClient) -> None:
    page = vault_client.get("/")

    assert "No questions yet. Generate some to get started." in page.text
    assert 'href="/generate"' in page.text


def test_inbox_shows_invalid_file_alert(
    vault_client: TestClient, vault_env: VaultEnv, vault: VaultStore
) -> None:
    broken = vault.root / "Learning" / "Questions" / "q-2026-01-03-broken.md"
    broken.write_text(
        "---\ntype: study-question\n---\n# Missing all required fields\n", encoding="utf-8"
    )

    page = vault_client.get("/")

    assert "1 file(s) could not be read" in page.text
    assert "q-2026-01-03-broken.md" in page.text
    broken.unlink()


async def test_draft_banner_and_retry(
    vault_client: TestClient,
    vault_env: VaultEnv,
    vault: VaultStore,
    writer: VaultWriter,
) -> None:
    await _accepted_question(vault_env, vault, writer, expect_failure=True)
    stray = vault_env.repos.app / "Learning" / "stray-note.md"

    page = vault_client.get("/")

    assert "1 change(s) could not be saved to the vault" in page.text
    assert "uncommitted changes" in page.text
    assert 'hx-post="/vault/retry"' in page.text

    stray.unlink()
    retry = vault_client.post("/vault/retry", follow_redirects=False)
    assert retry.status_code == 303
    assert retry.headers["location"] == "/"

    page = vault_client.get("/")
    assert "could not be saved to the vault" not in page.text
    conn = connect(vault_env.db_path)
    try:
        drafts = conn.execute("SELECT * FROM vault_drafts").fetchall()
        assert drafts == []
    finally:
        conn.close()
    # The retry wrote the accepted question into the vault.
    assert "Why does fake concept" in page.text


async def test_vault_retry_error_keeps_banner(
    vault_client: TestClient,
    vault_env: VaultEnv,
    vault: VaultStore,
    writer: VaultWriter,
) -> None:
    await _accepted_question(vault_env, vault, writer, expect_failure=True)
    stray = vault_env.repos.app / "Learning" / "stray-note.md"

    retry_html = vault_client.post(
        "/vault/retry", headers={"HX-Request": "true"}, follow_redirects=False
    )

    assert retry_html.status_code == 200
    assert "alert-error" in retry_html.text
    assert "uncommitted changes" in retry_html.text
    assert stray.is_file()
    stray.unlink()


def _run_async(coroutine):
    import asyncio

    return asyncio.run(coroutine)
