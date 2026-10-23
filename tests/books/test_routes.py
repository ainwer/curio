"""Route tests for the books feature."""

from fastapi.testclient import TestClient

from books.support import PDF_BYTES, FakeConverter, upload, write_converter_output
from curio_app.books.converter import ConverterUnavailable, get_converter
from curio_app.books.store import BookStore
from curio_app.config import Settings
from curio_app.main import create_app

TWO_CHAPTER_MARKDOWN = "## 1 Intro\n\nIntro body.\n\n## 2 Diodes\n\nDiode body.\n"


def test_upload_starts_conversion_and_saves_file(
    books_client: TestClient, fake_converter, store: BookStore, settings: Settings
) -> None:
    response = upload(books_client)

    assert response.status_code == 303
    assert response.headers["location"] == "/books"
    assert len(fake_converter.started) == 1
    books = store.list()
    assert len(books) == 1
    assert books[0].title == "Test Book"
    assert books[0].state == "converting"
    assert books[0].filename == "book.pdf"
    assert (settings.books_path / books[0].id / "source.pdf").read_bytes() == PDF_BYTES
    page = books_client.get("/books")
    assert "Test Book" in page.text
    assert f'hx-get="/books/{books[0].id}/status"' in page.text
    assert 'hx-trigger="every 5s"' in page.text


def test_upload_rejects_non_pdf_and_cleans_up(
    books_client: TestClient, fake_converter, store: BookStore, settings: Settings
) -> None:
    response = upload(books_client, title="Nope", content=b"definitely not a pdf")

    assert response.status_code == 400
    assert "not a PDF" in response.text
    assert store.list() == []
    assert list(settings.books_path.iterdir()) == []
    assert fake_converter.started == []


def test_upload_rejects_blank_title(books_client: TestClient) -> None:
    response = upload(books_client, title="   ")

    assert response.status_code == 400
    assert "Please enter a title" in response.text


def test_upload_rejects_missing_file(books_client: TestClient) -> None:
    response = books_client.post("/books", data={"title": "Some Title"})

    assert response.status_code == 400
    assert "Please choose a PDF file" in response.text


def test_upload_rejects_too_large(settings: Settings, fake_converter) -> None:
    small_settings = settings.model_copy(update={"max_upload_mb": 0})
    app = create_app(small_settings)
    app.dependency_overrides[get_converter] = lambda: fake_converter
    client = TestClient(app)

    response = upload(client)

    assert response.status_code == 400
    assert "larger than the limit" in response.text
    assert list(small_settings.books_path.iterdir()) == []


def test_upload_marks_failed_when_converter_unreachable(
    settings: Settings, store: BookStore
) -> None:
    app = create_app(settings)
    app.dependency_overrides[get_converter] = lambda: FakeConverter(error=ConverterUnavailable())
    client = TestClient(app)

    response = upload(client)

    assert response.status_code == 303
    books = store.list()
    assert len(books) == 1
    assert books[0].state == "failed"
    assert books[0].error == "Converter is not reachable."
    page = client.get(f"/books/{books[0].id}")
    assert "Converter is not reachable." in page.text


def test_retry_starts_conversion_again(
    settings: Settings, store: BookStore, fake_converter
) -> None:
    app = create_app(settings)
    app.dependency_overrides[get_converter] = lambda: fake_converter
    client = TestClient(app)
    fake_converter.error = ConverterUnavailable()
    upload(client)
    fake_converter.error = None
    book = store.list()[0]
    assert book.state == "failed"

    response = client.post(f"/books/{book.id}/retry", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/books/{book.id}"
    assert fake_converter.started == [book.id]
    assert store.list()[0].state == "converting"


def test_status_endpoint_syncs_to_ready(
    books_client: TestClient, store: BookStore, settings: Settings
) -> None:
    upload(books_client)
    book = store.list()[0]
    write_converter_output(store, book.id, TWO_CHAPTER_MARKDOWN, state="done")

    fragment = books_client.get(f"/books/{book.id}/status")

    assert fragment.status_code == 200
    assert "ready" in fragment.text
    assert "hx-get" not in fragment.text
    assert f'href="/books/{book.id}"' in fragment.text
    chapters = store.chapters(book.id)
    assert [chapter.title for chapter in chapters] == ["Intro", "Diodes"]
    page = books_client.get(f"/books/{book.id}")
    assert "Ch. 1" in page.text
    assert "Ch. 2" in page.text
    assert f'href="/generate?chapter={chapters[0].id}"' in page.text
    assert f'href="/generate?chapter={chapters[1].id}"' in page.text
    assert "~1 min read" in page.text


def test_unknown_book_returns_404(books_client: TestClient) -> None:
    page = books_client.get("/books/b-none-000000", headers={"accept": "text/html"})
    status = books_client.get("/books/b-none-000000/status")

    assert page.status_code == 404
    assert status.status_code == 404
