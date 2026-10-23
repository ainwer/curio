"""Tiny FastAPI service that converts an uploaded book PDF to Markdown with Docling."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

BOOK_ID_PATTERN = re.compile(r"^b-[a-z0-9-]{1,40}-[0-9a-f]{6}$")
BOOKS_ROOT = Path(os.environ.get("BOOKS_PATH", "/data/books"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_executor = ThreadPoolExecutor(max_workers=1)
_converter: Any = None


class ConvertRequest(BaseModel):
    """Request body of POST /convert."""

    book_id: str


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


def _write_status(book_dir: Path, state: str, error: str | None = None) -> None:
    status_path = book_dir / "status.json"
    tmp_path = book_dir / "status.json.tmp"
    tmp_path.write_text(json.dumps({"state": state, "error": error}), encoding="utf-8")
    tmp_path.replace(status_path)


def _get_converter() -> Any:
    global _converter
    if _converter is None:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options = PdfPipelineOptions()
        options.do_formula_enrichment = True
        options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.AUTO)
        _converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
    return _converter


def _convert_job(book_dir: Path, source_path: Path) -> None:
    try:
        _write_status(book_dir, "converting")
        converter = _get_converter()
        markdown = converter.convert(str(source_path)).document.export_to_markdown()
        tmp_path = book_dir / "book.md.tmp"
        tmp_path.write_text(markdown, encoding="utf-8")
        tmp_path.replace(book_dir / "book.md")
        _write_status(book_dir, "done")
    except Exception as exc:
        _write_status(book_dir, "failed", type(exc).__name__)


@app.post("/convert", status_code=202)
async def convert(payload: ConvertRequest) -> dict[str, str]:
    """Queue the conversion of one book; work happens in a single-thread executor."""
    if BOOK_ID_PATTERN.fullmatch(payload.book_id) is None:
        raise HTTPException(status_code=422, detail="invalid book id")
    book_dir = BOOKS_ROOT / payload.book_id
    source_path = book_dir / "source.pdf"
    if not source_path.is_file():
        raise HTTPException(status_code=404, detail="source.pdf not found")
    _write_status(book_dir, "queued")
    _executor.submit(_convert_job, book_dir, source_path)
    return {"state": "queued"}
