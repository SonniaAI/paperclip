"""Load or clear the deliberately flagged Phase 1 demo dataset.

This module is intentionally a CLI rather than an HTTP endpoint.  It refuses
to run unless the operator enables it in the environment *and* supplies the
corresponding confirmation flag.  Every inserted application record is listed
in ``demo_seed_records`` so ``wipe`` deletes only this run's data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import struct
import wave
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.config import get_settings

DEMO_LABEL = "manager.sonnia.ai/demo-data"
SEED_VERSION = "phase-1-demo-v1"
CONTACT_COUNT = 200
CALL_COUNT = 400
AUDIO_FIXTURE_COUNT = 5
DEMO_ENABLED_VALUE = "true"

ENTITY_TABLES = {
    "companies": "companies",
    "contacts": "contacts",
    "contact_phones": "contact_phones",
    "contact_emails": "contact_emails",
    "calls": "calls",
    "recordings": "recordings",
    "transcripts": "transcripts",
    "transcript_segments": "transcript_segments",
    "call_summaries": "call_summaries",
    "follow_ups": "follow_ups",
    "notes": "notes",
    "task_lists": "task_lists",
    "tasks": "tasks",
    "actions": "actions",
    "activity_log": "activity_log",
}

# Children must be removed before their referenced parent records.  This is
# also the complete allow-list for a wipe: no arbitrary table is ever deleted.
WIPE_ORDER = (
    "transcript_segments",
    "recordings",
    "transcripts",
    "call_summaries",
    "follow_ups",
    "notes",
    "tasks",
    "actions",
    "activity_log",
    "calls",
    "contact_phones",
    "contact_emails",
    "contacts",
    "task_lists",
    "companies",
)

FIRST_NAMES = (
    "Aisha", "Amelia", "Arjun", "Benjamin", "Chloe", "Daniel", "Elena", "Farid",
    "Grace", "Hannah", "Irfan", "Jasmine", "Kai", "Leah", "Marcus", "Nadia",
    "Owen", "Priya", "Ravi", "Sarah", "Theo", "Uma", "Victor", "Wei", "Yasmin",
)
LAST_NAMES = (
    "Lim", "Tan", "Ng", "Koh", "Goh", "Lee", "Wong", "Chua", "Kaur", "Shah",
    "Patel", "Raman", "Ong", "Yeo", "Chan", "Low", "Teo", "Ho", "Basu", "Nair",
)
COMPANY_STEMS = (
    "Brightfield", "Harbour", "Meridian", "Northstar", "Cedar", "Saffron", "Lumen",
    "Evergreen", "Civic", "Summit", "Orchard", "Tidal", "Atlas", "Crown", "Pioneer",
    "Riverview", "Kinetic", "Parkside", "Juniper", "Bluehaven", "Aster", "Keystone",
    "Willow", "Redwood", "Solace", "Horizon", "Crescent", "Mosaic", "Palisade", "Vantage",
    "Bayfront", "Sterling", "Nexus", "Beacon", "Seabrook", "Noble", "Terrace", "Elm",
    "Sunnyside", "Wellspring",
)
JOB_TITLES = (
    "Managing Director", "Operations Manager", "Facilities Lead", "Finance Director",
    "General Manager", "Sustainability Lead", "Procurement Manager", "Property Manager",
)
OUTCOMES = (
    ("qualified", "completed", "Interested in a site assessment; requested a proposal."),
    (
        "follow_up",
        "completed",
        "Positive conversation; asked to reconnect after the board meeting.",
    ),
    ("voicemail", "missed", "Reached voicemail; a concise follow-up is due."),
    ("not_now", "completed", "Timing is not right; revisit in the next planning cycle."),
    ("not_interested", "completed", "Not a fit today; honour the decision and do not chase."),
    ("wrong_contact", "failed", "The number is valid but belongs to a different team."),
    ("warm_referral", "completed", "A colleague was named as the right person to involve."),
)
TRANSCRIPT_RESPONSES = (
    "We are reviewing the roof-space plan with operations this month.",
    "The monthly bill has been volatile, so a clearer forecast would help.",
    "Please send something brief that I can share with the finance team.",
    "We have looked at solar before, but the installation timing was the concern.",
    "I can make time for a practical site-assessment conversation next week.",
)
MEMORY_FACTS = (
    "prefers a short written recap before a meeting",
    "cares most about installation disruption and operating hours",
    "asked for practical payback assumptions rather than a broad estimate",
    "will involve finance before signing off on a site visit",
    "responds best to a specific next step and calendar option",
)


@dataclass(frozen=True)
class AudioFixture:
    index: int
    storage_key: str
    local_path: Path
    byte_size: int
    duration_ms: int


@dataclass(frozen=True)
class DemoBundle:
    run_id: UUID
    records: Mapping[str, list[dict[str, Any]]]
    featured_call_id: UUID

    @property
    def counts(self) -> dict[str, int]:
        return {entity_type: len(rows) for entity_type, rows in self.records.items()}


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalise_reference_time(value: datetime | None) -> datetime:
    reference = value or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return reference.astimezone(UTC).replace(second=0, microsecond=0)


def _safe_audio_run_dir(root: Path, run_id: UUID) -> Path:
    resolved_root = root.expanduser().resolve()
    target = (resolved_root / str(run_id)).resolve()
    if target.parent != resolved_root:
        raise ValueError("audio fixture directory must remain below the supplied root")
    return target


def write_audio_fixtures(root: Path, run_id: UUID) -> tuple[AudioFixture, ...]:
    """Create five valid, non-vocal WAV fixtures for private demo playback.

    The fixtures are intentionally synthetic tones, not impersonated people or
    fabricated recordings.  They prove the player/storage path with actual
    playable audio and are staged under a run-specific private-storage prefix.
    """

    output_dir = _safe_audio_run_dir(root, run_id)
    output_dir.mkdir(parents=True, exist_ok=False)
    fixtures: list[AudioFixture] = []
    sample_rate = 16_000

    for index in range(AUDIO_FIXTURE_COUNT):
        duration_ms = 1_800 + index * 180
        frames = sample_rate * duration_ms // 1_000
        path = output_dir / f"call-{index + 1}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            for frame in range(frames):
                seconds = frame / sample_rate
                envelope = min(1.0, frame / 500) * min(1.0, (frames - frame) / 500)
                carrier = math.sin(2 * math.pi * (330 + index * 37) * seconds)
                harmonic = math.sin(2 * math.pi * (495 + index * 23) * seconds) * 0.35
                sample = int(6_500 * envelope * (carrier + harmonic))
                output.writeframesraw(struct.pack("<h", sample))
        fixtures.append(
            AudioFixture(
                index=index,
                storage_key=f"demo-seed/{run_id}/call-{index + 1}.wav",
                local_path=path,
                byte_size=path.stat().st_size,
                duration_ms=duration_ms,
            )
        )
    return tuple(fixtures)


def _insert_row(records: dict[str, list[dict[str, Any]]], collection: str, **values: Any) -> UUID:
    record_id = values.setdefault("id", uuid4())
    records[collection].append(values)
    return record_id


def build_demo_bundle(
    *,
    run_id: UUID,
    org_id: UUID,
    department_id: UUID,
    audio_fixtures: Iterable[AudioFixture],
    reference_time: datetime | None = None,
) -> DemoBundle:
    """Build realistic-but-fictional Phase 1 demo rows without writing a DB."""

    now = _normalise_reference_time(reference_time)
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    companies: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []

    for index, stem in enumerate(COMPANY_STEMS):
        company_id = _insert_row(
            records,
            "companies",
            org_id=org_id,
            department_id=department_id,
            name=f"{stem} Solar Pte. Ltd.",
            legal_name=f"{stem} Solar Private Limited",
            domain=f"{stem.lower()}.example",
            created_at=now - timedelta(days=120 + index),
            updated_at=now - timedelta(days=index % 14),
        )
        companies.append({"id": company_id, "name": f"{stem} Solar Pte. Ltd."})

    for index in range(CONTACT_COUNT):
        first_name = FIRST_NAMES[index % len(FIRST_NAMES)]
        last_name = LAST_NAMES[(index * 7) % len(LAST_NAMES)]
        company = companies[index % len(companies)]
        display_name = f"{first_name} {last_name}"
        contact_id = _insert_row(
            records,
            "contacts",
            org_id=org_id,
            department_id=department_id,
            company_id=company["id"],
            display_name=display_name,
            first_name=first_name,
            last_name=last_name,
            job_title=JOB_TITLES[index % len(JOB_TITLES)],
            do_not_contact=index % 47 == 0,
            created_at=now - timedelta(days=110 - index % 64),
            updated_at=now - timedelta(days=index % 16),
        )
        phone_number = f"+658{index:07d}"[-12:]
        _insert_row(
            records,
            "contact_phones",
            org_id=org_id,
            department_id=department_id,
            contact_id=contact_id,
            phone_e164=phone_number,
            label="mobile",
            is_primary=True,
            verified_at=now - timedelta(days=100 - index % 50),
            created_at=now - timedelta(days=110 - index % 64),
        )
        _insert_row(
            records,
            "contact_emails",
            org_id=org_id,
            department_id=department_id,
            contact_id=contact_id,
            email=f"{first_name.lower()}.{last_name.lower()}{index}@{company['name'].split()[0].lower()}.example",
            label="work",
            is_primary=True,
            verified_at=now - timedelta(days=100 - index % 50),
            created_at=now - timedelta(days=110 - index % 64),
        )
        contacts.append({"id": contact_id, "name": display_name, "company": company})

    call_rows: list[dict[str, Any]] = []
    for index in range(CALL_COUNT):
        contact = contacts[(index * 17) % len(contacts)]
        outcome, status, summary = OUTCOMES[index % len(OUTCOMES)]
        started_at = now - timedelta(
            days=(index * 11) % 84,
            hours=(index * 5) % 9,
            minutes=index % 53,
        )
        duration_seconds = 75 + (index * 29) % 680 if status == "completed" else 0
        ended_at = started_at + timedelta(seconds=duration_seconds) if duration_seconds else None
        call_id = _insert_row(
            records,
            "calls",
            org_id=org_id,
            department_id=department_id,
            contact_id=contact["id"],
            company_id=contact["company"]["id"],
            external_call_key=f"demo:{run_id}:call:{index + 1}",
            subject=f"{contact['name']} — {outcome.replace('_', ' ')}",
            direction="outbound" if index % 5 else "inbound",
            status=status,
            telnyx_call_control_id=None,
            telnyx_call_session_id=None,
            from_phone_e164="+6565550100",
            to_phone_e164=f"+658{(index * 17) % 10_000_000:07d}"[-12:],
            started_at=started_at,
            ended_at=ended_at,
            metadata=_json(
                {
                    "analytics_excluded": True,
                    "demo_seed": True,
                    "demo_seed_run_id": str(run_id),
                    "outcome": outcome,
                    "featured_demo_path": index == 0,
                }
            ),
            created_at=started_at,
            updated_at=ended_at or started_at,
        )
        call_rows.append(
            {
                "id": call_id,
                "contact": contact,
                "outcome": outcome,
                "status": status,
                "summary": summary,
                "started_at": started_at,
                "ended_at": ended_at,
            }
        )

    recorded_calls = [row for row in call_rows if row["status"] == "completed"][
        :AUDIO_FIXTURE_COUNT
    ]
    for fixture, call in zip(audio_fixtures, recorded_calls, strict=True):
        _insert_row(
            records,
            "recordings",
            org_id=org_id,
            department_id=department_id,
            call_id=call["id"],
            storage_bucket="manager-demo-private",
            storage_key=fixture.storage_key,
            content_type="audio/wav",
            byte_size=fixture.byte_size,
            duration_ms=fixture.duration_ms,
            is_private=True,
            created_at=call["started_at"],
        )

    for index, call in enumerate(call_rows):
        if call["status"] == "completed":
            contact_name = call["contact"]["name"]
            lines = (
                (
                    f"Sonnia: Hi {contact_name}, this is Sonnia. Is now still a useful time "
                    "to talk about your site plans?"
                ),
                f"{contact_name}: {TRANSCRIPT_RESPONSES[index % len(TRANSCRIPT_RESPONSES)]}",
                (
                    "Sonnia: That is helpful. I will send a concise recap and the next "
                    "practical option."
                ),
            )
            transcript_id = _insert_row(
                records,
                "transcripts",
                org_id=org_id,
                department_id=department_id,
                call_id=call["id"],
                provider="demo-seed",
                language_code="en-SG",
                status="complete",
                raw_text="\n".join(lines),
                created_at=call["started_at"],
                updated_at=call["ended_at"] or call["started_at"],
            )
            for segment_index, segment_text in enumerate(lines):
                _insert_row(
                    records,
                    "transcript_segments",
                    org_id=org_id,
                    department_id=department_id,
                    transcript_id=transcript_id,
                    sequence=segment_index,
                    speaker="Sonnia" if segment_index != 1 else contact_name,
                    text=segment_text,
                    start_ms=segment_index * 22_000,
                    end_ms=(segment_index + 1) * 22_000 - 750,
                    markers=_json(["demo-seed"] if segment_index == 0 else []),
                    created_at=call["started_at"],
                )
        _insert_row(
            records,
            "call_summaries",
            org_id=org_id,
            department_id=department_id,
            call_id=call["id"],
            summary=call["summary"],
            sentiment=(
                "positive" if call["outcome"] in {"qualified", "warm_referral"} else "neutral"
            ),
            outcome=call["outcome"],
            created_at=call["started_at"],
            updated_at=call["ended_at"] or call["started_at"],
        )

    for index, contact in enumerate(contacts):
        associated_call = call_rows[index * 2]
        _insert_row(
            records,
            "notes",
            org_id=org_id,
            department_id=department_id,
            contact_id=contact["id"],
            company_id=contact["company"]["id"],
            call_id=associated_call["id"],
            author_user_id=None,
            body=f"Memory: {contact['name']} {MEMORY_FACTS[index % len(MEMORY_FACTS)]}.",
            visibility="shared",
            created_at=associated_call["started_at"],
            updated_at=associated_call["started_at"],
        )

    for index, call in enumerate(call_rows[:84]):
        statuses = ("scheduled", "scheduled", "completed", "cancelled")
        follow_up_status = statuses[index % len(statuses)]
        _insert_row(
            records,
            "follow_ups",
            org_id=org_id,
            department_id=department_id,
            call_id=call["id"],
            contact_id=call["contact"]["id"],
            owner_user_id=None,
            scheduled_for=now + timedelta(days=(index % 21) - 7, hours=index % 8),
            status=follow_up_status,
            notes=(
                f"{follow_up_status.replace('_', ' ').title()} demo follow-up for "
                f"{call['contact']['name']}."
            ),
            created_at=call["started_at"],
            updated_at=now - timedelta(days=index % 5),
        )

    task_list_id = _insert_row(
        records,
        "task_lists",
        org_id=org_id,
        department_id=department_id,
        name="Demo follow-ups",
        description="Flagged demo tasks; clear with the demo seed run.",
        created_at=now - timedelta(days=20),
        updated_at=now,
    )
    for index, call in enumerate(call_rows[:60]):
        task_status = ("open", "in_progress", "done", "cancelled")[index % 4]
        _insert_row(
            records,
            "tasks",
            org_id=org_id,
            department_id=department_id,
            task_list_id=task_list_id,
            contact_id=call["contact"]["id"],
            call_id=call["id"],
            owner_user_id=None,
            created_by_user_id=None,
            title=f"{task_status.replace('_', ' ').title()}: {call['contact']['name']}",
            description="Demo task generated from a flagged demo call.",
            due_at=now + timedelta(days=(index % 14) - 3),
            status=task_status,
            visibility="shared",
            created_at=call["started_at"],
            updated_at=now - timedelta(days=index % 4),
        )
        _insert_row(
            records,
            "actions",
            org_id=org_id,
            department_id=department_id,
            call_id=call["id"],
            contact_id=call["contact"]["id"],
            owner_user_id=None,
            action="Send a concise, useful follow-up with the agreed next step.",
            due_at=now + timedelta(days=(index % 9) - 2),
            status=task_status,
            created_at=call["started_at"],
            updated_at=now - timedelta(days=index % 4),
        )

    for index, call in enumerate(call_rows[:18]):
        _insert_row(
            records,
            "activity_log",
            org_id=org_id,
            department_id=department_id,
            actor_user_id=None,
            action="sonnia_noticed",
            entity_type="call",
            entity_id=call["id"],
            metadata=_json(
                {
                    "headline": "Sonnia noticed something",
                    "detail": (
                        f"{call['contact']['name']} mentioned a concrete next step; "
                        "the follow-up is ready to review."
                    ),
                    "demo_seed": True,
                    "priority": "attention" if index % 3 == 0 else "normal",
                }
            ),
            created_at=call["started_at"] + timedelta(minutes=2),
        )

    return DemoBundle(run_id=run_id, records=dict(records), featured_call_id=call_rows[0]["id"])


class DemoSeedError(RuntimeError):
    """An operator-visible error that leaves existing customer data untouched."""


async def _set_scope(connection: AsyncConnection, *, org_id: UUID, department_id: UUID) -> None:
    await connection.execute(
        text("SELECT set_config('app.org_id', :org_id, true)"), {"org_id": str(org_id)}
    )
    await connection.execute(
        text("SELECT set_config('app.department_id', :department_id, true)"),
        {"department_id": str(department_id)},
    )


async def _active_run_id(
    connection: AsyncConnection, *, org_id: UUID, department_id: UUID
) -> UUID | None:
    result = await connection.execute(
        text(
            """
            SELECT id
            FROM demo_seed_runs
            WHERE org_id = :org_id
              AND department_id = :department_id
              AND label = :label
            """
        ),
        {"org_id": org_id, "department_id": department_id, "label": DEMO_LABEL},
    )
    return result.scalar_one_or_none()


def _insert_statement(table: str, row: Mapping[str, Any]) -> Any:
    # All table and column names come from this module's fixed bundle builder;
    # only values are bound parameters.
    json_columns = {"metadata", "markers", "run_metadata"}
    columns = tuple(row)
    values = ", ".join(
        f"CAST(:{column} AS jsonb)" if column in json_columns else f":{column}"
        for column in columns
    )
    return text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values})")


async def _insert_rows(
    connection: AsyncConnection, entity_type: str, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    table = ENTITY_TABLES[entity_type]
    await connection.execute(_insert_statement(table, rows[0]), rows)


def _inventory_rows(
    bundle: DemoBundle, *, org_id: UUID, department_id: UUID
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entity_type, records in bundle.records.items():
        if entity_type not in ENTITY_TABLES:
            raise DemoSeedError(f"unsupported demo entity type: {entity_type}")
        rows.extend(
            {
                "id": uuid4(),
                "org_id": org_id,
                "department_id": department_id,
                "seed_run_id": bundle.run_id,
                "entity_type": entity_type,
                "record_id": record["id"],
            }
            for record in records
        )
    return rows


async def _load_demo_bundle_on_connection(
    connection: AsyncConnection,
    bundle: DemoBundle,
    *,
    org_id: UUID,
    department_id: UUID,
) -> None:
    """Write a demo run using an already-open, tenant-scoped transaction."""

    await _set_scope(connection, org_id=org_id, department_id=department_id)
    existing = await _active_run_id(connection, org_id=org_id, department_id=department_id)
    if existing is not None:
        raise DemoSeedError(
            "A flagged demo run already exists for this scope "
            f"({existing}). Run `wipe` first; this command will not blend runs."
        )
    await connection.execute(
        text(
            """
            INSERT INTO demo_seed_runs (
                id, org_id, department_id, label, seed_version, run_metadata
            ) VALUES (
                :id, :org_id, :department_id, :label, :seed_version,
                CAST(:run_metadata AS jsonb)
            )
            """
        ),
        {
            "id": bundle.run_id,
            "org_id": org_id,
            "department_id": department_id,
            "label": DEMO_LABEL,
            "seed_version": SEED_VERSION,
            "run_metadata": _json(
                {
                    "demo_seed": True,
                    "analytics_excluded": True,
                    "marker": DEMO_LABEL,
                    "contact_count": CONTACT_COUNT,
                    "call_count": CALL_COUNT,
                }
            ),
        },
    )
    for entity_type in ENTITY_TABLES:
        await _insert_rows(connection, entity_type, bundle.records.get(entity_type, []))
    inventory = _inventory_rows(bundle, org_id=org_id, department_id=department_id)
    await connection.execute(_insert_statement("demo_seed_records", inventory[0]), inventory)


async def load_demo_bundle(
    bundle: DemoBundle,
    *,
    org_id: UUID,
    department_id: UUID,
    database_url: str,
) -> None:
    """Load one demo run atomically after confirming there is no active run."""

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await _load_demo_bundle_on_connection(
                connection,
                bundle,
                org_id=org_id,
                department_id=department_id,
            )
    finally:
        await engine.dispose()


async def _wipe_demo_run_on_connection(
    connection: AsyncConnection, *, org_id: UUID, department_id: UUID
) -> tuple[UUID | None, dict[str, int]]:
    """Delete exactly the rows registered to this scope's flagged demo run."""

    deleted: dict[str, int] = {}
    await _set_scope(connection, org_id=org_id, department_id=department_id)
    run_id = await _active_run_id(connection, org_id=org_id, department_id=department_id)
    if run_id is None:
        return None, deleted
    for entity_type in WIPE_ORDER:
        result = await connection.execute(
            text(
                f"""
                DELETE FROM {ENTITY_TABLES[entity_type]}
                WHERE id IN (
                    SELECT record_id
                    FROM demo_seed_records
                    WHERE seed_run_id = :run_id AND entity_type = :entity_type
                )
                """
            ),
            {"run_id": run_id, "entity_type": entity_type},
        )
        deleted[entity_type] = max(result.rowcount or 0, 0)
    await connection.execute(
        text("DELETE FROM demo_seed_records WHERE seed_run_id = :run_id"), {"run_id": run_id}
    )
    await connection.execute(
        text("DELETE FROM demo_seed_runs WHERE id = :run_id"),
        {"run_id": run_id},
    )
    return run_id, deleted


