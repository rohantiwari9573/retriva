"""Works out which client a per-IP rate limit should count a request against.

Requests can reach the API by different routes, and any forwarding header can be
forged unless something trusted set it. Sources, most specific first:

1. A signed assertion from our own edge proxy (frontend/src/proxy.ts on Vercel).
   All website traffic arrives from Vercel's addresses, so without this every
   visitor shares one identity. The HMAC proves our proxy set the value.
2. X-Real-IP, only when the TCP peer is listed in TRUSTED_PROXY_IPS (a reverse
   proxy on a known address, such as nginx in docker-compose.prod.yml).
3. TRUSTED_CLIENT_IP_HEADER, an opt-in for platforms whose edge always overwrites
   a header. Enable it only where that holds: on any other setup a client could
   set it to anything.
4. The TCP peer, which is the safe default.

Anything invalid falls through to the next source rather than being rejected, so
a forged header gains nothing and a misconfiguration can't block logins.
"""

import hmac
import ipaddress
import time
from hashlib import sha256

from fastapi import Request

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import client_ip_assertion_rejected_total

logger = get_logger(__name__)

VERIFIED_IP_HEADER = "x-retriva-client-ip"
TIMESTAMP_HEADER = "x-retriva-client-ip-timestamp"
SIGNATURE_HEADER = "x-retriva-client-ip-signature"

_IPV6_IDENTITY_PREFIX = 64


def _normalize_ip(value: str) -> str | None:
    """Canonical rate-limit identity, or None if value isn't a single IP address.

    IPv6 is reduced to its /64: one subscriber normally controls a whole /64, so
    limiting per address would be trivial to dodge.
    """
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        network = ipaddress.ip_network(f"{address}/{_IPV6_IDENTITY_PREFIX}", strict=False)
        return str(network)
    return str(address)


def _verified_assertion(request: Request) -> str | None:
    secret = settings.EDGE_PROXY_SHARED_SECRET
    if not secret:
        return None

    ip = request.headers.get(VERIFIED_IP_HEADER)
    if ip is None:
        return None
    timestamp = request.headers.get(TIMESTAMP_HEADER, "")
    signature = request.headers.get(SIGNATURE_HEADER, "")

    expected = hmac.new(secret.encode(), f"{timestamp}.{ip}".encode(), sha256).hexdigest()
    # Compared as bytes: compare_digest raises TypeError on non-ASCII str input,
    # and this value comes straight from a request header.
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        client_ip_assertion_rejected_total.labels(reason="bad_signature").inc()
        logger.warning("client_ip_assertion_rejected", reason="bad_signature")
        return None

    if not timestamp.isdecimal() or (
        abs(time.time() - int(timestamp)) > settings.EDGE_PROXY_MAX_CLOCK_SKEW_SECONDS
    ):
        client_ip_assertion_rejected_total.labels(reason="stale").inc()
        logger.warning("client_ip_assertion_rejected", reason="stale")
        return None

    normalized = _normalize_ip(ip)
    if normalized is None:
        client_ip_assertion_rejected_total.labels(reason="invalid_ip").inc()
    return normalized


def resolve_client_ip(request: Request) -> str:
    asserted = _verified_assertion(request)
    if asserted is not None:
        return asserted

    peer = request.client.host if request.client else None

    if peer is not None and peer in settings.TRUSTED_PROXY_IPS:
        real_ip = _normalize_ip(request.headers.get("x-real-ip", ""))
        if real_ip is not None:
            return real_ip

    if settings.TRUSTED_CLIENT_IP_HEADER:
        platform_ip = _normalize_ip(request.headers.get(settings.TRUSTED_CLIENT_IP_HEADER, ""))
        if platform_ip is not None:
            return platform_ip

    if peer is None:
        return "unknown"
    return _normalize_ip(peer) or peer
