"""SON-1374 per-IP rate-limit tests for the unauthenticated auth-probe routes.

``POST /auth/resend-verification`` and ``POST /auth/verification/pending``
answer unauthenticated clients, so both are throttled per client IP by the
shared in-process limiter (``_check_auth_probe_limit``), mirroring the
login-failure lockout response contract (429 + ``rate_limited`` +
``Retry-After``). These tests pin: threshold trips, window/block expiry,
per-IP and per-route bucket isolation, parallel-probe admission, the 4xx
envelope, and that normal below-threshold UX flows are untouched.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.database import get_session
from app.main import _reset_auth_probe_limits, app
from app.models import AppUser

RESEND = "/auth/resend-verification"
PENDING = "/auth/verification/pending"
RESEND_ENVELOPE = {
    "message": "If the account still needs verification, a new link has been sent.",
    "delivery": "development",
    "development_url": None,
}


class _FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def mappings(self) -> _FakeResult:
        return self

    def one_or_none(self) -> Any:
        return self._row


class _FakeBegin:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Minimal AsyncSession stand-in: unknown address resolves to no row."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def begin(self) -> _FakeBegin:
        return _FakeBegin(self)

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        self.statements.append(str(statement))
        return _FakeResult(None)

    async def get(self, model: type, pk: Any) -> AppUser | None:
        return None

    async def flush(self) -> None:  # pragma: no cover - must never be called
        raise AssertionError("probe limiter must not flush the request session")


def _install(session: _FakeSession) -> None:
    async def _override() -> Any:
        yield session

    app.dependency_overrides[get_session] = _override


@pytest.fixture(autouse=True)
def _probe_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_probe_max_hits", 3)
    monkeypatch.setattr(settings, "auth_probe_window_seconds", 900)
    _reset_auth_probe_limits()
    yield
    _reset_auth_probe_limits()
    app.dependency_overrides.pop(get_session, None)


def _client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def _post(client: TestClient, path: str, ip: str = "203.0.113.9") -> Any:
    return client.post(path, json={"email": "ghost@example.com"}, headers={"X-Forwarded-For": ip})


def test_threshold_trips_with_lockout_contract() -> None:
    _install(_FakeSession())
    with _client() as client:
        for _ in range(3):
            response = _post(client, PENDING)
            assert response.status_code == 200

        throttled = _post(client, PENDING)
        assert throttled.status_code == 429
        assert throttled.json() == {
            "detail": {
                "code": "rate_limited",
                "message": "Too many verification requests. Try again later.",
            }
        }
        retry_after = int(throttled.headers["Retry-After"])
        assert 1 <= retry_after <= 900


def test_requests_during_block_stay_throttled() -> None:
    _install(_FakeSession())
    with _client() as client:
        for _ in range(4):
            _post(client, PENDING)
        for _ in range(3):
            response = _post(client, PENDING)
            assert response.status_code == 429
            assert 1 <= int(response.headers["Retry-After"]) <= 900


def test_window_expiry_resets_the_counter_without_a_block() -> None:
    settings = get_settings()
    monkeypatch_window = pytest.MonkeyPatch()
    monkeypatch_window.setattr(settings, "auth_probe_window_seconds", 1)
    _reset_auth_probe_limits()
    try:
        _install(_FakeSession())
        with _client() as client:
            assert _post(client, PENDING).status_code == 200
            assert _post(client, PENDING).status_code == 200
            assert _post(client, PENDING).status_code == 200
            assert _post(client, PENDING).status_code == 429  # window of 3 used up
            time.sleep(1.1)
            time.sleep(1.1)
            assert _post(client, PENDING).status_code == 200  # fresh window
    finally:
        monkeypatch_window.undo()
        _reset_auth_probe_limits()


def test_block_expiry_reopens_the_route() -> None:
    settings = get_settings()
    monkeypatch_window = pytest.MonkeyPatch()
    monkeypatch_window.setattr(settings, "auth_probe_max_hits", 1)
    monkeypatch_window.setattr(settings, "auth_probe_window_seconds", 1)
    _reset_auth_probe_limits()
    try:
        _install(_FakeSession())
        with _client() as client:
            assert _post(client, PENDING).status_code == 200
            assert _post(client, PENDING).status_code == 429  # trips the block
            time.sleep(1.1)
            time.sleep(1.1)
            assert _post(client, PENDING).status_code == 200  # block expired
    finally:
        monkeypatch_window.undo()
        _reset_auth_probe_limits()


def test_buckets_are_isolated_per_client_ip() -> None:
    _install(_FakeSession())
    with _client() as client:
        assert _post(client, PENDING, ip="198.51.100.1").status_code == 200
        assert _post(client, PENDING, ip="198.51.100.1").status_code == 200
        assert _post(client, PENDING, ip="198.51.100.1").status_code == 200
        assert _post(client, PENDING, ip="198.51.100.1").status_code == 429
        # A different IP still has its full budget.
        assert _post(client, PENDING, ip="198.51.100.2").status_code == 200


def test_buckets_are_isolated_per_route() -> None:
    _install(_FakeSession())
    with _client() as client:
        for _ in range(3):
            assert _post(client, PENDING).status_code == 200
            assert _post(client, RESEND).status_code == 200
        # Each route burned only its own budget.
        assert _post(client, PENDING).status_code == 429
        assert _post(client, RESEND).status_code == 429


def test_parallel_probes_cannot_exceed_the_threshold() -> None:
    _install(_FakeSession())

    async def _fire() -> list[int]:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        PENDING,
                        json={"email": "ghost@example.com"},
                        headers={"X-Forwarded-For": "203.0.113.77"},
                    )
                    for _ in range(10)
                ]
            )
            return [r.status_code for r in responses]

    codes = asyncio.run(_fire())
    assert codes.count(200) == 3  # exactly the configured budget is admitted
    assert codes.count(429) == 7


def test_normal_resend_flow_is_unaffected_below_threshold() -> None:
    _install(_FakeSession())
    with _client() as client:
        for _ in range(3):
            response = _post(client, RESEND)
            assert response.status_code == 200
            assert response.json() == RESEND_ENVELOPE


def test_invalid_payloads_do_not_consume_the_budget() -> None:
    _install(_FakeSession())
    with _client() as client:
        for _ in range(5):
            response = client.post(
                PENDING, json={"email": "not-an-email"}, headers={"X-Forwarded-For": "203.0.113.9"}
            )
            assert response.status_code == 422
        # Budget untouched: a full valid window still succeeds afterwards.
        for _ in range(3):
            assert _post(client, PENDING).status_code == 200
        assert _post(client, PENDING).status_code == 429


def test_throttle_envelope_matches_login_lockout_contract() -> None:
    _install(_FakeSession())
    with _client() as client:
        for _ in range(4):
            _post(client, RESEND)
        response = _post(client, RESEND)
    assert response.status_code == 429
    body = response.json()["detail"]
    # Same code string the login-failure lockout raises.
    assert body["code"] == "rate_limited"
    assert isinstance(body["message"], str) and body["message"]
    assert int(response.headers["Retry-After"]) >= 1


def test_pending_envelope_unchanged_at_bucket_boundary() -> None:
    """Sanity: the limiter never alters the pending response envelope."""

    _install(_FakeSession())
    with _client() as client:
        response = _post(client, PENDING)
    assert response.status_code == 200
    assert response.json() == {"pending": False, "resend_available_in_seconds": 0}
