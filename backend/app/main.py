"""FastAPI application entrypoint."""

import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.metrics import (
    http_request_duration_seconds,
    http_requests_in_flight,
    http_requests_total,
)
from app.core.request_id import resolve_request_id
from app.core.telemetry import (
    init_tracing,
    instrument_celery,
    instrument_fastapi_app,
    instrument_httpx,
    instrument_redis,
    instrument_sqlalchemy,
)
from app.ingestion.schema_check import (
    EmbeddingSchemaMismatchError,
    assert_embedding_dimension_matches,
)
from app.storage.s3 import get_s3_storage_provider

configure_logging()
init_tracing()
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

instrument_fastapi_app(app)
instrument_sqlalchemy(engine)
instrument_redis()
instrument_httpx()
instrument_celery()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _route_template(request: Request) -> str:
    """The full, cumulative path template for the matched route (e.g.
    "/api/v1/organizations/{organization_id}/documents/{document_id}"), for
    use as a bounded, low-cardinality metric/log label - never the concrete
    request path, which contains real UUIDs.

    FastAPI's `include_router` no longer flattens nested routers into
    individually-prefixed APIRoute objects at include time (recent FastAPI
    versions resolve routing hierarchically instead), so
    `request.scope["route"].path` only carries the leaf router's OWN local
    pattern (e.g. "/{organization_id}/documents", missing the
    "/api/v1/organizations" prefix) - not reliable across FastAPI versions.
    Reconstructing the template from the concrete path plus the path params
    FastAPI already resolved is robust regardless of that internal
    flattening behavior: every matched path parameter's value is replaced
    with its `{name}` placeholder in the real request path.
    """
    if request.scope.get("route") is None:
        return "unmatched"
    path = request.url.path
    for name, value in request.path_params.items():
        path = path.replace(str(value), f"{{{name}}}")
    return path


@app.middleware("http")
async def request_observability_middleware(request: Request, call_next):
    """Request ID + HTTP metrics + access log, in one place per Step 1's
    "prefer centralized instrumentation" - this is the single choke point
    every HTTP request passes through exactly once.

    Binds request_id via structlog.contextvars WITHOUT ever clearing it
    afterward - see app/core/logging.py's docstring and the Phase 7 note it
    replaced: BaseHTTPMiddleware's call_next() runs the rest of the request
    (including a StreamingResponse's generator body) in a child asyncio task
    created *after* this bind, so that task's context is a snapshot that
    already includes request_id - the generator can log with it correctly
    even though it executes after this function returns. A fresh HTTP
    request is always a fresh asyncio Task with its own context, so there is
    no cross-request leakage to clean up despite never clearing.
    """
    structlog.contextvars.clear_contextvars()  # defensive: no leftover state on this task
    request_id = resolve_request_id(request.headers.get("x-request-id"))
    structlog.contextvars.bind_contextvars(request_id=request_id)

    http_requests_in_flight.inc()
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        http_requests_in_flight.dec()
    duration_seconds = time.perf_counter() - start

    route_template = _route_template(request)

    http_requests_total.labels(
        method=request.method, route=route_template, status_code=str(response.status_code)
    ).inc()
    http_request_duration_seconds.labels(method=request.method, route=route_template).observe(
        duration_seconds
    )

    response.headers["X-Request-ID"] = request_id
    logger.info(
        "http_request",
        method=request.method,
        route=route_template,
        status_code=response.status_code,
        duration_ms=round(duration_seconds * 1000, 2),
    )
    return response


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
app.include_router(metrics_router)
app.include_router(api_router, prefix=settings.API_V1_PREFIX)
