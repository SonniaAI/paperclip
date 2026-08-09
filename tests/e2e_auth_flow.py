"""Ephemeral PostgreSQL-wire integration check invoked by e2e_auth_flow.mjs.

PGlite's socket adapter always starts a connection as its internal postgres
role. The test-only scope wrapper immediately sets the real runtime role before
any RLS query, matching a normal deployment where the TCP connection is already
authenticated as ``manager_app``. It also disables asyncpg's prepared-statement
cache because the adapter multiplexes one PostgreSQL connection.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import time
import traceback
from uuid import uuid4

import asyncpg
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import database

database.engine = create_async_engine(
    os.environ["MANAGER_DATABASE_URL"],
    pool_pre_ping=False,
    connect_args={"statement_cache_size": 0},
)
database.SessionFactory = async_sessionmaker(database.engine, expire_on_commit=False)

main = importlib.import_module("app.main")
totp_for_test = importlib.import_module("app.security").totp_for_test


original_set_tenant_scope = main.set_tenant_scope


async def _pglite_set_tenant_scope(session: object, scope: object) -> None:
    await session.execute(text("SET LOCAL ROLE manager_app"))  # type: ignore[attr-defined]
    await original_set_tenant_scope(session, scope)  # type: ignore[arg-type]


main.set_tenant_scope = _pglite_set_tenant_scope


async def _insert_org_b_call() -> str:
    connection = await asyncpg.connect(
        os.environ["MANAGER_DATABASE_URL"].replace(
            "postgresql+asyncpg://manager_app:ignored",
            "postgresql://postgres:postgres",
        ),
        ssl=False,
        statement_cache_size=0,
    )
    try:
        row = await connection.fetchrow(
            """
            SELECT organization.id AS org_id, department.id AS department_id
            FROM organizations AS organization
            JOIN departments AS department ON department.org_id = organization.id
            WHERE organization.login_slug = 'org-b' AND department.login_slug = 'default'
            """
        )
        assert row is not None
        call_id = uuid4()
        await connection.execute(
            """
            INSERT INTO calls (id, org_id, department_id, external_call_key, subject)
            VALUES ($1, $2, $3, $4, $5)
            """,
            call_id,
            row["org_id"],
            row["department_id"],
            "org-b-test-call",
            "Org B private call",
        )
        return str(call_id)
    finally:
        connection.terminate()


def _run() -> None:
    with TestClient(main.app, base_url="https://testserver") as client:
        registration = client.post(
            "/auth/register",
            json={
                "organization_name": "Org A Solar",
                "organization_slug": "org-a",
                "display_name": "Owner A",
                "email": "owner-a@example.com",
                "password": "correct-horse-battery-staple",
            },
        )
        assert registration.status_code == 201, registration.text
        set_cookie = registration.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=lax" in set_cookie
        owner_a_cookie = client.cookies.get("manager_session")
        assert owner_a_cookie

        enrolled = client.post("/auth/totp/enroll")
        assert enrolled.status_code == 200, enrolled.text
        verified = client.post(
            "/auth/totp/verify",
            json={"code": totp_for_test(enrolled.json()["secret"], at_time=int(time.time()))},
        )
        assert verified.status_code == 204, verified.text

        invite = client.post(
            "/auth/invites",
            json={"email": "member-a@example.com", "role": "member"},
        )
        assert invite.status_code == 201, invite.text

        client.cookies.clear()
        accepted = client.post(
            "/auth/invites/accept",
            json={
                "token": invite.json()["invite_token"],
                "display_name": "Member A",
                "password": "another-correct-horse-battery",
            },
        )
        assert accepted.status_code == 200, accepted.text

        client.cookies.clear()
        logged_in = client.post(
            "/auth/login",
            json={
                "organization_slug": "org-a",
                "department_slug": "default",
                "email": "member-a@example.com",
                "password": "another-correct-horse-battery",
            },
        )
        assert logged_in.status_code == 200, logged_in.text

        client.cookies.clear()
        second_registration = client.post(
            "/auth/register",
            json={
                "organization_name": "Org B Solar",
                "organization_slug": "org-b",
                "display_name": "Owner B",
                "email": "owner-b@example.com",
                "password": "correct-horse-battery-staple",
            },
        )
        assert second_registration.status_code == 201, second_registration.text

        foreign_call_id = asyncio.run(_insert_org_b_call())
        client.cookies.clear()
        client.cookies.set("manager_session", owner_a_cookie)
        hidden = client.get(f"/api/calls/{foreign_call_id}")
        assert hidden.status_code == 404, hidden.text
        assert hidden.json() == {"detail": "Call not found"}


try:
    _run()
except BaseException:  # Ensure the wire-test subprocess never hangs on socket teardown.
    traceback.print_exc()
    os._exit(1)
else:
    os.write(
        1,
        (
            b"E2E PASS: registration, secure session, TOTP, signed invite, login, "
            b"and Org A -> Org B 404\n"
        ),
    )
    os._exit(0)
