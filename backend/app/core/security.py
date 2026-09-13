"""Password hashing, JWT access tokens, and opaque refresh tokens.

Passwords use Argon2id (via argon2-cffi's high-level API, which defaults to the
Argon2id variant) - the current OWASP-recommended choice over bcrypt for new
projects. Refresh tokens are random opaque strings, not JWTs: the server must be
able to revoke them individually and check expiry from the DB, which a
self-contained JWT can't do without an extra revocation-list lookup anyway - so
there's no benefit to making them JWTs, only the cost of a bigger token.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

from app.core.config import settings

_password_hasher = PasswordHasher()

# A precomputed hash of an unguessable, never-issued password. Used only so
# login() can run verify_password's ~100ms Argon2 cost on the "no such user"
# path too - otherwise response time would distinguish a registered email
# from an unregistered one even though the error text is identical.
DUMMY_PASSWORD_HASH = _password_hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _password_hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def create_access_token(user_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return payload


def generate_refresh_token() -> tuple[str, str]:
    """Returns (raw_token_for_cookie, sha256_hash_for_storage)."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
