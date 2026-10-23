"""HTML page routes."""

from fastapi import APIRouter, Request, Response
from starlette.templating import Jinja2Templates

router = APIRouter(tags=["pages"])


def _render(request: Request, name: str, active_tab: str) -> Response:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={"active_tab": active_tab},
    )


@router.get("/")
async def inbox_page(request: Request) -> Response:
    """Inbox placeholder."""
    return _render(request, "inbox.html", "inbox")


@router.get("/generate")
async def generate_page(request: Request) -> Response:
    """Generate placeholder."""
    return _render(request, "generate.html", "generate")


@router.get("/books")
async def books_page(request: Request) -> Response:
    """Books placeholder."""
    return _render(request, "books.html", "books")
