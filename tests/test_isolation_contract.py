from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database import TenantScope
from app.main import AuthenticatedContext, app, authenticated_context, get_call

ROOT = Path(__file__).resolve().parents[1]
RLS_SQL = (ROOT / "alembic/versions/20260809_gate0_rls.sql").read_text(encoding="utf-8")
ROUTE_SOURCE = (ROOT / "app/main.py").read_text(encoding="utf-8")

TENANT_TABLES = (
    "organizations",
    "departments",
    "app_users",
    "memberships",
    "membership_departments",
    "invites",
    "user_sessions",
    "calls",
    "do_not_contacts",
    "active_dials",
)


def test_every_application_table_has_org_department_and_forced_rls() -> None:
    for table in TENANT_TABLES:
        table_definition = RLS_SQL.split(f"CREATE TABLE {table} (", 1)[1].split(");", 1)[0]
        assert "org_id uuid NOT NULL" in table_definition
        assert "department_id uuid NOT NULL" in table_definition
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in RLS_SQL
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in RLS_SQL


def test_call_route_relies_on_database_rls_not_an_untrusted_tenant_filter() -> None:
    assert "select(Call).where(Call.id == call_id)" in ROUTE_SOURCE
    route_body = ROUTE_SOURCE.split("async def get_call(", 1)[1]
    assert "Call.org_id" not in route_body
    assert "Call.department_id" not in route_body


class RlsFilteredSession:
    """Represents the PostgreSQL result for a cross-org call lookup."""

    async def scalar(self, _statement: object) -> None:
        return None


@pytest.mark.asyncio
async def test_cross_org_call_is_404_after_rls_hides_the_row() -> None:
    context = AuthenticatedContext(
        session=RlsFilteredSession(),  # type: ignore[arg-type]
        user=None,  # type: ignore[arg-type]
        scope=TenantScope(org_id=uuid4(), department_id=uuid4()),
    )

    with pytest.raises(HTTPException) as error:
        await get_call(uuid4(), context)

    assert error.value.status_code == 404
    assert error.value.detail == "Call not found"


def test_org_a_user_receives_http_404_for_org_b_call_id() -> None:
    """The request-layer assertion paired with the real PostgreSQL RLS proof."""

    async def rls_filtered_org_a_context() -> AuthenticatedContext:
        return AuthenticatedContext(
            session=RlsFilteredSession(),  # type: ignore[arg-type]
            user=None,  # type: ignore[arg-type]
            scope=TenantScope(org_id=uuid4(), department_id=uuid4()),
        )

    app.dependency_overrides[authenticated_context] = rls_filtered_org_a_context
    try:
        with TestClient(app) as client:
            response = client.get(f"/api/calls/{uuid4()}")
    finally:
        app.dependency_overrides.pop(authenticated_context, None)

    assert response.status_code == 404
    assert response.json() == {"detail": "Call not found"}
