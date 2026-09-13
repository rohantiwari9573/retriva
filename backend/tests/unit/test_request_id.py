from app.core.request_id import generate_request_id, resolve_request_id


def test_generate_request_id_is_url_safe_and_reasonably_unique():
    a = generate_request_id()
    b = generate_request_id()
    assert a != b
    assert all(ch.isalnum() or ch in "-_" for ch in a)


def test_resolve_uses_valid_client_supplied_id():
    assert resolve_request_id("abc-123.xyz~ABC") == "abc-123.xyz~ABC"


def test_resolve_generates_when_none_supplied():
    result = resolve_request_id(None)
    assert result
    assert len(result) <= 128


def test_resolve_rejects_oversized_client_id():
    huge = "a" * 500
    result = resolve_request_id(huge)
    assert result != huge
    assert len(result) <= 128


def test_resolve_rejects_control_characters():
    malicious = "abc\r\nX-Injected: evil"
    result = resolve_request_id(malicious)
    assert result != malicious
    assert "\r" not in result and "\n" not in result


def test_resolve_rejects_empty_string():
    result = resolve_request_id("")
    assert result != ""
    assert len(result) > 0
