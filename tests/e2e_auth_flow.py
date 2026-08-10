"""Ephemeral PostgreSQL-wire integration check invoked by e2e_auth_flow.mjs.

PGlite's socket adapter always starts a connection as its internal postgres
role. The test-only scope wrapper immediately sets the real runtime role before
any RLS query, matching a normal deployment where the TCP connection is already
authenticated as ``manager_app``. It also disables asyncpg's prepared-statement
cache because the adapter multiplexes one PostgreSQL connection.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from app.hindsight import HindsightRecallHit

database.engine = create_async_engine(
    os.environ["MANAGER_DATABASE_URL"],
    pool_pre_ping=False,
    connect_args={"statement_cache_size": 0},
)
database.SessionFactory = async_sessionmaker(database.engine, expire_on_commit=False)

main = importlib.import_module("app.main")
ingestion = importlib.import_module("app.ingestion")
transcript_memory = importlib.import_module("app.transcript_memory")
totp_for_test = importlib.import_module("app.security").totp_for_test


original_set_tenant_scope = main.set_tenant_scope


async def _pglite_set_tenant_scope(session: object, scope: object) -> None:
    await session.execute(text("SET LOCAL ROLE manager_app"))  # type: ignore[attr-defined]
    await original_set_tenant_scope(session, scope)  # type: ignore[arg-type]


main.set_tenant_scope = _pglite_set_tenant_scope
ingestion.set_tenant_scope = _pglite_set_tenant_scope
transcript_memory.set_tenant_scope = _pglite_set_tenant_scope


class RuntimeHindsight:
    """A deterministic Hindsight boundary for the real HTTP/database flow."""

    def __init__(self) -> None:
        self.retain_calls: list[dict[str, object]] = []
        self.recall_calls: list[tuple[str, str, int]] = []

    async def retain(self, **kwargs: object) -> None:
        self.retain_calls.append(kwargs)

    async def recall(
        self,
        *,
        bank_id: str,
        query: str,
        limit: int,
    ) -> tuple[HindsightRecallHit, ...]:
        self.recall_calls.append((bank_id, query, limit))
        return (
            HindsightRecallHit(
                text="They mentioned a Tuesday tennis league.",
                memory_id="runtime-fuzzy-memory",
                document_id="runtime-fuzzy-document",
                confidence=0.81,
            ),
        )


hindsight = RuntimeHindsight()
main.configured_hindsight_client = lambda **_: hindsight


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


async def _org_scope(login_slug: str) -> tuple[str, str]:
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
            WHERE organization.login_slug = $1 AND department.login_slug = 'default'
            """,
            login_slug,
        )
        assert row is not None
        return str(row["org_id"]), str(row["department_id"])
    finally:
        connection.terminate()


