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
    S3_ENDPOINT_URL: str | None = "http://localhost:9000"  # None for real AWS S3
    S3_BUCKET: str = "nexus-documents"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    AWS_REGION: str = "us-east-1"

    # --- LLM / Embedding / Reranker providers ---
    # "openai_compatible" works for OpenAI, LM Studio, or any OpenAI-compatible local server.
    LLM_PROVIDER: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    LLM_BASE_URL: str = "http://localhost:1234/v1"
    LLM_API_KEY: str = "not-needed-for-local"
    LLM_MODEL: str = "qwen2.5-7b-instruct"

    EMBEDDING_PROVIDER: Literal["openai_compatible"] = "openai_compatible"
    EMBEDDING_BASE_URL: str = "http://localhost:1234/v1"
    EMBEDDING_API_KEY: str = "not-needed-for-local"
    EMBEDDING_MODEL: str = "nomic-embed-text"
    EMBEDDING_DIMENSIONS: int = 768

    RERANKER_PROVIDER: Literal["none", "cohere", "local_cross_encoder"] = "none"

    # --- RAG limits / cost control ---
    MAX_DOCUMENT_SIZE_MB: int = 25
    MAX_CHUNKS_PER_DOCUMENT: int = 2000
    RETRIEVAL_TOP_K: int = 8
    RETRIEVAL_CANDIDATE_POOL: int = 30
    MAX_CONTEXT_TOKENS: int = 3000
    MAX_RESPONSE_TOKENS: int = 1024
    CHUNK_SIZE_TOKENS: int = 500
    CHUNK_OVERLAP_TOKENS: int = 50

    # --- Rate limiting (requests per window per identity) ---
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 5
    RATE_LIMIT_REGISTER_PER_MINUTE: int = 3
    RATE_LIMIT_CHAT_PER_MINUTE: int = 20
    RATE_LIMIT_UPLOAD_PER_MINUTE: int = 10

    # --- Observability ---
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "console"
    ENABLE_METRICS: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
