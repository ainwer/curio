# Curio MVP: Plan

Question-driven learning web app: draw a question card, write a gut answer, research it with a
Socratic AI tutor, then distill 2–3 curated knowledge cards into an Obsidian vault (Git).
Spaced repetition is out of scope; the card format is designed for a later Anki / ts-fsrs exporter.

Status: draft for review. All code, docs, UI and generated content are English.

## 1. Decisions

| Topic | Decision | Why |
|---|---|---|
| Stack | Python 3.12, FastAPI, Jinja2, HTMX 2 (+ SSE extension), SQLite (stdlib `sqlite3`) | Small surface, server-rendered, easy for cheap coder models; no JS build step |
| Tooling | `uv` (lockfile), `ruff` (lint + format), `mypy --strict` on `src/`, `pytest` | One fast toolchain, reproducible |
| Source of truth | **Vault** for questions and cards; **SQLite** only for app state (suggestions, chats, wrap-up drafts, books, failed writes) | Questions created or edited in Obsidian show up in the app; no sync logic |
| Vault checkout | A **dedicated clone** of the vault repo, used only by the app | `git pull --rebase` needs a clean tree; no autostash, no surprises |
| Git | `git` CLI via `subprocess`, author `curio <curio@localhost>`, SSH deploy key mounted read-only, pinned `known_hosts` | Simple, testable against local bare repos |
| PDF conversion | Separate `converter` container: Docling with formula enrichment (LaTeX), NVIDIA GPU | Keeps the heavy PyTorch image out of the app; app stays small and testable |
| Chapter split + search | In the app (pure Python): split Markdown on headings; BM25 over sections | Testable without Docling; no vector store in the MVP |
| LLM | Provider interface with `anthropic` (default, `claude-sonnet-5-5`), `openai_compatible`, `fake` (dev/tests) | Configurable via env; workers and CI never need a key |
| Structured output | Anthropic: `messages.parse(output_format=<Pydantic model>)`; other providers: JSON in text → own tolerant parser; both validated by the same Pydantic models | One validation path, parser is unit-tested |
| Streaming | Tutor uses `messages.stream`; browser gets Server-Sent Events via `htmx-ext-sse` | Native token streaming, no WebSocket state |
| Math | KaTeX (vendored) auto-render for `$...$` / `$$...$$`; Markdown rendered server-side (`markdown-it-py`) | Formulas in cards, chat and book excerpts |
| Access | Tailscale sidecar, `tailscale serve` HTTPS; app shares the sidecar's network namespace; no published ports; no login | Tailnet is the access boundary |
| PWA | Manifest, icons, `apple-mobile-web-app-*` meta, minimal service worker (static assets only) | Home-screen app on iPhone |

## 2. Architecture

```
iPhone / Mac ──tailnet HTTPS──▶ tailscale (serve :443 → 127.0.0.1:8000)
                                   │ network namespace shared
                                   ▼
                                  app (FastAPI, uvicorn :8000)
                    ┌──────────────┼───────────────┬──────────────────┐
                    ▼              ▼               ▼                  ▼
            /vault (dedicated   /data/app.db   /data/books/<id>/   LLM API (HTTPS)
            vault clone, git)   (SQLite)        source.pdf, book.md, chapters/
                    │                               ▲
              git push (deploy key)                 │ shared data volume
                    ▼                               │
             GitHub (private vault repo)     converter (Docling, GPU, :8001, internal only)
```

- `app` → `converter`: `POST http://converter:8001/convert {"book_id": "..."}` (fire and forget).
  The converter reads `/data/books/<id>/source.pdf`, writes `book.md` and `status.json`
  (`{"state": "converting|done|failed", "error": null, "pages": 0, "progress": 0.0}`).
  The app polls `status.json` (HTMX polling every 5 s while converting), then splits chapters itself.
- Books live only in the data volume. The data volume path is outside the app repo
  (`DATA_PATH`, e.g. `/opt/curio-data`) and `data/` is git-ignored as a second guard.

### Request flows

