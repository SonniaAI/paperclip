"""One-connection integration proof for the flagged demo seed lifecycle.

PGLite's socket adapter does not support asyncpg's graceful close handshake
between short-lived subprocesses. Keeping this proof in one authenticated
connection tests the exact load/wipe transaction helpers without changing the
production CLI's normal close behavior.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from scripts.demo_seed import (
    _load_demo_bundle_on_connection,
    _require_demo_seed_enabled,
    _set_scope,
    _wipe_demo_run_on_connection,
    build_demo_bundle,
    remove_audio_fixtures,
    write_audio_fixtures,
)

ORG_ID = UUID("10000000-0000-0000-0000-0000000000a1")
DEPARTMENT_ID = UUID("10000000-0000-0000-0000-0000000000a2")
# Hold the PGLite test connection through ``os._exit`` below. Otherwise
# asyncio.run() garbage-collects it before the Node parent can stop the socket.
_KEEPALIVE: list[object] = []


async def _count(connection: AsyncConnection, table: str) -> int:
    result = await connection.execute(text(f"SELECT count(*) FROM {table}"))
    return int(result.scalar_one())


async def _run() -> None:
    database_url = os.environ["MANAGER_DATABASE_URL"]
    audio_root = Path(os.environ["MANAGER_DEMO_AUDIO_ROOT"])
    _require_demo_seed_enabled()
    run_id = uuid4()
    fixtures = write_audio_fixtures(audio_root, run_id)
    bundle = build_demo_bundle(
        run_id=run_id,
        org_id=ORG_ID,
        department_id=DEPARTMENT_ID,
        audio_fixtures=fixtures,
    )
    engine = create_async_engine(database_url, pool_pre_ping=True)
    # Intentionally no ``async with``/dispose: after all assertions the child
    # exits and the Node fixture tears down PGLite. A normal PostgreSQL CLI
    # path remains covered by load_demo_bundle/wipe_demo_run's disposal logic.
    connection = await engine.connect()
    _KEEPALIVE.extend((engine, connection))
    try:
        async with connection.begin():
            await _load_demo_bundle_on_connection(
                connection,
                bundle,
                org_id=ORG_ID,
                department_id=DEPARTMENT_ID,
            )

        async with connection.begin():
            await _set_scope(connection, org_id=ORG_ID, department_id=DEPARTMENT_ID)
            assert await _count(connection, "contacts") == 200
            assert await _count(connection, "calls") == 400
            assert await _count(connection, "recordings") == 5
            assert await _count(connection, "follow_ups") == 84
            assert await _count(connection, "demo_seed_runs") == 1
            assert await _count(connection, "demo_seed_records") > 1_500
            flagged = await connection.execute(
                text("SELECT app.is_demo_seed_record('calls', :call_id)"),
                {"call_id": bundle.featured_call_id},
            )
            assert flagged.scalar_one() is True
            assert len(list((audio_root / str(run_id)).glob("*.wav"))) == 5
            assert fixtures[0].local_path.read_bytes()[:4] == b"RIFF"

        async with connection.begin():
            wiped_run_id, deleted = await _wipe_demo_run_on_connection(
                connection, org_id=ORG_ID, department_id=DEPARTMENT_ID
            )
            assert wiped_run_id == run_id
            assert deleted["contacts"] == 200
            assert deleted["calls"] == 400

        async with connection.begin():
            await _set_scope(connection, org_id=ORG_ID, department_id=DEPARTMENT_ID)
            for table in (
                "contacts",
                "calls",
                "recordings",
                "transcripts",
                "follow_ups",
                "demo_seed_runs",
                "demo_seed_records",
            ):
                assert await _count(connection, table) == 0, table
    finally:
        assert remove_audio_fixtures(audio_root, run_id)


if __name__ == "__main__":
    asyncio.run(_run())
    print(
        "PASS: 200-contact/400-call demo dataset loads and wipes through the PostgreSQL "
        "wire protocol."
    )
    # The PGLite fixture is torn down by the Node parent. Avoid Python's
    # asyncpg finalizer after all assertions and fixture cleanup are complete.
    sys.stdout.flush()
    os._exit(0)
