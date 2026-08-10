"""Deterministic-first contact memory with an optional Hindsight fallback.

PostgreSQL contact-memory rows are the system of record.  Hindsight receives a
redacted, idempotent copy through a durable outbox and is never allowed to
silently rewrite a deterministic row.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import TenantScope
from app.hindsight import HindsightMemoryClient, HindsightRecallHit, HindsightUnavailableError
from app.models import (
    Call,
    Contact,
    ContactMemoryBatch,
    ContactMemoryEntry,
    HindsightSyncJob,
    Transcript,
)

MemoryKind = Literal["fact", "preference"]
RecallSource = Literal["sonnia_crm", "hindsight"]
HINDSIGHT_LABEL = "AI-assisted recall; verify before use."
HINDSIGHT_DOCUMENT_MAX_CHARS = 24_000

_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d .()\-]{6,}\d)(?!\w)")
_WORD = re.compile(r"[a-z0-9]+")


class ContactMemoryNotFoundError(LookupError):
    """A tenant-visible contact, call, or transcript was not found."""


@dataclass(frozen=True)
class ContactMemoryWrite:
    """Validated input from the transcript processor or an explicit CRM write."""

    idempotency_key: str
    facts: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    call_id: UUID | None = None
    transcript_id: UUID | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class DeterministicMemoryEntry:
    id: UUID
    kind: MemoryKind
    value: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class HindsightDocument:
    bank_id: str
    document_id: str
    content: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class StagedContactMemory:
    batch_id: UUID
    entry_ids: tuple[UUID, ...]
    sync_job_id: UUID
    hindsight_document_id: str
    created: bool


@dataclass(frozen=True)
class HindsightSyncResult:
    job_id: UUID
    status: Literal["delivered", "failed", "already_delivered"]
    attempts: int


@dataclass(frozen=True)
class ContactMemoryRecallHit:
    text: str
    source: RecallSource
    label: str
    kind: MemoryKind | None = None
    entry_id: UUID | None = None
    memory_id: str | None = None
    document_id: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class ContactMemoryRecall:
    deterministic: tuple[ContactMemoryRecallHit, ...]
    fuzzy: tuple[ContactMemoryRecallHit, ...]
    used_hindsight: bool
    hindsight_status: Literal["not_needed", "returned", "unavailable"]

    @property
    def hits(self) -> tuple[ContactMemoryRecallHit, ...]:
        """Compatibility view for voice callers that consume one fallback sequence."""

        return self.deterministic or self.fuzzy

    @property
    def fuzzy_available(self) -> bool:
        return self.hindsight_status != "unavailable"


def hindsight_bank_id(scope: TenantScope, contact_id: UUID) -> str:
    """Use a separate bank for every tenant/department/contact combination."""

    return f"sonnia-crm-{scope.org_id}-{scope.department_id}-{contact_id}"


def hindsight_document_id(batch_id: UUID) -> str:
    """Stable document IDs make post-commit retries safe provider upserts."""

    return f"sonnia-contact-memory-{batch_id}"


def redact_for_hindsight(value: str) -> str:
    """Do not send direct email addresses or phone numbers to fuzzy memory."""

    return _PHONE.sub("[redacted-phone]", _EMAIL.sub("[redacted-email]", value))


def build_hindsight_document(
    *,
    scope: TenantScope,
    contact_id: UUID,
    batch_id: UUID,
    transcript_text: str | None,
    entries: Sequence[DeterministicMemoryEntry],
) -> HindsightDocument:
    """Create the redacted provider copy from deterministic source rows."""

    facts = [entry.value for entry in entries if entry.kind == "fact"]
    preferences = [entry.value for entry in entries if entry.kind == "preference"]
    sections = [
        "Sonnia CRM contact-memory copy.",
        "This retrieval copy is non-authoritative; deterministic CRM rows are the source of truth.",
    ]
    if transcript_text:
        sections.extend(("Call transcript (redacted):", redact_for_hindsight(transcript_text)))
    if facts:
        sections.append("Validated facts:")
        sections.extend(f"- {redact_for_hindsight(value)}" for value in facts)
    if preferences:
        sections.append("Validated preferences:")
        sections.extend(f"- {redact_for_hindsight(value)}" for value in preferences)
    content = "\n".join(sections)
    if len(content) > HINDSIGHT_DOCUMENT_MAX_CHARS:
        content = content[: HINDSIGHT_DOCUMENT_MAX_CHARS - 12] + "\n[truncated]"
    return HindsightDocument(
        bank_id=hindsight_bank_id(scope, contact_id),
        document_id=hindsight_document_id(batch_id),
        content=content,
        metadata={
            "source": "sonnia_crm",
            "contact_id": str(contact_id),
            "batch_id": str(batch_id),
        },
    )


def deterministic_matches(
    entries: Sequence[DeterministicMemoryEntry],
    query: str,
    *,
    limit: int,
) -> tuple[DeterministicMemoryEntry, ...]:
    """Return deterministic matches without asking Hindsight to rank them."""

    normalized_query = " ".join(query.lower().split())
    if not normalized_query:
        raise ValueError("A memory recall query is required")
    query_words = set(_WORD.findall(normalized_query))
    ranked: list[tuple[int, int, DeterministicMemoryEntry]] = []
    for position, entry in enumerate(entries):
        text = entry.value.lower()
        words = set(_WORD.findall(text))
        overlap = len(query_words & words)
        if normalized_query not in text and not overlap:
            continue
        score = 10_000 if normalized_query in text else overlap
        ranked.append((score, -position, entry))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ranked[:limit])


async def deterministic_first_recall(
    *,
    entries: Sequence[DeterministicMemoryEntry],
    query: str,
    bank_id: str,
    hindsight: HindsightMemoryClient,
    limit: int = 5,
) -> ContactMemoryRecall:
    """Use Hindsight only when deterministic rows have no relevant answer."""

    if not 1 <= limit <= 20:
        raise ValueError("A memory recall limit must be between 1 and 20")
    deterministic = deterministic_matches(entries, query, limit=limit)
    if deterministic:
        return ContactMemoryRecall(
            deterministic=tuple(
                ContactMemoryRecallHit(
                    text=entry.value,
                    source="sonnia_crm",
                    label="Deterministic CRM record",
                    kind=entry.kind,
                    entry_id=entry.id,
                )
                for entry in deterministic
            ),
            fuzzy=(),
            used_hindsight=False,
            hindsight_status="not_needed",
        )
    try:
        fuzzy_hits = await hindsight.recall(bank_id=bank_id, query=query, limit=limit)
    except HindsightUnavailableError:
        return ContactMemoryRecall(
            deterministic=(),
            fuzzy=(),
            used_hindsight=True,
            hindsight_status="unavailable",
        )
    return ContactMemoryRecall(
        deterministic=(),
        fuzzy=tuple(_fuzzy_hit(hit) for hit in fuzzy_hits[:limit]),
        used_hindsight=True,
        hindsight_status="returned",
    )


async def stage_contact_memory(
    session: AsyncSession,
    *,
    scope: TenantScope,
    contact_id: UUID,
    write: ContactMemoryWrite,
    created_by_user_id: UUID | None = None,
) -> StagedContactMemory:
    """Persist the source of truth and its retryable Hindsight outbox record.

    Call this inside the caller's existing database transaction.  Do not call
    Hindsight here: a post-commit dispatcher invokes ``sync_hindsight_batch``.
    A transcript-only batch is valid: it has no deterministic entries but
    still gives the fuzzy layer a redacted call-transcript copy.
    """

    facts = _normalise_values(write.facts)
    preferences = _normalise_values(write.preferences)
    if not facts and not preferences and write.transcript_id is None:
        raise ValueError("A transcript, fact, or preference is required")
    if len(facts) + len(preferences) > 50:
        raise ValueError("A contact-memory write may contain at most 50 entries")
    idempotency_key = _normalise_idempotency_key(write.idempotency_key)

    if await session.get(Contact, contact_id) is None:
        raise ContactMemoryNotFoundError("Contact not found")
    existing = await _existing_batch(session, scope, contact_id, idempotency_key)
    if existing is not None:
        return await _staged_from_batch(session, existing, created=False)

    call_id, transcript_id = await _validate_source_links(
        session,
        contact_id=contact_id,
        call_id=write.call_id,
        transcript_id=write.transcript_id,
    )
    batch_id = uuid4()
    entry_rows = [
        ContactMemoryEntry(
            id=uuid4(),
            org_id=scope.org_id,
            department_id=scope.department_id,
            batch_id=batch_id,
            contact_id=contact_id,
            kind="fact",
            value=value,
            occurred_at=write.occurred_at,
        )
        for value in facts
    ] + [
        ContactMemoryEntry(
            id=uuid4(),
            org_id=scope.org_id,
            department_id=scope.department_id,
            batch_id=batch_id,
            contact_id=contact_id,
            kind="preference",
            value=value,
            occurred_at=write.occurred_at,
        )
        for value in preferences
    ]
    batch = ContactMemoryBatch(
        id=batch_id,
        org_id=scope.org_id,
        department_id=scope.department_id,
        contact_id=contact_id,
        call_id=call_id,
        transcript_id=transcript_id,
        source_kind="call_transcript" if transcript_id else "manual",
        idempotency_key=idempotency_key,
        hindsight_document_id=hindsight_document_id(batch_id),
        created_by_user_id=created_by_user_id,
    )
    job = HindsightSyncJob(
        id=uuid4(),
        org_id=scope.org_id,
        department_id=scope.department_id,
        batch_id=batch_id,
        status="pending",
    )
    try:
        async with session.begin_nested():
            session.add(batch)
            session.add_all(entry_rows)
            session.add(job)
            await session.flush()
    except IntegrityError:
        # The unique idempotency key is the database backstop for concurrent
        # webhook/transcript retries.  Reuse its one Hindsight document/job.
        existing = await _existing_batch(session, scope, contact_id, idempotency_key)
        if existing is None:
            raise
        return await _staged_from_batch(session, existing, created=False)
    return StagedContactMemory(
        batch_id=batch.id,
        entry_ids=tuple(row.id for row in entry_rows),
        sync_job_id=job.id,
        hindsight_document_id=batch.hindsight_document_id,
        created=True,
    )


async def sync_hindsight_batch(
    session: AsyncSession,
    *,
    scope: TenantScope,
    batch_id: UUID,
    hindsight: HindsightMemoryClient,
) -> HindsightSyncResult:
    """Deliver one committed outbox batch; retries reuse the same document ID."""

    batch = await session.get(ContactMemoryBatch, batch_id)
    if batch is None:
        raise ContactMemoryNotFoundError("Contact-memory batch not found")
    if batch.org_id != scope.org_id or batch.department_id != scope.department_id:
        raise ContactMemoryNotFoundError("Contact-memory batch not found")
    job = await session.scalar(
        select(HindsightSyncJob).where(HindsightSyncJob.batch_id == batch_id)
    )
    if job is None:
        raise ContactMemoryNotFoundError("Hindsight sync job not found")
    if job.status == "delivered":
        return HindsightSyncResult(job.id, "already_delivered", job.attempts)

    rows = await session.scalars(
        select(ContactMemoryEntry)
        .where(ContactMemoryEntry.batch_id == batch_id)
        .order_by(ContactMemoryEntry.created_at, ContactMemoryEntry.id)
    )
    entries = tuple(
        DeterministicMemoryEntry(
            id=row.id,
            kind=row.kind,
            value=row.value,
            created_at=row.created_at,
        )
        for row in rows
    )
    transcript_text: str | None = None
    if batch.transcript_id:
        transcript = await session.get(Transcript, batch.transcript_id)
        transcript_text = transcript.raw_text if transcript is not None else None
    document = build_hindsight_document(
        scope=scope,
        contact_id=batch.contact_id,
        batch_id=batch.id,
        transcript_text=transcript_text,
        entries=entries,
    )
    try:
        await hindsight.retain(
            bank_id=document.bank_id,
            document_id=document.document_id,
            content=document.content,
            metadata=document.metadata,
        )
    except HindsightUnavailableError:
        job.attempts += 1
        job.status = "failed"
        job.last_error = "Hindsight unavailable; retry is safe."
        await session.flush()
        return HindsightSyncResult(job.id, "failed", job.attempts)

    job.attempts += 1
    job.status = "delivered"
    job.last_error = None
    job.delivered_at = datetime.now(UTC)
    await session.flush()
    return HindsightSyncResult(job.id, "delivered", job.attempts)


async def recall_contact_memory(
    session: AsyncSession,
    *,
    scope: TenantScope,
    contact_id: UUID,
    query: str,
    hindsight: HindsightMemoryClient,
    limit: int = 5,
) -> ContactMemoryRecall:
    """Voice-agent seam: deterministic contact facts first, fuzzy fallback second."""

    if await session.get(Contact, contact_id) is None:
        raise ContactMemoryNotFoundError("Contact not found")
    rows = await session.scalars(
        select(ContactMemoryEntry)
        .where(ContactMemoryEntry.contact_id == contact_id)
        .order_by(ContactMemoryEntry.created_at.desc(), ContactMemoryEntry.id.desc())
    )
    entries = tuple(
        DeterministicMemoryEntry(
            id=row.id,
            kind=row.kind,
            value=row.value,
            created_at=row.created_at,
        )
        for row in rows
    )
    return await deterministic_first_recall(
        entries=entries,
        query=query,
        bank_id=hindsight_bank_id(scope, contact_id),
        hindsight=hindsight,
        limit=limit,
    )


def _fuzzy_hit(hit: HindsightRecallHit) -> ContactMemoryRecallHit:
    return ContactMemoryRecallHit(
        text=_safe_fuzzy_text(hit.text),
        source="hindsight",
        label=HINDSIGHT_LABEL,
        memory_id=hit.memory_id,
        document_id=hit.document_id,
        confidence=hit.confidence,
    )


def _safe_fuzzy_text(value: str) -> str:
    """Bound and redact provider output before it reaches a voice/UI consumer."""

    return redact_for_hindsight(" ".join(value.split()))[:2_000]


async def _existing_batch(
    session: AsyncSession,
    scope: TenantScope,
    contact_id: UUID,
    idempotency_key: str,
) -> ContactMemoryBatch | None:
    return await session.scalar(
        select(ContactMemoryBatch).where(
            ContactMemoryBatch.org_id == scope.org_id,
            ContactMemoryBatch.contact_id == contact_id,
            ContactMemoryBatch.idempotency_key == idempotency_key,
        )
    )


async def _staged_from_batch(
    session: AsyncSession,
    batch: ContactMemoryBatch,
    *,
    created: bool,
) -> StagedContactMemory:
    rows = await session.scalars(
        select(ContactMemoryEntry)
        .where(ContactMemoryEntry.batch_id == batch.id)
        .order_by(ContactMemoryEntry.created_at, ContactMemoryEntry.id)
    )
    job = await session.scalar(
        select(HindsightSyncJob).where(HindsightSyncJob.batch_id == batch.id)
    )
    if job is None:
        raise ContactMemoryNotFoundError("Hindsight sync job not found")
    return StagedContactMemory(
        batch_id=batch.id,
        entry_ids=tuple(row.id for row in rows),
        sync_job_id=job.id,
        hindsight_document_id=batch.hindsight_document_id,
        created=created,
    )


async def _validate_source_links(
    session: AsyncSession,
    *,
    contact_id: UUID,
    call_id: UUID | None,
    transcript_id: UUID | None,
) -> tuple[UUID | None, UUID | None]:
    transcript: Transcript | None = None
    if transcript_id is not None:
        transcript = await session.get(Transcript, transcript_id)
        if transcript is None:
            raise ContactMemoryNotFoundError("Transcript not found")
        if call_id is not None and transcript.call_id != call_id:
            raise ValueError("Transcript does not belong to the supplied call")
        call_id = transcript.call_id
    if call_id is not None:
        call = await session.get(Call, call_id)
        if call is None:
            raise ContactMemoryNotFoundError("Call not found")
        if call.contact_id not in {None, contact_id}:
            raise ValueError("Call belongs to a different contact")
    return call_id, transcript.id if transcript is not None else None


def _normalise_values(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = " ".join(raw.split())
        if not value:
            continue
        if len(value) > 4_000:
            raise ValueError("A fact or preference may not exceed 4,000 characters")
        key = value.casefold()
        if key not in seen:
            result.append(value)
            seen.add(key)
    return tuple(result)


def _normalise_idempotency_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise ValueError("An idempotency key is required")
    if len(key) > 200:
        raise ValueError("An idempotency key may not exceed 200 characters")
    return key
