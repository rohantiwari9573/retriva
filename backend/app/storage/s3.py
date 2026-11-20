"""S3-compatible storage (works for both MinIO and real AWS S3 - MinIO speaks
the S3 API, so the same boto3 client works against either by only changing
`endpoint_url`).

boto3 is synchronous; every call is offloaded to a thread via
asyncio.to_thread so it never blocks the event loop. The boto3 client itself
is safe to share across threads (it doesn't hold event-loop-affine state the
way an asyncio-native client would), so - unlike the Redis rate limiter's
earlier bug - a module-level singleton here is fine.
"""

import asyncio
import time
from contextlib import contextmanager
from functools import lru_cache
from typing import BinaryIO

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.core.exceptions import StorageError, StorageObjectNotFoundError
from app.core.logging import get_logger
from app.core.metrics import (
    storage_errors_total,
    storage_request_duration_seconds,
    storage_requests_total,
)
from app.core.telemetry import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_STREAM_CHUNK_BYTES = 1024 * 1024


@contextmanager
def _instrument(operation: str):
    """Records storage_requests_total/duration/errors and a span around one
    S3/MinIO call. `operation` is always one of the four fixed literals
    passed at each call site below (upload/download/delete/presign) - never
    derived from the object key, which stays out of every label per the
    cardinality policy in app/core/metrics.py."""
    start = time.perf_counter()
    with tracer.start_as_current_span(f"storage.{operation}"):
        try:
            yield
        except Exception:
            storage_requests_total.labels(operation=operation, status="failure").inc()
            storage_errors_total.labels(operation=operation).inc()
            raise
        else:
            storage_requests_total.labels(operation=operation, status="success").inc()
        finally:
            storage_request_duration_seconds.labels(operation=operation).observe(
                time.perf_counter() - start
            )


def _escape_content_disposition_filename(filename: str) -> str:
    """RFC 6266 quoted-string escaping for the filename param.

    `filename` is user-supplied (Document.original_filename, only lightly
    sanitized for printability - see _sanitize_display_filename). A literal
    `"` would otherwise break out of the quoted parameter, and a raw CR/LF
    would let it inject additional header content. Backslash-escape both
    quote and backslash per RFC 6266/2616 quoted-string syntax, and strip
    control characters outright since they have no legitimate place in a
    displayed filename.
    """
    sanitized = "".join(ch for ch in filename if ch.isprintable())
    return sanitized.replace("\\", "\\\\").replace('"', '\\"')


class S3StorageProvider:
    def __init__(self) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.AWS_REGION,
            config=BotoConfig(signature_version="s3v4"),
        )
        # Presigned URLs must be signed against the endpoint the *browser*
        # will call, which differs from the internal container-to-container
        # endpoint used for the upload/delete calls above.
        self._public_client = boto3.client(
            "s3",
            endpoint_url=settings.S3_PUBLIC_ENDPOINT_URL or settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.AWS_REGION,
            config=BotoConfig(signature_version="s3v4"),
        )
        self._bucket = settings.S3_BUCKET

    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        with _instrument("upload"):
            try:
                await asyncio.to_thread(
                    self._client.put_object,
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                )
            except ClientError as exc:
                logger.error("storage_upload_failed", key=key, exc_info=exc)
                raise StorageError("Failed to store the uploaded file.") from exc

    async def upload_file(self, key: str, fileobj: BinaryIO, content_type: str) -> None:
        """One PUT streamed from a seekable file: botocore reads the body in small
        blocks, so memory stays flat whatever the size. Deliberately not boto3's
        upload_fileobj, whose transfer manager runs ~10 threads with 8 MB parts."""
        with _instrument("upload"):
            try:
                fileobj.seek(0)
                await asyncio.to_thread(
                    self._client.put_object,
                    Bucket=self._bucket,
                    Key=key,
                    Body=fileobj,
                    ContentType=content_type,
                )
            except ClientError as exc:
                logger.error("storage_upload_failed", key=key, exc_info=exc)
                raise StorageError("Failed to store the uploaded file.") from exc

    async def download_to_file(self, key: str, fileobj: BinaryIO) -> int:
        """Streams the object into fileobj in 1 MiB pieces; returns bytes written."""
        with _instrument("download"):
            try:
                return await asyncio.to_thread(self._download_to_file_sync, key, fileobj)
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code in ("NoSuchKey", "404"):
                    raise StorageObjectNotFoundError("The stored file could not be found.") from exc
                logger.error("storage_download_failed", key=key, exc_info=exc)
                raise StorageError("Failed to read the stored file.") from exc
            except (BotoCoreError, OSError) as exc:
                # Connection reset/timeout mid-stream: transient, retryable.
                logger.error("storage_download_failed", key=key, exc_info=exc)
                raise StorageError("Failed to read the stored file.") from exc

    def _download_to_file_sync(self, key: str, fileobj: BinaryIO) -> int:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        written = 0
        for piece in response["Body"].iter_chunks(chunk_size=_STREAM_CHUNK_BYTES):
            fileobj.write(piece)
            written += len(piece)
        fileobj.flush()
        return written

    async def download(self, key: str) -> bytes:
        with _instrument("download"):
            try:
                response = await asyncio.to_thread(
                    self._client.get_object, Bucket=self._bucket, Key=key
                )
                body = response["Body"]
                return await asyncio.to_thread(body.read)
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code in ("NoSuchKey", "404"):
                    raise StorageObjectNotFoundError("The stored file could not be found.") from exc
                logger.error("storage_download_failed", key=key, exc_info=exc)
                raise StorageError("Failed to read the stored file.") from exc

    async def delete(self, key: str) -> None:
        with _instrument("delete"):
            try:
                await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
            except ClientError as exc:
                logger.error("storage_delete_failed", key=key, exc_info=exc)
                raise StorageError("Failed to delete the stored file.") from exc

    async def generate_presigned_download_url(
        self, key: str, filename: str, expires_in: int
    ) -> str:
        safe_filename = _escape_content_disposition_filename(filename)
        with _instrument("presign"):
            try:
                return await asyncio.to_thread(
                    self._public_client.generate_presigned_url,
                    "get_object",
                    Params={
                        "Bucket": self._bucket,
                        "Key": key,
                        "ResponseContentDisposition": f'attachment; filename="{safe_filename}"',
                    },
                    ExpiresIn=expires_in,
                )
            except ClientError as exc:
                logger.error("storage_presign_failed", key=key, exc_info=exc)
                raise StorageError("Failed to generate a download link.") from exc

    def ensure_bucket_exists(self) -> None:
        """Best-effort startup helper - MinIO doesn't auto-create buckets like
        some managed S3 setups. Failures are logged, not raised: a
        transiently-unreachable MinIO shouldn't crash app startup, and the
        first real upload will surface the problem clearly anyway."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except BotoCoreError as exc:  # endpoint unreachable, timeout, bad config
            logger.warning("storage_bucket_ensure_failed", exc_info=exc)
        except ClientError:
            try:
                self._client.create_bucket(Bucket=self._bucket)
                logger.info("storage_bucket_created", bucket=self._bucket)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("storage_bucket_ensure_failed", exc_info=exc)


@lru_cache
def get_s3_storage_provider() -> S3StorageProvider:
    return S3StorageProvider()
