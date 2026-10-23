"""Rendering and parsing of study-question files and card blocks (PLAN.md §4)."""

import re
from datetime import datetime

import yaml

from curio_app.domain import (
    CARD_ID_PATTERN,
    KEBAB_CASE_PATTERN,
    QUESTION_ID_PATTERN,
    Card,
    CardType,
    Question,
    Section,
    Status,
)

FRONT_MATTER_KEYS = ("type", "id", "status", "domain", "concepts", "source", "created")

CARD_MARKER = re.compile(r'^<!-- card id=(\S+) type=(\S+)(?: source="([^"]*)")? -->$')
_Q_PREFIX = "**Q:** "
_A_PREFIX = "**A:** "


class FormatError(ValueError):
    """A file or body does not follow the vault question format."""


def render_question(q: Question) -> str:
    """Render a question exactly as specified in PLAN.md §4 (LF, one trailing newline)."""
    lines = [
        "---",
        "type: study-question",
        f"id: {q.id}",
        f"status: {q.status.value}",
        f"domain: {q.domain}",
        f"concepts: [{', '.join(q.concepts)}]",
    ]
    if q.source is not None:
        lines.append(f'source: "{q.source}"')
    lines.append(f"created: {q.created.isoformat(timespec='seconds')}")
    for key, value in q.extra_frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    out = "\n".join(lines) + "\n" + f"# {q.title}\n"
    if q.preamble:
        out += f"\n{q.preamble}\n"
    if q.sections:
        out += "\n"
        for pos, section in enumerate(q.sections):
            out += f"## {section.heading}\n"
            if section.body:
                out += f"{section.body}\n"
            if pos < len(q.sections) - 1:
                out += "\n"
    return out


