"""Unit tests for RAGService's automatic-retry classification/backoff
helpers - pure functions, no DB/provider needed. End-to-end retry
behavior (event sequence, persistence) is covered separately in
tests/integration/test_rag_service_streaming.py."""

from app.rag.llm.base import (
    LLMProviderResponseError,
    LLMProviderStreamInterruptedError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)
from app.services.rag_service import _classify_stream_retry, _stream_retry_delay_seconds


def test_timeout_is_retryable():
    is_retryable, retry_after = _classify_stream_retry(LLMProviderTimeoutError("timed out"))
    assert is_retryable is True
    assert retry_after is None


def test_stream_interrupted_is_retryable():
    is_retryable, retry_after = _classify_stream_retry(
        LLMProviderStreamInterruptedError("connection lost")
    )
    assert is_retryable is True
    assert retry_after is None


def test_response_error_is_never_retryable():
    is_retryable, retry_after = _classify_stream_retry(
        LLMProviderResponseError("unexpected shape")
    )
    assert is_retryable is False
    assert retry_after is None


def test_unavailable_with_no_status_code_is_retryable():
    # Couldn't reach the server at all (connection refused/DNS) - plausibly
    # a transient network blip, distinct from a real, reachable rejection.
    is_retryable, retry_after = _classify_stream_retry(
        LLMProviderUnavailableError("could not reach backend")
    )
    assert is_retryable is True
    assert retry_after is None


def test_unavailable_429_is_retryable_and_carries_retry_after():
    is_retryable, retry_after = _classify_stream_retry(
        LLMProviderUnavailableError("rate limited", status_code=429, retry_after_seconds=3.5)
    )
    assert is_retryable is True
    assert retry_after == 3.5


def test_unavailable_503_is_retryable():
    is_retryable, _ = _classify_stream_retry(
        LLMProviderUnavailableError("service unavailable", status_code=503)
    )
    assert is_retryable is True


def test_unavailable_401_is_not_retryable():
    is_retryable, _ = _classify_stream_retry(
        LLMProviderUnavailableError("bad credentials", status_code=401)
    )
    assert is_retryable is False


def test_unavailable_400_is_not_retryable():
    is_retryable, _ = _classify_stream_retry(
        LLMProviderUnavailableError("bad request", status_code=400)
    )
    assert is_retryable is False


def test_unavailable_404_is_not_retryable():
    is_retryable, _ = _classify_stream_retry(
        LLMProviderUnavailableError("model not found", status_code=404)
    )
    assert is_retryable is False


def test_unrelated_exception_is_never_retryable():
    is_retryable, retry_after = _classify_stream_retry(ValueError("something else entirely"))
    assert is_retryable is False
    assert retry_after is None


def test_delay_uses_configured_default_when_no_retry_after():
    from app.core.config import settings

    assert _stream_retry_delay_seconds(0, None) == settings.LLM_STREAM_RETRY_DELAYS_SECONDS[0]
    assert _stream_retry_delay_seconds(1, None) == settings.LLM_STREAM_RETRY_DELAYS_SECONDS[1]


def test_delay_uses_last_configured_value_beyond_configured_attempts():
    from app.core.config import settings

    assert _stream_retry_delay_seconds(5, None) == settings.LLM_STREAM_RETRY_DELAYS_SECONDS[-1]


def test_delay_honors_retry_after_within_the_cap():
    from app.core.config import settings

    small_retry_after = min(1.0, settings.LLM_STREAM_RETRY_MAX_DELAY_SECONDS)
    assert _stream_retry_delay_seconds(0, small_retry_after) == small_retry_after


def test_delay_caps_an_excessive_retry_after():
    from app.core.config import settings

    assert _stream_retry_delay_seconds(0, 999.0) == settings.LLM_STREAM_RETRY_MAX_DELAY_SECONDS
