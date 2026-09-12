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

_DbDependency = Depends(get_db)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/liveness")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readiness")
async def readiness(response: Response, db: AsyncSession = _DbDependency) -> dict[str, object]:
    checks: dict[str, str] = {}
    healthy = True

    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "unavailable"
        healthy = False

    try:
        redis_client: Redis = Redis.from_url(settings.REDIS_URL)
        await redis_client.ping()
        await redis_client.aclose()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "unavailable"
        healthy = False

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if healthy else "not_ready", "checks": checks}
