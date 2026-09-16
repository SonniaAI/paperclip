from __future__ import annotations

import enum
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Role(enum.StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class DepartmentShape(enum.StrEnum):
    SILENT = "silent"
    SHARED = "shared"
    PRIVATE = "private"


class DuplicateCallProtection(enum.StrEnum):
    BLOCK = "block"
    WARN = "warn"
    ALLOW = "allow"


def _enum_values(enum_type: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


ROLE_ENUM = SqlEnum(Role, native_enum=False, values_callable=_enum_values)
DEPARTMENT_SHAPE_ENUM = SqlEnum(DepartmentShape, native_enum=False, values_callable=_enum_values)
DUPLICATE_CALL_PROTECTION_ENUM = SqlEnum(
    DuplicateCallProtection,
    native_enum=False,
    values_callable=_enum_values,
)


class TenantScoped:
    """Columns required for every application table under Gate 0."""

    org_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    department_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)


class Organization(Base, TenantScoped):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("org_id = id", name="organizations_org_id_is_primary_key"),
        UniqueConstraint("org_id", name="organizations_org_id_key"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    login_slug: Mapped[str] = mapped_column(Text, unique=True)
    account_type: Mapped[str] = mapped_column(
        String(10), default="business", server_default="business"
    )
    country: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(20), nullable=True)
    company_website: Mapped[str | None] = mapped_column(Text, nullable=True)
    referral_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    duplicate_call_protection: Mapped[DuplicateCallProtection] = mapped_column(
        DUPLICATE_CALL_PROTECTION_ENUM,
        default=DuplicateCallProtection.WARN,
    )
    onboarding_state: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Company(Base, TenantScoped):
    """Company entity from the §15 feature model."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("org_id", "id", name="companies_org_id_id_key"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    legal_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Integration(Base, TenantScoped):
    """§4.5 integrations row (provider/kind, config, status)."""

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("org_id", "department_id", "provider", "kind"),
        CheckConstraint(
            "status IN ('active', 'paused', 'error', 'revoked')",
            name="integrations_status_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    provider: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    external_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    credentials_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Department(Base, TenantScoped):
    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="departments_org_id_id_key"),
        UniqueConstraint("org_id", "login_slug", name="departments_org_id_login_slug_key"),
        CheckConstraint("department_id = id", name="departments_department_id_is_primary_key"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", deferrable=True, initially="DEFERRED"),
        index=True,
    )
    name: Mapped[str] = mapped_column(Text)
    login_slug: Mapped[str] = mapped_column(Text)
    shape: Mapped[DepartmentShape] = mapped_column(DEPARTMENT_SHAPE_ENUM)
    is_silent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppUser(Base, TenantScoped):
    __tablename__ = "app_users"
    __table_args__ = (
        UniqueConstraint("id", "org_id", name="app_users_id_org_id_key"),
        UniqueConstraint("org_id", "email", name="app_users_org_id_email_key"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(80), nullable=True)
    profile_role: Mapped[str | None] = mapped_column(String(120), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    terms_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    marketing_consent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    marketing_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    marketing_consent_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    email_verification_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_reset_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    two_factor_challenge_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    two_factor_challenge_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    totp_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base, TenantScoped):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("id", "org_id", name="memberships_id_org_id_key"),
        UniqueConstraint("user_id", "org_id", name="memberships_user_id_org_id_key"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("app_users.id"))
    role: Mapped[Role] = mapped_column(ROLE_ENUM)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MembershipDepartment(Base, TenantScoped):
    __tablename__ = "membership_departments"
    __table_args__ = (UniqueConstraint("membership_id", "department_id"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    membership_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("memberships.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Invite(Base, TenantScoped):
    __tablename__ = "invites"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    email: Mapped[str] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(ROLE_ENUM)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSession(Base, TenantScoped):
    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("app_users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Call(Base, TenantScoped):
    __tablename__ = "calls"
    __table_args__ = (UniqueConstraint("org_id", "external_call_key"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    external_call_key: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    contact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
    company_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id"), nullable=True
    )
    direction: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="completed")
    telnyx_call_control_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    telnyx_call_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_phone_e164: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_phone_e164: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    call_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DoNotContact(Base, TenantScoped):
    __tablename__ = "do_not_contacts"
    __table_args__ = (UniqueConstraint("org_id", "phone_e164"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    phone_e164: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActiveDial(Base, TenantScoped):
    __tablename__ = "active_dials"
    __table_args__ = (UniqueConstraint("org_id", "phone_e164"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    phone_e164: Mapped[str] = mapped_column(Text)
    call_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("calls.id"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Recording(Base, TenantScoped):
    __tablename__ = "recordings"
    __table_args__ = (
        UniqueConstraint("org_id", "storage_bucket", "storage_key"),
        CheckConstraint("is_private = true", name="recordings_private_only"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    call_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("calls.id"))
    storage_bucket: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transcript(Base, TenantScoped):
    """Canonical call transcript; Hindsight only receives a redacted copy."""

    __tablename__ = "transcripts"
    __table_args__ = (UniqueConstraint("org_id", "call_id", name="transcripts_org_id_call_id_key"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    call_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("calls.id"))
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TelnyxWebhookEvent(Base, TenantScoped):
    __tablename__ = "telnyx_webhook_events"
    __table_args__ = (
        UniqueConstraint("event_id"),
        CheckConstraint(
            "status IN ('received', 'processing', 'processed', 'ignored', 'failed')",
            name="telnyx_webhook_events_status_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    event_id: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    raw_payload: Mapped[dict[str, object]] = mapped_column(JSON)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="received")
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Contact(Base, TenantScoped):
    """§11 CRM contact, tenant-scoped and RLS-forced by the feature migration."""

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("org_id", "id", name="contacts_org_id_id_key"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    company_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContactPhone(Base, TenantScoped):
    __tablename__ = "contact_phones"
    __table_args__ = (UniqueConstraint("org_id", "contact_id", "phone_e164"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    contact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("contacts.id"))
    phone_e164: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContactEmail(Base, TenantScoped):
    __tablename__ = "contact_emails"
    __table_args__ = (UniqueConstraint("org_id", "contact_id", "email"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    contact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("contacts.id"))
    email: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContactMemoryBatch(Base, TenantScoped):
    """One idempotent source write and its stable Hindsight document identity."""

    __tablename__ = "contact_memory_batches"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="contact_memory_batches_org_id_id_key"),
        UniqueConstraint(
            "org_id",
            "contact_id",
            "idempotency_key",
            name="contact_memory_batches_idempotency_key",
        ),
        UniqueConstraint(
            "org_id",
            "hindsight_document_id",
            name="contact_memory_batches_document_key",
        ),
        CheckConstraint(
            "source_kind IN ('manual', 'call_transcript')",
            name="contact_memory_batches_source_kind_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    contact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("contacts.id"))
    call_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("calls.id"), nullable=True
    )
    transcript_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("transcripts.id"), nullable=True
    )
    source_kind: Mapped[str] = mapped_column(String(20), default="manual")
    idempotency_key: Mapped[str] = mapped_column(String(200))
    hindsight_document_id: Mapped[str] = mapped_column(String(200))
    source_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    speaker: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extraction_provenance: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContactMemoryEntry(Base, TenantScoped):
    """A deterministic fact or preference used before any fuzzy fallback."""

    __tablename__ = "contact_memory_entries"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="contact_memory_entries_org_id_id_key"),
        CheckConstraint("kind IN ('fact', 'preference')", name="contact_memory_entries_kind_check"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("contact_memory_batches.id")
    )
    contact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("contacts.id"))
    kind: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HindsightSyncJob(Base, TenantScoped):
    """Durable post-commit Hindsight outbox row for a contact-memory batch."""

    __tablename__ = "hindsight_sync_jobs"
    __table_args__ = (
        UniqueConstraint("org_id", "batch_id", name="hindsight_sync_jobs_batch_key"),
        CheckConstraint(
            "status IN ('pending', 'failed', 'delivered')",
            name="hindsight_sync_jobs_status_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("contact_memory_batches.id")
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContactMemoryDeletion(Base, TenantScoped):
    """One contact-only fuzzy-bank erasure request and its durable outcome.

    Deterministic CRM rows are removed in the same transaction that records
    this job.  Until the provider confirms the bank deletion, recall refuses
    to ask Hindsight for that contact so stale fuzzy text can never reappear.
    """

    __tablename__ = "contact_memory_deletions"
    __table_args__ = (
        UniqueConstraint("org_id", "contact_id", name="contact_memory_deletions_contact_key"),
        CheckConstraint(
            "status IN ('pending', 'failed', 'delivered')",
            name="contact_memory_deletions_status_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    contact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("contacts.id"))
    hindsight_bank_id: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Note(Base, TenantScoped):
    __tablename__ = "notes"
    __table_args__ = (
        CheckConstraint("visibility IN ('private', 'shared')", name="notes_visibility_check"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
    call_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("calls.id"), nullable=True
    )
    author_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(10), default="shared")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TaskList(Base, TenantScoped):
    """§4.4 personal (private) or team (shared) task list.

    The SON-419 migration makes these lists personal-or-team; the owner is
    the user a private list belongs to, and shared lists are company team
    lists.
    """

    __tablename__ = "task_lists"
    __table_args__ = (
        UniqueConstraint("org_id", "department_id", "name"),
        CheckConstraint("visibility IN ('private', 'shared')", name="task_lists_visibility_check"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(String(10), default="private")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Task(Base, TenantScoped):
    """§4.4 task with private/assigned/shared visibility and markdown body."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'in_progress', 'done', 'cancelled')", name="tasks_status_check"
        ),
        CheckConstraint("visibility IN ('private', 'shared')", name="tasks_visibility_check"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    task_list_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("task_lists.id"), nullable=True
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
    call_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("calls.id"), nullable=True
    )
    owner_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    visibility: Mapped[str] = mapped_column(String(10), default="shared")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Instruction(Base, TenantScoped):
    """§12 editable instructions with version history + in-effect-since.

    The SON-419 migration adds ``slug``/``version``/``effective_from``/
    ``superseded_at`` and a partial unique index guaranteeing at most one
    active (non-superseded) version per topic within a tenant.
    """

    __tablename__ = "instructions"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    applies_to: Mapped[str] = mapped_column(Text, default="all")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    slug: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(default=1)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ActivityLogEntry(Base, TenantScoped):
    """§12 one chronological feed, privacy-respecting.

    Private entries (e.g. a private note/task event) are written with a
    ``private`` flag; the feed query hides them from anyone other than the
    actor.
    """

    __tablename__ = "activity_log"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    activity_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SecurityEvent(Base, TenantScoped):
    """§4.5 visible log of security events (login, TOTP, password, revoke)."""

    __tablename__ = "security_events"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContactImport(Base, TenantScoped):
    """§11 durable CSV import run with mapping/preview/dedupe/summary."""

    __tablename__ = "contact_imports"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id"), index=True
    )
    filename: Mapped[str] = mapped_column(Text)
    column_count: Mapped[int] = mapped_column(nullable=False)
    row_count: Mapped[int] = mapped_column(nullable=False)
    created_count: Mapped[int] = mapped_column(default=0)
    merged_count: Mapped[int] = mapped_column(default=0)
    skipped_count: Mapped[int] = mapped_column(default=0)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("app_users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())



class WebhookReceiptCapture(Base):
    """CP2 - append-only webhook receipt boundary capture (SON-1458).

    System-level audit row for every receipt at POST /webhooks/telnyx:
    raw body verbatim as received, credential headers redacted, receive
    timestamp, and the signature-verification decision.  Insert-only;
    intentionally not tenant-scoped (records pre-ingestion receipts).
    """

    __tablename__ = "webhook_receipt_capture"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source: Mapped[str] = mapped_column(String(40), default="telnyx")
    method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers_redacted: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    raw_body: Mapped[str] = mapped_column(Text)
    body_bytes: Mapped[int] = mapped_column(Integer)
    signature_algorithm: Mapped[str | None] = mapped_column(String(40), nullable=True)
    signature_result: Mapped[str] = mapped_column(String(20))
    signature_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)


class IngestionTraceCapture(Base):
    """CP3 - append-only ingestion trace (SON-1458).

    Idempotency/dedupe decision, event-to-entity mapping (call, transcript,
    recording refs), deployed build git SHA, and processing latency for one
    ingested webhook event.  Insert-only; links to CP2 via receipt_id.
    """

    __tablename__ = "ingestion_trace_capture"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    receipt_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("webhook_receipt_capture.id"),
        nullable=True,
        index=True,
    )
    event_id: Mapped[str] = mapped_column(Text, index=True)
    event_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    outcome: Mapped[str] = mapped_column(String(40))
    call_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    contact_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    transcript_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_refs: Mapped[list[object] | None] = mapped_column(JSON, nullable=True)
    build_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
