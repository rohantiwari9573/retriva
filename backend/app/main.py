"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("app_startup", environment=settings.ENVIRONMENT)
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

register_exception_handlers(app)

app.include_router(health_router)
app.include_router(api_router, prefix=settings.API_V1_PREFIX)
