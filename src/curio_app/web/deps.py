"""FastAPI dependencies exposing the shared services from ``app.state``."""

import sqlite3
from typing import Annotated

from fastapi import Depends, Request
from starlette.templating import Jinja2Templates

from curio_app.books.store import BookStore
from curio_app.config import Settings
from curio_app.db import get_db
from curio_app.llm.base import LLMClient
from curio_app.services.streams import StreamRegistry
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriter


def get_llm(request: Request) -> LLMClient:
    """LLM client built once in ``create_app`` (or injected for tests)."""
    llm: LLMClient = request.app.state.llm
    return llm


def get_stream_registry(request: Request) -> StreamRegistry:
    """Background assistant-turn streams owned by the app."""
    registry: StreamRegistry = request.app.state.streams
    return registry


def get_vault_store(request: Request) -> VaultStore:
    """Read-only access to the vault checkout."""
    store: VaultStore = request.app.state.vault_store
    return store


def get_vault_writer(request: Request) -> VaultWriter:
    """Writer that applies file changes as commits and pushes them."""
    writer: VaultWriter = request.app.state.vault_writer
    return writer


def get_book_store(
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
    request: Request,
) -> BookStore:
    """Book metadata and chapter access for one request."""
    settings: Settings = request.app.state.settings
    return BookStore(conn, settings.books_path)


def get_templates(request: Request) -> Jinja2Templates:
    """The shared Jinja2 template environment."""
    templates: Jinja2Templates = request.app.state.templates
    return templates
