"""Deterministic-first contact memory with an optional Hindsight fallback.

PostgreSQL contact-memory rows are the system of record.  Hindsight receives a
redacted, idempotent copy through a durable outbox and is never allowed to
silently rewrite a deterministic row.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import TenantScope
from app.hindsight import (
    HindsightDisabledClient,
    HindsightMemoryClient,
    HindsightRecallHit,
    HindsightUnavailableError,
)
from app.models import (
    Call,
    Contact,
    ContactMemoryBatch,
    ContactMemoryDeletion,
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
_NON_RELEVANT_TERMS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "said",
        "say",
        "she",
        "should",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
        "you",
        "your",
    }
)


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
    source_event_id: str | None = None
    speaker: str | None = None
    extraction_provenance: str | None = None


@dataclass(frozen=True)
class DeterministicMemoryEntry:
    id: UUID
    kind: MemoryKind
    value: str
    created_at: datetime | None = None
    source_call_id: UUID | None = None
    source_event_id: str | None = None
    source_occurred_at: datetime | None = None
    speaker: str | None = None
    extraction_provenance: str | None = None


@dataclass(frozen=True)
class HindsightDocument:
    bank_id: str
    document_id: str
    content: str
    metadata: dict[str, str]
    tags: tuple[str, ...]


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
class StagedContactMemoryDeletion:
    deletion_id: UUID
    deterministic_entries_removed: int
    source_batches_removed: int
    hindsight_bank_id: str


@dataclass(frozen=True)
class HindsightDeletionResult:
    deletion_id: UUID
    status: Literal["deleted", "failed", "already_deleted"]
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
    source_call_id: UUID | None = None
    source_event_id: str | None = None
    source_occurred_at: datetime | None = None
    speaker: str | None = None
    extraction_provenance: str | None = None


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


def hindsight_scope_tags(scope: TenantScope, contact_id: UUID) -> tuple[str, ...]:
    """Provider-side scope assertions in addition to the contact-only bank.

    These opaque UUID tags contain no direct customer data.  Retain and recall
    both require all of them, and local document matching below is a final
    guard against a malformed provider response.
    """

    return (
        f"sonnia:org:{scope.org_id}",
        f"sonnia:department:{scope.department_id}",
        f"sonnia:contact:{contact_id}",
    )


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
    source_event_id: str | None = None,
    source_occurred_at: datetime | None = None,
    speaker: str | None = None,
    extraction_provenance: str | None = None,
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
    metadata = {
        "source": "sonnia_crm",
        "contact_id": str(contact_id),
        "batch_id": str(batch_id),
    }
    if source_event_id:
        metadata["source_event_id"] = source_event_id
    if source_occurred_at:
        metadata["source_occurred_at"] = source_occurred_at.isoformat()
    if speaker:
        metadata["speaker"] = speaker
    if extraction_provenance:
        metadata["extraction_provenance"] = extraction_provenance
    return HindsightDocument(
        bank_id=hindsight_bank_id(scope, contact_id),
        document_id=hindsight_document_id(batch_id),
        content=content,
        metadata=metadata,
        tags=hindsight_scope_tags(scope, contact_id),
    )


def deterministic_matches(
    entries: Sequence[DeterministicMemoryEntry],
    query: str,
    *,
    limit: int,
) -> tuple[DeterministicMemoryEntry, ...]:
    """Return rows sharing meaningful query terms without asking Hindsight."""

    normalized_query = " ".join(query.casefold().split())
    if not normalized_query:
        raise ValueError("A memory recall query is required")
    query_terms = _meaningful_terms(normalized_query)
    if not query_terms:
        return ()
    ranked: list[tuple[int, int, DeterministicMemoryEntry]] = []
    for position, entry in enumerate(entries):
        text = " ".join(entry.value.casefold().split())
        overlap = len(query_terms & _meaningful_terms(text))
        if not overlap:
            continue
        score = 10_000 + overlap if normalized_query in text else overlap
        ranked.append((score, -position, entry))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ranked[:limit])


def _meaningful_terms(value: str) -> set[str]:
    """Discard conversational filler before treating token overlap as relevance."""

    return {
        word
        for word in _WORD.findall(value.casefold())
        if len(word) > 1 and word not in _NON_RELEVANT_TERMS
    }


async def deterministic_first_recall(
    *,
    entries: Sequence[DeterministicMemoryEntry],
    query: str,
    bank_id: str,
    hindsight: HindsightMemoryClient,
    limit: int = 5,
    tags: Sequence[str] = (),
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
                    source_call_id=entry.source_call_id,
                    source_event_id=entry.source_event_id,
                    source_occurred_at=entry.source_occurred_at,
                    speaker=entry.speaker,
                    extraction_provenance=entry.extraction_provenance,
                )
                for entry in deterministic
            ),
            fuzzy=(),
            used_hindsight=False,
            hindsight_status="not_needed",
        )
    try:
        fuzzy_hits = await hindsight.recall(
            bank_id=bank_id,
            query=query,
            limit=limit,
            tags=tags,
        )
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
    source_event_id = _normalise_optional_text(
        write.source_event_id,
        field="source event ID",
        max_length=200,
    )
    speaker = _normalise_optional_text(write.speaker, field="speaker", max_length=100)
    extraction_provenance = _normalise_optional_text(
        write.extraction_provenance,
        field="extraction provenance",
        max_length=200,
    )

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
        source_event_id=source_event_id,
        source_occurred_at=write.occurred_at,
        speaker=speaker,
        extraction_provenance=extraction_provenance,
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

    deletion = await _contact_memory_deletion(session, batch.contact_id)
    if deletion is not None and deletion.status != "delivered":
        # A local erasure has committed but the provider has not confirmed it.
        # Do not re-populate a bank that might still contain the deleted copy.
        job.attempts += 1
        job.status = "failed"
        job.last_error = "Customer-memory deletion is incomplete; retry is safe."
        await session.flush()
        return HindsightSyncResult(job.id, "failed", job.attempts)

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
        source_event_id=batch.source_event_id,
        source_occurred_at=batch.source_occurred_at,
        speaker=batch.speaker,
        extraction_provenance=batch.extraction_provenance,
    )
    try:
        await hindsight.retain(
            bank_id=document.bank_id,
            document_id=document.document_id,
            content=document.content,
            metadata=document.metadata,
            tags=document.tags,
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
    rows = (
        await session.execute(
            select(ContactMemoryEntry, ContactMemoryBatch)
            .join(ContactMemoryBatch, ContactMemoryEntry.batch_id == ContactMemoryBatch.id)
            .where(ContactMemoryEntry.contact_id == contact_id)
            .order_by(ContactMemoryEntry.created_at.desc(), ContactMemoryEntry.id.desc())
        )
    ).all()
    entries = tuple(
        DeterministicMemoryEntry(
            id=entry.id,
            kind=entry.kind,
            value=entry.value,
            created_at=entry.created_at,
            source_call_id=batch.call_id,
            source_event_id=batch.source_event_id,
            source_occurred_at=entry.occurred_at or batch.source_occurred_at,
            speaker=batch.speaker,
            extraction_provenance=batch.extraction_provenance,
        )
        for entry, batch in rows
    )
    deletion = await _contact_memory_deletion(session, contact_id)
    # A provider erase that has not been acknowledged is an explicit fuzzy
    # outage for this contact. This prevents stale results from reappearing.
    recall_client: HindsightMemoryClient = (
        hindsight
        if deletion is None or deletion.status == "delivered"
        else HindsightDisabledClient()
    )
    recall = await deterministic_first_recall(
        entries=entries,
        query=query,
        bank_id=hindsight_bank_id(scope, contact_id),
        hindsight=recall_client,
        limit=limit,
        tags=hindsight_scope_tags(scope, contact_id),
    )
    if not recall.fuzzy:
        return recall

    # A provider response is eligible only when it names a local document for
    # this exact contact under the active RLS scope. Bank IDs and tags are both
    # required at the provider boundary; this is the final local backstop.
    document_ids = {hit.document_id for hit in recall.fuzzy if hit.document_id}
    batches: tuple[ContactMemoryBatch, ...] = ()
    if document_ids:
        batches = tuple(
            await session.scalars(
                select(ContactMemoryBatch).where(
                    ContactMemoryBatch.contact_id == contact_id,
                    ContactMemoryBatch.hindsight_document_id.in_(document_ids),
                )
            )
        )
    provenance_by_document = {batch.hindsight_document_id: batch for batch in batches}
    fuzzy = tuple(
        _fuzzy_hit_with_provenance(hit, provenance_by_document[hit.document_id])
        for hit in recall.fuzzy
        if hit.document_id is not None and hit.document_id in provenance_by_document
    )
    return replace(recall, fuzzy=fuzzy)


async def stage_contact_memory_deletion(
    session: AsyncSession,
    *,
    scope: TenantScope,
    contact_id: UUID,
    requested_by_user_id: UUID | None = None,
) -> StagedContactMemoryDeletion:
    """Delete derived CRM memory and queue the corresponding fuzzy-bank erase.

    Calls, transcripts, and the contact itself remain authoritative CRM data.
    Until the provider confirms the contact-only bank is gone, fuzzy recall is
    explicitly unavailable for this contact rather than risking stale text.
    """

    if await session.get(Contact, contact_id) is None:
        raise ContactMemoryNotFoundError("Contact not found")
    deterministic_entries_removed = int(
        await session.scalar(
            select(func.count())
            .select_from(ContactMemoryEntry)
            .where(ContactMemoryEntry.contact_id == contact_id)
        )
        or 0
    )
    source_batches_removed = int(
        await session.scalar(
            select(func.count())
            .select_from(ContactMemoryBatch)
            .where(ContactMemoryBatch.contact_id == contact_id)
        )
        or 0
    )
    await session.execute(
        delete(ContactMemoryBatch).where(ContactMemoryBatch.contact_id == contact_id)
    )
    bank_id = hindsight_bank_id(scope, contact_id)
    deletion = await _contact_memory_deletion(session, contact_id)
    if deletion is None:
        deletion = ContactMemoryDeletion(
            id=uuid4(),
            org_id=scope.org_id,
            department_id=scope.department_id,
            contact_id=contact_id,
            hindsight_bank_id=bank_id,
            status="pending",
            requested_by_user_id=requested_by_user_id,
        )
        session.add(deletion)
    else:
        if deletion.status == "delivered" and source_batches_removed == 0:
            # A repeated customer erase is idempotent. Do not turn a confirmed
            # provider deletion back into a failing request just to delete an
            # already-empty bank. New source batches do re-open the deletion.
            return StagedContactMemoryDeletion(
                deletion_id=deletion.id,
                deterministic_entries_removed=0,
                source_batches_removed=0,
                hindsight_bank_id=bank_id,
            )
        deletion.hindsight_bank_id = bank_id
        deletion.status = "pending"
        deletion.attempts = 0
        deletion.last_error = None
        deletion.requested_by_user_id = requested_by_user_id
        deletion.requested_at = datetime.now(UTC)
        deletion.delivered_at = None
    await session.flush()
    return StagedContactMemoryDeletion(
        deletion_id=deletion.id,
        deterministic_entries_removed=deterministic_entries_removed,
        source_batches_removed=source_batches_removed,
        hindsight_bank_id=bank_id,
    )


async def sync_hindsight_deletion(
    session: AsyncSession,
    *,
    scope: TenantScope,
    deletion_id: UUID,
    hindsight: HindsightMemoryClient,
) -> HindsightDeletionResult:
    """Deliver a contact-only fuzzy-bank erase after its CRM deletion commits."""

    deletion = await session.get(ContactMemoryDeletion, deletion_id)
    if deletion is None:
        raise ContactMemoryNotFoundError("Contact-memory deletion not found")
    if deletion.org_id != scope.org_id or deletion.department_id != scope.department_id:
        raise ContactMemoryNotFoundError("Contact-memory deletion not found")
    if deletion.status == "delivered":
        return HindsightDeletionResult(deletion.id, "already_deleted", deletion.attempts)
    try:
        await hindsight.delete_bank(bank_id=deletion.hindsight_bank_id)
    except HindsightUnavailableError:
        deletion.attempts += 1
        deletion.status = "failed"
        deletion.last_error = "Hindsight deletion unavailable; retry is safe."
        await session.flush()
        return HindsightDeletionResult(deletion.id, "failed", deletion.attempts)
    deletion.attempts += 1
    deletion.status = "delivered"
    deletion.last_error = None
    deletion.delivered_at = datetime.now(UTC)
    await session.flush()
    return HindsightDeletionResult(deletion.id, "deleted", deletion.attempts)


def _fuzzy_hit(hit: HindsightRecallHit) -> ContactMemoryRecallHit:
    return ContactMemoryRecallHit(
        text=_safe_fuzzy_text(hit.text),
        source="hindsight",
        label=HINDSIGHT_LABEL,
        memory_id=hit.memory_id,
        document_id=hit.document_id,
        confidence=hit.confidence,
    )


def _fuzzy_hit_with_provenance(
    hit: ContactMemoryRecallHit,
    batch: ContactMemoryBatch,
) -> ContactMemoryRecallHit:
    """Attach only locally verified source provenance to a fuzzy suggestion."""

    return replace(
        hit,
        source_call_id=batch.call_id,
        source_event_id=batch.source_event_id,
        source_occurred_at=batch.source_occurred_at,
        speaker=batch.speaker,
        extraction_provenance=batch.extraction_provenance,
    )


def _safe_fuzzy_text(value: str) -> str:
    """Bound and redact provider output before it reaches a voice/UI consumer."""

    return redact_for_hindsight(" ".join(value.split()))[:2_000]


async def _contact_memory_deletion(
    session: AsyncSession,
    contact_id: UUID,
) -> ContactMemoryDeletion | None:
    return await session.scalar(
        select(ContactMemoryDeletion).where(ContactMemoryDeletion.contact_id == contact_id)
    )


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


def _normalise_optional_text(value: str | None, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError(f"{field.capitalize()} may not exceed {max_length} characters")
    return normalized