async def _ingestion_counts(
    org_id: str, event_id: str, external_call_key: str
) -> tuple[int, int, str]:
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
            SELECT
                (SELECT count(*)::int FROM telnyx_webhook_events
                 WHERE org_id = $1 AND event_id = $2) AS event_count,
                (SELECT count(*)::int FROM calls
                 WHERE org_id = $1 AND external_call_key = $3) AS call_count,
                (SELECT status FROM telnyx_webhook_events
                 WHERE org_id = $1 AND event_id = $2) AS event_status
            """,
            org_id,
            event_id,
            external_call_key,
        )
        assert row is not None
        return row["event_count"], row["call_count"], row["event_status"]
    finally:
        connection.terminate()


async def _contact_id_by_phone(org_id: str, phone: str) -> str:
    connection = await asyncpg.connect(
        os.environ["MANAGER_DATABASE_URL"].replace(
            "postgresql+asyncpg://manager_app:ignored",
            "postgresql://postgres:postgres",
        ),
        ssl=False,
        statement_cache_size=0,
    )
    try:
        value = await connection.fetchval(
            """
            SELECT contact_id
            FROM contact_phones
            WHERE org_id = $1 AND phone_e164 = $2
            """,
            org_id,
            phone,
        )
        assert value is not None
        return str(value)
    finally:
        connection.terminate()


async def _contact_memory_counts(org_id: str, event_id: str) -> tuple[int, int, int, str | None]:
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
            SELECT
              (SELECT count(*)::int FROM transcripts WHERE org_id = $1) AS transcript_count,
              (SELECT count(*)::int FROM contact_memory_batches WHERE org_id = $1) AS batch_count,
              (SELECT count(*)::int FROM contact_memory_entries WHERE org_id = $1) AS entry_count,
              (SELECT job.status
               FROM hindsight_sync_jobs AS job
               JOIN contact_memory_batches AS batch ON batch.id = job.batch_id
               WHERE batch.org_id = $1
                 AND batch.idempotency_key = $2) AS sync_status
            """,
            org_id,
            f"telnyx-transcript:{hashlib.sha256(event_id.encode('utf-8')).hexdigest()}",
        )
        assert row is not None
        return (
            row["transcript_count"],
            row["batch_count"],
            row["entry_count"],
            row["sync_status"],
        )
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

        org_a_id, department_a_id = asyncio.run(_org_scope("org-a"))
        client.cookies.clear()
        client.cookies.set("manager_session", owner_a_cookie)
        imported = client.post(
            "/api/contacts/import",
            json={
                "filename": "call-contact.csv",
                "columns": ["Name", "Phone"],
                "rows": [
                    {
                        "index": 0,
                        "values": {"Name": "Ava Customer", "Phone": "+6512345678"},
                    }
                ],
                "mapping": {"display_name": "Name", "phone": "Phone"},
            },
        )
        assert imported.status_code == 201, imported.text
        contact_id = asyncio.run(_contact_id_by_phone(org_a_id, "+6512345678"))
        event_id = "evt-e2e-idempotent-1"
        external_call_key = "v3:e2e-control"
        telnyx_payload = {
            "data": {
                "id": event_id,
                "event_type": "call.hangup",
                "payload": {
                    "call_control_id": "v3:e2e-control",
                    "call_session_id": "e2e-session",
                    "direction": "incoming",
                    "from_phone_number": "+6512345678",
                    "to_phone_number": "+6587654321",
                    "start_time": "2026-08-09T19:00:00Z",
                    "end_time": "2026-08-09T19:05:00Z",
                    "contact_memory": {
                        "transcript": "Ava mentioned a Tuesday tennis league after work.",
                        "facts": ["Plays in a Tuesday tennis league."],
                        "preferences": ["Prefers email for status updates."],
                    },
                },
            }
        }
        first_ingest = client.post(
            "/webhooks/telnyx",
            headers={
                "X-Manager-Org-Id": org_a_id,
                "X-Manager-Department-Id": department_a_id,
            },
            json=telnyx_payload,
        )
        assert first_ingest.status_code == 202, first_ingest.text
        assert first_ingest.json() == {
            "accepted": True,
            "duplicate": False,
            "status": "processed",
        }

        replay_ingest = client.post(
            "/webhooks/telnyx",
            headers={
                "X-Manager-Org-Id": org_a_id,
                "X-Manager-Department-Id": department_a_id,
            },
            json=telnyx_payload,
        )
        assert replay_ingest.status_code == 200, replay_ingest.text
        assert replay_ingest.json() == {
            "accepted": True,
            "duplicate": True,
            "status": "duplicate",
        }
        event_count, call_count, event_status = asyncio.run(
            _ingestion_counts(org_a_id, event_id, external_call_key)
        )
        assert (event_count, call_count, event_status) == (1, 1, "processed")
        assert asyncio.run(_contact_memory_counts(org_a_id, event_id)) == (1, 1, 2, "delivered")
        assert len(hindsight.retain_calls) == 1
        retained_content = str(hindsight.retain_calls[0]["content"])
        assert "Ava mentioned a Tuesday tennis league" in retained_content
        assert "Plays in a Tuesday tennis league." in retained_content
        assert "Prefers email for status updates." in retained_content

        deterministic_memory = client.post(
            f"/api/voice/contacts/{contact_id}/memory-recall",
            json={"query": "email status", "limit": 5},
        )
        assert deterministic_memory.status_code == 200, deterministic_memory.text
        assert deterministic_memory.json()["used_hindsight"] is False
        assert deterministic_memory.json()["hindsight_status"] == "not_needed"
        assert deterministic_memory.json()["deterministic"][0]["source"] == "sonnia_crm"
        assert hindsight.recall_calls == []

        fuzzy_memory = client.post(
            f"/api/voice/contacts/{contact_id}/memory-recall",
            json={"query": "What did they mention casually?", "limit": 5},
        )
        assert fuzzy_memory.status_code == 200, fuzzy_memory.text
        assert fuzzy_memory.json()["used_hindsight"] is True
        assert fuzzy_memory.json()["hindsight_status"] == "returned"
        assert fuzzy_memory.json()["fuzzy"][0]["source"] == "hindsight"
        assert fuzzy_memory.json()["fuzzy"][0]["label"] == "AI-assisted recall; verify before use."
        assert len(hindsight.recall_calls) == 1

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
            b"Telnyx transcript memory write/duplicate handling, deterministic-first voice recall, "
            b"Hindsight fallback, and Org A -> Org B 404\n"
        ),
    )
    os._exit(0)
