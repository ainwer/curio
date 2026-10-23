"""FastAPI application factory."""

import logging
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.templating import Jinja2Templates

from curio_app import db
from curio_app.config import Settings, get_settings
from curio_app.llm.base import LLMClient
from curio_app.llm.factory import create_llm_client
from curio_app.logging import configure_logging
from curio_app.vault.git import GitRepo
from curio_app.vault.store import VaultStore
from curio_app.vault.writer import VaultWriter
from curio_app.web.routes import books, generate, health, inbox

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PACKAGE_DIR / "web" / "static"
_TEMPLATES_DIR = _PACKAGE_DIR / "web" / "templates"

_HTML_MIME_TYPE = "text/html"


def _error_page(request: Request, *, status_code: int, message: str) -> Response:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"active_tab": None, "message": message},
        status_code=status_code,
    )


def _wants_html(request: Request) -> bool:
    return _HTML_MIME_TYPE in request.headers.get("accept", "")


def _known_hosts_file(settings: Settings) -> Path | None:
    """Known-hosts file for vault pushes; env override first, packaged default second."""
    if settings.vault_known_hosts_file is not None:
        return settings.vault_known_hosts_file
    default = Path(__file__).resolve().parents[2] / "deploy" / "known_hosts"
    return default if default.is_file() else None


def create_app(settings: Settings | None = None, *, llm: LLMClient | None = None) -> FastAPI:
    """Build the FastAPI app; run migrations and register routes.

    An injected ``llm`` (tests, dev) replaces the provider built from the settings.
    """
    app_settings = settings if settings is not None else get_settings()
    configure_logging(app_settings)
    app_settings.data_path.mkdir(parents=True, exist_ok=True)
    app_settings.books_path.mkdir(parents=True, exist_ok=True)

    conn = db.connect(app_settings.db_path)
    try:
        db.migrate(conn)
    finally:
        conn.close()

    vault_store = VaultStore(
        app_settings.vault_path,
        app_settings.vault_questions_dir,
        app_settings.vault_profile_path,
    )
    vault_writer = VaultWriter(
        GitRepo(
            app_settings.vault_path,
            branch=app_settings.vault_git_branch,
            ssh_key_file=app_settings.vault_ssh_key_file,
            known_hosts_file=_known_hosts_file(app_settings),
            secrets=app_settings.secret_values(),
        ),
        app_settings.vault_path,
        app_settings.db_path,
        app_settings.data_path / "vault.lock",
    )

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, debug=False)
    app.state.settings = app_settings
    app.state.templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    app.state.llm = llm if llm is not None else create_llm_client(app_settings)
    app.state.vault_store = vault_store
    app.state.vault_writer = vault_writer
    app.include_router(health.router)
    app.include_router(inbox.router)
    app.include_router(generate.router)
    app.include_router(books.router)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def web_manifest() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
        logger.error(
            "Unhandled error on %s %s: %s",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        if _wants_html(request):
            return _error_page(
                request,
                status_code=500,
                message="Something went wrong. Please try again.",
            )
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return _error_page(request, status_code=404, message="Not found")
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app