1. **Generate** → `generation_runs` + `suggestions` rows → user accepts → question file written
   to the vault (status `inbox`) → commit + push. Accepting several at once = one commit.
2. **Open question** → gut answer form (required, 1–1000 chars) → file updated
   (`## Gut answer`, status `researching`) → commit + push → tutor session created
   (context snapshot) → first tutor turn streams immediately.
3. **Chat** → user message stored → SSE stream of the assistant turn → stored on completion.
4. **Wrap up** → structured LLM call → wrap-up draft (summary, misconceptions, 4–6 card drafts) in SQLite.
5. **Curate** → per card: accept / edit / discard (edits keep the card id).
6. **Save** → file updated (`## Insight`, `## Cards`, status `done`) → commit + push.
   On any Git failure the rendered file is kept in `vault_drafts`, the UI shows the error and a
   "Retry" button; nothing is lost and nothing is auto-resolved.

### Git write protocol (`vault/git.py`, `vault/writer.py`)

```
with vault_lock:                      # asyncio.Lock + fcntl lock file, one writer at a time
    git pull --rebase                 # on failure: git rebase --abort → raise VaultConflict
    write file(s)
    git add -- <paths>
    git commit --author="curio <curio@localhost>" -m "<msg>" -- <paths>
    git push                          # rejected (non-fast-forward): one more pull --rebase + push,
                                      # then raise VaultPushRejected
```
- `GIT_SSH_COMMAND="ssh -i /run/secrets/vault_deploy_key -o IdentitiesOnly=yes -o UserKnownHostsFile=/app/deploy/known_hosts -o StrictHostKeyChecking=yes"`.
- Also set `user.name`/`user.email` via `-c` so the committer is `curio` too.
- Dirty tree before pull (someone edited the clone) → `VaultDirty`, reported, no write.
- Commit messages: `curio: add question <id>`, `curio: gut answer <id>`, `curio: cards <id>`.
- Errors shown in the UI contain the Git stderr **after** redaction (see Security).

## 3. Repository layout

```
curio/
├── AGENTS.md                  # architecture map + commands for coder agents
├── PLAN.md  README.md  SECURITY.md  CHANGELOG.md  LICENSE (MIT)
├── .env.example  .gitignore  .gitleaks.toml  .pre-commit-config.yaml  .editorconfig
├── pyproject.toml  uv.lock
├── Dockerfile                 # app image (python:3.12-slim + git + openssh-client)
├── docker-compose.yml         # tailscale + app + converter
├── deploy/
│   ├── tailscale/serve.json   # HTTPS 443 → http://127.0.0.1:8000
│   └── known_hosts            # github.com public host keys (public data)
├── converter/                 # separate image: Docling + tiny FastAPI
│   ├── Dockerfile  pyproject.toml  app.py
├── src/curio/
│   ├── main.py                # create_app(): routers, static, exception handlers
│   ├── config.py              # pydantic-settings Settings (SecretStr for all secrets)
│   ├── logging.py             # redaction filter, JSON-free plain logging
│   ├── db.py                  # connection, migrations (PRAGMA user_version)
│   ├── domain.py              # Pydantic models: Question, Card, CardType, Status, ...
│   ├── vault/
│   │   ├── format.py          # render_question(q) -> str, parse_question(text) -> Question
│   │   ├── ids.py             # question_id(date, text), card_id()
│   │   ├── store.py           # list/read/write question files, knowledge profile, linked notes
│   │   ├── git.py             # GitRepo: pull_rebase, commit_paths, push, is_clean
│   │   └── writer.py          # VaultWriter: lock + pull + write + commit + push + drafts
│   ├── books/
│   │   ├── store.py           # book dirs, status.json, chapters
│   │   ├── chapters.py        # split_chapters(md) -> list[Chapter]
│   │   ├── search.py          # BM25 over sections
│   │   └── converter.py       # ConverterClient
│   ├── llm/
│   │   ├── base.py            # LLMClient protocol
│   │   ├── anthropic.py  openai_compat.py  fake.py
│   │   ├── schemas.py         # QuestionBatch, Wrapup, CardDraft (output models)
│   │   ├── parsing.py         # extract_json(text), parse_output(text, model)
│   │   ├── prompts.py         # build_* functions (render templates, assemble messages)
│   │   └── prompts/           # *.md.j2 prompt templates (written by the architect only)
│   ├── services/              # generation.py tutor.py wrapup.py curation.py
│   └── web/
│       ├── routes/            # home, generate, inbox, questions, chat, books, health
│       ├── templates/         # base.html + pages + partials (HTMX fragments)
│       └── static/            # app.css, htmx.min.js, sse.js, katex/, manifest.webmanifest, sw.js, icons/
└── tests/                     # unit + route tests; fixtures are invented data only
```

