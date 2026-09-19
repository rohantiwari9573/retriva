"""Unit tests for the LLM/embedding provider factories
(app/rag/llm/dependency.py, app/rag/embedding/dependency.py) - proves
provider selection is genuinely configuration-driven: the default
(LM Studio / any openai_compatible endpoint) keeps working unchanged, a
new EMBEDDING_PROVIDER=gemini value selects the Gemini-specific
embedding class, and an unrecognized value fails loudly rather than
silently falling back to something unexpected.
"""

import pytest

from app.rag.embedding.dependency import get_embedding_provider
from app.rag.embedding.gemini import GeminiEmbeddingProvider
from app.rag.embedding.lmstudio import LMStudioEmbeddingProvider
from app.rag.llm.dependency import get_llm_provider
from app.rag.llm.lmstudio import LMStudioLLMProvider


def test_default_embedding_provider_is_lmstudio_class(monkeypatch):
    # "openai_compatible" is the historical/default value - this proves
    # existing local LM Studio configuration is untouched by adding Gemini.
    monkeypatch.setattr(
        "app.rag.embedding.dependency.settings.EMBEDDING_PROVIDER", "openai_compatible"
    )
    provider = get_embedding_provider()
    assert isinstance(provider, LMStudioEmbeddingProvider)


def test_gemini_embedding_provider_selected_by_config(monkeypatch):
    monkeypatch.setattr("app.rag.embedding.dependency.settings.EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setattr("app.core.config.settings.EMBEDDING_API_KEY", "fake-key")
    provider = get_embedding_provider()
    assert isinstance(provider, GeminiEmbeddingProvider)


def test_unknown_embedding_provider_raises_value_error(monkeypatch):
    monkeypatch.setattr("app.rag.embedding.dependency.settings.EMBEDDING_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="Unknown EMBEDDING_PROVIDER"):
        get_embedding_provider()


def test_default_llm_provider_is_lmstudio_class(monkeypatch):
    # Gemini's chat/completions API is reached through this SAME class
    # (LLM_PROVIDER stays "openai_compatible", only LLM_BASE_URL/LLM_MODEL/
    # LLM_API_KEY differ) - see config.py's comment on why there is no
    # separate LLM_PROVIDER="gemini" value.
    monkeypatch.setattr("app.rag.llm.dependency.settings.LLM_PROVIDER", "openai_compatible")
    provider = get_llm_provider()
    assert isinstance(provider, LMStudioLLMProvider)


def test_unknown_llm_provider_raises_value_error(monkeypatch):
    monkeypatch.setattr("app.rag.llm.dependency.settings.LLM_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_llm_provider()
