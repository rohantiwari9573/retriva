"""LLM provider backed by an OpenAI-compatible /v1/chat/completions endpoint.

Same shape and same reasoning as LMStudioEmbeddingProvider
(app/rag/embedding/lmstudio.py): works against LM Studio's local server
(zero-cost target) or any other OpenAI-compatible endpoint, and opens a
fresh httpx.AsyncClient per call rather than caching one on the instance, to
avoid binding a connection pool to one event loop across FastAPI/Celery
contexts.

LLM_REQUEST_TIMEOUT_SECONDS defaults far higher than the embedding
provider's timeout - local CPU chat generation routinely takes 30-120s,
where a short timeout would misreport a working-but-slow LM Studio as
unavailable.
"""

import json
from collections.abc import AsyncGenerator

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.rag.llm.base import (
    ChatMessage,
    LLMProviderResponseError,
    LLMProviderStreamInterruptedError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)

logger = get_logger(__name__)


class LMStudioLLMProvider:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self.timeout_seconds = timeout_seconds or settings.LLM_REQUEST_TIMEOUT_SECONDS

    def _payload(
        self, messages: list[ChatMessage], *, max_tokens: int | None, stream: bool
    ) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens or settings.MAX_RESPONSE_TOKENS,
            "temperature": 0.1,  # low temperature: this is a grounded-QA task, not creative writing
        }
        if stream:
            payload["stream"] = True
        return payload

    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = self._payload(messages, max_tokens=max_tokens, stream=False)

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "llm_request_failed", status_code=exc.response.status_code, base_url=self.base_url
            )
            raise LLMProviderUnavailableError(
                f"LLM backend returned HTTP {exc.response.status_code}."
            ) from exc
        except httpx.TimeoutException as exc:
            # Must be caught before the generic httpx.HTTPError below -
            # TimeoutException is itself an HTTPError subclass, so ordering
            # here decides whether a slow-but-reachable backend is reported
            # as LLM_TIMEOUT (accurate) or LLM_UNAVAILABLE (misleading).
            logger.error(
                "llm_request_timeout",
                base_url=self.base_url,
                timeout_seconds=self.timeout_seconds,
            )
            raise LLMProviderTimeoutError(
                f"LLM backend at {self.base_url} did not respond within "
                f"{self.timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("llm_request_unreachable", base_url=self.base_url, exc_info=exc)
            raise LLMProviderUnavailableError(
                f"Could not reach LLM backend at {self.base_url}. "
                "Is LM Studio running with its local server started and a "
                "chat model loaded, and is the endpoint reachable from this "
                "process (use host.docker.internal, not localhost, when "
                "running in Docker)?"
            ) from exc

        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderResponseError(
                "LLM backend returned an unexpected response shape."
            ) from exc

    async def stream(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> AsyncGenerator[str, None]:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = self._payload(messages, max_tokens=max_tokens, stream=True)

        # Fresh client per call, same event-loop-safety reasoning as
        # generate() - this provider is used from both FastAPI (one loop)
        # and Celery/CLI contexts (a new loop per invocation), so caching a
        # client on the instance would eventually bind it to a dead loop.
        yielded_any = False
        try:
            async with (
                httpx.AsyncClient(timeout=self.timeout_seconds) as client,
                client.stream("POST", url, json=payload, headers=headers) as response,
            ):
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "llm_stream_request_failed",
                        status_code=exc.response.status_code,
                        base_url=self.base_url,
                    )
                    raise LLMProviderUnavailableError(
                        f"LLM backend returned HTTP {exc.response.status_code}."
                    ) from exc

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue  # blank keepalive lines, non-data SSE fields
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                        raise LLMProviderStreamInterruptedError(
                            "LLM backend sent a malformed stream chunk."
                        ) from exc
                    if delta:
                        yielded_any = True
                        yield delta
        except httpx.TimeoutException as exc:
            # Caught before the generic httpx.HTTPError below - same
            # ordering reason as generate(). A timeout after some tokens
            # were already yielded is a stream interruption (there's
            # partial output the caller must discard, not "never reached
            # the backend"); before any output, it's a plain timeout.
            if yielded_any:
                logger.error("llm_stream_interrupted_timeout", base_url=self.base_url, exc_info=exc)
                raise LLMProviderStreamInterruptedError(
                    "LLM backend timed out mid-generation."
                ) from exc
            logger.error(
                "llm_stream_timeout", base_url=self.base_url, timeout_seconds=self.timeout_seconds
            )
            raise LLMProviderTimeoutError(
                f"LLM backend at {self.base_url} did not respond within "
                f"{self.timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            if yielded_any:
                logger.error("llm_stream_interrupted", base_url=self.base_url, exc_info=exc)
                raise LLMProviderStreamInterruptedError(
                    "Connection to the LLM backend was lost mid-generation."
                ) from exc
            logger.error("llm_stream_unreachable", base_url=self.base_url, exc_info=exc)
            raise LLMProviderUnavailableError(
                f"Could not reach LLM backend at {self.base_url}. "
                "Is LM Studio running with its local server started and a "
                "chat model loaded, and is the endpoint reachable from this "
                "process (use host.docker.internal, not localhost, when "
                "running in Docker)?"
            ) from exc
