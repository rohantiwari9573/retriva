"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.ingestion.schema_check import (
    EmbeddingSchemaMismatchError,
    assert_embedding_dimension_matches,
)
from app.storage.s3 import get_s3_storage_provider

configure_logging()
logger = get_logger(__name__)


def _check_production_config() -> None:
    """Fail loudly at startup if production is misconfigured, rather than let
    a subtle security gap (cookies sent over plain HTTP, an open CORS
    allowlist) go unnoticed until it's exploited. Deliberately a plain
    function called from lifespan, not a Pydantic model_validator on
    Settings - a field validator runs at import time in every context
    including the test suite and Alembic, where ENVIRONMENT is never
    "production" but would still be painful to have explode unexpectedly."""
    if settings.ENVIRONMENT != "production":
        return
    if not settings.COOKIE_SECURE:
        raise RuntimeError(
            "COOKIE_SECURE must be true in production - session cookies would "
            "otherwise be sent over plain HTTP."
        )
    if "*" in settings.CORS_ORIGINS:
        raise RuntimeError(
            "CORS_ORIGINS must not include '*' in production (combined with "
            "allow_credentials=True this would allow any site to make "
            "authenticated requests on a user's behalf)."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("app_startup", environment=settings.ENVIRONMENT)
    _check_production_config()
    get_s3_storage_provider().ensure_bucket_exists()
    try:
        async with AsyncSessionLocal() as session:
            await assert_embedding_dimension_matches(session, settings.EMBEDDING_DIMENSIONS)
    except EmbeddingSchemaMismatchError as exc:
        # Fail loudly at startup rather than at the first document upload -
        # a misconfigured EMBEDDING_DIMENSIONS would otherwise only surface
        # as an opaque Postgres error deep inside a Celery worker.
        logger.error("embedding_schema_mismatch_at_startup", exc_info=exc)
        raise
    yield
    logger.info("app_shutdown")


app = FastAPI(
    title=settings.APP_NAME,
    debug=settings.DEBUG,
    lifespan=lifespan,
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    # Nexus's frontend is a separate origin (never rendered inside this API's
    # own responses), so these are cheap, low-risk defense-in-depth headers
    # rather than a bespoke CSP tuned to page content - there is no page
    # content here, only JSON (and Swagger's /docs in non-production, which
    # a strict CSP would break).
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if settings.ENVIRONMENT == "production":
        # Only meaningful (and only safe to assert) once the deployment is
        # actually served over HTTPS - asserting it in local dev would just
        # be a no-op at best and a confusing lie at worst.
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


register_exception_handlers(app)

app.include_router(health_router)
app.include_router(api_router, prefix=settings.API_V1_PREFIX)