## 4. Vault file format

Paths are configurable (defaults shown): `VAULT_QUESTIONS_DIR=Learning/Questions`,
`VAULT_PROFILE_PATH=Learning/Knowledge-Profile.md`. One file per question: `<id>.md`.

```markdown
---
type: study-question
id: q-2026-10-23-why-do-sic-mosfets-switch-faster
status: inbox
domain: power-electronics
concepts: [switching-losses, sic]
source: "Example Book, ch. 4"
created: 2026-10-23T08:15:00+00:00
---
# Why do SiC MOSFETs allow higher switching frequencies than Si IGBTs?

## Gut answer
Less tail current, so lower turn-off losses.

## Insight
**Answer:** ...
**Where I was wrong:** ...

## Cards

<!-- card id=c-6f1c2a9e-0d4b-4c51-9a37-2a1f0b8e5d10 type=tradeoff source="Example Book, ch. 4" -->
**Q:** What do you trade when raising the switching frequency of a SiC stage?
**A:** Smaller magnetics and filters versus switching losses $P_{sw} \propto f_{sw}$ and EMI.
```

Rules (enforced by `format.py`, covered by tests):
- `status ∈ {inbox, researching, done}`; `type` is always `study-question`; `source` optional; `concepts` kebab-case.
- `## Gut answer`, `## Insight`, `## Cards` are optional until filled; unknown sections and text
  the user adds in Obsidian are preserved verbatim and in order.
- A card = marker comment + `**Q:**` paragraph + `**A:**` paragraph (multi-line allowed) up to the
  next marker or heading. Marker attributes: `id` (required, `c-<uuid4>`), `type` (required:
  `why|estimate|tradeoff|transfer|fact`), `source` (optional, quoted).
- Card ids never change once written; edits keep the id. The exporter later keys on `id`.
- No learning state (due dates, ease, reviews) is ever written to the vault.
- `parse(render(q)) == q` and `render(parse(text)) == text` for app-written files.
- Question id: `q-<YYYY-MM-DD>-<slug>`; slug = ASCII kebab-case of the first ≤ 6 significant words,
  ≤ 50 chars; collision → `-2`, `-3`.

## 5. Data model (SQLite, `db.py`)

```sql
CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('uploaded','converting','ready','failed')),
  error TEXT, created_at TEXT NOT NULL);
CREATE TABLE chapters (id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  number INTEGER NOT NULL, title TEXT NOT NULL, path TEXT NOT NULL, char_count INTEGER NOT NULL,
  UNIQUE (book_id, number));
CREATE TABLE generation_runs (id INTEGER PRIMARY KEY,
  mode TEXT NOT NULL CHECK (mode IN ('source','topic','bridge')),
  input TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('running','done','failed')),
  error TEXT, created_at TEXT NOT NULL);
CREATE TABLE suggestions (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
  question TEXT NOT NULL, domain TEXT NOT NULL, concepts TEXT NOT NULL /* JSON */, source TEXT,
  hook TEXT NOT NULL, bridge_from TEXT,
  state TEXT NOT NULL CHECK (state IN ('new','accepted','dismissed')), question_id TEXT);
CREATE TABLE tutor_sessions (question_id TEXT PRIMARY KEY, context TEXT NOT NULL /* frozen context block */,
  created_at TEXT NOT NULL);
CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, question_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user','assistant')), content TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('streaming','done','failed')), created_at TEXT NOT NULL);
CREATE TABLE wrapups (question_id TEXT PRIMARY KEY, answer TEXT NOT NULL, misconceptions TEXT NOT NULL /* JSON */,
  created_at TEXT NOT NULL);
CREATE TABLE card_drafts (id TEXT PRIMARY KEY /* c-<uuid> */, question_id TEXT NOT NULL,
  position INTEGER NOT NULL, type TEXT NOT NULL, front TEXT NOT NULL, back TEXT NOT NULL, source TEXT,
  state TEXT NOT NULL CHECK (state IN ('proposed','accepted','discarded')));
CREATE TABLE vault_drafts (question_id TEXT PRIMARY KEY, path TEXT NOT NULL, content TEXT NOT NULL,
  commit_message TEXT NOT NULL, error TEXT NOT NULL, created_at TEXT NOT NULL);
```
Migrations: ordered list of SQL scripts; `PRAGMA user_version` = applied count; WAL mode; foreign keys on.

