# Lernsystem

A self-hosted, mobile-first web app for question-driven learning: draw a question,
write a gut answer, research it with a Socratic AI tutor, then distill two or three
curated knowledge cards into an Obsidian vault backed by Git.

> Status: early development. See [PLAN.md](PLAN.md) for the architecture and roadmap.

## Highlights (planned MVP)

- Generate question cards from a textbook chapter (PDF converted with Docling),
  from a topic, or as bridge questions from your own knowledge profile.
- Question inbox with filters; a gut answer is required before the tutor starts.
- Streaming tutor chat that reacts to your gut answer, gives hints instead of
  solutions and cites book chapters and vault notes.
- Wrap-up with a short summary and 4–6 card proposals to curate.
- One Markdown file per question in your vault; commit and push via a deploy key.
- Runs behind a Tailscale sidecar; no public ports, no login.

## Security

No secrets live in this repository. All keys come from a server-side `.env` file.
After creating your own copy of the repository, enable GitHub **Secret scanning**
and **Push protection** (Settings → Code security). See [SECURITY.md](SECURITY.md).

## Development

Install the Git hooks once (requires [pre-commit](https://pre-commit.com)):

```sh
pre-commit install
```

## License

[MIT](LICENSE)
