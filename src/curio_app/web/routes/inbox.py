"""Inbox page with filters, the vault retry and the read-only question page."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from curio_app.domain import Question, Status
from curio_app.vault.store import QuestionNotFound, VaultStore
from curio_app.vault.writer import VaultWriteError, VaultWriter
from curio_app.web.deps import get_templates, get_vault_store, get_vault_writer
from curio_app.web.markdown import render_markdown

logger = logging.getLogger(__name__)

router = APIRouter(tags=["inbox"])

STATUS_FILTERS: tuple[tuple[str, str], ...] = (
    ("open", "Open"),
    ("inbox", "Inbox"),
    ("researching", "Researching"),
    ("done", "Done"),
    ("all", "All"),
)


@router.get("/")
async def inbox_page(
    request: Request,
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    writer: Annotated[VaultWriter, Depends(get_vault_writer)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Inbox: questions with status and domain filter chips as plain links."""
    questions, invalid_files = vault.list_questions()
    drafts = writer.list_drafts()
    domains = sorted({question.domain for question in questions})
    status_filter = request.query_params.get("status", "open")
    if status_filter not in {value for value, _ in STATUS_FILTERS}:
        status_filter = "open"
    domain_filter = request.query_params.get("domain", "")
    if domain_filter and domain_filter not in domains:
        domain_filter = ""
    filtered = filter_questions(questions, status_filter, domain_filter)
    context = {
        "active_tab": "inbox",
        "questions": filtered,
        "invalid_files": invalid_files,
        "drafts": drafts,
        "status_filters": STATUS_FILTERS,
        "status_filter": status_filter,
        "domain_filter": domain_filter,
        "domains": domains,
        "filters_active": status_filter != "open" or bool(domain_filter),
    }
    return templates.TemplateResponse(request=request, name="inbox.html", context=context)


@router.post("/vault/retry")
async def retry_vault_writes(
    request: Request,
    writer: Annotated[VaultWriter, Depends(get_vault_writer)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Retry every queued vault draft; redirect back to the inbox."""
    try:
        await writer.retry_drafts()
    except VaultWriteError as exc:
        logger.warning("Vault retry failed: %s", exc.detail)
        return templates.TemplateResponse(
            request=request,
            name="partials/draft_banner.html",
            context={"drafts": writer.list_drafts()},
        )
    if "hx-request" in request.headers:
        response = Response(status_code=200)
        response.headers["HX-Redirect"] = "/"
        return response
    return RedirectResponse(url="/", status_code=303)


@router.get("/questions/{question_id}")
async def question_page(
    request: Request,
    question_id: str,
    vault: Annotated[VaultStore, Depends(get_vault_store)],
    templates: Annotated[Jinja2Templates, Depends(get_templates)],
) -> Response:
    """Minimal read-only view of one question until the full flow replaces it."""
    try:
        question = vault.get(question_id)
    except QuestionNotFound as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    sections = [
        (section.heading, render_markdown(section.body))
        for section in question.sections
        if section.body.strip()
    ]
    return templates.TemplateResponse(
        request=request,
        name="question.html",
        context={
            "active_tab": "inbox",
            "question": question,
            "created": question.created.date().isoformat(),
            "preamble_html": render_markdown(question.preamble),
            "sections": sections,
        },
    )


def filter_questions(
    questions: list[Question], status_filter: str, domain_filter: str
) -> list[Question]:
    """Apply the status and domain filters; 'open' means inbox plus researching."""
    filtered = questions
    if status_filter == "open":
        filtered = [q for q in filtered if q.status != Status.DONE]
    elif status_filter != "all":
        filtered = [q for q in filtered if q.status.value == status_filter]
    if domain_filter:
        filtered = [q for q in filtered if q.domain == domain_filter]
    return filtered
