"""Small, provider-owned adapter for the optional Hindsight memory service.

The rest of the application depends only on :class:`HindsightMemoryClient`.
That keeps deterministic CRM writes available when Hindsight is intentionally
not configured or temporarily unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class HindsightUnavailableError(RuntimeError):
    """The optional fuzzy-memory provider cannot serve this request."""


@dataclass(frozen=True)
class HindsightRecallHit:
    """The small, provider-neutral subset exposed to CRM callers."""

    text: str
    memory_id: str | None = None
    document_id: str | None = None
    confidence: float | None = None


class HindsightMemoryClient(Protocol):
    """Operations the contact-memory service needs from Hindsight."""

    async def retain(
        self,
        *,
        bank_id: str,
        document_id: str,
        content: str,
        metadata: Mapping[str, str],
        tags: Sequence[str] = (),
    ) -> None: ...

    async def recall(
        self,
        *,
        bank_id: str,
        query: str,
        limit: int,
        tags: Sequence[str] = (),
    ) -> Sequence[HindsightRecallHit]: ...

    async def delete_bank(self, *, bank_id: str) -> None: ...


class HindsightDisabledClient:
    """Explicitly fail fuzzy operations while leaving CRM operations healthy."""

    async def retain(
        self,
        *,
        bank_id: str,
        document_id: str,
        content: str,
        metadata: Mapping[str, str],
        tags: Sequence[str] = (),
    ) -> None:
        del bank_id, document_id, content, metadata, tags
        raise HindsightUnavailableError("Hindsight is not configured")

    async def recall(
        self,
        *,
        bank_id: str,
        query: str,
        limit: int,
        tags: Sequence[str] = (),
    ) -> Sequence[HindsightRecallHit]:
        del bank_id, query, limit, tags
        raise HindsightUnavailableError("Hindsight is not configured")

    async def delete_bank(self, *, bank_id: str) -> None:
        del bank_id
        raise HindsightUnavailableError("Hindsight is not configured")


class HindsightSDKClient:
    """Adapter around Vectorize's maintained ``hindsight-client`` SDK.

    The SDK import is deliberately lazy.  A development or recovery deployment
    can run the deterministic contact book without installing or configuring
    the optional memory provider.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        sdk_client: Any | None = None,
    ) -> None:
        if sdk_client is None:
            try:
                from hindsight_client import Hindsight
            except ImportError as exc:  # pragma: no cover - exercised by deployment config
                raise HindsightUnavailableError("The Hindsight SDK is not installed") from exc
            sdk_client = Hindsight(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout_seconds,
            )
        self._client = sdk_client

    async def retain(
        self,
        *,
        bank_id: str,
        document_id: str,
        content: str,
        metadata: Mapping[str, str],
        tags: Sequence[str] = (),
    ) -> None:
        try:
            await self._client.aretain(
                bank_id=bank_id,
                content=content,
                context="Sonnia CRM contact-memory copy",
                document_id=document_id,
                metadata=dict(metadata),
                tags=list(tags),
                # Hindsight's stable document ID alone is not an overwrite
                # guarantee.  Explicit replacement makes a delivery retry a
                # true provider-side upsert rather than another memory copy.
                update_mode="replace",
            )
        except Exception as exc:  # The provider boundary must not break the CRM write path.
            raise HindsightUnavailableError("Hindsight retain failed") from exc

    async def recall(
        self,
        *,
        bank_id: str,
        query: str,
        limit: int,
        tags: Sequence[str] = (),
    ) -> Sequence[HindsightRecallHit]:
        try:
            options: dict[str, Any] = {"bank_id": bank_id, "query": query}
            if tags:
                # The bank is already per-contact.  Strict tags add a second
                # provider-side boundary so an untagged document cannot be
                # returned even if a bank were populated incorrectly.
                options["tags"] = list(tags)
                options["tags_match"] = "all_strict"
            response = await self._client.arecall(**options)
        except Exception as exc:  # The caller turns this into a labeled unavailable result.
            raise HindsightUnavailableError("Hindsight recall failed") from exc

        raw_results = getattr(response, "results", response) or ()
        hits: list[HindsightRecallHit] = []
        for raw in raw_results:
            text = _field(raw, "text")
            if not isinstance(text, str) or not text.strip():
                continue
            memory_id = _field(raw, "id")
            document_id = _field(raw, "document_id", "documentId")
            confidence = _confidence(_field(raw, "confidence", "score"))
            hits.append(
                HindsightRecallHit(
                    text=text.strip(),
                    memory_id=str(memory_id) if memory_id is not None else None,
                    document_id=str(document_id) if document_id is not None else None,
                    confidence=confidence,
                )
            )
            if len(hits) >= limit:
                break
        return hits

    async def delete_bank(self, *, bank_id: str) -> None:
        """Erase one contact-only bank without touching deterministic CRM rows."""

        try:
            await self._client.adelete_bank(bank_id=bank_id)
        except Exception as exc:
            raise HindsightUnavailableError("Hindsight customer-memory deletion failed") from exc


def configured_hindsight_client(
    *,
    base_url: str | None,
    api_key: str | None,
    timeout_seconds: float,
) -> HindsightMemoryClient:
    """Return an explicit disabled client unless a Hindsight URL is configured."""

    if not base_url:
        return HindsightDisabledClient()
    try:
        return HindsightSDKClient(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
    except HindsightUnavailableError:
        # A partially configured optional provider must not make an otherwise
        # valid deterministic CRM write or voice read fail.
        return HindsightDisabledClient()


def _field(value: object, *names: str) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _confidence(value: object) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return confidence if 0 <= confidence <= 1 else None
