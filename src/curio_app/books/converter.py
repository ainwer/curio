"""Client for the separate Docling converter service."""

import httpx
from fastapi import Request

from curio_app.config import Settings

CONVERTER_UNREACHABLE_MESSAGE = "Converter is not reachable."
_TIMEOUT_SECONDS = 10.0


class ConverterUnavailable(Exception):
    """The converter service did not accept the conversion job."""


class ConverterClient:
    """Simple HTTP client for POST /convert; injectable transport for tests."""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def start(self, book_id: str) -> None:
        """Ask the converter to convert one book; any failure raises ConverterUnavailable."""
        try:
            with httpx.Client(base_url=self._base_url, transport=self._transport) as client:
                response = client.post(
                    "/convert", json={"book_id": book_id}, timeout=_TIMEOUT_SECONDS
                )
        except httpx.HTTPError as exc:
            raise ConverterUnavailable(CONVERTER_UNREACHABLE_MESSAGE) from exc
        if not 200 <= response.status_code < 300:
            raise ConverterUnavailable(CONVERTER_UNREACHABLE_MESSAGE)


def get_converter(request: Request) -> ConverterClient:
    """FastAPI dependency building a ConverterClient from the settings."""
    settings: Settings = request.app.state.settings
    return ConverterClient(settings.converter_url)