async def wipe_demo_run(
    *, org_id: UUID, department_id: UUID, database_url: str
) -> tuple[UUID | None, dict[str, int]]:
    """Open a transaction and delete only the current flagged demo run."""

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            return await _wipe_demo_run_on_connection(
                connection, org_id=org_id, department_id=department_id
            )
    finally:
        await engine.dispose()


def remove_audio_fixtures(root: Path, run_id: UUID) -> bool:
    """Remove only the run-specific local fixture directory after a DB wipe."""

    target = _safe_audio_run_dir(root, run_id)
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


def _database_url() -> str:
    return os.environ.get("MANAGER_DATABASE_URL", get_settings().database_url)


def _require_demo_seed_enabled() -> None:
    if os.environ.get("MANAGER_DEMO_SEED_ENABLED") != DEMO_ENABLED_VALUE:
        raise DemoSeedError(
            "Refusing to touch data. Set MANAGER_DEMO_SEED_ENABLED=true and pass the matching "
            "confirmation flag; this script is for flagged demo data only."
        )


async def _seed_from_args(args: argparse.Namespace) -> int:
    _require_demo_seed_enabled()
    run_id = uuid4()
    fixtures = write_audio_fixtures(args.audio_output_dir, run_id)
    try:
        bundle = build_demo_bundle(
            run_id=run_id,
            org_id=args.org_id,
            department_id=args.department_id,
            audio_fixtures=fixtures,
        )
        await load_demo_bundle(
            bundle,
            org_id=args.org_id,
            department_id=args.department_id,
            database_url=_database_url(),
        )
    except BaseException:
        remove_audio_fixtures(args.audio_output_dir, run_id)
        raise
    counts = bundle.counts
    print(f"Loaded flagged demo run {run_id} ({DEMO_LABEL}).")
    print(
        f"Contacts: {counts['contacts']}; calls: {counts['calls']}; "
        f"playable WAV fixtures: {counts['recordings']}; follow-ups: {counts['follow_ups']}."
    )
    print(f"Featured five-click call: {bundle.featured_call_id}")
    print(
        "Stage the generated private WAV files from "
        f"{args.audio_output_dir / str(run_id)} under the storage prefix demo-seed/{run_id}/."
    )
    print("To remove this exact run: rerun with `wipe` for the same organization and department.")
    return 0


