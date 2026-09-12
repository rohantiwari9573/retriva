"""LLMProvider interface.

Mirrors app/rag/embedding/base.py's shape: a small Protocol so the RAG
pipeline calls an abstraction, never LMStudioLLMProvider (or any future
provider) directly.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


class LLMProviderUnavailableError(Exception):
    """The chat backend could not be reached at all, or returned an error
    before any output was produced. Treated as a clear "dependency
    unavailable" failure - callers must never silently fall back to a paid
    API. Deliberately distinct from LLMProviderTimeoutError - "refused the
    connection" and "never responded in time" are different failure modes
    a caller may want to handle differently (e.g. retry timing)."""


class LLMProviderTimeoutError(Exception):
    """The chat backend didn't respond within the configured timeout,
    before producing any output. A sibling of LLMProviderUnavailableError,
    not a subclass - callers that only handle one must explicitly decide
    whether a timeout counts as "unavailable" for their purposes."""


class LLMProviderResponseError(Exception):
    """The chat backend responded, but not in a shape this provider can use
    (malformed JSON, missing fields)."""


class LLMProviderStreamInterruptedError(Exception):
    """A streaming generation started successfully (some tokens were
    already produced) but the connection dropped, timed out, or returned a
    malformed chunk before completion. Deliberately a different exception
    from LLMProviderUnavailableError: "never reached the backend" and "was
    mid-answer when it broke" are different system states, and the caller
    (RAGService/the streaming route) needs to tell them apart - the former
    can be retried from scratch, the latter has already streamed partial
    text to the client that must be discarded, never persisted as if it
    were a complete answer."""


class LLMProvider(Protocol):
    model: str

    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> str: ...

    def stream(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> AsyncGenerator[str, None]:
        """Yield incremental text deltas as the model generates them.

        Raises LLMProviderUnavailableError if the backend can't be reached
        at all, or LLMProviderStreamInterruptedError if the stream broke
        after already yielding at least one delta. Never buffers the full
        response before yielding - callers rely on this for time-to-first-
        token and for forwarding tokens to a client as they arrive.
        """
        ...
