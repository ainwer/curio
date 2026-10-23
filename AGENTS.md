# AGENTS.md

Guide for coding agents working on this repository. Read this first; it saves exploration.

## Project

Lernsystem: mobile-first FastAPI + HTMX web app for question-driven learning.
Architecture, data model, vault file format, LLM prompts and the batch plan are in
[PLAN.md](PLAN.md). PLAN.md is authoritative; do not change architecture or prompt
wording on your own.

## Stack

Python 3.12, FastAPI, Jinja2, HTMX 2 (+ SSE extension), SQLite (stdlib `sqlite3`),
`uv` for dependencies, `ruff` (lint + format), `mypy --strict` on `src/`, `pytest`.

## Layout

See PLAN.md §3. Source in `src/lernsystem/`, tests in `tests/`, converter service in
`converter/`, deployment files in `deploy/` and `docker-compose.yml`.

## Commands

```sh
uv sync                              # install dependencies into .venv
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
LLM_PROVIDER=fake DATA_PATH=./.cache/dev-data VAULT_PATH=./.cache/dev-vault \
  uv run uvicorn lernsystem.main:create_app --factory --reload
```

All four checks must pass before every commit.

## Rules

- English only: code, comments, docs, commit messages, UI text.
- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), body lines ≤ 100 chars.
- Never add secrets, real keys, personal data, host names, tailnet names or personal paths.
  Use `.env.example` placeholders only. Test data must be invented (no real book text).
- Do not name constants `*_KEY = "<string>"` or `*_TOKEN = "<string>"` (gitleaks false positives).
- Never log or render settings, environment variables or secret values.
- Use the `fake` LLM provider for all tests; tests must never touch the network.
- Use absolute paths inside the worktree; temporary files go to `.cache/scratch/`.
  Never write outside the worktree, never use `git stash`, never push.
