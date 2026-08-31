"""Tenant-scoped live contracts for Phase 1 campaigns and CSV imports.

The router is assembled by ``app.main`` so this module stays independent of
the authentication implementation while every handler still depends on the
verified server-session context.  In particular, no route accepts an
organisation or department identifier from a browser request.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.materials import MaterialUploadError, parse_upload_body

MAX_CONTACT_CSV_BYTES = 2 * 1024 * 1024
MAX_CONTACT_CSV_ROWS = 1_000
PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


ACTIVE_DAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NUMBER_TO_NAME: dict[int, str] = {
    number: name for number, name in enumerate(ACTIVE_DAY_NAMES, start=1)
}
_DAY_NAME_TO_NUMBER: dict[str, int] = {
    name: number for number, name in _DAY_NUMBER_TO_NAME.items()
}
_TONE_DIALS: tuple[str, ...] = (
    "tone_formality",
    "tone_pace",
    "tone_persistence",
    "tone_warmth",
    "tone_depth",
)
# Resolved org defaults per the SON-1493 ruling (read-time fallbacks).
DEFAULT_MAX_ATTEMPTS_PER_LEAD = 3
DEFAULT_RETRY_DELAY_MINUTES = 30
DEFAULT_TIMEZONE = "UTC"


def _parse_iana_timezone(value: str | None) -> str | None:
    """Validate an IANA timezone string; None/blank means "not configured"."""

    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        ZoneInfo(cleaned)
    except Exception as error:  # unknown zone names raise varied errors
        raise ValueError(f"unknown IANA timezone: {value!r}") from error
    return cleaned


def _parse_wall_clock(value: str | None) -> time | None:
    """Parse 'HH:MM' (or 'HH:MM:SS') into a wall-clock time; None clears."""

    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    parts = cleaned.split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        raise ValueError(f"invalid wall-clock time {value!r}: use HH:MM")
    try:
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(int(parts[0]), int(parts[1]), second)
    except ValueError as error:
        raise ValueError(f"invalid wall-clock time {value!r}: use HH:MM") from error


def _format_wall_clock(value: object) -> str | None:
    """Render a stored wall-clock time as HH:MM for the wire contract."""

    if value is None:
        return None
    if isinstance(value, time):
        return value.strftime("%H:%M")
    text_value = str(value)
    return text_value[:5]


def _normalise_active_days(value: str | list[int] | None) -> list[str] | None:
    """Map form day numbers (1=Mon … 7=Sun) to a sorted unique name list."""

    if value is None:
        return None
    if isinstance(value, str):
        tokens = [token.strip() for token in value.split(",") if token.strip()]
        if not tokens:
            return None
        try:
            numbers = [int(token) for token in tokens]
        except ValueError as error:
            raise ValueError(
                "call_window_days must be day numbers 1 (Mon) … 7 (Sun)"
            ) from error
    else:
        numbers = list(value)
    names: list[str] = []
    for number in numbers:
        name = _DAY_NUMBER_TO_NAME.get(number)
        if name is None:
            raise ValueError("call_window_days day numbers must be 1 (Mon) … 7 (Sun)")
        names.append(name)
    return sorted(set(names), key=ACTIVE_DAY_NAMES.index) or None


class CampaignScheduleToneFields(BaseModel):
    """Optional flat schedule + tone-dial fields (SON-1493 ruling).

    Field names are the wire contract the campaign setup form already sends.
    Absent = not configured (NULL in the DB); on PATCH an explicit null clears
    a value. The two retry minutes unify into ``retry_delay_minutes`` for v1,
    so both must agree when both are sent.
    """

    model_config = ConfigDict(extra="ignore")

    timezone: str | None = Field(default=None, max_length=64)
    business_hours_start: str | None = Field(default=None, max_length=8)
    business_hours_end: str | None = Field(default=None, max_length=8)
    call_window_days: str | list[int] | None = Field(default=None, max_length=64)
    max_calls_per_day: int | None = Field(default=None, ge=0, le=100_000)
    max_attempts_per_lead: int | None = Field(default=None, ge=1, le=100)
    retry_minutes_voicemail: int | None = Field(default=None, ge=0, le=43_200)
    retry_minutes_no_answer: int | None = Field(default=None, ge=0, le=43_200)
    max_total_calls: int | None = Field(default=None, ge=0, le=10_000_000)
    tone_formality: int | None = Field(default=None, ge=-2, le=2)
    tone_pace: int | None = Field(default=None, ge=-2, le=2)
    tone_persistence: int | None = Field(default=None, ge=0, le=1)
    tone_warmth: int | None = Field(default=None, ge=-2, le=2)
    tone_depth: int | None = Field(default=None, ge=0, le=2)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        return _parse_iana_timezone(value)

    @field_validator("business_hours_start", "business_hours_end")
    @classmethod
    def _validate_wall_clock_fields(cls, value: str | None) -> str | None:
        # Validate the shape but keep the raw string; SQL binds parsed times.
        _parse_wall_clock(value)
        return value

    @field_validator("call_window_days")
    @classmethod
    def _validate_call_window_days(
        cls, value: str | list[int] | None
    ) -> str | list[int] | None:
        _normalise_active_days(value)
        return value

    @model_validator(mode="after")
    def _unify_retry_minutes(self) -> CampaignScheduleToneFields:
        provided = self.model_fields_set
        if {"retry_minutes_voicemail", "retry_minutes_no_answer"} <= provided:
            voicemail = self.retry_minutes_voicemail
            no_answer = self.retry_minutes_no_answer
            if voicemail is not None and no_answer is not None and voicemail != no_answer:
                raise ValueError(
                    "retry_minutes_voicemail and retry_minutes_no_answer must match: "
                    "v1 persists one unified retry delay"
                )
        return self


def _schedule_tone_columns(payload: CampaignScheduleToneFields) -> dict[str, object]:
    """Map provided schedule/tone fields onto campaigns columns.

    Fields absent from ``model_fields_set`` stay untouched (PATCH semantics:
    absent = unchanged; explicit null = clear). Keys are a fixed allow-list;
    callers must bind the result through parameterised SQL only.
    """

    provided = payload.model_fields_set
    columns: dict[str, object] = {}
    if "timezone" in provided:
        columns["schedule_timezone"] = _parse_iana_timezone(payload.timezone)
    if "business_hours_start" in provided:
        columns["call_window_start"] = _parse_wall_clock(payload.business_hours_start)
    if "business_hours_end" in provided:
        columns["call_window_end"] = _parse_wall_clock(payload.business_hours_end)
    if "call_window_days" in provided:
        columns["active_days"] = _normalise_active_days(payload.call_window_days)
    if "max_calls_per_day" in provided:
        columns["daily_call_cap"] = payload.max_calls_per_day
    if "max_attempts_per_lead" in provided:
        columns["max_attempts_per_lead"] = payload.max_attempts_per_lead
    if "max_total_calls" in provided:
        columns["max_total_calls"] = payload.max_total_calls
    if "retry_minutes_voicemail" in provided or "retry_minutes_no_answer" in provided:
        columns["retry_delay_minutes"] = (
            payload.retry_minutes_voicemail
            if "retry_minutes_voicemail" in provided
            else payload.retry_minutes_no_answer
        )
    for dial in _TONE_DIALS:
        if dial in provided:
            columns[dial] = getattr(payload, dial)
    return columns


class CampaignCreatePayload(CampaignScheduleToneFields):
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=2_000)
    target_ids: list[UUID] = Field(min_length=1, max_length=500)

    @field_validator("name", "objective")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("a value is required")
        return value

    @field_validator("target_ids")
    @classmethod
    def _unique_targets(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("target_ids must not contain duplicates")
        return value


class CampaignPatchPayload(CampaignScheduleToneFields):
    """PATCH body: every field optional. Schedule/tone fields are
    NULL-preserving (absent = unchanged, explicit null = clear); name and
    objective stay required-non-null when provided."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    objective: str | None = Field(default=None, min_length=1, max_length=2_000)

    @field_validator("name", "objective")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("a value is required")
        return value


