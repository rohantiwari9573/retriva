"""In-memory StorageProvider used only in tests.

Lets the document upload/download/delete test suite run without a live
MinIO - CI has no object storage service, and spinning one up just for unit-
style assertions on the document API would be slow and brittle. Real
storage behavior (bucket creation, presigned URL signing) is exercised
manually against Docker Compose's MinIO, not by the automated suite.
"""

from app.core.exceptions import StorageObjectNotFoundError


class InMemoryStorageProvider:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def download(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise StorageObjectNotFoundError(
                "The stored file could not be found."
            ) from exc

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def generate_presigned_download_url(
        self, key: str, filename: str, expires_in: int
    ) -> str:
        return f"http://fake-storage.test/{key}?filename={filename}&expires_in={expires_in}"