def parse_question(text: str) -> Question:
    """Parse a study-question file; raise FormatError on any format violation."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise FormatError("missing front matter")
    close = _front_matter_end(lines)
    data = _front_matter_dict("\n".join(lines[1:close]))
    qtype = _required_str(data, "type")
    if qtype != "study-question":
        raise FormatError(f"invalid type '{qtype}'")
    qid = _required_str(data, "id")
    if re.fullmatch(QUESTION_ID_PATTERN, qid) is None:
        raise FormatError(f"invalid question id '{qid}'")
    status_raw = _required_str(data, "status")
    try:
        status = Status(status_raw)
    except ValueError as exc:
        raise FormatError(f"invalid status '{status_raw}'") from exc
    domain = _required_str(data, "domain")
    if re.fullmatch(KEBAB_CASE_PATTERN, domain) is None:
        raise FormatError(f"invalid domain '{domain}'")
    concepts = _concepts(data.get("concepts"))
    source = data.get("source")
    if source == "":
        source = None
    if source is not None and ('"' in source or "\n" in source):
        raise FormatError(f"invalid source '{source}'")
    created = _created(data)
    extra_frontmatter = {k: v for k, v in data.items() if k not in FRONT_MATTER_KEYS}
    title, preamble, sections = _parse_body(lines[close + 1 :])
    return Question(
        id=qid,
        status=status,
        domain=domain,
        concepts=concepts,
        source=source,
        created=created,
        title=title,
        preamble=preamble,
        sections=sections,
        extra_frontmatter=extra_frontmatter,
    )


def render_cards(cards: list[Card]) -> str:
    """Render card blocks for the "Cards" section body (separated by one blank line)."""
    blocks = []
    for card in cards:
        marker = f"<!-- card id={card.id} type={card.type.value}"
        if card.source is not None:
            marker += f' source="{card.source}"'
        blocks.append(f"{marker} -->\n{_Q_PREFIX}{card.front}\n{_A_PREFIX}{card.back}")
    return "\n\n".join(blocks)


def parse_cards(body: str) -> list[Card]:
    """Parse card blocks from a "Cards" section body; raise FormatError when invalid."""
    lines = body.split("\n")
    found: list[tuple[int, re.Match[str]]] = []
    for i, line in enumerate(lines):
        marker = CARD_MARKER.fullmatch(line)
        if marker is not None:
            found.append((i, marker))
    if not found:
        raise FormatError("no card marker found")
    if any(line.strip() for line in lines[: found[0][0]]):
        raise FormatError("text before the first card marker")
    cards: list[Card] = []
    for pos, (start, marker) in enumerate(found):
        end = found[pos + 1][0] if pos + 1 < len(found) else len(lines)
        cards.append(_parse_card_block(marker, lines[start + 1 : end]))
    return cards


def _front_matter_end(lines: list[str]) -> int:
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i
    raise FormatError("unterminated front matter")


def _front_matter_dict(text: str) -> dict[str, str | list[str]]:
    try:
        # BaseLoader keeps every scalar a string and every value a string or list of strings.
        data = yaml.load(text, Loader=yaml.BaseLoader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise FormatError(f"invalid front matter: {exc}") from exc
    if not isinstance(data, dict):
        raise FormatError("front matter must be a mapping")
    result: dict[str, str | list[str]] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise FormatError("front matter keys must be strings")
        if isinstance(value, str):
            result[key] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[key] = [str(item) for item in value]
        else:
            raise FormatError(f"invalid value for '{key}'")
    return result


def _required_str(data: dict[str, str | list[str]], key: str) -> str:
    value = data.get(key)
    if value is None:
        raise FormatError(f"missing '{key}'")
    if not isinstance(value, str):
        raise FormatError(f"invalid {key} '{value}'")
    return value


def _concepts(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FormatError(f"invalid concepts '{value}'")
    for concept in value:
        if re.fullmatch(KEBAB_CASE_PATTERN, concept) is None:
            raise FormatError(f"invalid concept '{concept}'")
    return list(value)


def _created(data: dict[str, str | list[str]]) -> datetime:
    raw = _required_str(data, "created")
    try:
        created = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise FormatError(f"invalid created '{raw}'") from exc
    if created.tzinfo is None or created.tzinfo.utcoffset(created) is None:
        raise FormatError(f"invalid created '{raw}' (timezone required)")
    return created


def _parse_body(lines: list[str]) -> tuple[str, str, list[Section]]:
    title_index = None
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title_index = i
            break
        if line.strip() == "":
            continue
        raise FormatError("unexpected text before the title")
    if title_index is None:
        raise FormatError("missing title")
    title = lines[title_index][2:].strip()
    headings = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if headings:
        preamble = "\n".join(lines[title_index + 1 : headings[0]]).strip()
    else:
        preamble = "\n".join(lines[title_index + 1 :]).strip()
    sections = []
    for pos, start in enumerate(headings):
        end = headings[pos + 1] if pos + 1 < len(headings) else len(lines)
        heading = lines[start][3:].strip()
        body = "\n".join(_trim_blank_lines(lines[start + 1 : end]))
        sections.append(Section(heading=heading, body=body))
    return title, preamble, sections


def _trim_blank_lines(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and lines[start].strip() == "":
        start += 1
    while end > start and lines[end - 1].strip() == "":
        end -= 1
    return lines[start:end]


def _parse_card_block(marker: re.Match[str], block: list[str]) -> Card:
    card_id, card_type, card_source = marker.group(1), marker.group(2), marker.group(3)
    if re.fullmatch(CARD_ID_PATTERN, card_id) is None:
        raise FormatError(f"invalid card id '{card_id}'")
    try:
        ctype = CardType(card_type)
    except ValueError as exc:
        raise FormatError(f"invalid card type '{card_type}'") from exc
    q_index = _find_prefix_line(block, _Q_PREFIX, f"missing '{_Q_PREFIX.strip()}'")
    a_index = _find_prefix_line(block[q_index + 1 :], _A_PREFIX, f"missing '{_A_PREFIX.strip()}'")
    a_index += q_index + 1
    front = "\n".join([block[q_index][len(_Q_PREFIX) :], *block[q_index + 1 : a_index]])
    back = "\n".join([block[a_index][len(_A_PREFIX) :], *block[a_index + 1 :]])
    return Card(id=card_id, type=ctype, front=front, back=back, source=card_source)


def _find_prefix_line(lines: list[str], prefix: str, message: str) -> int:
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    raise FormatError(message)
