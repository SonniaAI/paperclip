"""Idempotent, replayable Telnyx webhook ingestion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import TenantScope, set_tenant_scope
from app.storage import PrivateObjectStorage


class TelnyxPayloadError(ValueError):
    """The envelope cannot be safely deduplicated or normalized."""


@dataclass(frozen=True)
class RecordingSource:
    url: str
    content_type: str | None = None


@dataclass(frozen=True)
class NormalizedCall:
    external_call_key: str
    subject: str
    direction: str | None
    status: str
    call_control_id: str | None
    call_session_id: str | None
    from_phone_e164: str | None
    to_phone_e164: str | None
    started_at: datetime | None
    ended_at: datetime | None
    recordings: tuple[RecordingSource, ...]


@dataclass(frozen=True)
class IngestionResult:
    event_id: str
    status: str
    duplicate: bool = False
    call_id: UUID | None = None
    error: str | None = None
    canonical_payload: Mapping[str, Any] | None = None


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def event_id_from_payload(payload: Mapping[str, Any]) -> str:
    data = _as_mapping(payload.get("data"))
    event_id = payload.get("id") or data.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise TelnyxPayloadError("Telnyx event id is required")
    return event_id.strip()


def event_type_from_payload(payload: Mapping[str, Any]) -> str:
    data = _as_mapping(payload.get("data"))
    body = _as_mapping(data.get("payload"))
    value = data.get("event_type") or payload.get("event_type") or body.get("event_type")
    return str(value).strip()[:160] if value else "unknown"


def _payload_body(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = _as_mapping(payload.get("data"))
    return _as_mapping(data.get("payload")) or data or payload


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _phone(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _recordings(body: Mapping[str, Any]) -> tuple[RecordingSource, ...]:
    raw = body.get("recording_urls") or body.get("recordings") or body.get("recording_url")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        return ()
    result: list[RecordingSource] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            result.append(RecordingSource(item.strip()))
        elif isinstance(item, Mapping):
            url = item.get("url") or item.get("recording_url")
            if isinstance(url, str) and url.strip():
                content_type = item.get("content_type")
                result.append(
                    RecordingSource(url.strip(), str(content_type) if content_type else None)
                )
    return tuple(result)


def normalize_telnyx_call(payload: Mapping[str, Any]) -> NormalizedCall | None:
    """Normalize call-shaped Telnyx events without retaining provider objects."""

    event_type = event_type_from_payload(payload)
    body = _payload_body(payload)
    if not (event_type.startswith("call.") or "recording" in event_type):
        return None

    control_id = body.get("call_control_id") or body.get("call_control_identity")
    session_id = body.get("call_session_id") or body.get("call_leg_id")
    provider_id = body.get("id") or body.get("connection_id")
    key = control_id or session_id or provider_id or event_id_from_payload(payload)
    if not isinstance(key, str) or not key.strip():
        raise TelnyxPayloadError("Telnyx call key is required")

    direction = body.get("direction")
    if direction in {"incoming", "inbound"}:
        direction = "inbound"
    elif direction in {"outgoing", "outbound"}:
        direction = "outbound"
    else:
        direction = None

    status = "completed"
    if event_type.endswith(".initiated") or event_type.endswith(".ringing"):
        status = "ringing"
    elif event_type.endswith(".answered") or event_type.endswith(".bridged"):
        status = "in_progress"
    elif event_type.endswith(".failed"):
        status = "failed"
    elif event_type.endswith(".hangup") or event_type.endswith(".ended"):
        status = "completed"

    return NormalizedCall(
        external_call_key=str(key).strip(),
        subject=str(body.get("subject") or "Telnyx call"),
        direction=direction,
        status=status,
        call_control_id=str(control_id) if control_id else None,
        call_session_id=str(session_id) if session_id else None,
        from_phone_e164=_phone(body.get("from") or body.get("from_phone_number")),
        to_phone_e164=_phone(body.get("to") or body.get("to_phone_number")),
        started_at=_timestamp(body.get("start_time") or body.get("started_at")),
        ended_at=_timestamp(body.get("end_time") or body.get("ended_at")),
        recordings=_recordings(body),
    )


def _canonical_json(payload: Mapping[str, Any]) -> tuple[str, str]:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _decode_stored_payload(value: object) -> Mapping[str, Any] | None:
    """Return a mapping from PostgreSQL jsonb or its string representation."""

    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, Mapping) else None


async def _load_canonical_payload(
    db: AsyncSession,
    scope: TenantScope,
    event_id: str,
) -> Mapping[str, Any]:
    """Load the tenant-scoped durable inbox body for a duplicate delivery."""

    async with db.begin():
        await set_tenant_scope(db, scope)
        raw_payload = (
            await db.execute(
                text("SELECT raw_payload FROM telnyx_webhook_events WHERE event_id = :event_id"),
                {"event_id": event_id},
            )
        ).scalar_one_or_none()
    payload = _decode_stored_payload(raw_payload)
    if payload is None:
        # A global event-ID conflict outside this tenant is not a valid replay.
        # Keep the response generic so it cannot disclose the owning tenant.
        raise TelnyxPayloadError("Telnyx event conflicts with the durable inbox")
    return payload


async def _mark_event(
    db: AsyncSession,
    scope: TenantScope,
    event_id: str,
    status: str,
    error: str | None = None,
) -> None:
    async with db.begin():
        await set_tenant_scope(db, scope)
        await db.execute(
            text(
                """
                UPDATE telnyx_webhook_events
                SET status = :status, parse_error = :error,
                    parsed_at = CASE
                        WHEN :status IN ('processed', 'ignored') THEN now()
                        ELSE parsed_at
                    END
                WHERE event_id = :event_id
                """
            ),
            {"status": status, "error": error, "event_id": event_id},
        )


async def _process_stored_event(
    db: AsyncSession,
    scope: TenantScope,
    event_id: str,
    payload: Mapping[str, Any],
    *,
    storage: PrivateObjectStorage | None,
) -> IngestionResult:
    try:
        normalized = normalize_telnyx_call(payload)
    except TelnyxPayloadError as exc:
        await _mark_event(db, scope, event_id, "failed", str(exc))
        return IngestionResult(
            event_id=event_id,
            status="failed",
            error=str(exc),
            canonical_payload=payload,
        )

    if normalized is None:
        await _mark_event(db, scope, event_id, "ignored")
        return IngestionResult(
            event_id=event_id,
            status="ignored",
            canonical_payload=payload,
        )

    try:
        async with db.begin():
            await set_tenant_scope(db, scope)
            call_row = (
                await db.execute(
                    text(
                        """
                        INSERT INTO calls (
                            id, org_id, department_id, external_call_key, subject,
                            direction, status, telnyx_call_control_id,
                            telnyx_call_session_id, from_phone_e164, to_phone_e164,
                            started_at, ended_at, metadata
                        ) VALUES (
                            :id, :org_id, :department_id, :external_call_key, :subject,
                            :direction, :status, :call_control_id,
                            :call_session_id, :from_phone, :to_phone,
                            :started_at, :ended_at, CAST(:metadata AS jsonb)
                        )
                        ON CONFLICT (org_id, external_call_key) DO UPDATE SET
                            subject = EXCLUDED.subject,
                            direction = COALESCE(EXCLUDED.direction, calls.direction),
                            status = EXCLUDED.status,
                            telnyx_call_control_id = COALESCE(
                                EXCLUDED.telnyx_call_control_id, calls.telnyx_call_control_id
                            ),
                            telnyx_call_session_id = COALESCE(
                                EXCLUDED.telnyx_call_session_id, calls.telnyx_call_session_id
                            ),
                            from_phone_e164 = COALESCE(
                                EXCLUDED.from_phone_e164, calls.from_phone_e164
                            ),
                            to_phone_e164 = COALESCE(
                                EXCLUDED.to_phone_e164, calls.to_phone_e164
                            ),
                            started_at = COALESCE(EXCLUDED.started_at, calls.started_at),
                            ended_at = COALESCE(EXCLUDED.ended_at, calls.ended_at),
                            updated_at = now()
                        RETURNING id
                        """
                    ),
                    {
                        "id": str(uuid4()),
                        "org_id": str(scope.org_id),
                        "department_id": str(scope.department_id),
                        "external_call_key": normalized.external_call_key,
                        "subject": normalized.subject,
                        "direction": normalized.direction,
                        "status": normalized.status,
                        "call_control_id": normalized.call_control_id,
                        "call_session_id": normalized.call_session_id,
                        "from_phone": normalized.from_phone_e164,
                        "to_phone": normalized.to_phone_e164,
                        "started_at": normalized.started_at,
                        "ended_at": normalized.ended_at,
                        "metadata": "{}",
                    },
                )
            ).scalar_one()
            call_id = UUID(str(call_row))

            if storage is not None:
                for recording in normalized.recordings:
                    suffix = hashlib.sha256(recording.url.encode("utf-8")).hexdigest()
                    storage_key = f"recordings/{scope.org_id}/{call_id}/{suffix}"
                    await storage.copy_from_url(recording.url, storage_key)
                    await db.execute(
                        text(
                            """
                            INSERT INTO recordings (
                                id, org_id, department_id, call_id, storage_bucket,
                                storage_key, content_type, is_private
                            ) VALUES (
                                :id, :org_id, :department_id, :call_id, :bucket,
                                :storage_key, :content_type, true
                            )
                            ON CONFLICT (org_id, storage_bucket, storage_key) DO NOTHING
                            """
                        ),
                        {
                            "id": str(uuid4()),
                            "org_id": str(scope.org_id),
                            "department_id": str(scope.department_id),
                            "call_id": str(call_id),
                            "bucket": storage.bucket_name,
                            "storage_key": storage_key,
                            "content_type": recording.content_type,
                        },
                    )
            await db.execute(
                text(
                    """
                    UPDATE telnyx_webhook_events
                    SET status = 'processed', parse_error = NULL, parsed_at = now()
                    WHERE event_id = :event_id
                    """
                ),
                {"event_id": event_id},
            )
    except Exception as exc:
        await _mark_event(db, scope, event_id, "failed", str(exc)[:2000])
        raise

    return IngestionResult(
        event_id=event_id,
        status="processed",
        call_id=call_id,
        canonical_payload=payload,
    )


async def ingest_telnyx_event(
    db: AsyncSession,
    scope: TenantScope,
    payload: Mapping[str, Any],
    *,
    storage: PrivateObjectStorage | None = None,
) -> IngestionResult:
    """Persist the raw event first, then parse/apply it exactly once."""

    if not isinstance(payload, Mapping):
        raise TelnyxPayloadError("Telnyx payload must be a JSON object")
    event_id = event_id_from_payload(payload)
    raw_payload, payload_sha256 = _canonical_json(payload)
    canonical_payload = _decode_stored_payload(raw_payload)
    if canonical_payload is None:  # json.dumps of a Mapping must round-trip to a Mapping.
        raise TelnyxPayloadError("Telnyx payload must be a JSON object")
    event_type = event_type_from_payload(payload)

    async with db.begin():
        await set_tenant_scope(db, scope)
        inserted = (
            await db.execute(
                text(
                    """
                    INSERT INTO telnyx_webhook_events (
                        id, org_id, department_id, event_id, event_type,
                        raw_payload, payload_sha256
                    ) VALUES (
                        :id, :org_id, :department_id, :event_id, :event_type,
                        CAST(:raw_payload AS jsonb), :payload_sha256
                    )
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id
                    """
                ),
                {
                    "id": str(uuid4()),
                    "org_id": str(scope.org_id),
                    "department_id": str(scope.department_id),
                    "event_id": event_id,
                    "event_type": event_type,
                    "raw_payload": raw_payload,
                    "payload_sha256": payload_sha256,
                },
            )
        ).scalar_one_or_none()

    if inserted is None:
        # Dispatchers must only see the durable first delivery.  In particular,
        # an altered same-ID webhook must not overwrite a transcript before an
        # already-delivered Hindsight batch is reused.
        stored_payload = await _load_canonical_payload(db, scope, event_id)
        return IngestionResult(
            event_id=event_id,
            status="duplicate",
            duplicate=True,
            canonical_payload=stored_payload,
        )

    return await _process_stored_event(
        db,
        scope,
        event_id,
        canonical_payload,
        storage=storage,
    )


async def replay_telnyx_event(
    db: AsyncSession,
    scope: TenantScope,
    event_id: str,
    *,
    storage: PrivateObjectStorage | None = None,
) -> IngestionResult:
    """Replay the durable raw body; useful after a transient parser/storage failure."""

    async with db.begin():
        await set_tenant_scope(db, scope)
        stored = (
            await db.execute(
                text("SELECT raw_payload FROM telnyx_webhook_events WHERE event_id = :event_id"),
                {"event_id": event_id},
            )
        ).scalar_one_or_none()
    stored_payload = _decode_stored_payload(stored)
    if stored_payload is None:
        raise TelnyxPayloadError("stored Telnyx event not found")
    return await _process_stored_event(db, scope, event_id, stored_payload, storage=storage)
