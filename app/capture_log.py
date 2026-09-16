"""CP2/CP3 append-only capture logging at the Telnyx webhook boundary.

SON-1458, per the approved SON-1442 design outline v1 (comment dd132f00):

- CP2 - webhook receipt boundary: raw body verbatim as received (secret
  headers redacted), receive timestamp, signature-verification result
  (algorithm + pass/fail/skipped).  Append-only.
- CP3 - ingestion trace: idempotency/dedupe decision, event-to-entity
  mapping (call, transcript, recording refs), deployed build git SHA,
  processing latency.  Append-only.

Bounds (CEO, standing): capture logging must not alter webhook processing
semantics.  Every writer below is therefore best-effort - failures are
logged and swallowed - and each capture commits on its own short-lived
session, so request transactions are never held open or rolled back by
capture activity.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.config import get_settings

logger = logging.getLogger("manager.capture_log")

SIGNATURE_ALGORITHM = "hmac-sha256"
_REDACTED = "[REDACTED]"
_SENSITIVE_NAME_PARTS = (
    "authorization",
    "cookie",
    "token",
    "secret",
    "password",
    "api-key",
    "apikey",
    "key",
)

_RECEIPT_INSERT = text(
    """
    INSERT INTO webhook_receipt_capture (
        id, source, method, path, remote_addr, headers_redacted,
        raw_body, body_bytes, signature_algorithm, signature_result,
        signature_detail, tenant_scope, event_id
    ) VALUES (
        :id, :source, :method, :path, :remote_addr,
        CAST(:headers_redacted AS jsonb), :raw_body, :body_bytes,
        :signature_algorithm, :signature_result, :signature_detail,
        :tenant_scope, :event_id
    )
    """
)

_TRACE_INSERT = text(
    """
    INSERT INTO ingestion_trace_capture (
        id, receipt_id, event_id, event_type, dedupe_decision, outcome,
        call_id, contact_id, transcript_ref, recording_refs, build_sha,
        latency_ms
    ) VALUES (
        :id, :receipt_id, :event_id, :event_type, :dedupe_decision,
        :outcome, :call_id, :contact_id, :transcript_ref,
        CAST(:recording_refs AS jsonb), :build_sha, :latency_ms
    )
    """
)


def evaluate_telnyx_signature(
    secret: str | None, supplied: str | None, body: bytes
) -> tuple[str, str, str | None]:
    """Pure CP2 signature decision: (algorithm, result, detail).

    Mirrors the historical webhook verification semantics exactly: no
    configured secret means verification is skipped; a missing or
    mismatched signature fails closed.
    """

    if not secret:
        return (SIGNATURE_ALGORITHM, "skipped", "no webhook secret configured")
    expected = hmac.new(secret.encode("utf-8"), body, "sha256").hexdigest()
    if not supplied or not hmac.compare_digest(supplied, expected):
        return (SIGNATURE_ALGORITHM, "fail", "missing or mismatched signature")
    return (SIGNATURE_ALGORITHM, "pass", None)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of headers with credential-bearing values redacted."""

    redacted: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if any(part in lowered for part in _SENSITIVE_NAME_PARTS):
            redacted[name] = _REDACTED
        else:
            redacted[name] = value
    return redacted


@dataclass(frozen=True)
class EventContext:
    event_id: str
    event_type: str | None
    recording_refs: tuple[str, ...]
    transcript_ref: str | None


