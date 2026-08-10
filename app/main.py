from __future__ import annotations

import hmac
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.contact_memory import (
    ContactMemoryNotFoundError,
    ContactMemoryRecallHit,
    recall_contact_memory,
)
from app.database import TenantScope, get_session, set_tenant_scope
from app.hindsight import configured_hindsight_client
from app.ingestion import TelnyxPayloadError, ingest_telnyx_event
from app.materials import (
    MAX_MATERIAL_BYTES,
    MaterialUploadError,
    UnsupportedMaterialError,
    extract_material,
    parse_upload_body,
)
from app.models import (
    ActivityLogEntry,
    AppUser,
    Call,
    Contact,
    ContactEmail,
    ContactImport,
    ContactPhone,
    Department,
    DepartmentShape,
    Instruction,
    Integration,
    Invite,
    Membership,
    MembershipDepartment,
    Organization,
    Role,
    SecurityEvent,
    Task,
    TaskList,
    UserSession,
)
from app.schemas import (
    ActivityFeedEntry,
    ActivityFeedResponse,
    AuthResponse,
    BillingSettingsResponse,
    BillingTopUpPlaceholderResponse,
    CallResponse,
    ChangePasswordRequest,
    CompanySettingsResponse,
    CompanyUpdateRequest,
    ContactImportCommitRequest,
    ContactImportPreviewRequest,
    ContactImportPreviewResponse,
    ContactImportPreviewRow,
    ContactImportSummaryResponse,
    InstructionCreateRequest,
    InstructionListResponse,
    InstructionResponse,
    InstructionUpdateRequest,
    IntegrationResponse,
    InviteAcceptRequest,
    InviteCreateRequest,
    InviteResponse,
    LoginRequest,
    OnboardingProgressRequest,
    OnboardingStateResponse,
    ProfileSettingsResponse,
    ProfileUpdateRequest,
    RegisterRequest,
    SecurityEventResponse,
    TaskCreateRequest,
    TaskListCreateRequest,
    TaskListResponse,
    TaskListViewResponse,
    TaskResponse,
    TaskUpdateRequest,
    TeamMemberResponse,
    TotpEnrollmentResponse,
    TotpVerifyRequest,
    UserSessionResponse,
    VoiceMemoryRecallHitResponse,
    VoiceMemoryRecallRequest,
    VoiceMemoryRecallResponse,
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
from app.son419 import (
    dedupe_keys_for_row,
    display_name_for_row,
    next_instruction_version,
    preview_import,
)
from app.storage import configured_private_object_storage
from app.transcript_memory import dispatch_transcript_memory

settings = get_settings()
app = FastAPI(title="manager.sonnia.ai", version="0.1.0")
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


@dataclass
class AuthenticatedContext:
    session: AsyncSession
    user: AppUser
    scope: TenantScope
    session_id: UUID | None = None


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
        yield AuthenticatedContext(
            session=db,
            user=user,
            scope=claims.scope,
            session_id=claims.session_id,
        )


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
    if result.status in {"processed", "duplicate"}:
        await dispatch_transcript_memory(
            db,
            scope=scope,
            event_id=result.event_id,
            payload=payload,
            call_id=result.call_id,
            hindsight=configured_hindsight_client(
                base_url=settings.hindsight_base_url,
                api_key=settings.hindsight_api_key,
                timeout_seconds=settings.hindsight_timeout_seconds,
            ),
        )
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


@app.post(
    "/api/voice/contacts/{contact_id}/memory-recall",
    response_model=VoiceMemoryRecallResponse,
)
async def voice_contact_memory_recall(
    contact_id: UUID,
    payload: VoiceMemoryRecallRequest,
    context: CurrentContext,
) -> VoiceMemoryRecallResponse:
    """Ground a voice turn in deterministic memory before any fuzzy fallback."""

    try:
        recall = await recall_contact_memory(
            context.session,
            scope=context.scope,
            contact_id=contact_id,
            query=payload.query,
            limit=payload.limit,
            hindsight=configured_hindsight_client(
                base_url=settings.hindsight_base_url,
                api_key=settings.hindsight_api_key,
                timeout_seconds=settings.hindsight_timeout_seconds,
            ),
        )
    except ContactMemoryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contact not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return VoiceMemoryRecallResponse(
        deterministic=[_voice_memory_hit(hit) for hit in recall.deterministic],
        fuzzy=[_voice_memory_hit(hit) for hit in recall.fuzzy],
        used_hindsight=recall.used_hindsight,
        hindsight_status=recall.hindsight_status,
    )


def _voice_memory_hit(hit: ContactMemoryRecallHit) -> VoiceMemoryRecallHitResponse:
    """Make the public voice response explicit instead of leaking provider objects."""

    return VoiceMemoryRecallHitResponse(
        text=hit.text,
        source=hit.source,
        label=hit.label,
        kind=hit.kind,
        entry_id=hit.entry_id,
        memory_id=hit.memory_id,
        document_id=hit.document_id,
        confidence=hit.confidence,
    )


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


@app.post("/api/materials/extract")
async def extract_uploaded_material(
    request: Request,
    context: CurrentContext,
) -> dict[str, object]:
    """Extract one pitch material into a source-linked cold-call brief.

    The Phase 1 route is intentionally synchronous and deterministic.  It is
    an offline fallback: the upload is read in memory, parsed locally, and
    returned with an honest processing state.  No material claim is generated
    without a document locator.  Persistence can be added once campaign UI
    chooses a campaign; the single-campaign demo does not need that coupling.
    """

    del context  # Authentication and RLS scope are enforced by the dependency.
    body = await request.body()
    if len(body) > MAX_MATERIAL_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="material is larger than the 25 MB Phase 1 limit",
        )
    try:
        upload = parse_upload_body(
            body,
            request.headers.get("content-type"),
            request.headers.get("x-material-filename") or request.headers.get("x-filename"),
        )
        return extract_material(upload)
    except UnsupportedMaterialError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except MaterialUploadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.get("/api/billing/top-up", response_model=BillingTopUpPlaceholderResponse)
