# Curio converter

Separate container service that converts one uploaded book PDF into Markdown using Docling
with formula enrichment (LaTeX in `$...$` / `$$...$$`). It reads and writes inside the
shared books volume; the app triggers it over HTTP and polls the status file it produces.

## API

- `POST /convert {"book_id": "..."}`: queues the conversion of
  `BOOKS_PATH/<book_id>/source.pdf` and returns 202. It writes `book.md` and `status.json`
  (`{"state": "queued|converting|done|failed", "error": str|null}`) next to the source file.
  Invalid ids yield 422, a missing source file 404.
- `GET /healthz`: liveness probe.

## Configuration

- `BOOKS_PATH`: root directory holding one subdirectory per book (default `/data/books`).
- `HF_HOME`: Hugging Face model cache inside the image.