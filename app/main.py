from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import TenantScope, get_session, set_tenant_scope
from app.ingestion import TelnyxPayloadError, ingest_telnyx_event
from app.models import (
    AppUser,
    Call,
    Department,
    DepartmentShape,
    Invite,
    Membership,
    MembershipDepartment,
    Organization,
    Role,
    UserSession,
)
from app.schemas import (
    AuthResponse,
    CallResponse,
    InviteAcceptRequest,
    InviteCreateRequest,
    InviteResponse,
    LoginRequest,
    RegisterRequest,
    TotpEnrollmentResponse,
    TotpVerifyRequest,
)
from app.security import (
    decode_invite_token,
    decode_session_cookie,
    hash_password,
    invite_expiry,
    issue_invite_token,
    issue_session_cookie,
    new_totp_secret,
    session_expiry,
    token_digest,
    verify_password,
    verify_totp,
)
from app.storage import configured_private_object_storage

settings = get_settings()
app = FastAPI(title="manager.sonnia.ai", version="0.1.0")
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


@dataclass
class AuthenticatedContext:
    session: AsyncSession
    user: AppUser
    scope: TenantScope


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")


def _response_with_session(
    body: AuthResponse,
    session: UserSession,
    scope: TenantScope,
    *,
    status_code: int = status.HTTP_200_OK,
) -> JSONResponse:
    cookie = issue_session_cookie(session_id=session.id, user_id=session.user_id, scope=scope)
    response = JSONResponse(content=jsonable_encoder(body), status_code=status_code)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=cookie,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    return response


async def _issue_session(db: AsyncSession, *, user: AppUser, scope: TenantScope) -> UserSession:
    session = UserSession(
        id=uuid4(),
        org_id=scope.org_id,
        department_id=scope.department_id,
        user_id=user.id,
        expires_at=session_expiry(),
    )
    db.add(session)
    await db.flush()
    return session


async def authenticated_context(
    request: Request,
    db: DatabaseSession,
) -> AsyncIterator[AuthenticatedContext]:
    raw_cookie = request.cookies.get(settings.session_cookie_name)
    if not raw_cookie:
        raise _unauthorized()
    try:
        claims = decode_session_cookie(raw_cookie)
    except ValueError as exc:
        raise _unauthorized() from exc

    async with db.begin():
        await set_tenant_scope(db, claims.scope)
        persisted = await db.get(UserSession, claims.session_id)
        if (
            persisted is None
            or persisted.user_id != claims.user_id
            or persisted.org_id != claims.scope.org_id
            or persisted.department_id != claims.scope.department_id
            or persisted.revoked_at is not None
            or persisted.expires_at <= datetime.now(UTC)
        ):
            raise _unauthorized()
        user = await db.get(AppUser, claims.user_id)
        if user is None or not user.is_active:
            raise _unauthorized()
        yield AuthenticatedContext(session=db, user=user, scope=claims.scope)


CurrentContext = Annotated[AuthenticatedContext, Depends(authenticated_context)]


async def _require_admin(context: AuthenticatedContext) -> Membership:
    membership = await context.session.scalar(
        select(Membership).where(Membership.user_id == context.user.id)
    )
    if membership is None or membership.role not in {Role.OWNER, Role.ADMIN}:
        raise _forbidden()
    return membership


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _webhook_scope(request: Request) -> TenantScope:
    org_value = request.headers.get("X-Manager-Org-Id") or request.headers.get("X-Org-Id")
    department_value = request.headers.get("X-Manager-Department-Id") or request.headers.get(
        "X-Department-Id"
    )
    try:
        return TenantScope(org_id=UUID(org_value or ""), department_id=UUID(department_value or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook tenant scope is required",
        ) from exc


async def _verify_telnyx_webhook(request: Request, body: bytes) -> None:
    if not settings.telnyx_webhook_secret:
        return
    supplied = request.headers.get("X-Telnyx-Signature") or request.headers.get(
        "Telnyx-Signature-Ed25519"
    )
    expected = hmac.new(
        settings.telnyx_webhook_secret.encode("utf-8"), body, "sha256"
    ).hexdigest()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )


