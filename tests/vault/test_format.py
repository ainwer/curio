"""Tests for rendering and parsing study-question files (PLAN.md §4)."""

from datetime import UTC, datetime

import pytest

from curio_app.domain import Card, CardType, Question, Section, Status
from curio_app.vault.format import (
    FormatError,
    parse_cards,
    parse_question,
    render_cards,
    render_question,
)

FULL_FILE = """\
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

Asked while reading chapter 4 in the book.

## Gut answer
Less tail current, so lower turn-off losses.

## Insight
**Answer:** Tail charge vanishes quickly.
**Where I was wrong:** I ignored the reverse recovery charge.

## Cards
<!-- card id=c-6f1c2a9e-0d4b-4c51-9a37-2a1f0b8e5d10 type=tradeoff source="Example Book, ch. 4" -->
**Q:** What do you trade when raising the switching frequency of a SiC stage?
**A:** Smaller magnetics and filters versus switching losses $P_{sw} \\propto f_{sw}$ and EMI.
"""

MINIMAL_FILE = """\
---
type: study-question
id: q-2026-10-23-battery-internal-resistance
status: done
domain: power-electronics
concepts: []
created: 2026-10-23T09:00:00+00:00
---
# What determines the internal resistance of a Li-ion cell?

## Gut answer
Electrolyte conductivity and electrode area.

## Cards
"""


def full_question() -> Question:
    card = Card(
        id="c-6f1c2a9e-0d4b-4c51-9a37-2a1f0b8e5d10",
        type=CardType.TRADEOFF,
        front="What do you trade when raising the switching frequency of a SiC stage?",
        back=r"Smaller magnetics and filters versus switching losses "
        r"$P_{sw} \propto f_{sw}$ and EMI.",
        source="Example Book, ch. 4",
    )
    return Question(
        id="q-2026-10-23-why-do-sic-mosfets-switch-faster",
        status=Status.INBOX,
        domain="power-electronics",
        concepts=["switching-losses", "sic"],
        source="Example Book, ch. 4",
        created=datetime(2026, 10, 23, 8, 15, 0, tzinfo=UTC),
        title="Why do SiC MOSFETs allow higher switching frequencies than Si IGBTs?",
        preamble="Asked while reading chapter 4 in the book.",
        sections=[
            Section(heading="Gut answer", body="Less tail current, so lower turn-off losses."),
            Section(
                heading="Insight",
                body="**Answer:** Tail charge vanishes quickly.\n"
                "**Where I was wrong:** I ignored the reverse recovery charge.",
            ),
            Section(heading="Cards", body=render_cards([card])),
        ],
    )


def minimal_question() -> Question:
    return Question(
        id="q-2026-10-23-battery-internal-resistance",
        status=Status.DONE,
        domain="power-electronics",
        created=datetime(2026, 10, 23, 9, 0, 0, tzinfo=UTC),
        title="What determines the internal resistance of a Li-ion cell?",
        sections=[
            Section(heading="Gut answer", body="Electrolyte conductivity and electrode area."),
            Section(heading="Cards", body=""),
        ],
    )


def bare_question() -> Question:
    return Question(
        id="q-2026-10-23-maximum-power-point",
        status=Status.RESEARCHING,
        domain="photovoltaics",
        concepts=["mpp-tracking"],
        created=datetime(2026, 10, 23, 10, 0, 0, tzinfo=UTC),
        title="Why does a solar panel need a maximum power point tracker?",
    )


def assert_round_trip(q: Question) -> None:
    text = render_question(q)
    assert parse_question(text) == q
    assert render_question(parse_question(text)) == text


def test_render_matches_golden_string() -> None:
    assert render_question(full_question()) == FULL_FILE


def test_parse_matches_golden_question() -> None:
    assert parse_question(FULL_FILE) == full_question()


def test_render_minimal_matches_golden_string() -> None:
    assert render_question(minimal_question()) == MINIMAL_FILE


def test_round_trip_bare_question() -> None:
    assert_round_trip(bare_question())


