"""The LLM toggle: Gemini's free tier or a local Ollama model, optionally with fallback."""

from __future__ import annotations

from google.genai.types import HttpRetryOptions
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.ollama import OllamaProvider

from .config import Settings
from .pacing import PacedModel

# Retry free-tier rate limits and server errors with a long, growing backoff.
GEMINI_RETRY = HttpRetryOptions(
    attempts=4,
    initial_delay=10.0,
    max_delay=60.0,
    exp_base=2.0,
    http_status_codes=[429, 500, 502, 503, 504],
)


def _ollama(settings: Settings) -> Model:
    return OpenAIChatModel(
        settings.ollama_model, provider=OllamaProvider(base_url=settings.ollama_base_url)
    )


def build_model(settings: Settings) -> Model:
    """The model for the agent.

    Raises:
        ValueError: If the provider is Google and ``GOOGLE_API_KEY`` is missing.
    """
    if settings.model_provider == "ollama":
        return _ollama(settings)
    if settings.google_api_key is None:
        raise ValueError("GOOGLE_API_KEY is not set")
    provider = GoogleProvider(
        api_key=settings.google_api_key.get_secret_value(), retry_options=GEMINI_RETRY
    )
    gemini = PacedModel(
        GoogleModel(settings.gemini_model, provider=provider),
        settings.gemini_min_interval_seconds,
    )
    if settings.ollama_fallback:
        return FallbackModel(gemini, _ollama(settings))
    return gemini
