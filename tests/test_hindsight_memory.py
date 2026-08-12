"""Focused contract tests for the deterministic-first Hindsight seam."""

from __future__ import annotations

from datetime import UTC, datetime
from inspect import signature
from pathlib import Path
from uuid import uuid4

import pytest

from app.contact_memory import (
    HINDSIGHT_LABEL,
    DeterministicMemoryEntry,
    MemoryKind,
    build_hindsight_document,
    deterministic_first_recall,
    hindsight_bank_id,
    hindsight_scope_tags,
)
from app.database import TenantScope
from app.hindsight import HindsightRecallHit, HindsightSDKClient, HindsightUnavailableError

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_SQL = (ROOT / "alembic/versions/20260810_hindsight_memory.sql").read_text(
    encoding="utf-8"
)
MIGRATION_PY = (ROOT / "alembic/versions/20260810_hindsight_memory.py").read_text(encoding="utf-8")
CUSTOMER_MEMORY_SQL = (
    ROOT / "alembic/versions/20260812_son551_customer_memory.sql"
).read_text(encoding="utf-8")
CUSTOMER_MEMORY_PY = (
    ROOT / "alembic/versions/20260812_son551_customer_memory.py"
).read_text(encoding="utf-8")


class FakeHindsight:
    def __init__(
        self,
        *,
        hits: tuple[HindsightRecallHit, ...] = (),
        unavailable: bool = False,
    ) -> None:
        self.hits = hits
        self.unavailable = unavailable
        self.recall_calls: list[tuple[str, str, int]] = []
        self.retain_calls: list[tuple[str, str, str, dict[str, str], tuple[str, ...]]] = []

    async def retain(
        self,
        *,
        bank_id: str,
        document_id: str,
        content: str,
        metadata: dict[str, str],
        tags: tuple[str, ...] = (),
    ) -> None:
        if self.unavailable:
            raise HindsightUnavailableError("provider unavailable")
        self.retain_calls.append((bank_id, document_id, content, metadata, tags))

    async def recall(
        self,
        *,
        bank_id: str,
        query: str,
        limit: int,
        tags: tuple[str, ...] = (),
    ) -> tuple[HindsightRecallHit, ...]:
        self.recall_calls.append((bank_id, query, limit))
        if self.unavailable:
            raise HindsightUnavailableError("provider unavailable")
        return self.hits

    async def delete_bank(self, *, bank_id: str) -> None:
        if self.unavailable:
            raise HindsightUnavailableError("provider unavailable")


class FakeHindsightSDK:
    """Records the actual async SDK invocation shape without a network call."""

    def __init__(self) -> None:
        self.aretain_calls: list[dict[str, object]] = []

    async def aretain(self, **kwargs: object) -> None:
        self.aretain_calls.append(kwargs)


class FakeRecallHindsightSDK:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.arecall_calls: list[dict[str, object]] = []

    async def arecall(self, **kwargs: object) -> list[object]:
        self.arecall_calls.append(kwargs)
        if self.timeout:
            raise TimeoutError("controlled provider timeout")
        return []


def _scope() -> TenantScope:
    return TenantScope(org_id=uuid4(), department_id=uuid4())


