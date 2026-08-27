"""SON-1363 contract tests for POST /auth/verification/pending.

The endpoint is the FE check-inbox probe: it must answer whether an address
still needs email verification with an error-free envelope that does not leak
address existence, and it must never mutate state (no token rotation, no resend,
no rate-limit writes). These tests pin the route contract and the handler's
decision table against a fake session, mirroring the real RLS call pattern
(resolve_auth_scope -> set_tenant_scope -> db.get) without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.database import TenantScope, get_session
from app.main import _resolve_auth_scope, app, verification_pending
from app.models import AppUser
from app.schemas import VerificationPendingResponse

ENDPOINT = "/auth/verification/pending"


class _FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def mappings(self) -> _FakeResult:
        return self

    def one_or_none(self) -> Any:
        return self._row


class _FakeBegin:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Minimal AsyncSession stand-in recording every statement it is asked to run."""

    def __init__(self, row: Any = None, user: AppUser | None = None) -> None:
        self.row = row
        self.user = user
        self.statements: list[str] = []
        self.flushed = False

    def begin(self) -> _FakeBegin:
        return _FakeBegin(self)

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        self.statements.append(str(statement))
        return _FakeResult(self.row)

    async def get(self, model: type, pk: Any) -> AppUser | None:
        return self.user

    async def flush(self) -> None:  # pragma: no cover - must never be called
        self.flushed = True


def _install(session: _FakeSession) -> None:
    async def _override() -> Any:
        yield session

    app.dependency_overrides[get_session] = _override


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.pop(get_session, None)


def _user(*, verified: bool, active: bool = True) -> AppUser:
    return AppUser(
        id=uuid4(),
        org_id=uuid4(),
        department_id=uuid4(),
        email="pending-user@example.com",
        display_name="Pending User",
        password_hash="x",
        is_active=active,
        email_verified_at=None if not verified else datetime.now(UTC),
    )


def test_route_is_registered_with_response_model() -> None:
    paths = [
        (getattr(r, "path", ""), list(getattr(r, "methods", []) or []))
        for r in app.routes
        if hasattr(r, "path")
    ]
    assert (ENDPOINT, ["POST"]) in paths
    assert verification_pending.__name__ == "verification_pending"


def test_response_model_exposes_exactly_the_published_envelope() -> None:
    fields = set(VerificationPendingResponse.model_fields)
    assert fields == {"pending", "resend_available_in_seconds"}


@pytest.mark.parametrize("payload", [{"email": "not-an-email"}, {}, {"email": ""}])
def test_invalid_email_fails_validation_like_resend(payload: dict[str, str]) -> None:
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(ENDPOINT, json=payload)
    assert response.status_code == 422


def test_unknown_address_is_error_free_and_non_pending() -> None:
    session = _FakeSession(row=None)  # resolve_auth_scope finds nothing
    _install(session)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(ENDPOINT, json={"email": "ghost@example.com"})

    assert response.status_code == 200
    assert response.json() == {"pending": False, "resend_available_in_seconds": 0}
    assert session.flushed is False


def test_unverified_active_address_reports_pending() -> None:
    scope = TenantScope(org_id=uuid4(), department_id=uuid4())
    row = {"org_id": scope.org_id, "department_id": scope.department_id, "user_id": uuid4()}
    session = _FakeSession(row=row, user=_user(verified=False))
    _install(session)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(ENDPOINT, json={"email": "pending-user@example.com"})

    assert response.status_code == 200
    assert response.json() == {"pending": True, "resend_available_in_seconds": 0}
    assert session.flushed is False


@pytest.mark.parametrize("verified,active", [(True, True), (False, False), (True, False)])
def test_verified_inactive_and_missing_users_are_indistinguishable(
    verified: bool, active: bool
) -> None:
    scope = TenantScope(org_id=uuid4(), department_id=uuid4())
    row = {"org_id": scope.org_id, "department_id": scope.department_id, "user_id": uuid4()}
    session = _FakeSession(row=row, user=_user(verified=verified, active=active))
    _install(session)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(ENDPOINT, json={"email": "someone@example.com"})

    assert response.status_code == 200
    assert response.json() == {"pending": False, "resend_available_in_seconds": 0}


def test_missing_user_row_with_resolved_scope_is_non_pending() -> None:
    scope = TenantScope(org_id=uuid4(), department_id=uuid4())
    row = {"org_id": scope.org_id, "department_id": scope.department_id, "user_id": uuid4()}
    session = _FakeSession(row=row, user=None)
    _install(session)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(ENDPOINT, json={"email": "vanishing-user@example.com"})

    assert response.status_code == 200
    assert response.json() == {"pending": False, "resend_available_in_seconds": 0}


def test_probe_writes_nothing_to_the_session() -> None:
    session = _FakeSession(row=None)
    _install(session)

    with TestClient(app, base_url="https://testserver") as client:
        client.post(ENDPOINT, json={"email": "ghost@example.com"})

    # Only the RLS scope SELECTs (set_config) and the resolve_auth_scope read
    # may run; any INSERT/UPDATE/DELETE would be a state mutation regression.
    for statement in session.statements:
        lowered = statement.lower()
        assert not any(
            keyword in lowered for keyword in ("insert into", "update ", "delete from")
        ), statement


def test_resolve_auth_scope_is_reused_not_duplicated() -> None:
    """Guard against a parallel email->scope implementation drifting from auth."""

    import inspect

    source = inspect.getsource(verification_pending)
    assert "_resolve_auth_scope" in source
    assert _resolve_auth_scope.__name__ == "_resolve_auth_scope"
