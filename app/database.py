from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


@dataclass(frozen=True)
class TenantScope:
    org_id: UUID
    department_id: UUID


async def set_tenant_scope(session: AsyncSession, scope: TenantScope) -> None:
    """Set transaction-local Postgres RLS context before any tenant query."""

    await session.execute(
        text("SELECT set_config('app.org_id', :org_id, true)"),
        {"org_id": str(scope.org_id)},
    )
    await session.execute(
        text("SELECT set_config('app.department_id', :department_id, true)"),
        {"department_id": str(scope.department_id)},
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
