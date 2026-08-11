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
from copy import deepcopy
from urllib.parse import unquote
from uuid import UUID, uuid4

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
            SELECT account.org_id, department.id AS department_id
            FROM app_users AS account
            JOIN departments AS department
              ON department.org_id = account.org_id
             AND department.id = account.department_id
            WHERE account.email = 'owner-b@example.com'
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


async def _insert_contact_linked_call(
    *,
    org_id: str,
    department_id: str,
    contact_id: str,
    external_call_key: str,
    from_phone: str,
) -> str:
    """Create the existing call a cross-tenant duplicate must never use."""

    connection = await asyncpg.connect(
        os.environ["MANAGER_DATABASE_URL"].replace(
            "postgresql+asyncpg://manager_app:ignored",
            "postgresql://postgres:postgres",
        ),
        ssl=False,
        statement_cache_size=0,
    )
    try:
        call_id = uuid4()
        await connection.execute(
            """
            INSERT INTO calls (
                id, org_id, department_id, contact_id, external_call_key,
                subject, from_phone_e164
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            call_id,
            UUID(org_id),
            UUID(department_id),
            UUID(contact_id),
            external_call_key,
            "Cross-tenant duplicate target",
            from_phone,
        )
        return str(call_id)
    finally:
        connection.terminate()


async def _org_scope(email: str) -> tuple[str, str]:
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
            SELECT account.org_id, department.id AS department_id
            FROM app_users AS account
            JOIN departments AS department
              ON department.org_id = account.org_id
             AND department.id = account.department_id
            WHERE account.email = $1
            """,
            email,
        )
        assert row is not None
        return str(row["org_id"]), str(row["department_id"])
    finally:
        connection.terminate()


def _token_from_development_url(response: object) -> str:
    payload = response.json()  # type: ignore[attr-defined]
    assert payload["delivery"] == "development", payload
    url = payload["development_url"]
    assert url
    return unquote(url.rsplit("/", maxsplit=1)[-1])


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