async def billing_top_up_placeholder(context: CurrentContext) -> BillingTopUpPlaceholderResponse:
    """Expose Phase 1's contact-us affordance without making a payment attempt."""

    del context
    return BillingTopUpPlaceholderResponse()



# ---------------------------------------------------------------------------
# SON-419: /instructions, /tasks, /activity, settings, CSV import, onboarding
# ---------------------------------------------------------------------------


def _activity_row(
    *,
    scope: TenantScope,
    actor_user_id: UUID | None,
    action: str,
    entity_type: str,
    entity_id: UUID | None = None,
    private: bool = False,
    activity_metadata: dict[str, object] | None = None,
) -> ActivityLogEntry:
    return ActivityLogEntry(
        id=uuid4(),
        org_id=scope.org_id,
        department_id=scope.department_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        private=private,
        activity_metadata=activity_metadata or {},
    )


def _security_row(
    *,
    scope: TenantScope,
    actor_user_id: UUID | None,
    event_type: str,
    ip_address: str | None,
    user_agent: str | None,
    event_metadata: dict[str, object] | None = None,
) -> SecurityEvent:
    return SecurityEvent(
        id=uuid4(),
        org_id=scope.org_id,
        department_id=scope.department_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        ip_address=ip_address,
        user_agent=user_agent,
        event_metadata=event_metadata or {},
    )


def _request_origin(request: Request) -> tuple[str | None, str | None]:
    forwarded = request.headers.get("x-forwarded-for")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else None)
    )
    return ip, request.headers.get("user-agent")


def _instruction_response(row: Instruction) -> InstructionResponse:
    return InstructionResponse(
        id=row.id,
        title=row.title,
        body=row.body,
        slug=row.slug,
        version=row.version,
        applies_to=row.applies_to,
        active=bool(row.active),
        effective_from=row.effective_from,
        superseded_at=row.superseded_at,
        created_at=row.created_at,
    )


def _task_response(row: Task) -> TaskResponse:
    return TaskResponse(
        id=row.id,
        title=row.title,
        description=row.description,
        status=row.status,
        visibility=row.visibility,
        due_at=row.due_at,
        task_list_id=row.task_list_id,
        contact_id=row.contact_id,
        call_id=row.call_id,
        owner_user_id=row.owner_user_id,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
    )


# -- /instructions ---------------------------------------------------------