async def _wipe_from_args(args: argparse.Namespace) -> int:
    _require_demo_seed_enabled()
    run_id, deleted = await wipe_demo_run(
        org_id=args.org_id,
        department_id=args.department_id,
        database_url=_database_url(),
    )
    if run_id is None:
        print("No flagged demo run exists for this organization and department.")
        return 0
    removed_audio = remove_audio_fixtures(args.audio_output_dir, run_id)
    total = sum(deleted.values())
    print(f"Wiped flagged demo run {run_id}: {total} registered database rows removed.")
    print(f"Local playable-audio fixtures removed: {'yes' if removed_audio else 'already absent'}.")
    print(f"If staged externally, delete the private storage prefix demo-seed/{run_id}/ as well.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load or wipe explicitly flagged manager.sonnia.ai demo data."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add_scope_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--org-id", required=True, type=UUID)
        command.add_argument("--department-id", required=True, type=UUID)
        command.add_argument(
            "--audio-output-dir",
            type=Path,
            default=Path(".demo-audio"),
            help="local staging root for the run-specific private WAV fixtures",
        )

    seed = commands.add_parser("seed", help="load 200 contacts and 400 flagged demo calls")
    add_scope_arguments(seed)
    seed.add_argument(
        "--confirm-demo-seed",
        action="store_true",
        help="required acknowledgement that this is synthetic, wipeable demo data",
    )

    wipe = commands.add_parser("wipe", help="remove only the current flagged demo run")
    add_scope_arguments(wipe)
    wipe.add_argument(
        "--confirm-demo-wipe",
        action="store_true",
        help="required acknowledgement before removing the registered demo run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "seed":
        if not args.confirm_demo_seed:
            parser.error("seed requires --confirm-demo-seed")
        return asyncio.run(_seed_from_args(args))
    if args.command == "wipe":
        if not args.confirm_demo_wipe:
            parser.error("wipe requires --confirm-demo-wipe")
        return asyncio.run(_wipe_from_args(args))
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DemoSeedError as error:
        raise SystemExit(f"demo seed: {error}") from error
