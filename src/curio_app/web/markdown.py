"""Markdown to HTML rendering (no raw HTML) for question content."""

from markdown_it import MarkdownIt

_markdown = MarkdownIt("commonmark", {"html": False})


def render_markdown(text: str) -> str:
    """Render a markdown fragment to HTML; inline HTML is always escaped."""
    rendered = _markdown.render(text)
    return str(rendered)
