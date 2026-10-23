"""Reading questions, the knowledge profile and linked notes from the vault."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from curio_app.domain import Question
from curio_app.vault.format import FormatError, parse_question, render_question
from curio_app.vault.ids import slugify

WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")
EXCLUDED_DIRS = frozenset({".git", ".obsidian", ".trash"})


@dataclass(frozen=True)
class InvalidFile:
    """A question file that violates the format; path is relative to the vault root."""

    path: str
    error: str


@dataclass(frozen=True)
class Note:
    """One markdown note in the vault; title is the file stem, text has no front matter."""

    title: str
    path: str
    text: str


class QuestionNotFound(LookupError):
    """No question file with the given id exists in the vault."""


class VaultStore:
    """Read access to question files plus helpers for rendering and note lookup."""

    def __init__(self, root: Path, questions_dir: str, profile_path: str) -> None:
        self.root = root
        self.questions_dir = questions_dir
        self.profile_path = profile_path
        self._resolved_root = root.resolve()

    def relative_question_path(self, question_id: str) -> str:
        """Relative path of the file for a question id."""
        return f"{self.questions_dir}/{question_id}.md"

    def list_questions(self) -> tuple[list[Question], list[InvalidFile]]:
        """All study-questions directly in the questions dir, plus invalid files.

        Files with different or missing front matter type are skipped silently (other
        notes may live there). Result is sorted by created descending, then id.
        """
        questions_dir = self.root / self.questions_dir
        if not questions_dir.is_dir():
            return [], []
        questions: list[Question] = []
        invalid: list[InvalidFile] = []
        for path in sorted(
            p
            for p in questions_dir.iterdir()
            if p.is_file() and p.suffix == ".md" and self._within_root(p)
        ):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                invalid.append(
                    InvalidFile(path=f"{self.questions_dir}/{path.name}", error="not UTF-8 text")
                )
                continue
            if _is_unrelated_note(text):
                continue
            try:
                questions.append(parse_question(text))
            except FormatError as exc:
                invalid.append(
                    InvalidFile(path=f"{self.questions_dir}/{path.name}", error=str(exc))
                )
        questions.sort(key=lambda q: q.id)
        questions.sort(key=lambda q: q.created, reverse=True)
        return questions, invalid

    def get(self, question_id: str) -> Question:
        """Read one question; raise QuestionNotFound when the file is missing."""
        path = self.root / self.relative_question_path(question_id)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise QuestionNotFound(question_id) from exc
        return parse_question(text)

    def existing_ids(self) -> set[str]:
        """Ids of all readable study-questions in the vault."""
        questions, _ = self.list_questions()
        return {q.id for q in questions}

    def render(self, q: Question) -> tuple[str, str]:
        """Relative path and file content for a question; writing happens elsewhere."""
        return self.relative_question_path(q.id), render_question(q)

    def read_profile(self) -> str | None:
        """Knowledge profile text, or None when missing or blank."""
        path = self.root / self.profile_path
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        return text if text.strip() else None

    def linked_notes(self, q: Question, max_notes: int = 3, max_chars: int = 3000) -> list[Note]:
        """Notes referenced via wikilinks or concept slugs, deduplicated, at most max_notes."""
        by_stem: dict[str, Path] = {}
        by_slug: dict[str, Path] = {}
        for path in self._iter_markdown_files():
            by_stem.setdefault(path.stem.lower(), path)
            by_slug.setdefault(slugify(path.stem), path)
        candidates: list[Path] = []
        for target in self._wikilink_targets(q):
            match = by_stem.get(target.lower())
            if match is not None:
                candidates.append(match)
        for concept in q.concepts:
            match = by_slug.get(concept)
            if match is not None:
                candidates.append(match)
        notes: list[Note] = []
        seen: set[str] = set()
        for path in candidates:
            relative = _relative_posix(self.root, path)
            if relative in seen or not self._within_root(path):
                continue
            try:
                text = _note_text(path)
            except (OSError, UnicodeDecodeError):
                continue
            seen.add(relative)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n[…]"
            notes.append(Note(title=path.stem, path=relative, text=text))
            if len(notes) >= max_notes:
                break
        return notes

    def _wikilink_targets(self, q: Question) -> list[str]:
        targets: list[str] = []
        for text in [q.title, q.preamble, *(s.body for s in q.sections)]:
            for match in WIKILINK.finditer(text):
                target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
                if target:
                    targets.append(target)
        return targets

    def _iter_markdown_files(self) -> list[Path]:
        """All .md files anywhere in the vault, excluding .git, .obsidian, .trash and questions."""
        questions_dir = self.root / self.questions_dir
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            here = Path(dirpath)
            if here == questions_dir:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
            files.extend(here / name for name in sorted(filenames) if name.endswith(".md"))
        return sorted(files)

    def _within_root(self, path: Path) -> bool:
        if not path.is_symlink():
            return True
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return False
        return resolved.is_relative_to(self._resolved_root)


def _is_unrelated_note(text: str) -> bool:
    """True for files that demonstrably are not study-questions (no or other type)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return True
    try:
        close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return False
    try:
        data = yaml.load("\n".join(lines[1:close]), Loader=yaml.BaseLoader)  # noqa: S506
    except yaml.YAMLError:
        return False
    if not isinstance(data, dict):
        return False
    return data.get("type") != "study-question"


def _note_text(path: Path) -> str:
    """Note text with the front matter removed."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        lines = text.split("\n")
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1 :]).lstrip("\n")
    return text


def _relative_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
