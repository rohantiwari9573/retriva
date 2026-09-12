"""FastAPI dependency for storage access.

A plain function (not a cached global reference baked into route signatures)
so tests can override it via app.dependency_overrides[get_storage_provider],
the same pattern used for get_db - swap in InMemoryStorageProvider without
touching route code or requiring a live MinIO in CI.
"""

from app.storage.base import StorageProvider
from app.storage.s3 import get_s3_storage_provider


def get_storage_provider() -> StorageProvider:
    return get_s3_storage_provider()
