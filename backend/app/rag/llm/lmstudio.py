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

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.rag.llm.base import ChatMessage, LLMProviderResponseError, LLMProviderUnavailableError

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

    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens or settings.MAX_RESPONSE_TOKENS,
            "temperature": 0.1,  # low temperature: this is a grounded-QA task, not creative writing
        }

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
