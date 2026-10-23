"""Markdown to HTML rendering (no raw HTML) with math protection for KaTeX."""

import html
import re

from markdown_it import MarkdownIt
from markupsafe import Markup

_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
# Inline math: single line, opening and closing $ not preceded by a backslash.
_INLINE_MATH = re.compile(r"(?<!\\)\$([^$\n]+?)\$")

_STASH_PREFIX = "@@CURIOMATH"

# CommonMark preset, raw HTML always escaped; the table extension is enabled on top.
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")


def render_markdown(text: str) -> Markup:
    """Render markdown, keeping math segments untouched for the browser's KaTeX.

    ``$$...$$`` (may span lines) and ``$...$`` (single line) are replaced with
    placeholders before rendering and restored HTML-escaped afterwards, so
    emphasis and inline HTML rules never touch their content.
    """
    segments: list[str] = []

    def stash(match: re.Match[str]) -> str:
        segments.append(match.group(0))
        return f"{_STASH_PREFIX}{len(segments) - 1}@@"

    protected = _DISPLAY_MATH.sub(stash, text)
    protected = _INLINE_MATH.sub(stash, protected)
    rendered = _markdown.render(protected)
    for index, segment in enumerate(segments):
        rendered = rendered.replace(f"{_STASH_PREFIX}{index}@@", html.escape(segment, quote=False))
    # The input is fully escaped: raw HTML is disabled in MarkdownIt and math is escaped.
    return Markup(rendered)  # noqa: S704
