"""LLMProvider interface.

Mirrors app/rag/embedding/base.py's shape: a small Protocol so the RAG
pipeline calls an abstraction, never LMStudioLLMProvider (or any future
provider) directly.
"""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


class LLMProviderUnavailableError(Exception):
    """The chat backend could not be reached, timed out, or returned an
    error. Treated as a clear "dependency unavailable" failure - callers
    must never silently fall back to a paid API."""


class LLMProviderResponseError(Exception):
    """The chat backend responded, but not in a shape this provider can use
    (malformed JSON, missing fields)."""


class LLMProvider(Protocol):
    model: str

    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> str: ...