async def _transcript_text_for_event(org_id: str, event_id: str) -> str | None:
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
            SELECT transcript.raw_text
            FROM transcripts AS transcript
            JOIN contact_memory_batches AS batch ON batch.transcript_id = transcript.id
            WHERE batch.org_id = $1
              AND batch.idempotency_key = $2
            """,
            org_id,
            f"telnyx-transcript:{hashlib.sha256(event_id.encode('utf-8')).hexdigest()}",
        )
        return str(value) if value is not None else None
    finally:
        connection.terminate()


def _run() -> None:
    with TestClient(main.app, base_url="https://testserver") as client:
        registration = client.post(
            "/auth/register",
            json={
                "account_type": "business",
                "full_name": "Owner A",
                "email": "owner-a@example.com",
                "password": "correct-horse-battery-staple",
                "company_name": "Org A Solar",
                "country": "Singapore",
            },
        )
        assert registration.status_code == 201, registration.text
        assert "set-cookie" not in registration.headers
        verification_token = _token_from_development_url(registration)

        unverified = client.post(
            "/auth/login",
            json={
                "email": "owner-a@example.com",
                "password": "correct-horse-battery-staple",
            },
        )
        assert unverified.status_code == 403, unverified.text
        assert unverified.json()["detail"]["code"] == "email_unverified"

        verification = client.post(
            "/auth/verify-email",
            json={"token": verification_token},
        )
        assert verification.status_code == 200, verification.text
        assert verification.json()["authenticated"] is True
        assert verification.json()["user"]["company"] == "Org A Solar"
        set_cookie = verification.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=lax" in set_cookie
        owner_a_cookie = client.cookies.get("manager_session")
        assert owner_a_cookie
        reused_verification = client.post(
            "/auth/verify-email",
            json={"token": verification_token},
        )
        assert reused_verification.status_code == 400, reused_verification.text
        assert reused_verification.json()["detail"]["code"] == "already_used"

        enrolled = client.post("/auth/totp/enroll")
        assert enrolled.status_code == 200, enrolled.text
        totp_secret = enrolled.json()["secret"]
        verified = client.post(
            "/auth/totp/verify",
            json={"code": totp_for_test(totp_secret, at_time=int(time.time()))},
        )
        assert verified.status_code == 204, verified.text

        signed_out = client.post("/auth/logout")
        assert signed_out.status_code == 204, signed_out.text
        login_challenge = client.post(
            "/auth/login",
            json={
                "email": "owner-a@example.com",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login_challenge.status_code == 200, login_challenge.text
        assert login_challenge.json()["requires_2fa"] is True
        assert "set-cookie" not in login_challenge.headers
        completed_login = client.post(
            "/auth/login/2fa",
            json={
                "challenge_token": login_challenge.json()["challenge_token"],
                "code": totp_for_test(totp_secret, at_time=int(time.time())),
            },
        )
        assert completed_login.status_code == 200, completed_login.text
        owner_a_cookie = client.cookies.get("manager_session")
        assert owner_a_cookie

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
                "email": "member-a@example.com",
                "password": "another-correct-horse-battery",
            },
        )
        assert logged_in.status_code == 200, logged_in.text
        assert logged_in.json()["user"]["department"] == "default"

        old_member_cookie = client.cookies.get("manager_session")
        forgot = client.post(
            "/auth/forgot-password",
            json={"email": "member-a@example.com"},
        )
        assert forgot.status_code == 200, forgot.text
        reset_token = _token_from_development_url(forgot)
        unknown_forgot = client.post(
            "/auth/forgot-password",
            json={"email": "unknown@example.com"},
        )
        assert unknown_forgot.status_code == 200, unknown_forgot.text
        assert unknown_forgot.json()["message"] == forgot.json()["message"]
        reset = client.post(
            "/auth/reset-password",
            json={
                "token": reset_token,
                "new_password": "new-member-password-2026",
            },
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["authenticated"] is True
        new_member_cookie = client.cookies.get("manager_session")
        assert new_member_cookie != old_member_cookie
        client.cookies.clear()
        client.cookies.set("manager_session", old_member_cookie)
        assert client.get("/auth/me").status_code == 401
        client.cookies.clear()
        client.cookies.set("manager_session", new_member_cookie)

        client.cookies.clear()
        second_registration = client.post(
            "/auth/register",
            json={
                "account_type": "individual",
                "full_name": "Owner B",
                "email": "owner-b@example.com",
                "password": "correct-horse-battery-staple",
            },
        )
        assert second_registration.status_code == 201, second_registration.text
        second_verification = client.post(
            "/auth/verify-email",
            json={"token": _token_from_development_url(second_registration)},
        )
        assert second_verification.status_code == 200, second_verification.text

        org_a_id, department_a_id = asyncio.run(_org_scope("owner-a@example.com"))
        org_b_id, department_b_id = asyncio.run(_org_scope("owner-b@example.com"))
        imported_b = client.post(
            "/api/contacts/import",
            json={
                "filename": "cross-tenant-contact.csv",
                "columns": ["Name", "Phone"],
                "rows": [
                    {
                        "index": 0,
                        "values": {"Name": "Bryn Customer", "Phone": "+6512345679"},
                    }
                ],
                "mapping": {"display_name": "Name", "phone": "Phone"},
            },
        )
        assert imported_b.status_code == 201, imported_b.text
        other_tenant_contact_id = asyncio.run(_contact_id_by_phone(org_b_id, "+6512345679"))
        other_tenant_call_key = "v3:cross-tenant-duplicate"
        asyncio.run(
            _insert_contact_linked_call(
                org_id=org_b_id,
                department_id=department_b_id,
                contact_id=other_tenant_contact_id,
                external_call_key=other_tenant_call_key,
                from_phone="+6512345679",
            )
        )

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
                        "preferences": ["They prefer email."],
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
        assert "They prefer email." in retained_content

        altered_replay = deepcopy(telnyx_payload)
        altered_transcript = "Untrusted replay says they prefer SMS instead."
        altered_replay["data"]["payload"]["contact_memory"] = {
            "transcript": altered_transcript,
            "facts": ["Untrusted renewal fact."],
            "preferences": ["They prefer SMS."],
        }
        altered_ingest = client.post(
            "/webhooks/telnyx",
            headers={
                "X-Manager-Org-Id": org_a_id,
                "X-Manager-Department-Id": department_a_id,
            },
            json=altered_replay,
        )
        assert altered_ingest.status_code == 200, altered_ingest.text
        assert altered_ingest.json() == {
            "accepted": True,
            "duplicate": True,
            "status": "duplicate",
        }
        assert asyncio.run(_transcript_text_for_event(org_a_id, event_id)) == (
            "Ava mentioned a Tuesday tennis league after work."
        )
        assert asyncio.run(_contact_memory_counts(org_a_id, event_id)) == (1, 1, 2, "delivered")
        assert len(hindsight.retain_calls) == 1
        assert altered_transcript not in retained_content

        cross_tenant_replay = deepcopy(telnyx_payload)
        cross_tenant_replay["data"]["payload"].update(
            {
                "call_control_id": other_tenant_call_key,
                "call_session_id": "cross-tenant-session",
                "from_phone_number": "+6512345679",
                "contact_memory": {
                    "transcript": "This was never admitted to Org B's durable inbox.",
                    "facts": ["Untrusted Org B fact."],
                    "preferences": ["They prefer untrusted SMS."],
                },
            }
        )
        cross_tenant_ingest = client.post(
            "/webhooks/telnyx",
            headers={
                "X-Manager-Org-Id": org_b_id,
                "X-Manager-Department-Id": department_b_id,
            },
            json=cross_tenant_replay,
        )
        assert cross_tenant_ingest.status_code == 400, cross_tenant_ingest.text
        assert asyncio.run(_contact_memory_counts(org_b_id, event_id)) == (0, 0, 0, None)
        assert len(hindsight.retain_calls) == 1

        deterministic_memory = client.post(
            f"/api/voice/contacts/{contact_id}/memory-recall",
            json={"query": "email status", "limit": 5},
        )
        assert deterministic_memory.status_code == 200, deterministic_memory.text
        assert deterministic_memory.json()["used_hindsight"] is False
        assert deterministic_memory.json()["hindsight_status"] == "not_needed"
        assert deterministic_memory.json()["deterministic"][0]["source"] == "sonnia_crm"
        assert hindsight.recall_calls == []

        unrelated_memory = client.post(
            f"/api/voice/contacts/{contact_id}/memory-recall",
            json={"query": "What did they say about renewal?", "limit": 5},
        )
        assert unrelated_memory.status_code == 200, unrelated_memory.text
        assert unrelated_memory.json()["deterministic"] == []
        assert unrelated_memory.json()["used_hindsight"] is True
        assert unrelated_memory.json()["hindsight_status"] == "returned"
        assert unrelated_memory.json()["fuzzy"][0]["source"] == "hindsight"
        assert len(hindsight.recall_calls) == 1

        fuzzy_memory = client.post(
            f"/api/voice/contacts/{contact_id}/memory-recall",
            json={"query": "What did they mention casually?", "limit": 5},
        )
        assert fuzzy_memory.status_code == 200, fuzzy_memory.text
        assert fuzzy_memory.json()["used_hindsight"] is True
        assert fuzzy_memory.json()["hindsight_status"] == "returned"
        assert fuzzy_memory.json()["fuzzy"][0]["source"] == "hindsight"
        assert fuzzy_memory.json()["fuzzy"][0]["label"] == "AI-assisted recall; verify before use."
        assert len(hindsight.recall_calls) == 2

        foreign_call_id = asyncio.run(_insert_org_b_call())
        client.cookies.clear()
        client.cookies.set("manager_session", owner_a_cookie)
        hidden = client.get(f"/api/calls/{foreign_call_id}")
        assert hidden.status_code == 404, hidden.text
        assert hidden.json() == {"detail": "Call not found"}

        client.cookies.clear()
        for _ in range(4):
            failed_login = client.post(
                "/auth/login",
                json={"email": "rate-limit@example.com", "password": "incorrect-password"},
            )
            assert failed_login.status_code == 401, failed_login.text
        limited_login = client.post(
            "/auth/login",
            json={"email": "rate-limit@example.com", "password": "incorrect-password"},
        )
        assert limited_login.status_code == 429, limited_login.text
        assert limited_login.headers["retry-after"] == str(main.settings.auth_lockout_seconds)


try:
    _run()
except BaseException:  # Ensure the wire-test subprocess never hangs on socket teardown.
    traceback.print_exc()
    os._exit(1)
else:
    os.write(
        1,
        (
            b"E2E PASS: company + individual registration, verification, secure session, "
            b"email-only login, conditional TOTP, password reset, rate limiting, signed invite, "
            b"Telnyx transcript memory write/duplicate handling, deterministic-first voice recall, "
            b"Hindsight fallback, and Org A -> Org B 404\n"
        ),
    )
    os._exit(0)
