"""Tests for the app skeleton routes and error handling."""

import pytest
from conftest import TEST_SECRET
from fastapi.testclient import TestClient

from curio_app.config import Settings
from curio_app.main import create_app


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("path", "tab"),
    [("/", "inbox"), ("/generate", "generate"), ("/books", "books")],
)
def test_pages_render(client: TestClient, path: str, tab: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'class="tab-bar"' in response.text
    assert 'aria-current="page"' in response.text
    active = f'<a href="{path}" class="tab is-active" aria-current="page">'
    assert active in response.text


def test_inbox_empty_state(client: TestClient) -> None:
    response = client.get("/")

    assert "No questions yet. Generate some to get started." in response.text
    assert 'href="/generate"' in response.text


def test_manifest_content_type(client: TestClient) -> None:
    response = client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")


def test_sw_js_served(client: TestClient) -> None:
    response = client.get("/sw.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-cache"


def test_unknown_path_renders_error_page(client: TestClient) -> None:
    response = client.get("/does-not-exist", headers={"accept": "text/html"})

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "Not found" in response.text


def test_unhandled_error_is_generic(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(settings)

    def boom() -> None:
        message = f"secret {TEST_SECRET}"
        raise RuntimeError(message)

    app.add_api_route("/boom", boom, methods=["GET"])
    client = TestClient(app, raise_server_exceptions=False)

    html_response = client.get("/boom", headers={"accept": "text/html"})
    json_response = client.get("/boom")

    assert html_response.status_code == 500
    assert "Something went wrong. Please try again." in html_response.text
    assert TEST_SECRET not in html_response.text

    assert json_response.status_code == 500
    assert json_response.json() == {"error": "internal_error"}
    assert TEST_SECRET not in json_response.text

    assert TEST_SECRET not in caplog.text
    assert "RuntimeError" in caplog.text
