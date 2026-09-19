from app.ai.anthropic import AnthropicProvider
from app.ai.gemini import GeminiProvider
from app.ai.openrouter import OpenRouterProvider
from app.ai.provider import AIProvider
from app.core.config import Settings
from app.integrations.errors import IntegrationError


def create_ai_provider(settings: Settings) -> AIProvider:
    provider = settings.ai_provider.lower().strip()

    if provider == "anthropic":
        return AnthropicProvider(settings)

    if provider == "openrouter":
        return OpenRouterProvider(settings)

    if provider == "gemini":
        return GeminiProvider(settings)

    raise IntegrationError(f"Unsupported AI provider: {provider}")