## 6. Configuration (`.env.example`, all placeholders)

```
# Tailscale (one-time auth key; remove after first start)
TS_AUTHKEY=
TS_HOSTNAME=curio
# Host paths
VAULT_PATH=/path/to/dedicated/vault-clone
DATA_PATH=/path/to/curio-data
VAULT_DEPLOY_KEY_PATH=/path/to/vault_deploy_key
# Vault layout
VAULT_QUESTIONS_DIR=Learning/Questions
VAULT_PROFILE_PATH=Learning/Knowledge-Profile.md
VAULT_GIT_BRANCH=main
# LLM
LLM_PROVIDER=anthropic              # anthropic | openai_compatible | fake
LLM_MODEL=claude-sonnet-5-5
LLM_TUTOR_MODEL=                    # optional override for the tutor
LLM_EFFORT_GENERATION=medium        # anthropic only
LLM_EFFORT_TUTOR=low                # anthropic only
LLM_FALLBACK=default                # anthropic server-side refusal fallback: default | off
ANTHROPIC_API_KEY=
LLM_BASE_URL=                       # openai_compatible only
LLM_API_KEY=                        # openai_compatible only
# App
CONVERTER_URL=http://converter:8001
MAX_UPLOAD_MB=200
LOG_LEVEL=INFO
```

## 7. LLM integration

### Interface (`llm/base.py`)

```python
class LLMClient(Protocol):
    async def generate(self, *, system: str, messages: list[Message], schema: type[T],
                       purpose: Literal["generation", "wrapup"]) -> T: ...
    def stream(self, *, system: str, messages: list[Message]) -> AsyncIterator[str]: ...

class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str
```
- Anthropic: `AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())`;
  `generate` → `client.messages.parse(model, max_tokens=16000, system=[{text, cache_control: ephemeral}], messages, output_format=schema, output_config={"effort": ...})`;
  `stream` → `client.messages.stream(..., max_tokens=8000)` + `text_stream`.
  Adaptive thinking is the model default (omit `thinking`). Check `stop_reason == "refusal"` → `LLMRefused`.
  With `LLM_FALLBACK=default`: beta client, `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`.
- Large stable inputs (chapter text, tutor context block) carry a `cache_control` breakpoint.
- OpenAI-compatible: `openai` SDK, `response_format={"type": "json_object"}`, then `parsing.parse_output`.
- Fake: deterministic canned outputs built from the input (used in tests, dev and screenshots).
- Errors map to `LLMUnavailable | LLMRefused | LLMOutputInvalid`; one retry on invalid output with
  the validation error appended as a user message.

### Output schemas (`llm/schemas.py`)

