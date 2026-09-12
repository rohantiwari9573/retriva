"""Redis fixed-window rate limiter.

A dedicated package (slowapi, etc.) wasn't worth the dependency for a single
INCR+EXPIRE pattern. Keyed by client IP + route name; a real deployment behind
a proxy would key off a trusted X-Forwarded-For instead, noted here rather than
implemented since Nexus has no reverse proxy in front of it yet (added in
Phase 11's Nginx config).
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request
from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import RateLimitedError


def rate_limit(
    key_prefix: str, max_requests: int, window_seconds: int = 60
) -> Callable[[Request], Coroutine[Any, Any, None]]:
    async def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"ratelimit:{key_prefix}:{client_ip}"
        # Deliberately not a module-level singleton: a cached connection pool
        # is bound to the event loop that created it, which breaks under
        # pytest-asyncio's per-test event loops (and would equally break any
        # other multi-loop deployment). Redis.from_url() is cheap - it does
        # not eagerly open a socket, only the first command does.
        redis: Redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, window_seconds)
        finally:
            await redis.aclose()
        if count > max_requests:
            raise RateLimitedError(
                "Too many requests. Please try again later.", code="RATE_LIMITED"
            )

    return dependency
