"""StorageProvider interface.

Kept deliberately small (upload/delete/presigned-download-url) - everything
the document pipeline needs and nothing a hypothetical future backend
(Azure Blob, GCS) couldn't also implement.
"""

from typing import BinaryIO, Protocol


class StorageProvider(Protocol):
    async def upload(self, key: str, data: bytes, content_type: str) -> None: ...

    async def download(self, key: str) -> bytes: ...

    # Streaming variants used by the upload and ingestion paths, so a document is
    # never held in memory whole: upload_file sends from a seekable file object
    # (the request's spooled temp file), download_to_file writes into one.
    async def upload_file(self, key: str, fileobj: BinaryIO, content_type: str) -> None: ...

    async def download_to_file(self, key: str, fileobj: BinaryIO) -> int: ...

    async def delete(self, key: str) -> None: ...

    async def generate_presigned_download_url(
        self, key: str, filename: str, expires_in: int
    ) -> str: ...
