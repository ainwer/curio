"""Extract and parse JSON model output into typed schemas."""

import json

from pydantic import BaseModel, ValidationError

from curio_app.llm.base import LLMOutputInvalid, T


def extract_json(text: str) -> str:
    """Return the first balanced top-level ``{...}`` object from *text*.

    String-aware brace matching: braces inside strings and escaped quotes do not
    confuse the scan. Raises :class:`LLMOutputInvalid` when no object is found.
    """
    index = text.find("{")
    while index != -1:
        candidate = _balanced_braces(text, index)
        if candidate is not None:
            return candidate
        index = text.find("{", index + 1)
    raise LLMOutputInvalid("No JSON object was found in the output.")


def _balanced_braces(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        character = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start : position + 1]
    return None


def parse_output(text: str, schema: type[T]) -> T:
    """Parse *text* into an instance of *schema*; failures become ``LLMOutputInvalid``."""
    try:
        return schema.model_validate(json.loads(extract_json(text)))
    except json.JSONDecodeError as error:
        raise LLMOutputInvalid(f"Output is not valid JSON ({error.msg}).") from None
    except ValidationError as error:
        raise LLMOutputInvalid(_describe_validation_error(error)) from None


def _describe_validation_error(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"Invalid output at {location}: {first['msg']}"


def schema_instruction(schema: type[BaseModel]) -> str:
    """A system-prompt appendix that asks for exactly one JSON object."""
    return "Return only a JSON object matching this JSON Schema:\n" + json.dumps(
        schema.model_json_schema(), indent=2
    )