@dataclass(frozen=True)
class CsvContactRow:
    row_index: int
    name: str
    phone: str
    email: str
    company: str
    errors: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _uuid(value: object) -> str:
    return str(value)


def _normalise_phone(value: str) -> str:
    return re.sub(r"[\s().-]", "", value.strip())


def _normalise_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _normalise_email(value: str) -> str:
    return value.strip().lower()


def _parse_csv(upload_name: str, body: bytes) -> list[CsvContactRow]:
    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CSV must use UTF-8 encoding",
        ) from exc

    reader = csv.DictReader(io.StringIO(decoded))
    if not reader.fieldnames:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CSV must include a header row",
        )

    header_names = {str(name or "").strip().lower() for name in reader.fieldnames}
    required_headers = {"name", "phone", "email", "company"}
    if not required_headers.issubset(header_names):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CSV must include name, phone, email, and company headers",
        )

    rows: list[CsvContactRow] = []
    for source_index, raw in enumerate(reader, start=2):
        if len(rows) >= MAX_CONTACT_CSV_ROWS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"CSV may contain at most {MAX_CONTACT_CSV_ROWS} data rows",
            )
        normalized = {
            str(key or "").strip().lower(): _normalise_text(value)
            for key, value in raw.items()
            if key is not None
        }
        name = normalized.get("name", "")
        phone = _normalise_phone(normalized.get("phone", ""))
        email = _normalise_email(normalized.get("email", ""))
        company = normalized.get("company", "")
        if not any((name, phone, email, company)):
            continue

        errors: list[str] = []
        if not name:
            errors.append("Name is required")
        elif len(name) > 160:
            errors.append("Name is too long")
        if not PHONE_PATTERN.fullmatch(phone):
            errors.append("Phone must include a valid country code")
        if email and not EMAIL_PATTERN.fullmatch(email):
            errors.append("Email address is invalid")
        if len(company) > 160:
            errors.append("Company name is too long")
        rows.append(
            CsvContactRow(
                row_index=source_index - 1,
                name=name,
                phone=phone,
                email=email,
                company=company,
                errors=tuple(errors),
            )
        )

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{upload_name} does not contain any contact rows",
        )
    return rows


