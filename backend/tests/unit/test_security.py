import uuid

import jwt
import pytest

from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def test_password_hash_is_not_plaintext():
    hashed = hash_password("correct-horse-99")
    assert hashed != "correct-horse-99"
    assert hashed.startswith("$argon2")


def test_verify_password_correct_and_incorrect():
    hashed = hash_password("correct-horse-99")
    assert verify_password("correct-horse-99", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_access_token_roundtrip():
    user_id = uuid.uuid4()
    token = create_access_token(user_id)
    payload = decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["type"] == "access"


def test_expired_access_token_raises():
    user_id = uuid.uuid4()
    token = create_access_token(user_id)
    # Tamper with exp to simulate an expired token without sleeping.
    from app.core.config import settings

    decoded = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    decoded["exp"] = 0
    expired_token = jwt.encode(decoded, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(expired_token)


def test_refresh_token_hash_is_deterministic_and_not_reversible():
    raw, token_hash = generate_refresh_token()
    assert hash_refresh_token(raw) == token_hash
    assert token_hash != raw
    assert len(token_hash) == 64  # sha256 hex digest


def test_refresh_tokens_are_unique():
    raw1, _ = generate_refresh_token()
    raw2, _ = generate_refresh_token()
    assert raw1 != raw2
