from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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
    duplicate_call_protection: Mapped[DuplicateCallProtection] = mapped_column(
        DUPLICATE_CALL_PROTECTION_ENUM,
        default=DuplicateCallProtection.WARN,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    password_hash: Mapped[str] = mapped_column(Text)
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
