"""Fixtures for the books feature tests."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from books.support import FakeConverter
from curio_app.books.converter import get_converter
from curio_app.books.store import BookStore
from curio_app.config import Settings
from curio_app.db import connect, migrate
from curio_app.main import create_app


@pytest.fixture
def db_conn(settings: Settings) -> sqlite3.Connection:
    conn = connect(settings.db_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def store(settings: Settings, db_conn: sqlite3.Connection) -> BookStore:
    return BookStore(db_conn, settings.books_path)


@pytest.fixture
def fake_converter() -> FakeConverter:
    return FakeConverter()


@pytest.fixture
def books_client(settings: Settings, fake_converter: FakeConverter) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_converter] = lambda: fake_converter
    return TestClient(app)
