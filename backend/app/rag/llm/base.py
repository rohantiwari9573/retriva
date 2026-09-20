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
    a caller may want to handle differently (e.g. retry timing).

    `status_code` is the upstream HTTP status when one was actually
    received (None if the connection never got that far, e.g. DNS/refused)
    - callers use it to decide whether automatic retry is appropriate
    (429/503 or no response at all are commonly transient; 400/401/403/404
    are not, and retrying them wastes an attempt on something that will
    never succeed). `retry_after_seconds`, when the upstream sent a
    `Retry-After` header with a plain integer/float seconds value, is the
    provider's own stated wait time - parsed defensively, never an HTTP-date
    form, and always still subject to the caller's own maximum delay cap."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


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
