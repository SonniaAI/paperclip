from __future__ import annotations

from uuid import uuid4

import pytest

from app.config import Settings
from app.database import TenantScope
from app.security import (
    decode_invite_token,
    decode_session_cookie,
    hash_password,
    issue_invite_token,
    issue_session_cookie,
    new_totp_secret,
    totp_for_test,
    verify_password,
    verify_totp,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(session_secret="test-signing-secret-that-is-long-enough")


def test_scrypt_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password("correct-horse-battery-staple")
    second = hash_password("correct-horse-battery-staple")

    assert first != second
    assert verify_password("correct-horse-battery-staple", first)
    assert not verify_password("wrong-password", first)
    assert not verify_password("correct-horse-battery-staple", "not-a-hash")


def test_totp_accepts_current_window_but_not_unrelated_code() -> None:
    secret = new_totp_secret()
    fixed_time = 1_726_000_000
    code = totp_for_test(secret, at_time=fixed_time)

    assert verify_totp(secret, code, at_time=fixed_time)
    assert verify_totp(secret, code, at_time=fixed_time + 30)
    assert not verify_totp(secret, "000000", at_time=fixed_time, window=0)


def test_signed_session_claims_cannot_be_tampered_with(settings: Settings) -> None:
    scope = TenantScope(org_id=uuid4(), department_id=uuid4())
    cookie = issue_session_cookie(
        session_id=uuid4(), user_id=uuid4(), scope=scope, settings=settings
    )

    claims = decode_session_cookie(cookie, settings)
    assert claims.scope == scope
    with pytest.raises(ValueError):
        decode_session_cookie(f"{cookie}tampered", settings)


def test_signed_invite_carries_only_its_own_tenant_scope(settings: Settings) -> None:
    scope = TenantScope(org_id=uuid4(), department_id=uuid4())
    token = issue_invite_token(
        invite_id=uuid4(), scope=scope, email="member@example.com", settings=settings
    )

    claims = decode_invite_token(token, settings)
    assert claims.scope == scope
    assert claims.email == "member@example.com"