```python
class CardType(StrEnum): WHY="why"; ESTIMATE="estimate"; TRADEOFF="tradeoff"; TRANSFER="transfer"; FACT="fact"

class GeneratedQuestion(BaseModel):
    question: str            # one sentence, ends with "?"
    domain: str              # kebab-case
    concepts: list[str]      # 1–4 kebab-case
    source: str | None       # "<Book>, ch. <n>" when from a source
    hook: str                # one sentence: why this is worth answering
    bridge_from: str | None  # bridge mode: the known area it connects to

class QuestionBatch(BaseModel):
    questions: list[GeneratedQuestion]   # validated: 8–10 (topic/source), 5–8 (bridge)

class CardDraft(BaseModel):
    type: CardType; front: str; back: str; source: str | None

class Wrapup(BaseModel):
    answer: str                  # ≤ 5 sentences
    misconceptions: list[str]    # what the gut answer got wrong or missed; may be empty
    cards: list[CardDraft]       # validated: 4–6
```

`parsing.py`: strip code fences, take the first balanced top-level JSON object, `model_validate`,
then post-validate (counts, `?` suffix, kebab-case normalisation, dedupe questions). Tests cover
fenced JSON, prose around JSON, trailing commas rejected, wrong counts, missing fields.

### Prompts (`llm/prompts/*.md.j2`, verbatim; coders must not change wording)

**`_card_rules.md.j2`** (included where cards or questions are produced)
```
Card rules:
- One idea per card. The answer is at most 3 sentences, or one formula plus a one-sentence explanation.
- Preferred types: why (mechanism), estimate (order of magnitude, back-of-envelope), tradeoff
  (what you gain vs. what you pay), transfer (apply the idea to a different system).
  Use fact sparingly, only for numbers or definitions worth memorising.
- No trivia. No yes/no questions. No questions answerable by recalling a single word.
- Write formulas in LaTeX: inline $...$, display $$...$$.
- Give the source as "<Book>, ch. <n>" when the content comes from a provided excerpt; otherwise null.
  Never invent a source.
```

**`question_generator.system.md.j2`**
```
You write question cards for a self-directed learner who learns by asking questions first:
they pick a question, write a gut answer, then research it with a tutor.

Good question cards:
- make the learner curious: they point at a surprising effect, a design choice, or a number that
  needs explaining;
- are concrete and answerable in 15–30 minutes of focused research;
- refer to real systems and hardware the learner meets in practice (see the knowledge profile),
  not to textbook abstractions;
- cannot be answered with yes/no or a single word;
- each target one core concept; together they cover different concepts, not variations of one.

Prefer "why", "how much / estimate", "what is the tradeoff" and "what changes if" questions.

{% include "_card_rules.md.j2" %}

Field rules:
- domain: kebab-case, broad field (e.g. power-electronics).
- concepts: 1–4 kebab-case concept names, specific (e.g. reverse-recovery, not physics).
- hook: one sentence telling the learner why the answer matters in practice.
- Write in English. Return only the JSON object requested.

<knowledge_profile>
{{ profile or "No profile provided." }}
</knowledge_profile>
```

**`question_generator.source.md.j2`** (user message; the chapter block gets `cache_control`)
```
<chapter book="{{ book_title }}" number="{{ chapter_number }}" title="{{ chapter_title }}">
{{ chapter_text }}
</chapter>

Write 8–10 question cards grounded in this chapter. Each must be answerable with the chapter's
content plus reasoning, and set source to "{{ book_title }}, ch. {{ chapter_number }}".
Skip the chapter's trivial definitions; aim at mechanisms, magnitudes and design tradeoffs.
{% if existing %}Avoid duplicating these existing questions:
{% for q in existing %}- {{ q }}
{% endfor %}{% endif %}
```

**`question_generator.topic.md.j2`**
```
Topic: {{ topic }}

Write 8–10 question cards about this topic, ordered from foundational to advanced.
Set source to null. {% if existing %}Avoid duplicating these existing questions:
{% for q in existing %}- {{ q }}
{% endfor %}{% endif %}
```

**`question_generator.bridge.md.j2`**
```
{% if focus %}Focus on this new area: {{ focus }}{% else %}Pick new areas adjacent to the profile.{% endif %}

Write 5–8 bridge questions. A bridge question connects something the learner does NOT know yet
to an area the knowledge profile says they know well, so the known area becomes the entry point
(e.g. "In <known system>, why does <new concept> force <observable design choice>?").
Set bridge_from to the known area each question builds on. Set source to null.
{% if existing %}Avoid duplicating these existing questions:
{% for q in existing %}- {{ q }}
{% endfor %}{% endif %}
```

