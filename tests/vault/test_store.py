"""Tests for VaultStore: listing, get, profile and linked notes."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from curio_app.domain import Card, CardType, Question, Section, Status
from curio_app.vault.format import render_question
from curio_app.vault.ids import card_id
from curio_app.vault.store import InvalidFile, Note, QuestionNotFound, VaultStore

QUESTIONS_DIR = "Learning/Questions"
PROFILE_PATH = "Learning/Knowledge-Profile.md"


def make_question(qid: str, created: datetime, status: Status = Status.INBOX) -> Question:
    return Question(
        id=qid,
        status=status,
        domain="power-electronics",
        concepts=["sic"],
        created=created,
        title=f"Question {qid}",
        sections=[Section(heading="Gut answer", body=f"Answer for {qid}.")],
    )


def write_question(vault: Path, q: Question) -> None:
    path = vault / QUESTIONS_DIR / f"{q.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_question(q), encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def store(vault: Path) -> VaultStore:
    return VaultStore(vault, QUESTIONS_DIR, PROFILE_PATH)


def test_list_questions_empty_when_dir_missing(store: VaultStore) -> None:
    assert store.list_questions() == ([], [])


def test_list_questions_sorts_by_created_desc_then_id(store: VaultStore, vault: Path) -> None:
    write_question(
        vault,
        make_question("q-2026-10-23-first", datetime(2026, 10, 23, 8, 0, tzinfo=UTC)),
    )
    write_question(
        vault, make_question("q-2026-10-22-second", datetime(2026, 10, 22, 8, 0, tzinfo=UTC))
    )
    write_question(
        vault,
        make_question("q-2026-10-23-a-third", datetime(2026, 10, 23, 8, 0, tzinfo=UTC)),
    )
    questions, invalid = store.list_questions()
    assert [q.id for q in questions] == [
        "q-2026-10-23-a-third",
        "q-2026-10-23-first",
        "q-2026-10-22-second",
    ]
    assert invalid == []


def test_list_questions_reports_invalid_file_and_skips_other_notes(
    store: VaultStore, vault: Path
) -> None:
    write_question(
        vault, make_question("q-2026-10-23-fine", datetime(2026, 10, 23, 8, 0, tzinfo=UTC))
    )
    broken = vault / QUESTIONS_DIR / "q-2026-10-23-broken.md"
    broken.write_text("no front matter at all\n", encoding="utf-8")
    # no front matter: not a study-question, silently skipped
    note = vault / QUESTIONS_DIR / "my-note.md"
    note.write_text("---\ntype: meeting-note\n---\n# Note\n", encoding="utf-8")
    bad_status = vault / QUESTIONS_DIR / "q-2026-10-23-status.md"
    bad_status.write_text(
        "---\ntype: study-question\nid: q-2026-10-23-status\nstatus: archive\n"
        "domain: power-electronics\ncreated: 2026-10-23T08:00:00+00:00\n---\n# T\n",
        encoding="utf-8",
    )
    questions, invalid = store.list_questions()
    assert [q.id for q in questions] == ["q-2026-10-23-fine"]
    assert invalid == [
        InvalidFile(
            path="Learning/Questions/q-2026-10-23-status.md", error="invalid status 'archive'"
        )
    ]


def test_get_existing_and_missing(store: VaultStore, vault: Path) -> None:
    written = make_question("q-2026-10-23-target", datetime(2026, 10, 23, 8, 0, tzinfo=UTC))
    write_question(vault, written)
    assert store.get("q-2026-10-23-target") == written
    with pytest.raises(QuestionNotFound):
        store.get("q-2026-10-23-nothing")


def test_existing_ids(store: VaultStore, vault: Path) -> None:
    write_question(vault, make_question("q-2026-10-23-a", datetime(2026, 10, 23, 8, 0, tzinfo=UTC)))
    write_question(vault, make_question("q-2026-10-23-b", datetime(2026, 10, 23, 9, 0, tzinfo=UTC)))
    assert store.existing_ids() == {"q-2026-10-23-a", "q-2026-10-23-b"}


def test_relative_question_path(store: VaultStore) -> None:
    assert store.relative_question_path("q-2026-10-23-x") == "Learning/Questions/q-2026-10-23-x.md"


def test_render_returns_path_and_content(store: VaultStore) -> None:
    q = make_question("q-2026-10-23-render", datetime(2026, 10, 23, 8, 0, tzinfo=UTC))
    path, content = store.render(q)
    assert path == "Learning/Questions/q-2026-10-23-render.md"
    assert content == render_question(q)


def test_read_profile(store: VaultStore, vault: Path) -> None:
    assert store.read_profile() is None
    profile = vault / "Learning" / "Knowledge-Profile.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("   \n\t\n", encoding="utf-8")
    assert store.read_profile() is None
    profile.write_text("I know power electronics well.\n", encoding="utf-8")
    assert store.read_profile() == "I know power electronics well.\n"


def make_note(vault: Path, relative: str, text: str, front_matter: str | None = None) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"---\n{front_matter}\n---\n{text}" if front_matter else text
    path.write_text(body, encoding="utf-8")
    return path


def question_with_sections(*bodies: str) -> Question:
    sections = [
        Section(heading=heading, body=body)
        for heading, body in zip(("Gut answer", "Insight"), bodies, strict=False)
    ]
    return Question(
        id="q-2026-10-23-linked",
        status=Status.RESEARCHING,
        domain="power-electronics",
        concepts=["switching-losses", "sic"],
        created=datetime(2026, 10, 23, 8, 0, tzinfo=UTC),
        title="Why do SiC devices reduce switching losses?",
        preamble="See [[Wide-Bandgap]] and [[Wide-Bandgap|alias]].",
        sections=sections,
    )


def test_linked_notes_via_wikilink_variants_concept_and_dedupe(
    store: VaultStore, vault: Path
) -> None:
    make_note(
        vault,
        "Notes/Wide-Bandgap.md",
        "Wide bandgap means high critical field.\n",
        front_matter="tags: semiconductor",
    )
    make_note(vault, "Notes/Switching-Losses.md", "Losses scale linearly with frequency.\n")
    make_note(vault, "Deep/Nested/Sic.md", "Silicon carbide tolerates high fields.\n")
    make_note(vault, ".trash/Wide-Bandgap.md", "Deleted draft.\n")
    (vault / QUESTIONS_DIR / "Wide-Bandgap.md").parent.mkdir(parents=True, exist_ok=True)
    (vault / QUESTIONS_DIR / "Wide-Bandgap.md").write_text(
        "---\ntype: study-question\n---\nNot a note.\n", encoding="utf-8"
    )
    q = question_with_sections("Look at [[Wide-Bandgap#Basics]] for the field strength.", "")
    notes = store.linked_notes(q)
    assert [n.path for n in notes] == [
        "Notes/Wide-Bandgap.md",
        "Notes/Switching-Losses.md",
        "Deep/Nested/Sic.md",
    ]
    assert [n.title for n in notes] == ["Wide-Bandgap", "Switching-Losses", "Sic"]
    assert notes[0].text == "Wide bandgap means high critical field.\n"


def test_linked_notes_strip_front_matter(store: VaultStore, vault: Path) -> None:
    make_note(vault, "Notes/Switching-Losses.md", "Body text.\n", front_matter="tags: loss")
    q = question_with_sections("", "")
    notes = store.linked_notes(q)
    assert len(notes) == 1
    assert notes[0].text == "Body text.\n"


def test_linked_notes_deduplicates_same_path(store: VaultStore, vault: Path) -> None:
    make_note(vault, "Notes/Switching-Losses.md", "Losses.\n")
    q = question_with_sections("[[Switching-Losses]] here, [[Switching-Losses]] again.", "")
    notes = store.linked_notes(q)
    assert [n.path for n in notes] == ["Notes/Switching-Losses.md"]


def test_linked_notes_respects_limit_and_truncates(store: VaultStore, vault: Path) -> None:
    make_note(vault, "A.md", "A" * 100 + "\n")
    make_note(vault, "B.md", "B" * 100 + "\n")
    make_note(vault, "Notes/Switching-Losses.md", "C" * 100 + "\n")
    q = question_with_sections("[[A]] [[B]]", "")
    notes = store.linked_notes(q, max_notes=2, max_chars=50)
    assert [n.path for n in notes] == ["A.md", "B.md"]
    assert notes[0].text == "A" * 50 + "\n[…]"
    assert len(notes[0].text) == 50 + len("\n[…]")


def test_linked_notes_skips_symlinks_outside_root(
    store: VaultStore, vault: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("Secret outside text.\n", encoding="utf-8")
    (vault / "Nested").mkdir(parents=True)
    linked = vault / "Nested" / "Outside.md"
    linked.symlink_to(outside)
    q = question_with_sections("[[Outside]]", "")
    assert store.linked_notes(q) == []


def test_store_skips_binary_file_as_invalid(store: VaultStore, vault: Path) -> None:
    (vault / QUESTIONS_DIR).mkdir(parents=True)
    (vault / QUESTIONS_DIR / "binary.md").write_bytes(b"\xff\xfe\x00binary")
    questions, invalid = store.list_questions()
    assert questions == []
    assert invalid == [InvalidFile(path="Learning/Questions/binary.md", error="not UTF-8 text")]


def test_full_cycle_write_read_cards(store: VaultStore, vault: Path) -> None:
    card = Card(
        id=card_id(),
        type=CardType.FACT,
        front="What is tested here?",
        back="The full write and read cycle.",
    )
    q = make_question("q-2026-10-23-cycle", datetime(2026, 10, 23, 8, 0, tzinfo=UTC))
    q = q.with_gut_answer("Gut.").with_insight("Fine.").with_cards([card])
    path, content = store.render(q)
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    read_back = store.get("q-2026-10-23-cycle")
    assert read_back == q
    assert read_back.cards() == [card]


def test_note_dataclass_fields() -> None:
    note = Note(title="T", path="p.md", text="text")
    assert note.title == "T"
    assert note.path == "p.md"
    assert note.text == "text"
