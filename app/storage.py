"""Private object-storage seam for recordings and campaign materials."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlparse

import boto3
import httpx
from botocore.config import Config

from app.config import Settings, get_settings

MAX_SIGNED_URL_TTL_SECONDS = 900


def bounded_signed_url_ttl(seconds: int) -> int:
    if seconds < 1 or seconds > MAX_SIGNED_URL_TTL_SECONDS:
        raise ValueError("signed URLs must expire within 15 minutes")
    return seconds


class PrivateObjectStorage(Protocol):
    bucket_name: str

    async def copy_from_url(self, source_url: str, object_key: str) -> None: ...

    async def create_signed_url(self, object_key: str, expires_in_seconds: int = 300) -> str: ...


class InMemoryPrivateObjectStorage:
    """Small test adapter; production supplies S3/GCS/etc. behind the protocol."""

    def __init__(self) -> None:
        self.bucket_name = "private-recordings"
        self.objects: dict[str, str] = {}

    async def copy_from_url(self, source_url: str, object_key: str) -> None:
        # The adapter records the private key only.  It intentionally never
        # persists or returns the provider's public/source URL.
        self.objects[object_key] = source_url

    async def create_signed_url(self, object_key: str, expires_in_seconds: int = 300) -> str:
        bounded_signed_url_ttl(expires_in_seconds)
        if object_key not in self.objects:
            raise KeyError(object_key)
        return f"memory-private://{object_key}?expires={expires_in_seconds}"


class S3PrivateObjectStorage:
    """S3-compatible private storage with no public-object code path.

    The bucket itself enforces public access blocking and default encryption.
    This adapter deliberately does not send an ACL parameter: callers can only
    copy provider media in, or receive a short-lived signed GET URL back.
    """

    def __init__(
        self,
        *,
        bucket_name: str,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        max_copy_bytes: int = 268_435_456,
        client: Any | None = None,
    ) -> None:
        if not bucket_name:
            raise ValueError("a storage bucket is required")
        self.bucket_name = bucket_name
        self._max_copy_bytes = max_copy_bytes
        self._client = client or boto3.client(
            "s3",
            region_name=region_name,
            endpoint_url=endpoint_url,
            config=Config(signature_version="s3v4"),
        )

    async def copy_from_url(self, source_url: str, object_key: str) -> None:
        """Copy an HTTPS provider recording into the configured private bucket."""

        parsed = urlparse(source_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("recording source must use HTTPS")

        timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0)
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=timeout) as http,
            http.stream("GET", source_url) as response,
        ):
                response.raise_for_status()
                final_url = urlparse(str(response.url))
                if final_url.scheme != "https" or not final_url.netloc:
                    raise ValueError("recording redirect must use HTTPS")
                declared_size = response.headers.get("content-length")
                if declared_size and int(declared_size) > self._max_copy_bytes:
                    raise ValueError("recording is too large for private ingestion")
                copied = 0
                with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b") as body:
                    async for chunk in response.aiter_bytes():
                        copied += len(chunk)
                        if copied > self._max_copy_bytes:
                            raise ValueError("recording is too large for private ingestion")
                        body.write(chunk)
                    body.seek(0)
                    content_type_header = response.headers.get("content-type", "")
                    content_type = content_type_header.split(";", maxsplit=1)[0]
                    extra_args = {"ContentType": content_type} if content_type else None
                    await asyncio.to_thread(
                        self._client.upload_fileobj,
                        body,
                        self.bucket_name,
                        object_key,
                        ExtraArgs=extra_args,
                    )

    async def create_signed_url(self, object_key: str, expires_in_seconds: int = 300) -> str:
        ttl = bounded_signed_url_ttl(expires_in_seconds)
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": object_key},
            ExpiresIn=ttl,
            HttpMethod="GET",
        )


def configured_private_object_storage(
    settings: Settings | None = None,
) -> S3PrivateObjectStorage | None:
    """Return the real adapter only when a deployment has supplied a bucket."""

    configured = settings or get_settings()
    if not configured.storage_bucket:
        return None
    return S3PrivateObjectStorage(
        bucket_name=configured.storage_bucket,
        region_name=configured.storage_region,
        endpoint_url=configured.storage_endpoint_url,
        max_copy_bytes=configured.storage_max_copy_bytes,
    )


def private_recording_row(*, storage_bucket: str, storage_key: str) -> Mapping[str, object]:
    """Return the only storage fields a recording row is allowed to expose."""

    return {
        "storage_bucket": storage_bucket,
        "storage_key": storage_key,
        "is_private": True,
    }
