from __future__ import annotations

from datetime import datetime

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
