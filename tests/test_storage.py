from __future__ import annotations

import pytest

from app.config import Settings
from app.storage import (
    S3PrivateObjectStorage,
    bounded_signed_url_ttl,
    configured_private_object_storage,
)


class FakeS3Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def generate_presigned_url(self, operation: str, **kwargs: object) -> str:
        self.calls.append((operation, kwargs))
        return "https://signed.example.test/private-object"


@pytest.mark.asyncio
async def test_s3_adapter_issues_a_short_lived_private_get_url() -> None:
    client = FakeS3Client()
    storage = S3PrivateObjectStorage(
        bucket_name="manager-sonnia-private",
        region_name="us-east-1",
        client=client,
    )

    url = await storage.create_signed_url(
        "recordings/org-a/call-a/source", expires_in_seconds=300
    )

    assert url == "https://signed.example.test/private-object"
    assert client.calls == [
        (
            "get_object",
            {
                "Params": {
                    "Bucket": "manager-sonnia-private",
                    "Key": "recordings/org-a/call-a/source",
                },
                "ExpiresIn": 300,
                "HttpMethod": "GET",
            },
        )
    ]


def test_storage_factory_requires_an_explicit_bucket() -> None:
    assert configured_private_object_storage(Settings()) is None


def test_signed_url_ttl_rejects_long_lived_links() -> None:
    with pytest.raises(ValueError):
        bounded_signed_url_ttl(901)
