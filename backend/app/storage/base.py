"""StorageProvider interface.

Kept deliberately small (upload/delete/presigned-download-url) - everything
the document pipeline needs and nothing a hypothetical future backend
(Azure Blob, GCS) couldn't also implement.
"""

from typing import Protocol


class StorageProvider(Protocol):
    async def upload(self, key: str, data: bytes, content_type: str) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def generate_presigned_download_url(
        self, key: str, filename: str, expires_in: int
    ) -> str: ...