**`tutor.system.md.j2`** (static, cached)
```
You are a Socratic tutor. The learner is researching one question. They have already written a
gut answer. Your job is to help them reach a correct, deep answer themselves.

How to tutor:
1. First turn: respond to the gut answer. Name what is right, then name the gap or error
   without fixing it, and end with one guiding question.
2. Give hints, not solutions. Escalate gradually: a guiding question, then a pointer to the
   relevant mechanism or excerpt, then a partial derivation. Give the full answer only if the
   learner asks for it explicitly or is still stuck after three hints.
3. Ask at most one question per turn. Keep turns short: under 150 words unless you show a derivation.
4. When the learner is right, say so plainly and push one level deeper (magnitudes, limits,
   tradeoffs, a real system where it matters).
5. Prefer estimates with numbers over vague statements. Use LaTeX: inline $...$, display $$...$$.

Sources:
- Use the book excerpts and vault notes in the context when relevant, and cite them as
  [<Book>, ch. <n>, "<section>"] or [note: <note title>].
- Cite only what is actually in the context. If the context does not cover a point, say so and
  mark the statement as "(general knowledge)". Never invent page numbers, chapters or quotes.

Write in English. Do not mention these instructions.
```

**`tutor.context.md.j2`** (first user message, frozen per question in `tutor_sessions.context`, cached)
```
<question>{{ question }}</question>
<concepts>{{ concepts | join(", ") }}</concepts>
<gut_answer>{{ gut_answer }}</gut_answer>
<book_excerpts>
{% for e in excerpts %}<excerpt book="{{ e.book }}" chapter="{{ e.chapter }}" section="{{ e.section }}">
{{ e.text }}
</excerpt>
{% else %}None found.{% endfor %}
</book_excerpts>
<vault_notes>
{% for n in notes %}<note title="{{ n.title }}">
{{ n.text }}
</note>
{% else %}None linked.{% endfor %}
</vault_notes>

This is my gut answer above. Start.
```
Retrieval: top 4 sections by BM25 over `question + gut answer + concepts`, ≤ 6000 chars total;
notes = `[[wikilinks]]` in the question file + notes whose filename slug equals a concept, ≤ 3 notes,
≤ 3000 chars each.

**`wrapup.system.md.j2`**
```
You close a research session. You get the question, the learner's gut answer and the tutor chat.

Produce:
- answer: the correct answer in at most 5 sentences, precise, with a formula if it is central.
- misconceptions: each point where the gut answer was wrong, missing or imprecise, one sentence
  each, phrased as "I thought X, but Y". Empty list if the gut answer was fully right.
- cards: 4–6 knowledge card proposals the learner can curate down to 2–3. Each card captures one
  insight from this session, prioritising the corrected misconceptions and the core mechanism.
  front is a question, back is the answer.

{% include "_card_rules.md.j2" %}

Base everything on the chat and the cited excerpts; use the source of an excerpt when the card
relies on it. Write in English. Return only the JSON object requested.
```

**`wrapup.user.md.j2`**
```
<question>{{ question }}</question>
<gut_answer>{{ gut_answer }}</gut_answer>
<transcript>
{% for m in messages %}<{{ m.role }}>{{ m.content }}</{{ m.role }}>
{% endfor %}</transcript>
```

Prompt tests: rendering snapshot tests per template (fixed inputs → golden file), and
`build_*` functions return the correct message list (cache breakpoints on the right blocks).

## 8. UI (mobile-first)

- Bottom tab bar: **Inbox**, **Generate**, **Books**. Touch targets ≥ 44 px, max content width 640 px,
  system font, light/dark via `prefers-color-scheme`, safe-area insets.
- Inbox: cards list (question, domain chip, status chip); filter chips for status and domain (query
  params, HTMX swap); a "Retry save" banner when `vault_drafts` is non-empty.
- Question page by status: `inbox` → gut answer form only; `researching` → chat (messages, composer
  pinned to bottom, "Wrap up" button); after wrap-up → curation (card editors with
  Accept / Edit / Discard, counter "2–3 recommended", "Save to vault"); `done` → read-only view.
