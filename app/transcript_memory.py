"""Production post-commit dispatch for transcript-backed contact memory.

Telnyx ingestion commits its durable inbox and canonical call first.  This
module then records the transcript/facts/preferences in the deterministic
contact book and, only after that commit, delivers the redacted Hindsight copy.
It is deliberately safe to call again for a duplicate provider event.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contact_memory import (
    ContactMemoryWrite,
    HindsightSyncResult,
    stage_contact_memory,
    sync_hindsight_batch,
)
from app.database import TenantScope, set_tenant_scope
from app.hindsight import HindsightMemoryClient
from app.ingestion import normalize_telnyx_call
from app.models import Call, Contact, ContactPhone, Transcript


@dataclass(frozen=True)
class TranscriptMemoryPayload:
    """The transcript processor fields accepted from a stored call event."""

    transcript_text: str | None
    facts: tuple[str, ...]
    preferences: tuple[str, ...]
    language_code: str | None

    @property
    def has_memory(self) -> bool:
        return bool(self.transcript_text or self.facts or self.preferences)


@dataclass(frozen=True)
class TranscriptMemoryDispatch:
    """A non-secret observation for the webhook handler and operational logs."""

    status: Literal["dispatched", "skipped_no_memory", "skipped_no_call", "skipped_no_contact"]
    batch_id: UUID | None = None
    sync: HindsightSyncResult | None = None


def extract_transcript_memory(payload: Mapping[str, Any]) -> TranscriptMemoryPayload:
    """Read the documented transcript-processor fields without trusting them as facts.

    The transcript processor may place its normalized result in
    ``data.payload.contact_memory``.  Direct ``transcript`` and
    ``transcript_text`` fields are accepted for providers that put their final
    transcript on the call event.  The values are persisted as source material;
    only explicit ``facts`` and ``preferences`` become deterministic entries.
    """

    body = _payload_body(payload)
    contact_memory = _as_mapping(body.get("contact_memory"))
    transcript_text = _first_text(
        contact_memory.get("transcript"),
        contact_memory.get("transcript_text"),
        body.get("transcript"),
        body.get("transcript_text"),
        body.get("transcription"),
        body.get("transcription_text"),
    )
    facts = _string_values(contact_memory.get("facts") or body.get("facts"))
    preferences = _string_values(
        contact_memory.get("preferences") or body.get("preferences")
    )
    language = contact_memory.get("language_code") or body.get("language_code")
    language_code = str(language).strip()[:20] if language else None
    return TranscriptMemoryPayload(
        transcript_text=transcript_text,
        facts=facts,
        preferences=preferences,
        language_code=language_code or None,
    )


async def dispatch_transcript_memory(
    session: AsyncSession,
    *,
    scope: TenantScope,
    event_id: str,
    payload: Mapping[str, Any],
    call_id: UUID | None,
    hindsight: HindsightMemoryClient,
) -> TranscriptMemoryDispatch:
    """Stage a transcript memory transaction, commit it, then deliver its outbox.

    This is the production caller for the deterministic write and Hindsight
    dispatch seams.  It starts a new transaction after call ingestion has
    completed, so a provider timeout can never roll back the inbox/call write.
    """

    memory = extract_transcript_memory(payload)
    if not memory.has_memory:
        return TranscriptMemoryDispatch(status="skipped_no_memory")

    async with session.begin():
        await set_tenant_scope(session, scope)
        call = await _call_for_event(session, payload=payload, call_id=call_id)
        if call is None:
            return TranscriptMemoryDispatch(status="skipped_no_call")
        contact_id = await _contact_for_call(session, call)
        if contact_id is None:
            return TranscriptMemoryDispatch(status="skipped_no_contact")
        transcript = await _upsert_transcript(
            session,
            scope=scope,
            call=call,
            memory=memory,
        )
        staged = await stage_contact_memory(
            session,
            scope=scope,
            contact_id=contact_id,
            write=ContactMemoryWrite(
                idempotency_key=_idempotency_key(event_id),
                facts=memory.facts,
                preferences=memory.preferences,
                call_id=call.id,
                transcript_id=transcript.id if transcript is not None else None,
                occurred_at=call.ended_at or call.started_at,
            ),
        )

    # Hindsight is intentionally invoked only after the transaction containing
    # the deterministic rows and durable outbox has committed.
    async with session.begin():
        await set_tenant_scope(session, scope)
        sync = await sync_hindsight_batch(
            session,
            scope=scope,
            batch_id=staged.batch_id,
            hindsight=hindsight,
        )
    return TranscriptMemoryDispatch(status="dispatched", batch_id=staged.batch_id, sync=sync)


async def _call_for_event(
    session: AsyncSession,
    *,
    payload: Mapping[str, Any],
    call_id: UUID | None,
) -> Call | None:
    if call_id is not None:
        call = await session.get(Call, call_id)
        if call is not None:
            return call
    normalized = normalize_telnyx_call(payload)
    if normalized is None:
        return None
    return await session.scalar(
        select(Call).where(Call.external_call_key == normalized.external_call_key)
    )


async def _contact_for_call(session: AsyncSession, call: Call) -> UUID | None:
    """Use an existing call link or one unambiguous imported phone match."""

    if call.contact_id is not None and await session.get(Contact, call.contact_id) is not None:
        return call.contact_id

    phones = tuple(
        phone
        for phone in (call.from_phone_e164, call.to_phone_e164)
        if phone is not None and phone.strip()
    )
    if not phones:
        return None
    rows = await session.scalars(
        select(ContactPhone).where(ContactPhone.phone_e164.in_(phones))
    )
    contact_ids = {row.contact_id for row in rows}
    if len(contact_ids) != 1:
        return None
    contact_id = contact_ids.pop()
    # Carry the deterministic association forward so later transcript events do
    # not depend on matching a mutable phone number again.
    call.contact_id = contact_id
    await session.flush()
    return contact_id


async def _upsert_transcript(
    session: AsyncSession,
    *,
    scope: TenantScope,
    call: Call,
    memory: TranscriptMemoryPayload,
) -> Transcript | None:
    if memory.transcript_text is None:
        return None
    transcript = await session.scalar(select(Transcript).where(Transcript.call_id == call.id))
    if transcript is None:
        transcript = Transcript(
            id=uuid4(),
            org_id=scope.org_id,
            department_id=scope.department_id,
            call_id=call.id,
            provider="telnyx",
            language_code=memory.language_code,
            status="complete",
            raw_text=memory.transcript_text,
        )
        session.add(transcript)
    else:
        transcript.provider = "telnyx"
        transcript.language_code = memory.language_code or transcript.language_code
        transcript.status = "complete"
        transcript.raw_text = memory.transcript_text
    await session.flush()
    return transcript


def _payload_body(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = _as_mapping(payload.get("data"))
    return _as_mapping(data.get("payload")) or data or payload


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: object) -> str | None:
    for value in values:
        text = _text(value)
        if text:
            return text
    return None


def _text(value: object) -> str | None:
    if isinstance(value, str):
        normalized = " ".join(value.split())
        return normalized or None
    if isinstance(value, Mapping):
        for key in ("text", "content", "transcript", "transcript_text"):
            nested = _text(value.get(key))
            if nested:
                return nested
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = [part for item in value if (part := _text(item))]
        return " ".join(parts) or None
    return None


def _string_values(value: object) -> tuple[str, ...]:
    values: list[str] = []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Mapping):
        text = _first_text(value.get("value"), value.get("text"), value.get("content"))
        values = [text] if text else []
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, Mapping):
                text = _first_text(item.get("value"), item.get("text"), item.get("content"))
            else:
                text = _text(item)
            if text:
                values.append(text)
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        normalized = " ".join(raw.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
        if len(result) == 25:
            break
    return tuple(result)


def _idempotency_key(event_id: str) -> str:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"telnyx-transcript:{digest}"