@app.get("/api/instructions", response_model=InstructionListResponse)
async def list_active_instructions(
    context: CurrentContext,
) -> InstructionListResponse:
    """List the active (in-effect) instruction version for every topic."""

    rows = (
        (
            await context.session.execute(
                select(Instruction)
                .where(Instruction.superseded_at.is_(None), Instruction.active.is_(True))
                .order_by(Instruction.effective_from.desc())
            )
        )
        .scalars()
        .all()
    )
    return InstructionListResponse(items=[_instruction_response(r) for r in rows])


@app.get("/api/instructions/{slug}/history", response_model=InstructionListResponse)
async def instruction_history(
    slug: str,
    context: CurrentContext,
) -> InstructionListResponse:
    """Return the full version history for one instruction topic, newest first."""

    rows = (
        (
            await context.session.execute(
                select(Instruction)
                .where(Instruction.slug == slug)
                .order_by(Instruction.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return InstructionListResponse(items=[_instruction_response(r) for r in rows])


@app.post(
    "/api/instructions",
    response_model=InstructionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_instruction(
    payload: InstructionCreateRequest,
    context: CurrentContext,
) -> InstructionResponse:
    """Create the first version of an instruction topic (in effect from now)."""

    edit = next_instruction_version(
        slug=payload.slug,
        title=payload.title,
        body=payload.body,
        applies_to=payload.applies_to,
        current_version=0,
        effective_from=payload.effective_from,
    )
    row = Instruction(
        id=uuid4(),
        org_id=context.scope.org_id,
        department_id=context.scope.department_id,
        title=edit.title,
        body=edit.body,
        applies_to=edit.applies_to,
        slug=edit.slug,
        version=edit.version,
        effective_from=edit.effective_from,
        active=True,
        created_by_user_id=context.user.id,
    )
    context.session.add(row)
    context.session.add(
        _activity_row(
            scope=context.scope,
            actor_user_id=context.user.id,
            action="instruction_created",
            entity_type="instruction",
            entity_id=row.id,
            activity_metadata={"slug": edit.slug, "version": edit.version},
        )
    )
    await context.session.flush()
    return _instruction_response(row)


@app.patch("/api/instructions/{slug}", response_model=InstructionResponse)
async def update_instruction(
    slug: str,
    payload: InstructionUpdateRequest,
    context: CurrentContext,
) -> InstructionResponse:
    """Edit an instruction: supersede the active version and publish the next.

    The previous wording stays in history (immutable); the new version's
    ``effective_from`` (default: now) states when the change takes effect.
    """

    active = await context.session.scalar(
        select(Instruction).where(
            Instruction.slug == slug,
            Instruction.superseded_at.is_(None),
            Instruction.active.is_(True),
        )
    )
    if active is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Instruction not found"
        )
    edit = next_instruction_version(
        slug=active.slug,
        title=payload.title or active.title,
        body=payload.body,
        applies_to=active.applies_to,
        current_version=active.version,
        effective_from=payload.effective_from,
    )
    now = datetime.now(UTC)
    active.superseded_at = now
    active.active = False
    row = Instruction(
        id=uuid4(),
        org_id=context.scope.org_id,
        department_id=context.scope.department_id,
        title=edit.title,
        body=edit.body,
        applies_to=edit.applies_to,
        slug=edit.slug,
        version=edit.version,
        effective_from=edit.effective_from,
        active=True,
        created_by_user_id=context.user.id,
    )
    context.session.add(row)
    context.session.add(
        _activity_row(
            scope=context.scope,
            actor_user_id=context.user.id,
            action="instruction_updated",
            entity_type="instruction",
            entity_id=row.id,
            activity_metadata={"slug": edit.slug, "version": edit.version},
        )
    )
    await context.session.flush()
    return _instruction_response(row)


# -- /tasks ----------------------------------------------------------------


@app.get("/api/task-lists", response_model=list[TaskListViewResponse])
async def list_task_lists(
    context: CurrentContext,
) -> list[TaskListViewResponse]:
    """My task lists (private, owned by me) plus shared team lists."""

    rows = (
        (
            await context.session.execute(
                select(TaskList)
                .where(
                    or_(
                        TaskList.visibility == "shared",
                        TaskList.owner_user_id == context.user.id,
                    )
                )
                .order_by(TaskList.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    result: list[TaskListViewResponse] = []
    for row in rows:
        count = (
            await context.session.scalar(
                select(Task).where(
                    Task.task_list_id == row.id,
                    or_(
                        Task.visibility == "shared",
                        Task.owner_user_id == context.user.id,
                    ),
                )
            )
            or 0
        )
        result.append(
            TaskListViewResponse(
                id=row.id,
                name=row.name,
                description=row.description,
                visibility=row.visibility,
                owner_user_id=row.owner_user_id,
                task_count=count,
                created_at=row.created_at,
            )
        )
    return result


@app.post(
    "/api/task-lists",
    response_model=TaskListResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_task_list(
    payload: TaskListCreateRequest,
    context: CurrentContext,
) -> TaskListResponse:
    """Create a private (default) or shared task list."""

    row = TaskList(
        id=uuid4(),
        org_id=context.scope.org_id,
        department_id=context.scope.department_id,
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
        owner_user_id=context.user.id if payload.visibility == "private" else None,
    )
    context.session.add(row)
    await context.session.flush()
    return TaskListResponse(
        id=row.id,
        name=row.name,
        description=row.description,
        visibility=row.visibility,
        owner_user_id=row.owner_user_id,
        created_at=row.created_at,
    )


@app.get("/api/tasks", response_model=list[TaskResponse])
async def list_tasks(
    context: CurrentContext,
    task_list_id: UUID | None = None,
    status_value: str | None = None,
) -> list[TaskResponse]:
    """My Tasks (private default) + shared team tasks.

    Privacy is enforced at the query layer: a caller sees a shared task or a
    private task it owns. The visibility toggle stays available because shared
    tasks are always listed.
    """

    clause = or_(
        Task.visibility == "shared",
        Task.owner_user_id == context.user.id,
    )
    if task_list_id is not None:
        clause = and_(clause, Task.task_list_id == task_list_id)
    if status_value is not None:
        clause = and_(clause, Task.status == status_value)
    rows = (
        (
            await context.session.execute(
                select(Task).where(clause).order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_task_response(r) for r in rows]


@app.post("/api/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreateRequest,
    context: CurrentContext,
) -> TaskResponse:
    """Create a task. Private tasks default to the current user as owner."""

    row = Task(
        id=uuid4(),
        org_id=context.scope.org_id,
        department_id=context.scope.department_id,
        title=payload.title,
        description=payload.description,
        due_at=payload.due_at,
        task_list_id=payload.task_list_id,
        contact_id=payload.contact_id,
        call_id=payload.call_id,
        visibility=payload.visibility,
        owner_user_id=context.user.id if payload.visibility == "private" else None,
        created_by_user_id=context.user.id,
    )
    context.session.add(row)
    context.session.add(
        _activity_row(
            scope=context.scope,
            actor_user_id=context.user.id,
            action="task_created",
            entity_type="task",
            entity_id=row.id,
            private=payload.visibility == "private",
            activity_metadata={"title": payload.title},
        )
    )
    await context.session.flush()
    return _task_response(row)


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: UUID,
    payload: TaskUpdateRequest,
    context: CurrentContext,
) -> TaskResponse:
    """Update a visible task (shared, or private owned by the caller)."""

    task = await context.session.scalar(
        select(Task).where(
            Task.id == task_id,
            or_(Task.visibility == "shared", Task.owner_user_id == context.user.id),
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    if payload.visibility == "private" and task.owner_user_id is None:
        task.owner_user_id = context.user.id
    context.session.add(
        _activity_row(
            scope=context.scope,
            actor_user_id=context.user.id,
            action="task_updated",
            entity_type="task",
            entity_id=task.id,
            private=task.visibility == "private",
        )
    )
    await context.session.flush()
    return _task_response(task)


# -- /activity -------------------------------------------------------------


@app.get("/api/activity", response_model=ActivityFeedResponse)
async def activity_feed(
    context: CurrentContext,
    kind: str | None = None,
    actor_user_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> ActivityFeedResponse:
    """One chronological feed, filtered by type + actor, privacy respected.

    Private entries are only visible to their own actor. Cursor is the ISO
    timestamp of the last seen entry for inclusive-exclusive paging.
    """

    limit = max(1, min(limit, 200))
    clause = or_(
        ActivityLogEntry.private.is_(False),
        ActivityLogEntry.actor_user_id == context.user.id,
    )
    if kind:
        clause = and_(clause, ActivityLogEntry.action == kind)
    if actor_user_id is not None:
        clause = and_(clause, ActivityLogEntry.actor_user_id == actor_user_id)
    if cursor:
        clause = and_(clause, ActivityLogEntry.created_at < datetime.fromisoformat(cursor))
    rows = (
        (
            await context.session.execute(
                select(ActivityLogEntry)
                .where(clause)
                .order_by(ActivityLogEntry.created_at.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    items: list[ActivityFeedEntry] = []
    for row in page:
        display_name = None
        if row.actor_user_id is not None:
            actor = await context.session.get(AppUser, row.actor_user_id)
            display_name = actor.display_name if actor else None
        items.append(
            ActivityFeedEntry(
                id=row.id,
                actor_user_id=row.actor_user_id,
                actor_display_name=display_name,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                metadata=row.activity_metadata,
                created_at=row.created_at,
            )
        )
    next_cursor = page[-1].created_at.isoformat() if (has_more and page) else None
    return ActivityFeedResponse(items=items, next_cursor=next_cursor)


# -- settings (profile/security/company/team/billing/integrations) ---------


@app.get("/api/settings/profile", response_model=ProfileSettingsResponse)
async def profile_settings(
    context: CurrentContext,
) -> ProfileSettingsResponse:
    org = await context.session.get(Organization, context.scope.org_id)
    return ProfileSettingsResponse(
        display_name=context.user.display_name,
        email=context.user.email,
        totp_enabled=context.user.totp_enabled,
        organization_name=org.name if org else "",
        onboarding_state=org.onboarding_state if org else "pending",
    )


@app.patch("/api/settings/profile", response_model=ProfileSettingsResponse)
async def update_profile(
    payload: ProfileUpdateRequest,
    context: CurrentContext,
) -> ProfileSettingsResponse:
    if payload.display_name:
        context.user.display_name = payload.display_name
        await context.session.flush()
    return await profile_settings(context)


@app.post("/api/settings/security/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    context: CurrentContext,
) -> Response:
    if not verify_password(payload.current_password, context.user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect"
        )
    context.user.password_hash = hash_password(payload.new_password)
    ip, ua = _request_origin(request)
    context.session.add(
        _security_row(
            scope=context.scope,
            actor_user_id=context.user.id,
            event_type="password_changed",
            ip_address=ip,
            user_agent=ua,
        )
    )
    await context.session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/settings/security/sessions", response_model=list[UserSessionResponse])
async def list_sessions(
    context: CurrentContext,
) -> list[UserSessionResponse]:
    rows = (
        (
            await context.session.execute(
                select(UserSession)
                .where(UserSession.user_id == context.user.id)
                .order_by(UserSession.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        UserSessionResponse(
            id=row.id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            current=row.id == context.session_id,
        )
        for row in rows
    ]


@app.post(
    "/api/settings/security/sessions/{session_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_session(
    session_id: UUID,
    request: Request,
    context: CurrentContext,
) -> Response:
    session = await context.session.scalar(
        select(UserSession).where(
            UserSession.id == session_id, UserSession.user_id == context.user.id
        )
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if session.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Session already revoked"
        )
    session.revoked_at = datetime.now(UTC)
    ip, ua = _request_origin(request)
    context.session.add(
        _security_row(
            scope=context.scope,
            actor_user_id=context.user.id,
            event_type="session_revoked",
            ip_address=ip,
            user_agent=ua,
            event_metadata={"session_id": str(session_id)},
        )
    )
    await context.session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/settings/security/events", response_model=list[SecurityEventResponse])
async def security_events(
    context: CurrentContext,
) -> list[SecurityEventResponse]:
    rows = (
        (
            await context.session.execute(
                select(SecurityEvent)
                .where(SecurityEvent.actor_user_id == context.user.id)
                .order_by(SecurityEvent.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [
        SecurityEventResponse(
            id=row.id,
            event_type=row.event_type,
            ip_address=row.ip_address,
            created_at=row.created_at,
            metadata=row.event_metadata,
        )
        for row in rows
    ]


@app.get("/api/settings/company", response_model=CompanySettingsResponse)
async def company_settings(
    context: CurrentContext,
) -> CompanySettingsResponse:
    org = await context.session.get(Organization, context.scope.org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    departments = (
        (
            await context.session.execute(
                select(Department).where(Department.org_id == context.scope.org_id)
            )
        )
        .scalars()
        .all()
    )
    return CompanySettingsResponse(
        id=org.id,
        name=org.name,
        login_slug=org.login_slug,
        duplicate_call_protection=org.duplicate_call_protection,
        onboarding_state=org.onboarding_state,
        department_count=len(departments),
    )


@app.patch("/api/settings/company", response_model=CompanySettingsResponse)
async def update_company_settings(
    payload: CompanyUpdateRequest,
    context: CurrentContext,
) -> CompanySettingsResponse:
    await _require_admin(context)
    org = await context.session.get(Organization, context.scope.org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    if payload.name is not None:
        org.name = payload.name
    if payload.duplicate_call_protection is not None:
        org.duplicate_call_protection = payload.duplicate_call_protection
    await context.session.flush()
    return await company_settings(context)


@app.get("/api/settings/team", response_model=list[TeamMemberResponse])
async def team_members(
    context: CurrentContext,
) -> list[TeamMemberResponse]:
    memberships = (
        (
            await context.session.execute(
                select(Membership).where(Membership.org_id == context.scope.org_id)
            )
        )
        .scalars()
        .all()
    )
    result: list[TeamMemberResponse] = []
    for membership in memberships:
        user = await context.session.get(AppUser, membership.user_id)
        if user is None:
            continue
        result.append(
            TeamMemberResponse(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=membership.role,
                is_active=user.is_active,
                created_at=user.created_at,
            )
        )
    return result


@app.get("/api/settings/billing", response_model=BillingSettingsResponse)
async def billing_settings(
    context: CurrentContext,
) -> BillingSettingsResponse:
    del context
    return BillingSettingsResponse()


@app.get("/api/settings/integrations", response_model=list[IntegrationResponse])
async def integrations(
    context: CurrentContext,
) -> list[IntegrationResponse]:
    rows = (
        (
            await context.session.execute(
                select(Integration)
                .where(Integration.org_id == context.scope.org_id)
                .order_by(Integration.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        IntegrationResponse(
            id=row.id,
            provider=row.provider,
            kind=row.kind,
            external_account_id=row.external_account_id,
            status=row.status,
            created_at=row.created_at,
        )
        for row in rows
    ]


# -- CSV import ------------------------------------------------------------


@app.post("/api/contacts/import/preview", response_model=ContactImportPreviewResponse)
async def contact_import_preview(
    payload: ContactImportPreviewRequest,
    context: CurrentContext,
) -> ContactImportPreviewResponse:
    """Column mapping inference + dedupe preview without persisting anything."""

    del context
    row_models = [
        ContactImportPreviewRow(index=row.index, values=row.values) for row in payload.rows
    ]
    estimated = preview_import(columns=payload.columns, rows=row_models)
    return ContactImportPreviewResponse(
        mapping=estimated.mapping,
        column_count=len(payload.columns),
        row_count=len(payload.rows),
        created_estimate=estimated.created_estimate,
        merged_estimate=estimated.merged_estimate,
        skipped_estimate=estimated.skipped_estimate,
        notes=estimated.notes,
    )


@app.post(
    "/api/contacts/import",
    response_model=ContactImportSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def contact_import_commit(
    payload: ContactImportCommitRequest,
    context: CurrentContext,
) -> ContactImportSummaryResponse:
    """Commit a CSV import: dedupe by email/phone, create contacts, record summary."""

    row_models = [
        ContactImportPreviewRow(index=row.index, values=row.values) for row in payload.rows
    ]
    created = 0
    merged = 0
    skipped = 0
    seen_keys: set[str] = set()
    for row in row_models:
        keys = dedupe_keys_for_row(row.values, payload.mapping)
        if not keys:
            skipped += 1
            continue
        if seen_keys & keys:
            merged += 1
            seen_keys.update(keys)
            continue
        existing = await _find_contact_by_keys(context, keys)
        if existing is not None:
            merged += 1
            seen_keys.update(keys)
            continue
        seen_keys.update(keys)
        contact = Contact(
            id=uuid4(),
            org_id=context.scope.org_id,
            department_id=context.scope.department_id,
            display_name=display_name_for_row(row.values, payload.mapping),
            first_name=_cell_by_name(row.values, payload.mapping.first_name) or None,
            last_name=_cell_by_name(row.values, payload.mapping.last_name) or None,
            job_title=_cell_by_name(row.values, payload.mapping.job_title) or None,
        )
        context.session.add(contact)
        email = _cell_by_name(row.values, payload.mapping.email)
        if email:
            context.session.add(
                ContactEmail(
                    id=uuid4(),
                    org_id=context.scope.org_id,
                    department_id=context.scope.department_id,
                    contact_id=contact.id,
                    email=email.strip().lower(),
                    is_primary=True,
                )
            )
        phone = _cell_by_name(row.values, payload.mapping.phone)
        if phone:
            digits = re.sub(r"[^\d]", "", phone)
            context.session.add(
                ContactPhone(
                    id=uuid4(),
                    org_id=context.scope.org_id,
                    department_id=context.scope.department_id,
                    contact_id=contact.id,
                    phone_e164=f"+{digits}" if digits else phone,
                    is_primary=True,
                )
            )
        created += 1
    import_record = ContactImport(
        id=uuid4(),
        org_id=context.scope.org_id,
        department_id=context.scope.department_id,
        filename=payload.filename,
        column_count=len(payload.columns),
        row_count=len(payload.rows),
        created_count=created,
        merged_count=merged,
        skipped_count=skipped,
        created_by_user_id=context.user.id,
    )
    context.session.add(import_record)
    context.session.add(
        _activity_row(
            scope=context.scope,
            actor_user_id=context.user.id,
            action="contacts_imported",
            entity_type="contact_import",
            entity_id=import_record.id,
            activity_metadata={
                "filename": payload.filename,
                "created": created,
                "merged": merged,
                "skipped": skipped,
            },
        )
    )
    await context.session.flush()
    return ContactImportSummaryResponse(
        id=import_record.id,
        filename=import_record.filename,
        column_count=import_record.column_count,
        row_count=import_record.row_count,
        created_count=import_record.created_count,
        merged_count=import_record.merged_count,
        skipped_count=import_record.skipped_count,
        created_at=import_record.created_at,
    )


async def _find_contact_by_keys(
    context: AuthenticatedContext, keys: set[str]
) -> Contact | None:
    for key in keys:
        if key.startswith("email:"):
            email_value = key.split(":", 1)[1]
            row = await context.session.scalar(
                select(ContactEmail).where(ContactEmail.email == email_value)
            )
            if row is not None:
                return await context.session.get(Contact, row.contact_id)
        elif key.startswith("phone:"):
            phone_value = key.removeprefix("phone:")
            row = await context.session.scalar(
                select(ContactPhone).where(ContactPhone.phone_e164 == phone_value)
            )
            if row is not None:
                return await context.session.get(Contact, row.contact_id)
    return None


def _cell_by_name(row_values: dict[str, str], column: str | None) -> str:
    if not column:
        return ""
    value = row_values.get(column)
    if value is None:
        value = row_values.get(str(column))
    return (value or "").strip()


# -- onboarding ------------------------------------------------------------


@app.get("/api/onboarding", response_model=OnboardingStateResponse)
async def onboarding_state(
    context: CurrentContext,
) -> OnboardingStateResponse:
    org = await context.session.get(Organization, context.scope.org_id)
    state = org.onboarding_state if org else "pending"
    return OnboardingStateResponse(state=state, next=_onboarding_next(state), message="")


@app.patch("/api/onboarding", response_model=OnboardingStateResponse)
async def set_onboarding_state(
    payload: OnboardingProgressRequest,
    context: CurrentContext,
) -> OnboardingStateResponse:
    org = await context.session.get(Organization, context.scope.org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    org.onboarding_state = payload.state
    await context.session.flush()
    return OnboardingStateResponse(
        state=payload.state, next=_onboarding_next(payload.state), message=""
    )


def _onboarding_next(state: str) -> str | None:
    return {
        "pending": "instructions",
        "instructions": "import",
        "import": "invite",
        "invite": "complete",
        "complete": None,
    }.get(state)
