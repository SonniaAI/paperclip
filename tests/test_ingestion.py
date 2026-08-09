from __future__ import annotations

from datetime import UTC

import pytest

from app.ingestion import (
    TelnyxPayloadError,
    event_id_from_payload,
    normalize_telnyx_call,
)


def _event(**body: object) -> dict[str, object]:
    return {
        "id": "evt-call-1",
        "data": {
            "id": "evt-call-1",
            "event_type": "call.hangup",
            "payload": body,
        },
    }


def test_telnyx_call_normalization_is_provider_shape_tolerant() -> None:
    call = normalize_telnyx_call(
        _event(
            call_control_id="v3:call-control",
            call_session_id="session-1",
            direction="incoming",
            from_phone_number="+6512345678",
            to_phone_number="+6587654321",
            start_time="2026-08-09T19:00:00Z",
            end_time="2026-08-09T19:05:00Z",
            recording_urls=[
                {"url": "https://telnyx.example/recording", "content_type": "audio/wav"}
            ],
        )
    )

    assert call is not None
    assert call.external_call_key == "v3:call-control"
    assert call.direction == "inbound"
    assert call.from_phone_e164 == "+6512345678"
    assert call.started_at is not None and call.started_at.tzinfo == UTC
    assert call.recordings[0].url.startswith("https://")


def test_non_call_events_are_stored_but_not_projected_as_calls() -> None:
    payload = {"id": "evt-message", "data": {"event_type": "message.sent"}}
    assert normalize_telnyx_call(payload) is None


def test_events_without_ids_cannot_enter_the_dedupe_inbox() -> None:
    with pytest.raises(TelnyxPayloadError):
        event_id_from_payload({"data": {"event_type": "call.hangup"}})
