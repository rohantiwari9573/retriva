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
from functools import lru_cache

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.exceptions import StorageError
from app.core.logging import get_logger

logger = get_logger(__name__)


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

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=key
            )
        except ClientError as exc:
            logger.error("storage_delete_failed", key=key, exc_info=exc)
            raise StorageError("Failed to delete the stored file.") from exc

    async def generate_presigned_download_url(
        self, key: str, filename: str, expires_in: int
    ) -> str:
        try:
            return await asyncio.to_thread(
                self._public_client.generate_presigned_url,
                "get_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{filename}"',
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
        except ClientError:
            try:
                self._client.create_bucket(Bucket=self._bucket)
                logger.info("storage_bucket_created", bucket=self._bucket)
            except ClientError as exc:
                logger.warning("storage_bucket_ensure_failed", exc_info=exc)


@lru_cache
def get_s3_storage_provider() -> S3StorageProvider:
    return S3StorageProvider()
