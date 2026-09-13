"""Request correlation IDs - distinct from OpenTelemetry trace IDs.

REQUEST ID vs. TRACE ID (see docs/observability.md for the full writeup):
- `request_id` is an application-level correlation identifier: returned to
  the client as `X-Request-ID`, safe to hand back in a support ticket, and
  present on every log line for that request. It exists independently of
  whether tracing is enabled at all.
- `trace_id` is OpenTelemetry's distributed-tracing identifier, used to look
  up the request's spans in Jaeger. It only exists when OTEL_ENABLED=true
  and a span was actually created.
They are never the same value and must not be conflated - a client-supplied
request_id, for instance, must never be trusted as a trace_id or used to
look anything up in the tracing backend.

A client MAY supply its own X-Request-ID (useful for a frontend that wants
to correlate its own error UI with a specific backend log line before the
response even comes back); this module validates and bounds that value
rather than trusting it outright, since it becomes a log field and a
response header value: unbounded length is a memory/log-volume concern, and
control characters in a header value are a header-injection concern.
"""

import re
import secrets

_MAX_REQUEST_ID_LENGTH = 128
# Conservative allowlist: ASCII letters, digits, and -._~ (a subset of RFC
# 3986 unreserved characters) - enough for a UUID, a ULID, or a short opaque
# token, and never a value that could smuggle a CR/LF/extra header into the
# response.
_VALID_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def generate_request_id() -> str:
    return secrets.token_urlsafe(18)  # 24 chars, URL-safe, no padding


def resolve_request_id(client_supplied: str | None) -> str:
    """Returns a safe request ID to use for this request: the client's own
    value if it passes validation, otherwise a freshly generated one. Never
    raises - an invalid client-supplied ID is silently replaced, not treated
    as a client error, since X-Request-ID is a diagnostic convenience, not a
    contract the client can violate."""
    if (
        client_supplied
        and len(client_supplied) <= _MAX_REQUEST_ID_LENGTH
        and _VALID_REQUEST_ID_RE.match(client_supplied)
    ):
        return client_supplied
    return generate_request_id()
