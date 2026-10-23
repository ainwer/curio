"""Select the LLM client configured in the settings."""

from curio_app.config import Settings
from curio_app.llm.anthropic import AnthropicClient
from curio_app.llm.base import LLMClient
from curio_app.llm.fake import FakeLLMClient
from curio_app.llm.openai_compat import OpenAICompatClient


def create_llm_client(settings: Settings) -> LLMClient:
    """Instantiate the provider named by ``settings.llm_provider``."""
    match settings.llm_provider:
        case "anthropic":
            return AnthropicClient(settings)
        case "openai_compatible":
            return OpenAICompatClient(settings)
        case _:
            return FakeLLMClient()
