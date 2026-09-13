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
    APP_NAME: str = "Nexus"
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
    # "openai_compatible" works for OpenAI, LM Studio, or any OpenAI-compatible local server.
    LLM_PROVIDER: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    LLM_BASE_URL: str = "http://localhost:1234/v1"
    LLM_API_KEY: str = "not-needed-for-local"
    LLM_MODEL: str = "qwen2.5-7b-instruct"
    # Local CPU/GPU chat generation on a developer machine routinely takes
    # 30-120s, unlike the sub-second embedding calls above - a short timeout
    # here would misreport a working-but-slow LM Studio as "unavailable".
    LLM_REQUEST_TIMEOUT_SECONDS: int = 120

    EMBEDDING_PROVIDER: Literal["openai_compatible"] = "openai_compatible"
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
    VECTOR_SEARCH_WEIGHT: float = 0.7
    KEYWORD_SEARCH_WEIGHT: float = 0.3
    RRF_K: int = 60
    # Applied to raw cosine similarity (1 - cosine distance) of the single
    # best vector hit, not to the fused RRF score - RRF scores aren't on a
    # meaningful absolute scale. A heuristic, not a calibrated probability;
    # see docs/retrieval.md for why and its limitations.
    RETRIEVAL_MIN_SIMILARITY: float = 0.3
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
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 5
    RATE_LIMIT_REGISTER_PER_MINUTE: int = 3
    RATE_LIMIT_REFRESH_PER_MINUTE: int = 20
    RATE_LIMIT_CHAT_PER_MINUTE: int = 20
    RATE_LIMIT_UPLOAD_PER_MINUTE: int = 10
    RATE_LIMIT_RETRY_PER_MINUTE: int = 10
    RATE_LIMIT_RETRIEVAL_DEBUG_PER_MINUTE: int = 20

    # --- Observability ---
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "console"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
