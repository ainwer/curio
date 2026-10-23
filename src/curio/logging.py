"""Plain logging with redaction of configured secret values."""

import logging
from collections.abc import Iterable

from curio.config import Settings

_REDACTED = "***"
_MIN_SECRET_LENGTH = 4
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_redacting_handlers: set[logging.Handler] = set()


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret of at least 4 characters in *text* with ***."""
    for secret in secrets:
        if len(secret) >= _MIN_SECRET_LENGTH and secret in text:
            text = text.replace(secret, _REDACTED)
    return text


class RedactingFilter(logging.Filter):
    """Rewrite records so no configured secret value is visible."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        return True


class RedactingFormatter(logging.Formatter):
    """Redact the final formatted output, including tracebacks added by the formatter."""

    def __init__(self, fmt: str, secrets: Iterable[str]) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def configure_logging(settings: Settings) -> None:
    """Install one redacting stream handler on the root logger."""
    for previous in list(_redacting_handlers):
        _redacting_handlers.discard(previous)
        logging.getLogger().removeHandler(previous)

    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(_FORMAT, settings.secret_values()))
    handler.addFilter(RedactingFilter(settings.secret_values()))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    _redacting_handlers.add(handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        for stale in list(logger.handlers):
            logger.removeHandler(stale)
        logger.propagate = True
