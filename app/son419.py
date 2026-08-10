"""SON-419 service layer.

Pure, dependency-light helpers for the four feature areas:
  * versioned instructions (create/edit with in-effect-since),
  * privacy-scoped task querying,
  * a privacy-respecting activity feed,
  * CSV contact import with column mapping, preview, dedupe and summary.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.schemas import ContactImportColumnMapping, ContactImportPreviewRow

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def slugify(value: str) -> str:
    """Lowercase, ASCII-dash slug used to key instruction topics."""
    lowered = value.strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


@dataclass(frozen=True)
class InstructionEdit:
    """A new version of an instruction topic."""

    slug: str
    title: str
    body: str
    applies_to: str
    version: int
    effective_from: datetime


def next_instruction_version(
    *,
    slug: str,
    title: str,
    body: str,
    applies_to: str,
    current_version: int,
    effective_from: datetime | None,
) -> InstructionEdit:
    """Produce the next version when editing (or the first when creating).

    A change takes effect from ``effective_from`` (default: now). The active
    version is superseded by the caller inside one transaction so the history
    is complete and the partial unique index (one active per slug) holds.
    """
    return InstructionEdit(
        slug=slugify(slug),
        title=title,
        body=body,
        applies_to=applies_to,
        version=(current_version + 1) if current_version else 1,
        effective_from=effective_from or datetime.now(UTC),
    )


# -- CSV import -----------------------------------------------------------

@dataclass(frozen=True)
class CsvRow:
    index: int
    values: dict[str, str]


def parse_csv(text_value: str) -> list[CsvRow]:
    """Parse CSV text into ordered rows without trusting headers."""
    sample = text_value[:2000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",")
    except csv.Error:
        dialect = csv.excel
    rows: list[CsvRow] = []
    for index, raw in enumerate(csv.reader(io.StringIO(text_value), dialect)):
        values = {}
        for position, value in enumerate(raw):
            values[str(position)] = value.strip()
        rows.append(CsvRow(index=index, values=values))
    return rows


def infer_column_mapping(columns: list[str]) -> ContactImportColumnMapping:
    """Guess the semantic mapping from raw CSV header labels."""
    mapping = ContactImportColumnMapping()
    for column in columns:
        label = column.strip().lower()
        if any(token in label for token in ("name", "display", "full")):
            mapping.display_name = column
        elif label in ("first", "first name", "firstname", "given"):
            mapping.first_name = column
        elif label in ("last", "last name", "lastname", "family", "surname"):
            mapping.last_name = column
        elif any(token in label for token in ("title", "role", "position", "job")):
            mapping.job_title = column
        elif "email" in label:
            mapping.email = column
        elif any(token in label for token in ("phone", "mobile", "tel", "number")):
            mapping.phone = column
        elif any(token in label for token in ("company", "organisation", "organization", "org")):
            mapping.company = column
    return mapping


@dataclass(frozen=True)
class ImportPreview:
    mapping: ContactImportColumnMapping
    created_estimate: int
    merged_estimate: int
    skipped_estimate: int
    notes: list[str]


def preview_import(
    *,
    columns: list[str],
    rows: list[ContactImportPreviewRow],
    mapping: ContactImportColumnMapping | None = None,
) -> ImportPreview:
    """Estimate created/merged/skipped counts and surface mapping notes.

    Dedupe keys are email (normalised) and phone (E.164-ish). A row that
    carries neither is skipped; a row whose key already appears within the
    same file is skipped as a duplicate; otherwise one row per untouched key
    counts as created. The caller applies the same key logic at commit time.
    """
    effective_mapping = mapping or infer_column_mapping(columns)
    notes: list[str] = []
    if not effective_mapping.display_name and not (
        effective_mapping.first_name or effective_mapping.last_name
    ):
        notes.append("No name column mapped; display names will be blank.")
    if not effective_mapping.email and not effective_mapping.phone:
        notes.append("Neither email nor phone is mapped; no contacts can be created.")
    if not effective_mapping.email and effective_mapping.phone:
        notes.append("Email is not mapped; dedupe relies on phone only.")

    seen_keys: set[str] = set()
    created = 0
    merged = 0
    for row in rows:
        keys = _dedupe_keys(row.values, effective_mapping)
        if not keys:
            continue
        matched = bool(seen_keys & keys)
        if matched:
            merged += 1
            seen_keys.update(keys)
        else:
            created += 1
            seen_keys.update(keys)
    skipped = len(rows) - created - merged
    return ImportPreview(
        mapping=effective_mapping,
        created_estimate=created,
        merged_estimate=merged,
        skipped_estimate=skipped,
        notes=notes,
    )


def _cell(row_values: dict[str, str], column: str | None) -> str:
    if not column:
        return ""
    return row_values.get(column, row_values.get(str(column), "")).strip()


def _normalise_email(value: str) -> str:
    return value.strip().lower()


def _normalise_phone(value: str) -> str:
    digits = re.sub(r"[^\d]", "", value)
    if not digits:
        return ""
    if digits.startswith("0") and len(digits) <= 10:
        digits = "65" + digits.lstrip("0")
    return f"+{digits}"


def _dedupe_keys(
    row_values: dict[str, str], mapping: ContactImportColumnMapping
) -> set[str]:
    keys: set[str] = set()
    email = _normalise_email(_cell(row_values, mapping.email))
    if email and EMAIL_RE.match(email):
        keys.add(f"email:{email}")
    phone = _normalise_phone(_cell(row_values, mapping.phone))
    if phone and len(phone) >= 8:
        keys.add(f"phone:{phone}")
    return keys


def dedupe_keys_for_row(
    row_values: dict[str, str], mapping: ContactImportColumnMapping
) -> set[str]:
    """Public dedupe-key helper used by the commit route."""
    return _dedupe_keys(row_values, mapping)


def display_name_for_row(
    row_values: dict[str, str], mapping: ContactImportColumnMapping
) -> str:
    display = _cell(row_values, mapping.display_name)
    if display:
        return display
    first = _cell(row_values, mapping.first_name)
    last = _cell(row_values, mapping.last_name)
    return f"{first} {last}".strip() or "(no name)"