def test_round_trip_with_unknown_sections_around_known_ones() -> None:
    q = full_question().model_copy(
        update={
            "sections": [
                Section(heading="Overview", body="Collected background notes."),
                Section(heading="Gut answer", body="Less tail current, so lower turn-off losses."),
                Section(heading="Scratch", body="Random thoughts.\n\nSecond paragraph."),
                Section(heading="Insight", body="**Answer:** Tail charge vanishes quickly."),
                Section(
                    heading="Cards",
                    body=render_cards(
                        [
                            Card(
                                id="c-9a1b2c3d-0e4f-4a5b-8c6d-7e8f9a0b1c2d",
                                type=CardType.FACT,
                                front="What limits the turn-off of an Si IGBT?",
                                back="The tail current of the bipolar Minority carriers.",
                            ),
                            Card(
                                id="c-1b2c3d4e-5f60-4781-9ab2-c3d4e5f60718",
                                type=CardType.ESTIMATE,
                                front="Estimate the switching loss share at 100 kHz.",
                                back="First line.\n$$\nP_{sw} = 0.5 V I f_{sw}\n$$\nOrder of 10 W.",
                            ),
                        ]
                    ),
                ),
                Section(heading="Epilogue", body="Wrote this after finishing."),
                Section(heading="Insight", body="Should never happen in this test."),
            ]
        }
    )
    assert_round_trip(q)


def test_round_trip_without_source_and_empty_concepts() -> None:
    q = bare_question().model_copy(update={"concepts": [], "source": None})
    text = render_question(q)
    assert "source:" not in text
    assert "concepts: []" in text
    assert_round_trip(q)


def test_round_trip_with_extra_frontmatter() -> None:
    q = bare_question().model_copy(
        update={"extra_frontmatter": {"reviewed": "yes", "tags": ["mosfets", "sic"]}}
    )
    assert_round_trip(q)


def test_round_trip_multiline_card_back_with_latex() -> None:
    card = Card(
        id="c-2a3b4c5d-6e70-4f81-a2b3-c4d5e6f70819",
        type=CardType.WHY,
        front="Why does thermal runaway matter here?\nContext: cell ageing.",
        back="The resistance drop heats the cell.\n$$\nQ = I^2 R\n$$\nMore heat, lower resistance.",
    )
    q = bare_question().with_cards([card])
    assert isinstance(q.cards()[0].back, str)
    assert q.cards()[0].back.count("$$") == 2
    parsed_back = parse_question(render_question(q)).cards()[0].back
    assert parsed_back == card.back
    assert_round_trip(q)


def test_round_trip_preamble_without_sections() -> None:
    q = bare_question().model_copy(update={"preamble": "Only a preamble, nothing else yet."})
    assert_round_trip(q)


def test_render_file_ends_with_single_newline() -> None:
    for q in (bare_question(), full_question(), minimal_question()):
        text = render_question(q)
        assert text.endswith("\n")
        assert not text.endswith("\n\n")


def test_invalid_status_raises() -> None:
    text = FULL_FILE.replace("status: inbox", "status: archive")
    with pytest.raises(FormatError, match="invalid status 'archive'"):
        parse_question(text)


def test_wrong_type_raises() -> None:
    text = FULL_FILE.replace("type: study-question", "type: daily-note")
    with pytest.raises(FormatError, match="invalid type 'daily-note'"):
        parse_question(text)


def test_missing_title_raises() -> None:
    text = (
        "---\n"
        "type: study-question\n"
        "id: q-2026-10-23-no-title\n"
        "status: inbox\n"
        "domain: power-electronics\n"
        "created: 2026-10-23T08:00:00+00:00\n"
        "---\n"
        "\n"
    )
    with pytest.raises(FormatError, match="missing title"):
        parse_question(text)


def test_text_before_title_raises() -> None:
    text = MINIMAL_FILE.replace(
        "# What determines the internal resistance of a Li-ion cell?",
        "Just some prose, no heading here.",
    )
    with pytest.raises(FormatError, match="unexpected text before the title"):
        parse_question(text)


def test_missing_front_matter_raises() -> None:
    with pytest.raises(FormatError, match="missing front matter"):
        parse_question("# Just some text\n")


def test_naive_created_raises() -> None:
    text = FULL_FILE.replace("created: 2026-10-23T08:15:00+00:00", "created: 2026-10-23T08:15:00")
    with pytest.raises(FormatError, match="timezone required"):
        parse_question(text)


def test_invalid_created_raises() -> None:
    text = FULL_FILE.replace("created: 2026-10-23T08:15:00+00:00", "created: not-a-date")
    with pytest.raises(FormatError, match="invalid created"):
        parse_question(text)