async def _read_customer_csv(request: Request) -> tuple[str, list[CsvContactRow]]:
    body = await request.body()
    if len(body) > MAX_CONTACT_CSV_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="CSV is larger than the 2 MB import limit",
        )
    try:
        upload = parse_upload_body(
            body,
            request.headers.get("content-type"),
            request.headers.get("x-import-filename")
            or request.headers.get("x-material-filename")
            or request.headers.get("x-filename"),
        )
    except MaterialUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"CSV upload is invalid: {exc}",
        ) from exc
    if not upload.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only CSV files can be imported",
        )
    if len(upload.content) > MAX_CONTACT_CSV_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="CSV is larger than the 2 MB import limit",
        )
    return upload.filename, _parse_csv(upload.filename, upload.content)


async def _matching_contact(
    session: AsyncSession,
    *,
    phone: str,
    email: str,
) -> dict[str, object] | None:
    row = (
        (
            await session.execute(
                text(
                    """
                SELECT c.id, c.company_id
                FROM contacts AS c
                WHERE (
                    :phone <> '' AND EXISTS (
                        SELECT 1 FROM contact_phones AS cp
                        WHERE cp.contact_id = c.id AND cp.phone_e164 = :phone
                    )
                ) OR (
                    :email <> '' AND EXISTS (
                        SELECT 1 FROM contact_emails AS ce
                        WHERE ce.contact_id = c.id AND lower(ce.email) = :email
                    )
                )
                ORDER BY c.created_at ASC
                LIMIT 1
                """
                ),
                {"phone": phone, "email": email},
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


async def _find_or_create_company(
    session: AsyncSession,
    context: Any,
    company_name: str,
) -> UUID | None:
    if not company_name:
        return None
    existing = await session.scalar(
        text(
            """
            SELECT id FROM companies
            WHERE lower(name) = lower(:name)
            ORDER BY created_at ASC
            LIMIT 1
            """
        ),
        {"name": company_name},
    )
    if existing is not None:
        return UUID(str(existing))

    company_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO companies (id, org_id, department_id, name)
            VALUES (:id, :org_id, :department_id, :name)
            """
        ),
        {
            "id": str(company_id),
            "org_id": str(context.scope.org_id),
            "department_id": str(context.scope.department_id),
            "name": company_name,
        },
    )
    return company_id


async def _ensure_contact_methods(
    session: AsyncSession,
    context: Any,
    *,
    contact_id: UUID,
    phone: str,
    email: str,
) -> None:
    tenant = {
        "org_id": str(context.scope.org_id),
        "department_id": str(context.scope.department_id),
        "contact_id": str(contact_id),
    }
    if phone:
        has_phone = await session.scalar(
            text(
                """
                SELECT 1 FROM contact_phones
                WHERE contact_id = :contact_id AND phone_e164 = :phone
                """
            ),
            {**tenant, "phone": phone},
        )
        if has_phone is None:
            await session.execute(
                text(
                    """
                    INSERT INTO contact_phones
                        (id, org_id, department_id, contact_id, phone_e164, is_primary)
                    VALUES (:id, :org_id, :department_id, :contact_id, :phone, false)
                    """
                ),
                {**tenant, "id": str(uuid4()), "phone": phone},
            )
    if email:
        has_email = await session.scalar(
            text(
                """
                SELECT 1 FROM contact_emails
                WHERE contact_id = :contact_id AND lower(email) = :email
                """
            ),
            {**tenant, "email": email},
        )
        if has_email is None:
            await session.execute(
                text(
                    """
                    INSERT INTO contact_emails
                        (id, org_id, department_id, contact_id, email, is_primary)
                    VALUES (:id, :org_id, :department_id, :contact_id, :email, false)
                    """
                ),
                {**tenant, "id": str(uuid4()), "email": email},
            )


async def _create_contact(
    session: AsyncSession,
    context: Any,
    row: CsvContactRow,
) -> UUID:
    company_id = await _find_or_create_company(session, context, row.company)
    contact_id = uuid4()
    first_name, _, last_name = row.name.partition(" ")
    await session.execute(
        text(
            """
            INSERT INTO contacts
                (id, org_id, department_id, company_id, display_name, first_name, last_name)
            VALUES
                (:id, :org_id, :department_id, :company_id, :display_name, :first_name, :last_name)
            """
        ),
        {
            "id": str(contact_id),
            "org_id": str(context.scope.org_id),
            "department_id": str(context.scope.department_id),
            "company_id": str(company_id) if company_id else None,
            "display_name": row.name,
            "first_name": first_name or None,
            "last_name": last_name or None,
        },
    )
    await _ensure_contact_methods(
        session,
        context,
        contact_id=contact_id,
        phone=row.phone,
        email=row.email,
    )
    return contact_id


async def _preview_rows(
    session: AsyncSession,
    rows: list[CsvContactRow],
) -> tuple[list[dict[str, object]], int, int, int]:
    preview_rows: list[dict[str, object]] = []
    created = merged = skipped = 0
    seen_identifiers: set[tuple[str, str]] = set()

    for row in rows:
        status_value: str
        if not row.is_valid:
            status_value = "skipped"
        else:
            identifiers = {("phone", row.phone)}
            if row.email:
                identifiers.add(("email", row.email))
            matching = await _matching_contact(session, phone=row.phone, email=row.email)
            if matching is not None or bool(identifiers & seen_identifiers):
                status_value = "merged"
            else:
                status_value = "created"
            seen_identifiers.update(identifiers)

        if status_value == "created":
            created += 1
        elif status_value == "merged":
            merged += 1
        else:
            skipped += 1
        preview_rows.append(
            {
                "row_index": row.row_index,
                "name": row.name,
                "phone": row.phone,
                "email": row.email,
                "company": row.company,
                "status": status_value,
            }
        )
    return preview_rows, created, merged, skipped


async def _import_rows(
    session: AsyncSession,
    context: Any,
    *,
    filename: str,
    rows: list[CsvContactRow],
) -> dict[str, object]:
    import_id = uuid4()
    imported_rows: list[dict[str, object]] = []
    linked_contacts: list[tuple[UUID, int]] = []
    created = merged = skipped = 0
    linked_ids: set[UUID] = set()

    for row in rows:
        if not row.is_valid:
            skipped += 1
            imported_rows.append(
                {
                    "row_index": row.row_index,
                    "name": row.name,
                    "phone": row.phone,
                    "email": row.email,
                    "company": row.company,
                    "status": "invalid",
                    "errors": list(row.errors),
                }
            )
            continue

        matching = await _matching_contact(session, phone=row.phone, email=row.email)
        if matching is None:
            contact_id = await _create_contact(session, context, row)
            imported_status = "valid"
            created += 1
        else:
            contact_id = UUID(str(matching["id"]))
            await _ensure_contact_methods(
                session,
                context,
                contact_id=contact_id,
                phone=row.phone,
                email=row.email,
            )
            imported_status = "duplicate"
            merged += 1

        if contact_id not in linked_ids:
            linked_contacts.append((contact_id, row.row_index))
            linked_ids.add(contact_id)
        imported_rows.append(
            {
                "row_index": row.row_index,
                "name": row.name,
                "phone": row.phone,
                "email": row.email,
                "company": row.company,
                "status": imported_status,
                "errors": [],
            }
        )

    await session.execute(
        text(
            """
            INSERT INTO customer_list_imports
                (id, org_id, department_id, filename, total_rows, valid_rows, invalid_rows,
                 status, rows, created_by_user_id)
            VALUES
                (:id, :org_id, :department_id, :filename, :total_rows, :valid_rows, :invalid_rows,
                 'completed', CAST(:rows AS jsonb), :created_by_user_id)
            """
        ),
        {
            "id": str(import_id),
            "org_id": str(context.scope.org_id),
            "department_id": str(context.scope.department_id),
            "filename": filename,
            "total_rows": len(rows),
            "valid_rows": created + merged,
            "invalid_rows": skipped,
            "rows": json.dumps(imported_rows),
            "created_by_user_id": str(context.user.id),
        },
    )
    for contact_id, row_index in linked_contacts:
        await session.execute(
            text(
                """
                INSERT INTO customer_list_import_contacts
                    (id, org_id, department_id, import_id, contact_id, row_index)
                VALUES (:id, :org_id, :department_id, :import_id, :contact_id, :row_index)
                """
            ),
            {
                "id": str(uuid4()),
                "org_id": str(context.scope.org_id),
                "department_id": str(context.scope.department_id),
                "import_id": str(import_id),
                "contact_id": str(contact_id),
                "row_index": row_index,
            },
        )

    return {
        "id": str(import_id),
        "filename": filename,
        "uploaded_at": datetime.now().astimezone().isoformat(),
        "total_rows": len(rows),
        "valid_rows": created + merged,
        "invalid_rows": skipped,
        "status": "completed",
        "rows": imported_rows,
    }


def _import_response(row: dict[str, object]) -> dict[str, object]:
    raw_rows = row["rows"]
    if isinstance(raw_rows, str):
        raw_rows = json.loads(raw_rows)
    return {
        "id": _uuid(row["id"]),
        "filename": str(row["filename"]),
        "uploaded_at": _iso(row["uploaded_at"]),
        "total_rows": int(row["total_rows"]),
        "valid_rows": int(row["valid_rows"]),
        "invalid_rows": int(row["invalid_rows"]),
        "status": str(row["status"]),
        "rows": raw_rows,
    }


def _schedule_tone_response(
    row: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Build (flat echo, schedule block, tone block) per the SON-1493 contract.

    The truth blocks carry configured/value/resolved per field so the UI never
    invents persistence; resolved falls back to the org defaults for timezone
    (UTC), attempts (3) and retry delay (30). Null resolved = policy default
    (24/7 window, every day, unlimited).
    """

    timezone = row.get("schedule_timezone")
    active_days = row.get("active_days")
    day_names = list(active_days) if active_days else None
    call_window_days = (
        ",".join(str(_DAY_NAME_TO_NUMBER[name]) for name in day_names)
        if day_names
        else None
    )
    attempts = row.get("max_attempts_per_lead")
    retry_delay = row.get("retry_delay_minutes")

    def _entry(value: object, resolved: object = None) -> dict[str, object]:
        return {"configured": value is not None, "value": value, "resolved": resolved}

    schedule_block = {
        "timezone": _entry(timezone, timezone if timezone is not None else DEFAULT_TIMEZONE),
        "call_window_start": _entry(_format_wall_clock(row.get("call_window_start"))),
        "call_window_end": _entry(_format_wall_clock(row.get("call_window_end"))),
        "active_days": _entry(day_names),
        "max_attempts_per_lead": _entry(
            attempts,
            attempts if attempts is not None else DEFAULT_MAX_ATTEMPTS_PER_LEAD,
        ),
        "retry_delay_minutes": _entry(
            retry_delay,
            retry_delay if retry_delay is not None else DEFAULT_RETRY_DELAY_MINUTES,
        ),
        "daily_call_cap": _entry(row.get("daily_call_cap")),
        "max_total_calls": _entry(row.get("max_total_calls")),
    }
    tone_block = {
        dial.removeprefix("tone_"): _entry(row.get(dial), row.get(dial))
        for dial in _TONE_DIALS
    }
    flat = {
        "timezone": timezone,
        "business_hours_start": _format_wall_clock(row.get("call_window_start")),
        "business_hours_end": _format_wall_clock(row.get("call_window_end")),
        "call_window_days": call_window_days,
        "max_calls_per_day": row.get("daily_call_cap"),
        "max_attempts_per_lead": attempts,
        "retry_minutes_voicemail": retry_delay,
        "retry_minutes_no_answer": retry_delay,
        "max_total_calls": row.get("max_total_calls"),
    }
    return flat, schedule_block, tone_block


def _campaign_response(row: dict[str, object]) -> dict[str, object]:
    target_ids = row["target_ids"] or []
    flat, schedule_block, tone_block = _schedule_tone_response(row)
    return {
        "id": _uuid(row["id"]),
        "name": str(row["name"]),
        "objective": str(row["objective"]),
        "status": str(row["status"]),
        "target_ids": [_uuid(value) for value in target_ids],
        "created_at": _iso(row["created_at"]),
        "launched_at": _iso(row["launched_at"]),
        # Calls have no campaign foreign key in the current canonical model;
        # targets are the honest queued-call total until execution persists it.
        "total_calls": int(row["total_calls"]),
        "completed_calls": 0,
        "updated_at": _iso(row.get("updated_at")),
        **flat,
        "schedule": schedule_block,
        "tone": tone_block,
    }


async def _campaign_by_id(session: AsyncSession, campaign_id: UUID) -> dict[str, object] | None:
    row = (
        (
            await session.execute(
                text(
                    """
                SELECT c.id, c.name, c.objective, c.status, c.created_at, c.launched_at,
                    c.schedule_timezone, c.call_window_start, c.call_window_end,
                    c.active_days, c.max_attempts_per_lead, c.retry_delay_minutes,
                    c.daily_call_cap, c.max_total_calls,
                    c.tone_formality, c.tone_pace, c.tone_persistence,
                    c.tone_warmth, c.tone_depth, c.updated_at,
                    COALESCE(
                        array_agg(ct.contact_id) FILTER (WHERE ct.contact_id IS NOT NULL),
                        ARRAY[]::uuid[]
                    ) AS target_ids,
                    count(ct.contact_id)::int AS total_calls
                FROM campaigns AS c
                LEFT JOIN campaign_targets AS ct ON ct.campaign_id = c.id
                WHERE c.id = :campaign_id
                GROUP BY c.id
                """
                ),
                {"campaign_id": str(campaign_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


async def preview_contact_csv_upload(
    request: Request,
    context: Any,
) -> dict[str, object]:
    """Preview the multipart CSV contract without persisting customer data."""

    filename, rows = await _read_customer_csv(request)
    preview_rows, created, merged, skipped = await _preview_rows(context.session, rows)
    return {
        "filename": filename,
        "total_rows": len(rows),
        "created": created,
        "merged": merged,
        "skipped": skipped,
        "rows": preview_rows,
    }


def build_router(
    auth_dependency: Callable[..., object],
    *,
    include_contact_import_preview: bool = True,
) -> APIRouter:
    router = APIRouter()
    context_dependency = Depends(auth_dependency)

    @router.get("/api/contacts")
    async def list_contacts(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        search: str | None = Query(default=None, max_length=160),
        company: str | None = Query(default=None, max_length=160),
        do_not_contact: bool | None = Query(default=None),
        has_calls: bool | None = Query(default=None),
        context: Any = context_dependency,
    ) -> dict[str, object]:
        clauses: list[str] = []
        params: dict[str, object] = {}
        if search and search.strip():
            clauses.append(
                """(
                    c.display_name ILIKE :search_pattern
                    OR EXISTS (
                        SELECT 1 FROM contact_phones AS cp_search
                        WHERE cp_search.contact_id = c.id
                          AND cp_search.phone_e164 ILIKE :search_pattern
                    )
                    OR EXISTS (
                        SELECT 1 FROM contact_emails AS ce_search
                        WHERE ce_search.contact_id = c.id
                          AND ce_search.email ILIKE :search_pattern
                    )
                )"""
            )
            params["search_pattern"] = f"%{search.strip()}%"
        if company and company.strip():
            clauses.append("co.name ILIKE :company_pattern")
            params["company_pattern"] = f"%{company.strip()}%"
        if do_not_contact is not None:
            clauses.append("c.do_not_contact = :do_not_contact")
            params["do_not_contact"] = do_not_contact
        if has_calls is True:
            clauses.append("EXISTS (SELECT 1 FROM calls AS ca_has WHERE ca_has.contact_id = c.id)")
        elif has_calls is False:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM calls AS ca_none WHERE ca_none.contact_id = c.id)"
            )
        filters = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = await context.session.scalar(
            text(
                f"""
                SELECT count(*)::int
                FROM contacts AS c
                LEFT JOIN companies AS co ON co.id = c.company_id
                {filters}
                """
            ),
            params,
        )
        result = await context.session.execute(
            text(
                f"""
                SELECT
                    c.id,
                    c.display_name AS name,
                    co.name AS company,
                    c.job_title AS title,
                    (
                        SELECT cp.phone_e164 FROM contact_phones AS cp
                        WHERE cp.contact_id = c.id
                        ORDER BY cp.is_primary DESC, cp.created_at ASC
                        LIMIT 1
                    ) AS primary_phone,
                    (
                        SELECT ce.email FROM contact_emails AS ce
                        WHERE ce.contact_id = c.id
                        ORDER BY ce.is_primary DESC, ce.created_at ASC
                        LIMIT 1
                    ) AS primary_email,
                    c.do_not_contact,
                    (
                        SELECT count(*)::int FROM calls AS ca
                        WHERE ca.contact_id = c.id
                    ) AS total_calls,
                    (
                        SELECT max(ca.created_at) FROM calls AS ca
                        WHERE ca.contact_id = c.id
                    ) AS last_interaction,
                    c.created_at,
                    c.updated_at
                FROM contacts AS c
                LEFT JOIN companies AS co ON co.id = c.company_id
                {filters}
                ORDER BY c.display_name ASC, c.id ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": page_size, "offset": (page - 1) * page_size},
        )
        items = []
        for row in result.mappings():
            value = dict(row)
            phone = value["primary_phone"]
            email = value["primary_email"]
            items.append(
                {
                    "id": _uuid(value["id"]),
                    "name": str(value["name"]),
                    "company": value["company"],
                    "title": value["title"],
                    "phone": phone,
                    "email": email,
                    "primary_phone": phone,
                    "primary_email": email,
                    "do_not_contact": bool(value["do_not_contact"]),
                    "total_calls": int(value["total_calls"]),
                    "last_interaction": _iso(value["last_interaction"]),
                    "created_at": _iso(value["created_at"]),
                    "updated_at": _iso(value["updated_at"]),
                }
            )
        return {"items": items, "total": int(total or 0), "page": page, "page_size": page_size}

    async def preview_contact_import(
        request: Request,
        context: Any = context_dependency,
    ) -> dict[str, object]:
        return await preview_contact_csv_upload(request, context)

    if include_contact_import_preview:
        router.add_api_route(
            "/api/contacts/import/preview",
            preview_contact_import,
            methods=["POST"],
        )

    @router.get("/api/imports")
    async def list_customer_imports(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        context: Any = context_dependency,
    ) -> dict[str, object]:
        total = await context.session.scalar(
            text("SELECT count(*)::int FROM customer_list_imports")
        )
        result = await context.session.execute(
            text(
                """
                SELECT id, filename, uploaded_at, total_rows, valid_rows, invalid_rows, status, rows
                FROM customer_list_imports
                ORDER BY uploaded_at DESC, id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": page_size, "offset": (page - 1) * page_size},
        )
        return {
            "items": [_import_response(dict(row)) for row in result.mappings()],
            "total": int(total or 0),
            "page": page,
            "page_size": page_size,
        }

    @router.post("/api/imports", status_code=status.HTTP_201_CREATED)
    async def import_customer_list(
        request: Request,
        context: Any = context_dependency,
    ) -> dict[str, object]:
        filename, rows = await _read_customer_csv(request)
        return await _import_rows(context.session, context, filename=filename, rows=rows)

    @router.get("/api/imports/{import_id}/contacts")
    async def list_import_contacts(
        import_id: UUID,
        context: Any = context_dependency,
    ) -> list[dict[str, object]]:
        exists = await context.session.scalar(
            text("SELECT 1 FROM customer_list_imports WHERE id = :import_id"),
            {"import_id": str(import_id)},
        )
        if exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found")
        result = await context.session.execute(
            text(
                """
                SELECT
                    c.id,
                    cilc.import_id,
                    c.display_name AS name,
                    COALESCE((
                        SELECT cp.phone_e164 FROM contact_phones AS cp
                        WHERE cp.contact_id = c.id
                        ORDER BY cp.is_primary DESC, cp.created_at ASC
                        LIMIT 1
                    ), '') AS phone,
                    COALESCE((
                        SELECT ce.email FROM contact_emails AS ce
                        WHERE ce.contact_id = c.id
                        ORDER BY ce.is_primary DESC, ce.created_at ASC
                        LIMIT 1
                    ), '') AS email,
                    COALESCE(co.name, '') AS company,
                    c.created_at
                FROM customer_list_import_contacts AS cilc
                JOIN contacts AS c ON c.id = cilc.contact_id
                LEFT JOIN companies AS co ON co.id = c.company_id
                WHERE cilc.import_id = :import_id
                ORDER BY cilc.row_index ASC
                """
            ),
            {"import_id": str(import_id)},
        )
        return [
            {
                "id": _uuid(row["id"]),
                "import_id": _uuid(row["import_id"]),
                "name": str(row["name"]),
                "phone": str(row["phone"]),
                "email": str(row["email"]),
                "company": str(row["company"]),
                "created_at": _iso(row["created_at"]),
            }
            for row in result.mappings()
        ]

    @router.get("/api/campaigns")
    async def list_campaigns(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        status_filter: str | None = Query(default=None, alias="status", max_length=20),
        context: Any = context_dependency,
    ) -> dict[str, object]:
        where = "WHERE c.status = :status" if status_filter else ""
        params: dict[str, object] = {"status": status_filter} if status_filter else {}
        total = await context.session.scalar(
            text(f"SELECT count(*)::int FROM campaigns AS c {where}"), params
        )
        result = await context.session.execute(
            text(
                f"""
                SELECT c.id, c.name, c.objective, c.status, c.created_at, c.launched_at,
                    c.schedule_timezone, c.call_window_start, c.call_window_end,
                    c.active_days, c.max_attempts_per_lead, c.retry_delay_minutes,
                    c.daily_call_cap, c.max_total_calls,
                    c.tone_formality, c.tone_pace, c.tone_persistence,
                    c.tone_warmth, c.tone_depth, c.updated_at,
                    COALESCE(
                        array_agg(ct.contact_id) FILTER (WHERE ct.contact_id IS NOT NULL),
                        ARRAY[]::uuid[]
                    ) AS target_ids,
                    count(ct.contact_id)::int AS total_calls
                FROM campaigns AS c
                LEFT JOIN campaign_targets AS ct ON ct.campaign_id = c.id
                {where}
                GROUP BY c.id
                ORDER BY c.created_at DESC, c.id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": page_size, "offset": (page - 1) * page_size},
        )
        return {
            "items": [_campaign_response(dict(row)) for row in result.mappings()],
            "total": int(total or 0),
            "page": page,
            "page_size": page_size,
        }

    @router.post("/api/campaigns", status_code=status.HTTP_201_CREATED)
    async def create_campaign(
        payload: CampaignCreatePayload,
        context: Any = context_dependency,
    ) -> dict[str, object]:
        for contact_id in payload.target_ids:
            contact_exists = await context.session.scalar(
                text("SELECT 1 FROM contacts WHERE id = :contact_id"),
                {"contact_id": str(contact_id)},
            )
            if contact_exists is None:
                # RLS deliberately makes hidden tenant records indistinguishable
                # from absent records.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Contact not found",
                )

        campaign_id = uuid4()
        settings = _schedule_tone_columns(payload)
        setting_columns = list(settings)
        column_list = ", ".join(
            ["id", "org_id", "department_id", "name", "objective", *setting_columns, "status"]
        )
        placeholder_list = ", ".join(
            [":id", ":org_id", ":department_id", ":name", ":objective", *[f":{c}" for c in setting_columns], "'draft'"]
        )
        insert_params: dict[str, object] = {
            "id": str(campaign_id),
            "org_id": str(context.scope.org_id),
            "department_id": str(context.scope.department_id),
            "name": payload.name,
            "objective": payload.objective,
            **settings,
        }
        await context.session.execute(
            text(
                f"""
                INSERT INTO campaigns ({column_list})
                VALUES ({placeholder_list})
                """
            ),
            insert_params,
        )
        for contact_id in payload.target_ids:
            await context.session.execute(
                text(
                    """
                    INSERT INTO campaign_targets
                        (id, org_id, department_id, campaign_id, contact_id)
                    VALUES (:id, :org_id, :department_id, :campaign_id, :contact_id)
                    """
                ),
                {
                    "id": str(uuid4()),
                    "org_id": str(context.scope.org_id),
                    "department_id": str(context.scope.department_id),
                    "campaign_id": str(campaign_id),
                    "contact_id": str(contact_id),
                },
            )
        campaign = await _campaign_by_id(context.session, campaign_id)
        if campaign is None:  # Defensive: RLS must expose a row we just inserted.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
        return _campaign_response(campaign)

    @router.get("/api/campaigns/{campaign_id}")
    async def get_campaign(
        campaign_id: UUID,
        context: Any = context_dependency,
    ) -> dict[str, object]:
        campaign = await _campaign_by_id(context.session, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
        return _campaign_response(campaign)

    @router.patch("/api/campaigns/{campaign_id}")
    async def update_campaign(
        campaign_id: UUID,
        payload: CampaignPatchPayload,
        context: Any = context_dependency,
    ) -> dict[str, object]:
        campaign = await _campaign_by_id(context.session, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
        provided = payload.model_fields_set
        assignments: dict[str, object] = {}
        for text_field in ("name", "objective"):
            if text_field in provided:
                value = getattr(payload, text_field)
                if value is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"{text_field} cannot be null",
                    )
                assignments[text_field] = value
        assignments.update(_schedule_tone_columns(payload))
        if not assignments:
            # Absent keys = unchanged; an empty PATCH is an honest no-op.
            return _campaign_response(campaign)
        set_clause = ", ".join(f"{column} = :{column}" for column in assignments)
        await context.session.execute(
            text(
                f"""
                UPDATE campaigns
                SET {set_clause}, updated_at = now()
                WHERE id = :campaign_id
                """
            ),
            {**assignments, "campaign_id": str(campaign_id)},
        )
        updated = await _campaign_by_id(context.session, campaign_id)
        if updated is None:  # Defensive: protect hidden records from disclosure.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
        return _campaign_response(updated)

    async def _change_campaign_status(
        context: Any,
        campaign_id: UUID,
        *,
        expected_status: str,
        next_status: str,
        set_launched_at: bool = False,
    ) -> dict[str, object]:
        campaign = await _campaign_by_id(context.session, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
        if campaign["status"] != expected_status:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Campaign cannot be changed from its current state",
            )
        launched_sql = ", launched_at = COALESCE(launched_at, now())" if set_launched_at else ""
        await context.session.execute(
            text(
                f"""
                UPDATE campaigns
                SET status = :next_status, updated_at = now(){launched_sql}
                WHERE id = :campaign_id
                """
            ),
            {"next_status": next_status, "campaign_id": str(campaign_id)},
        )
        changed = await _campaign_by_id(context.session, campaign_id)
        if changed is None:  # Defensive: protect hidden records from disclosure.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
        return _campaign_response(changed)

    @router.post("/api/campaigns/{campaign_id}/launch")
    async def launch_campaign(
        campaign_id: UUID,
        context: Any = context_dependency,
    ) -> dict[str, object]:
        return await _change_campaign_status(
            context,
            campaign_id,
            expected_status="draft",
            next_status="active",
            set_launched_at=True,
        )

    @router.post("/api/campaigns/{campaign_id}/pause")
    async def pause_campaign(
        campaign_id: UUID,
        context: Any = context_dependency,
    ) -> dict[str, object]:
        return await _change_campaign_status(
            context,
            campaign_id,
            expected_status="active",
            next_status="paused",
        )

    return router
