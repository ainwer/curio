"""Vault write protocol: lock, pull, write, commit, push; drafts on failure."""

import asyncio
import contextlib
import fcntl
import os
import sqlite3
import tempfile
import weakref
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from curio_app.db import connect
from curio_app.vault.git import GitError, GitRepo, VaultDirty, VaultPushRejected

_DIRTY_MESSAGE = (
    "The vault checkout has uncommitted changes. Clean it up on the server, then retry."
)

_WRITE_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


@dataclass(frozen=True)
class FileChange:
    """One file change; *path* is a relative POSIX path inside the vault root."""

    question_id: str
    path: str
    content: str

    def __post_init__(self) -> None:
        ensure_rel_path(self.path)


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one vault write attempt."""

    commit: str | None
    pushed: bool


@dataclass(frozen=True)
class Draft:
    """A queued unsaved vault change waiting for a retry."""

    question_id: str
    path: str
    error: str
    created_at: str


class VaultWriteError(Exception):
    """User-facing vault write failure; *detail* carries the redacted git stderr."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


def ensure_rel_path(path: str) -> None:
    """Reject empty, absolute or escaping vault paths."""
    posix = PurePosixPath(path)
    outside = not path or posix.is_absolute() or ".." in posix.parts or not posix.parts
    if outside:
        raise VaultWriteError(f"Invalid vault file path: {path!r}")


def _write_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _WRITE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _WRITE_LOCKS[loop] = lock
    return lock


@contextmanager
def _process_lock(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive flock on *lock_path* for the calling thread."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _run_with_db[T](db_path: Path, action: Callable[[sqlite3.Connection], T]) -> T:
    conn = connect(db_path)
    try:
        return action(conn)
    finally:
        conn.close()


def _read_draft_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT question_id, path, content, error, created_at"
        " FROM vault_drafts ORDER BY created_at, question_id"
    ).fetchall()


def _store_drafts(
    conn: sqlite3.Connection, changes: Sequence[FileChange], message: str, error: str
) -> None:
    created_at = datetime.now(UTC).isoformat()
    conn.executemany(
        "INSERT INTO vault_drafts (question_id, path, content, commit_message, error, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(question_id) DO UPDATE SET"
        " path = excluded.path, content = excluded.content,"
        " commit_message = excluded.commit_message, error = excluded.error,"
        " created_at = excluded.created_at",
        [
            (change.question_id, change.path, change.content, message, error, created_at)
            for change in changes
        ],
    )
    conn.commit()


def _delete_drafts(conn: sqlite3.Connection, question_ids: Iterable[str]) -> None:
    conn.executemany(
        "DELETE FROM vault_drafts WHERE question_id = ?",
        [(question_id,) for question_id in question_ids],
    )
    conn.commit()


def _write_atomic(target: Path, content: str) -> None:
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(text)
        os.replace(temp, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


class VaultWriter:
    """Applies file changes to the vault checkout one commit at a time."""

    def __init__(self, repo: GitRepo, root: Path, db_path: Path, lock_path: Path) -> None:
        self._repo = repo
        self._root = root
        self._db_path = db_path
        self._lock_path = lock_path

    async def write(self, changes: Sequence[FileChange], message: str) -> WriteResult:
        """Apply *changes* as one commit; on any Git failure store drafts and raise."""
        if not changes:
            return WriteResult(commit=None, pushed=False)
        async with _write_lock():
            return await asyncio.to_thread(self._write_locked, list(changes), message)

    async def retry_drafts(self) -> WriteResult:
        """Write every queued draft row in one commit; rows are deleted on success."""
        rows = _run_with_db(self._db_path, _read_draft_rows)
        changes = [
            FileChange(question_id=row["question_id"], path=row["path"], content=row["content"])
            for row in rows
        ]
        message = f"curio: retry saving {len(changes)} file(s)"
        return await self.write(changes, message)

    def list_drafts(self) -> list[Draft]:
        """Return all queued draft rows, oldest first."""
        rows = _run_with_db(self._db_path, _read_draft_rows)
        return [
            Draft(
                question_id=row["question_id"],
                path=row["path"],
                error=row["error"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _write_locked(self, changes: list[FileChange], message: str) -> WriteResult:
        with _process_lock(self._lock_path):
            return self._apply(changes, message)

    def _apply(self, changes: list[FileChange], message: str) -> WriteResult:
        for change in changes:
            ensure_rel_path(change.path)
        prepared = [(self._root / Path(change.path), change.content) for change in changes]
        base: str | None = None
        try:
            if not self._repo.is_clean():
                raise VaultDirty(_DIRTY_MESSAGE)
            self._repo.pull_rebase()
            base = self._repo.head()
            for target, content in prepared:
                _write_atomic(target, content)
            commit = self._commit_and_push([change.path for change in changes], message)
            self._delete_draft_rows({change.question_id for change in changes})
            return WriteResult(commit=commit, pushed=commit is not None)
        except (GitError, OSError) as error:
            if base is not None:
                with contextlib.suppress(GitError):
                    self._repo.reset_hard(base)
            if isinstance(error, GitError):
                user_message, detail = error.message, error.detail
            else:
                user_message = "Could not write the file to the vault."
                detail = type(error).__name__
            self._store_draft_rows(changes, message, user_message)
            raise VaultWriteError(user_message, detail) from error

    def _commit_and_push(self, paths: list[str], message: str) -> str | None:
        commit = self._repo.commit_paths(paths, message)
        if commit is None:
            return None
        try:
            self._repo.push()
        except VaultPushRejected:
            self._repo.pull_rebase()
            self._repo.push()
        return self._repo.head()

    def _store_draft_rows(self, changes: list[FileChange], message: str, error: str) -> None:
        def action(conn: sqlite3.Connection) -> None:
            _store_drafts(conn, changes, message, error)

        _run_with_db(self._db_path, action)

    def _delete_draft_rows(self, question_ids: Iterable[str]) -> None:
        ids = sorted(set(question_ids))

        def action(conn: sqlite3.Connection) -> None:
            _delete_drafts(conn, ids)

        _run_with_db(self._db_path, action)