def extract_event_context(payload: Mapping[str, Any]) -> EventContext:
    """Best-effort CP3 entity references from a Telnyx event payload."""

    data = payload.get("data")
    data = data if isinstance(data, Mapping) else {}
    body = data.get("payload")
    body = body if isinstance(body, Mapping) else {}

    raw_event_id = payload.get("id") or data.get("id")
    event_id = str(raw_event_id).strip()[:256] if raw_event_id else "unknown"

    raw_event_type = (
        data.get("event_type") or payload.get("event_type") or body.get("event_type")
    )
    event_type = str(raw_event_type).strip()[:160] if raw_event_type else None

    refs: list[str] = []
    recordings = body.get("recording_urls") or body.get("recordings") or []
    if isinstance(recordings, list):
        for item in recordings:
            if isinstance(item, Mapping) and isinstance(item.get("url"), str):
                refs.append(item["url"])
            elif isinstance(item, str):
                refs.append(item)

    transcript = body.get("transcript_url") or body.get("transcript_text")
    transcript_ref = str(transcript)[:512] if transcript else None

    return EventContext(event_id, event_type, tuple(refs), transcript_ref)


def _peek_event_id(body: bytes) -> str | None:
    """Lenient event-id extraction for CP2 rows (never raises)."""

    try:
        payload = json.loads(body)
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    event_id = extract_event_context(payload).event_id
    return None if event_id == "unknown" else event_id


async def capture_webhook_receipt(
    headers: Mapping[str, str],
    body: bytes,
    *,
    signature_algorithm: str | None,
    signature_result: str,
    signature_detail: str | None = None,
    tenant_scope: str | None = None,
    source: str = "telnyx",
    method: str | None = None,
    path: str | None = None,
    remote_addr: str | None = None,
) -> UUID | None:
    """CP2 writer.  Returns the receipt id, or None when disabled/failed."""

    settings = get_settings()
    if not settings.capture_log_enabled:
        return None
    from app.database import SessionFactory  # lazy: keeps unit tests DB-free

    try:
        receipt_id = uuid4()
        async with SessionFactory() as session:
            async with session.begin():
                await session.execute(
                    _RECEIPT_INSERT,
                    {
                        "id": str(receipt_id),
                        "source": source,
                        "method": method,
                        "path": path,
                        "remote_addr": remote_addr,
                        "headers_redacted": json.dumps(redact_headers(headers)),
                        "raw_body": body.decode("utf-8", errors="replace"),
                        "body_bytes": len(body),
                        "signature_algorithm": signature_algorithm,
                        "signature_result": signature_result,
                        "signature_detail": signature_detail,
                        "tenant_scope": tenant_scope,
                        "event_id": _peek_event_id(body),
                    },
                )
        return receipt_id
    except Exception:
        logger.warning("CP2 webhook receipt capture failed", exc_info=True)
        return None


async def capture_ingestion_trace(
    receipt_id: UUID | None,
    *,
    event_id: str,
    event_type: str | None = None,
    dedupe_decision: str | None = None,
    outcome: str,
    call_id: str | None = None,
    contact_id: str | None = None,
    transcript_ref: str | None = None,
    recording_refs: list[str] | tuple[str, ...] | None = None,
    latency_ms: int | None = None,
) -> UUID | None:
    """CP3 writer.  Returns the trace id, or None when disabled/failed."""

    settings = get_settings()
    if not settings.capture_log_enabled:
        return None
    from app.database import SessionFactory  # lazy: keeps unit tests DB-free

    try:
        trace_id = uuid4()
        async with SessionFactory() as session:
            async with session.begin():
                await session.execute(
                    _TRACE_INSERT,
                    {
                        "id": str(trace_id),
                        "receipt_id": str(receipt_id) if receipt_id else None,
                        "event_id": event_id,
                        "event_type": event_type,
                        "dedupe_decision": dedupe_decision,
                        "outcome": outcome,
                        "call_id": call_id,
                        "contact_id": contact_id,
                        "transcript_ref": transcript_ref,
                        "recording_refs": (
                            json.dumps(list(recording_refs))
                            if recording_refs
                            else None
                        ),
                        "build_sha": settings.build_sha or None,
                        "latency_ms": latency_ms,
                    },
                )
        return trace_id
    except Exception:
        logger.warning("CP3 ingestion trace capture failed", exc_info=True)
        return None
