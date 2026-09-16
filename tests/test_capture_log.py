from __future__ import annotations

import asyncio
import hmac
import json

from app.capture_log import (
    _peek_event_id,
    evaluate_telnyx_signature,
    extract_event_context,
    redact_headers,
)


def _expected_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, "sha256").hexdigest()


def test_signature_pass_matches_hmac_sha256() -> None:
    body = json.dumps({"id": "evt-1"}).encode()
    secret = "whsec-test"
    algorithm, result, detail = evaluate_telnyx_signature(
        secret, _expected_signature(secret, body), body
    )
    assert algorithm == "hmac-sha256"
    assert result == "pass"
    assert detail is None


def test_signature_fails_closed_on_missing_or_wrong_signature() -> None:
    body = b"{}"
    algorithm, result, detail = evaluate_telnyx_signature("whsec-test", None, body)
    assert (algorithm, result) == ("hmac-sha256", "fail")
    assert detail
    _, result, _ = evaluate_telnyx_signature("whsec-test", "deadbeef", body)
    assert result == "fail"


def test_signature_without_secret_is_skipped() -> None:
    algorithm, result, detail = evaluate_telnyx_signature(None, None, b"{}")
    assert (algorithm, result) == ("hmac-sha256", "skipped")
    assert detail


def test_redact_headers_hides_credentials_keeps_signature() -> None:
    headers = {
        "Authorization": "Bearer sekrit",
        "X-Api-Key": "k",
        "Content-Type": "application/json",
        "X-Telnyx-Signature": "abc123",
        "X-Manager-Org-Id": "org",
    }
    redacted = redact_headers(headers)
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["X-Api-Key"] == "[REDACTED]"
    assert redacted["Content-Type"] == "application/json"
    assert redacted["X-Telnyx-Signature"] == "abc123"
    assert redacted["X-Manager-Org-Id"] == "org"
    assert headers["Authorization"] == "Bearer sekrit"


def _event(payload_body: dict) -> dict:
    return {
        "id": "evt-call-1",
        "data": {
            "id": "evt-call-1",
            "event_type": "call.hangup",
            "payload": payload_body,
        },
    }


def test_extract_event_context_maps_call_entities() -> None:
    context = extract_event_context(
        _event(
            {
                "call_control_id": "v3:call-control",
                "recording_urls": [
                    {"url": "https://telnyx.example/rec.wav", "content_type": "audio/wav"},
                    "https://telnyx.example/rec2.wav",
                ],
                "transcript_url": "https://telnyx.example/transcript",
            }
        )
    )
    assert context.event_id == "evt-call-1"
    assert context.event_type == "call.hangup"
    assert context.recording_refs == (
        "https://telnyx.example/rec.wav",
        "https://telnyx.example/rec2.wav",
    )
    assert context.transcript_ref == "https://telnyx.example/transcript"


def test_extract_event_context_tolerates_unknown_shapes() -> None:
    context = extract_event_context({"data": {"event_type": "message.sent"}})
    assert context.event_id == "unknown"
    assert context.event_type == "message.sent"
    assert context.recording_refs == ()
    assert context.transcript_ref is None


def test_peek_event_id_is_lenient() -> None:
    assert _peek_event_id(b"not-json") is None
    assert _peek_event_id(json.dumps(_event({})).encode()) == "evt-call-1"
    assert _peek_event_id(b"[1, 2]") is None


def test_capture_writers_respect_the_kill_switch(monkeypatch) -> None:
    from app import capture_log

    class _DisabledSettings:
        capture_log_enabled = False
        build_sha = ""

    monkeypatch.setattr(capture_log, "get_settings", lambda: _DisabledSettings())
    assert (
        asyncio.run(
            capture_log.capture_webhook_receipt(
                {}, b"{}", signature_algorithm="hmac-sha256", signature_result="pass"
            )
        )
        is None
    )
    assert (
        asyncio.run(
            capture_log.capture_ingestion_trace(None, event_id="e", outcome="ingested")
        )
        is None
    )
