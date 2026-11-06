"""Liveness/readiness/health endpoints.

Kept unversioned (not under /api/v1) since these are infra-facing, not part of the
public API contract. Readiness checks actual dependencies; liveness does not (a slow
Postgres shouldn't make an orchestrator kill and restart an otherwise-healthy process).
"""

from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/liveness")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readiness")
async def readiness(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    checks: dict[str, str] = {}
    healthy = True
    degraded: list[str] = []

    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "unavailable"
        healthy = False

    # Redis being down degrades this instance but doesn't make it unready: rate
    # limiting falls back to per-process counters and auth keeps working, whereas
    # pulling every instance out of rotation would turn a Redis outage into a
    # full one. Celery-mode uploads do need the broker and report that themselves.
    redis_client: Redis = Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
        socket_timeout=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
    )
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "unavailable"
        degraded.append("redis")
    finally:
        await redis_client.aclose()

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if healthy else "not_ready",
        "checks": checks,
        "degraded": degraded,
    }
