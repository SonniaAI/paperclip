"""Provider-neutral CRM boundary.

The product model is canonical.  A connector can be added later without
leaking provider-specific objects into contacts, calls, or tasks.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID


class CRMAdapter(Protocol):
    async def getContact(self, contact_id: UUID) -> dict[str, Any] | None: ...

    async def searchContacts(self, query: str) -> list[dict[str, Any]]: ...

    async def upsertContact(self, contact: dict[str, Any]) -> dict[str, Any]: ...

    async def logInteraction(self, contact_id: UUID, interaction: dict[str, Any]) -> None: ...

    async def createTask(self, contact_id: UUID, task: dict[str, Any]) -> dict[str, Any]: ...

    async def getOwner(self, contact_id: UUID) -> dict[str, Any] | None: ...


class NoopCRMAdapter:
    """Safe default while no external CRM connector is configured."""

    async def getContact(self, contact_id: UUID) -> dict[str, Any] | None:
        return None

    async def searchContacts(self, query: str) -> list[dict[str, Any]]:
        return []

    async def upsertContact(self, contact: dict[str, Any]) -> dict[str, Any]:
        return contact

    async def logInteraction(self, contact_id: UUID, interaction: dict[str, Any]) -> None:
        return None

    async def createTask(self, contact_id: UUID, task: dict[str, Any]) -> dict[str, Any]:
        return task

    async def getOwner(self, contact_id: UUID) -> dict[str, Any] | None:
        return None
