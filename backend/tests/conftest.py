"""Shared pytest fixtures.

Kept minimal in Phase 1 - just enough to prove the app imports and boots. Database
fixtures (test transaction rollback, async client with dependency overrides) are
added in Phase 2 alongside auth.
"""

import os

os.environ.setdefault("JWT_SECRET", "test-secret-please-override-1234567890")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus_test")

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
