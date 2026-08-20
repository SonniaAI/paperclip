from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import Role


def _normalise_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("a valid email address is required")
    return email


RegistrationGender = Literal["female", "male", "other", "prefer_not_to_say"]
RegistrationRole = Literal[
    "owner_founder",
    "director_c_level",
    "sales_manager",
    "salesperson",
    "marketing",
    "operations",
    "customer_service",
    "administrator",
    "other",
]
RegistrationIndustry = Literal[
    "automotive",
    "beauty_wellness",
    "construction_trades",
    "education_training",
    "energy_utilities",
    "financial_services",
    "healthcare_medical",
    "hospitality_events",
    "insurance",
    "legal",
    "logistics_transport",
    "manufacturing",
    "marketing_advertising",
    "non_profit",
    "professional_services",
    "property_real_estate",
    "recruitment_staffing",
    "retail_ecommerce",
    "software_technology",
    "solar_renewable_energy",
    "telecommunications",
    "travel_tourism",
    "other",
]
RegistrationCompanySize = Literal["just_me", "2_10", "11_50", "51_200", "201_500", "500_plus"]
RegistrationReferralSource = Literal[
    "search", "social_media", "word_of_mouth", "event", "press", "other"
]


def _normalise_required_text(value: str) -> str:
    normalised = value.strip()
    if not normalised:
        raise ValueError("a value is required")
    return normalised


def _normalise_phone(value: str) -> str:
    """Validate and normalise a phone number to E.164 via libphonenumber.

    The Registration & Password spec §2, Section 2 requires libphonenumber
    validation (never a hand-rolled regex).
    """

    from phonenumbers import (
        NumberParseException,
        PhoneNumberFormat,
        format_number,
        is_valid_number,
        parse,
    )

    stripped = value.strip()
    if not stripped:
        raise ValueError("a phone number is required")
    try:
        parsed = parse(stripped, region=None)
    except NumberParseException as exc:
        raise ValueError("phone must use international format, for example +6581234567") from exc
    if not is_valid_number(parsed):
        raise ValueError("phone must use a valid international number, for example +6581234567")
    return format_number(parsed, PhoneNumberFormat.E164)


class RegisterRequest(BaseModel):
    account_type: Literal["individual", "business"]
    full_name: str = Field(min_length=1, max_length=160)
    email: str
    password: str = Field(min_length=10, max_length=512)
    company_name: str | None = Field(default=None, min_length=2, max_length=160)
    country: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=8, max_length=32)
    date_of_birth: date
    gender: RegistrationGender | None = None
    role: RegistrationRole
    industry: RegistrationIndustry
    company_size: RegistrationCompanySize | None = None
    company_website: str | None = Field(default=None, max_length=2048)
    referral_source: RegistrationReferralSource | None = None
    terms_version: str = Field(min_length=1, max_length=40)
    marketing_consent: bool

    _validate_email = field_validator("email")(_normalise_email)
    _validate_full_name = field_validator("full_name")(_normalise_required_text)
    _validate_phone = field_validator("phone")(_normalise_phone)
    _validate_country = field_validator("country")(_normalise_required_text)
    _validate_terms_version = field_validator("terms_version")(_normalise_required_text)

    @field_validator("date_of_birth")
    @classmethod
    def validate_date_of_birth(cls, value: date) -> date:
        today = date.today()
        eighteenth_birthday = value.replace(year=value.year + 18)
        if eighteenth_birthday > today:
            raise ValueError("you must be 18 or over to create an account")
        return value

    @field_validator("company_website")
    @classmethod
    def validate_company_website(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = value.strip()
        if not normalised:
            return None
        parsed = urlparse(normalised)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("company website must be a valid http or https URL")
        return normalised

    @model_validator(mode="after")
    def validate_account_fields(self) -> RegisterRequest:
        self.full_name = self.full_name.strip()
        self.country = self.country.strip()
        self.terms_version = self.terms_version.strip()
        if self.account_type == "business":
            if not self.company_name or not self.company_name.strip():
                raise ValueError("company name is required for a business account")
            if not self.company_size:
                raise ValueError("company size is required for a business account")
            self.company_name = self.company_name.strip()
        else:
            self.company_name = None
            self.company_size = None
        return self


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=512)

    _validate_email = field_validator("email")(_normalise_email)


class TwoFactorLoginRequest(BaseModel):
    challenge_token: str = Field(min_length=20)
    code: str = Field(pattern=r"^\d{6}$")


class EmailAddressRequest(BaseModel):
    email: str

    _validate_email = field_validator("email")(_normalise_email)


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=20)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=20)
    new_password: str = Field(min_length=10, max_length=512)


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


class AuthUserResponse(BaseModel):
    name: str
    email: str
    company: str
    department: str


class AuthResponse(BaseModel):
    authenticated: bool = True
    requires_2fa: bool = False
    user: AuthUserResponse | None = None
    redirect_to: str = "/"


class LoginChallengeResponse(BaseModel):
    authenticated: bool = False
    requires_2fa: bool = True
    challenge_token: str


class EmailDeliveryResponse(BaseModel):
    message: str
    delivery: Literal["smtp", "development", "failed"]
    development_url: str | None = None


class RegisterResponse(EmailDeliveryResponse):
    email: str


class TotpEnrollmentResponse(BaseModel):
    secret: str
    issuer: str = "manager.sonnia.ai"


class InviteResponse(BaseModel):
    invite_token: str
    expires_at: datetime


class CallResponse(BaseModel):
    subject: str
    created_at: datetime


class VoiceMemoryRecallRequest(BaseModel):
    """One grounded contact-memory question from the authenticated voice agent."""

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class VoiceMemoryRecallHitResponse(BaseModel):
    text: str
    source: Literal["sonnia_crm", "hindsight"]
    label: str
    kind: Literal["fact", "preference"] | None = None
    entry_id: UUID | None = None
    memory_id: str | None = None
    document_id: str | None = None
    confidence: float | None = None
    source_call_id: UUID | None = None
    source_event_id: str | None = None
    source_occurred_at: datetime | None = None
    speaker: str | None = None
    extraction_provenance: str | None = None


class VoiceMemoryRecallResponse(BaseModel):
    deterministic: list[VoiceMemoryRecallHitResponse] = Field(default_factory=list)
    fuzzy: list[VoiceMemoryRecallHitResponse] = Field(default_factory=list)
    used_hindsight: bool
    hindsight_status: Literal["not_needed", "returned", "unavailable"]


class ContactMemoryDeleteResponse(BaseModel):
    """A customer-requested erase result without exposing deleted source text."""

    deterministic_entries_removed: int
    source_batches_removed: int
    fuzzy_status: Literal["deleted", "unavailable", "already_deleted"]
    attempts: int


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
    phone: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    role: str | None = None
    industry: str | None = None
    country: str | None = None
    company_size: str | None = None
    company_website: str | None = None
    referral_source: str | None = None
    terms_version: str | None = None
    terms_accepted_at: datetime | None = None
    marketing_consent: bool = False
    marketing_consent_at: datetime | None = None
    marketing_consent_source: str | None = None


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    marketing_consent: bool | None = None


class CompanySettingsResponse(BaseModel):
    id: UUID
    name: str
    login_slug: str
    duplicate_call_protection: str
    onboarding_state: str
    department_count: int = 0


class CompanyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    duplicate_call_protection: str | None = Field(default=None, pattern="^(block|warn|allow)$")


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
