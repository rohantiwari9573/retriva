"""Tests for app.core.rate_limit._resolve_client_ip - the trusted-proxy IP
resolution added to fix rate limiting behind nginx on the AWS deployment
(request.client.host was nginx's own connection there, not the real
visitor - see rate_limit.py's module docstring for the full story).
"""

from unittest.mock import MagicMock

from app.core.config import settings
from app.core.rate_limit import _resolve_client_ip


def _make_request(peer_ip: str | None, headers: dict[str, str] | None = None) -> MagicMock:
    request = MagicMock()
    request.client = MagicMock(host=peer_ip) if peer_ip is not None else None
    request.headers = headers or {}
    return request


def test_no_trusted_proxies_uses_direct_peer(monkeypatch):
    """Default (empty TRUSTED_PROXY_IPS) - the exact pre-existing behavior,
    for local dev and CI where nothing sits in front of the app."""
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", [])
    request = _make_request("203.0.113.7", headers={"x-real-ip": "10.0.0.99"})
    assert _resolve_client_ip(request) == "203.0.113.7"


def test_trusted_proxy_with_real_ip_header_is_used(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", ["172.28.0.10"])
    request = _make_request("172.28.0.10", headers={"x-real-ip": "198.51.100.42"})
    assert _resolve_client_ip(request) == "198.51.100.42"


def test_untrusted_peer_cannot_spoof_real_ip_header(monkeypatch):
    """The core spoofing-prevention case: a direct, untrusted connection
    supplying X-Real-IP itself must NOT have that header honored - only the
    configured trusted proxy's own connection can supply it."""
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", ["172.28.0.10"])
    request = _make_request("198.51.100.1", headers={"x-real-ip": "1.2.3.4"})
    assert _resolve_client_ip(request) == "198.51.100.1"


def test_trusted_proxy_without_real_ip_header_falls_back_safely(monkeypatch):
    """A trusted proxy that (misconfigured, or a non-app request) doesn't
    send X-Real-IP must not crash or resolve to a spoofable default - it
    falls back to the direct peer (the proxy's own address)."""
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", ["172.28.0.10"])
    request = _make_request("172.28.0.10", headers={})
    assert _resolve_client_ip(request) == "172.28.0.10"


def test_no_client_at_all_resolves_to_unknown(monkeypatch):
    """request.client can be None (e.g. some ASGI test transports) - must
    not raise."""
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", [])
    request = _make_request(None)
    assert _resolve_client_ip(request) == "unknown"


def test_forwarded_for_header_is_never_consulted(monkeypatch):
    """X-Forwarded-For is deliberately never read here (see the module
    docstring for why it's spoofable via client-side prepending) - even a
    trusted proxy's X-Forwarded-For must have no effect, only X-Real-IP."""
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", ["172.28.0.10"])
    request = _make_request(
        "172.28.0.10",
        headers={"x-forwarded-for": "9.9.9.9", "x-real-ip": "198.51.100.42"},
    )
    assert _resolve_client_ip(request) == "198.51.100.42"