- Generate: segmented control Source / Topic / Bridge; suggestions list with "Add to inbox" per item
  and "Add selected".
- Books: upload (PDF), conversion status, chapter list (chapter → "Generate questions").
- Errors are shown as inline alerts with a generic message plus a short reason; never stack traces.

## 9. Security and public-repo hygiene

- Secrets only at runtime from `.env` (`env_file`); `SecretStr` fields; `Settings.__repr__` and
  logs never print them. A logging filter replaces any configured secret value with `***`.
- Global exception handler: generic message to the client, exception type + redacted message in
  logs; no tracebacks in responses; `debug=False` always.
- Git stderr passes through the same redaction before it reaches UI or logs.
- `.gitignore` (first commit): `.env`, `.env.*` except `.env.example`, `data/`, `*.db`, `*.sqlite*`,
  `books/`, `*.pdf`, `logs/`, `*.log`, `tailscale/`, `.cache/`, `.venv/`, `__pycache__/`, `vault/`.
- gitleaks: pre-commit hook (`.pre-commit-config.yaml`) + GitHub Action on every push and PR
  (full history checkout). README: enable GitHub Secret Scanning + Push Protection.
- Tests and fixtures use invented content only; no real book text.
- Coder agents get `.env.example` only. Every diff is reviewed for secrets and private data before commit.

## 10. Feature batches

Each batch = GitHub issue + branch + PR; the user merges. Checks before every PR:
`uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest` + gitleaks.
Prompt templates are written by the architect and handed to the coder verbatim.

| # | Batch | Agent | Depends on |
|---|---|---|---|
| B0 | Repo bootstrap (architect) | — | — |
| B1 | App skeleton | build | B0 |
| B2 | Vault file format | build | B1 |
| B3 | Git logic + VaultWriter | build | B1 (parallel to B2) |
| B4 | LLM layer + prompts | build | B1 (parallel to B2/B3) |
| B5 | Books: upload, converter, chapters, search | build | B1 |
| B6 | Question generation + inbox UI | ui | B2, B3, B4, B5 |
| B7 | Gut answer + tutor chat | build (+ ui pass) | B6 |
| B8 | Wrap-up, curation, save | build (+ ui pass) | B7 |
| B9 | Deployment: compose, Tailscale, README | quick + docs | B5 |

**B0 Repo bootstrap** (architect): `.gitignore`, `LICENSE`, `.env.example`, `.gitleaks.toml`,
`.pre-commit-config.yaml` (gitleaks, ruff), CI workflow (gitleaks job), `README.md` stub,
`AGENTS.md`, `PLAN.md`, `SECURITY.md`. Public repo `ainwer/curio`.
- AC: first commit contains `.gitignore`; gitleaks passes on full history; Actions green.

**B1 App skeleton**: `pyproject.toml` + `uv.lock`, `config.py`, `logging.py`, `db.py` with the full
schema, `main.py`, `base.html` with tab bar, `app.css`, vendored `htmx.min.js` + `sse.js` + KaTeX,
manifest + icons + `sw.js`, `/healthz`, `Dockerfile` (non-root user, `git`, `openssh-client`), CI job
(ruff, mypy, pytest).
- AC: `uv run uvicorn curio.main:create_app --factory` serves an empty Inbox page at 390 px width;
  `/healthz` → `{"status":"ok"}`; Lighthouse-style PWA basics present (manifest, icons, theme color).
- Tests: settings load from env; `repr(settings)` and log output never contain secret values;
  migrations idempotent and set `user_version`; health route; exception handler hides details.

**B2 Vault file format**: `domain.py`, `vault/format.py`, `vault/ids.py`, `vault/store.py`
(list/read/write questions, read profile, linked notes).
- AC: format exactly as in §4; unknown sections preserved; invalid files reported, not crashing the inbox.
- Tests: round trip both directions; card parsing (multi-line answers, LaTeX, missing source);
  invalid status/type → error; id/slug generation incl. collisions and non-ASCII; profile missing;
  wikilink + concept-based note lookup.

