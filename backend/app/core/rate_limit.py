"""Redis fixed-window rate limiter.

A dedicated package (slowapi, etc.) wasn't worth the dependency for a single
INCR+EXPIRE pattern. Two identity strategies are provided:

- `rate_limit`: keyed by client IP + route name. Used for pre-auth or
  cheap-to-spoof-check endpoints (login, register, refresh). A real
  deployment behind a proxy would key off a trusted X-Forwarded-For instead,
  noted here rather than implemented since Nexus has no reverse proxy in
  front of it yet (added in Phase 11's Nginx config).
- `rate_limit_for_user`: keyed by authenticated user id + route name. Used
  for endpoints only reachable once logged in (upload, retry, chat,
  retrieval-debug) - an IP-keyed limit there would let one abusive org
  member exhaust the shared budget for every other user behind the same
  NAT/proxy, and would also let a user dodge the limit by rotating IPs.

Both are best-effort application-level limiting on a single Redis instance -
not a distributed-systems-grade limiter (no token bucket, no clock skew
handling across Redis replicas). That's an appropriate tradeoff for this
project's scale, not a claim of production-grade distributed rate limiting.
"""

import time
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, Request
from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import RateLimitedError
from app.core.metrics import (
    rate_limit_allowed_total,
    rate_limit_rejected_total,
    redis_errors_total,
    redis_operation_duration_seconds,
)


async def _check_and_increment(
    key_prefix: str, key: str, max_requests: int, window_seconds: int
) -> None:
    # Deliberately not a module-level singleton: a cached connection pool is
    # bound to the event loop that created it, which breaks under
    # pytest-asyncio's per-test event loops (and would equally break any
    # other multi-loop deployment). Redis.from_url() is cheap - it does not
    # eagerly open a socket, only the first command does.
    redis: Redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    start = time.perf_counter()
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window_seconds)
    except Exception:
        redis_errors_total.labels(operation="rate_limit_incr").inc()
        raise
    finally:
        redis_operation_duration_seconds.labels(operation="rate_limit_incr").observe(
            time.perf_counter() - start
        )
        await redis.aclose()
    if count > max_requests:
        rate_limit_rejected_total.labels(endpoint=key_prefix).inc()
        raise RateLimitedError(
            "Too many requests. Please try again later.", code="RATE_LIMITED"
        )
    rate_limit_allowed_total.labels(endpoint=key_prefix).inc()


def rate_limit(
    key_prefix: str, max_requests: int, window_seconds: int = 60
) -> Callable[[Request], Coroutine[Any, Any, None]]:
    async def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"ratelimit:{key_prefix}:ip:{client_ip}"
        await _check_and_increment(key_prefix, key, max_requests, window_seconds)

    return dependency


def rate_limit_for_user(
    key_prefix: str, max_requests: int, window_seconds: int = 60
) -> Callable[..., Coroutine[Any, Any, None]]:
    # Imported lazily inside the factory, not at module scope, to avoid a
    # circular import (app.api.v1.deps does not import this module, but
    # keeping the dependency direction one-way is worth the small ugliness).
    from app.api.v1.deps import get_current_user
    from app.models.user import User

    async def dependency(current_user: User = Depends(get_current_user)) -> None:
        key = f"ratelimit:{key_prefix}:user:{current_user.id}"
        await _check_and_increment(key_prefix, key, max_requests, window_seconds)

    return dependency
