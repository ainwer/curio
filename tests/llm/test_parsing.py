"""Tests for JSON extraction and output parsing."""

import json

import pytest

from curio_app.llm.base import LLMOutputInvalid
from curio_app.llm.parsing import extract_json, parse_output, schema_instruction
from curio_app.llm.schemas import GeneratedQuestion

VALID = {
    "question": "Why does a diode get hot?",
    "domain": "Power Electronics",
    "concepts": ["Reverse Recovery"],
    "source": "Invented Book, ch. 3",
    "hook": "Because it matters.",
    "bridge_from": "",
}


def test_extract_plain_json() -> None:
    assert extract_json('{"a": 1}') == '{"a": 1}'


def test_extract_json_from_fences() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_extract_json_after_prose_and_before_text() -> None:
    text = 'Sure! Here it is: {"a": 1} hope that helps.'
    assert extract_json(text) == '{"a": 1}'


def test_extract_braces_inside_strings() -> None:
    text = '{"code": "if x { return {} }"}'
    assert extract_json(text) == '{"code": "if x { return {} }"}'


def test_extract_escaped_quotes_inside_strings() -> None:
    text = '{"say": "a \\"brace } inside\\" here", "n": 2}'
    assert extract_json(text) == '{"say": "a \\"brace } inside\\" here", "n": 2}'


def test_extract_nested_objects_and_arrays() -> None:
    text = 'pre {"a": {"b": [{"c": 1}]}, "d": "}"} post'
    assert extract_json(text) == '{"a": {"b": [{"c": 1}]}, "d": "}"}'


def test_extract_only_first_object() -> None:
    text = '{"first": 1} trailing {"second": 2}'
    assert extract_json(text) == '{"first": 1}'


def test_extract_no_json_raises() -> None:
    with pytest.raises(LLMOutputInvalid, match="No JSON object"):
        extract_json("no json in this prose at all")


def test_parse_output_valid() -> None:
    question = parse_output(json.dumps(VALID), GeneratedQuestion)
    assert question.question == "Why does a diode get hot?"
    assert question.domain == "power-electronics"
    assert question.source == "Invented Book, ch. 3"
    assert question.bridge_from is None


def test_parse_output_from_fenced_prose() -> None:
    text = "Here you go:\n```json\n" + json.dumps(VALID) + "\n```\nDone."
    question = parse_output(text, GeneratedQuestion)
    assert question.domain == "power-electronics"


def test_parse_output_invalid_json_trailing_comma() -> None:
    text = '{"question": "Why?", "domain": "a",}'
    with pytest.raises(LLMOutputInvalid, match="not valid JSON"):
        parse_output(text, GeneratedQuestion)


def test_parse_output_no_json() -> None:
    with pytest.raises(LLMOutputInvalid):
        parse_output("nothing parseable", GeneratedQuestion)


def test_parse_output_validation_error_is_short_and_redacted() -> None:
    payload = {**VALID, "question": "Why does the token SECRET-RAWDATA appear?"}
    payload.pop("domain")
    text = json.dumps(payload) + " trailing prose"
    with pytest.raises(LLMOutputInvalid, match="domain") as info:
        parse_output(text, GeneratedQuestion)
    message = str(info.value)
    assert "SECRET-RAWDATA" not in message
    assert "trailing prose" not in message


def test_parse_output_schema_violation_reported() -> None:
    payload = {**VALID, "question": "Not a question."}
    with pytest.raises(LLMOutputInvalid, match="must end with") as info:
        parse_output(json.dumps(payload), GeneratedQuestion)
    message = str(info.value)
    assert json.dumps(payload) not in message


def test_schema_instruction_contains_schema() -> None:
    instruction = schema_instruction(GeneratedQuestion)
    assert instruction.startswith("Return only a JSON object matching this JSON Schema:\n{")
    assert '"GeneratedQuestion"' in instruction