**B3 Git logic + VaultWriter**: `vault/git.py`, `vault/writer.py`, `vault_drafts` handling.
- AC: protocol in §2; custom author/committer; only the given paths are committed; conflicts,
  dirty tree and push rejection raise typed errors and leave the repo clean (rebase aborted).
- Tests (temp dirs: bare remote + two clones): happy path; remote ahead (fast rebase); conflicting
  edit → `VaultConflict` + draft stored + clean repo; dirty tree → `VaultDirty`; push rejected then
  succeeds after re-pull; author/committer check; redaction of stderr; retry from draft.

**B4 LLM layer + prompts**: `llm/*` as in §7, templates verbatim.
- AC: provider selected by env; fake provider usable without keys; anthropic adapter builds the
  documented request (verified with a mocked SDK client, no network).
- Tests: parsing (cases in §7); schema post-validation; template golden files; message assembly
  incl. cache breakpoints; anthropic request kwargs; refusal → `LLMRefused`; retry on invalid output.

**B5 Books**: upload route (streamed to disk, size limit, PDF magic check), `books/*`, converter
service (`converter/`: FastAPI, Docling `PdfPipelineOptions(do_formula_enrichment=True)`,
model cache volume, GPU), status polling, chapter split, BM25 search.
- AC: upload → `converting` → `ready` with chapter list (verified with a fake converter that writes
  `book.md`); books never written outside `DATA_PATH`.
- Tests: `split_chapters` on invented Markdown (H1/H2 numbering, front matter, no headings →
  single chapter); search ranking; upload validation; converter client with mocked HTTP.

**B6 Question generation + inbox UI**: `services/generation.py`, Generate page (3 modes), suggestions,
accept → `VaultWriter`, Inbox with filters.
- AC: with the fake LLM: each mode yields suggestions; accepting creates a valid vault file (status
  `inbox`) and one commit; inbox filters by status and domain; usable at 390 px (screenshot).
- Tests: route tests with fake LLM + temp vault repo; duplicate accept is idempotent.

**B7 Gut answer + tutor chat**: gut answer form, status change, `tutor_sessions` context
snapshot (retrieval as in §7), SSE streaming endpoint, chat persistence, KaTeX render.
- AC: tutor cannot start without a gut answer; first tutor turn streams right after submit; reload
  shows history; interrupted stream marks message `failed` with a retry button.
- Tests: gut answer required (server-side); context assembly (excerpts, notes, limits); SSE stream
  with fake LLM; message states.

**B8 Wrap-up, curation, save**: wrap-up call, `card_drafts`, curation UI, save to vault
(`## Insight`, `## Cards`, status `done`), conflict banner + retry.
- AC: 4–6 proposals; accept/edit/discard persist; save writes only accepted cards with stable ids;
  re-save after edit keeps ids; Git failure shows the banner and keeps the draft.
- Tests: wrap-up with fake LLM; curation state transitions; saved file parses back with the
  accepted cards; conflict path.

**B9 Deployment**: `docker-compose.yml` (tailscale sidecar with `TS_SERVE_CONFIG`, app with
`network_mode: service:tailscale`, converter with NVIDIA GPU reservation, volumes), `serve.json`,
`known_hosts`, `README.md` (prerequisites incl. NVIDIA Container Toolkit, deploy key, dedicated vault
clone, `.env`, start, update, remove auth key, Secret Scanning setup), `SECURITY.md`.
- AC: `docker compose config` valid with `.env.example` values; no secret values in compose,
  Dockerfiles, docs; README steps complete.

## 11. Out of scope (MVP)

Spaced repetition, Anki sync, coverage map, vector search, multi-user, native iOS app, Hermes integration.

## 12. Later: spaced repetition path

Exporter reads `study-question` files, emits cards keyed by `c-<uuid>`: either Anki via
`genanki`/AnkiConnect (note GUID = card id, so edits update instead of duplicate) or an in-app
ts-fsrs/py-fsrs review queue with learning state in SQLite only.
