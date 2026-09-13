"""GET /metrics - Prometheus scrape endpoint.

Kept unversioned and outside app.api.v1, same reasoning as app/api/health.py:
infra-facing, not part of the public API contract, and it must stay reachable
even if something in the v1 router's dependency graph is unhealthy.

Deployment note (not enforced in code): Prometheus scrape endpoints are
conventionally reachable only from the scraping network, not the public
internet - this project has no reverse proxy in front of it yet (see
docs/security.md), so exposing this unauthenticated on a real deployment
would rely entirely on network-level restriction (a firewall rule, a
Docker-internal-only network) rather than application auth. It carries no
per-tenant or per-user data (see the cardinality policy in
app/core/metrics.py), so an application-level auth check was judged
unnecessary complexity for what this project actually is - documented as a
production deployment responsibility in docs/observability.md rather than
silently assumed.
"""

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.config import settings

router = APIRouter(tags=["observability"])


@router.get("/metrics")
async def metrics() -> Response:
    if not settings.PROMETHEUS_ENABLED:
        return Response(status_code=404)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
