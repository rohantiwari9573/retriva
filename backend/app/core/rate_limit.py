"""Redis fixed-window rate limiter.

A dedicated package (slowapi, etc.) wasn't worth the dependency for a single
INCR+EXPIRE pattern. Two identity strategies are provided:

- `rate_limit`: keyed by client IP + route name. Used for pre-auth or
  cheap-to-spoof-check endpoints (login, register, refresh). Resolves the
  identity via `_resolve_client_ip()` below - `request.client.host`
  directly unless that peer is a configured trusted proxy
  (`settings.TRUSTED_PROXY_IPS`), in which case the proxy's own
  `X-Real-IP` header is used instead. This is deliberately X-Real-IP, not
  X-Forwarded-For: nginx's `proxy_set_header X-Real-IP $remote_addr;`
  *overwrites* any client-supplied value, so it can't be spoofed, whereas
  `proxy_add_x_forwarded_for` *appends* to whatever X-Forwarded-For value
  the client already sent - a client could set
  `X-Forwarded-For: 1.2.3.4` themselves and have nginx append its own
  address after it, and naively trusting "the first entry" would then
  trust the attacker-supplied value. With no trusted proxy configured
  (the default - local dev, CI), behavior is identical to before this was
  added.
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


def _resolve_client_ip(request: Request) -> str:
    """The identity a per-IP rate limit keys on.

    Trusts X-Real-IP only when the direct TCP peer is in
    settings.TRUSTED_PROXY_IPS - an untrusted or absent peer always falls
    back to request.client.host, exactly the pre-existing behavior. This
    ordering matters: checking the trusted-peer condition first means an
    attacker connecting directly (not through the real proxy) can supply
    any X-Real-IP they like and it's simply never consulted.
    """
    direct_peer = request.client.host if request.client else None
    if direct_peer is not None and direct_peer in settings.TRUSTED_PROXY_IPS:
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
    return direct_peer or "unknown"


def rate_limit(
    key_prefix: str, max_requests: int, window_seconds: int = 60
) -> Callable[[Request], Coroutine[Any, Any, None]]:
    async def dependency(request: Request) -> None:
        client_ip = _resolve_client_ip(request)
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
