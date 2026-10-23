# Curio

**Question-first learning with a Socratic AI tutor.**
Curio turns curiosity into durable knowledge: pick a question, commit to a hunch,
work it out with a tutor that asks instead of tells, and keep only the two or three
cards worth remembering. Cards live as plain Markdown in your Obsidian vault.

## How it works

1. **Ask.** Generate question cards from a textbook chapter, from a topic, or as
   *bridge questions* that connect a new field to what you already know.
2. **Guess.** Write a gut answer before any help appears. The tutor starts from it.
3. **Work it out.** A streaming tutor responds to your answer, gives hints rather than
   solutions, and cites the book chapters and vault notes it draws on.
4. **Distill.** Curio summarizes the answer and where you went wrong, then proposes
   knowledge cards. Accept, edit or discard until two or three remain.
5. **Keep.** Each question is saved as one Markdown file in your vault and committed
   with Git. Card IDs are stable, ready for export to Anki or an FSRS scheduler.

## Features

- Mobile-first web app, installable as a PWA on iOS and Android
- Textbook import from PDF via [Docling](https://github.com/docling-project/docling),
  with formulas preserved as LaTeX
- Math rendering with KaTeX in questions, chat and cards
- Plain-text storage: your vault stays the source of truth, readable without Curio
- Pluggable LLM providers: Anthropic (default) or any OpenAI-compatible API
- Self-hosted with Docker Compose; private access through Tailscale

## Card format

```markdown
<!-- card id=c-6f1c2a9e-0d4b-4c51-9a37-2a1f0b8e5d10 type=tradeoff source="Example Book, ch. 4" -->
**Q:** What do you trade when raising the switching frequency of a power stage?
**A:** Smaller magnetics and filters versus higher switching losses ($P_{sw} \propto f_{sw}$) and EMI.
```

Card types: `why`, `estimate`, `tradeoff`, `transfer`, `fact`.
See [PLAN.md](PLAN.md) for the full file format and architecture.

## Getting started

To deploy Curio on your own server with Docker, Tailscale and a GPU-backed converter,
see [docs/self-hosting.md](docs/self-hosting.md).

### Run locally

```sh
uv sync
scripts/dev-vault.sh
LLM_PROVIDER=fake DATA_PATH=./.cache/dev-data VAULT_PATH=./.cache/dev-vault \
  uv run uvicorn curio_app.main:create_app --factory --reload
```

## Development

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
pre-commit install   # gitleaks and ruff hooks
```

## License

[MIT](LICENSE)
