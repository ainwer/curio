"""Tests for the markdown filter with math protection."""

from markupsafe import Markup

from curio_app.web.markdown import render_markdown


def test_emphasis_inside_math_is_not_touched() -> None:
    rendered = render_markdown("Gut answer: $a_b * c_d$ stays.")

    assert "$a_b * c_d$" in rendered
    assert "<em>" not in rendered
    assert "<strong>" not in rendered


def test_inline_math_renders_around_emphasis() -> None:
    rendered = render_markdown("*real* emphasis with $x_1$.")

    assert "<em>real</em>" in rendered
    assert "$x_1$" in rendered


def test_display_math_across_lines_survives() -> None:
    rendered = render_markdown("Before.\n\n$$\nx + y = z\n$$\n\nAfter.")

    assert "$$\nx + y = z\n$$" in rendered
    assert "<p>Before.</p>" in rendered
    assert "<p>After.</p>" in rendered


def test_raw_html_is_escaped() -> None:
    rendered = render_markdown("<script>alert(1)</script>")

    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered


def test_literal_escaped_dollar_stays_text() -> None:
    rendered = render_markdown(r"Costs \$5 and change.")

    # CommonMark unescapes \$ to a literal $; it must not open a math segment.
    assert "$5 and change." in rendered
    assert "$$5" not in rendered


def test_tables_render_with_the_table_rule_enabled() -> None:
    rendered = render_markdown("| A | B |\n| - | - |\n| 1 | 2 |")

    assert "<table>" in rendered
    assert "<td>1</td>" in rendered


def test_returns_safe_markup() -> None:
    rendered = render_markdown("$x$")

    assert isinstance(rendered, Markup)
