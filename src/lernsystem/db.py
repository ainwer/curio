"""SQLite connection handling and schema migrations."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request

from lernsystem.config import Settings

MIGRATIONS: list[str] = [
    """\
CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('uploaded','converting','ready','failed')),
  error TEXT, created_at TEXT NOT NULL);
CREATE TABLE chapters (id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  number INTEGER NOT NULL, title TEXT NOT NULL, path TEXT NOT NULL, char_count INTEGER NOT NULL,
  UNIQUE (book_id, number));
CREATE TABLE generation_runs (id INTEGER PRIMARY KEY,
  mode TEXT NOT NULL CHECK (mode IN ('source','topic','bridge')),
  input TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('running','done','failed')),
  error TEXT, created_at TEXT NOT NULL);
CREATE TABLE suggestions (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
  question TEXT NOT NULL, domain TEXT NOT NULL, concepts TEXT NOT NULL /* JSON */, source TEXT,
  hook TEXT NOT NULL, bridge_from TEXT,
  state TEXT NOT NULL CHECK (state IN ('new','accepted','dismissed')), question_id TEXT);
CREATE TABLE tutor_sessions (question_id TEXT PRIMARY KEY, context TEXT NOT NULL /* frozen context block */,
  created_at TEXT NOT NULL);
CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, question_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user','assistant')), content TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('streaming','done','failed')), created_at TEXT NOT NULL);
CREATE TABLE wrapups (question_id TEXT PRIMARY KEY, answer TEXT NOT NULL, misconceptions TEXT NOT NULL /* JSON */,
  created_at TEXT NOT NULL);
CREATE TABLE card_drafts (id TEXT PRIMARY KEY /* c-<uuid> */, question_id TEXT NOT NULL,
  position INTEGER NOT NULL, type TEXT NOT NULL, front TEXT NOT NULL, back TEXT NOT NULL, source TEXT,
  state TEXT NOT NULL CHECK (state IN ('proposed','accepted','discarded')));
CREATE TABLE vault_drafts (question_id TEXT PRIMARY KEY, path TEXT NOT NULL, content TEXT NOT NULL,
  commit_message TEXT NOT NULL, error TEXT NOT NULL, created_at TEXT NOT NULL);
"""
]


def _user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise RuntimeError("PRAGMA user_version returned no row")
    return int(row[0])


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection with WAL, foreign keys and Row rows enabled."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations, each in a transaction; return user_version."""
    current = _user_version(conn)
    for version, script in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    return _user_version(conn)


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency yielding one connection per request."""
    settings: Settings = request.app.state.settings
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()
