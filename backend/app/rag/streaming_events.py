"""SSE event protocol for streaming chat responses - the one place these
shapes are defined. RAGService.ask_stream() yields these plain dataclasses;
the API route is the only place that knows how to turn one into an actual
`event: ...\\ndata: ...\\n\\n` wire frame (see format_sse()) - RAGService has
no knowledge of HTTP/SSE, same layering rule as the rest of this package.

Every event carries a flat, JSON-serializable `data` shape so the frontend
has exactly one parsing path per event type, never a bare string payload.
"""

import json
import uuid
from dataclasses import asdict, dataclass

from app.rag.citations import Citation

# ---- Domain events yielded by RAGService.ask_stream() ----


@dataclass(frozen=True)
class MessageStartEvent:
    """Emitted once, immediately after the user's message is persisted -
    before retrieval or generation begins - so the frontend can render the
    user's turn and a "thinking" state right away."""

    conversation_id: uuid.UUID
    user_message_id: uuid.UUID


@dataclass(frozen=True)
class TokenEvent:
    """One incremental text delta from the LLM. Raw model output - may
    contain a [SOURCE-N] tag split across multiple TokenEvents, and may
    contain a tag the citation validator will later strip. The frontend
    should render tokens as they arrive but replace its buffer with
    MessageCompleteEvent.answer once that arrives (see docs/streaming.md)."""

    text: str


@dataclass(frozen=True)
class CitationsEvent:
    """The validated citation list for the completed answer - always
    emitted, even when empty (zero valid citations after generation is
    itself meaningful: it means the answer was demoted to the
    insufficient-evidence response)."""

    citations: list[Citation]


@dataclass(frozen=True)
class MessageCompleteEvent:
    """The authoritative final answer text, after citation validation may
    have stripped fabricated [SOURCE-N] tags or replaced the whole answer
    with the insufficient-evidence response. The frontend must replace its
    accumulated token buffer with `answer`, not keep what it rendered from
    TokenEvents - those are unvalidated model output."""

    message_id: uuid.UUID
    answer: str
    chunks_considered: int
    chunks_used: int


@dataclass(frozen=True)
class ErrorEvent:
    """A failure that occurred after the SSE stream had already started (so
    it can't be reported as a normal HTTP error status - the 200 and
    headers are already sent). `code` matches the app's structured error
    codes (see app/core/exceptions.py) so the frontend can distinguish, for
    example, LLM_UNAVAILABLE from RETRIEVAL_FAILED rather than showing one
    generic "something went wrong" message for every failure mode."""

    code: str
    message: str


@dataclass(frozen=True)
class RetryingEvent:
    """Emitted when a transient provider failure (HTTP 429/503, a timeout,
    or a dropped connection - never an auth/request error or a malformed
    response) triggers an automatic retry, before the fresh attempt's own
    TokenEvents start arriving - see RAGService's retry policy.

    Any TokenEvents already emitted for the failed attempt were genuine
    model output at the time, but each retry is a brand-new generation,
    never a continuation - the frontend must discard/reset whatever it had
    accumulated from the failed attempt upon receiving this event, or the
    next attempt's tokens would silently concatenate onto the discarded
    ones. `attempt` is 1-indexed and counts the attempt about to start
    (2 for the first retry, 3 for the second), `max_attempts` is the total
    including the initial one (fixed at 3 today - see
    settings.LLM_STREAM_MAX_RETRIES)."""

    attempt: int
    max_attempts: int


StreamEvent = (
    MessageStartEvent
    | TokenEvent
    | CitationsEvent
    | MessageCompleteEvent
    | ErrorEvent
    | RetryingEvent
)

_EVENT_NAMES: dict[type, str] = {
    MessageStartEvent: "message_start",
    TokenEvent: "token",
    CitationsEvent: "citations",
    MessageCompleteEvent: "message_complete",
    RetryingEvent: "retrying",
    ErrorEvent: "error",
}


def _json_default(value: object) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"Object of type {type(value)} is not JSON serializable")


def format_sse(event: StreamEvent) -> str:
    """Render one domain event as a complete SSE wire frame, including the
    trailing blank line the SSE spec requires between events."""
    name = _EVENT_NAMES[type(event)]
    data = json.dumps(asdict(event), default=_json_default)
    return f"event: {name}\ndata: {data}\n\n"
