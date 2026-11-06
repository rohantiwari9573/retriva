"""Redis fixed-window rate limiter with a per-process in-memory fallback.

A dedicated package (slowapi, etc.) wasn't worth the dependency for a single
counter pattern. Identity strategies:

- `rate_limit`: keyed by client IP + route name, for pre-auth endpoints (login,
  register, refresh). The IP comes from app/core/client_ip.py, which also explains
  which forwarding headers are trusted and why.
- `rate_limit_for_user`: keyed by authenticated user id + route name, for endpoints
  only reachable once logged in (chat, upload, retry, retrieval-debug). An IP-keyed
  limit there would let one member exhaust the budget for everyone behind the same
  NAT or proxy, and would let a user dodge the limit by rotating IPs.
- `enforce_login_account_limit` / `record_login_failure`: failed logins keyed by a
  hash of the submitted email, so guessing one account's password from many addresses
  slows down and then stops. Only failures count, so the owner's own logins are never
  what locks them out, and one or a few attacking addresses can't reach the cap.

Redis outage behaviour: a failed Redis call never fails the request. Each process
switches to its own bounded counters and keeps enforcing the same limits, then
returns to Redis as soon as a retry succeeds. While degraded the limit is per
process, not global: N API processes allow up to N times the limit, and counts
aren't carried across the switch in either direction, so a client can briefly get
up to twice its limit around a transition. If the fallback itself fails, requests
are rejected with 503 rather than run unlimited.

This is best-effort application-level limiting, not a distributed-systems-grade
limiter (no token bucket, no clock skew handling across Redis replicas). That's an
appropriate tradeoff for this project's scale, not a claim otherwise.
"""

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from typing import Any, cast

from fastapi import Depends, Request
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.core.client_ip import resolve_client_ip
from app.core.config import settings
from app.core.exceptions import RateLimitedError, RateLimiterUnavailableError
from app.core.logging import get_logger
from app.core.metrics import (
    rate_limit_allowed_total,
    rate_limit_fallback_active,
    rate_limit_fallback_requests_total,
    rate_limit_rejected_total,
    redis_errors_total,
    redis_operation_duration_seconds,
)

logger = get_logger(__name__)

