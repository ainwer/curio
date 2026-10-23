"""Tests for SQLite connect and migrations."""

import sqlite3
from pathlib import Path

import pytest

from lernsystem import db

_TABLES = {
    "books",
    "chapters",
    "generation_runs",
    "suggestions",
    "tutor_sessions",
    "chat_messages",
    "wrapups",
    "card_drafts",
    "vault_drafts",
}


def test_migrate_fresh_db(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "data" / "app.db")
    try:
        assert db.migrate(conn) == 1
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert names.issuperset(_TABLES)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "app.db")
    try:
        assert db.migrate(conn) == 1
        assert db.migrate(conn) == 1
    finally:
        conn.close()


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "app.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_books_state_check_constraint(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "app.db")
    try:
        db.migrate(conn)
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(
                "INSERT INTO books (id, title, filename, state, error, created_at)"
                " VALUES ('b1', 'Title', 'file.pdf', 'bogus', NULL, '2026-01-01T00:00:00Z')"
            )
    finally:
        conn.close()
