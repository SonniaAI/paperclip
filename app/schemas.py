from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models import Role


def _normalise_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("a valid email address is required")
    return email


def _normalise_slug(value: str) -> str:
    slug = value.strip().lower()
    allowed_characters = "abcdefghijklmnopqrstuvwxyz0123456789-"
    if not slug or any(character not in allowed_characters for character in slug):
        raise ValueError("slug may contain lowercase letters, numbers, and hyphens only")
    return slug


class RegisterRequest(BaseModel):
    organization_name: str = Field(min_length=2, max_length=160)
    organization_slug: str = Field(min_length=2, max_length=80)
    display_name: str = Field(min_length=1, max_length=160)
    email: str
    password: str = Field(min_length=12, max_length=512)

    _validate_email = field_validator("email")(_normalise_email)
    _validate_slug = field_validator("organization_slug")(_normalise_slug)


class LoginRequest(BaseModel):
    organization_slug: str = Field(min_length=2, max_length=80)
    department_slug: str = Field(default="default", min_length=2, max_length=80)
    email: str
    password: str = Field(min_length=1, max_length=512)
    totp_code: str | None = Field(default=None, min_length=6, max_length=6)

    _validate_email = field_validator("email")(_normalise_email)
    _validate_organization_slug = field_validator("organization_slug")(_normalise_slug)
    _validate_department_slug = field_validator("department_slug")(_normalise_slug)


class InviteCreateRequest(BaseModel):
    email: str
    role: Role = Role.MEMBER

    _validate_email = field_validator("email")(_normalise_email)


class InviteAcceptRequest(BaseModel):
    token: str = Field(min_length=20)
    display_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=12, max_length=512)


class TotpVerifyRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class AuthResponse(BaseModel):
    authenticated: bool = True


class TotpEnrollmentResponse(BaseModel):
    secret: str
    issuer: str = "manager.sonnia.ai"


class InviteResponse(BaseModel):
    invite_token: str
    expires_at: datetime


class CallResponse(BaseModel):
    subject: str
    created_at: datetime


class BillingTopUpPlaceholderResponse(BaseModel):
    label: str = "Add credit"
    action: str = "contact_us"
    enabled: bool = False


class InstructionCreateRequest(BaseModel):
    """Create the first version of an instruction topic."""

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    slug: str = Field(min_length=1, max_length=120)
    applies_to: str = Field(default="all", max_length=80)
    effective_from: datetime | None = None


class InstructionUpdateRequest(BaseModel):
    """Edit an instruction: always creates the next version."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str = Field(min_length=1)
    effective_from: datetime | None = None


class InstructionResponse(BaseModel):
    id: UUID
    title: str
    body: str
    slug: str
    version: int
    applies_to: str
    active: bool
    effective_from: datetime
    superseded_at: datetime | None = None
    created_at: datetime


class TaskListCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=400)
    visibility: str = Field(default="private", pattern="^(private|shared)$")


class TaskListResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    visibility: str
    owner_user_id: UUID | None = None
    created_at: datetime


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None)
    due_at: datetime | None = None
    task_list_id: UUID | None = None
    contact_id: UUID | None = None
    call_id: UUID | None = None
    visibility: str = Field(default="shared", pattern="^(private|shared)$")


class TaskUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(open|in_progress|done|cancelled)$")
    due_at: datetime | None = None
    task_list_id: UUID | None = None
    visibility: str | None = Field(default=None, pattern="^(private|shared)$")


class TaskResponse(BaseModel):
    id: UUID
    title: str
    description: str | None = None
    status: str
    visibility: str
    due_at: datetime | None = None
    task_list_id: UUID | None = None
    contact_id: UUID | None = None
    call_id: UUID | None = None
    owner_user_id: UUID | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime


class ActivityLogEntryResponse(BaseModel):
    id: UUID
    actor_user_id: UUID | None = None
    action: str
    entity_type: str
    entity_id: UUID | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class SecurityEventResponse(BaseModel):
    id: UUID
    event_type: str
    ip_address: str | None = None
    created_at: datetime
    metadata: dict[str, object] = Field(default_factory=dict)


class UserSessionResponse(BaseModel):
    id: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    current: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class ContactImportColumnMapping(BaseModel):
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    job_title: str | None = None
    email: str | None = None
    phone: str | None = None
    company: str | None = None


class ContactImportPreviewRow(BaseModel):
    index: int
    values: dict[str, str] = Field(default_factory=dict)


class ContactImportPreviewRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    columns: list[str] = Field(min_length=1)
    rows: list[ContactImportPreviewRow] = Field(default_factory=list)


class ContactImportPreviewResponse(BaseModel):
    mapping: ContactImportColumnMapping
    column_count: int
    row_count: int
    created_estimate: int
    merged_estimate: int
    skipped_estimate: int
    notes: list[str] = Field(default_factory=list)


class ContactImportCommitRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    columns: list[str] = Field(min_length=1)
    rows: list[ContactImportPreviewRow] = Field(default_factory=list)
    mapping: ContactImportColumnMapping


class ContactImportSummaryResponse(BaseModel):
    id: UUID
    filename: str
    column_count: int
    row_count: int
    created_count: int
    merged_count: int
    skipped_count: int
    created_at: datetime


class OnboardingProgressRequest(BaseModel):
    state: str = Field(pattern="^(pending|instructions|import|invite|complete)$")


class OnboardingProgressResponse(BaseModel):
    state: str
    next: str
    message: str


class ProfileSettingsResponse(BaseModel):
    display_name: str
    email: str
    totp_enabled: bool
    organization_name: str
    onboarding_state: str


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)


class CompanySettingsResponse(BaseModel):
    id: UUID
    name: str
    login_slug: str
    duplicate_call_protection: str
    onboarding_state: str
    department_count: int = 0


class CompanyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    duplicate_call_protection: str | None = Field(
        default=None, pattern="^(block|warn|allow)$"
    )


class TeamMemberResponse(BaseModel):
    id: UUID
    email: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime


class IntegrationResponse(BaseModel):
    id: UUID
    provider: str
    kind: str
    external_account_id: str | None = None
    status: str
    created_at: datetime


class BillingSettingsResponse(BaseModel):
    balance_label: str = "Top-up"
    action: str = "contact_us"
    enabled: bool = False


class OnboardingStateResponse(BaseModel):
    state: str
    next: str | None = None
    message: str = ""


class OnboardingAdvanceRequest(BaseModel):
    state: str = Field(pattern="^(pending|instructions|import|invite|complete)$")


class ActivityFeedEntry(BaseModel):
    id: UUID
    actor_user_id: UUID | None = None
    actor_display_name: str | None = None
    action: str
    entity_type: str
    entity_id: UUID | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class ActivityFeedResponse(BaseModel):
    items: list[ActivityFeedEntry]
    next_cursor: str | None = None


class TaskListViewResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    visibility: str
    owner_user_id: UUID | None = None
    task_count: int = 0
    created_at: datetime


class InstructionListResponse(BaseModel):
    items: list[InstructionResponse]


class InstructionHistoryEntry(InstructionResponse):
    superseded_at: datetime | None = None