def _entry(kind: MemoryKind, value: str) -> DeterministicMemoryEntry:
    return DeterministicMemoryEntry(
        id=uuid4(),
        kind=kind,
        value=value,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_deterministic_match_skips_hindsight() -> None:
    contact_id = uuid4()
    hindsight = FakeHindsight(hits=(HindsightRecallHit(text="This must never be requested"),))

    result = await deterministic_first_recall(
        entries=(_entry("preference", "Prefers email for status updates."),),
        query="email status",
        bank_id=hindsight_bank_id(_scope(), contact_id),
        hindsight=hindsight,
    )

    assert result.used_hindsight is False
    assert result.hindsight_status == "not_needed"
    assert hindsight.recall_calls == []
    assert result.fuzzy == ()
    assert result.deterministic[0].source == "sonnia_crm"
    assert result.deterministic[0].label == "Deterministic CRM record"
    assert result.deterministic[0].kind == "preference"


@pytest.mark.asyncio
async def test_empty_deterministic_match_uses_labeled_hindsight_fallback() -> None:
    fuzzy = HindsightRecallHit(
        text="They mentioned a Tuesday tennis league.",
        memory_id="memory-123",
        document_id="transcript-456",
    )
    hindsight = FakeHindsight(hits=(fuzzy,))

    result = await deterministic_first_recall(
        entries=(_entry("fact", "Works from the Singapore office."),),
        query="What sport do they play?",
        bank_id="bank-for-contact",
        hindsight=hindsight,
    )

    assert result.used_hindsight is True
    assert result.hindsight_status == "returned"
    assert hindsight.recall_calls == [("bank-for-contact", "What sport do they play?", 5)]
    assert result.deterministic == ()
    assert result.fuzzy[0].source == "hindsight"
    assert result.fuzzy[0].label == HINDSIGHT_LABEL
    assert result.fuzzy[0].memory_id == "memory-123"
    assert result.fuzzy[0].document_id == "transcript-456"


@pytest.mark.asyncio
async def test_stopword_only_overlap_uses_hindsight_fallback() -> None:
    """An unrelated natural-language question must not suppress fuzzy recall."""

    hindsight = FakeHindsight(hits=(HindsightRecallHit(text="They requested a renewal quote."),))

    result = await deterministic_first_recall(
        entries=(_entry("preference", "They prefer email."),),
        query="What did they say about renewal?",
        bank_id="bank-for-contact",
        hindsight=hindsight,
    )

    assert result.deterministic == ()
    assert result.used_hindsight is True
    assert result.hindsight_status == "returned"
    assert hindsight.recall_calls == [
        ("bank-for-contact", "What did they say about renewal?", 5)
    ]


@pytest.mark.asyncio
async def test_unavailable_hindsight_does_not_break_the_contact_read() -> None:
    hindsight = FakeHindsight(unavailable=True)

    result = await deterministic_first_recall(
        entries=(),
        query="What did they say about renewal?",
        bank_id="bank-for-contact",
        hindsight=hindsight,
    )

    assert result.deterministic == ()
    assert result.fuzzy == ()
    assert result.used_hindsight is True
    assert result.hindsight_status == "unavailable"
    assert result.fuzzy_available is False


@pytest.mark.asyncio
async def test_retries_keep_one_stable_hindsight_document_id() -> None:
    scope = _scope()
    contact_id = uuid4()
    batch_id = uuid4()
    document = build_hindsight_document(
        scope=scope,
        contact_id=contact_id,
        batch_id=batch_id,
        transcript_text=None,
        entries=(_entry("fact", "Asked for a renewal proposal."),),
    )
    retry = build_hindsight_document(
        scope=scope,
        contact_id=contact_id,
        batch_id=batch_id,
        transcript_text=None,
        entries=(_entry("fact", "Asked for a renewal proposal."),),
    )
    hindsight = FakeHindsight()

    await hindsight.retain(
        bank_id=document.bank_id,
        document_id=document.document_id,
        content=document.content,
        metadata=document.metadata,
        tags=document.tags,
    )
    await hindsight.retain(
        bank_id=retry.bank_id,
        document_id=retry.document_id,
        content=retry.content,
        metadata=retry.metadata,
        tags=retry.tags,
    )

    assert document.document_id == retry.document_id
    assert [call[1] for call in hindsight.retain_calls] == [document.document_id] * 2


@pytest.mark.asyncio
async def test_sdk_retain_uses_hindsight_replace_mode_for_retry_safe_upserts() -> None:
    sdk = FakeHindsightSDK()
    client = HindsightSDKClient(
        base_url="https://hindsight.example.test",
        api_key="test-key",
        timeout_seconds=10,
        sdk_client=sdk,
    )

    await client.retain(
        bank_id="contact-bank",
        document_id="stable-document-id",
        content="redacted contact memory",
        metadata={"contact_id": "contact-1"},
        tags=("sonnia:contact:contact-1",),
    )

    assert sdk.aretain_calls == [
        {
            "bank_id": "contact-bank",
            "content": "redacted contact memory",
            "context": "Sonnia CRM contact-memory copy",
            "document_id": "stable-document-id",
            "metadata": {"contact_id": "contact-1"},
            "tags": ["sonnia:contact:contact-1"],
            "update_mode": "replace",
        }
    ]


@pytest.mark.asyncio
async def test_sdk_recall_requires_strict_contact_scope_tags() -> None:
    sdk = FakeRecallHindsightSDK()
    client = HindsightSDKClient(
        base_url="https://hindsight.example.test",
        api_key="test-key",
        timeout_seconds=10,
        sdk_client=sdk,
    )

    result = await client.recall(
        bank_id="contact-only-bank",
        query="When should I ring them?",
        limit=3,
        tags=("sonnia:org:one", "sonnia:contact:two"),
    )

    assert result == []
    assert sdk.arecall_calls == [
        {
            "bank_id": "contact-only-bank",
            "query": "When should I ring them?",
            "tags": ["sonnia:org:one", "sonnia:contact:two"],
            "tags_match": "all_strict",
        }
    ]


@pytest.mark.asyncio
async def test_sdk_timeout_becomes_a_safe_unavailable_result() -> None:
    sdk = FakeRecallHindsightSDK(timeout=True)
    client = HindsightSDKClient(
        base_url="https://hindsight.example.test",
        api_key="test-key",
        timeout_seconds=10,
        sdk_client=sdk,
    )

    with pytest.raises(HindsightUnavailableError, match="Hindsight recall failed"):
        await client.recall(
            bank_id="contact-only-bank",
            query="When should I ring them?",
            limit=3,
            tags=("sonnia:contact:two",),
        )


def test_installed_hindsight_sdk_exposes_the_replace_contract() -> None:
    """Pin the supported SDK surface instead of assuming a stable document ID is enough."""

    from hindsight_client import Hindsight

    assert "update_mode" in signature(Hindsight.aretain).parameters


def test_hindsight_document_includes_memory_and_redacts_direct_contact_data() -> None:
    scope = _scope()
    contact_id = uuid4()
    document = build_hindsight_document(
        scope=scope,
        contact_id=contact_id,
        batch_id=uuid4(),
        transcript_text=(
            "Customer: Reach me at alice@example.com or +65 9000 0001. I prefer quarterly calls."
        ),
        entries=(
            _entry("fact", "Primary email is alice@example.com."),
            _entry("preference", "Prefers SMS at +65 9000 0001 for urgent updates."),
        ),
    )

    assert "Call transcript (redacted):" in document.content
    assert "Validated facts:" in document.content
    assert "Validated preferences:" in document.content
    assert "alice@example.com" not in document.content
    assert "+65 9000 0001" not in document.content
    assert document.content.count("[redacted-email]") == 2
    assert document.content.count("[redacted-phone]") == 2
    assert document.tags == hindsight_scope_tags(scope, contact_id)
    assert document.tags[-1] == f"sonnia:contact:{contact_id}"


@pytest.mark.asyncio
async def test_fuzzy_output_is_bounded_redacted_and_kept_separate() -> None:
    hindsight = FakeHindsight(
        hits=(
            HindsightRecallHit(
                text="Email alice@example.com or +65 9000 0001. " + ("x" * 2_100),
                confidence=0.8,
            ),
        )
    )

    result = await deterministic_first_recall(
        entries=(),
        query="How should we contact them?",
        bank_id="bank-for-contact",
        hindsight=hindsight,
    )

    assert result.deterministic == ()
    assert len(result.fuzzy[0].text) == 2_000
    assert "alice@example.com" not in result.fuzzy[0].text
    assert "+65 9000 0001" not in result.fuzzy[0].text
    assert result.fuzzy[0].confidence == 0.8


def test_migration_makes_memory_rows_tenant_scoped_idempotent_and_retryable() -> None:
    for table in ("contact_memory_batches", "contact_memory_entries", "hindsight_sync_jobs"):
        definition = MIGRATION_SQL.split(f"CREATE TABLE {table} (", 1)[1].split(");", 1)[0]
        assert "org_id uuid NOT NULL" in definition
        assert "department_id uuid NOT NULL" in definition
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in MIGRATION_SQL
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in MIGRATION_SQL
    assert "UNIQUE (org_id, contact_id, idempotency_key)" in MIGRATION_SQL
    assert "UNIQUE (org_id, batch_id)" in MIGRATION_SQL
    assert "Hindsight unavailable; retry is safe." not in MIGRATION_SQL
    assert 'down_revision = ("20260809_demo_seed", "20260809_son419")' in MIGRATION_PY


def test_customer_memory_migration_adds_tenant_scoped_erasure_and_provenance() -> None:
    definition = CUSTOMER_MEMORY_SQL.split("CREATE TABLE contact_memory_deletions (", 1)[1].split(
        ");", 1
    )[0]
    assert "org_id uuid NOT NULL" in definition
    assert "department_id uuid NOT NULL" in definition
    assert "UNIQUE (org_id, contact_id)" in definition
    assert "ALTER TABLE contact_memory_deletions ENABLE ROW LEVEL SECURITY;" in CUSTOMER_MEMORY_SQL
    assert "ALTER TABLE contact_memory_deletions FORCE ROW LEVEL SECURITY;" in CUSTOMER_MEMORY_SQL
    for column in (
        "source_event_id varchar(200)",
        "source_occurred_at timestamptz",
        "speaker varchar(100)",
        "extraction_provenance varchar(200)",
    ):
        assert column in CUSTOMER_MEMORY_SQL
    assert 'down_revision = "20260811_auth_claims"' in CUSTOMER_MEMORY_PY
