"""Tests for Markdown chapter and section splitting."""

from curio_app.books.chapters import split_chapters, split_sections

TWO_CHAPTERS = (
    "## 1 Intro\n\nOhms law text.\n\n## 1.1 Units\n\nUnit text.\n\n## 2 Diodes\n\nDiode text.\n"
)


def test_numbered_chapters_split_at_numbered_headings() -> None:
    chapters = split_chapters(TWO_CHAPTERS, "Fallback")

    assert [chapter.number for chapter in chapters] == [1, 2]
    assert [chapter.title for chapter in chapters] == ["Intro", "Diodes"]
    assert chapters[0].text.startswith("## 1 Intro")
    assert "## 1.1 Units" in chapters[0].text
    assert "Diode text." in chapters[1].text


def test_chapter_word_prefix_style() -> None:
    markdown = "## Chapter 3: Rectifiers\n\nBody three.\n\n## Chapter 4: Inverters\n\nBody four."

    chapters = split_chapters(markdown, "Fallback")

    assert [(chapter.number, chapter.title) for chapter in chapters] == [
        (3, "Rectifiers"),
        (4, "Inverters"),
    ]


def test_subsection_headings_do_not_start_chapters() -> None:
    markdown = "## 3.1 Detail\n\nSub body.\n\n## 3.2 More\n\nMore body."

    chapters = split_chapters(markdown, "Fallback Title")

    assert len(chapters) == 1
    assert chapters[0].number == 1
    assert chapters[0].title == "Fallback Title"
    assert "## 3.1 Detail" in chapters[0].text


def test_duplicate_numbers_keep_richest_occurrence() -> None:
    toc_entries = "## 1 Intro\n## 2 Methods\n"
    real_bodies = "## 1 Intro\n\nLong real intro body about circuits.\n"
    real_bodies += "## 2 Methods\n\nLong real methods body about measurements.\n"
    markdown = toc_entries + real_bodies

    chapters = split_chapters(markdown, "Fallback")

    assert [chapter.number for chapter in chapters] == [1, 2]
    assert "Long real intro body" in chapters[0].text
    assert "Long real methods body" in chapters[1].text


def test_h1_headings_numbered_sequentially() -> None:
    markdown = "# Alpha\n\nAlpha text.\n\n# Beta\n\nBeta text."

    chapters = split_chapters(markdown, "Fallback")

    assert [(chapter.number, chapter.title) for chapter in chapters] == [
        (1, "Alpha"),
        (2, "Beta"),
    ]
    assert "Alpha text." in chapters[0].text
    assert chapters[1].text.startswith("# Beta")


def test_no_headings_single_chapter() -> None:
    markdown = "Just plain text\nspread over lines."

    chapters = split_chapters(markdown, "Fallback Title")

    assert len(chapters) == 1
    assert chapters[0].number == 1
    assert chapters[0].title == "Fallback Title"
    assert chapters[0].text == "Just plain text\nspread over lines."


def test_short_front_matter_dropped() -> None:
    markdown = "Small preface.\n\n## 1 Foo\n\nBody one.\n\n## 2 Bar\n\nBody two."

    chapters = split_chapters(markdown, "Fallback")

    assert [chapter.number for chapter in chapters] == [1, 2]
    assert "Small preface." not in chapters[0].text


def test_long_front_matter_kept_as_chapter_zero() -> None:
    markdown = "P" * 2500 + "\n\n## 1 Foo\n\nBody one.\n\n## 2 Bar\n\nBody two."

    chapters = split_chapters(markdown, "Fallback")

    assert [chapter.number for chapter in chapters] == [0, 1, 2]
    assert chapters[0].title == "Front matter"
    assert chapters[0].text == "P" * 2500
    assert chapters[1].title == "Foo"


def test_split_sections() -> None:
    chapter_text = "Intro paragraph.\n\n## Section A\n\nBody A.\n\n### Deep\n\nBody deep."

    sections = split_sections(chapter_text)

    assert [(section.heading, section.text) for section in sections] == [
        ("", "Intro paragraph."),
        ("Section A", "Body A."),
        ("Deep", "Body deep."),
    ]


def test_split_sections_starting_with_heading() -> None:
    sections = split_sections("# Only\n\nbody here")

    assert [(section.heading, section.text) for section in sections] == [("Only", "body here")]
