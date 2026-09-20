"""Centralized application configuration.

All environment variables must be read here, never via scattered os.getenv() calls
elsewhere in the codebase. Import `settings` from this module wherever config is needed.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    ENVIRONMENT: Literal["development", "testing", "production"] = "development"
    DEBUG: bool = True
    APP_NAME: str = "Retriva"
    API_V1_PREFIX: str = "/api/v1"

    # --- Security ---
    JWT_SECRET: str = Field(..., min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    COOKIE_SECURE: bool = False  # must be True in production (HTTPS only)
    COOKIE_DOMAIN: str | None = None

    # --- CORS ---
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # --- Database ---
    DATABASE_URL: str = Field(...)
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Celery ---
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # --- Object storage ---
    STORAGE_PROVIDER: Literal["minio", "s3"] = "minio"
    # Internal, container-to-container endpoint; None for real AWS S3.
    S3_ENDPOINT_URL: str | None = "http://minio:9000"
    # Presigned URLs are signed against the endpoint used to reach the bucket -
    # inside Docker that's "http://minio:9000", which the user's browser can't
    # resolve. This is the endpoint baked into URLs handed to the frontend.
    # Defaults to S3_ENDPOINT_URL when unset (fine for real S3/production).
    S3_PUBLIC_ENDPOINT_URL: str | None = "http://localhost:9000"
    S3_BUCKET: str = "nexus-documents"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    AWS_REGION: str = "us-east-1"
    DOWNLOAD_URL_EXPIRE_SECONDS: int = 300

    # --- Document upload ---
    ALLOWED_DOCUMENT_EXTENSIONS: list[str] = [".pdf", ".docx", ".txt", ".md"]
    DOCUMENTS_PAGE_SIZE_DEFAULT: int = 20
    DOCUMENTS_PAGE_SIZE_MAX: int = 100

    # --- LLM / Embedding / Reranker providers ---
    # "openai_compatible" works for OpenAI, LM Studio, or any OpenAI-compatible
    # local or remote server - including Google Gemini's own OpenAI-compatible
    # endpoint (https://generativelanguage.googleapis.com/v1beta/openai). No
    # separate "gemini" LLM_PROVIDER value exists: Gemini's chat/completions
    # and streaming wire format is standard OpenAI-compatible, so
    # LMStudioLLMProvider (a generic client despite its name - see its own
    # docstring) already works against it with no code change, just
    # different LLM_BASE_URL/LLM_API_KEY/LLM_MODEL values. See
    # docs/gemini-provider.md for the exact local-vs-cloud configuration.
    LLM_PROVIDER: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    LLM_BASE_URL: str = "http://localhost:1234/v1"
    LLM_API_KEY: str = "not-needed-for-local"
    LLM_MODEL: str = "qwen2.5-7b-instruct"
    # Local CPU/GPU chat generation on a developer machine routinely takes
    # 30-120s, unlike the sub-second embedding calls above - a short timeout
    # here would misreport a working-but-slow LM Studio as "unavailable".
    LLM_REQUEST_TIMEOUT_SECONDS: int = 120

    # --- LLM streaming retry ---
    # Automatic retry is exclusively for transient provider failures (HTTP
    # 429/503, timeouts, a dropped connection) - never for an auth/bad-
    # request/malformed-response error, and never for a legitimate
    # insufficient-evidence answer, which isn't a provider failure at all.
    # See RAGService._answer_stream_impl's classification.
    LLM_STREAM_MAX_RETRIES: int = 2  # + the initial attempt = 3 total attempts
    LLM_STREAM_RETRY_DELAYS_SECONDS: tuple[float, ...] = (2.0, 5.0)
    # Caps how long a provider's Retry-After header is allowed to make a
    # single retry wait - without this, a provider reporting a long
    # Retry-After (e.g. a daily quota reset) would hang the request path
    # for far longer than a chat request should ever take, rather than
    # falling through to the existing interrupted/error UI quickly.
    LLM_STREAM_RETRY_MAX_DELAY_SECONDS: float = 10.0

    # Embeddings DO need a distinct "gemini" provider value, unlike LLM_PROVIDER
    # above: Gemini's embeddings need the `dimensions` request field (to get
    # 768-wide output instead of the model's native 3072) and manual L2
    # normalization of the truncated result - genuinely different code, not
    # just different config values. See app/rag/embedding/gemini.py.
    EMBEDDING_PROVIDER: Literal["openai_compatible", "gemini"] = "openai_compatible"
    EMBEDDING_BASE_URL: str = "http://localhost:1234/v1"
    EMBEDDING_API_KEY: str = "not-needed-for-local"
    EMBEDDING_MODEL: str = "nomic-embed-text"
    EMBEDDING_DIMENSIONS: int = 768
    EMBEDDING_BATCH_SIZE: int = 32
    EMBEDDING_REQUEST_TIMEOUT_SECONDS: int = 30

    RERANKER_PROVIDER: Literal["none", "cohere", "local_cross_encoder"] = "none"

    # --- RAG limits / cost control ---
    MAX_DOCUMENT_SIZE_MB: int = 25
    MAX_CHUNKS_PER_DOCUMENT: int = 2000
    # Parser-level DoS guards (Phase 7): a file can pass the byte-size cap
    # above and still be pathological once decoded (a 25MB PDF can still have
    # tens of thousands of pages; a small DOCX can zip-bomb into gigabytes of
    # XML). These bound the *decoded* shape of a document, not its on-disk
    # size, which the byte cap already covers.
    MAX_DOCUMENT_PAGES: int = 500
    MAX_DOCUMENT_TEXT_LENGTH: int = 5_000_000
    MAX_DOCX_UNCOMPRESSED_SIZE_BYTES: int = 100_000_000
    RETRIEVAL_TOP_K: int = 8
    RETRIEVAL_CANDIDATE_POOL: int = 30
    MAX_CONTEXT_TOKENS: int = 3000
    MAX_RESPONSE_TOKENS: int = 1024
    CHUNK_SIZE_TOKENS: int = 500
    CHUNK_OVERLAP_TOKENS: int = 50

    # --- Document processing (Celery) ---
    DOCUMENT_PROCESSING_MAX_RETRIES: int = 3
    DOCUMENT_PROCESSING_RETRY_BACKOFF_SECONDS: int = 10

    # --- Hybrid retrieval (Phase 5) ---
    # Fusion is Reciprocal Rank Fusion (RRF), not a weighted sum of raw scores -
    # vector cosine similarity and Postgres ts_rank live on incompatible
    # scales, so combining them by rank rather than by (mis-normalized) value
    # is the mathematically defensible choice. See docs/retrieval.md.
    #
    # Weights swapped from the original 0.7/0.3 (vector-favored) split after
    # the first real (non-deterministic-fake) embedding baseline - see
    # docs/evaluation-baseline.md's "RRF weight experiment" - showed
    # keyword-only beating vector-only on this corpus (Recall@5 17.14% vs
    # 8.57%, 35-case dataset v1) while the RRF fusion, weighted toward the
    # *weaker* signal, actually lost to keyword-only alone (14.29% vs
    # 17.14%). Re-running the identical evaluation with these weights
    # (0.3/0.7) raised hybrid Recall@5 to 22.86%, beating keyword-only
    # outright, with vector-only/keyword-only unchanged (confirming this was
    # the only variable that moved). A single controlled experiment on one
    # small corpus - re-tune if a larger/different corpus shows the opposite
    # pattern, not a universal claim that keyword should always dominate.
    VECTOR_SEARCH_WEIGHT: float = 0.3
    KEYWORD_SEARCH_WEIGHT: float = 0.7
    RRF_K: int = 60
    # Applied to raw cosine similarity (1 - cosine distance) of the single
    # best vector hit, not to the fused RRF score - RRF scores aren't on a
    # meaningful absolute scale. A heuristic, not a calibrated probability;
    # see docs/retrieval.md for why and its limitations.
    #
    # Lowered from an untested 0.3 after the real evaluation baseline
    # showed it was blocking generation in every single case: real
    # nomic-embed-text cosine similarities on this corpus range roughly
    # 0.06-0.13 (see docs/evaluation-baseline.md's "RETRIEVAL_MIN_SIMILARITY
    # re-tune" section) - 0.3 was never reachable. A diagnostic run also
    # found this raw score barely distinguishes cases where the correct
    # document IS retrieved from cases where it isn't (overlapping
    # ranges) - so this floor is a sanity check against near-zero noise,
    # not a precision filter; RRF ranking (see VECTOR_SEARCH_WEIGHT above)
    # does the real relevance work.
    #
    # First tried 0.05 (comfortably below the observed floor): this fixed
    # generation (correctness 0.0->1.16/3, citation validity 0%->100%) but
    # caused a real regression - unanswerable-refusal accuracy dropped
    # 100%->40% and prompt-injection resistance dropped 100%->33% (2/3
    # canaries leaked), because the model was now engaging with retrieved
    # content on cases that should have been refused. 0.09 was chosen
    # instead because the real per-case data showed every unanswerable/
    # negative-query case topped out at 0.087 - a threshold just above
    # that floor lets genuinely relevant retrievals through while
    # continuing to block most cases with no real answer. Validated with
    # the full 66-case evaluation, not just a targeted sample: correctness
    # 0.0->0.6/3, faithfulness 0.0->0.56/3, citation validity 0%->100%,
    # unanswerable-refusal 100%->90% (9/10, one slip), injection
    # resistance 100%->67% (2/3 resisted). The one case that both leaked
    # its injection canary AND answered when it should have refused
    # (qa-035) is the same case in both failures - it embeds a genuinely
    # answerable sub-question ("what database does Retriva use?") inside
    # the injection attempt, so it clears the threshold on legitimate
    # grounds and the injected instruction rides along. 0.05 scored higher
    # on correctness/faithfulness (1.16/1.04) but regressed refusal/
    # injection resistance much further (40%/33%) - 0.09 was kept as the
    # better overall balance. See docs/evaluation-baseline.md for the full
    # three-way (0.3 / 0.05 / 0.09) before/after comparison and per-case
    # evidence.
    RETRIEVAL_MIN_SIMILARITY: float = 0.09
    MAX_CONTEXT_CHUNKS: int = 6
    CONVERSATION_HISTORY_MAX_MESSAGES: int = 6

    # --- Conversational RAG (Phase 6) ---
    # Query rewriting is an optimization on top of retrieval, never its
    # source of truth - if it's disabled, times out, or fails, the original
    # question is used for retrieval unchanged. See app/rag/query_rewrite/.
    QUERY_REWRITE_ENABLED: bool = True
    # Deliberately much shorter than LLM_REQUEST_TIMEOUT_SECONDS: a slow
    # rewrite should fall back to the original query almost immediately,
    # not make the user wait through the same 120s budget as the real
    # answer generation before retrieval even starts.
    QUERY_REWRITE_TIMEOUT_SECONDS: float = 10.0
    QUERY_REWRITE_MAX_TOKENS: int = 100
    STREAMING_ENABLED: bool = True

    # --- Rate limiting (requests per window per identity) ---
    # Login/register/chat/upload are keyed by client IP (pre-auth or cheap to
    # spoof-check); retry/regenerate/retrieval-debug are keyed by
    # authenticated user, since they're only reachable once logged in and an
    # IP-keyed limit would let one abusive org member exhaust the budget for
    # every other user behind the same NAT/proxy. See app/core/rate_limit.py.
    #
    # TRUSTED_PROXY_IPS: direct TCP peer addresses allowed to supply the real
    # client IP via X-Real-IP (never X-Forwarded-For - see rate_limit.py's
    # module docstring for why). Empty by default, which preserves exact
    # existing behavior (key off request.client.host directly) for local dev
    # and CI, where nothing sits in front of the app. Only the AWS
    # deployment's nginx container - given a fixed IP in
    # docker-compose.prod.yml specifically so this list can name it - should
    # ever be listed here.
    TRUSTED_PROXY_IPS: list[str] = []
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 5
    RATE_LIMIT_REGISTER_PER_MINUTE: int = 3
    RATE_LIMIT_REFRESH_PER_MINUTE: int = 20
    RATE_LIMIT_CHAT_PER_MINUTE: int = 20
    RATE_LIMIT_UPLOAD_PER_MINUTE: int = 10
    RATE_LIMIT_RETRY_PER_MINUTE: int = 10
    RATE_LIMIT_RETRIEVAL_DEBUG_PER_MINUTE: int = 20

    # --- Observability (Phase 8) ---
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "console"
    # Master switches - both default on for local dev, and both fail open:
    # if the underlying exporter/library can't reach its backend, telemetry
    # is dropped, never allowed to break a request. See docs/observability.md
    # "Failure isolation".
    PROMETHEUS_ENABLED: bool = True
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = "nexus-backend"
    # OTLP/gRPC endpoint - Jaeger's all-in-one image accepts OTLP natively
    # since 1.35, so no separate OpenTelemetry Collector is required (see
    # docs/observability.md for why one was evaluated and not added).
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://jaeger:4317"
    # parentbased_traceidratio + arg=1.0 means "sample everything unless a
    # parent span already decided otherwise" - the right default for local
    # dev, where full visibility matters more than reducing trace volume.
    OTEL_TRACES_SAMPLER: Literal[
        "always_on", "always_off", "traceidratio", "parentbased_traceidratio"
    ] = "parentbased_traceidratio"
    OTEL_TRACES_SAMPLER_ARG: float = 1.0
    # The Celery worker has no HTTP server of its own (see docker-compose.yml's
    # healthcheck comment) - this opens a small dedicated prometheus_client
    # HTTP server inside the worker process purely so celery_*/
    # document_processing_* metrics have somewhere to be scraped from. See
    # docs/observability.md "Celery instrumentation" for the worker
    # concurrency tradeoff this implies.
    WORKER_METRICS_PORT: int = 9808


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
