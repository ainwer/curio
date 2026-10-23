"""Split converted book Markdown into chapters and searchable sections."""

import re
from dataclasses import dataclass

CHAPTER_HEADING_PATTERN = re.compile(
    r"^#{1,3}\s+(?:Chapter\s+)?(\d{1,3})(?!\.\d)(?:[.:]?\s+)(\S.*)$",
    re.IGNORECASE,
)
H1_PATTERN = re.compile(r"^#\s+\S.*$")
H1_TITLE_PATTERN = re.compile(r"^#\s+")
ANY_HEADING_PATTERN = re.compile(r"^#{1,6}\s+")

FRONT_MATTER_TITLE = "Front matter"
MIN_FRONT_MATTER_CHARS = 2000


@dataclass(frozen=True)
class Chapter:
    """One chapter produced by splitting a converted book."""

    number: int
    title: str
    text: str


@dataclass(frozen=True)
class SectionText:
    """One section of a chapter: a heading plus the text after it."""

    heading: str
    text: str


@dataclass(frozen=True)
class _Segment:
    number: int
    title: str
    body: str
    full_text: str


def split_chapters(markdown: str, fallback_title: str) -> list[Chapter]:
    """Split Markdown into chapters; drop empty chapters and short front matter."""
    lines = markdown.splitlines()
    chapters = _split_at_chapter_headings(lines)
    if chapters is None:
        chapters = _split_at_h1_headings(lines)
    if chapters is None:
        text = markdown.strip()
        if not text:
            return []
        return [Chapter(number=1, title=fallback_title, text=text)]
    return [chapter for chapter in chapters if chapter.text]


def split_sections(chapter_text: str) -> list[SectionText]:
    """Split a chapter on any Markdown heading; text before the first heading has no heading."""
    sections: list[SectionText] = []
    heading = ""
    body_lines: list[str] = []

    def flush() -> None:
        text = "\n".join(body_lines).strip()
        if text:
            sections.append(SectionText(heading=heading, text=text))

    for line in chapter_text.splitlines():
        match = ANY_HEADING_PATTERN.match(line)
        if match is None:
            body_lines.append(line)
            continue
        flush()
        heading = line[match.end() :].strip()
        body_lines = []
    flush()
    return sections


def _segment_at(lines: list[str], index: int, end: int, number: int, title: str) -> _Segment:
    return _Segment(
        number=number,
        title=title,
        body="\n".join(lines[index + 1 : end]).strip(),
        full_text="\n".join(lines[index:end]).strip(),
    )


def _split_at_chapter_headings(lines: list[str]) -> list[Chapter] | None:
    found: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = CHAPTER_HEADING_PATTERN.match(line)
        if match is not None:
            found.append((index, match))
    if len(found) < 2:
        return None
    segments: dict[int, _Segment] = {}
    for position, (index, match) in enumerate(found):
        end = found[position + 1][0] if position + 1 < len(found) else len(lines)
        segment = _segment_at(lines, index, end, int(match.group(1)), match.group(2).strip())
        existing = segments.get(segment.number)
        if existing is None or len(segment.body) >= len(existing.body):
            segments[segment.number] = segment
    return _assemble(lines, list(segments), segments, found[0][0])


def _split_at_h1_headings(lines: list[str]) -> list[Chapter] | None:
    indexes = [index for index, line in enumerate(lines) if H1_PATTERN.match(line)]
    if len(indexes) < 2:
        return None
    segments: dict[int, _Segment] = {}
    for position, index in enumerate(indexes):
        end = indexes[position + 1] if position + 1 < len(indexes) else len(lines)
        title = H1_TITLE_PATTERN.sub("", lines[index], count=1).strip()
        segments[position + 1] = _segment_at(lines, index, end, position + 1, title)
    return _assemble(lines, list(segments), segments, indexes[0])


def _assemble(
    lines: list[str], order: list[int], segments: dict[int, _Segment], front_matter_end: int
) -> list[Chapter]:
    chapters: list[Chapter] = []
    front_matter = "\n".join(lines[:front_matter_end]).strip()
    if len(front_matter) >= MIN_FRONT_MATTER_CHARS:
        chapters.append(Chapter(number=0, title=FRONT_MATTER_TITLE, text=front_matter))
    for key in order:
        segment = segments[key]
        chapters.append(Chapter(number=segment.number, title=segment.title, text=segment.full_text))
    return chapters