@app.post("/webhooks/telnyx", status_code=status.HTTP_202_ACCEPTED)
async def telnyx_webhook(request: Request, db: DatabaseSession) -> JSONResponse:
    """Accept one Telnyx event, durably inbox it, then apply it once.

    Scope headers are supplied by the integration endpoint configuration, not
    by the provider payload.  The raw body is stored before normalization.
    """

    scope = _webhook_scope(request)
    body = await request.body()
    await _verify_telnyx_webhook(request, body)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc
    try:
        result = await ingest_telnyx_event(
            db,
            scope,
            payload,
            storage=configured_private_object_storage(settings),
        )
    except TelnyxPayloadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return JSONResponse(
        status_code=status.HTTP_200_OK if result.duplicate else status.HTTP_202_ACCEPTED,
        content={"accepted": True, "duplicate": result.duplicate, "status": result.status},
    )


@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: DatabaseSession) -> Response:
    """Create an organization, its silent department, and its first Owner atomically."""

    org_id = uuid4()
    department_id = uuid4()
    user_id = uuid4()
    membership_id = uuid4()
    scope = TenantScope(org_id=org_id, department_id=department_id)

    async with db.begin():
        await set_tenant_scope(db, scope)
        await db.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        organization = Organization(
            id=org_id,
            org_id=org_id,
            department_id=department_id,
            name=payload.organization_name,
            login_slug=payload.organization_slug,
        )
        department = Department(
            id=department_id,
            org_id=org_id,
            department_id=department_id,
            name="__default__",
            login_slug="default",
            shape=DepartmentShape.SILENT,
            is_silent=True,
        )
        user = AppUser(
            id=user_id,
            org_id=org_id,
            department_id=department_id,
            email=payload.email,
            display_name=payload.display_name,
            password_hash=hash_password(payload.password),
        )
        membership = Membership(
            id=membership_id,
            org_id=org_id,
            department_id=department_id,
            user_id=user_id,
            role=Role.OWNER,
        )
        membership_department = MembershipDepartment(
            id=uuid4(),
            org_id=org_id,
            department_id=department_id,
            membership_id=membership_id,
        )
        # The organization/default-department FKs are mutually deferred, but
        # user and membership FKs remain immediate. Flush each boundary so the
        # database, not SQLAlchemy insertion order, defines safe creation.
        db.add_all([organization, department])
        await db.flush()
        db.add(user)
        await db.flush()
        db.add(membership)
        await db.flush()
        db.add(membership_department)
        await db.flush()
        session = await _issue_session(db, user=user, scope=scope)

    return _response_with_session(
        AuthResponse(),
        session,
        scope,
        status_code=status.HTTP_201_CREATED,
    )


