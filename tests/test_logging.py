"""Tests for the redacting logging filter."""

import logging
import sys

from conftest import TEST_SECRET

from lernsystem.logging import RedactingFilter, RedactingFormatter, redact


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.format(record)
        self.records.append(record)


def _make_record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_redact_replaces_secrets() -> None:
    assert redact("token abc123 done", ["abc123"]) == "token *** done"
    assert redact("aaaa bbbb c", ["aaaa", "bbbb"]) == "*** *** c"
    assert redact("aaaa bbbb c", ["dddd"]) == "aaaa bbbb c"
    assert redact("no secrets here", []) == "no secrets here"


def test_redact_respects_minimum_length() -> None:
    assert redact("value ab here", ["ab"]) == "value ab here"
    assert redact("value abcd here", ["abcd"]) == "value *** here"


def test_filter_redacts_message_and_args() -> None:
    handler = _ListHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactingFilter([TEST_SECRET]))
    logger = logging.getLogger("redacting-test")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.error("Request failed with key %s", TEST_SECRET)
        literal = f"Static failure: {TEST_SECRET}"
        logger.error(literal)
    finally:
        logger.removeHandler(handler)

    messages = [record.getMessage() for record in handler.records]
    assert messages[0] == "Request failed with key ***"
    assert messages[1] == "Static failure: ***"
    assert all(record.args == () for record in handler.records)
    assert all(TEST_SECRET not in record.msg for record in handler.records)


def test_filter_redacts_exc_text(secret: str) -> None:
    record = _make_record("boom")
    record.exc_text = f"Traceback with {secret} inside"
    keep = RedactingFilter([secret]).filter(record)

    assert keep is True
    assert record.exc_text == "Traceback with *** inside"


def test_filter_ignores_short_secrets() -> None:
    record = _make_record("value ab here")

    RedactingFilter(["ab"]).filter(record)

    assert record.msg == "value ab here"


def test_formatter_redacts_tracebacks(secret: str) -> None:
    formatter = RedactingFormatter("%(message)s", [secret])
    try:
        raise RuntimeError(f"failure with {secret}")
    except RuntimeError:
        record = logging.getLogger("test").makeRecord(
            "test", logging.ERROR, __file__, 1, "boom", (), sys.exc_info()
        )

    output = formatter.format(record)

    assert "RuntimeError" in output
    assert secret not in output
