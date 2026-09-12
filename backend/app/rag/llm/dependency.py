"""LLM provider factory - same pattern as app/rag/embedding/dependency.py.

A plain function, not a cached singleton, so both FastAPI (via Depends) and
any future non-request caller construct providers the same way.
"""

from app.core.config import settings
from app.rag.llm.base import LLMProvider
from app.rag.llm.lmstudio import LMStudioLLMProvider


def get_llm_provider() -> LLMProvider:
    if settings.LLM_PROVIDER == "openai_compatible":
        return LMStudioLLMProvider()
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.LLM_PROVIDER}")
