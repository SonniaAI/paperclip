"""Runtime proof for durable, idempotent, replayable Telnyx ingestion."""

from __future__ import annotations

import asyncio
import os
import traceback
from copy import deepcopy
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import database, ingestion
from app.database import TenantScope
from app.storage import InMemoryPrivateObjectStorage


async def _run() -> None:
    engine = create_async_engine(
        os.environ["MANAGER_DATABASE_URL"],
        pool_pre_ping=False,
        connect_args={"statement_cache_size": 0},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = TenantScope(
        org_id=UUID(os.environ["MANAGER_TEST_ORG_ID"]),
        department_id=UUID(os.environ["MANAGER_TEST_DEPARTMENT_ID"]),
    )
    original_set_tenant_scope = database.set_tenant_scope

    async def pglite_set_tenant_scope(session: object, tenant_scope: TenantScope) -> None:
        # PGlite's socket adapter starts every connection as postgres. Production
        # connects directly as manager_app; this test switches to that same role.
        await session.execute(text("SET LOCAL ROLE manager_app"))  # type: ignore[attr-defined]
        await original_set_tenant_scope(session, tenant_scope)  # type: ignore[arg-type]

    ingestion.set_tenant_scope = pglite_set_tenant_scope
    storage = InMemoryPrivateObjectStorage()
    payload = {
        "data": {
            "id": "evt-runtime-replay-1",
            "event_type": "call.hangup",
            "payload": {
                "call_control_id": "call-control-runtime-1",
                "direction": "outgoing",
                "from": "+6560000001",
                "to": "+6560000002",
                "start_time": "2026-08-09T20:00:00Z",
                "end_time": "2026-08-09T20:03:00Z",
                "subject": "Stored raw subject",
                "recording_url": "https://example.invalid/private-source.wav",
            },
        }
    }
    duplicate_payload = deepcopy(payload)

    try:
        async with sessions() as session:
            first = await ingestion.ingest_telnyx_event(
                session,
                scope,
                payload,
                storage=storage,
            )
            assert first.status == "processed"
            assert first.duplicate is False
            assert first.call_id is not None

            duplicate = await ingestion.ingest_telnyx_event(
                session,
                scope,
                duplicate_payload,
                storage=storage,
            )
            assert duplicate.status == "duplicate"
            assert duplicate.duplicate is True

            # Replay must use the durable database copy, not this now-mutated object.
            payload["data"]["payload"]["subject"] = "Mutated after persistence"
            replayed = await ingestion.replay_telnyx_event(
                session,
                scope,
                first.event_id,
                storage=storage,
            )
            assert replayed.status == "processed"
            assert replayed.call_id == first.call_id

            async with session.begin():
                await pglite_set_tenant_scope(session, scope)
                evidence = (
                    await session.execute(
                        text(
                            """
                            SELECT
                              (SELECT count(*) FROM telnyx_webhook_events) AS event_count,
                              (SELECT count(*) FROM calls
                                WHERE external_call_key = 'call-control-runtime-1') AS call_count,
                              (SELECT count(*) FROM recordings) AS recording_count,
                              (SELECT raw_payload #>> '{data,payload,subject}'
                                FROM telnyx_webhook_events
                                WHERE event_id = 'evt-runtime-replay-1') AS stored_subject,
                              (SELECT subject FROM calls
                                WHERE external_call_key = 'call-control-runtime-1') AS call_subject
                            """
                        )
                    )
                ).mappings().one()

            assert evidence["event_count"] == 1
            assert evidence["call_count"] == 1
            assert evidence["recording_count"] == 1
            assert evidence["stored_subject"] == "Stored raw subject"
            assert evidence["call_subject"] == "Stored raw subject"
            assert len(storage.objects) == 1
            object_key = next(iter(storage.objects))
            assert object_key.startswith(f"recordings/{scope.org_id}/{first.call_id}/")
            signed_url = await storage.create_signed_url(object_key, expires_in_seconds=300)
            assert signed_url.endswith("?expires=300")
    finally:
        ingestion.set_tenant_scope = original_set_tenant_scope


try:
    asyncio.run(_run())
except BaseException:
    # PGlite's socket adapter does not implement asyncpg's graceful close
    # reliably, so use the same bounded subprocess teardown as the auth E2E.
    traceback.print_exc()
    os._exit(1)
else:
    os.write(
        1,
        (
            b"PASS: raw event stored once; duplicate skipped; replay reused durable payload; "
            b"one private recording key with a 300-second signed URL.\n"
        ),
    )
    os._exit(0)
