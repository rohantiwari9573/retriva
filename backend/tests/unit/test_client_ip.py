"""R0.3: who a per-IP rate limit is about, and who is allowed to say so."""

import hmac
import time
from hashlib import sha256
from unittest.mock import MagicMock

import pytest

from app.core.client_ip import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    VERIFIED_IP_HEADER,
    resolve_client_ip,
)
from app.core.config import settings

SECRET = "unit-test-shared-secret-at-least-32-chars"


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", [])
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", "")
    monkeypatch.setattr(settings, "TRUSTED_CLIENT_IP_HEADER", "")


def _request(peer: str | None, headers: dict[str, str] | None = None) -> MagicMock:
    request = MagicMock()
    request.client = MagicMock(host=peer) if peer is not None else None
    request.headers = headers or {}
    return request


def _signed(ip: str, *, secret: str = SECRET, timestamp: int | None = None) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = hmac.new(secret.encode(), f"{stamp}.{ip}".encode(), sha256).hexdigest()
    return {VERIFIED_IP_HEADER: ip, TIMESTAMP_HEADER: stamp, SIGNATURE_HEADER: signature}


# --- signed assertion from the edge proxy ---------------------------------


def test_valid_signed_assertion_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    assert resolve_client_ip(_request("10.0.0.1", _signed("198.51.100.7"))) == "198.51.100.7"


def test_two_visitors_behind_one_proxy_get_separate_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported bug: every website visitor arrived as Vercel's address."""
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    first = resolve_client_ip(_request("76.76.21.21", _signed("198.51.100.7")))
    second = resolve_client_ip(_request("76.76.21.21", _signed("203.0.113.9")))
    assert first != second


def test_assertion_is_ignored_when_no_secret_is_configured() -> None:
    assert resolve_client_ip(_request("10.0.0.1", _signed("198.51.100.7"))) == "10.0.0.1"


def test_forged_signature_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    forged = _signed("198.51.100.7", secret="attacker-guess")
    assert resolve_client_ip(_request("10.0.0.1", forged)) == "10.0.0.1"


def test_unsigned_header_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    assert resolve_client_ip(_request("10.0.0.1", {VERIFIED_IP_HEADER: "1.2.3.4"})) == "10.0.0.1"


def test_signature_cannot_be_moved_to_another_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    headers = {**_signed("198.51.100.7"), VERIFIED_IP_HEADER: "1.2.3.4"}
    assert resolve_client_ip(_request("10.0.0.1", headers)) == "10.0.0.1"


@pytest.mark.parametrize("age", [-3600, 3600])
def test_stale_or_future_assertion_is_rejected(
    monkeypatch: pytest.MonkeyPatch, age: int
) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    headers = _signed("198.51.100.7", timestamp=int(time.time()) + age)
    assert resolve_client_ip(_request("10.0.0.1", headers)) == "10.0.0.1"


@pytest.mark.parametrize("timestamp", ["", "soon", "1e9"])
def test_malformed_timestamp_is_rejected(monkeypatch: pytest.MonkeyPatch, timestamp: str) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    headers = {**_signed("198.51.100.7"), TIMESTAMP_HEADER: timestamp}
    assert resolve_client_ip(_request("10.0.0.1", headers)) == "10.0.0.1"


def test_validly_signed_garbage_ip_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    assert resolve_client_ip(_request("10.0.0.1", _signed("not-an-ip"))) == "10.0.0.1"


def test_non_ascii_signature_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    headers = {**_signed("198.51.100.7"), SIGNATURE_HEADER: "é" * 64}
    assert resolve_client_ip(_request("10.0.0.1", headers)) == "10.0.0.1"


# --- forwarding headers are never trusted by default ----------------------


@pytest.mark.parametrize(
    "header",
    [
        "x-forwarded-for",
        "x-real-ip",
        "true-client-ip",
        "cf-connecting-ip",
        "x-vercel-forwarded-for",
    ],
)
def test_client_supplied_forwarding_headers_are_ignored_by_default(header: str) -> None:
    assert resolve_client_ip(_request("203.0.113.7", {header: "1.2.3.4"})) == "203.0.113.7"


def test_configured_platform_header_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_CLIENT_IP_HEADER", "true-client-ip")
    request = _request("10.0.0.1", {"true-client-ip": "198.51.100.7"})
    assert resolve_client_ip(request) == "198.51.100.7"


def test_garbage_in_configured_platform_header_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "TRUSTED_CLIENT_IP_HEADER", "true-client-ip")
    request = _request("10.0.0.1", {"true-client-ip": "1.2.3.4, 5.6.7.8"})
    assert resolve_client_ip(request) == "10.0.0.1"


def test_signed_assertion_outranks_platform_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Via Vercel, the platform header holds Vercel's address, not the visitor's."""
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", SECRET)
    monkeypatch.setattr(settings, "TRUSTED_CLIENT_IP_HEADER", "true-client-ip")
    headers = {**_signed("198.51.100.7"), "true-client-ip": "76.76.21.21"}
    assert resolve_client_ip(_request("10.0.0.1", headers)) == "198.51.100.7"


# --- trusted reverse proxy on a known address (existing behaviour) --------


def test_trusted_peer_real_ip_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", ["172.28.0.10"])
    assert (
        resolve_client_ip(_request("172.28.0.10", {"x-real-ip": "198.51.100.42"}))
        == "198.51.100.42"
    )
    assert resolve_client_ip(_request("172.28.0.10", {"x-real-ip": "evil\r\n"})) == "172.28.0.10"


# --- normalization --------------------------------------------------------


def test_ipv6_clients_share_an_identity_per_slash_64(monkeypatch: pytest.MonkeyPatch) -> None:
    """One subscriber controls a whole /64, so per-address limits are trivial to dodge."""
    monkeypatch.setattr(settings, "TRUSTED_CLIENT_IP_HEADER", "true-client-ip")
    first = resolve_client_ip(_request("10.0.0.1", {"true-client-ip": "2001:db8:1:2::aaaa"}))
    second = resolve_client_ip(_request("10.0.0.1", {"true-client-ip": "2001:db8:1:2::bbbb"}))
    other = resolve_client_ip(_request("10.0.0.1", {"true-client-ip": "2001:db8:1:3::aaaa"}))
    assert first == second
    assert first != other


def test_ipv4_mapped_ipv6_is_the_same_client_as_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRUSTED_CLIENT_IP_HEADER", "true-client-ip")
    request = _request("10.0.0.1", {"true-client-ip": "::ffff:198.51.100.7"})
    assert resolve_client_ip(request) == "198.51.100.7"


def test_no_peer_at_all_has_a_stable_identity() -> None:
    assert resolve_client_ip(_request(None)) == "unknown"


def test_signature_vector_matches_the_frontend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same vector as frontend/src/lib/client-ip-assertion.test.ts, verified by the
    production code path, so a change to the backend's message format fails here
    even if this file's _signed helper were changed to match it."""
    monkeypatch.setattr(settings, "EDGE_PROXY_SHARED_SECRET", "contract-test-secret")
    monkeypatch.setattr("app.core.client_ip.time.time", lambda: 1700000000.0)
    headers = {
        VERIFIED_IP_HEADER: "198.51.100.7",
        TIMESTAMP_HEADER: "1700000000",
        SIGNATURE_HEADER: "434f9f400f62952a6a60d90a2a5f1c9885c87386b26838a52cf4971c0868489e",
    }
    assert resolve_client_ip(_request("10.0.0.1", headers)) == "198.51.100.7"