# Atomic, so a crash can't leave a counter without an expiry, which would lock
# that client out permanently. Also repairs any such key from before this existed.
_INCREMENT_WITH_EXPIRY = """
local count = redis.call('INCR', KEYS[1])
if count == 1 or redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


class InMemoryRateLimiter:
    """Fixed-window counters for one process. Memory is bounded: at capacity it
    drops expired windows first, then the oldest, so a flood of distinct keys can
    only cost those clients their counts, never exhaust memory."""

    def __init__(self, max_keys: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._max_keys = max_keys
        self._clock = clock
        self._windows: OrderedDict[str, tuple[int, float]] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._windows)

    def hit(self, key: str, window_seconds: int) -> int:
        now = self._clock()
        count, expires_at = self._windows.get(key, (0, 0.0))
        if expires_at <= now:
            self._windows.pop(key, None)
            count, expires_at = 0, now + window_seconds
            self._make_room(now)
        self._windows[key] = (count + 1, expires_at)
        return count + 1

    def peek(self, key: str) -> int:
        count, expires_at = self._windows.get(key, (0, 0.0))
        return count if expires_at > self._clock() else 0

    def _make_room(self, now: float) -> None:
        if len(self._windows) < self._max_keys:
            return
        for key in [k for k, (_, expires_at) in self._windows.items() if expires_at <= now]:
            del self._windows[key]
        while len(self._windows) >= self._max_keys:
            self._windows.popitem(last=False)


_fallback = InMemoryRateLimiter(settings.RATE_LIMIT_FALLBACK_MAX_KEYS)
_fallback_is_active = False
_next_redis_attempt = 0.0


def fallback_active() -> bool:
    return _fallback_is_active


def reset_fallback_state() -> None:
    global _fallback, _fallback_is_active, _next_redis_attempt
    _fallback = InMemoryRateLimiter(settings.RATE_LIMIT_FALLBACK_MAX_KEYS)
    _fallback_is_active = False
    _next_redis_attempt = 0.0
    rate_limit_fallback_active.set(0)


async def _redis_increment(key: str, window_seconds: int) -> int:
    # Deliberately not a module-level singleton: a cached connection pool is bound
    # to the event loop that created it, which breaks under pytest-asyncio's
    # per-test event loops (and would equally break any other multi-loop
    # deployment). Redis.from_url() is cheap - it does not eagerly open a socket,
    # only the first command does. Retries are off and timeouts short because this
    # runs inside every limited request: the fallback is the retry strategy.
    redis: Redis = Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
        socket_timeout=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), 0),
    )
    start = time.perf_counter()
    try:
        return cast(
            int,
            await redis.eval(_INCREMENT_WITH_EXPIRY, 1, key, window_seconds),  # type: ignore[misc]
        )
    finally:
        redis_operation_duration_seconds.labels(operation="rate_limit_incr").observe(
            time.perf_counter() - start
        )
        await redis.aclose()


async def _redis_peek(key: str) -> int:
    redis: Redis = Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
        socket_timeout=settings.RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), 0),
    )
    try:
        return int(await redis.get(key) or 0)
    finally:
        await redis.aclose()


def _enter_fallback(exc: Exception) -> None:
    global _fallback_is_active, _next_redis_attempt
    redis_errors_total.labels(operation="rate_limit_incr").inc()
    _next_redis_attempt = time.monotonic() + settings.RATE_LIMIT_REDIS_RETRY_SECONDS
    if not _fallback_is_active:
        _fallback_is_active = True
        rate_limit_fallback_active.set(1)
        logger.warning("rate_limit_fallback_activated", reason=type(exc).__name__)


def _leave_fallback() -> None:
    global _fallback_is_active
    if _fallback_is_active:
        _fallback_is_active = False
        rate_limit_fallback_active.set(0)
        logger.info("rate_limit_fallback_deactivated")


async def _increment(key: str, window_seconds: int) -> int:
    if time.monotonic() >= _next_redis_attempt:
        try:
            count = await _redis_increment(key, window_seconds)
        except Exception as exc:  # noqa: BLE001 - a Redis failure must never fail the request
            _enter_fallback(exc)
        else:
            _leave_fallback()
            return count

    rate_limit_fallback_requests_total.inc()
    try:
        return _fallback.hit(key, window_seconds)
    except Exception as exc:
        logger.error("rate_limit_fallback_failed", exc_info=exc)
        raise RateLimiterUnavailableError(
            "This service is temporarily unavailable. Please try again shortly."
        ) from exc


async def _peek(key: str) -> int:
    if time.monotonic() >= _next_redis_attempt:
        try:
            count = await _redis_peek(key)
        except Exception as exc:  # noqa: BLE001 - a Redis failure must never fail the request
            _enter_fallback(exc)
        else:
            _leave_fallback()
            return count

    try:
        return _fallback.peek(key)
    except Exception as exc:
        logger.error("rate_limit_fallback_failed", exc_info=exc)
        raise RateLimiterUnavailableError(
            "This service is temporarily unavailable. Please try again shortly."
        ) from exc


async def _check_and_increment(
    key_prefix: str, key: str, max_requests: int, window_seconds: int
) -> None:
    count = await _increment(key, window_seconds)
    if count > max_requests:
        rate_limit_rejected_total.labels(endpoint=key_prefix).inc()
        raise RateLimitedError(
            "Too many requests. Please try again later.", code="RATE_LIMITED"
        )
    rate_limit_allowed_total.labels(endpoint=key_prefix).inc()


def _resolve_client_ip(request: Request) -> str:
    return resolve_client_ip(request)


def _login_failures_key(email: str) -> str:
    # Hashed so email addresses aren't stored in Redis or in memory.
    identity = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    return f"ratelimit:login:account_failures:{identity}"


async def enforce_login_account_limit(email: str) -> None:
    """Runs before the password is checked, for known and unknown emails alike, so
    the response never reveals whether an account exists or a guess was right."""
    failures = await _peek(_login_failures_key(email))
    if failures >= settings.RATE_LIMIT_LOGIN_ACCOUNT_FAILURES_PER_MINUTE:
        rate_limit_rejected_total.labels(endpoint="login_account").inc()
        raise RateLimitedError(
            "Too many requests. Please try again later.", code="RATE_LIMITED"
        )
    over_soft_limit = failures - settings.RATE_LIMIT_LOGIN_ACCOUNT_SOFT_FAILURES
    if over_soft_limit >= 0:
        delay = settings.RATE_LIMIT_LOGIN_ACCOUNT_DELAY_BASE_SECONDS * 2**over_soft_limit
        await asyncio.sleep(min(delay, settings.RATE_LIMIT_LOGIN_ACCOUNT_DELAY_MAX_SECONDS))


async def record_login_failure(email: str) -> None:
    await _increment(_login_failures_key(email), 60)


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
