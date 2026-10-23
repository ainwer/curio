"""Tests for LLM client selection."""

from curio_app.config import Settings
from curio_app.llm.anthropic import AnthropicClient
from curio_app.llm.factory import create_llm_client
from curio_app.llm.fake import FakeLLMClient
from curio_app.llm.openai_compat import OpenAICompatClient


def test_anthropic_provider() -> None:
    client = create_llm_client(Settings(llm_provider="anthropic"))
    assert isinstance(client, AnthropicClient)


def test_openai_compatible_provider() -> None:
    client = create_llm_client(Settings(llm_provider="openai_compatible"))
    assert isinstance(client, OpenAICompatClient)


def test_fake_provider() -> None:
    client = create_llm_client(Settings(llm_provider="fake"))
    assert isinstance(client, FakeLLMClient)


def test_anthropic_client_stays_lazy() -> None:
    client = AnthropicClient(Settings(llm_provider="anthropic"))
    assert client.__dict__.get("_created") is None