@app.post("/auth/login")
async def login(payload: LoginRequest, db: DatabaseSession) -> Response:
    """Authenticate into one explicitly granted department.

    A user may hold grants for more than one department, but each session is
    intentionally scoped to one grant. Owners do not bypass that boundary.
    """

    async with db.begin():
        scope_row = (
            (
                await db.execute(
                    text(
                        """
                    SELECT org_id, department_id
                    FROM app.resolve_login_scope(:organization_slug, :department_slug)
                    """
                    ),
                    {
                        "organization_slug": payload.organization_slug,
                        "department_slug": payload.department_slug,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if scope_row is None:
            raise _unauthorized()
        scope = TenantScope(
            org_id=scope_row["org_id"],
            department_id=scope_row["department_id"],
        )
        await set_tenant_scope(db, scope)
        user = await db.scalar(select(AppUser).where(AppUser.email == payload.email))
        if (
            user is None
            or not user.is_active
            or not verify_password(payload.password, user.password_hash)
        ):
            raise _unauthorized()
        membership = await db.scalar(select(Membership).where(Membership.user_id == user.id))
        if membership is None:
            raise _unauthorized()
        if user.totp_enabled and (not payload.totp_code or not user.totp_secret):
            raise _unauthorized()
        if user.totp_enabled and not verify_totp(user.totp_secret, payload.totp_code or ""):
            raise _unauthorized()
        session = await _issue_session(db, user=user, scope=scope)

    return _response_with_session(
        AuthResponse(),
        session,
        scope,
    )


@app.post("/auth/totp/enroll", response_model=TotpEnrollmentResponse)
async def enroll_totp(
    context: CurrentContext,
) -> TotpEnrollmentResponse:
    context.user.totp_secret = new_totp_secret()
    context.user.totp_enabled = False
    await context.session.flush()
    return TotpEnrollmentResponse(secret=context.user.totp_secret)


@app.post("/auth/totp/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify_enrolled_totp(
    payload: TotpVerifyRequest,
    context: CurrentContext,
) -> Response:
    if not context.user.totp_secret or not verify_totp(context.user.totp_secret, payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid TOTP code")
    context.user.totp_enabled = True
    await context.session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/auth/invites", response_model=InviteResponse, status_code=status.HTTP_201_CREATED)
async def create_invite(
    payload: InviteCreateRequest,
    context: CurrentContext,
) -> InviteResponse:
    await _require_admin(context)
    invite = Invite(
        id=uuid4(),
        org_id=context.scope.org_id,
        department_id=context.scope.department_id,
        email=payload.email,
        role=payload.role,
        token_digest="pending",
        expires_at=invite_expiry(),
    )
    context.session.add(invite)
    await context.session.flush()
    token = issue_invite_token(invite_id=invite.id, scope=context.scope, email=invite.email)
    invite.token_digest = token_digest(token)
    await context.session.flush()
    return InviteResponse(invite_token=token, expires_at=invite.expires_at)


@app.post("/auth/invites/accept")
async def accept_invite(payload: InviteAcceptRequest, db: DatabaseSession) -> Response:
    try:
        claims = decode_invite_token(payload.token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired invite"
        ) from exc

    async with db.begin():
        await set_tenant_scope(db, claims.scope)
        invite = await db.get(Invite, claims.invite_id)
        if (
            invite is None
            or invite.accepted_at is not None
            or invite.expires_at <= datetime.now(UTC)
            or invite.email != claims.email
            or not hmac.compare_digest(invite.token_digest, token_digest(payload.token))
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired invite"
            )

        existing_user = await db.scalar(select(AppUser).where(AppUser.email == invite.email))
        if existing_user is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

        user = AppUser(
            id=uuid4(),
            org_id=claims.scope.org_id,
            department_id=claims.scope.department_id,
            email=invite.email,
            display_name=payload.display_name,
            password_hash=hash_password(payload.password),
        )
        membership = Membership(
            id=uuid4(),
            org_id=claims.scope.org_id,
            department_id=claims.scope.department_id,
            user_id=user.id,
            role=invite.role,
        )
        membership_department = MembershipDepartment(
            id=uuid4(),
            org_id=claims.scope.org_id,
            department_id=claims.scope.department_id,
            membership_id=membership.id,
        )
        invite.accepted_at = datetime.now(UTC)
        db.add(user)
        await db.flush()
        db.add(membership)
        await db.flush()
        db.add(membership_department)
        await db.flush()
        session = await _issue_session(db, user=user, scope=claims.scope)

    return _response_with_session(
        AuthResponse(),
        session,
        claims.scope,
    )


@app.get("/api/calls/{call_id}", response_model=CallResponse)
async def get_call(
    call_id: UUID,
    context: CurrentContext,
) -> CallResponse:
    """Return 404 for absent *and RLS-hidden* calls, never a cross-org clue."""

    # Deliberately no org/department condition: transaction-scoped PostgreSQL
    # RLS is the access boundary. A cross-org ID produces no row.
    call = await context.session.scalar(select(Call).where(Call.id == call_id))
    if call is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")
    return CallResponse(subject=call.subject, created_at=call.created_at)


@app.get("/api/recordings/{recording_id}/url")
async def get_recording_url(recording_id: UUID, context: CurrentContext) -> dict[str, object]:
    """Issue a short-lived URL for a private object; never return a public path."""

    recording = (
        await context.session.execute(
            text(
                """
                SELECT storage_bucket, storage_key
                FROM recordings
                WHERE id = :recording_id AND is_private = true
                """
            ),
            {"recording_id": str(recording_id)},
        )
    ).mappings().one_or_none()
    if recording is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")

    storage = configured_private_object_storage(settings)
    if storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recording storage unavailable",
        )
    signed_url = await storage.create_signed_url(
        recording["storage_key"], settings.recording_signed_url_ttl_seconds
    )
    return {
        "url": signed_url,
        "expires_in_seconds": settings.recording_signed_url_ttl_seconds,
    }
