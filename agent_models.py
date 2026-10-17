"""Model factory — switch every agent between Gemini and Claude via one env var.

For the AI Builder Cup (Google Cloud), set MODEL_PROVIDER=gemini. For local dev
on your existing Anthropic subscription, leave it unset (defaults to anthropic).

  MODEL_PROVIDER=gemini     -> GEMINI_MODEL   (default "gemini-3.6-flash")
  MODEL_PROVIDER=anthropic  -> ANTHROPIC_MODEL (default "claude-opus-5-5")

Gemini on Vertex AI (recommended on Cloud Run — authenticates via the service
account, no API key):
  GOOGLE_GENAI_USE_VERTEXAI=TRUE
  GOOGLE_CLOUD_PROJECT=<your-project>
  GOOGLE_CLOUD_LOCATION=us-central1
Gemini via AI Studio key instead: set GOOGLE_API_KEY.

This module stays dependency-light (Anthropic is imported lazily) so it can be
imported from any agent package without pulling in heavy deps.
"""

import os


def provider() -> str:
    return os.environ.get("MODEL_PROVIDER", "anthropic").lower()


def build_model(effort: str = "medium"):
    """Returns (model, generate_content_config) to pass to an ADK Agent.

    - Gemini: returns the model-id string and None (ADK reads Vertex/API env).
    - Anthropic: returns an AnthropicLlm plus an effort config.
    """
    if provider() == "gemini":
        return os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"), None

    from google.adk.models.anthropic_llm import (
        AnthropicGenerateContentConfig,
        AnthropicLlm,
    )
    model = AnthropicLlm(model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"))
    return model, AnthropicGenerateContentConfig(effort=effort)
