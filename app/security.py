from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from itsdangerous import BadData, URLSafeTimedSerializer

from app.config import Settings, get_settings
from app.database import TenantScope

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Create a salted scrypt password hash without storing a reversible secret."""

    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    encoded_salt = base64.urlsafe_b64encode(salt).decode("ascii")
    encoded_digest = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${encoded_salt}${encoded_digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, encoded_salt, expected = encoded.split("$", maxsplit=5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode("ascii"))
        expected_digest = base64.urlsafe_b64decode(expected.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_digest),
        )
    except (TypeError, ValueError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected_digest)


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_code(secret: str, counter: int, digits: int = 6) -> str:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded, casefold=True)
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def verify_totp(secret: str, code: str, *, at_time: int | None = None, window: int = 1) -> bool:
    """Verify RFC 6238-style SHA-1 TOTP codes with a small clock-skew window."""

    if not code.isdigit() or len(code) != 6:
        return False
    counter = int((time.time() if at_time is None else at_time) // 30)
    return any(
        hmac.compare_digest(_totp_code(secret, counter + offset), code)
        for offset in range(-window, window + 1)
    )


def totp_for_test(secret: str, *, at_time: int) -> str:
    """Deterministic helper kept separate from runtime verification."""

    return _totp_code(secret, at_time // 30)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionClaims:
    session_id: UUID
    user_id: UUID
    scope: TenantScope


@dataclass(frozen=True)
class InviteClaims:
    invite_id: UUID
    scope: TenantScope
    email: str


@dataclass(frozen=True)
class UserActionClaims:
    user_id: UUID
    scope: TenantScope


def _serializer(settings: Settings | None = None, *, purpose: str) -> URLSafeTimedSerializer:
    configured = settings or get_settings()
    return URLSafeTimedSerializer(configured.session_secret, salt=f"manager.{purpose}.v1")


def issue_session_cookie(
    *, session_id: UUID, user_id: UUID, scope: TenantScope, settings: Settings | None = None
) -> str:
    return _serializer(settings, purpose="session").dumps(
        {
            "sid": str(session_id),
            "uid": str(user_id),
            "org": str(scope.org_id),
            "dept": str(scope.department_id),
        }
    )


def decode_session_cookie(value: str, settings: Settings | None = None) -> SessionClaims:
    configured = settings or get_settings()
    try:
        data: dict[str, Any] = _serializer(configured, purpose="session").loads(
            value,
            max_age=configured.session_ttl_seconds,
        )
        return SessionClaims(
            session_id=UUID(data["sid"]),
            user_id=UUID(data["uid"]),
            scope=TenantScope(org_id=UUID(data["org"]), department_id=UUID(data["dept"])),
        )
    except (BadData, KeyError, ValueError, TypeError) as exc:
        raise ValueError("invalid session") from exc


def issue_invite_token(
    *, invite_id: UUID, scope: TenantScope, email: str, settings: Settings | None = None
) -> str:
    return _serializer(settings, purpose="invite").dumps(
        {
            "invite": str(invite_id),
            "org": str(scope.org_id),
            "dept": str(scope.department_id),
            "email": email.lower(),
        }
    )


def decode_invite_token(value: str, settings: Settings | None = None) -> InviteClaims:
    configured = settings or get_settings()
    try:
        data: dict[str, Any] = _serializer(configured, purpose="invite").loads(
            value,
            max_age=configured.invite_ttl_seconds,
        )
        return InviteClaims(
            invite_id=UUID(data["invite"]),
            scope=TenantScope(org_id=UUID(data["org"]), department_id=UUID(data["dept"])),
            email=str(data["email"]).lower(),
        )
    except (BadData, KeyError, ValueError, TypeError) as exc:
        raise ValueError("invalid or expired invite") from exc


def _issue_user_action_token(
    *,
    purpose: str,
    user_id: UUID,
    scope: TenantScope,
    settings: Settings | None = None,
) -> str:
    return _serializer(settings, purpose=purpose).dumps(
        {
            "uid": str(user_id),
            "org": str(scope.org_id),
            "dept": str(scope.department_id),
        }
    )


def _decode_user_action_token(
    value: str,
    *,
    purpose: str,
    max_age: int,
    settings: Settings | None = None,
) -> UserActionClaims:
    configured = settings or get_settings()
    try:
        data: dict[str, Any] = _serializer(configured, purpose=purpose).loads(
            value,
            max_age=max_age,
        )
        return UserActionClaims(
            user_id=UUID(data["uid"]),
            scope=TenantScope(
                org_id=UUID(data["org"]),
                department_id=UUID(data["dept"]),
            ),
        )
    except (BadData, KeyError, ValueError, TypeError) as exc:
        raise ValueError("invalid or expired token") from exc


def issue_email_verification_token(
    *, user_id: UUID, scope: TenantScope, settings: Settings | None = None
) -> str:
    return _issue_user_action_token(
        purpose="verify-email", user_id=user_id, scope=scope, settings=settings
    )


def decode_email_verification_token(
    value: str, settings: Settings | None = None
) -> UserActionClaims:
    configured = settings or get_settings()
    return _decode_user_action_token(
        value,
        purpose="verify-email",
        max_age=configured.email_verification_ttl_seconds,
        settings=configured,
    )


def issue_password_reset_token(
    *, user_id: UUID, scope: TenantScope, settings: Settings | None = None
) -> str:
    return _issue_user_action_token(
        purpose="reset-password", user_id=user_id, scope=scope, settings=settings
    )


def decode_password_reset_token(value: str, settings: Settings | None = None) -> UserActionClaims:
    configured = settings or get_settings()
    return _decode_user_action_token(
        value,
        purpose="reset-password",
        max_age=configured.password_reset_ttl_seconds,
        settings=configured,
    )


def issue_two_factor_challenge(
    *, user_id: UUID, scope: TenantScope, settings: Settings | None = None
) -> str:
    return _issue_user_action_token(
        purpose="two-factor", user_id=user_id, scope=scope, settings=settings
    )


def decode_two_factor_challenge(value: str, settings: Settings | None = None) -> UserActionClaims:
    configured = settings or get_settings()
    return _decode_user_action_token(
        value,
        purpose="two-factor",
        max_age=configured.two_factor_ttl_seconds,
        settings=configured,
    )


def session_expiry(settings: Settings | None = None) -> datetime:
    configured = settings or get_settings()
    return datetime.now(UTC) + timedelta(seconds=configured.session_ttl_seconds)


def invite_expiry(settings: Settings | None = None) -> datetime:
    configured = settings or get_settings()
    return datetime.now(UTC) + timedelta(seconds=configured.invite_ttl_seconds)