def test_invalid_question_id_raises() -> None:
    text = FULL_FILE.replace("id: q-2026-10-23-why", "id: q-not-a-question")
    with pytest.raises(FormatError, match="invalid question id"):
        parse_question(text)


def test_missing_required_key_raises() -> None:
    text = FULL_FILE.replace("domain: power-electronics\n", "")
    with pytest.raises(FormatError, match="missing 'domain'"):
        parse_question(text)


def test_parse_cards_round_trip() -> None:
    cards = [
        Card(
            id="c-3a4b5c6d-7e80-4a91-b2c3-d4e5f6071829",
            type=CardType.TRANSFER,
            front="Apply the tail current argument to thyristors.",
            back="Thyristors latch, so the analogy is weaker.",
            source="Example Book, ch. 4",
        ),
        Card(
            id="c-4b5c6d7e-8f90-4ab2-83c4-d5e6f708293a",
            type=CardType.ESTIMATE,
            front="Estimate the EMI budget for 100 kHz switching.",
            back="Roughly 10 dB more shielding.",
        ),
    ]
    body = render_cards(cards)
    assert parse_cards(body) == cards
    assert render_cards(parse_cards(body)) == body


def test_parse_cards_with_marker_without_source() -> None:
    body = (
        "<!-- card id=c-5c6d7e8f-9a10-4bc3-84d5-e6f708293a4b type=fact -->\n"
        "**Q:** What does the marker omit?\n"
        "**A:** The optional source attribute."
    )
    cards = parse_cards(body)
    assert cards[0].source is None
    assert render_cards(cards) == body


def test_parse_cards_text_before_first_marker_raises() -> None:
    body = (
        "A stray remark.\n\n"
        "<!-- card id=c-5c6d7e8f-9a10-4bc3-84d5-e6f708293a4b type=fact -->\n"
        "**Q:** f\n**A:** b"
    )
    with pytest.raises(FormatError, match="text before the first card marker"):
        parse_cards(body)


def test_parse_cards_without_marker_raises() -> None:
    with pytest.raises(FormatError, match="no card marker found"):
        parse_cards("**Q:** Nothing but a question.\n**A:** And an answer.")


def test_parse_cards_missing_back_raises() -> None:
    body = "<!-- card id=c-5c6d7e8f-9a10-4bc3-84d5-e6f708293a4b type=fact -->\n**Q:** Only asks."
    with pytest.raises(FormatError, match="missing"):
        parse_cards(body)


def test_parse_cards_bad_id_raises() -> None:
    body = "<!-- card id=not-a-uuid type=fact -->\n**Q:** asks\n**A:** answers"
    with pytest.raises(FormatError, match="invalid card id"):
        parse_cards(body)


def test_parse_cards_unknown_type_raises() -> None:
    body = (
        "<!-- card id=c-5c6d7e8f-9a10-4bc3-84d5-e6f708293a4b type=riddle -->\n"
        "**Q:** asks\n**A:** answers"
    )
    with pytest.raises(FormatError, match="invalid card type 'riddle'"):
        parse_cards(body)


def test_render_cards_omits_source_and_quotes_text() -> None:
    card = Card(
        id="c-6d7e8f90-a1b2-4cd3-85e6-f708293a4b5c",
        type=CardType.FACT,
        front="With a source",
        back="Answer",
        source="Another Example, ch. 7",
    )
    body = render_cards([card])
    marker = (
        "<!-- card id=c-6d7e8f90-a1b2-4cd3-85e6-f708293a4b5c type=fact "
        'source="Another Example, ch. 7" -->'
    )
    assert body.startswith(marker)


def test_parse_cards_empty_source_attribute_becomes_none() -> None:
    body = (
        '<!-- card id=c-5c6d7e8f-9a10-4bc3-84d5-e6f708293a4b type=fact source="" -->\n'
        "**Q:** asks\n**A:** answers"
    )
    assert parse_cards(body)[0].source is None


def test_parse_preserves_blank_lines_within_section_body() -> None:
    text = FULL_FILE.replace("Less tail current, so lower turn-off losses.", "First.\n\nThird.")
    q = parse_question(text)
    assert q.gut_answer() == "First.\n\nThird."
