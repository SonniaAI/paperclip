"""SON-1789 — team invite send path: wiring + real-socket delivery.

Covers the send wiring added to POST /auth/invites: the §8 team invite
email is rendered through the shared template and delivered with the
production SMTP path, the accept URL uses the /invite/accept path with
the token as the only user-specific data (spec §7), and the response
reports the delivery mode.
"""

from __future__ import annotations

import asyncio
import uuid
from email import message_from_string
from email import policy as email_policy
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from pydantic import ValidationError

from app import main as main_module
from app.config import Settings
from app.database import TenantScope
from app.email_delivery import EmailDeliveryResult, deliver_rendered_email
from app.email_templates import TEAM_INVITE_SUBJECT, render_team_invite_email
from app.main import _first_name, _public_action_url, create_invite
from app.models import Invite, Membership, Role
from app.schemas import InviteAcceptRequest, InviteCreateRequest


class _CaptureServer:
    """Minimal in-process SMTP sink — just enough protocol for smtplib."""

    def __init__(self) -> None:
        self.messages: list[object] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        assert self._server.sockets is not None
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        def reply(line: str) -> None:
            writer.write(line.encode())

        reply("220 son1789 capture\r\n")
        await writer.drain()
        lines: list[str] = []
        in_data = False
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace")
            if in_data:
                if line.rstrip("\r\n") == ".":
                    in_data = False
                    self.messages.append(
                        message_from_string("".join(lines), policy=email_policy.default)
                    )
                    lines = []
                    reply("250 OK\r\n")
                else:
                    if line.startswith(".."):
                        line = line[1:]
                    lines.append(line)
                await writer.drain()
                continue
            verb = line.strip().upper()
            if verb == "DATA":
                in_data = True
                reply("354 go\r\n")
            elif verb == "QUIT":
                reply("221 bye\r\n")
                await writer.drain()
                break
            else:
                reply("250 OK\r\n")
                await writer.drain()
        writer.close()

    def close(self) -> None:
        assert self._server is not None
        self._server.close()

    async def wait_closed(self) -> None:
        assert self._server is not None
        await self._server.wait_closed()


@pytest.fixture
async def _smtp_server():
    server = _CaptureServer()
    await server.start()
    yield server
    server.close()
    await server.wait_closed()


class _FakeSession:
    def __init__(self, scalar_result: str) -> None:
        self._scalar_result = scalar_result
        self.added: list[object] = []
        self.flushes = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    async def scalar(self, _statement: object) -> str:
        return self._scalar_result


def _invite_context(session: _FakeSession) -> SimpleNamespace:
    return SimpleNamespace(
        session=session,
        user=SimpleNamespace(
            id=uuid.uuid4(),
            display_name="Alex Chen",
            email="alex@sunpeak.solar",
        ),
        scope=TenantScope(org_id=uuid.uuid4(), department_id=uuid.uuid4()),
    )


def test_team_invite_subject_constant_matches_family_copy() -> None:
    assert TEAM_INVITE_SUBJECT.format(inviter="Alex", company="SunPeak Solar") == (
        "Alex invited you to SunPeak Solar on Sonnia"
    )


def test_accept_action_url_keeps_token_only_in_path() -> None:
    url = _public_action_url("/invite/accept", "secret token/with#junk")
    assert url.endswith(f"/invite/accept/{quote('secret token/with#junk', safe='')}")
    assert " " not in url and "#" not in url and "/" not in url.rsplit("/", 1)[-1]


async def test_invite_email_delivered_over_real_smtp(_smtp_server: _CaptureServer) -> None:
    settings = Settings(
        smtp_host="127.0.0.1",
        smtp_port=_smtp_server.port,
        smtp_use_tls=False,
        smtp_from_email="notifications@sonnia.ai",
    )
    action_url = _public_action_url("/invite/accept", "tok-123")
    rendered = render_team_invite_email(inviter="Alex", company="SunPeak Solar", url=action_url)
    result = await asyncio.to_thread(
        deliver_rendered_email,
        settings=settings,
        recipient="priya@sunpeak.solar",
        subject=TEAM_INVITE_SUBJECT.format(inviter="Alex", company="SunPeak Solar"),
        rendered=rendered,
    )

    assert result.mode == "smtp"
    assert len(_smtp_server.messages) == 1
    message = _smtp_server.messages[0]
    assert message["Subject"] == "Alex invited you to SunPeak Solar on Sonnia"
    assert message["To"] == "priya@sunpeak.solar"
    assert message["From"] == "notifications@sonnia.ai"
    assert message.get_content_type() == "multipart/alternative"
    html_parts = [
        part.get_content()
        for part in message.walk()
        if part.get_content_type() == "text/html"
    ]
    assert html_parts and action_url in html_parts[0]


async def test_create_invite_renders_team_invite_and_returns_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_deliver(**kwargs: str) -> EmailDeliveryResult:
        captured.update(kwargs)
        return EmailDeliveryResult(mode="smtp")

    async def fake_admin(_context: object) -> Membership:
        return Membership()

    monkeypatch.setattr(main_module, "_require_admin", fake_admin)
    monkeypatch.setattr(main_module, "_deliver_team_invite_email", fake_deliver)

    session = _FakeSession(scalar_result="SunPeak Solar")
    response = await create_invite(
        InviteCreateRequest(email="priya@sunpeak.solar", role=Role.MEMBER),
        _invite_context(session),  # type: ignore[arg-type]
    )

    token = response.invite_token
    assert response.delivery == "smtp"
    assert response.message == "Invitation sent to priya@sunpeak.solar."
    assert response.development_url is None
    assert captured["recipient"] == "priya@sunpeak.solar"
    assert captured["inviter"] == _first_name("Alex Chen")
    assert captured["company"] == "SunPeak Solar"
    assert captured["action_url"] == (
        f"{main_module.settings.public_app_url}/invite/accept/{quote(token, safe='')}"
    )
    invite_row = session.added[0]
    assert isinstance(invite_row, Invite)
    assert invite_row.email == "priya@sunpeak.solar"
    assert invite_row.token_digest == main_module.token_digest(token)


async def test_create_invite_without_smtp_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_admin(_context: object) -> Membership:
        return Membership()

    monkeypatch.setattr(main_module, "_require_admin", fake_admin)
    monkeypatch.setattr(main_module.settings, "smtp_host", None)

    response = await create_invite(
        InviteCreateRequest(email="priya@sunpeak.solar"),
        _invite_context(_FakeSession(scalar_result="SunPeak Solar")),  # type: ignore[arg-type]
    )
    assert response.delivery == "development"
    assert "/dev/emails" in response.message


def test_accept_request_enforces_password_floor() -> None:
    with pytest.raises(ValidationError):
        InviteAcceptRequest(token="x" * 24, display_name="Priya Patel", password="short")
    payload = InviteAcceptRequest(
        token="x" * 24, display_name="Priya Patel", password="correct-horse-battery"
    )
    assert payload.display_name == "Priya Patel"
